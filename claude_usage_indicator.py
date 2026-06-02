#!/usr/bin/env python3
"""
Claude 用量顶栏指示器（纯 Python，方案 B）。

不需要浏览器开着标签页 / Tampermonkey / 本地 HTTP 服务。原理：
  1. browser_cookie3 自动从 Chrome 的 cookie 库读取 sessionKey + 当前 org id
     （读不到时回退到 ~/.config/claude-usage-indicator/config.json）
  2. curl_cffi 伪装 Chrome 的 TLS 指纹，直接请求 claude.ai 的内部用量接口
     （普通 requests/curl 会被 Cloudflare 以 TLS 指纹拦截，必须用 curl_cffi）
  3. 解析 JSON，更新 GTK AppIndicator 顶栏

刷新策略（自适应）：
  - claude.ai 没有推送通道，只能轮询。数据在变（活跃使用）时快轮询(~10s)≈准实时，
    长时间无变化时自动退避到慢轮询(90s)，出错时按错误间隔重试。
  - 倒计时等显示值在「渲染层」每秒即时重算，所以 2h57m 会平滑跳动，不依赖轮询。

健康监测（心跳）：
  - 每次轮询就是一次健康检查。失败分类 auth/cloudflare/schema/http/network/cookie，
    进入异常立刻弹桌面通知；持续异常每 30 分钟再提醒一次。
  - schema 专门检测「接口结构变化」：必需字段缺失/类型不对时报警并把原始响应 dump 到磁盘。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---- 仓库信息 ----
GITHUB_OWNER = "muyangli-byte"
GITHUB_REPO = "claude-usage-indicator"

APP_NAME = "claude-usage-indicator"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
DATA_DIR = Path.home() / ".local" / "share" / APP_NAME
DIAG_DIR = DATA_DIR / "diagnostics"
CONFIG_PATH = CONFIG_DIR / "config.json"
UPDATE_RESULT = DATA_DIR / "update_result.txt"  # 自更新把 ok|ver / fail|reason 写这里，重启后 GUI 读取并通知


def _read_version() -> str:
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip() or "0.0.0"
    except Exception:
        return "0.0.0"


__version__ = _read_version()


# ===================== 配置 =====================
POLL_FAST_S = 5             # 数据在变（活跃使用）时的快轮询间隔
POLL_SLOW_S = 90            # 长时间无变化时退避到的慢轮询间隔
POLL_ERROR_S = 60           # 出错时的重试间隔（避免猛打一个失败/被拦的接口）
RENOTIFY_BAD_S = 1800       # 持续异常时，每 30 分钟再提醒一次
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


def _write_config(updates: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except Exception:
            pass
        cfg = _read_config()
        cfg.update(updates)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        os.chmod(CONFIG_PATH, 0o600)
    except Exception as e:
        print(f"[config] write failed: {e}", flush=True)


def _default_lang() -> str:
    loc = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
           or os.environ.get("LANG") or "").lower()
    return "zh" if loc.startswith("zh") else "en"


def load_lang() -> str:
    lang = _read_config().get("lang")
    return lang if lang in ("zh", "en") else _default_lang()


def _write_update_result(text: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPDATE_RESULT.write_text(text)
    except Exception:
        pass


# 通知文案中英双语（菜单保持英文原样，只有桌面通知按所选语言切换）
NOTIFY_MSG = {
    "auth": {
        "zh": ("⚠ Claude 用量：登录已过期", "去 Chrome 打开 claude.ai 重新登录即可恢复。"),
        "en": ("⚠ Claude usage: login expired", "Re-login to claude.ai in Chrome to restore."),
    },
    "cloudflare": {
        "zh": ("⚠ Claude 用量：被 Cloudflare 拦截", "TLS 伪装可能失效，脚本或许需要更新；详见 diagnostics 目录。"),
        "en": ("⚠ Claude usage: blocked by Cloudflare", "TLS impersonation may have broken; the tool might need an update. See the diagnostics dir."),
    },
    "schema": {
        "zh": ("⚠ Claude 用量：接口结构变了", "用量接口字段变化，脚本需要更新；原始响应已存到 diagnostics 目录。"),
        "en": ("⚠ Claude usage: API schema changed", "The usage API changed; the tool needs an update. Raw response saved to the diagnostics dir."),
    },
    "cookie": {
        "zh": ("⚠ Claude 用量：读取 Chrome cookie 失败", "请确认已登录 claude.ai；keyring 可能未解锁。"),
        "en": ("⚠ Claude usage: cannot read Chrome cookies", "Make sure you're logged into claude.ai; the keyring may be locked."),
    },
    "network": {
        "zh": ("⚠ Claude 用量：网络错误", "稍后会自动重试。"),
        "en": ("⚠ Claude usage: network error", "Will retry automatically."),
    },
    "http": {
        "zh": ("⚠ Claude 用量：请求失败", "稍后会自动重试。"),
        "en": ("⚠ Claude usage: request failed", "Will retry automatically."),
    },
}


def load_credentials() -> tuple[Optional[str], Optional[str]]:
    """返回 (session_key, org_id)。优先浏览器 cookie，其次配置文件。"""
    import browser_cookie3 as bc3

    sk = org = None
    errors = tried = 0
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

    if sk is None and tried > 0 and errors == tried and not cfg:
        raise CookieError("cannot read browser cookies (keyring locked?)")
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


def _redact(text: str) -> str:
    """抹掉响应里可能出现的凭证（万一接口回显了 cookie/token），再落盘。"""
    if not text:
        return text
    text = re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "sk-ant-***REDACTED***", text)
    text = re.sub(r"(sessionKey=)[^;\s\"']+", r"\1***REDACTED***", text)
    return text


def dump_diagnostics(kind: str, status_code, text: str) -> str:
    """把异常响应写到 diagnostics/，便于事后定位/修脚本。脱敏 + 0600，只保留最近 20 份。"""
    try:
        DIAG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(DIAG_DIR, 0o700)
        except Exception:
            pass
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = DIAG_DIR / f"{ts}-{kind}.txt"
        header = f"kind={kind}\nstatus={status_code}\nversion={__version__}\ntime={ts}\n\n"
        path.write_text(_redact(header + (text or "")[:20000]))
        os.chmod(path, 0o600)
        for old in sorted(DIAG_DIR.glob("*.txt"))[:-20]:
            try:
                old.unlink()
            except Exception:
                pass
        return str(path)
    except Exception:
        return ""


def fetch_usage(session_key: str, org_id: str) -> dict:
    """请求用量接口，返回已校验的原始值 dict。失败抛上面的分类异常。"""
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
    """校验 JSON 契约并抽取原始值。结构不符抛 SchemaError 并 dump 原始响应。"""
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
    return json_to_raw(data)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):  # Python < 3.11 的 fromisoformat 不认 'Z'，统一成 +00:00
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:  # 接口万一不带时区，按 UTC 处理，避免 aware/naive 相减崩溃
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def json_to_raw(j: dict) -> dict:
    """抽取原始数值/时间（不做格式化，格式化留到渲染层即时计算）。"""
    def util(o):
        return o.get("utilization") if isinstance(o, dict) else None

    def reset(o):
        return _parse_iso(o.get("resets_at")) if isinstance(o, dict) else None

    return dict(
        five_hour_util=util(j.get("five_hour")),
        five_hour_reset=reset(j.get("five_hour")),
        seven_day_util=util(j.get("seven_day")),
        seven_day_reset=reset(j.get("seven_day")),
        sonnet_util=util(j.get("seven_day_sonnet")),
        opus_util=util(j.get("seven_day_opus")),
    )


def _pct(u) -> str:
    return "--" if u is None else f"{int(round(u))}%"


def _fmt_countdown(dt: Optional[datetime]) -> str:
    """距接口给的重置时刻 resets_at 还剩多久 -> '2h3m' / '45m'。
    resets_at 是接口真实数据；这里只是用「resets_at - 现在」算出剩余时间（每次渲染即时算，自然倒数）。"""
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
    # 存接口原始值；显示在渲染层即时格式化：当前会话显示距 resets_at 还剩多久（每秒自然倒数），周限显示绝对重置时刻
    five_hour_util: Optional[float] = None
    five_hour_reset: Optional[datetime] = None
    seven_day_util: Optional[float] = None
    seven_day_reset: Optional[datetime] = None
    sonnet_util: Optional[float] = None
    opus_util: Optional[float] = None
    status: str = "init"        # init|ok|auth|cloudflare|schema|http|network|cookie
    error_msg: str = ""
    received_at: Optional[datetime] = None     # 最近一次成功拉取
    changed_at: Optional[datetime] = None      # 最近一次数据「真正」变化
    consecutive_failures: int = 0
    update_available: Optional[str] = None

    # 菜单/托盘统一英文（只有桌面通知按语言切换）
    STATUS_LABEL = {
        "ok": "ok", "auth": "login expired", "cloudflare": "Cloudflare blocked",
        "schema": "API schema changed", "http": "HTTP error", "network": "network error",
        "cookie": "cookie read failed", "init": "starting…",
    }

    # —— 即时计算的显示值 ——
    @property
    def current_session_used(self) -> str:
        return _pct(self.five_hour_util)

    @property
    def current_session_reset(self) -> str:
        return _fmt_countdown(self.five_hour_reset)

    @property
    def all_models_used(self) -> str:
        return _pct(self.seven_day_util)

    @property
    def all_models_reset(self) -> str:
        return _fmt_resetday(self.seven_day_reset)

    @property
    def sonnet_used(self) -> str:
        return _pct(self.sonnet_util)

    @property
    def opus_used(self) -> str:
        return _pct(self.opus_util)

    def snapshot(self) -> tuple:
        """用于「数据是否真的变了」的比较：只看原始值，不看随时间走动的倒计时。"""
        return (self.five_hour_util, self.seven_day_util, self.sonnet_util,
                self.opus_util, self.five_hour_reset, self.seven_day_reset)

    def short_label(self) -> str:
        base = (f"Cur {self.current_session_used} {self.current_session_reset} "
                f"| All {self.all_models_used} {self.all_models_reset}")
        if self.received_at is None:
            return {
                "auth": "⚠ Claude: login expired", "cloudflare": "⚠ Cloudflare blocked",
                "schema": "⚠ API schema changed", "cookie": "⚠ cookie read failed",
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

    def apply(self, status: str, msg: str, fields: Optional[dict]) -> bool:
        """更新数据，返回「原始值是否相比上次发生了变化」。"""
        with self._lock:
            d = self._data
            changed = False
            if fields is not None:
                new_snap = (fields["five_hour_util"], fields["seven_day_util"],
                            fields["sonnet_util"], fields["opus_util"],
                            fields["five_hour_reset"], fields["seven_day_reset"])
                changed = d.received_at is not None and new_snap != d.snapshot()
                for k, v in fields.items():
                    setattr(d, k, v)
                now = datetime.now()
                d.received_at = now
                if changed or d.changed_at is None:
                    d.changed_at = now
                d.consecutive_failures = 0
            else:
                d.consecutive_failures += 1
            d.status = status
            d.error_msg = msg
            return changed

    def set_update(self, version: Optional[str]) -> None:
        with self._lock:
            self._data.update_available = version

    def get(self) -> UsageData:
        with self._lock:
            return UsageData(**vars(self._data))


STORE = UsageStore()


# ===================== 轮询线程（自适应） =====================
class Poller(threading.Thread):
    def __init__(self, app: "ClaudeIndicatorApp") -> None:
        super().__init__(daemon=True)
        self.app = app
        self._wake = threading.Event()
        self._sk: Optional[str] = None
        self._org: Optional[str] = None
        self._last_update_check = 0.0
        self._stable = 0  # 连续无变化的轮询次数（用于退避）

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
            return "auth", "no sessionKey (log into claude.ai)", None
        if not org:
            return "http", "no org id (set org_id in config.json)", None
        try:
            return "ok", "", fetch_usage(sk, org)
        except AuthError:
            try:  # sessionKey 可能轮换了，强制重读 cookie 再试一次
                sk, org = self._creds(force=True)
                return "ok", "", fetch_usage(sk, org)
            except AuthError as e:
                return "auth", str(e), None
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

    def _next_interval(self, status: str, changed: bool) -> int:
        if status != "ok":
            self._stable = 0
            return POLL_ERROR_S
        if changed:
            self._stable = 0
            return POLL_FAST_S
        # 无变化：指数退避 10 -> 20 -> 40 -> 80 -> 90(封顶)
        self._stable += 1
        return min(POLL_SLOW_S, POLL_FAST_S * (2 ** min(self._stable, 5)))

    def run(self) -> None:
        from gi.repository import GLib
        while True:
            try:
                status, msg, fields = self._do_fetch()
            except Exception as e:  # 兜底，绝不让轮询线程挂掉
                status, msg, fields = "http", repr(e)[:120], None
            changed = STORE.apply(status, msg, fields)
            try:
                self._maybe_check_update()
            except Exception:
                pass
            interval = self._next_interval(status, changed)
            tag = ", changed" if changed else ""
            print(f"[poll] {status} {STORE.get().short_label()} (next {interval}s{tag})"
                  + (f" :: {msg}" if msg else ""), flush=True)
            GLib.idle_add(self.app.refresh_ui)
            self._wake.wait(interval)
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
            self.lang = load_lang()
            self.indicator = AppIndicator3.Indicator.new(
                APP_NAME, "network-transmit-receive",
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

            self.menu.append(Gtk.SeparatorMenuItem())
            self._action("Refresh now", self.on_refresh_now)
            self._action("Check for updates", self.on_check_update)
            self.action_update = self._action("Update now", self.on_update_now)
            self._action("Open usage page", self.on_open_page)
            self.action_lang = self._action(self._lang_label(), self.on_toggle_lang)
            self._action(f"Quit  (v{__version__})", self.on_quit)
            self.menu.show_all()
            self.indicator.set_menu(self.menu)
            self.action_update.set_visible(False)  # 只有 check 到新版才显示这一行

            self._last_status = "init"
            self._last_notify_t = 0.0
            self._notified_update = None
            self._notification = None
            self.poller: Optional[Poller] = None

            GLib.timeout_add_seconds(1, self._tick)  # 每秒重绘：倒计时平滑走动 + 健康判断
            GLib.timeout_add_seconds(2, self._consume_update_breadcrumb)  # 自更新重启后通知"已更新到 vX"

        def _info(self, text: str):
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            self.menu.append(item)
            return item

        def _action(self, label: str, cb):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            self.menu.append(item)
            return item

        def L(self, zh: str, en: str) -> str:
            return zh if self.lang == "zh" else en

        def _lang_label(self) -> str:
            return f"Notification language: {'中文' if self.lang == 'zh' else 'English'}"

        def _tick(self) -> bool:
            # 兜底：refresh_ui 万一抛异常也不能让每秒 tick 被 GLib 移除（否则倒计时永久冻结）
            try:
                self.refresh_ui()
            except Exception as e:
                print(f"[ui] tick error: {e!r}", flush=True)
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
            if d.status != "ok" and d.consecutive_failures > 1:
                status_text += f" (x{d.consecutive_failures})"
            extra = f" — {d.error_msg}" if d.error_msg else ""
            self.item_status.set_label(f"Status: {status_text}{extra}")
            self.item_updated.set_label(f"Updated: {d.received_clock_text()} ({d.refreshed_ago_text()})")

            if d.update_available:
                self.action_update.set_label(f"⬆ Update now → v{d.update_available}")
                self.action_update.set_visible(True)
            else:
                self.action_update.set_visible(False)

            # 心跳/健康告警：进入异常立刻提醒；持续异常每 30 分钟再提醒一次
            if d.status not in ("ok", "init"):
                now_t = time.time()
                if d.status != self._last_status or (now_t - self._last_notify_t) > RENOTIFY_BAD_S:
                    self._notify_status(d)
                    self._last_notify_t = now_t
            self._last_status = d.status

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
                        n.add_action("open", self.L("打开用量页", "Open usage page"),
                                     lambda *a: self.on_open_page(None), None)
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
            pair = NOTIFY_MSG.get(d.status)
            if pair:
                title, body = pair[self.lang]
            else:
                title, body = self.L("⚠ Claude 用量异常", "⚠ Claude usage error"), d.error_msg
            if d.error_msg:
                body = f"{body}\n({d.error_msg})"
            self._notify(title, body)

        def _notify_update(self, ver: str) -> None:
            self._notify(
                self.L("↑ Claude 用量指示器有新版本", "↑ New version available"),
                self.L(f"v{__version__} → v{ver}\n托盘菜单点 Update now 一键更新",
                       f"v{__version__} → v{ver}\nClick Update now in the tray menu"))

        def on_refresh_now(self, _w) -> None:
            if self.poller:
                self.poller.wake()

        def on_check_update(self, _w) -> None:
            def worker():
                remote = fetch_remote_version()
                newer = remote_is_newer(remote)
                STORE.set_update(remote if newer else None)
                if newer:
                    title = self.L("↑ 发现新版本", "↑ Update available")
                    body = self.L(f"v{__version__} → v{remote}\n菜单点 Update now 一键更新",
                                  f"v{__version__} → v{remote}\nClick Update now in the menu")

                    def announce():
                        self._notified_update = remote  # 在主线程内设置，避免与 _tick 竞态重复弹
                        self._notify(title, body)
                        self.refresh_ui()
                        return False
                    GLib.idle_add(announce)
                else:
                    title = self.L("Claude 用量指示器", "Claude usage indicator")
                    body = self.L(f"已是最新版 v{__version__}（无需更新）",
                                  f"Already up to date (v{__version__})")
                    GLib.idle_add(lambda: self._notify(title, body) or self.refresh_ui())
            threading.Thread(target=worker, daemon=True).start()

        def on_update_now(self, _w) -> None:
            # 在独立的 systemd 瞬时单元里跑自更新，这样它重启本服务时不会把更新进程一起杀掉
            here = Path(__file__).resolve().parent
            py = str(here / "venv" / "bin" / "python")
            script = str(here / "claude_usage_indicator.py")
            self._notify(self.L("Claude 用量指示器", "Claude usage indicator"),
                         self.L("正在后台更新并重启…", "Updating in the background and restarting…"))
            try:
                subprocess.Popen(
                    ["systemd-run", "--user", "--collect", py, script, "--self-update"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:  # 没有 systemd-run：直接脱离会话起子进程（用 list 避免空格/特殊字符问题）
                subprocess.Popen([py, script, "--self-update"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            # 更新成功会重启服务（新进程启动时读面包屑通知）；失败则本进程仍在，30s 后读面包屑提示
            GLib.timeout_add_seconds(30, self._consume_update_breadcrumb)

        def _consume_update_breadcrumb(self) -> bool:
            # 读取自更新结果并通知一次（成功 ok|版本 / 失败 fail|原因），然后删除。一次性。
            try:
                if not UPDATE_RESULT.exists():
                    return False
                content = UPDATE_RESULT.read_text().strip()
                UPDATE_RESULT.unlink()
            except Exception:
                return False
            kind, _, info = content.partition("|")
            if kind == "ok":
                self._notify(self.L("✓ 已更新", "✓ Updated"),
                             self.L(f"已更新到 v{info} 并重启。", f"Updated to v{info} and restarted."))
            elif kind == "fail":
                self._notify(self.L("⚠ 更新失败", "⚠ Update failed"),
                             self.L(f"{info}\n可在终端运行：{APP_NAME} --update",
                                    f"{info}\nRun in a terminal: {APP_NAME} --update"))
            return False

        def on_open_page(self, _w) -> None:
            try:
                subprocess.Popen(["xdg-open", USAGE_PAGE_URL])
            except Exception as exc:
                print(f"[open] xdg-open failed: {exc}", flush=True)

        def on_toggle_lang(self, _w) -> None:
            self.lang = "en" if self.lang == "zh" else "zh"
            _write_config({"lang": self.lang})
            self.action_lang.set_label(self._lang_label())
            self._notify(self.L("通知语言已切换", "Notification language switched"),
                         self.L("通知将以中文显示。", "Notifications will be shown in English."))

        def on_quit(self, _w) -> None:
            Gtk.main_quit()

    return ClaudeIndicatorApp, Gtk


def run_gui() -> None:
    AppClass, Gtk = build_app()
    app = AppClass()
    poller = Poller(app)
    app.poller = poller
    poller.start()
    print(f"[poller] running v{__version__}, fast={POLL_FAST_S}s slow={POLL_SLOW_S}s", flush=True)
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


def cmd_update() -> int:
    url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/install.sh"
    print(f"[update] 拉取并运行 {url}")
    return subprocess.call(f"curl -fsSL {url} | bash", shell=True)


def cmd_self_update() -> int:
    """轻量自更新（无需 sudo）：在自身安装目录里 git 拉取最新 + pip 装依赖 + 重启服务。
    供托盘「Update now」用；只更新代码/依赖，不动系统库。若系统库有变动请改用 --update。
    把结果（ok|版本 / fail|原因）写到 UPDATE_RESULT，重启后的 GUI 会读取并弹通知。"""
    import shutil

    def fail(msg: str) -> int:
        print(msg)
        _write_update_result(f"fail|{msg}")
        return 1

    here = Path(__file__).resolve().parent
    if not (here / ".git").exists():
        return fail("not a git install dir; use --update instead")
    try:
        # 保护开发副本：有未提交改动就别动，避免 reset 丢工作
        dirty = subprocess.run(["git", "-C", str(here), "status", "--porcelain"],
                               capture_output=True, text=True)
        if dirty.stdout.strip():
            return fail("local uncommitted changes; skipped (update manually or use --update)")
        subprocess.run(["git", "-C", str(here), "fetch", "--depth", "1", "origin", "main"], check=True)
        # ff-only：开发副本若领先/分叉会安全失败，不会丢本地提交
        subprocess.run(["git", "-C", str(here), "merge", "--ff-only", "origin/main"], check=True)
    except subprocess.CalledProcessError:
        return fail("git update failed (local branch ahead/diverged?)")

    # 校验 venv（python 小版本升级后旧 venv 会失效），坏了就重建
    venv = here / "venv"
    py = venv / "bin" / "python"
    if not (py.exists() and subprocess.run([str(py), "-c", "pass"]).returncode == 0):
        try:
            if venv.exists():
                shutil.rmtree(venv)
            subprocess.run(["python3", "-m", "venv", str(venv)], check=True)
        except Exception:
            return fail("venv rebuild failed; use --update")
    pip = venv / "bin" / "pip"
    try:
        subprocess.run([str(pip), "install", "-q", "--upgrade", "pip", "wheel"], check=True)
        subprocess.run([str(pip), "install", "-q", "-r", str(here / "requirements.txt")], check=True)
    except subprocess.CalledProcessError:
        return fail("pip install failed (new system libs needed? use --update)")

    newver = _read_version()
    _write_update_result(f"ok|{newver}")  # 先写成功，重启后新进程读到并通知
    rc = subprocess.run(["systemctl", "--user", "restart", f"{APP_NAME}.service"]).returncode
    if rc != 0:
        return fail(f"service restart failed (rc={rc}); run: systemctl --user restart {APP_NAME}.service")
    print(f"updated and restarted (v{newver})")
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
    args = p.parse_args()

    if args.once:
        sys.exit(cmd_once())
    if args.check:
        sys.exit(cmd_check())
    if args.update:
        sys.exit(cmd_update())
    if args.self_update:
        sys.exit(cmd_self_update())
    run_gui()


if __name__ == "__main__":
    main()
