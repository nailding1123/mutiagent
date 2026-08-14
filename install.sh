#!/bin/sh

# MultiAgent one-click installer for macOS and Linux.
# It copies the application into an isolated user installation and exposes the
# single `multiagent` command through ~/.local/bin. No network access is needed.

set -eu

SCRIPT_DIR=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$SCRIPT_DIR
INSTALL_ROOT=${MULTIAGENT_INSTALL_ROOT:-"$HOME/.local/share/multiagent"}
VENV_DIR=${MULTIAGENT_VENV_DIR:-"$INSTALL_ROOT/venv"}
BIN_DIR=${MULTIAGENT_BIN_DIR:-"$HOME/.local/bin"}
ADD_PATH=1
INSTALL_AGENTS=0

usage() {
    cat <<'EOF'
用法：./install.sh [--install-agents] [--no-path]

选项：
  --install-agents  通过 npm 安装缺失的 Claude Code 和 Codex CLI。
  --no-path  不修改 shell 配置，只安装到本地虚拟环境和命令目录。

环境变量：
  PYTHON_BIN             指定 Python 可执行文件。
  MULTIAGENT_INSTALL_ROOT 指定用户安装目录。
  MULTIAGENT_VENV_DIR   指定虚拟环境目录。
  MULTIAGENT_BIN_DIR    指定 multiagent 命令目录。
EOF
}

for argument in "$@"; do
    case "$argument" in
        --install-agents) INSTALL_AGENTS=1 ;;
        --no-path) ADD_PATH=0 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "错误：未知参数 $argument" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -n "${PYTHON_BIN:-}" ]; then
    PYTHON=$PYTHON_BIN
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
    PYTHON=$(command -v python)
else
    echo "错误：找不到 Python 3。请先安装 Python 3.9 或更高版本。" >&2
    exit 2
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    echo "错误：$PYTHON 版本低于 Python 3.9。" >&2
    exit 2
fi

echo "使用 Python：$PYTHON"
echo "项目目录：$PROJECT_DIR"
echo "创建或复用虚拟环境：$VENV_DIR"

mkdir -p "$INSTALL_ROOT"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
SITE_PACKAGES=$("$VENV_PYTHON" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
PACKAGE_TARGET="$SITE_PACKAGES/multiagent_cli"

"$VENV_PYTHON" - "$PROJECT_DIR/multiagent_cli" "$PACKAGE_TARGET" <<'PY'
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
if not (source / "__init__.py").is_file():
    raise SystemExit(f"MultiAgent 程序目录不存在：{source}")
if target.exists():
    shutil.rmtree(target)
shutil.copytree(
    source,
    target,
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
)
PY

mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/multiagent"

if [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; then
    if [ -L "$LAUNCHER" ]; then
        TARGET=$(readlink "$LAUNCHER" || true)
        case "$TARGET" in
            "$PROJECT_DIR"/*|*/multiagent/.venv/bin/multiagent) rm -f "$LAUNCHER" ;;
            *)
                echo "错误：$LAUNCHER 已存在且不是 MultiAgent 安装入口。" >&2
                echo "如需覆盖，请先手动移除它，或设置 MULTIAGENT_BIN_DIR。" >&2
                exit 3
                ;;
        esac
    elif grep -q "MultiAgent installer managed" "$LAUNCHER" 2>/dev/null; then
        rm -f "$LAUNCHER"
    else
        echo "错误：$LAUNCHER 已存在且不是 MultiAgent 安装入口。" >&2
        echo "如需覆盖，请先手动移除它，或设置 MULTIAGENT_BIN_DIR。" >&2
        exit 3
    fi
fi

"$VENV_PYTHON" - "$LAUNCHER" "$VENV_PYTHON" <<'PY'
from pathlib import Path
import shlex
import sys

launcher = Path(sys.argv[1])
python = sys.argv[2]
launcher.write_text(
    "#!/bin/sh\n"
    "# MultiAgent installer managed\n"
    f"exec {shlex.quote(python)} -m multiagent_cli.web_launcher \"$@\"\n",
    encoding="utf-8",
)
launcher.chmod(0o755)
PY

PATH_NOTE=""
if [ "$ADD_PATH" -eq 1 ]; then
    case "${SHELL:-}" in
        */zsh) SHELL_RC="$HOME/.zshrc" ;;
        */fish) SHELL_RC="$HOME/.config/fish/config.fish" ;;
        *) SHELL_RC="$HOME/.bashrc" ;;
    esac
    mkdir -p "$(dirname -- "$SHELL_RC")"
    touch "$SHELL_RC"
    if ! grep -Fq '# MultiAgent installer PATH' "$SHELL_RC" 2>/dev/null; then
        if [ "${SHELL:-}" = "${SHELL%/fish}" ]; then
            {
                printf '\n# MultiAgent installer PATH\n'
                printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"
            } >> "$SHELL_RC"
        else
            {
                printf '\n# MultiAgent installer PATH\n'
                printf 'fish_add_path "%s"\n' "$BIN_DIR"
            } >> "$SHELL_RC"
        fi
    fi
    PATH_NOTE="已将 $BIN_DIR 加入 $SHELL_RC；重新打开终端后生效。"
fi

"$LAUNCHER" --help >/dev/null

AGENT_INSTALL_FAILED=0
CLAUDE_MISSING=0
CODEX_MISSING=0
if ! command -v claude >/dev/null 2>&1; then
    CLAUDE_MISSING=1
fi
if ! command -v codex >/dev/null 2>&1; then
    CODEX_MISSING=1
fi

if [ "$CLAUDE_MISSING" -eq 0 ] && [ "$CODEX_MISSING" -eq 0 ]; then
    echo "已检测到 Claude Code 和 Codex CLI。"
elif [ "$INSTALL_AGENTS" -eq 0 ]; then
    if [ "$CLAUDE_MISSING" -eq 1 ]; then
        echo "提示：未检测到 Claude Code（claude）。"
    fi
    if [ "$CODEX_MISSING" -eq 1 ]; then
        echo "提示：未检测到 Codex CLI（codex）。"
    fi
    echo "如需自动安装缺失的 Agent，请重新运行：./install.sh --install-agents"
else
    if ! command -v npm >/dev/null 2>&1; then
        echo "错误：自动安装 Agent 需要 Node.js 和 npm。" >&2
        echo "请先安装 Node.js，再重新运行 ./install.sh --install-agents。" >&2
        AGENT_INSTALL_FAILED=1
    else
        if [ "$CLAUDE_MISSING" -eq 1 ]; then
            echo "正在安装 Claude Code 官方 npm 包…"
            if ! npm install --global @anthropic-ai/claude-code; then
                echo "错误：Claude Code 安装失败；不会自动使用 sudo 重试。" >&2
                AGENT_INSTALL_FAILED=1
            fi
        fi
        if [ "$CODEX_MISSING" -eq 1 ]; then
            echo "正在安装 Codex CLI 官方 npm 包…"
            if ! npm install --global @openai/codex; then
                echo "错误：Codex CLI 安装失败；不会自动使用 sudo 重试。" >&2
                AGENT_INSTALL_FAILED=1
            fi
        fi
        NPM_PREFIX=$(npm prefix --global 2>/dev/null || true)
        NPM_GLOBAL_BIN=${NPM_PREFIX:+"$NPM_PREFIX/bin"}
        if [ -n "$NPM_GLOBAL_BIN" ] && [ -d "$NPM_GLOBAL_BIN" ]; then
            case ":$PATH:" in
                *":$NPM_GLOBAL_BIN:"*) ;;
                *) PATH="$NPM_GLOBAL_BIN:$PATH"; export PATH ;;
            esac
            if [ "$ADD_PATH" -eq 1 ] && ! grep -Fq '# MultiAgent npm global PATH' "$SHELL_RC" 2>/dev/null; then
                if [ "${SHELL:-}" = "${SHELL%/fish}" ]; then
                    {
                        printf '\n# MultiAgent npm global PATH\n'
                        printf 'export PATH="%s:$PATH"\n' "$NPM_GLOBAL_BIN"
                    } >> "$SHELL_RC"
                else
                    {
                        printf '\n# MultiAgent npm global PATH\n'
                        printf 'fish_add_path "%s"\n' "$NPM_GLOBAL_BIN"
                    } >> "$SHELL_RC"
                fi
            fi
        fi
        hash -r 2>/dev/null || true
        if ! command -v claude >/dev/null 2>&1; then
            echo "错误：安装后仍找不到 claude 命令，请检查 npm 全局命令目录是否在 PATH 中。" >&2
            AGENT_INSTALL_FAILED=1
        fi
        if ! command -v codex >/dev/null 2>&1; then
            echo "错误：安装后仍找不到 codex 命令，请检查 npm 全局命令目录是否在 PATH 中。" >&2
            AGENT_INSTALL_FAILED=1
        fi
        if [ "$AGENT_INSTALL_FAILED" -eq 0 ]; then
            echo "Claude Code 与 Codex CLI 已准备完成。"
            echo "首次使用时请分别运行 claude 和 codex，按官方流程完成登录或授权。"
        fi
    fi
fi

echo ""
echo "MultiAgent 安装完成。"
echo "程序目录：$INSTALL_ROOT"
echo "命令入口：$LAUNCHER"
if [ -n "$PATH_NOTE" ]; then
    echo "$PATH_NOTE"
fi
echo "现在可以运行：multiagent"
if [ "$AGENT_INSTALL_FAILED" -ne 0 ]; then
    exit 4
fi
