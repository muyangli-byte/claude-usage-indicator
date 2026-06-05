//! cui rust-dev 入口：env 取凭证 → ksni 托盘 → 轮询拉用量 → 刷新托盘。
//! 迁移期最小可运行纵切：证明 ksni 托盘 + wreq 拉取能在 GNOME 上与 Python 正式版并存。
//! 凭证暂从 CUI_SK/CUI_ORG 环境变量读取（真凭证层 credentials 模块为下一步）。
mod api;
mod config;
mod tray;

use cui_core::{bar, fmt_countdown, fmt_countdown_long, fmt_resetday, fmt_resetday_long, pct};
use ksni::TrayMethods;
use std::time::Duration;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let sk = std::env::var("CUI_SK").unwrap_or_default();
    let org = std::env::var("CUI_ORG").unwrap_or_default();
    let client = api::client()?;

    let handle = tray::CuiTray::default().spawn().await?;
    println!(
        "[rust-dev] ksni 托盘已注册 (id={})，每 {}s 刷新",
        config::APP_ID,
        config::POLL_SECS
    );

    loop {
        let result = if sk.is_empty() || org.is_empty() {
            Err(anyhow::anyhow!("未设 CUI_SK/CUI_ORG（凭证层待建）"))
        } else {
            api::fetch_usage(&client, &sk, &org).await
        };

        handle
            .update(|t: &mut tray::CuiTray| match &result {
                Ok(raw) => {
                    t.healthy = true;
                    t.summary = format!(
                        "Cur {} {} | All {} {}",
                        pct(raw.five_hour_util),
                        fmt_countdown(raw.five_hour_reset),
                        pct(raw.seven_day_util),
                        fmt_resetday(raw.seven_day_reset),
                    );
                    t.rows = vec![
                        format!("Current session | Resets in {}", fmt_countdown_long(raw.five_hour_reset)),
                        format!("{}  {}", bar(raw.five_hour_util, 24), pct(raw.five_hour_util)),
                        format!("All models | Resets {}", fmt_resetday_long(raw.seven_day_reset)),
                        format!("{}  {}", bar(raw.seven_day_util, 24), pct(raw.seven_day_util)),
                    ];
                    t.status_line = "Status: ok".into();
                }
                Err(e) => {
                    t.healthy = false;
                    t.summary = "⚠ Claude usage".into();
                    t.status_line = format!("Status: {e}");
                }
            })
            .await;

        match &result {
            Ok(raw) => println!("[poll] ok | Cur {} | All {}", pct(raw.five_hour_util), pct(raw.seven_day_util)),
            Err(e) => println!("[poll] err | {e}"),
        }
        tokio::time::sleep(Duration::from_secs(config::POLL_SECS)).await;
    }
}
