#!/usr/bin/env bash
#
# Claude 用量指示器 一键卸载
#
#   curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash
#
# 默认保留系统库（可能被别的程序用）。加 --purge 连配置目录(config.json)一起清，并提示如何手动移除系统库。
# 诊断数据在安装目录内，默认就会一并删除。
# 用管道运行时传参：curl ... | bash -s -- --purge
#
set -uo pipefail

APP="claude-usage-indicator"
INSTALL_DIR="${HOME}/.local/share/${APP}"
BIN="${HOME}/.local/bin/${APP}"
SERVICE="${HOME}/.config/systemd/user/${APP}.service"
CONFIG_DIR="${HOME}/.config/${APP}"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

log() { printf '\033[1;34m[uninstall]\033[0m %s\n' "$*"; }

log "停止并禁用服务..."
systemctl --user stop "$APP.service" 2>/dev/null || true
systemctl --user disable "$APP.service" 2>/dev/null || true
rm -f "$SERVICE"

# 迁移产物（Rust 期）：回退看门狗定时器、语言开关哨兵、Rust 二进制兄弟目录、迁移状态/缓存。
# 迁移前这些大多不存在，删起来无副作用；保证 Python 或 Rust 任一形态都被彻底清掉。
systemctl --user disable --now "${APP}-watchdog.timer" 2>/dev/null || true
rm -f "${HOME}/.config/systemd/user/${APP}-watchdog.timer" \
      "${HOME}/.config/systemd/user/${APP}-watchdog.service" \
      "${HOME}/.config/${APP}/use-rust"
rm -rf "${HOME}/.local/share/${APP}-bin" \
       "${HOME}/.local/state/${APP}" \
       "${HOME}/.cache/${APP}"
systemctl --user daemon-reload 2>/dev/null || true

log "删除程序文件（诊断数据在安装目录内，一并删除）..."
rm -rf "$INSTALL_DIR"
rm -f "$BIN"
# 清理 install.sh 升级时可能留下的备份目录
for bak in "${INSTALL_DIR}".bak.*; do
  [ -e "$bak" ] && rm -rf "$bak"
done

if [ "$PURGE" = 1 ]; then
  log "清除配置目录（config.json）..."
  rm -rf "$CONFIG_DIR"
  echo
  echo "  系统库默认保留（可能被其他程序使用）。若确认要移除，可手动执行："
  echo "    sudo apt-get remove gir1.2-appindicator3-0.1 gir1.2-notify-0.7 libnotify-bin"
elif [ -d "$CONFIG_DIR" ]; then
  echo "  配置已保留：$CONFIG_DIR（语言等设置）。要一并删除请加 --purge："
  echo "    curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash -s -- --purge"
fi

log "卸载完成。"
