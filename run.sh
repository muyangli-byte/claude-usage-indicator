#!/usr/bin/env bash
# 可重定位 + 运行环境隔离。
# 无论用户的 shell 里有 conda/pyenv、自定义 PATH、PYTHONPATH/PYTHONHOME、LD_LIBRARY_PATH/LD_PRELOAD
# 等任何定制，这里都剔除会让 Python「加载到错误代码 / 错误动态库」的变量，并用确定的系统 PATH，
# 保证所有人的运行环境完全一致（conda libffi 崩溃就是这类污染导致的）。
# 保留 DBus / 显示 / XDG_RUNTIME_DIR 等会话变量——读浏览器 keyring、画托盘图标、弹通知都要用。
# 这是服务和 `claude-usage-indicator` 命令的唯一入口，隔离只需在此一处做。
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR"

# 剔除会影响 Python 模块/动态库加载的变量
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE \
      LD_LIBRARY_PATH LD_PRELOAD \
      VIRTUAL_ENV CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PYTHON_EXE
export PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1
# 确定的系统 PATH：子进程（git / xdg-open / systemctl / systemd-run）都走系统版本，不走 conda/pyenv
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

# ── 迁移语言开关（Python → Rust 无缝切换的载体）─────────────────────────────
# 若已迁移到 Rust——哨兵文件存在、且兄弟目录里的二进制能正常 `--version`——就运行 Rust；
# 否则照常运行 Python（永远可回落的安全分支）。二进制与哨兵都在本 git 树之外，
# `git reset --hard` / install.sh 的 .bak 备份都碰不到；二进制跑不起来时绝不切过去。
CUI_BIN="${HOME}/.local/share/claude-usage-indicator-bin/cui"
CUI_SENTINEL="${HOME}/.config/claude-usage-indicator/use-rust"
if [ -f "$CUI_SENTINEL" ] && [ -x "$CUI_BIN" ] && "$CUI_BIN" --version >/dev/null 2>&1; then
    exec "$CUI_BIN" "$@"     # current_exe()==$CUI_BIN，与自更新替换路径天然一致
fi
# ────────────────────────────────────────────────────────────────────────────

# 用 venv 内（由系统 Python 建的）解释器，绝对路径调用——不依赖 PATH，也无需 source activate
exec "$DIR/venv/bin/python" "$DIR/claude_usage_indicator.py" "$@"
