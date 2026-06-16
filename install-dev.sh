#!/usr/bin/env bash
# 安装/更新 **dev 链**：编译 --features dev 的 Rust 托盘，部署成独立后台服务，与 prod 完全并存。
#
# dev 链是纯 Rust（无 Python / run.sh / 迁移看门狗——那些是 Python→Rust 过渡件，只属 prod）。
# 身份与所有路径都按 APP_ID=claude-usage-indicator-dev 派生，和 prod 互不可见：
#   二进制  ~/.local/share/claude-usage-indicator-dev-bin/cui
#   服务    claude-usage-indicator-dev.service
#   配置    ~/.config/claude-usage-indicator-dev/
#   缓存    ~/.cache/claude-usage-indicator-dev/
#   托盘    独立 DBus id + 标签前缀 [dev]；更新通道走 `dev` 预发布（见 config.rs）
#
# 迭代开发更常用 `cargo run -p cui --features dev`（前台、实时日志、秒重启）；
# 本脚本用于把当前 dev 构建“升级成后台服务”，以便测试自启/自更新等完整生命周期。
# 卸载 dev：跑 dev 二进制的 `cui --uninstall`，或 `systemctl --user disable --now claude-usage-indicator-dev.service`。
#
# 构建工具链按需用环境变量覆盖：CARGO（默认 PATH 里的）、LIBCLANG_PATH、CMAKE。
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APP="claude-usage-indicator-dev"
BIN_DIR="$HOME/.local/share/${APP}-bin"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/$APP.service"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
CARGO="${CARGO:-cargo}"

echo "==> 编译 dev (--features dev --release)"
( cd "$REPO/rust" && "$CARGO" build -p cui --release --features dev )
SRC="$REPO/rust/target/release/cui"
"$SRC" --version | grep -q -- '-dev' \
  || { echo "❌ 编出来的不是 dev 构建（--version 末尾应带 -dev）。是不是漏了 --features dev / 被 prod 构建覆盖了？"; exit 1; }

echo "==> 部署二进制到 $BIN_DIR"
mkdir -p "$BIN_DIR"
install -m 0755 "$SRC" "$BIN_DIR/cui"

echo "==> 写入并启用 $APP.service"
mkdir -p "$UNIT_DIR"
# env -u 剔除会让动态库/解释器加载出错的污染变量（对齐 prod run.sh 的隔离，防 conda/LD 污染）。
cat > "$UNIT" <<UNIT
[Unit]
Description=Claude Usage Indicator (dev)
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/env -u LD_LIBRARY_PATH -u LD_PRELOAD -u PYTHONPATH -u PYTHONHOME $BIN_DIR/cui
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$APP.service"

echo "✅ dev 链已上线：$("$BIN_DIR/cui" --version)   托盘多一个带 [dev] 前缀的图标。"
echo "   状态： systemctl --user status $APP.service"
echo "   卸载： '$BIN_DIR/cui' --uninstall   （只删 dev，不碰 prod）"
