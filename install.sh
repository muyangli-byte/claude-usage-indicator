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
sudo apt-get update -y
sudo apt-get install -y \
  build-essential python3 python3-venv python3-dev \
  python3-gi gir1.2-gtk-3.0 gir1.2-appindicator3-0.1 gir1.2-notify-0.7 \
  libgirepository1.0-dev libcairo2-dev pkg-config \
  libnotify-bin xdg-utils git curl

# ---- 2. 拉取代码 ----
log "下载代码到 ${INSTALL_DIR} ..."
mkdir -p "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch --depth 1 origin main
  git -C "$INSTALL_DIR" reset --hard origin/main
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
if [ ! -d "$INSTALL_DIR/venv" ]; then
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
echo "  ▸ 约 30 秒后顶栏开始显示用量；若显示「⚠ 登录已过期」请到 Chrome 重新登录"
echo "  ▸ 常用命令："
echo "      $APP --check     # 检查更新"
echo "      $APP --update    # 更新到最新版"
echo "      $APP --once      # 拉取一次并打印（调试）"
echo "      systemctl --user status $APP.service"
echo "      journalctl --user -u $APP.service -f"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  ⚠ ${BIN_DIR} 不在 PATH，请把它加入 PATH 后才能直接用 '$APP' 命令";;
esac
