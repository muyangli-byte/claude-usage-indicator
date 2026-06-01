#!/usr/bin/env python3
"""
Claude 用量顶栏指示器（纯 Python，方案 B）。

不需要浏览器开着标签页 / Tampermonkey / 本地 HTTP 服务。原理：
  1. browser_cookie3 自动从 Chrome 的 cookie 库读取 sessionKey + 当前 org id
     （读不到时回退到 ~/.config/claude-usage-indicator/config.json）
  2. curl_cffi 伪装 Chrome 的 TLS 指纹，直接请求 claude.ai 的内部用量接口
     （普通 requests/curl 会被 Cloudflare 以 TLS 指纹拦截，必须用 curl_cffi）
  3. 解析 JSON，更新 GTK AppIndicator 顶栏

健壮性（应对 claude.ai 接口/结构变动）：
  - 失败分类：auth / cloudflare / schema / http / network / cookie，各给不同提示与通知
  - schema 校验：必需字段缺失/类型不对 -> 报「接口结构变了」并把原始响应 dump 到磁盘
  - 版本检查：每天比对仓库里的 VERSION，有新版只通知（不自动更新），靠 `--update` 手动升级
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---- 仓库信息（创建 GitHub 仓库时把 muyangli-byte 替换成真实用户名）----
GITHUB_OWNER = "muyangli-byte"
GITHUB_REPO = "claude-usage-indicator"

APP_NAME = "claude-usage-indicator"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
DATA_DIR = Path.home() / ".local" / "share" / APP_NAME
DIAG_DIR = DATA_DIR / "diagnostics"
CONFIG_PATH = CONFIG_DIR / "config.json"


def _read_version() -> str:
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip() or "0.0.0"
    except Exception:
        return "0.0.0"


__version__ = _read_version()


# ===================== 配置 =====================
POLL_INTERVAL_S = 30           # 轮询间隔（秒）。别设太小：直连 API 太频繁可能被限流
REQUEST_TIMEOUT_S = 20
UPDATE_CHECK_INTERVAL_S = 86400  # 每天查一次新版本
USAGE_PAGE_URL = "https://claude.ai/new#settings/usage"
BROWSERS = ["chrome", "chromium", "brave", "edge"]  # 依次尝试读取 cookie


# ===================== 凭证读取 =====================
class CookieError(Exception):
    """读取/解密浏览器 cookie 失败（keyring 不可用等）。"""


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def load_credentials() -> tuple[Optional[str], Optional[str]]:
    """返回 (session_key, org_id)。优先浏览器 cookie，其次配置文件。

    若所有浏览器都抛异常（而不是「没有该 cookie」），视为 CookieError。
    """
    import browser_cookie3 as bc3

    sk = org = None
    errors = 0
    tried = 0
    for name in BROWSERS:
        fn = getattr(bc3, name, None)
        if fn is None:
            continue
        tried += 1
        try:
            cookies = {c.name: c.value for c in fn(domain_name="claude.ai")}
        except Exception:
            errors += 1
            continue
        if cookies.get("sessionKey"):
            sk = cookies["sessionKey"]
            org = cookies.get("lastActiveOrg") or org
            break

    cfg = _read_config()
    sk = sk or cfg.get("session_key")
    org = org or cfg.get("org_id")

    # 配置文件也没有、且每个尝试过的浏览器都报错 -> 大概率是 keyring/权限问题
    if sk is None and tried > 0 and errors == tried and not cfg:
        raise CookieError("无法读取任何浏览器 cookie（keyring 可能未解锁）")
    return sk, org


# ===================== 拉取 + 解析 =====================
class AuthError(Exception):
    """sessionKey 缺失/过期/被拒。"""


class CloudflareError(Exception):
    """被 Cloudflare 挑战页拦截（TLS 伪装可能失效）。"""


class SchemaError(Exception):
    """HTTP 200 但 JSON 不符合预期契约（接口结构可能变了）。"""


def _is_challenge(text: str) -> bool:
    t = (text or "")[:4000]
    return "Just a moment" in t or "challenge-platform" in t or "cf-chl" in t


def dump_diagnostics(kind: str, status_code, text: str) -> str:
    """把异常响应写到 diagnostics/，便于事后定位/修脚本。只保留最近 20 份。"""
    try:
        DIAG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = DIAG_DIR / f"{ts}-{kind}.txt"
        header = f"kind={kind}\nstatus={status_code}\nversion={__version__}\ntime={ts}\n\n"
        path.write_text(header + (text or "")[:20000])
        for old in sorted(DIAG_DIR.glob("*.txt"))[:-20]:
            try:
                old.unlink()
            except Exception:
                pass
        return str(path)
    except Exception:
        return ""


def fetch_usage(session_key: str, org_id: str) -> dict:
    """请求用量接口，返回已校验的字段 dict。失败抛上面的分类异常。"""
    from curl_cffi import requests as creq

    url = f"https://claude.ai/api/organizations/{org_id}/usage"
    headers = {
        "accept": "*/*",
        "anthropic-client-platform": "web_claude_ai",
        "referer": "https://claude.ai/new",
    }
    try:
        r = creq.get(
            url,
            cookies={"sessionKey": session_key},
            headers=headers,
            impersonate="chrome",  # 关键：伪装 Chrome TLS 指纹，过 Cloudflare
            timeout=REQUEST_TIMEOUT_S,
        )
    except Exception as e:  # 连接/超时等
        raise ConnectionError(str(e)[:120])

    ct = r.headers.get("content-type", "")

    if r.status_code in (401, 403):
        if "text/html" in ct and _is_challenge(r.text):
            dump_diagnostics("cloudflare", r.status_code, r.text)
            raise CloudflareError(f"HTTP {r.status_code} 被 Cloudflare 挑战拦截")
        raise AuthError(f"HTTP {r.status_code}")

    if r.status_code != 200:
        if _is_challenge(r.text):
            dump_diagnostics("cloudflare", r.status_code, r.text)
            raise CloudflareError(f"HTTP {r.status_code} 挑战页")
        raise RuntimeError(f"HTTP {r.status_code}")

    # 200
    try:
        data = r.json()
    except Exception:
        if _is_challenge(r.text):
            dump_diagnostics("cloudflare", 200, r.text)
            raise CloudflareError("HTTP 200 但返回挑战页")
        dump_diagnostics("schema", 200, r.text)
        raise SchemaError("响应不是 JSON")

    return validate_and_extract(data, r.text)


def validate_and_extract(data, raw_text: str = "") -> dict:
    """校验 JSON 契约并抽取字段。结构不符抛 SchemaError 并 dump 原始响应。"""
    if not isinstance(data, dict):
        dump_diagnostics("schema", 200, raw_text or json.dumps(data)[:20000])
        raise SchemaError("顶层不是对象")
    for key in ("five_hour", "seven_day"):
        sub = data.get(key)
        if not isinstance(sub, dict):
            dump_diagnostics("schema", 200, raw_text or json.dumps(data))
            raise SchemaError(f"缺少必需字段 {key}（接口结构可能变了）")
        if not isinstance(sub.get("utilization"), (int, float)):
            dump_diagnostics("schema", 200, raw_text or json.dumps(data))
            raise SchemaError(f"{key}.utilization 不是数字（接口结构可能变了）")
    return json_to_fields(data)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):  # Python < 3.11 的 fromisoformat 不认 'Z'，统一成 +00:00
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _fmt_pct(obj) -> str:
    if not isinstance(obj, dict):
        return "--"
    u = obj.get("utilization")
    return "--" if u is None else f"{int(round(u))}%"


def _fmt_countdown(dt: Optional[datetime]) -> str:
    """到重置还剩多久 -> '4h50m' / '50m'。"""
    if dt is None:
        return "--"
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "0m"
    h, m = int(secs // 3600), int((secs % 3600) // 60)
    return f"{h}h{m}m" if h else f"{m}m"


def _fmt_resetday(dt: Optional[datetime]) -> str:
    """重置的绝对时刻（转本地时区）-> 'Mon 7am' / 'Tue 3:30pm'。"""
    if dt is None:
        return "--"
    loc = dt.astimezone()
    h12 = loc.strftime("%I").lstrip("0") or "12"
    ap = loc.strftime("%p").lower()
    wd = loc.strftime("%a")
    return f"{wd} {h12}:{loc.minute:02d}{ap}" if loc.minute else f"{wd} {h12}{ap}"


def json_to_fields(j: dict) -> dict:
    fh = j.get("five_hour") or {}
    sd = j.get("seven_day") or {}
    return dict(
        current_session_used=_fmt_pct(fh),
        current_session_reset=_fmt_countdown(_parse_iso(fh.get("resets_at"))),
        all_models_used=_fmt_pct(sd),
        all_models_reset=_fmt_resetday(_parse_iso(sd.get("resets_at"))),
        sonnet_used=_fmt_pct(j.get("seven_day_sonnet")),
        opus_used=_fmt_pct(j.get("seven_day_opus")),
    )


# ===================== 版本检查 =====================
def _ver_tuple(s) -> tuple:
    try:
        return tuple(int(x) for x in str(s).strip().split("."))
    except Exception:
        return ()


def fetch_remote_version() -> Optional[str]:
    from curl_cffi import requests as creq

    url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/VERSION"
    try:
        r = creq.get(url, timeout=10)
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        pass
    return None


def remote_is_newer(remote: Optional[str]) -> bool:
    rt, lt = _ver_tuple(remote), _ver_tuple(__version__)
    return bool(rt) and rt > lt


# ===================== 数据模型 =====================
@dataclass
class UsageData:
    current_session_used: str = "--"
    current_session_reset: str = "--"
    all_models_used: str = "--"
    all_models_reset: str = "--"
    sonnet_used: str = "--"
    opus_used: str = "--"
    status: str = "init"        # init|ok|auth|cloudflare|schema|http|network|cookie
    error_msg: str = ""
    received_at: Optional[datetime] = None     # 最近一次成功拉取
    update_available: Optional[str] = None     # 检测到的更高版本号

    STATUS_LABEL = {
        "ok": "ok",
        "auth": "登录已过期",
        "cloudflare": "Cloudflare 拦截",
        "schema": "接口结构变了",
        "http": "HTTP 错误",
        "network": "网络错误",
        "cookie": "读 cookie 失败",
        "init": "启动中",
    }

    def short_label(self) -> str:
        base = (
            f"Cur {self.current_session_used} {self.current_session_reset} "
            f"| All {self.all_models_used} {self.all_models_reset}"
        )
        if self.received_at is None:
            return {
                "auth": "⚠ Claude 登录已过期",
                "cloudflare": "⚠ Cloudflare 拦截",
                "schema": "⚠ 接口结构变了",
                "cookie": "⚠ 读 cookie 失败",
            }.get(self.status, "Claude usage waiting...")
        return ("⚠ " + base) if self.status != "ok" else base

    def refreshed_ago_text(self) -> str:
        if self.received_at is None:
            return "--"
        sec = max(0, int((datetime.now() - self.received_at).total_seconds()))
        return f"{sec}s ago"

    def received_clock_text(self) -> str:
        return "--" if self.received_at is None else self.received_at.strftime("%H:%M:%S")


class UsageStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = UsageData()

    def apply(self, status: str, msg: str, fields: Optional[dict]) -> None:
        with self._lock:
            d = self._data
            if fields:  # 成功才更新数值与时间；失败保留上次数值
                for k, v in fields.items():
                    setattr(d, k, v)
                d.received_at = datetime.now()
            d.status = status
            d.error_msg = msg

    def set_update(self, version: Optional[str]) -> None:
        with self._lock:
            self._data.update_available = version

    def get(self) -> UsageData:
        with self._lock:
            return UsageData(**vars(self._data))


STORE = UsageStore()


# ===================== 轮询线程 =====================
class Poller(threading.Thread):
    def __init__(self, app: "ClaudeIndicatorApp") -> None:
        super().__init__(daemon=True)
        self.app = app
        self._wake = threading.Event()
        self._sk: Optional[str] = None
        self._org: Optional[str] = None
        self._last_update_check = 0.0

    def wake(self) -> None:
        self._wake.set()

    def _creds(self, force: bool = False) -> tuple[Optional[str], Optional[str]]:
        if force or not (self._sk and self._org):
            self._sk, self._org = load_credentials()
        return self._sk, self._org

    def _do_fetch(self) -> tuple[str, str, Optional[dict]]:
        try:
            sk, org = self._creds()
        except CookieError as e:
            return "cookie", str(e), None
        if not sk or not org:
            try:
                sk, org = self._creds(force=True)
            except CookieError as e:
                return "cookie", str(e), None
        if not sk:
            return "auth", "找不到 sessionKey，请在 Chrome 登录 claude.ai", None
        if not org:
            return "http", "找不到 org id", None
        try:
            return "ok", "", fetch_usage(sk, org)
        except AuthError:
            # sessionKey 可能轮换了，强制重读 cookie 再试一次
            try:
                sk, org = self._creds(force=True)
                return "ok", "", fetch_usage(sk, org)
            except AuthError as e:
                return "auth", f"登录已过期（{e}），请在 Chrome 重新登录 claude.ai", None
            except CloudflareError as e:
                return "cloudflare", str(e), None
            except SchemaError as e:
                return "schema", str(e), None
            except Exception as e:
                return "network", str(e)[:120], None
        except CloudflareError as e:
            return "cloudflare", str(e), None
        except SchemaError as e:
            return "schema", str(e), None
        except ConnectionError as e:
            return "network", str(e)[:120], None
        except Exception as e:
            return "http", str(e)[:120], None

    def _maybe_check_update(self) -> None:
        now = time.time()
        if now - self._last_update_check < UPDATE_CHECK_INTERVAL_S:
            return
        self._last_update_check = now
        remote = fetch_remote_version()
        STORE.set_update(remote if remote_is_newer(remote) else None)

    def run(self) -> None:
        while True:
            try:
                status, msg, fields = self._do_fetch()
            except Exception as e:  # 兜底，绝不让轮询线程挂掉
                status, msg, fields = "http", repr(e)[:120], None
            STORE.apply(status, msg, fields)
            try:
                self._maybe_check_update()
            except Exception:
                pass
            print(f"[poll] {status} {STORE.get().short_label()}" + (f" :: {msg}" if msg else ""), flush=True)
            from gi.repository import GLib
            GLib.idle_add(self.app.refresh_ui)
            self._wake.wait(POLL_INTERVAL_S)
            self._wake.clear()


# ===================== GTK 顶栏 =====================
def build_app():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import Gtk, GLib, AppIndicator3

    have_notify = False
    Notify = None
    try:
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify as _N
        _N.init("Claude Usage Indicator")
        Notify = _N
        have_notify = True
    except Exception:
        pass

    class ClaudeIndicatorApp:
        def __init__(self) -> None:
            self.indicator = AppIndicator3.Indicator.new(
                APP_NAME,
                "network-transmit-receive",
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.indicator.set_label("Claude usage waiting...", "Claude usage")

            self.menu = Gtk.Menu()
            self.item_summary = self._info("Waiting for Claude usage...")
            self.item_session = self._info("Current session: --")
            self.item_all = self._info("All models (weekly): --")
            self.item_sonnet = self._info("Sonnet (weekly): --")
            self.item_opus = self._info("Opus (weekly): --")
            self.item_status = self._info("Status: --")
            self.item_updated = self._info("Updated: --")
            self.item_update = self._info("")  # 有新版本时才显示

            self.menu.append(Gtk.SeparatorMenuItem())
            self._action("Refresh now", self.on_refresh_now)
            self._action("Check for updates", self.on_check_update)
            self._action("Open usage page", self.on_open_page)
            self._action(f"Quit  (v{__version__})", self.on_quit)
            self.menu.show_all()
            self.indicator.set_menu(self.menu)

            self._last_status = "init"
            self._notified_update = None
            self._notification = None
            self.poller: Optional[Poller] = None

            GLib.timeout_add_seconds(1, self._tick)

        def _info(self, text: str):
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            self.menu.append(item)
            return item

        def _action(self, label: str, cb):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            self.menu.append(item)

        def _tick(self) -> bool:
            self.refresh_ui()
            return True

        def refresh_ui(self) -> bool:
            d = STORE.get()
            label = d.short_label()
            self.indicator.set_label(label, label)
            self.item_summary.set_label(label)
            self.item_session.set_label(f"Current session: {d.current_session_used} | reset {d.current_session_reset}")
            self.item_all.set_label(f"All models (weekly): {d.all_models_used} | reset {d.all_models_reset}")
            self.item_sonnet.set_label(f"Sonnet (weekly): {d.sonnet_used}")
            self.item_opus.set_label(f"Opus (weekly): {d.opus_used}")
            self.item_opus.set_visible(d.opus_used != "--")
            status_text = UsageData.STATUS_LABEL.get(d.status, d.status)
            extra = f" — {d.error_msg}" if d.error_msg else ""
            self.item_status.set_label(f"Status: {status_text}{extra}")
            self.item_updated.set_label(f"Updated: {d.received_clock_text()} ({d.refreshed_ago_text()})")

            if d.update_available:
                self.item_update.set_label(f"↑ 有新版 v{d.update_available}：运行 {APP_NAME} --update")
                self.item_update.set_visible(True)
            else:
                self.item_update.set_visible(False)

            # 边沿触发通知：进入异常状态时
            if d.status not in ("ok", "init") and d.status != self._last_status:
                self._notify_status(d)
            self._last_status = d.status

            # 边沿触发通知：发现新版本
            if d.update_available and d.update_available != self._notified_update:
                self._notify_update(d.update_available)
                self._notified_update = d.update_available
            return False

        def _notify(self, title: str, body: str) -> None:
            if have_notify:
                try:
                    n = Notify.Notification.new(title, body, "dialog-warning")
                    n.set_urgency(Notify.Urgency.NORMAL)
                    try:
                        n.add_action("open", "打开用量页", lambda *a: self.on_open_page(None), None)
                    except Exception:
                        pass
                    self._notification = n
                    n.show()
                    return
                except Exception as exc:
                    print(f"[notify] libnotify failed: {exc}", flush=True)
            try:
                subprocess.Popen(["notify-send", "-u", "normal", title, body])
            except Exception as exc:
                print(f"[notify] notify-send failed: {exc}", flush=True)

        def _notify_status(self, d: UsageData) -> None:
            mapping = {
                "auth": ("⚠ Claude 用量：登录已过期", "去 Chrome 打开 claude.ai 重新登录即可恢复。"),
                "cloudflare": ("⚠ Claude 用量：被 Cloudflare 拦截", "TLS 伪装可能失效，脚本或许需要更新。详见 diagnostics 目录。"),
                "schema": ("⚠ Claude 用量：接口结构变了", "用量接口字段变化，脚本需要更新。原始响应已存到 diagnostics 目录。"),
                "cookie": ("⚠ Claude 用量：读取 Chrome cookie 失败", "请确认已登录 claude.ai；keyring 可能未解锁。"),
                "network": ("⚠ Claude 用量：网络错误", "稍后会自动重试。"),
                "http": ("⚠ Claude 用量：请求失败", "稍后会自动重试。"),
            }
            title, body = mapping.get(d.status, ("⚠ Claude 用量异常", d.error_msg))
            if d.error_msg:
                body = f"{body}\n（{d.error_msg}）"
            self._notify(title, body)

        def _notify_update(self, ver: str) -> None:
            self._notify("↑ Claude 用量指示器有新版本",
                         f"v{__version__} → v{ver}\n在终端运行：{APP_NAME} --update")

        def on_refresh_now(self, _w) -> None:
            if self.poller:
                self.poller.wake()

        def on_check_update(self, _w) -> None:
            def worker():
                remote = fetch_remote_version()
                STORE.set_update(remote if remote_is_newer(remote) else None)
                GLib.idle_add(self.refresh_ui)
                if not remote_is_newer(remote):
                    GLib.idle_add(lambda: self._notify("Claude 用量指示器", f"已是最新版 v{__version__}") or False)
            threading.Thread(target=worker, daemon=True).start()

        def on_open_page(self, _w) -> None:
            try:
                subprocess.Popen(["xdg-open", USAGE_PAGE_URL])
            except Exception as exc:
                print(f"[open] xdg-open failed: {exc}", flush=True)

        def on_quit(self, _w) -> None:
            Gtk.main_quit()

    return ClaudeIndicatorApp, Gtk


def run_gui() -> None:
    AppClass, Gtk = build_app()
    app = AppClass()
    poller = Poller(app)
    app.poller = poller
    poller.start()
    print(f"[poller] running v{__version__}, interval={POLL_INTERVAL_S}s", flush=True)
    Gtk.main()


# ===================== CLI =====================
def cmd_once() -> int:
    """拉取一次并打印结果（调试用，不起 GUI）。"""
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
    for k, v in fields.items():
        print(f"  {k}: {v}")
    return 0


def cmd_update() -> int:
    """重新运行安装脚本以更新到最新版。"""
    url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/install.sh"
    print(f"[update] 拉取并运行 {url}")
    return subprocess.call(f"curl -fsSL {url} | bash", shell=True)


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
    p.add_argument("--update", action="store_true", help="更新到最新版（重跑安装脚本）")
    args = p.parse_args()

    if args.once:
        sys.exit(cmd_once())
    if args.check:
        sys.exit(cmd_check())
    if args.update:
        sys.exit(cmd_update())
    run_gui()


if __name__ == "__main__":
    main()
