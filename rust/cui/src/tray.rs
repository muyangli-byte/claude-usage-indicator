//! ksni 托盘（纯 SNI/D-Bus，无 GTK）。完整菜单对齐 Python cui/tray.py：
//! 每档两行（名称|reset + 进度条+%）、Sonnet/Opus 用过才显示、Status 行、More 子菜单全部动作。
//! 顶栏内联文字走 XAyatanaLabel（patched ksni）。
use crate::config::{APP_ID, BUILD_TAG, ID_SUFFIX, LABEL_PREFIX, REPO_URL, USAGE_PAGE_URL, VERSION};
use cui_core::{bar, fmt_countdown, fmt_countdown_long, fmt_resetday, fmt_resetday_long, pct, Raw};
use ksni::menu::{CheckmarkItem, RadioGroup, RadioItem, StandardItem, SubMenu};
use ksni::{MenuItem, ToolTip, Tray};
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
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
    pub consecutive: u32,                 // 连续失败次数（Status 行 >1 时显示 (xN)，对齐 Python）
    // 用量阈值提醒：菜单渲染用 alert_enabled/alert_threshold；shared 原子推给 poller 读
    pub alert_enabled: bool,
    pub alert_threshold: u8,
    pub alert_en_shared: Option<Arc<AtomicBool>>,
    pub alert_thr_shared: Option<Arc<AtomicU8>>,
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
    /// 顶栏内联文字（XAyatanaLabel）+ tooltip 标题。逐字对齐 Python model.short_label：
    /// 取数前只有 auth/cloudflare/schema/cookie 报警，其余一律中性 waiting；取数后 status!=ok 即加 ⚠。
    fn summary(&self) -> String {
        if self.received_at.is_none() {
            return match self.status.as_str() {
                "auth" => "⚠ Claude: login expired",
                "cloudflare" => "⚠ Cloudflare blocked",
                "schema" => "⚠ API schema changed",
                "cookie" => "⚠ cookie read failed",
                _ => "Claude usage waiting...",
            }
            .to_string();
        }
        let r = self.raw.clone().unwrap_or_default();
        let base = format!(
            "Cur {} {} | All {} {}",
            pct(r.five_hour_util),
            fmt_countdown(r.five_hour_reset),
            pct(r.seven_day_util),
            fmt_resetday(r.seven_day_reset),
        );
        if self.status == "ok" {
            base
        } else {
            format!("⚠ {base}")
        }
    }
    fn feedback_url(&self) -> String {
        let body = format!(
            "<!-- describe the issue -->\n\n---\nClaude Usage Indicator{ID_SUFFIX} v{VERSION}\nstatus: {}\nerror: {}",
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
        "Claude usage".into() // 与 Python set_label 标题大小写一致
    }
    fn label(&self) -> String {
        format!("{}{}", LABEL_PREFIX, self.summary()) // prod 无前缀；dev 加 "[rust] "
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
            "Status: {}{}{} | Last updated: {}",
            if self.healthy() { "" } else { "⚠️ " },
            self.status_label(),
            if !self.healthy() && self.consecutive > 1 { format!(" (x{})", self.consecutive) } else { String::new() },
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

        // 用量阈值提醒：开关 + 阈值（current session 穿过阈值时 poller 触发醒目闪窗）
        sub.push(MenuItem::Separator);
        sub.push(
            CheckmarkItem {
                label: "Usage alert (current session)".into(),
                checked: self.alert_enabled,
                activate: Box::new(|t: &mut CuiTray| {
                    t.alert_enabled = !t.alert_enabled;
                    if let Some(a) = &t.alert_en_shared {
                        a.store(t.alert_enabled, Ordering::Relaxed);
                    }
                    crate::creds::write_alert_cfg(t.alert_enabled, t.alert_threshold);
                }),
                ..Default::default()
            }
            .into(),
        );
        {
            const THR: [u8; 5] = [60, 70, 80, 90, 95];
            let selected = THR.iter().position(|&x| x == self.alert_threshold).unwrap_or(2);
            sub.push(
                SubMenu {
                    label: "Alert threshold".into(),
                    submenu: vec![RadioGroup {
                        selected,
                        select: Box::new(|t: &mut CuiTray, idx: usize| {
                            const THR: [u8; 5] = [60, 70, 80, 90, 95];
                            let v = THR.get(idx).copied().unwrap_or(80);
                            t.alert_threshold = v;
                            if let Some(a) = &t.alert_thr_shared {
                                a.store(v, Ordering::Relaxed);
                            }
                            crate::creds::write_alert_cfg(t.alert_enabled, t.alert_threshold);
                        }),
                        options: THR
                            .iter()
                            .map(|p| RadioItem { label: format!("{p}%"), ..Default::default() })
                            .collect(),
                    }
                    .into()],
                    ..Default::default()
                }
                .into(),
            );
        }

        sub.push(act(format!("About (GitHub)  v{VERSION}{BUILD_TAG}"), Box::new(|_| open(REPO_URL))));
        // prod：与 Python 一致，最后是 "Uninstall…"（在分离单元里跑 uninstall.sh --purge 后退出）。
        // dev：Python 无 Quit，但本机测试保留 Quit 更方便。
        #[cfg(not(feature = "dev"))]
        {
            sub.push(MenuItem::Separator);
            sub.push(act(
                "Uninstall…".into(),
                Box::new(|_| {
                    crate::uninstall::spawn_detached_uninstall();
                    std::process::exit(0);
                }),
            ));
        }
        #[cfg(feature = "dev")]
        {
            sub.push(MenuItem::Separator);
            sub.push(act("Quit (rust-dev)".into(), Box::new(|_| std::process::exit(0))));
        }

        items.push(SubMenu { label: "More".into(), submenu: sub, ..Default::default() }.into());
        items
    }
}
