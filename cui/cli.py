"""命令行子命令（--once/--doctor/--check/--update/--self-update/--uninstall）、
GUI 启动（run_gui）与入口（main）。依赖以上所有模块。"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from cui.api import client_fingerprint, fetch_usage, fetch_remote_version, remote_is_newer
from cui.config import (APP_NAME, APP_ROOT, BROWSERS, DISPLAY_VERSION, GITHUB_OWNER,
                        GITHUB_REPO, IS_DEV, POLL_FAST_S, POLL_SLOW_S, REPO_URL,
                        UPDATE_RESULT, __version__, _read_config, _read_version,
                        _write_update_result, load_lang)
from cui.credentials import (CookieError, _cookie_presence, _profile_cookie_files,
                             _profile_label, _valid_org, _valid_sk, load_credentials)
from cui.model import UsageData


def run_gui() -> None:
    from cui.tray import build_app
    from cui.poller import Poller
    AppClass, Gtk = build_app()
    app = AppClass()
    poller = Poller(app)
    app.poller = poller
    poller.start()
    print(f"[poller] running v{DISPLAY_VERSION}, fast={POLL_FAST_S}s slow={POLL_SLOW_S}s", flush=True)
    print(f"[poller] {client_fingerprint()}", flush=True)  # 记录 TLS 伪装指纹，便于排查 Cloudflare 拦截
    Gtk.main()


# ===================== CLI =====================
def cmd_once() -> int:
    try:
        sk, org = load_credentials()
    except CookieError as e:
        print(f"cookie error: {e}")
        return 2
    if not sk:
        print("auth: 找不到 sessionKey（请在 Chrome 登录 claude.ai）")
        return 2
    if not org:
        print("error: 找不到 org id（可在 ~/.config/claude-usage-indicator/config.json 设置 org_id）")
        return 2
    try:
        fields = fetch_usage(sk, org)
    except Exception as e:
        print(f"{type(e).__name__}: {e}")
        return 1
    d = UsageData(status="ok", received_at=datetime.now(), **fields)
    print(f"  current session : {d.current_session_used}  (reset {d.current_session_reset})")
    print(f"  all models (wk) : {d.all_models_used}  (reset {d.all_models_reset})")
    print(f"  sonnet (wk)     : {d.sonnet_used}")
    print(f"  opus (wk)       : {d.opus_used}")
    return 0


def cmd_doctor(lang: str = "en") -> int:
    """扫描凭证并打印自检报告（双语，绝不泄露任何密钥）。
    流程：列出每个浏览器 profile 是否有 claude.ai 登录 cookie → 读取并校验 sessionKey/org →
    用拿到的凭证真打一次用量 API 确认可用。拿到且能用返回 0，否则 1。
    install.sh 用它在「激活服务」前做预检；用户也可随时 `--doctor` 自查。"""
    import getpass
    zh = lang == "zh"

    def line(z: str, e: str) -> None:
        print(z if zh else e)

    print("=" * 52)
    line(" Claude 用量指示器 —— 登录态自检", " Claude Usage Indicator — login self-check")
    print("=" * 52)
    user = getpass.getuser()
    line(f"系统用户：{user}", f"System user: {user}")
    line(f"桌面环境：{os.environ.get('XDG_CURRENT_DESKTOP', '?')}",
         f"Desktop:     {os.environ.get('XDG_CURRENT_DESKTOP', '?')}")
    print()
    line("扫描浏览器 profile（找 claude.ai 登录 cookie）：",
         "Scanning browser profiles for a claude.ai login cookie:")
    any_cookie = False
    for name in BROWSERS:
        for cf in _profile_cookie_files(name):
            present, prefix = _cookie_presence(cf)
            if present:
                any_cookie = True
                line(f"  ✓ [{name}] {_profile_label(cf)} —— 有登录 cookie（加密 {prefix}）",
                     f"  ✓ [{name}] {_profile_label(cf)} — has login cookie (enc {prefix})")
            else:
                line(f"  · [{name}] {_profile_label(cf)} —— 无",
                     f"  · [{name}] {_profile_label(cf)} — none")
    if not any_cookie:
        line("  （没找到任何 claude.ai 登录 cookie）", "  (no claude.ai login cookie found anywhere)")
    print()

    sk = org = None
    try:
        sk, org = load_credentials()
    except CookieError:
        pass
    if _read_config().get("session_key"):
        line("凭证来源：config.json 显式配置（优先）", "Source: config.json override (takes precedence)")

    sk_ok, org_ok = _valid_sk(sk), _valid_org(org)
    line(f"sessionKey：{'✓ 已获取并通过格式校验' if sk_ok else '✗ 未获取到有效值'}",
         f"sessionKey: {'✓ obtained and validated' if sk_ok else '✗ not obtained'}")
    line(f"org_id    ：{'✓ ' + org if org_ok else '✗ 未获取'}",
         f"org_id    : {'✓ ' + org if org_ok else '✗ not obtained'}")

    if not (sk_ok and org_ok):
        print()
        line("→ 登录态还没就绪，请确认：", "→ Login not ready yet. Please make sure:")
        line("  1) 已在 Chrome/Chromium/Brave/Edge 登录 https://claude.ai",
             "  1) you're logged into https://claude.ai in Chrome/Chromium/Brave/Edge")
        line("  2) 系统钥匙环已解锁（GNOME keyring / KDE KWallet）",
             "  2) your keyring is unlocked (GNOME keyring / KDE KWallet)")
        line("  3) 仍不行可在 ~/.config/claude-usage-indicator/config.json 填 session_key+org_id",
             "  3) or set session_key+org_id in ~/.config/claude-usage-indicator/config.json")
        return 1

    print()
    line("用拿到的凭证试拉一次用量，确认 sessionKey 真的能用……",
         "Trying a live usage fetch to confirm the sessionKey actually works…")
    try:
        fields = fetch_usage(sk, org)
    except Exception as e:
        line(f"  ✗ 拉取失败：{type(e).__name__}: {e}", f"  ✗ fetch failed: {type(e).__name__}: {e}")
        line("  （sessionKey 可能已过期：请在浏览器重新登录 claude.ai 再试）",
             "  (sessionKey may be expired: re-login to claude.ai and retry)")
        return 1
    d = UsageData(status="ok", received_at=datetime.now(), **fields)
    # 指标名保持与托盘菜单/网页一致的英文（Current session / All models），不翻译
    line(f"  ✓ 成功！Current session {d.current_session_used}，All models {d.all_models_used}",
         f"  ✓ Success! Current session {d.current_session_used}, All models {d.all_models_used}")
    print()
    line("✓ 一切就绪，可以安装。", "✓ All set — ready to install.")
    return 0


def cmd_update() -> int:
    url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/install.sh"
    print(f"[update] 拉取并运行 {url}")
    return subprocess.call(f"curl -fsSL {url} | bash", shell=True)


def cmd_self_update() -> int:
    """轻量自更新（无需 sudo）：在自身安装目录里 git 拉取最新 + pip 装依赖 + 重启服务。
    供托盘「Update now」用；只更新代码/依赖，不动系统库。若系统库有变动请改用 --update。
    把结果（ok|版本 / fail|原因）写到 UPDATE_RESULT，重启后的 GUI 会读取并弹通知。"""
    def fail(msg: str) -> int:
        print(msg)
        _write_update_result(f"fail|{msg}")
        return 1

    # dev 实例（从开发仓库而非安装目录运行）：拒绝自更新——否则会把开发工作树 reset 到 origin/main。
    # 仅靠脏树检查不够（已提交在分支上的改动会被冲掉）；用 IS_DEV 这个更强的信号短路。
    if IS_DEV:
        return fail("dev instance; update via git manually (refusing to reset the dev working tree)")

    here = APP_ROOT
    # 清掉可能残留的旧面包屑，免得它被下面的脏树检查/git 当成改动而卡住更新
    try:
        UPDATE_RESULT.unlink()
    except Exception:
        pass
    if not (here / ".git").exists():
        return fail("not a git install dir; use --update instead")
    try:
        # 保护未提交改动：脏树就别动（避免 reset 丢工作）
        dirty = subprocess.run(["git", "-C", str(here), "status", "--porcelain"],
                               capture_output=True, text=True)
        if dirty.stdout.strip():
            return fail("local uncommitted changes; skipped (update manually or use --update)")
        # 浅克隆（git clone --depth 1）下不能用 merge --ff-only：fetch 来的新提交与本地无共同祖先，
        # 会报 'refusing to merge unrelated histories'。fetch 后 reset 到 FETCH_HEAD（浅克隆安全，
        # 且上面的脏树检查已护住未提交改动）。
        subprocess.run(["git", "-C", str(here), "fetch", "--depth", "1", "origin", "main"], check=True)
        subprocess.run(["git", "-C", str(here), "reset", "--hard", "FETCH_HEAD"], check=True)

        # 校验 venv（python 小版本升级后旧 venv 会失效），坏了就重建
        venv = here / "venv"
        py = venv / "bin" / "python"
        try:
            venv_ok = py.exists() and subprocess.run([str(py), "-c", "pass"]).returncode == 0
        except Exception:
            venv_ok = False
        # 构建用「与用户环境隔离」的干净环境（同 install.sh）：系统 PATH、忽略 conda/pyenv、
        # 忽略 PYTHONPATH/PIP_*/pip.conf、锁定官方源——保证重建出的 venv 与全新安装完全一致。
        clean_env = {
            "HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1",
            "PIP_CONFIG_FILE": "/dev/null", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1", "PIP_NO_CACHE_DIR": "1",
        }
        if not venv_ok:
            if venv.exists():
                shutil.rmtree(venv)
            # 用系统 Python 建 venv（绝不用 conda/pyenv 的，否则运行期 libffi 与系统 libgobject 冲突）
            sys_py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
            subprocess.run([sys_py, "-m", "venv", str(venv)], check=True, env=clean_env)
        pip = venv / "bin" / "pip"
        idx = ["--index-url", "https://pypi.org/simple"]
        subprocess.run([str(pip), "install", "-q", *idx, "--upgrade", "pip", "wheel"], check=True, env=clean_env)
        subprocess.run([str(pip), "install", "-q", *idx, "-r", str(here / "requirements.txt")], check=True, env=clean_env)
    except subprocess.CalledProcessError as e:
        return fail(f"update step failed ({e}); try --update")
    except Exception as e:  # 任何意外都留下 fail 面包屑，保证 GUI 有反馈
        return fail(f"unexpected error ({e}); try --update")

    newver = _read_version()
    _write_update_result(f"ok|{newver}")  # 先写成功，重启后新进程读到并通知
    rc = subprocess.run(["systemctl", "--user", "restart", f"{APP_NAME}.service"]).returncode
    if rc != 0:
        return fail(f"service restart failed (rc={rc}); run: systemctl --user restart {APP_NAME}.service")
    print(f"updated and restarted (v{newver})")
    return 0


def cmd_uninstall() -> int:
    """托盘「Uninstall」触发，在独立 systemd 瞬时单元里运行（不会被 systemctl stop 连带杀掉）：
    等触发它的 GUI 退出后，用 uninstall.sh --purge 彻底删除 服务/命令/安装目录/配置，最后打开项目主页。"""
    here = APP_ROOT
    time.sleep(1)  # 让触发它的 GUI 先退出，避免和 uninstall.sh 里的 systemctl stop 抢
    uninstaller = here / "uninstall.sh"
    try:
        if uninstaller.exists():
            subprocess.run(["bash", str(uninstaller), "--purge"], check=False)
        else:  # 兜底：uninstall.sh 不在就内联清（路径与 uninstall.sh 一致）
            home = Path.home()
            subprocess.run(["systemctl", "--user", "disable", "--now", f"{APP_NAME}.service"], check=False)
            shutil.rmtree(home / ".local/share" / APP_NAME, ignore_errors=True)
            shutil.rmtree(home / ".config" / APP_NAME, ignore_errors=True)
            (home / ".local/bin" / APP_NAME).unlink(missing_ok=True)
            (home / ".config/systemd/user" / f"{APP_NAME}.service").unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    except Exception as e:
        print(f"[uninstall] {e}", flush=True)
    try:
        subprocess.Popen(["xdg-open", REPO_URL])  # 完成后打开项目主页
    except Exception as e:
        print(f"[uninstall] xdg-open failed: {e}", flush=True)
    time.sleep(3)  # 给浏览器 dbus 激活/启动留点时间，再让本瞬时单元被回收
    return 0


def cmd_check() -> int:
    remote = fetch_remote_version()
    if remote_is_newer(remote):
        print(f"有新版本：v{__version__} -> v{remote}（运行 `{APP_NAME} --update` 升级）")
    else:
        print(f"已是最新版 v{__version__}" + (f"（远端 v{remote}）" if remote else "（无法获取远端版本）"))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog=APP_NAME, description="Claude 用量顶栏指示器")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--once", action="store_true", help="拉取一次并打印（调试）")
    p.add_argument("--check", action="store_true", help="检查是否有新版本")
    p.add_argument("--update", action="store_true", help="更新到最新版（重跑安装脚本，含系统库，需 sudo）")
    p.add_argument("--self-update", action="store_true", help="轻量自更新：git+pip+重启，无需 sudo（托盘 Update now 用）")
    p.add_argument("--doctor", action="store_true", help="扫描并自检登录态/凭证（不泄露密钥；安装脚本与排错用）")
    p.add_argument("--uninstall", action="store_true", help="彻底卸载（托盘 Uninstall 用；删服务/命令/安装目录/配置后打开项目主页）")
    p.add_argument("--lang", choices=["zh", "en"], help="输出语言（默认按系统/配置自动判断）")
    args = p.parse_args()

    if args.doctor:
        sys.exit(cmd_doctor(args.lang or load_lang()))
    if args.uninstall:
        sys.exit(cmd_uninstall())
    if args.once:
        sys.exit(cmd_once())
    if args.check:
        sys.exit(cmd_check())
    if args.update:
        sys.exit(cmd_update())
    if args.self_update:
        sys.exit(cmd_self_update())
    run_gui()
