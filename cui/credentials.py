"""凭证读取：browser_cookie3 遍历所有浏览器/profile（GNOME 钥匙环），
KWallet 直查 + 手动 AES 解密（KDE 回退），以及 config.json 显式覆盖。依赖 config。"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
from typing import Optional

from cui.config import BROWSERS, _read_config


class CookieError(Exception):
    """读取/解密浏览器 cookie 失败（keyring 不可用等）。"""


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


def load_credentials() -> "tuple[Optional[str], Optional[str]]":
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
