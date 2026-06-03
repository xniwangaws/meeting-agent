#!/bin/bash
# Meeting Agent 启动脚本
#   ./run.sh             英文 → 中文 (默认)
#   ./run.sh --lang ja   日文 → 中文
#   ./run.sh ja          快捷写法,等于 --lang ja
cd "$(dirname "$0")"
# Configure your AWS profile / region for Bedrock here, or set in your shell.
export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

if [ "$1" = "ja" ] || [ "$1" = "en" ]; then
    LANG_ARG="$1"
    shift
    exec python3 agent.py --lang "$LANG_ARG" "$@"
else
    exec python3 agent.py "$@"
fi
