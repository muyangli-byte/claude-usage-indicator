#!/usr/bin/env bash
#
# Claude 用量指示器 一键安装（Debian/Ubuntu）
#
#   curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/install.sh | bash
#
# 可重复运行 —— 再跑一次就是更新到最新版。
#
set -euo pipefail

OWNER="muyangli-byte"
APP="claude-usage-indicator"
REPO_URL="https://github.com/${OWNER}/${APP}"
INSTALL_DIR="${HOME}/.local/share/${APP}"
BIN_DIR="${HOME}/.local/bin"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE="${SERVICE_DIR}/${APP}.service"

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; }

# ---- 0. 环境检查 ----
if ! command -v apt-get >/dev/null 2>&1; then
  err "目前仅支持 Debian/Ubuntu（apt）。其他发行版请手动安装依赖后运行 run.sh。"
  exit 1
fi

# ---- 1. 系统依赖（需要 sudo）----
log "安装系统依赖（需要 sudo 授权）..."
# 关键：把 apt 命令的 stdin 接到 /dev/null —— 否则用 `curl | bash` 安装时，全新机器上
# apt/debconf 配置包会读取 stdin，把管道里「剩余的脚本」吞掉，导致安装在 apt 之后被截断
# （clone/venv/pip 全都不执行，却以退出码 0 结束）。DEBIAN_FRONTEND + sudo -E 避免交互配置。
# apt-get update 容错：机器上常有无关的第三方源签名/网络出错，不该因此中断整个安装。
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y </dev/null || log "apt-get update 报错（可能是无关的第三方源），跳过更新、继续安装所需包..."
# AppIndicator GIR 包名因发行版而异：老的 gir1.2-appindicator3-0.1 vs 新的 Ayatana fork。
# 两者都提供运行所需的 AppIndicator3-0.1 typelib；选当前 apt 能装到的那个。
IND_PKG="gir1.2-appindicator3-0.1"
if ! apt-cache show "$IND_PKG" >/dev/null 2>&1; then
  IND_PKG="gir1.2-ayatanaappindicator3-0.1"
fi
log "AppIndicator 包: $IND_PKG"
sudo -E apt-get install -y \
  build-essential python3 python3-venv python3-dev \
  python3-gi gir1.2-gtk-3.0 "$IND_PKG" gir1.2-notify-0.7 \
  libgirepository1.0-dev libcairo2-dev pkg-config \
  libnotify-bin xdg-utils git curl </dev/null

# ---- 2. 拉取代码 ----
# 始终部署 GitHub 最新 main（clone 主要用于查看代码；运行副本独立在 INSTALL_DIR）。
log "部署 GitHub 最新 main 到 ${INSTALL_DIR}（注意：装的是最新 main，不是你本地的 clone）..."
mkdir -p "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch --depth 1 origin main
  git -C "$INSTALL_DIR" reset --hard origin/main   # reset 不依赖共同祖先，浅克隆安全
else
  if [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
    backup="${INSTALL_DIR}.bak.$(date +%s)"
    log "目录非空，备份旧内容到 ${backup}"
    mv "$INSTALL_DIR" "$backup"
  fi
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

# ---- 3. 虚拟环境 + Python 依赖 ----
log "创建虚拟环境并安装 Python 依赖..."
# venv 不存在、或 python 小版本升级后失效（symlink 悬空），都重建，让"重跑=更新"能自愈
if [ ! -d "$INSTALL_DIR/venv" ] || ! "$INSTALL_DIR/venv/bin/python" -c 'pass' >/dev/null 2>&1; then
  rm -rf "$INSTALL_DIR/venv"
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip wheel
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

chmod +x "$INSTALL_DIR/run.sh"

# ---- 4. 包装命令 claude-usage-indicator ----
log "安装命令 ${BIN_DIR}/${APP} ..."
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/$APP" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/claude_usage_indicator.py" "\$@"
EOF
chmod +x "$BIN_DIR/$APP"

# ---- 5. systemd 用户服务 ----
log "安装并启动 systemd 用户服务..."
mkdir -p "$SERVICE_DIR"
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$INSTALL_DIR/packaging/${APP}.service" > "$SERVICE"
# 没有用户级 systemd 会话时（SSH/headless）不要因 set -e 整个失败：降级为提示
if systemctl --user daemon-reload 2>/dev/null; then
  systemctl --user enable "$APP.service" >/dev/null 2>&1 || true
  systemctl --user restart "$APP.service" \
    || log "服务未能启动，稍后可手动：systemctl --user restart $APP.service"
else
  log "未检测到用户级 systemd 会话（SSH/headless？）。文件已装好，登录图形会话后运行："
  log "    systemctl --user enable --now $APP.service"
fi

# ---- 6. 收尾提示 ----
log "完成！当前版本 v$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '?')"
echo
echo "  ▸ 前置条件：在 Chrome 里登录 https://claude.ai（无需常开标签页）"
echo "  ▸ 几秒内顶栏开始显示用量；若显示「⚠ 登录已过期」请到 Chrome 重新登录"
echo "  ▸ 常用命令："
echo "      $APP --check        # 检查更新"
echo "      $APP --self-update  # 更新（无需 sudo，同托盘 Update now）"
echo "      $APP --update       # 更新（含系统库，需 sudo）"
echo "      $APP --once         # 拉取一次并打印（调试）"
echo "      systemctl --user status $APP.service"
echo "      journalctl --user -u $APP.service -f"
# 桌面环境提示：没有图形会话 / GNOME 缺扩展时，托盘图标不会出现（服务仍正常）
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "  ⚠ 当前无图形会话（DISPLAY/WAYLAND 为空）：托盘图标只在桌面会话显示；可先用 '$APP --once' 验证数据。"
elif printf '%s' "${XDG_CURRENT_DESKTOP:-}" | grep -qi gnome; then
  echo "  ▸ GNOME 桌面：托盘图标需 AppIndicator 扩展（Ubuntu 通常已默认启用）。若看不到图标："
  echo "      sudo apt-get install -y gnome-shell-extension-appindicator 然后注销重登"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo "  ⚠ ${BIN_DIR} 不在 PATH —— 顶栏托盘照常工作（systemd 用绝对路径启动），"
    echo "    但要直接用 '$APP …' 命令，请把这行加进 ~/.bashrc 再重开终端："
    echo "        export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo "    或本次用全路径：$BIN_DIR/$APP --check";;
esac
