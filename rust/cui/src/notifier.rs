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
        "auth" => pick("登录已过期\x1f去 Chrome 打开 claude.ai 重新登录即可恢复。", "Login expired\x1fRe-login to claude.ai in Chrome to restore."),
        "cloudflare" => pick("被 Cloudflare 拦截\x1fTLS 伪装可能失效，脚本或许需要更新；详见 diagnostics 目录。", "Blocked by Cloudflare\x1fTLS impersonation may have broken; the tool might need an update."),
        "schema" => pick("接口结构变了\x1f用量接口字段变化，脚本需要更新。", "API schema changed\x1fThe usage API changed; the tool needs an update."),
        "cookie" => pick("读不到登录态\x1f钥匙环可能锁着。解锁钥匙环，或在 config.json 填 session_key+org_id。", "Can't read login\x1fKeyring may be locked. Unlock it, or set session_key+org_id in config.json."),
        "network" => pick("网络错误\x1f稍后会自动重试。", "Network error\x1fWill retry automatically."),
        "http" => pick("请求失败\x1f稍后会自动重试。", "Request failed\x1fWill retry automatically."),
        _ => pick("用量异常", "Usage error"),
    }
}

fn show(
    handles: &mut HashMap<&'static str, NotificationHandle>,
    channel: &'static str,
    icon: &str,
    title: &str,
    body: &str,
    urgency: Urgency,
    timeout: Timeout,
) {
    let mut n = Notification::new();
    n.appname("Claude Usage Indicator").summary(title).body(body).icon(icon).urgency(urgency).timeout(timeout);
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
                    show(&mut handles, "status", "dialog-warning", &title, &body, u, t);
                }
                NotifyCmd::Update { ver, lang } => {
                    let title = if lang == "zh" { "发现新版本" } else { "Update available" };
                    let body = format!("v{} → v{}", crate::config::VERSION, ver);
                    show(&mut handles, "update", "software-update-available", title, &body, Urgency::Critical, Timeout::Never);
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
