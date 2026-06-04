"""常量、路径、版本、dev/prod 判定，以及配置/语言/面包屑的读写。无内部依赖。"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ---- 仓库信息 ----
GITHUB_OWNER = "muyangli-byte"
GITHUB_REPO = "claude-usage-indicator"
REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"

APP_NAME = "claude-usage-indicator"
# 安装根目录 = 含 VERSION / venv / claude_usage_indicator.py 入口的目录。
# 本文件在 <root>/cui/config.py，故根目录是上两级。全程用它定位资源，
# 不要再用 Path(__file__).parent（拆包后会指到 cui/ 而非安装目录）。
APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path.home() / ".config" / APP_NAME
DATA_DIR = Path.home() / ".local" / "share" / APP_NAME
DIAG_DIR = DATA_DIR / "diagnostics"
CONFIG_PATH = CONFIG_DIR / "config.json"
UPDATE_RESULT = DATA_DIR / "update_result.txt"  # 自更新把 ok|ver / fail|reason 写这里，重启后 GUI 读取并通知


def _read_version() -> str:
    try:
        return (APP_ROOT / "VERSION").read_text().strip() or "0.0.0"
    except Exception:
        return "0.0.0"


__version__ = _read_version()

# dev / 正式版自动区分：从「安装目录」(~/.local/share/...) 跑 = 正式版；从别处(如开发仓库
# ~/claude-usage-indicator) 跑 = dev。dev 只改运行期标识(托盘 id / 标题 / 版本号显示)，让 dev 能与
# 正式版并存、且一眼可辨；不影响配置/路径/服务，也不会触发发版（发版只看 push + VERSION 变化）。
IS_DEV = APP_ROOT != DATA_DIR
DISPLAY_VERSION = f"{__version__}-dev" if IS_DEV else __version__
APP_ID = APP_NAME + ("-dev" if IS_DEV else "")  # 托盘 indicator id（dev 用不同 id 以便并存）


# ===================== 轮询/通知 参数 =====================
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


# ===================== 配置 / 语言 / 面包屑 =====================
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
        "zh": ("读不到登录态", "已扫描所有浏览器 profile 仍读不到（钥匙环可能锁着）。可解锁钥匙环，或在 config.json 填 session_key+org_id。详见 README。"),  # noqa: E501
        "en": ("Can't read login", "Scanned all browser profiles but found no valid sessionKey (keyring may be locked). Unlock your keyring, or set session_key+org_id in config.json. See README."),  # noqa: E501
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
