//! ksni 托盘（纯 SNI/D-Bus，无 GTK）。每档两行（名称|reset + 进度条+%）、Sonnet/Opus 用过才显示、
//! Status 行、Show error details（出故障时）、More…（点开 fltk 动作面板）。
//! 顶栏内联文字走 XAyatanaLabel（patched ksni）。
use crate::config::{APP_ID, ID_SUFFIX, LABEL_PREFIX, REPO_URL, VERSION};
use cui_core::{bar, fmt_countdown, fmt_countdown_long, fmt_resetday, fmt_resetday_long, pct, Raw};
use ksni::menu::StandardItem;
use ksni::{MenuItem, ToolTip, Tray};
use std::sync::mpsc::Sender;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::Notify;

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
    pub update_available: Option<String>, // 有新版本时随 MorePanel 传给弹窗显示「更新到 vX」
    pub received_at: Option<Instant>,
    pub show_error: Option<Arc<Notify>>, // "Show error details" → 让 poller 弹当前故障通知
    pub consecutive: u32,                // 连续失败次数（Status 行 >1 时显示 (xN)，对齐 Python）
    pub ui_tx: Option<Sender<crate::ui::UiCmd>>, // 点「More…」→ 发 MorePanel 让 fltk 弹动作面板
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
    /// 用量进度条文本（托盘菜单与 More 弹窗同源,保证「完全一样」）：4 行基础 + Sonnet/Opus（用过才有）+ Status 行。
    /// main 的 1s 定时器也调它写入共享态,供弹窗实时刷新(倒计时走字)。
    pub(crate) fn usage_lines(&self) -> Vec<String> {
        let r = self.raw.clone().unwrap_or_default();
        let used = |u: Option<f64>, has_reset: bool| has_reset || u.map_or(false, |v| v != 0.0);
        let mut v = vec![
            format!("Current session | Resets in {}", fmt_countdown_long(r.five_hour_reset)),
            format!("{}  {:>4}", bar(r.five_hour_util, 24), pct(r.five_hour_util)),
            format!("All models | Resets {}", fmt_resetday_long(r.seven_day_reset)),
            format!("{}  {:>4}", bar(r.seven_day_util, 24), pct(r.seven_day_util)),
        ];
        if used(r.sonnet_util, r.sonnet_reset.is_some()) {
            v.push("Sonnet only".into());
            v.push(format!("{}  {:>4}", bar(r.sonnet_util, 24), pct(r.sonnet_util)));
        }
        if used(r.opus_util, r.opus_reset.is_some()) {
            v.push("Opus only".into());
            v.push(format!("{}  {:>4}", bar(r.opus_util, 24), pct(r.opus_util)));
        }
        v.push(format!(
            "Status: {}{}{} | Last updated: {}",
            if self.healthy() { "" } else { "⚠️ " },
            self.status_label(),
            if !self.healthy() && self.consecutive > 1 { format!(" (x{})", self.consecutive) } else { String::new() },
            self.ago()
        ));
        v
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
        let mut items: Vec<MenuItem<Self>> = self.usage_lines().into_iter().map(dim).collect();
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

        // More：整合成一个按钮 → 点开 fltk 弹窗，原 More 子菜单里的所有动作(刷新/检查更新/打开页面/
        // 反馈/语言/用量提醒/About/卸载)都在窗里。update_available 与 feedback_url 取点击时的快照传过去。
        items.push(act(
            "More…".into(),
            Box::new(|t: &mut CuiTray| {
                if let Some(tx) = &t.ui_tx {
                    let _ = tx.send(crate::ui::UiCmd::MorePanel {
                        lines: t.usage_lines(),
                        update: t.update_available.clone(),
                        feedback_url: t.feedback_url(),
                    });
                }
            }),
        ));
        items
    }
}
