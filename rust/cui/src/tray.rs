//! ksni 托盘（纯 SNI/D-Bus，无 GTK）。完整菜单对齐 Python cui/tray.py：
//! 每档两行（名称|reset + 进度条+%）、Sonnet/Opus 用过才显示、Status 行、More 子菜单全部动作。
//! 顶栏内联文字走 XAyatanaLabel（patched ksni）。
use crate::config::{APP_ID, REPO_URL, USAGE_PAGE_URL, VERSION};
use cui_core::{bar, fmt_countdown, fmt_countdown_long, fmt_resetday, fmt_resetday_long, pct, Raw};
use ksni::menu::{StandardItem, SubMenu};
use ksni::{MenuItem, ToolTip, Tray};
use std::process::Command;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::Notify;

fn open(url: &str) {
    let _ = Command::new("xdg-open").arg(url).spawn();
}

fn urlencode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

#[derive(Default)]
pub struct CuiTray {
    pub raw: Option<Raw>,
    pub status: String, // ""/init/ok/auth/cloudflare/schema/http/network/cookie
    pub error: String,
    pub update_available: Option<String>,
    pub lang: String, // "en"/"zh"（为后续通知用）
    pub received_at: Option<Instant>,
    pub refresh: Option<Arc<Notify>>, // "Refresh now" → 唤醒轮询（并强制重读 cookie）
    pub show_error: Option<Arc<Notify>>, // "Show error details" → 让 poller 弹当前故障通知
    pub check_update: Option<Arc<Notify>>, // "Check for updates" → 立即查 GitHub 版本
}

impl CuiTray {
    fn healthy(&self) -> bool {
        matches!(self.status.as_str(), "ok" | "init" | "")
    }
    fn status_label(&self) -> &'static str {
        match self.status.as_str() {
            "ok" => "ok",
            "auth" => "login expired",
            "cloudflare" => "Cloudflare blocked",
            "schema" => "API schema changed",
            "http" => "HTTP error",
            "network" => "network error",
            "cookie" => "cookie read failed",
            _ => "starting…",
        }
    }
    fn ago(&self) -> String {
        match self.received_at {
            Some(t) => format!("{}s ago", t.elapsed().as_secs()),
            None => "--".into(),
        }
    }
    /// 顶栏内联文字（XAyatanaLabel）+ tooltip 标题。对齐 Python short_label。
    fn summary(&self) -> String {
        match &self.raw {
            Some(r) if self.received_at.is_some() => {
                let base = format!(
                    "Cur {} {} | All {} {}",
                    pct(r.five_hour_util),
                    fmt_countdown(r.five_hour_reset),
                    pct(r.seven_day_util),
                    fmt_resetday(r.seven_day_reset),
                );
                if self.healthy() {
                    base
                } else {
                    format!("⚠ {base}")
                }
            }
            _ if self.healthy() => "Claude usage…".into(),
            _ => format!("⚠ {}", self.status_label()),
        }
    }
    fn feedback_url(&self) -> String {
        let body = format!(
            "<!-- describe the issue -->\n\n---\nClaude Usage Indicator (rust-dev) v{VERSION}\nstatus: {}\nerror: {}",
            self.status, self.error
        );
        format!("{REPO_URL}/issues/new?title={}&body={}", urlencode("Feedback: "), urlencode(&body))
    }
    fn toggle_lang(&mut self) {
        self.lang = if self.lang == "zh" { "en".into() } else { "zh".into() };
        let path = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default())
            .join(".config/claude-usage-indicator/config.json");
        let mut cfg: serde_json::Value = std::fs::read_to_string(&path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_else(|| serde_json::json!({}));
        cfg["lang"] = serde_json::Value::String(self.lang.clone());
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let _ = std::fs::write(&path, serde_json::to_string_pretty(&cfg).unwrap_or_default());
    }
}

impl Tray for CuiTray {
    fn id(&self) -> String {
        APP_ID.into()
    }
    fn title(&self) -> String {
        "Claude Usage".into()
    }
    fn label(&self) -> String {
        format!("[rust] {}", self.summary()) // dev 版加前缀，与 Python 正式版区分
    }
    fn icon_name(&self) -> String {
        if self.healthy() {
            "network-transmit-receive".into()
        } else {
            "dialog-warning".into()
        }
    }
    fn tool_tip(&self) -> ToolTip {
        ToolTip {
            title: self.summary(),
            description: format!("Status: {} | Last updated: {}", self.status_label(), self.ago()),
            ..Default::default()
        }
    }
    fn menu(&self) -> Vec<MenuItem<Self>> {
        let dim = |s: String| -> MenuItem<Self> {
            StandardItem { label: s, enabled: false, ..Default::default() }.into()
        };
        let act = |label: String, f: Box<dyn Fn(&mut Self) + Send>| -> MenuItem<Self> {
            StandardItem { label, activate: f, ..Default::default() }.into()
        };
        let r = self.raw.clone().unwrap_or_default();
        let used = |u: Option<f64>, has_reset: bool| has_reset || u.map_or(false, |v| v != 0.0);

        let mut items: Vec<MenuItem<Self>> = vec![
            dim(format!("Current session | Resets in {}", fmt_countdown_long(r.five_hour_reset))),
            dim(format!("{}  {:>4}", bar(r.five_hour_util, 24), pct(r.five_hour_util))),
            dim(format!("All models | Resets {}", fmt_resetday_long(r.seven_day_reset))),
            dim(format!("{}  {:>4}", bar(r.seven_day_util, 24), pct(r.seven_day_util))),
        ];
        if used(r.sonnet_util, r.sonnet_reset.is_some()) {
            items.push(dim("Sonnet only".into()));
            items.push(dim(format!("{}  {:>4}", bar(r.sonnet_util, 24), pct(r.sonnet_util))));
        }
        if used(r.opus_util, r.opus_reset.is_some()) {
            items.push(dim("Opus only".into()));
            items.push(dim(format!("{}  {:>4}", bar(r.opus_util, 24), pct(r.opus_util))));
        }
        items.push(dim(format!(
            "Status: {}{} | Last updated: {}",
            if self.healthy() { "" } else { "⚠️ " },
            self.status_label(),
            self.ago()
        )));
        if !self.healthy() {
            // 出故障：点开把具体故障以通知弹出（对齐 Python "Show error details"）
            let se = self.show_error.clone();
            items.push(act(
                "⚠️  Show error details".into(),
                Box::new(move |_| {
                    if let Some(n) = &se {
                        n.notify_one();
                    }
                }),
            ));
        }
        items.push(MenuItem::Separator);

        // More ▸
        let refresh = self.refresh.clone();
        let chk = self.check_update.clone();
        let mut sub: Vec<MenuItem<Self>> = vec![act(
            "Refresh now".into(),
            Box::new(move |_| {
                if let Some(n) = &refresh {
                    n.notify_one();
                }
            }),
        )];
        if let Some(v) = &self.update_available {
            sub.push(act(format!("⬆ Update now → v{v}"), Box::new(|_| crate::selfupdate::spawn_detached())));
        }
        sub.push(act(
            "Check for updates".into(),
            Box::new(move |_| {
                if let Some(n) = &chk {
                    n.notify_one();
                }
            }),
        ));
        sub.push(act("Open Claude Usage page".into(), Box::new(|_| open(USAGE_PAGE_URL))));
        sub.push(act("Send feedback / report issue".into(), Box::new(|t| open(&t.feedback_url()))));
        sub.push(act(
            format!("Notification language: {}", if self.lang == "zh" { "中文" } else { "English" }),
            Box::new(|t| t.toggle_lang()),
        ));
        sub.push(act(format!("About (GitHub)  v{VERSION}-rust-dev"), Box::new(|_| open(REPO_URL))));
        sub.push(MenuItem::Separator);
        sub.push(act("Quit (rust-dev)".into(), Box::new(|_| std::process::exit(0))));

        items.push(SubMenu { label: "More".into(), submenu: sub, ..Default::default() }.into());
        items
    }
}
