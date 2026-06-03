"""Meeting Agent — 录音 + 实时翻译总结 + 截图汇总 + Q&A.

Usage:
    python3 agent.py                    # 启动 agent (默认 Bedrock Sonnet 4.6)
    python3 agent.py --model sonnet-4-5 # 改用其他 Sonnet
    python3 agent.py --no-translate     # 只录音不翻译

CLI 命令(运行时输入):
    /screenshot       触发截图 + OCR + 总结
    /ask <问题>       基于已转录内容提问(打断翻译流)
    /summary          打印当前 session 完整总结
    /save             保存 session 到 summaries/
    /pause            暂停翻译流
    /resume           恢复翻译流
    /quit             停止录音并退出
    其他文本          走 /ask
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import boto3

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

HERE = pathlib.Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "sessions"

WHISPER_BIN = os.environ.get("WHISPER_BIN", "/opt/homebrew/bin/whisper-stream")
WHISPER_MODEL = os.environ.get(
    "WHISPER_MODEL",
    "/opt/homebrew/Cellar/whisper-cpp/1.8.3/share/whisper-cpp/models/ggml-large-v3-turbo.bin",
)
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "1")  # macOS audio input device index

# Default Bedrock model. Override via --model or BEDROCK_MODEL env var.
DEFAULT_MODEL = os.environ.get("BEDROCK_MODEL", "anthropic.claude-sonnet-4-5-20250929-v1:0")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "default")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

TRANSLATE_INTERVAL_SEC = 10  # 多久翻译一次新增的转录内容
CONTEXT_TAIL_LINES = 80  # /ask 时给 LLM 的上下文行数

DESKTOP_DIR = pathlib.Path.home() / "Desktop"
SCREENSHOT_WATCH_INTERVAL = 2  # 秒;watch Desktop 间隔
SCREENSHOT_NAME_PREFIXES = ("Screenshot ", "Screen Shot ", "屏幕快照 ", "截屏")


# ---------------------------------------------------------------------------
# Bedrock client
# ---------------------------------------------------------------------------


def make_bedrock_client(profile: str | None = None, region: str | None = None):
    session = boto3.Session(
        profile_name=profile or AWS_PROFILE,
        region_name=region or AWS_REGION,
    )
    return session.client("bedrock-runtime")


def call_sonnet(client, model_id: str, messages: list[dict], system: str | None = None,
                max_tokens: int = 1024) -> str:
    body: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        body["system"] = system
    resp = client.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(resp["body"].read())
    return "".join(blk.get("text", "") for blk in payload.get("content", []))


# ---------------------------------------------------------------------------
# Transcript watcher
# ---------------------------------------------------------------------------


class TranscriptState:
    """跟踪 whisper-stream 文件的进度,提取 dedup 后的新内容。"""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.last_line_count = 0
        self.lock = threading.Lock()

    def read_new_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        with self.lock:
            text = self.path.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            if len(lines) <= self.last_line_count:
                return []
            new = lines[self.last_line_count:]
            self.last_line_count = len(lines)
            return new

    def read_tail(self, n: int) -> list[str]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        return lines[-n:]


JA_HALLUCINATION_PHRASES = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "ご清聴ありがとうございました",
    "字幕by",
    "字幕 by",
    "チャンネル登録",
    "Thanks for watching",
)


def dedup_whisper_lines(lines: list[str]) -> str:
    """whisper-stream 每个新片段会重复前一个片段的尾巴 — 简单 dedup。"""
    cleaned: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # whisper 偶尔幻觉静音内容(英文)
        if line in ("Okay.", ".", "*sigh*", "Thank you.", "Amen.", "*Loud noise*"):
            continue
        if line.startswith("*") and line.endswith("*"):
            continue
        # 日文 whisper 常见幻觉
        if any(phrase in line for phrase in JA_HALLUCINATION_PHRASES):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# Whisper recording
# ---------------------------------------------------------------------------


def start_whisper(output_path: pathlib.Path, lang: str = "en") -> subprocess.Popen:
    cmd = [
        WHISPER_BIN,
        "-m", WHISPER_MODEL,
        "--step", "3000",
        "--length", "10000",
        "-t", "4",
        "--keep", "1000",
        "-c", WHISPER_DEVICE,
        "-l", lang,
        "-f", str(output_path),
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Screenshot + OCR
# ---------------------------------------------------------------------------


def take_screenshot(output_path: pathlib.Path) -> bool:
    """触发系统 screencapture,等用户框选区域。"""
    print("\n📸 框选屏幕区域(松开鼠标完成)...")
    result = subprocess.run(
        ["screencapture", "-i", "-x", str(output_path)],
        capture_output=True,
    )
    return result.returncode == 0 and output_path.exists()


class DesktopScreenshotWatcher(threading.Thread):
    """监听桌面新出现的 macOS 截图文件,自动搬到 session 目录 + LLM 总结。

    支持原生 Cmd+Shift+3/4/5 截图(默认存桌面)。
    """

    def __init__(self, session_dir: pathlib.Path, transcript: 'TranscriptState',
                 client, model_id: str, translation_log: pathlib.Path,
                 paused_event: threading.Event, stop_event: threading.Event,
                 output_queue: queue.Queue):
        super().__init__(daemon=True)
        self.session_dir = session_dir
        self.transcript = transcript
        self.client = client
        self.model_id = model_id
        self.translation_log = translation_log
        self.paused_event = paused_event
        self.stop_event = stop_event
        self.output_queue = output_queue
        # 记录启动时已存在的截图,只处理之后新增的
        self.seen: set[str] = set()
        if DESKTOP_DIR.exists():
            for p in DESKTOP_DIR.iterdir():
                if self._is_screenshot(p):
                    self.seen.add(p.name)

    @staticmethod
    def _is_screenshot(p: pathlib.Path) -> bool:
        if not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            return False
        return any(p.name.startswith(prefix) for prefix in SCREENSHOT_NAME_PREFIXES)

    def run(self):
        if not DESKTOP_DIR.exists():
            return
        while not self.stop_event.is_set():
            for _ in range(SCREENSHOT_WATCH_INTERVAL):
                if self.stop_event.is_set():
                    return
                time.sleep(1)
            try:
                for p in DESKTOP_DIR.iterdir():
                    if not self._is_screenshot(p) or p.name in self.seen:
                        continue
                    # 等文件写完(macOS 截图会有短暂的 .png 写入延迟)
                    time.sleep(0.5)
                    self.seen.add(p.name)
                    self._handle_new_screenshot(p)
            except Exception as e:
                self.output_queue.put(("error", f"截图监听: {e}"))

    def _handle_new_screenshot(self, src: pathlib.Path):
        ts = dt.datetime.now().strftime("%H%M%S")
        dest = self.session_dir / f"shot_{ts}_{src.name}"
        try:
            shutil.move(str(src), str(dest))
        except Exception:
            try:
                shutil.copy2(str(src), str(dest))
            except Exception as e:
                self.output_queue.put(("error", f"截图搬运失败: {e}"))
                return
        self.output_queue.put(("info", f"📸 截图入库: {dest.name}"))
        self.paused_event.set()
        try:
            ctx = dedup_whisper_lines(self.transcript.read_tail(40))
            summary = summarize_screenshot(self.client, self.model_id, dest, ctx)
            entry = f"\n[截图 {dest.name}]\n{summary}\n"
            with self.translation_log.open("a", encoding="utf-8") as f:
                f.write(entry)
            self.output_queue.put(("translation", entry))
        except Exception as e:
            self.output_queue.put(("error", f"截图分析: {e}"))
        finally:
            self.paused_event.clear()


def summarize_screenshot(client, model_id: str, image_path: pathlib.Path,
                         transcript_context: str) -> str:
    import base64
    img_bytes = image_path.read_bytes()
    img_b64 = base64.standard_b64encode(img_bytes).decode("ascii")
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    messages = [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": img_b64},
            },
            {
                "type": "text",
                "text": (
                    "这是一张会议截图。简洁中文总结画面内容(图表/diagram/聊天/代码 都可能)。"
                    "如果是流程图或架构图,描述节点和关键路径。如果是文字,提取要点。"
                    f"\n\n最近会议转录上下文(供你判断截图相关性):\n{transcript_context}\n\n"
                    "输出格式:\n标题: <一句话主题>\n内容: <3-5 行要点>\n关联: <这张图和上下文怎么关联,1 行>"
                ),
            },
        ],
    }]
    return call_sonnet(client, model_id, messages, max_tokens=800)


# ---------------------------------------------------------------------------
# Translation worker
# ---------------------------------------------------------------------------


class TranslateWorker(threading.Thread):
    def __init__(self, transcript: TranscriptState, client, model_id: str,
                 out_log: pathlib.Path, paused_event: threading.Event,
                 stop_event: threading.Event, output_queue: queue.Queue,
                 source_lang: str = "en", participants_text: str = ""):
        super().__init__(daemon=True)
        self.transcript = transcript
        self.client = client
        self.model_id = model_id
        self.out_log = out_log
        self.paused_event = paused_event
        self.stop_event = stop_event
        self.output_queue = output_queue
        self.source_lang = source_lang
        self.participants_text = participants_text
        self.out_log.write_text("", encoding="utf-8")

    def run(self):
        while not self.stop_event.is_set():
            for _ in range(TRANSLATE_INTERVAL_SEC):
                if self.stop_event.is_set():
                    return
                time.sleep(1)
            if self.paused_event.is_set():
                continue
            new_lines = self.transcript.read_new_lines()
            if not new_lines:
                continue
            chunk = dedup_whisper_lines(new_lines)
            if not chunk or len(chunk) < 20:
                continue
            try:
                summary = self.translate_chunk(chunk)
            except Exception as e:
                self.output_queue.put(("error", f"翻译失败: {e}"))
                continue
            ts = dt.datetime.now().strftime("%H:%M:%S")
            entry = f"\n[{ts}]\n{summary}\n"
            with self.out_log.open("a", encoding="utf-8") as f:
                f.write(entry)
            self.output_queue.put(("translation", entry))

    def translate_chunk(self, chunk: str) -> str:
        if self.source_lang == "ja":
            system = (
                "你是会议实时翻译。把日文会议转录翻译成详细的中文。\n"
                "\n"
                "**翻译规则**:\n"
                "1. **逐句翻译,不要总结、不要省略、不要合并**\n"
                "2. 保留专有名词原文(人名、产品名、技术术语)\n"
                "3. whisper 同句被重复输出多次(尾巴重叠)→ 只翻译一次\n"
                "4. 「えーと/あの/まあ/はい」等日语填充词略过\n"
                "5. 不加 bullet 不加序号,直接每行一句\n"
                "6. 「ご視聴ありがとうございました」「字幕」等明显是 whisper 静音幻觉 → 跳过\n"
            )
        else:
            participants_block = self.participants_text or (
                "  (no participants context provided — copy "
                "`participants.example.txt` to `participants.txt` and edit "
                "it to give the model speaker hints)"
            )
            system = (
                "你是会议实时翻译。把英文会议转录翻译成详细的中文。\n"
                "\n"
                f"**会议参与者**(基于发言风格/内容/立场猜说话人):\n{participants_block}\n"
                "\n"
                "**翻译规则**:\n"
                "1. **逐句翻译,不要总结、不要省略、不要合并**\n"
                "2. **每行格式**:`[说话人?] 中文翻译` —— 不确定加 `?`,完全猜不出就不加 prefix\n"
                "3. 保留专有名词原文(人名、产品、技术术语)\n"
                "4. whisper 同句被重复输出多次(尾巴重叠)→ 只翻译一次\n"
                "5. 'yeah/okay/right/I mean' 等填充词略过\n"
                "6. 不加 bullet 不加序号,直接每行一句\n"
                "\n"
                "示例输出:\n"
                "```\n"
                "[Alice] schema 应该只有 6-7 个 field,保持简单。\n"
                "[Bob] 我同意,所有 logic 都该在配置层,不是 schema。\n"
                "[Carol?] 那上游那边的字段怎么 map?\n"
                "```"
            )
        messages = [{"role": "user", "content": chunk}]
        return call_sonnet(self.client, self.model_id, messages, system=system, max_tokens=2000)


# ---------------------------------------------------------------------------
# Main interactive loop
# ---------------------------------------------------------------------------


def print_output_loop(output_queue: queue.Queue, stop_event: threading.Event):
    """后台线程,把翻译结果异步打到终端,不阻塞用户输入。"""
    while not stop_event.is_set():
        try:
            kind, payload = output_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if kind == "translation":
            sys.stdout.write(f"\r\033[K{payload}\n> ")
            sys.stdout.flush()
        elif kind == "info":
            sys.stdout.write(f"\r\033[K{payload}\n> ")
            sys.stdout.flush()
        elif kind == "error":
            sys.stdout.write(f"\r\033[K⚠️  {payload}\n> ")
            sys.stdout.flush()


def handle_ask(question: str, transcript: TranscriptState, client, model_id: str) -> str:
    tail = transcript.read_tail(CONTEXT_TAIL_LINES)
    context = dedup_whisper_lines(tail)
    if not context:
        return "(还没有转录内容)"
    system = (
        "你是会议助手。基于以下英文会议转录回答用户问题(中文回答)。"
        "如果转录里没有相关信息,直接说'转录里没提到'。简洁,不要凑字数。"
    )
    messages = [{
        "role": "user",
        "content": f"会议转录(最近 {len(tail)} 行):\n{context}\n\n用户问题: {question}",
    }]
    return call_sonnet(client, model_id, messages, system=system, max_tokens=800)


def handle_summary(transcript: TranscriptState, translation_log: pathlib.Path,
                   client, model_id: str) -> str:
    full = translation_log.read_text(encoding="utf-8") if translation_log.exists() else ""
    if not full.strip():
        return "(暂无翻译内容)"
    system = "把以下零散翻译片段汇总成一份完整的中文会议纪要。包含:1) 核心议题 2) 关键决策 3) Action items 4) 未决问题。"
    messages = [{"role": "user", "content": full[-30000:]}]  # 限长
    return call_sonnet(client, model_id, messages, system=system, max_tokens=2500)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=DEFAULT_MODEL, help="Bedrock model id")
    p.add_argument("--no-translate", action="store_true", help="只录音不翻译")
    p.add_argument("--no-screenshot-watch", action="store_true", help="关闭桌面截图自动监听")
    p.add_argument("--profile", default=AWS_PROFILE)
    p.add_argument("--lang", default="en", choices=["en", "ja"],
                   help="源语言 (en=英文→中文, ja=日文→中文)")
    args = p.parse_args()

    if not shutil.which(WHISPER_BIN) and not pathlib.Path(WHISPER_BIN).exists():
        print(f"❌ whisper-stream 未找到: {WHISPER_BIN}")
        return 1
    if not pathlib.Path(WHISPER_MODEL).exists():
        print(f"❌ whisper model 未找到: {WHISPER_MODEL}")
        return 1

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    # 每次会议自己一个 session 目录,所有产物都在里面
    session_dir = HERE / "sessions" / f"meeting_{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = session_dir / "transcript.txt"
    translation_log = session_dir / "translation.md"
    session_screenshots = session_dir / "screenshots"
    session_screenshots.mkdir(parents=True, exist_ok=True)

    # Optional participants context (gitignored). Improves speaker attribution.
    participants_path = HERE / "participants.txt"
    participants_text = ""
    if participants_path.exists():
        participants_text = participants_path.read_text(encoding="utf-8").strip()

    print(f"🎙️  Meeting Agent 启动")
    print(f"    Session:     {session_dir}")
    print(f"    Model:       {args.model}")
    print(f"    Lang:        {args.lang} → zh")
    if participants_text:
        print(f"    Participants: loaded from {participants_path.name}")
    print()

    # Start whisper
    whisper_proc = start_whisper(transcript_path, lang=args.lang)
    print(f"✅ 录音中(PID {whisper_proc.pid})")

    # Bedrock client
    client = make_bedrock_client(profile=args.profile)

    # State
    transcript = TranscriptState(transcript_path)
    output_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    paused_event = threading.Event()

    if not args.no_translate:
        worker = TranslateWorker(
            transcript, client, args.model, translation_log,
            paused_event, stop_event, output_queue,
            source_lang=args.lang,
            participants_text=participants_text,
        )
        worker.start()
        print(f"✅ 自动翻译开启(每 {TRANSLATE_INTERVAL_SEC} 秒)")

    # 后台 printer 总是要的,即使 --no-translate(截图监听也用它)
    printer = threading.Thread(
        target=print_output_loop, args=(output_queue, stop_event), daemon=True,
    )
    printer.start()

    # 监听 macOS 桌面截图(Cmd+Shift+3/4/5 默认存桌面)
    if not args.no_screenshot_watch:
        watcher = DesktopScreenshotWatcher(
            session_screenshots, transcript, client, args.model,
            translation_log, paused_event, stop_event, output_queue,
        )
        watcher.start()
        print(f"✅ 桌面截图监听开启(Cmd+Shift+4 截图自动入库)")

    print()
    print("命令: /screenshot  /ask <q>  /summary  /save  /pause  /resume  /quit")
    print("(直接输入文字也会被当作 /ask 问)")
    print()

    def cleanup():
        stop_event.set()
        try:
            whisper_proc.terminate()
            whisper_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            whisper_proc.kill()

    signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(0)))

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue

            if line in ("/quit", "/exit", "/q"):
                break
            elif line.startswith("/screenshot"):
                shot_path = session_screenshots / f"manual_{dt.datetime.now().strftime('%H%M%S')}.png"
                if take_screenshot(shot_path):
                    print(f"📸 已保存: {shot_path}")
                    print("🔍 正在 OCR + 总结...")
                    paused_event.set()
                    try:
                        ctx = dedup_whisper_lines(transcript.read_tail(40))
                        result = summarize_screenshot(client, args.model, shot_path, ctx)
                        print(f"\n{result}\n")
                        with translation_log.open("a", encoding="utf-8") as f:
                            f.write(f"\n[截图 {shot_path.name}]\n{result}\n")
                    except Exception as e:
                        print(f"⚠️  截图分析失败: {e}")
                    finally:
                        paused_event.clear()
                else:
                    print("📸 取消")
            elif line.startswith("/ask "):
                question = line[5:].strip()
                if question:
                    paused_event.set()
                    try:
                        ans = handle_ask(question, transcript, client, args.model)
                        print(f"\n{ans}\n")
                    except Exception as e:
                        print(f"⚠️  {e}")
                    finally:
                        paused_event.clear()
            elif line == "/summary":
                paused_event.set()
                try:
                    s = handle_summary(transcript, translation_log, client, args.model)
                    print(f"\n{s}\n")
                except Exception as e:
                    print(f"⚠️  {e}")
                finally:
                    paused_event.clear()
            elif line == "/save":
                final_path = session_dir / "final_summary.md"
                paused_event.set()
                try:
                    s = handle_summary(transcript, translation_log, client, args.model)
                    final_path.write_text(s, encoding="utf-8")
                    print(f"✅ 保存到 {final_path}")
                finally:
                    paused_event.clear()
            elif line == "/pause":
                paused_event.set()
                print("⏸️  翻译已暂停(录音继续)")
            elif line == "/resume":
                paused_event.clear()
                print("▶️  翻译已恢复")
            else:
                # 默认当 /ask
                paused_event.set()
                try:
                    ans = handle_ask(line, transcript, client, args.model)
                    print(f"\n{ans}\n")
                except Exception as e:
                    print(f"⚠️  {e}")
                finally:
                    paused_event.clear()
    finally:
        cleanup()
        print("\n🛑 录音已停止")
        print(f"📁 Session: {session_dir}")
        print(f"   ├── transcript.txt       (whisper 原文)")
        print(f"   ├── translation.md       (中文翻译片段)")
        print(f"   ├── final_summary.md     (/save 生成)")
        if any(session_screenshots.iterdir()):
            n = sum(1 for _ in session_screenshots.iterdir())
            print(f"   └── screenshots/         ({n} 张图)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
