//! 桌面通知（notify-rust，纯 zbus）。对齐 Python cui/tray._notify 的智能逻辑：
//! 按 channel 合并(复用同一条 id 原地替换)、按 level 定紧急度/超时、恢复即关。
//! notify-rust 的 show()/close() 是同步阻塞，放在专用线程跑（避开 tokio 嵌套 runtime + handle Send 问题）；
//! poller 通过 channel 发指令。决策（连续≥2 才弹、分级、抑制、恢复关）在 poller 里。
use notify_rust::{Notification, NotificationHandle, Timeout, Urgency};
use std::collections::HashMap;
use std::sync::mpsc::{self, Sender};

pub enum NotifyCmd {
    Status { status: String, error: String, lang: String },
    Update { ver: String, lang: String },
    Updated { ver: String, lang: String },  // 自更新完成后开机首弹
    UpToDate { ver: String, lang: String },  // 用户主动「检查更新」且已是最新 → 瞬时提示(对齐 Python)
    Close(&'static str),
}

/// 6 类故障的双语文案（对齐 Python NOTIFY_MSG）。
fn notify_msg(status: &str, lang: &str) -> (String, String) {
    let zh = lang == "zh";
    let pick = |z: &str, e: &str| -> (String, String) {
        let s = if zh { z } else { e };
        let (t, b) = s.split_once('\x1f').unwrap_or((s, ""));
        (t.to_string(), b.to_string())
    };
    match status {
        "auth" => pick("Claude 登录已过期\x1f① 在 Chrome 重新登录 claude.ai ② 若仍旧报错，完全退出 Chrome 再重开。", "Claude login expired\x1f1) Re-login to claude.ai in Chrome  2) If it persists, fully quit Chrome and reopen."),
        "cloudflare" => pick("被 Cloudflare 拦截\x1fTLS 伪装可能失效，脚本或许需要更新；详见 diagnostics 目录。", "Blocked by Cloudflare\x1fTLS impersonation may have broken; the tool might need an update."),
        "schema" => pick("接口结构变了\x1f用量接口字段变化，脚本需要更新。", "API schema changed\x1fThe usage API changed; the tool needs an update."),
        // 读不到 sessionKey 最常见的原因是浏览器把登录 cookie 只留在内存、没写盘 → 重启 Chrome 即可
        "cookie" => pick("读不到 Claude 登录\x1f① 在 Chrome 登录 claude.ai ② 若已登录仍报此错，完全退出 Chrome 再重开（登录 cookie 可能没存到磁盘）。", "Can't read your Claude login\x1f1) Sign in to claude.ai in Chrome  2) If already signed in, fully quit Chrome and reopen (login cookie may not be saved to disk)."),
        "network" => pick("网络错误\x1f稍后会自动重试。", "Network error\x1fWill retry automatically."),
        "http" => pick("请求失败\x1f稍后会自动重试。", "Request failed\x1fWill retry automatically."),
        _ => pick("用量异常", "Usage error"),
    }
}

#[allow(clippy::too_many_arguments)]
fn show(
    handles: &mut HashMap<&'static str, NotificationHandle>,
    channel: &'static str,
    icon: &str,
    title: &str,
    body: &str,
    urgency: Urgency,
    timeout: Timeout,
    actions: &[(&str, &str)], // (key, label)：按钮；点击经 ActionInvoked 信号派发（见 listen_actions）
) {
    let mut n = Notification::new();
    n.appname("Claude Usage Indicator").summary(title).body(body).icon(icon).urgency(urgency).timeout(timeout);
    for (key, label) in actions {
        n.action(key, label);
    }
    if let Some(h) = handles.get(channel) {
        n.id(h.id()); // 复用同 channel 的 id → 守护进程原地替换，不堆叠
    }
    match n.show() {
        Ok(h) => {
            println!("[notify] shown ch={channel} id={}", h.id());
            handles.insert(channel, h);
        }
        Err(e) => eprintln!("[notify] failed ch={channel}: {e}"),
    }
}

/// 起一个通知线程，返回发指令用的 Sender。
pub fn spawn() -> Sender<NotifyCmd> {
    let (tx, rx) = mpsc::channel::<NotifyCmd>();
    std::thread::spawn(move || {
        let mut handles: HashMap<&'static str, NotificationHandle> = HashMap::new();
        while let Ok(cmd) = rx.recv() {
            match cmd {
                NotifyCmd::Status { status, error, lang } => {
                    let (title, mut body) = notify_msg(&status, &lang);
                    if !error.is_empty() {
                        body = format!("{body}\n({error})");
                    }
                    let (u, t) = if cui_core::status_level(&status) == "critical" {
                        (Urgency::Critical, Timeout::Never)
                    } else {
                        (Urgency::Normal, Timeout::Milliseconds(12000))
                    };
                    let open_label = if lang == "zh" { "打开用量页" } else { "Open page" };
                    show(&mut handles, "status", "dialog-warning", &title, &body, u, t, &[(crate::config::ACTION_OPEN, open_label)]);
                }
                NotifyCmd::Update { ver, lang } => {
                    let title = if lang == "zh" { "发现新版本" } else { "Update available" };
                    let body = format!("v{} → v{}", crate::config::VERSION, ver);
                    let upd_label = if lang == "zh" { "立即更新" } else { "Update now" };
                    show(&mut handles, "update", "software-update-available", title, &body, Urgency::Critical, Timeout::Never, &[(crate::config::ACTION_UPDATE, upd_label)]);
                }
                NotifyCmd::Updated { ver, lang } => {
                    let (title, body) = if lang == "zh" {
                        ("已更新".to_string(), format!("已更新到 v{ver}"))
                    } else {
                        ("Updated".to_string(), format!("Now running v{ver}"))
                    };
                    // 复用 update channel → 顶掉「发现新版本」横幅（同进程内），瞬时展示
                    show(&mut handles, "update", "emblem-default", &title, &body, Urgency::Normal, Timeout::Milliseconds(8000), &[]);
                }
                NotifyCmd::UpToDate { ver, lang } => {
                    let (title, body) = if lang == "zh" {
                        ("已是最新".to_string(), format!("当前 v{ver}"))
                    } else {
                        ("Already up to date".to_string(), format!("You're on v{ver}"))
                    };
                    // 复用 update channel,瞬时展示(对齐 Python on_check_update 的 transient 提示)
                    show(&mut handles, "update", "emblem-default", &title, &body, Urgency::Normal, Timeout::Milliseconds(6000), &[]);
                }
                NotifyCmd::Close(ch) => {
                    if let Some(h) = handles.remove(ch) {
                        h.close();
                    }
                }
            }
        }
    });
    tx
}

/// 监听 org.freedesktop.Notifications 的 ActionInvoked 信号，按 action key 派发通知上的按钮点击。
/// 纯 zbus，不依赖 notify-rust 的 wait_for_action（那会和按 channel 合并/关闭抢句柄）。在 tokio 主运行时跑。
pub async fn listen_actions() {
    use futures_util::StreamExt;
    let conn = match zbus::Connection::session().await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[notify] action listener: {e}");
            return;
        }
    };
    let proxy = match zbus::Proxy::new(
        &conn,
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
    )
    .await
    {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[notify] action listener: {e}");
            return;
        }
    };
    let mut stream = match proxy.receive_signal("ActionInvoked").await {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[notify] action listener: {e}");
            return;
        }
    };
    while let Some(msg) = stream.next().await {
        if let Ok((_id, action)) = msg.body().deserialize::<(u32, String)>() {
            // action key 按通道命名(prod=cui-*，dev=cui-*-dev)：只响应本通道自己的按钮。
            // ActionInvoked 是会话总线广播，dev/prod 同跑时都会收到对方的信号——靠这个区分，
            // 避免点 prod 通知的「更新/打开」连 dev 一起误触(反之亦然)。
            if action == crate::config::ACTION_OPEN {
                let _ = std::process::Command::new("xdg-open").arg(crate::config::USAGE_PAGE_URL).spawn();
            } else if action == crate::config::ACTION_UPDATE {
                crate::selfupdate::spawn_detached();
            }
        }
    }
}
