"""向 claude.ai 内部用量接口发请求（curl_cffi 伪装 Chrome 过 Cloudflare）、错误分类、
诊断转储，以及 GitHub 版本检查。依赖 model.json_to_raw。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Optional

from cui.config import (APP_NAME, DIAG_DIR, GITHUB_OWNER, GITHUB_REPO, IMPERSONATE,
                        REQUEST_TIMEOUT_S, __version__)
from cui.model import json_to_raw


def client_fingerprint() -> str:
    """返回 'curl_cffi <版本> / impersonate=<目标>'，用于启动日志与诊断头。
    被 Cloudflare 拦截时，多半是 TLS 指纹过期——这行能立刻看出当时用的是哪个版本/目标。"""
    try:
        import curl_cffi
        ver = getattr(curl_cffi, "__version__", "?")
    except Exception:
        ver = "missing"
    return f"curl_cffi {ver} / impersonate={IMPERSONATE}"


# ===================== 异常分类 =====================
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
        header = (f"kind={kind}\nstatus={status_code}\nversion={__version__}\n"
                  f"fingerprint={client_fingerprint()}\ntime={ts}\n\n")
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
            impersonate=IMPERSONATE,  # 关键：伪装 Chrome TLS 指纹，过 Cloudflare
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
