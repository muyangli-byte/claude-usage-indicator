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
# 注意：不能只用 [ -r /dev/tty ]——容器/cron 里设备节点存在但打不开（无控制终端）。必须真正试开。
if (exec </dev/tty >/dev/tty) 2>/dev/null; then INTERACTIVE=1; else INTERACTIVE=0; fi

# ---- 语言 ----
# 默认中文（不依赖系统 locale —— 同事机器 LANG=C 时也默认中文）；交互时仍可在下面的选择器里改 English。
LC=zh
if [ "$INTERACTIVE" = 1 ]; then
  if [ "$LC" = zh ]; then _def=1; else _def=2; fi
  {
    printf '\n  请选择语言 / Choose your language\n'
    printf '  ────────────────────────────────\n'
    printf '    【1】 中文\n'
    printf '    【2】 English\n\n'
    printf '  输入 1 或 2 后回车 / Type 1 or 2, then Enter  〔默认 default: %s〕: ' "$_def"
  } > /dev/tty
  IFS= read -r _l < /dev/tty || _l=""
  case "$_l" in 1|zh|ZH|中*) LC=zh;; 2|en|EN|e|E) LC=en;; *) : ;; esac
fi

# ---- 输出助手（双语；msg 支持 printf 占位符）----
# 颜色：仅当 stdout 是终端时上色（管道 / 重定向 / CI 里不污染输出，避免出现裸 ESC 码）。
if [ -t 1 ]; then _G=$'\033[32m'; _Y=$'\033[33m'; _R=$'\033[31m'; _D=$'\033[90m'; _C=$'\033[36m'; _B=$'\033[1m'; _N=$'\033[0m'
else _G=''; _Y=''; _R=''; _D=''; _C=''; _B=''; _N=''; fi
log() { printf '%s[install]%s %s\n' "$_C" "$_N" "$*"; }
err() { printf '%s[install]%s %s\n' "$_R" "$_N" "$*" >&2; }
warn(){ printf '%s[install]%s %s\n' "$_Y" "$_N" "$*"; }
msg() { local z="$1" e="$2"; shift 2; if [ "$LC" = zh ]; then printf "$z\n" "$@"; else printf "$e\n" "$@"; fi; }

# 每个大步拆成若干「小步」逐条展示。小步的输出写进日志，成功只留一行 ✓，失败才把日志吐出来。
_qlog="$(mktemp 2>/dev/null || echo "/tmp/cui-install.$$.log")"
trap 'rm -f "$_qlog"' EXIT
dump() { sed 's/^/        /' "$_qlog" >&2; }   # 失败日志，缩进到小步符号之下

# 即时结果行（无命令）：✓ 成功 / · 跳过或提示 / ⚠ 非致命警告 / ✗ 失败
ok()   { printf '    %s✓%s %s\n' "$_G" "$_N" "$1"; }
skip() { printf '    %s·%s %s\n' "$_D" "$_N" "$1"; }
bad()  { printf '    %s⚠%s %s\n' "$_Y" "$_N" "$1"; }
fail() { printf '    %s✗%s %s\n' "$_R" "$_N" "$1" >&2; }
phase(){ printf '\n%s▸%s %s%s%s\n' "$_C" "$_N" "$_B" "$(msg "$1" "$2")" "$_N"; }   # 大步标题：空行 + ▸ 加粗

# 转圈：运行时在原地显示「⠹ 正在做什么 …」，直到后台 pid 结束；返回该命令的退出码。
# 非交互（无 /dev/tty）时不画动画，直接等。stdin 一律接 /dev/null（避免 curl|bash 把剩余脚本喂给子进程）。
_spin() {  # _spin <pid> <label>
  local pid="$1" label="$2" fr=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏) i=0
  [ "$INTERACTIVE" = 1 ] || { wait "$pid"; return $?; }
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r    %s%s%s %s … ' "$_C" "${fr[i % 10]}" "$_N" "$label" >/dev/tty 2>/dev/null || true
    sleep 0.1; i=$(( i + 1 ))
  done
  printf '\r\033[K' >/dev/tty 2>/dev/null || true
  wait "$pid"
}
step()  {  # 致命小步：成功 ✓；失败 ✗ + 吐日志，返回 1（调用方决定是否退出）
  local label="$1"; shift
  "$@" </dev/null >"$_qlog" 2>&1 &
  if _spin "$!" "$label"; then ok "$label"; return 0; fi
  fail "$label"; dump; return 1
}
stepw() {  # 不致命小步（如 apt update）：成功 ✓；失败只 ⚠，不吐日志、不退出
  local label="$1"; shift
  "$@" </dev/null >"$_qlog" 2>&1 &
  if _spin "$!" "$label"; then ok "$label"; else bad "$label$(msg '（有警告，已跳过）' ' (warned, skipped)')"; fi
  return 0
}

# ================= 【1/5】环境检查（无 apt 硬退出；其余只警告）=================
phase "【1/5】检查环境" "[1/5] Checking environment"

if ! command -v apt-get >/dev/null 2>&1; then
  fail "$(msg '没找到 apt：本脚本仅支持 Debian/Ubuntu。其他发行版请手动装依赖后用 run.sh 运行。' 'apt not found: this installer supports Debian/Ubuntu only. On other distros install the deps manually and use run.sh.')"
  exit 1
fi
ok "$(msg '包管理器：apt（Debian/Ubuntu）' 'package manager: apt (Debian/Ubuntu)')"

DISTRO="$( . /etc/os-release 2>/dev/null && echo "${ID:-?} ${VERSION_ID:-}" )"
case " $DISTRO " in
  *" ubuntu "*|*" debian "*|*" linuxmint "*|*" pop "*|*" elementary "*|*" zorin "*|*" neon "*)
    ok "$(msg '发行版：%s' 'distro: %s' "$DISTRO")" ;;
  *) bad "$(msg '发行版：%s（非主流衍生版，多半也能装，失败的话请手动装依赖）' 'distro: %s (uncommon derivative — likely fine; install deps manually if it fails)' "$DISTRO")" ;;
esac

if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
  ok "$(msg '图形会话：已检测到' 'graphical session: detected')"
else
  bad "$(msg '图形会话：无（DISPLAY/WAYLAND 为空，像 SSH/headless）——托盘只在桌面会话显示，服务仍会跑' 'no graphical session (looks like SSH/headless) — the tray only shows in a desktop session; the service still runs')"
fi

if command -v sudo >/dev/null 2>&1 || [ "$(id -u)" = 0 ]; then
  ok "$(msg 'sudo：可用' 'sudo: available')"
else
  bad "$(msg 'sudo：无且非 root——装系统依赖那步可能失败' 'no sudo and not root — installing system deps may fail')"
fi

if printf '%s' "${XDG_CURRENT_DESKTOP:-}" | grep -qi gnome; then
  skip "$(msg 'GNOME 桌面：托盘需 AppIndicator 扩展（Ubuntu 通常已默认启用），收尾会再提示' 'GNOME desktop: the tray needs the AppIndicator extension (usually on by default in Ubuntu); reminder at the end')"
fi
[ "$INTERACTIVE" = 1 ] || skip "$(msg '非交互环境（无 /dev/tty）：跳过提问，用默认值继续' 'non-interactive (no /dev/tty): skipping prompts, using defaults')"

# ================= 【2/5】系统依赖（sudo）=================
# 说明：扫描登录态需要先把 Python 依赖装好，所以「装依赖」必须在「验证登录」之前；
# 真正会让程序常驻运行的「激活服务」放在登录验证通过之后。
phase "【2/5】安装系统依赖（需要 sudo 授权）" "[2/5] Installing system dependencies (sudo required)"
export DEBIAN_FRONTEND=noninteractive

# 先触发一次 sudo 密码提示（可见）；之后 apt 静默运行就不会把密码提示藏进日志。
sudo -v || { fail "$(msg '需要 sudo 权限来安装系统依赖。' 'sudo is required to install system dependencies.')"; exit 1; }
ok "$(msg '已获得 sudo 授权' 'sudo authorized')"

# apt 命令一律由 step/stepw 经 </dev/null 运行（否则 curl|bash 时 apt/debconf 会吞掉管道里剩余脚本导致截断）。
_apt_update(){ sudo apt-get update -y; }
stepw "$(msg '更新软件源' 'update package lists')" _apt_update

# AppIndicator GIR 包名因发行版而异：老的 vs 新的 Ayatana fork，二者都提供所需 typelib。
IND_PKG="gir1.2-appindicator3-0.1"
apt-cache show "$IND_PKG" >/dev/null 2>&1 || IND_PKG="gir1.2-ayatanaappindicator3-0.1"

# 分组安装：每组一行小步，便于看清进度（apt 会跳过已装的，重复运行很快）。
_apt(){ sudo -E apt-get install -y "$@"; }
_apt_fail(){ err "$(msg '系统依赖安装失败（详见上方日志）。' 'failed to install system dependencies (see log above).')"; exit 1; }
step "$(msg '编译工具与开发库' 'build tools & dev libraries')" \
     _apt build-essential python3 python3-venv python3-dev libgirepository1.0-dev libcairo2-dev pkg-config || _apt_fail
step "$(msg 'GTK / 托盘 / 通知 组件' 'GTK / tray / notification components')" \
     _apt python3-gi gir1.2-gtk-3.0 "$IND_PKG" gir1.2-notify-0.7 libnotify-bin || _apt_fail
step "$(msg '辅助工具（git / curl / xdg-utils）' 'helper tools (git / curl / xdg-utils)')" \
     _apt xdg-utils git curl || _apt_fail

# ================= 【3/5】拉取代码 + venv + 命令（准备，尚未激活服务）=================
phase "【3/5】部署最新版 + 建虚拟环境" "[3/5] Deploying latest version + building venv"
mkdir -p "$INSTALL_DIR"

# 部署最新 main（已是 git 副本就 fetch+reset，浅克隆安全；否则备份非空目录后 clone）
_deploy() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" fetch --depth 1 origin main && git -C "$INSTALL_DIR" reset --hard origin/main
  else
    [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ] && mv "$INSTALL_DIR" "${INSTALL_DIR}.bak.$(date +%s)"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
}

# ── 与用户环境完全隔离 ──
# 构建（建 venv / 装 pip 依赖 / 编译 PyGObject）一律在 env -i 清空后的「白名单」环境里跑，
# 不受 conda/pyenv、自定义 PATH、PYTHONPATH/PYTHONHOME、PIP_* 与 ~/.config/pip/pip.conf（可能指向
# 私有源）、LD_LIBRARY_PATH/LD_PRELOAD 等任何用户定制影响 → 所有人装出来的环境完全一致。
clean_build(){ env -i \
  HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PYTHONNOUSERSITE=1 PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PIP_NO_CACHE_DIR=1 \
  "$@"; }
SYS_PY="/usr/bin/python3"; [ -x "$SYS_PY" ] || SYS_PY="$(command -v python3)"
PIP="$INSTALL_DIR/venv/bin/pip"
_build_venv(){ rm -rf "$INSTALL_DIR/venv"; clean_build "$SYS_PY" -m venv "$INSTALL_DIR/venv"; }
_pip_upgrade(){ clean_build "$PIP" install -q --index-url https://pypi.org/simple --upgrade pip wheel; }
_pip_reqs(){ clean_build "$PIP" install -q --index-url https://pypi.org/simple -r "$INSTALL_DIR/requirements.txt"; }
_pip_all(){ _pip_upgrade && _pip_reqs; }
_gi_check(){ clean_build "$PY" -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk, GLib"; }
_gi_ok(){ _gi_check >/dev/null 2>&1; }

step "$(msg '拉取最新代码' 'fetch latest code')" _deploy \
  || { err "$(msg '拉取代码失败（详见上方日志）。' 'failed to fetch the code (see log above).')"; exit 1; }
skip "$(msg '版本 v%s' 'version v%s' "$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '?')")"

# venv：缺失 / python 失效（小版本升级后 symlink 悬空）/ 不是系统 Python 建的（conda/pyenv）→ 重建；否则复用。
need_build=0
[ -d "$INSTALL_DIR/venv" ] && "$PY" -c 'pass' >/dev/null 2>&1 || need_build=1
if [ "$need_build" = 0 ]; then
  bp="$("$PY" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || echo '')"
  case "$bp" in /usr|/usr/*) : ;; *) need_build=1 ;; esac
fi
if [ "$need_build" = 1 ]; then
  step "$(msg '创建虚拟环境（系统 Python）' 'create virtualenv (system Python)')" _build_venv \
    || { err "$(msg '创建虚拟环境失败（详见上方日志）。' 'failed to create the virtualenv (see log above).')"; exit 1; }
else
  skip "$(msg '复用已有虚拟环境' 'reuse existing virtualenv')"
fi
step "$(msg '升级 pip / wheel' 'upgrade pip / wheel')" _pip_upgrade \
  || { err "$(msg 'pip 升级失败（详见上方日志）。' 'pip upgrade failed (see log above).')"; exit 1; }
step "$(msg '安装 Python 依赖（curl_cffi / browser_cookie3 / PyGObject）' 'install Python deps (curl_cffi / browser_cookie3 / PyGObject)')" _pip_reqs \
  || { err "$(msg 'Python 依赖安装失败（详见上方日志）。' 'failed to install Python deps (see log above).')"; exit 1; }

# 验证 PyGObject 能加载；不行就用系统 Python 重建一次（兜底：老 venv 是 conda 的 Python 建的）。
if ! _gi_ok; then
  bad "$(msg 'GTK 组件加载失败，改用系统 Python 重建…' 'GTK failed to load — rebuilding with system Python…')"
  step "$(msg '重建虚拟环境' 'rebuild virtualenv')" _build_venv \
    || { err "$(msg '重建失败（详见上方日志）。' 'rebuild failed (see log above).')"; exit 1; }
  step "$(msg '重新安装 Python 依赖' 'reinstall Python deps')" _pip_all \
    || { err "$(msg '重新安装失败（详见上方日志）。' 'reinstall failed (see log above).')"; exit 1; }
fi
step "$(msg '验证 GTK 组件可加载' 'verify GTK components load')" _gi_check \
  || { err "$(msg 'PyGObject 仍无法加载：请勿在 conda 环境内安装（先 conda deactivate 再重试）。' 'PyGObject still fails to load: do not run inside a conda env (conda deactivate and retry).')"; exit 1; }

chmod +x "$INSTALL_DIR/run.sh"
# 命令包装器：统一走 run.sh（同一套环境隔离），用户手动 `claude-usage-indicator …` 也不受其 shell 定制影响
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/$APP" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/run.sh" "\$@"
EOF
chmod +x "$BIN_DIR/$APP"
ok "$(msg '安装命令 claude-usage-indicator' 'installed command: claude-usage-indicator')"

# ================= 3. 登录验证（激活前的关卡）=================
phase "【4/5】验证 claude.ai 登录态（激活服务前）" "[4/5] Verifying your claude.ai login (before activating)"
GATE=0
while :; do
  if [ "$INTERACTIVE" = 1 ]; then
    if [ "$LC" = zh ]; then
      {
        printf '\n  ▶ 第一步：在浏览器（Chrome / Chromium / Brave / Edge）登录 https://claude.ai\n'
        printf '    登录好之后，按【回车】开始检查登录态… '
      } > /dev/tty
    else
      {
        printf '\n  ▶ Step 1: log into https://claude.ai in your browser (Chrome / Chromium / Brave / Edge)\n'
        printf '    Once you are logged in, press [Enter] to check… '
      } > /dev/tty
    fi
    IFS= read -r _ < /dev/tty || true
  fi
  echo
  if "$INSTALL_DIR/run.sh" --doctor --lang "$LC"; then
    break
  fi
  if [ "$INTERACTIVE" != 1 ]; then
    msg "（非交互：未拿到登录态，仍继续安装，顶栏稍后会提示）" \
        "(non-interactive: login not ready, proceeding anyway; the tray will prompt later)"
    break
  fi
  echo
  if [ "$LC" = zh ]; then
    {
      printf '\n  ⚠ 没读到可用的登录态，请选择：\n'
      printf '    【1】 我已登录 / 已解锁钥匙环 —— 重新检查\n'
      printf '    【2】 不管它，仍然继续安装（之后自己修）\n'
      printf '    【3】 退出，暂不激活服务\n\n'
      printf '  输入 1 / 2 / 3 后回车  〔默认 default: 1〕: '
    } > /dev/tty
  else
    {
      printf '\n  ⚠ Could not read a usable login. Choose:\n'
      printf '    【1】 I am logged in / keyring unlocked — re-check\n'
      printf '    【2】 Install anyway (I will fix it later)\n'
      printf '    【3】 Quit, do not activate the service yet\n\n'
      printf '  Type 1 / 2 / 3, then Enter  〔default: 1〕: '
    } > /dev/tty
  fi
  IFS= read -r _ans < /dev/tty || _ans=3
  case "$_ans" in
    2) break;;
    3) GATE=3; break;;
    *) : ;;  # 重新扫描
  esac
done

if [ "$GATE" = 3 ]; then
  msg "已停在激活前（依赖已装好，服务未启动）。修好登录后：\n    %s --doctor      # 再次自检，显示 ✓ 后运行：\n    systemctl --user enable --now %s.service\n或直接重跑本安装命令。" \
      "Stopped before activation (deps installed, service not started). After fixing login:\n    %s --doctor      # re-check; once it shows ✓ run:\n    systemctl --user enable --now %s.service\nor just re-run this installer." \
      "$BIN_DIR/$APP" "$APP"
  exit 0
fi

# ================= 【5/5】激活 systemd 用户服务 =================
phase "【5/5】激活后台服务" "[5/5] Activating the background service"
mkdir -p "$SERVICE_DIR"
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$INSTALL_DIR/packaging/${APP}.service" > "$SERVICE"
ok "$(msg '写入 systemd 服务文件' 'wrote the systemd service file')"
# 没有用户级 systemd 会话（SSH/headless）时不要因 set -e 整体失败：降级为提示
if systemctl --user daemon-reload 2>/dev/null; then
  ok "$(msg '重载 systemd 配置' 'reloaded systemd')"
  if systemctl --user enable "$APP.service" >/dev/null 2>&1; then
    ok "$(msg '设置开机自启' 'enabled on login')"
  else
    bad "$(msg '设置开机自启失败（不影响本次运行）' 'could not enable on login (does not affect this run)')"
  fi
  _restart(){ systemctl --user restart "$APP.service"; }
  step "$(msg '启动后台服务' 'start the background service')" _restart \
    || bad "$(msg '服务未能启动，稍后可手动：systemctl --user restart '"$APP"'.service' 'service failed to start; later run: systemctl --user restart '"$APP"'.service')"
else
  skip "$(msg '未检测到用户级 systemd 会话（SSH/headless）——文件已装好；进入桌面会话后运行：systemctl --user enable --now '"$APP"'.service' 'no user systemd session (SSH/headless) — files installed; in a desktop session run: systemctl --user enable --now '"$APP"'.service')"
fi

# ================= 5. 收尾 =================
VER="$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '?')"
echo
printf '%s✓%s %s\n' "$_G" "$_N" "$(msg '完成！当前版本 v%s' 'Done! version v%s' "$VER")"
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
