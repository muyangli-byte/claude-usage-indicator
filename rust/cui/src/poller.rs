//! 自适应轮询（对齐 Python Poller）：status≠ok→60s、changed→5s、无变化→指数退避封顶 90s；
//! 定期查 GitHub 版本；可被 Refresh now / Check for updates 唤醒（并强制重读 cookie）。
use crate::config::{POLL_ERROR_S, POLL_FAST_S, POLL_SLOW_S, UPDATE_CHECK_S, VERSION};
use crate::tray::CuiTray;
use crate::{api, creds};
use cui_core::{remote_is_newer, Raw};
use ksni::Handle;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Notify;
use wreq::Client;

// 仅比较原始值（不含随时间走的倒计时），对齐 Python snapshot
type Snap = (Option<f64>, Option<f64>, Option<f64>, Option<f64>);
fn snap(r: &Raw) -> Snap {
    (r.five_hour_util, r.seven_day_util, r.sonnet_util, r.opus_util)
}

fn classify(msg: &str) -> &'static str {
    match msg.split(':').next().unwrap_or("") {
        "auth" => "auth",
        "cloudflare" => "cloudflare",
        "schema" => "schema",
        "http" => "http",
        "cookie" => "cookie",
        _ => "network", // 连接/超时等
    }
}

fn next_interval(status: &str, changed: bool, stable: &mut u32) -> u64 {
    if status != "ok" {
        *stable = 0;
        return POLL_ERROR_S;
    }
    if changed {
        *stable = 0;
        return POLL_FAST_S;
    }
    *stable += 1;
    POLL_SLOW_S.min(POLL_FAST_S * 2u64.pow((*stable).min(5)))
}

pub async fn run(handle: Handle<CuiTray>, client: Client, refresh: Arc<Notify>, mut sk: String, mut org: String) {
    let mut stable = 0u32;
    let mut last_snap: Option<Snap> = None;
    let mut force_creds = sk.is_empty() || org.is_empty();
    let mut last_ver_check: Option<Instant> = None;

    loop {
        if force_creds {
            if let Ok((s, o)) = creds::load_credentials().await {
                sk = s;
                org = o;
            }
            force_creds = false;
        }

        let (status, error, raw): (String, String, Option<Raw>) = if sk.is_empty() || org.is_empty() {
            ("cookie".into(), "no valid sessionKey (login? keyring locked?)".into(), None)
        } else {
            match api::fetch_usage(&client, &sk, &org).await {
                Ok(r) => ("ok".into(), String::new(), Some(r)),
                Err(e) => {
                    let m = e.to_string();
                    (classify(&m).into(), m, None)
                }
            }
        };

        let changed = matches!((&raw, &last_snap), (Some(r), Some(prev)) if snap(r) != *prev);
        if let Some(r) = &raw {
            last_snap = Some(snap(r));
        }

        let (st, er, rw) = (status.clone(), error.clone(), raw.clone());
        handle
            .update(move |t: &mut CuiTray| {
                t.status = st;
                t.error = er;
                if let Some(r) = rw {
                    t.raw = Some(r);
                    t.received_at = Some(Instant::now());
                }
            })
            .await;

        // 版本检查：首次 + 每 UPDATE_CHECK_S（Refresh now 唤醒也会顺带触发，因为间隔判定）
        if last_ver_check.map_or(true, |t| t.elapsed() >= Duration::from_secs(UPDATE_CHECK_S)) {
            last_ver_check = Some(Instant::now());
            if let Some(remote) = api::fetch_remote_version(&client).await {
                let newer = remote_is_newer(&remote, VERSION);
                handle
                    .update(move |t: &mut CuiTray| {
                        t.update_available = if newer { Some(remote) } else { None };
                    })
                    .await;
            }
        }

        let interval = next_interval(&status, changed, &mut stable);
        let tag = if changed { ", changed" } else { "" };
        let extra = if error.is_empty() { String::new() } else { format!(" :: {error}") };
        println!("[poll] {status} (next {interval}s{tag}){extra}");

        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(interval)) => {}
            _ = refresh.notified() => { force_creds = true; }  // Refresh now：立刻醒 + 重读 cookie
        }
    }
}
