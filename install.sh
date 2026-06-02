#!/usr/bin/env bash
#
# Claude Usage Indicator — guided installer (Debian/Ubuntu)
# Claude 用量指示器 —— 引导式安装（Debian/Ubuntu）
#
#   curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/install.sh | bash
#
# Interactive: asks language, checks the environment, installs dependencies, then verifies your
# claude.ai login (which browser/profile, sessionKey ok?) BEFORE activating the background service.
# Re-running it = update to the latest version.
#
# 交互式：先选语言、检查环境、装依赖，然后在「激活后台服务」前先验证 claude.ai 登录态
# （哪个浏览器/profile、sessionKey 是否拿到），再启动服务。重复运行 = 更新到最新版。
#
set -euo pipefail

OWNER="muyangli-byte"
APP="claude-usage-indicator"
REPO_URL="https://github.com/${OWNER}/${APP}"
INSTALL_DIR="${HOME}/.local/share/${APP}"
BIN_DIR="${HOME}/.local/bin"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE="${SERVICE_DIR}/${APP}.service"
PY="${INSTALL_DIR}/venv/bin/python"
SCRIPT="${INSTALL_DIR}/claude_usage_indicator.py"

# ---- 交互能力探测：curl|bash 时 stdin 是管道(脚本本身)，所以一律从 /dev/tty 读用户输入 ----
if [ -r /dev/tty ] && [ -w /dev/tty ]; then INTERACTIVE=1; else INTERACTIVE=0; fi

# ---- 语言 ----
case "${LANG:-}" in zh*) LC=zh;; *) LC=en;; esac
if [ "$INTERACTIVE" = 1 ]; then
  printf '\n  Choose language / 选择语言:\n    1) English\n    2) 中文\n  [default %s] > ' "$LC" > /dev/tty
  IFS= read -r _l < /dev/tty || _l=""
  case "$_l" in 1|e|E|en|EN) LC=en;; 2|z|Z|zh|ZH|中*) LC=zh;; *) : ;; esac
fi

# ---- 输出助手（双语；msg 支持 printf 占位符）----
log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; }
warn(){ printf '\033[1;33m[install]\033[0m %s\n' "$*"; }
msg() { local z="$1" e="$2"; shift 2; if [ "$LC" = zh ]; then printf "$z\n" "$@"; else printf "$e\n" "$@"; fi; }

# ================= 0. 环境检查（无 apt 硬退出；其余只警告）=================
msg "【1/5】检查环境…" "[1/5] Checking environment…"

if ! command -v apt-get >/dev/null 2>&1; then
  msg "✗ 没找到 apt：本安装脚本仅支持 Debian/Ubuntu 系。其他发行版请手动装依赖后用 run.sh 运行。" \
      "✗ apt not found: this installer supports Debian/Ubuntu only. On other distros install the deps manually and use run.sh." >&2
  exit 1
fi

DISTRO="$( . /etc/os-release 2>/dev/null && echo "${ID:-?} ${VERSION_ID:-}" )"
msg "  发行版：%s" "  Distro: %s" "$DISTRO"
case " $DISTRO " in
  *" ubuntu "*|*" debian "*|*" linuxmint "*|*" pop "*|*" elementary "*|*" zorin "*|*" neon "*) ;;
  *) msg "  ⚠ 非主流 Debian/Ubuntu 衍生版——多半也能装，失败的话请手动装依赖。" \
         "  ⚠ Not a mainstream Debian/Ubuntu derivative — likely fine, but install deps manually if it fails." ;;
esac

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  msg "  ⚠ 当前没有图形会话（DISPLAY/WAYLAND 为空，像是 SSH/headless）：托盘图标只在桌面会话出现；服务仍会跑，可用 --once/--doctor 验证。" \
      "  ⚠ No graphical session (DISPLAY/WAYLAND empty — looks like SSH/headless): the tray icon only shows in a desktop session; the service still runs, verify with --once/--doctor."
fi
if printf '%s' "${XDG_CURRENT_DESKTOP:-}" | grep -qi gnome; then
  msg "  · GNOME 桌面：托盘图标需 AppIndicator 扩展（Ubuntu 通常已默认启用），收尾会再提示。" \
      "  · GNOME desktop: the tray needs the AppIndicator extension (usually on by default in Ubuntu); reminder at the end."
fi
if ! command -v sudo >/dev/null 2>&1 && [ "$(id -u)" != 0 ]; then
  msg "  ⚠ 没有 sudo 且非 root：安装系统依赖那一步可能失败。" \
      "  ⚠ No sudo and not root: installing system dependencies may fail."
fi
if [ "$INTERACTIVE" != 1 ]; then
  msg "  · 非交互环境（无 /dev/tty）：跳过提问，用默认值继续。" \
      "  · Non-interactive (no /dev/tty): skipping prompts, using defaults."
fi

# ================= 1. 系统依赖（sudo）=================
# 说明：扫描登录态需要先把 Python 依赖装好，所以「装依赖」必须在「验证登录」之前；
# 真正会让程序常驻运行的「激活服务」放在登录验证通过之后。
msg "【2/5】安装系统依赖（需要 sudo 授权）…" "[2/5] Installing system dependencies (sudo required)…"
# 关键：apt 命令 stdin 接 /dev/null —— 否则 curl|bash 时 apt/debconf 会吞掉管道里「剩余脚本」，
# 导致安装在 apt 后被截断（clone/venv 全不执行却以退出码 0 结束）。
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y </dev/null || warn "$(msg 'apt-get update 报错（可能是无关第三方源），跳过更新继续。' 'apt-get update failed (likely an unrelated 3rd-party repo), skipping update and continuing.')"
# AppIndicator GIR 包名因发行版而异：老的 gir1.2-appindicator3-0.1 vs 新的 Ayatana fork，二者都提供所需 typelib。
IND_PKG="gir1.2-appindicator3-0.1"
if ! apt-cache show "$IND_PKG" >/dev/null 2>&1; then IND_PKG="gir1.2-ayatanaappindicator3-0.1"; fi
log "AppIndicator: $IND_PKG"
sudo -E apt-get install -y \
  build-essential python3 python3-venv python3-dev \
  python3-gi gir1.2-gtk-3.0 "$IND_PKG" gir1.2-notify-0.7 \
  libgirepository1.0-dev libcairo2-dev pkg-config \
  libnotify-bin xdg-utils git curl </dev/null

# ================= 2. 拉取代码 + venv + 命令（准备，尚未激活服务）=================
msg "【3/5】部署最新版 + 建虚拟环境…" "[3/5] Deploying latest version + building venv…"
mkdir -p "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch --depth 1 origin main
  git -C "$INSTALL_DIR" reset --hard origin/main   # reset 不依赖共同祖先，浅克隆安全
else
  if [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
    backup="${INSTALL_DIR}.bak.$(date +%s)"
    log "backup -> ${backup}"
    mv "$INSTALL_DIR" "$backup"
  fi
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
# venv 不存在或 python 小版本升级后失效（symlink 悬空）都重建，让"重跑=更新"能自愈
if [ ! -d "$INSTALL_DIR/venv" ] || ! "$PY" -c 'pass' >/dev/null 2>&1; then
  rm -rf "$INSTALL_DIR/venv"
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip wheel
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
chmod +x "$INSTALL_DIR/run.sh"

# 命令包装器（无害，提前装好，便于下面用 `claude-usage-indicator --doctor` 自查）
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/$APP" <<EOF
#!/usr/bin/env bash
exec "$PY" "$SCRIPT" "\$@"
EOF
chmod +x "$BIN_DIR/$APP"

# ================= 3. 登录验证（激活前的关卡）=================
msg "【4/5】验证 claude.ai 登录态（激活服务前）…" "[4/5] Verifying your claude.ai login (before activating)…"
GATE=0
while :; do
  if [ "$INTERACTIVE" = 1 ]; then
    msg "\n请在浏览器（Chrome/Chromium/Brave/Edge）登录 https://claude.ai，登录好后按回车开始检查…" \
        "\nLog into https://claude.ai in your browser (Chrome/Chromium/Brave/Edge), then press Enter to check…"
    IFS= read -r _ < /dev/tty || true
  fi
  echo
  if "$PY" "$SCRIPT" --doctor --lang "$LC"; then
    break
  fi
  if [ "$INTERACTIVE" != 1 ]; then
    msg "（非交互：未拿到登录态，仍继续安装，顶栏稍后会提示）" \
        "(non-interactive: login not ready, proceeding anyway; the tray will prompt later)"
    break
  fi
  echo
  msg "没拿到可用登录态。请选择：\n  1) 我已登录 / 已解锁钥匙环 —— 重新检查\n  2) 仍然继续安装（稍后自行修复）\n  3) 退出（先不激活服务）" \
      "Login not ready. Choose:\n  1) I've logged in / unlocked the keyring — re-check\n  2) Install anyway (fix it later)\n  3) Quit (don't activate the service yet)"
  printf '> ' > /dev/tty; IFS= read -r _ans < /dev/tty || _ans=3
  case "$_ans" in
    2) break;;
    3) GATE=3; break;;
    *) : ;;  # 重新扫描
  esac
done

if [ "$GATE" = 3 ]; then
  msg "已停在激活前（依赖已装好，服务未启动）。修好登录后：\n    %s --doctor      # 再次自检，显示 ✅ 后运行：\n    systemctl --user enable --now %s.service\n或直接重跑本安装命令。" \
      "Stopped before activation (deps installed, service not started). After fixing login:\n    %s --doctor      # re-check; once it shows ✅ run:\n    systemctl --user enable --now %s.service\nor just re-run this installer." \
      "$BIN_DIR/$APP" "$APP"
  exit 0
fi

# ================= 4. 激活 systemd 用户服务 =================
msg "【5/5】激活后台服务…" "[5/5] Activating the background service…"
mkdir -p "$SERVICE_DIR"
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$INSTALL_DIR/packaging/${APP}.service" > "$SERVICE"
# 没有用户级 systemd 会话（SSH/headless）时不要因 set -e 整体失败：降级为提示
if systemctl --user daemon-reload 2>/dev/null; then
  systemctl --user enable "$APP.service" >/dev/null 2>&1 || true
  systemctl --user restart "$APP.service" \
    || warn "$(msg '服务未能启动，稍后可手动：systemctl --user restart '"$APP"'.service' 'Service failed to start; later run: systemctl --user restart '"$APP"'.service')"
else
  msg "未检测到用户级 systemd 会话（SSH/headless？）。文件已装好，登录图形会话后运行：\n    systemctl --user enable --now %s.service" \
      "No user systemd session detected (SSH/headless?). Files are installed; in a desktop session run:\n    systemctl --user enable --now %s.service" "$APP"
fi

# ================= 5. 收尾 =================
VER="$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '?')"
echo
msg "✅ 完成！当前版本 v%s" "✅ Done! version v%s" "$VER"
msg "  ▸ 几秒内顶栏开始显示用量。" "  ▸ The tray will start showing your usage within seconds."
msg "  ▸ 常用命令：" "  ▸ Handy commands:"
echo  "      $APP --doctor       # $(msg '登录态/凭证自检（不泄露密钥）' 'login/credential self-check (no secrets leaked)')"
echo  "      $APP --check        # $(msg '检查更新' 'check for updates')"
echo  "      $APP --self-update  # $(msg '更新（无需 sudo，同托盘 Update now）' 'update (no sudo; same as tray Update now)')"
echo  "      $APP --once         # $(msg '拉取一次并打印（调试）' 'fetch once and print (debug)')"
echo  "      systemctl --user status $APP.service"
if printf '%s' "${XDG_CURRENT_DESKTOP:-}" | grep -qi gnome; then
  msg "  ▸ GNOME：看不到托盘图标时——\n      sudo apt-get install -y gnome-shell-extension-appindicator  然后注销重登" \
      "  ▸ GNOME: if the tray icon doesn't appear —\n      sudo apt-get install -y gnome-shell-extension-appindicator  then log out and back in"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    msg "  ⚠ %s 不在 PATH——顶栏照常工作（服务用绝对路径启动）；要直接用 '%s …' 命令，把这行加进 ~/.bashrc 再重开终端：\n      export PATH=\"\$HOME/.local/bin:\$PATH\"\n    或本次用全路径：%s --doctor" \
        "  ⚠ %s is not on PATH — the tray still works (service uses absolute paths); to run '%s …' directly, add this to ~/.bashrc and reopen the terminal:\n      export PATH=\"\$HOME/.local/bin:\$PATH\"\n    or use the full path now: %s --doctor" \
        "$BIN_DIR" "$APP" "$BIN_DIR/$APP" ;;
esac
