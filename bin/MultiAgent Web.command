#!/bin/sh

SCRIPT_DIR=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")

if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=$(command -v python3)
else
    osascript -e 'display alert "MultiAgent Web" message "找不到 Python 3，请先安装 Python 3.9 或更高版本。" as critical'
    exit 1
fi

export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$PROJECT_DIR" || exit 1
nohup "$PYTHON_BIN" -m multiagent_cli.web_launcher >/tmp/multiagent-web.log 2>&1 &
exit 0
