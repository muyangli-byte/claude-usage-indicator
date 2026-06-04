"""自适应轮询线程：拉取用量、写入 STORE、驱动 UI 刷新；外加常驻 ntfy 订阅做即时版本通知。
依赖 api / credentials / model / config。GLib 在方法内惰性导入（仅 GUI 进程用得到）。"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

from cui.api import (AuthError, CloudflareError, SchemaError, fetch_remote_version,
                     fetch_usage, remote_is_newer)
from cui.config import (APP_NAME, NTFY_TOPIC, POLL_ERROR_S, POLL_FAST_S, POLL_SLOW_S,
                        UPDATE_CHECK_INTERVAL_S, __version__)
from cui.credentials import CookieError, load_credentials
from cui.model import STORE


class Poller(threading.Thread):
    def __init__(self, app) -> None:  # app: ClaudeIndicatorApp（定义在 tray.build_app 内，故不标注）
        super().__init__(daemon=True)
        self.app = app
        self._wake = threading.Event()
        self._force_creds = False  # 手动 Refresh now 时置位：下次拉取强制重读 cookie（重登立刻生效）
        self._sk: Optional[str] = None
        self._org: Optional[str] = None
        self._last_update_check = 0.0
        self._stable = 0  # 连续无变化的轮询次数（用于退避）

    def wake(self, force_creds: bool = False) -> None:
        if force_creds:
            self._force_creds = True
        self._wake.set()

    def _creds(self, force: bool = False) -> "tuple[Optional[str], Optional[str]]":
        if force or not (self._sk and self._org):
            self._sk, self._org = load_credentials()
        return self._sk, self._org

    def _do_fetch(self) -> "tuple[str, str, Optional[dict]]":
        force = self._force_creds   # 手动刷新：强制重读 cookie，省去等 403 才发现 sessionKey 换了
        self._force_creds = False
        try:
            sk, org = self._creds(force=force)
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
            except CookieError as e:   # 重读 cookie 时钥匙环锁了：是「读不到登录态」，不是网络问题
                return "cookie", str(e), None
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
