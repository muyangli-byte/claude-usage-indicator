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
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---- 仓库信息 ----
GITHUB_OWNER = "muyangli-byte"
GITHUB_REPO = "claude-usage-indicator"
REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"

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
UPDATE_CHECK_INTERVAL_S = 86400  # 轮询兜底：每天查一次（即时通知靠下面的 ntfy 推送）
# 发布即时通知：发布(VERSION 变化)时 GitHub Action 往这个公开 ntfy 主题发一条信号，
# 客户端常驻订阅、收到就立刻去 GitHub 复核版本（GitHub 仍是唯一真相源，ntfy 只当触发器）。
NTFY_TOPIC = "claude-usage-indicator-muyangli-byte-7c1e9a"
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


# 通知文案中英双语。标题简洁、不重复 app 名、不带 emoji（severity 交给图标表达；
# 系统会单独显示应用名 "Claude Usage Indicator"）。菜单仍全英文，只有通知按语言切换。
NOTIFY_MSG = {
    "auth": {
        "zh": ("登录已过期", "去 Chrome 打开 claude.ai 重新登录即可恢复。"),
        "en": ("Login expired", "Re-login to claude.ai in Chrome to restore."),
    },
    "cloudflare": {
        "zh": ("被 Cloudflare 拦截", "TLS 伪装可能失效，脚本或许需要更新；详见 diagnostics 目录。"),
        "en": ("Blocked by Cloudflare", "TLS impersonation may have broken; the tool might need an update. See the diagnostics dir."),
    },
    "schema": {
        "zh": ("接口结构变了", "用量接口字段变化，脚本需要更新；原始响应已存到 diagnostics 目录。"),
        "en": ("API schema changed", "The usage API changed; the tool needs an update. Raw response saved to the diagnostics dir."),
    },
    "cookie": {
        "zh": ("读不到登录态", "已扫描所有浏览器 profile 仍读不到（钥匙环可能锁着）。可解锁钥匙环，或在 config.json 填 session_key+org_id。详见 README。"),
        "en": ("Can't read login", "Scanned all browser profiles but found no valid sessionKey (keyring may be locked). Unlock your keyring, or set session_key+org_id in config.json. See README."),
    },
    "network": {
        "zh": ("网络错误", "稍后会自动重试。"),
        "en": ("Network error", "Will retry automatically."),
    },
    "http": {
        "zh": ("请求失败", "稍后会自动重试。"),
        "en": ("Request failed", "Will retry automatically."),
    },
}

# 通知图标按语义区分（全用 freedesktop 通用图标，主题缺失也会优雅回退）
NOTIFY_ICONS = {"warn": "dialog-warning", "update": "software-update-available", "info": "dialog-information"}


# ---- 凭证形状校验（拒绝"错钥匙解出的乱码"，避免拿垃圾去请求被当成 login expired）----
SK_RE = re.compile(r"^sk-ant-sid\d{2}-[A-Za-z0-9_-]{20,}$")
ORG_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _valid_sk(sk) -> bool:
    return bool(sk) and bool(SK_RE.match(sk))


def _valid_org(o) -> bool:
    return bool(o) and bool(ORG_RE.match(o))


# 每个浏览器：所有 profile 的 cookie 路径 glob（含新版 Network/ 子目录）+ KWallet 产品名
_BROWSERS_INFO = {
    "chrome":   {"globs": ["~/.config/google-chrome/*/Cookies", "~/.config/google-chrome/*/Network/Cookies"], "kw": "Chrome"},
    "chromium": {"globs": ["~/.config/chromium/*/Cookies", "~/.config/chromium/*/Network/Cookies"], "kw": "Chromium"},
    "brave":    {"globs": ["~/.config/BraveSoftware/Brave-Browser/*/Cookies", "~/.config/BraveSoftware/Brave-Browser/*/Network/Cookies"], "kw": "Brave"},
    "edge":     {"globs": ["~/.config/microsoft-edge/*/Cookies", "~/.config/microsoft-edge/*/Network/Cookies"], "kw": "Microsoft Edge"},
}


def _profile_cookie_files(name: str) -> list:
    out = []
    for pat in _BROWSERS_INFO.get(name, {}).get("globs", []):
        out += sorted(glob.glob(os.path.expanduser(pat)))
    return out


def _profile_label(cf: str) -> str:
    """从 cookie 路径推出 profile 名（Default / Profile 3 …），兼容新版 Network/ 子目录。"""
    d = os.path.dirname(cf)
    if os.path.basename(d) == "Network":
        d = os.path.dirname(d)
    return os.path.basename(d)


def _cookie_presence(cookie_file: str) -> tuple:
    """只看某 profile 是否存在 claude.ai 的 sessionKey cookie + 加密版本前缀（v10/v11）。
    不解密、不打印任何密钥；用于 --doctor 报告。返回 (有没有, 'v11'|'v10'|None)。"""
    tmp = None
    try:
        tmp = tempfile.mktemp()
        shutil.copy2(cookie_file, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        r = con.execute("SELECT encrypted_value FROM cookies "
                        "WHERE name='sessionKey' AND host_key LIKE '%claude.ai'").fetchone()
        con.close()
        if r and r[0]:
            return True, bytes(r[0][:3]).decode("ascii", "replace")
        return False, None
    except Exception:
        return False, None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _derive_key(pw: bytes) -> bytes:
    from Cryptodome.Protocol.KDF import PBKDF2
    from Cryptodome.Hash import SHA1
    return PBKDF2(pw, b"saltysalt", 16, 1, hmac_hash_module=SHA1)


def _decrypt_cookie(enc: bytes, safe_pw: bytes, db_version: int, host_key: str) -> str:
    """按 Chromium Linux 方案解密一个 cookie 值（v11=keyring 钥匙, v10=peanuts）。错钥匙会抛异常。"""
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import unpad
    prefix, body = enc[:3], enc[3:]
    if prefix == b"v11":
        key = _derive_key(safe_pw)
    elif prefix == b"v10":
        key = _derive_key(b"peanuts")
    else:
        return enc.decode("utf-8", "replace")
    dec = unpad(AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(body), AES.block_size)
    if db_version >= 24:  # Chrome DB v24+ 在明文前加了 sha256(host_key)
        if dec[:32] != hashlib.sha256(host_key.encode()).digest():
            raise ValueError("domain hash mismatch")
        dec = dec[32:]
    return dec.decode("utf-8")


def _read_creds_from_db(cookie_file: str, safe_pw: bytes) -> tuple:
    """自己读 Cookies SQLite + 解密，返回 (session_key, org_id)。失败返回 (None, None)。"""
    sk = org = None
    tmp = None
    try:
        tmp = tempfile.mktemp()
        shutil.copy2(cookie_file, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            db_version = int(row[0]) if row else 0
        except Exception:
            db_version = 0
        for cname, slot in (("sessionKey", 0), ("lastActiveOrg", 1)):
            try:
                r = con.execute(
                    "SELECT host_key, encrypted_value FROM cookies WHERE name=? AND host_key LIKE '%claude.ai'",
                    (cname,)).fetchone()
                if r and r[1]:
                    val = _decrypt_cookie(r[1], safe_pw, db_version, r[0])
                    if slot == 0:
                        sk = val
                    else:
                        org = val
            except Exception:
                pass
        con.close()
    except Exception:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return sk, org


def _kwallet_password(folder: str, entry: str) -> Optional[str]:
    """非交互地从 KWallet（kwalletd6 优先，再 kwalletd5）读一个密码条目。
    只在钱包已解锁时读，绝不调 open() 触发"创建/解锁密码"弹框；任何异常/超时都返回 None。"""
    try:
        from jeepney import new_method_call, DBusAddress
        from jeepney.io.blocking import open_dbus_connection
    except Exception:
        return None
    APP = "claude-usage-indicator"
    try:
        conn = open_dbus_connection(bus="SESSION")
    except Exception:
        return None
    try:
        dbus = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                           interface="org.freedesktop.DBus")

        def call(addr, method, sig=None, args=()):
            m = new_method_call(addr, method, sig, args) if sig else new_method_call(addr, method)
            return conn.send_and_get_reply(m, timeout=2).body

        for svc, path in (("org.kde.kwalletd6", "/modules/kwalletd6"),
                          ("org.kde.kwalletd5", "/modules/kwalletd5")):
            try:
                if not call(dbus, "NameHasOwner", "s", (svc,))[0]:
                    continue  # 用 NameHasOwner 探测，避免 D-Bus 自动拉起 daemon 弹框
                kw = DBusAddress(path, bus_name=svc, interface="org.kde.KWallet")
                if not call(kw, "isEnabled")[0]:
                    continue
                wallet = call(kw, "networkWallet")[0]
                if not call(kw, "isOpen", "s", (wallet,))[0]:
                    continue  # 未解锁就放弃，绝不 open() 触发弹框
                handle = call(kw, "open", "sxs", (wallet, 0, APP))[0]
                if handle < 0:
                    continue
                try:
                    if not call(kw, "hasFolder", "iss", (handle, folder, APP))[0]:
                        continue
                    pw = call(kw, "readPassword", "isss", (handle, folder, entry, APP))[0]
                    if pw:
                        return pw
                finally:
                    try:
                        call(kw, "close", "ibs", (handle, False, APP))
                    except Exception:
                        pass
            except Exception:
                continue
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return None


def load_credentials() -> tuple[Optional[str], Optional[str]]:
    """返回 (session_key, org_id)，尽量全自动覆盖 多 profile / GNOME / KDE / 无 keyring。

    顺序：① config.json 显式配置（优先，绕过一切）② browser_cookie3 遍历所有浏览器的所有 profile
    ③ KDE 回退：直查 KWallet 拿钥匙 + 自己解密所有 profile。每步都校验 sessionKey 形状，乱码即跳过。"""
    cfg = _read_config()
    sk = cfg.get("session_key") or None
    org = cfg.get("org_id") or None
    if sk and not _valid_sk(sk):
        print("[creds] config.json 的 session_key 格式不对，已忽略", flush=True)
        sk = None
    if org and not _valid_org(org):
        org = None
    if sk and org:
        return sk, org  # 显式配置齐全，绕过浏览器/keyring

    cookie_seen = False

    # Step 1: browser_cookie3 遍历每个浏览器的每个 profile（它自己用可用的 keyring 解密）
    try:
        import browser_cookie3 as bc3
    except Exception:
        bc3 = None
    if bc3 is not None:
        for name in BROWSERS:
            fn = getattr(bc3, name, None)
            if fn is None:
                continue
            for cf in (_profile_cookie_files(name) or [None]):
                try:
                    ck = fn(cookie_file=cf, domain_name="claude.ai") if cf else fn(domain_name="claude.ai")
                    cookies = {c.name: c.value for c in ck}
                except Exception:
                    continue
                if cf is not None:
                    cookie_seen = True
                if not sk and _valid_sk(cookies.get("sessionKey")):
                    sk = cookies.get("sessionKey")
                if not org and _valid_org(cookies.get("lastActiveOrg")):
                    org = cookies.get("lastActiveOrg")
                if sk and org:
                    return sk, org

    # Step 2: KDE 回退——browser_cookie3 拿不到钥匙时，直查 KWallet + 自己解密所有 profile
    if not sk:
        for name in BROWSERS:
            info = _BROWSERS_INFO.get(name)
            files = _profile_cookie_files(name)
            if not info or not files:
                continue
            cookie_seen = True
            pw = _kwallet_password(f'{info["kw"]} Keys', f'{info["kw"]} Safe Storage')
            if not pw:
                continue
            pwb = pw.encode("utf-8")
            for cf in files:
                csk, corg = _read_creds_from_db(cf, pwb)
                if not sk and _valid_sk(csk):
                    sk = csk
                if not org and _valid_org(corg):
                    org = corg
                if sk and org:
                    return sk, org

    if sk:
        return sk, org
    if cookie_seen:
        raise CookieError("found browser cookies but no valid sessionKey (keyring locked/absent?)")
    raise CookieError("no claude.ai cookie found (logged in? right browser?)")


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
            raise CloudflareError(f"HTTP {r.status_code} Cloudflare challenge")
        raise AuthError(f"HTTP {r.status_code}")
    if r.status_code != 200:
        if _is_challenge(r.text):
            dump_diagnostics("cloudflare", r.status_code, r.text)
            raise CloudflareError(f"HTTP {r.status_code} challenge page")
        raise RuntimeError(f"HTTP {r.status_code}")

    try:
        data = r.json()
    except Exception:
        if _is_challenge(r.text):
            dump_diagnostics("cloudflare", 200, r.text)
            raise CloudflareError("HTTP 200 returned a challenge page")
        dump_diagnostics("schema", 200, r.text)
        raise SchemaError("response is not JSON")

    return validate_and_extract(data, r.text)


def validate_and_extract(data, raw_text: str = "") -> dict:
    """校验 JSON 契约并抽取原始值。结构不符抛 SchemaError 并 dump 原始响应（异常消息用英文，会进英文菜单）。"""
    if not isinstance(data, dict):
        dump_diagnostics("schema", 200, raw_text or json.dumps(data)[:20000])
        raise SchemaError("top level is not an object")
    for key in ("five_hour", "seven_day"):
        sub = data.get(key)
        if not isinstance(sub, dict):
            dump_diagnostics("schema", 200, raw_text or json.dumps(data))
            raise SchemaError(f"missing required field {key} (API schema changed?)")
        if not isinstance(sub.get("utilization"), (int, float)):
            dump_diagnostics("schema", 200, raw_text or json.dumps(data))
            raise SchemaError(f"{key}.utilization is not a number (API schema changed?)")
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

    ua = {"User-Agent": f"{APP_NAME}/{__version__}"}
    # 优先 GitHub contents API（raw media type 直接返回文件内容，缓存仅 ~60s）——
    # 比 raw.githubusercontent 的 5 分钟 CDN 缓存新鲜得多，ntfy 推送后能立刻读到新版本。
    api = (f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/VERSION?ref=main")
    try:
        r = creq.get(api, timeout=10,
                     headers={**ua, "Accept": "application/vnd.github.raw+json"})
        if r.status_code == 200 and r.text.strip():
            t = r.text.strip()
            if t[:1] == "{":  # 万一拿到的是 JSON（content 为 base64）
                import base64
                t = base64.b64decode(json.loads(t).get("content", "")).decode().strip()
            if t:
                return t
    except Exception:
        pass
    # 兜底：raw（有 ~5 分钟 CDN 缓存，但作为兜底足够）
    url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/VERSION"
    try:
        r = creq.get(url, timeout=10, headers=ua)
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
    def __init__(self, app) -> None:  # app: ClaudeIndicatorApp（定义在 build_app 内，故不标注）
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

    def _check_update_now(self) -> None:
        """立即查一次新版本（ntfy 推送触发 / 到了轮询间隔都走这里），结果写入 STORE 并刷新 UI。"""
        self._last_update_check = time.time()
        remote = fetch_remote_version()
        STORE.set_update(remote if remote_is_newer(remote) else None)
        try:
            from gi.repository import GLib
            GLib.idle_add(self.app.refresh_ui)
        except Exception:
            pass

    def _maybe_check_update(self) -> None:
        if time.time() - self._last_update_check < UPDATE_CHECK_INTERVAL_S:
            return
        self._check_update_now()

    def _ntfy_loop(self) -> None:
        """常驻订阅 ntfy 主题；收到任意消息就立刻去 GitHub 复核版本（GitHub 仍是真相源，
        所以即便有人往公开主题发垃圾也只是多查一次、不会误报）。断线指数退避重连；
        ntfy 不可达完全不影响每天一次的轮询兜底。"""
        import urllib.request
        url = f"https://ntfy.sh/{NTFY_TOPIC}/json"
        backoff = 5
        while True:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": f"{APP_NAME}/{__version__}"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    backoff = 5  # 连上即重置退避
                    for raw in resp:  # 按行阻塞读取：每条消息一行 JSON，外加周期性 keepalive
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        if ev.get("event") == "message":
                            print("[ntfy] 收到发布信号 → 立即复核 GitHub 版本", flush=True)
                            try:
                                self._check_update_now()
                            except Exception:
                                pass
            except Exception as e:
                print(f"[ntfy] 断开（{type(e).__name__}），{backoff}s 后重连", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

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
        threading.Thread(target=self._ntfy_loop, daemon=True).start()  # 即时发布通知（订阅）
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
            # 不再重复托盘标签那行（顶栏已显示 "Cur … | All …"）；菜单直接从分项开始
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
            self._action(f"About (GitHub)  v{__version__}", self.on_about)
            self.menu.append(Gtk.SeparatorMenuItem())
            self._action("Uninstall…", self.on_uninstall)
            self._action("Quit", self.on_quit)
            self.menu.show_all()
            self.indicator.set_menu(self.menu)
            self.action_update.set_visible(False)  # 只有 check 到新版才显示这一行

            self._last_status = "init"
            self._last_notify_t = 0.0
            self._notified_update = None
            # 按「类别」复用通知对象：同类(如反复的异常告警)原地更新同一条、不堆叠；
            # 不同类(更新 vs 告警)各自一条、互不覆盖。保住引用也避免被 GC 导致按钮回调失效。
            self._notifs: dict = {}
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

        def _notify(self, title: str, body: str, kind: str = "info") -> None:
            icon = NOTIFY_ICONS.get(kind, "dialog-information")
            # GNOME 会忽略自定义超时：普通(normal)通知约 4 秒就收进消息列表，critical 则一直留在横幅直到手动关闭。
            # 其他桌面(KDE/XFCE/MATE)会遵循超时。所以重要的(发现新版本 / 异常)用 critical + 永不超时，
            # 停留够久不易错过；普通信息给 12 秒。
            urgent = kind in ("update", "warn")
            if have_notify:
                try:
                    # 同一类别复用同一条通知：已存在就原地 update（守护进程按同 id 刷新、不堆叠新横幅），
                    # 否则新建并挂上「打开用量页」动作。引用存进 self._notifs，避免被 GC 致动作回调失效。
                    n = self._notifs.get(kind)
                    if n is None:
                        n = Notify.Notification.new(title, body, icon)
                        try:
                            n.add_action("open", self.L("打开用量页", "Open usage page"),
                                         lambda *a: self.on_open_page(None), None)
                        except Exception:
                            pass
                        self._notifs[kind] = n
                    else:
                        n.update(title, body, icon)
                    n.set_urgency(Notify.Urgency.CRITICAL if urgent else Notify.Urgency.NORMAL)
                    n.set_timeout(0 if urgent else 12000)  # 0 = 永不自动消失（直到用户关闭）
                    n.show()
                    return
                except Exception as exc:
                    print(f"[notify] libnotify failed: {exc}", flush=True)
            try:
                # 无 libnotify 时也带上应用名、语义图标、紧急度与停留时长
                subprocess.Popen(["notify-send", "-a", "Claude Usage Indicator", "-i", icon,
                                  "-u", "critical" if urgent else "normal",
                                  "-t", "0" if urgent else "12000", title, body])
            except Exception as exc:
                print(f"[notify] notify-send failed: {exc}", flush=True)

        def _notify_status(self, d: UsageData) -> None:
            pair = NOTIFY_MSG.get(d.status)
            if pair:
                title, body = pair[self.lang]
            else:
                title, body = self.L("用量异常", "Usage error"), d.error_msg
            if d.error_msg:
                body = f"{body}\n({d.error_msg})"
            self._notify(title, body, kind="warn")

        def _notify_update(self, ver: str) -> None:
            self._notify(
                self.L("发现新版本", "Update available"),
                self.L(f"v{__version__} → v{ver}\n托盘菜单点 Update now 一键更新",
                       f"v{__version__} → v{ver}\nClick Update now in the tray menu"),
                kind="update")

        def on_refresh_now(self, _w) -> None:
            if self.poller:
                self.poller.wake()

        def on_check_update(self, _w) -> None:
            def worker():
                remote = fetch_remote_version()
                newer = remote_is_newer(remote)
                if newer:
                    title = self.L("发现新版本", "Update available")
                    body = self.L(f"v{__version__} → v{remote}\n菜单点 Update now 一键更新",
                                  f"v{__version__} → v{remote}\nClick Update now in the menu")

                    def announce():
                        # set_update 与 _notified_update 都在主线程内、同一回调里设置，
                        # 这样 _tick/refresh_ui 永远不会在两者之间看到"有新版但未通知"的中间态
                        self._notified_update = remote
                        STORE.set_update(remote)
                        self._notify(title, body, kind="update")
                        self.refresh_ui()
                        return False
                    GLib.idle_add(announce)
                else:
                    title = self.L("已是最新", "Already up to date")
                    body = self.L(f"当前 v{__version__}", f"You're on v{__version__}")

                    def announce_none():
                        STORE.set_update(None)
                        self._notify(title, body, kind="info")
                        self.refresh_ui()
                        return False
                    GLib.idle_add(announce_none)
            threading.Thread(target=worker, daemon=True).start()

        def on_update_now(self, _w) -> None:
            # 在独立的 systemd 瞬时单元里跑自更新，这样它重启本服务时不会把更新进程一起杀掉
            here = Path(__file__).resolve().parent
            py = str(here / "venv" / "bin" / "python")
            script = str(here / "claude_usage_indicator.py")
            self._notify(self.L("正在更新…", "Updating…"),
                         self.L("正在后台更新并重启。", "Updating in the background and restarting."),
                         kind="info")
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
                self._notify(self.L(f"已更新到 v{info}", f"Updated to v{info}"),
                             self.L("已重启生效。", "Restarted and running."), kind="info")
            elif kind == "fail":
                self._notify(self.L("更新失败", "Update failed"),
                             self.L(f"{info}\n可在终端运行：{APP_NAME} --update",
                                    f"{info}\nRun in a terminal: {APP_NAME} --update"), kind="warn")
            return False

        def on_open_page(self, _w) -> None:
            try:
                subprocess.Popen(["xdg-open", USAGE_PAGE_URL])
            except Exception as exc:
                print(f"[open] xdg-open failed: {exc}", flush=True)

        def on_about(self, _w) -> None:
            try:
                subprocess.Popen(["xdg-open", REPO_URL])
            except Exception as exc:
                print(f"[open] xdg-open failed: {exc}", flush=True)

        def on_uninstall(self, _w) -> None:
            dialog = Gtk.MessageDialog(
                transient_for=None, modal=True,
                message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE,
                text=self.L("卸载 Claude Usage Indicator？", "Uninstall Claude Usage Indicator?"),
            )
            dialog.format_secondary_text(self.L(
                "将停止并删除：后台服务、命令、安装目录、配置——干净无痕。完成后会打开项目主页。\n（随时可用一行命令重新安装。）",
                "This stops and removes the service, command, install directory and config — clean and complete. "
                "The project page opens when done.\n(You can reinstall anytime with the one-liner.)"))
            dialog.add_button(self.L("取消", "Cancel"), Gtk.ResponseType.CANCEL)
            dialog.add_button(self.L("卸载", "Uninstall"), Gtk.ResponseType.OK)
            dialog.set_default_response(Gtk.ResponseType.CANCEL)
            resp = dialog.run()
            dialog.destroy()
            if resp != Gtk.ResponseType.OK:
                return

            here = Path(__file__).resolve().parent
            py = str(here / "venv" / "bin" / "python")
            script = str(here / "claude_usage_indicator.py")
            # 把图形会话环境传给瞬时单元，这样卸载完 xdg-open 才能打开浏览器
            setenv = [f"--setenv={k}={os.environ[k]}"
                      for k in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                                "XAUTHORITY", "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP")
                      if os.environ.get(k)]
            self._notify(self.L("正在卸载…", "Uninstalling…"),
                         self.L("正在删除服务与文件，完成后打开项目主页。",
                                "Removing the service and files; the project page opens when done."), kind="info")
            try:
                # 独立 systemd 瞬时单元：卸载里 systemctl stop 杀掉本服务时不会把卸载进程一起带走
                subprocess.Popen(["systemd-run", "--user", "--collect", *setenv, py, script, "--uninstall"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except FileNotFoundError:  # 没有 systemd-run：脱离会话起子进程兜底
                subprocess.Popen([py, script, "--uninstall"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            Gtk.main_quit()  # 立刻收起托盘图标；卸载在独立单元里继续

        def on_toggle_lang(self, _w) -> None:
            self.lang = "en" if self.lang == "zh" else "zh"
            _write_config({"lang": self.lang})
            self.action_lang.set_label(self._lang_label())
            self._notify(self.L("通知语言：中文", "Notification language: English"),
                         self.L("以后通知用中文显示。", "Notifications will now be in English."), kind="info")

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
    line(f"  ✓ 成功！当前会话 {d.current_session_used}，本周全模型 {d.all_models_used}",
         f"  ✓ Success! Current session {d.current_session_used}, weekly all-models {d.all_models_used}")
    print()
    line("✅ 一切就绪，可以安装。", "✅ All set — ready to install.")
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
    here = Path(__file__).resolve().parent
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


if __name__ == "__main__":
    main()
