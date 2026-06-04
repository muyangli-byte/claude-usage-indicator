"""GTK AppIndicator 顶栏 + 菜单 + 桌面通知。gi 在 build_app 内惰性导入。
依赖 model / config / api。Poller 由 run_gui（cli）注入到 app.poller，本模块不直接 import 它。"""
from __future__ import annotations

import os
import subprocess
import threading
import time

from cui.api import fetch_remote_version, remote_is_newer
from cui.config import (APP_ID, APP_NAME, APP_ROOT, DISPLAY_VERSION, IS_DEV, NOTIFY_ICONS,
                        NOTIFY_MSG, RENOTIFY_BAD_S, REPO_URL, UPDATE_RESULT, USAGE_PAGE_URL,
                        __version__, _write_config, load_lang)
from cui.model import STORE, UsageData, _bar, _fmt_countdown_long, _fmt_resetday_long


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
                APP_ID, "network-transmit-receive",   # dev 用 -dev 的 id，能与正式版并存
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.indicator.set_label("Claude usage waiting...", "Claude usage")
            # 出故障时切到的 attention 图标（freedesktop 通用名，缺主题也会回退）
            try:
                self.indicator.set_attention_icon_full("dialog-warning", "Claude usage problem")
            except Exception:
                pass

            self.menu = Gtk.Menu()
            # 不再重复托盘标签那行（顶栏已显示 "Cur … | All …"）；菜单直接从分项开始
            # 每个指标两行：名称 / 进度条+%+reset（reset 跟在「定宽进度条」后 → 各行 reset 自动对齐）
            self.item_session_name = self._info("Current session")
            self.item_session_bar = self._info(f"{_bar(None)}   --")
            self.item_all_name = self._info("All models")
            self.item_all_bar = self._info(f"{_bar(None)}   --")
            self.item_sonnet_name = self._info("Sonnet only")
            self.item_sonnet_bar = self._info(f"{_bar(None)}   --")
            self.item_opus_name = self._info("Opus only")
            self.item_opus_bar = self._info(f"{_bar(None)}   --")
            self.item_status = self._info("Status: --")

            self.menu.append(Gtk.SeparatorMenuItem())
            # 仅在出故障时显示：点开把具体故障信息以通知弹出
            self.action_error = self._action("⚠️  Show error details", self.on_show_error)

            # 所有动作都收进 More ▸ 子菜单（hover 展开）
            more = Gtk.MenuItem(label="More")
            submenu = Gtk.Menu()

            def _sub(label, cb):
                it = Gtk.MenuItem(label=label)
                it.connect("activate", cb)
                submenu.append(it)
                return it

            _sub("Refresh now", self.on_refresh_now)
            self.action_update = _sub("Update now", self.on_update_now)
            _sub("Check for updates", self.on_check_update)
            _sub("Open Claude Usage page", self.on_open_page)
            _sub("Send feedback / report issue", self.on_feedback)
            self.action_lang = _sub(self._lang_label(), self.on_toggle_lang)
            _sub(f"About (GitHub)  v{DISPLAY_VERSION}", self.on_about)
            submenu.append(Gtk.SeparatorMenuItem())
            _sub("Uninstall…", self.on_uninstall)
            more.set_submenu(submenu)
            self.menu.append(more)

            self.menu.show_all()
            self.indicator.set_menu(self.menu)
            self.action_update.set_visible(False)  # 只有 check 到新版才显示这一行
            self.action_error.set_visible(False)   # 只有出故障才显示

            self._last_status = "init"
            self._last_notify_t = 0.0
            self._notified_update = None
            # 按「类别」复用通知对象：同类(如反复的异常告警)原地更新同一条、不堆叠；
            # 不同类(更新 vs 告警)各自一条、互不覆盖。保住引用也避免被 GC 导致按钮回调失效。
            self._notifs: dict = {}
            self._notif_text: dict = {}   # kind -> (title, body)，供 Copy 复制
            self.poller = None            # 由 run_gui 注入

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

        def _set_metric(self, name_item, bar_item, name, util, pct_str,
                        reset_dt, countdown=False, sub_limit=False) -> None:
            """两行：名称 + reset / 进度条+%。
            sub_limit=True（Sonnet/Opus 子限额）：未启用（无 reset 且无用量）时整行隐藏，用了才出现。"""
            visible = (not sub_limit) or (reset_dt is not None) or (util not in (None, 0, 0.0))
            name_item.set_visible(visible)
            bar_item.set_visible(visible)
            if not visible:
                return
            if reset_dt is not None:
                rst = ("Resets in " + _fmt_countdown_long(reset_dt)) if countdown \
                    else ("Resets " + _fmt_resetday_long(reset_dt))
                name_item.set_label(f"{name} | {rst}")   # 标题 | reset，紧凑不补空格
            else:
                name_item.set_label(name)
            bar_item.set_label(f"{_bar(util)}  {pct_str:>4}")

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
            label = ("[dev] " if IS_DEV else "") + d.short_label()
            self.indicator.set_label(label, label)
            # 名称行固定；这里更新每个指标的「进度条」与「%+reset」两行
            self._set_metric(self.item_session_name, self.item_session_bar, "Current session",
                             d.five_hour_util, d.current_session_used, d.five_hour_reset, countdown=True)
            self._set_metric(self.item_all_name, self.item_all_bar, "All models",
                             d.seven_day_util, d.all_models_used, d.seven_day_reset)
            self._set_metric(self.item_sonnet_name, self.item_sonnet_bar, "Sonnet only",
                             d.sonnet_util, d.sonnet_used, d.sonnet_reset, sub_limit=True)
            self._set_metric(self.item_opus_name, self.item_opus_bar, "Opus only",
                             d.opus_util, d.opus_used, d.opus_reset, sub_limit=True)
            status_text = UsageData.STATUS_LABEL.get(d.status, d.status)
            bad = d.status not in ("ok", "init")
            # 托盘图标也反映健康：出故障 → ATTENTION（显示 attention 图标），恢复 → ACTIVE。
            # 在图标-only 面板（无 AppIndicator 标签）上，这是唯一能一眼看出异常的途径。
            self.indicator.set_status(
                AppIndicator3.IndicatorStatus.ATTENTION if bad else AppIndicator3.IndicatorStatus.ACTIVE)
            if bad:
                if d.consecutive_failures > 1:
                    status_text += f" (x{d.consecutive_failures})"
                status_text = f"⚠️ {status_text}"   # 故障 emoji；具体信息走「Show error details」
            # Status 行：A | B 格式（详情不再塞这行，点菜单按钮弹通知看）
            self.item_status.set_label(f"Status: {status_text} | Last updated: {d.refreshed_ago_text()}")
            self.action_error.set_visible(bad)   # 仅出故障时显示「Show error details」

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
            self._notif_text[kind] = (title, body)   # 供「Copy」按钮复制当前这条通知的内容
            if have_notify:
                try:
                    # 同一类别复用同一条通知：已存在就原地 update（守护进程按同 id 刷新、不堆叠新横幅）。
                    # 引用存进 self._notifs，避免被 GC 致动作回调失效。
                    n = self._notifs.get(kind)
                    if n is None:
                        n = Notify.Notification.new(title, body, icon)
                        self._notifs[kind] = n
                    else:
                        n.update(title, body, icon)
                    # 按钮每次都按「当前语言」重建（切换语言后按钮也跟着变）。
                    # 每种通知都带「Copy」；update 带「一键更新」，其余带「打开 Claude Usage 页面」。
                    try:
                        n.clear_actions()
                        if kind == "update":
                            n.add_action("update", self.L("一键更新", "Update now"),
                                         lambda *a: self.on_update_now(None), None)
                        else:
                            n.add_action("open", self.L("打开 Claude Usage 页面", "Open Claude Usage page"),
                                         lambda *a: self.on_open_page(None), None)
                        n.add_action("copy", self.L("复制信息", "Copy"),
                                     lambda *a, k=kind: self._copy_notif(k), None)
                    except Exception:
                        pass
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

        def _close_notif(self, kind: str) -> None:
            """主动关掉某一类的常驻通知（如点 Update now 后让「发现新版本」立刻消失）。"""
            n = self._notifs.pop(kind, None)
            if n is not None:
                try:
                    n.close()
                except Exception:
                    pass

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
                self.L(f"v{__version__} → v{ver}\n点下方「一键更新」即可",
                       f"v{__version__} → v{ver}\nClick “Update now” below"),
                kind="update")

        def on_refresh_now(self, _w) -> None:
            if self.poller:
                self.poller.wake(force_creds=True)   # 重读 cookie：重新登录后立刻生效

        def on_show_error(self, _w) -> None:
            # 把当前故障的具体信息以通知弹出（复用分状态的提示文案 + 错误详情）
            self._notify_status(STORE.get())

        def _diag_text(self) -> str:
            """诊断信息（复制/反馈用）：版本 + 状态 + 错误详情 + 桌面环境。"""
            d = STORE.get()
            label = UsageData.STATUS_LABEL.get(d.status, d.status)
            return (f"Claude Usage Indicator v{DISPLAY_VERSION}\n"
                    f"status: {d.status} ({label})\n"
                    f"error: {d.error_msg or '-'}\n"
                    f"desktop: {os.environ.get('XDG_CURRENT_DESKTOP', '?')} / "
                    f"{os.environ.get('XDG_SESSION_TYPE', '?')}")

        def _copy_notif(self, kind: str) -> None:
            """复制该类通知的当前内容（标题+正文）+ 一行上下文（版本/状态/桌面），便于反馈/排查。
            静默复制（不再弹「已复制」以免和带 Copy 按钮的 info 通知相互递归）。"""
            title, body = self._notif_text.get(kind, ("", ""))
            d = STORE.get()
            footer = (f"— Claude Usage Indicator v{DISPLAY_VERSION} | status: {d.status} | "
                      f"{os.environ.get('XDG_CURRENT_DESKTOP', '?')}/{os.environ.get('XDG_SESSION_TYPE', '?')}")
            text = f"{title}\n{body}\n\n{footer}".strip()
            try:
                from gi.repository import Gdk
                cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                cb.set_text(text, -1)
                cb.store()
                print("[copy] copied notification to clipboard", flush=True)
            except Exception as e:
                print(f"[copy] failed: {e}", flush=True)

        def on_feedback(self, _w) -> None:
            # 直接打开 GitHub 新建 issue 的页面，并预填版本/状态/环境信息
            import urllib.parse
            body = (self.L("<!-- 请描述你遇到的问题 -->", "<!-- Please describe the issue -->")
                    + "\n\n\n---\n" + self._diag_text())
            url = (f"{REPO_URL}/issues/new?title="
                   + urllib.parse.quote("Feedback: ")
                   + "&body=" + urllib.parse.quote(body))
            try:
                subprocess.Popen(["xdg-open", url])
            except Exception as e:
                print(f"[feedback] failed: {e}", flush=True)

        def on_check_update(self, _w) -> None:
            def worker():
                remote = fetch_remote_version()
                newer = remote_is_newer(remote)
                if newer:
                    title = self.L("发现新版本", "Update available")
                    body = self.L(f"v{__version__} → v{remote}\n点下方「一键更新」即可",
                                  f"v{__version__} → v{remote}\nClick “Update now” below")

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
            # 点了 Update now：先让那条常驻的「发现新版本」立刻消失，再弹「正在更新…」
            # （正在更新用 info：~4s 自动收起，本进程稍后被重启杀掉也不会残留一条常驻通知）
            self._close_notif("update")
            # 在独立的 systemd 瞬时单元里跑自更新，这样它重启本服务时不会把更新进程一起杀掉
            py = str(APP_ROOT / "venv" / "bin" / "python")
            script = str(APP_ROOT / "claude_usage_indicator.py")
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

            py = str(APP_ROOT / "venv" / "bin" / "python")
            script = str(APP_ROOT / "claude_usage_indicator.py")
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

    return ClaudeIndicatorApp, Gtk
