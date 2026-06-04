"""数据模型、线程安全存储，以及纯格式化 / JSON 解析。全部无副作用、无内部依赖。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# ===================== 解析 =====================
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
        sonnet_reset=reset(j.get("seven_day_sonnet")),
        opus_util=util(j.get("seven_day_opus")),
        opus_reset=reset(j.get("seven_day_opus")),
    )


# ===================== 格式化（渲染层即时计算） =====================
def _pct(u) -> str:
    return "--" if u is None else f"{int(round(u))}%"


def _bar(u, n: int = 24) -> str:
    """纯文字进度条：▕████░░░░▏。进度条独占一行、行首对齐，所以各行的条是齐的。"""
    if u is None:
        return "▕" + "░" * n + "▏"
    p = max(0.0, min(100.0, float(u)))
    f = int(round(n * p / 100.0))
    return "▕" + "█" * f + "░" * (n - f) + "▏"


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


# —— 菜单用的全格式（和网页一致）：'3 hr 17 min' / 'Mon 7:00 AM'。托盘标签仍用上面的简写以省空间。——
def _fmt_countdown_long(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "0 min"
    h, m = int(secs // 3600), int((secs % 3600) // 60)
    parts = []
    if h:
        parts.append(f"{h} hr")
    if m or not h:
        parts.append(f"{m} min")
    return " ".join(parts)


def _fmt_resetday_long(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    loc = dt.astimezone()
    h12 = loc.strftime("%I").lstrip("0") or "12"
    return f"{loc.strftime('%a')} {h12}:{loc.strftime('%M')} {loc.strftime('%p')}"


# ===================== 数据模型 =====================
@dataclass
class UsageData:
    # 存接口原始值；显示在渲染层即时格式化：当前会话显示距 resets_at 还剩多久（每秒自然倒数），周限显示绝对重置时刻
    five_hour_util: Optional[float] = None
    five_hour_reset: Optional[datetime] = None
    seven_day_util: Optional[float] = None
    seven_day_reset: Optional[datetime] = None
    sonnet_util: Optional[float] = None
    sonnet_reset: Optional[datetime] = None
    opus_util: Optional[float] = None
    opus_reset: Optional[datetime] = None
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
