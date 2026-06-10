//! 自适应轮询（对齐 Python Poller）：status≠ok→60s、changed→5s、无变化→指数退避封顶 90s；
//! 定期查 GitHub 版本；可被 Refresh now 唤醒（并强制重读 cookie）。
//! 通知策略（对齐 Python）：恢复即关故障横幅、连续≥2 次失败才弹、版本变化弹更新、Show error details 立即弹。
use crate::config::{POLL_ERROR_S, POLL_FAST_S, POLL_SLOW_S, RENOTIFY_BAD_S, UPDATE_CHECK_S, VERSION};
use crate::notifier::NotifyCmd;
use crate::tray::CuiTray;
use crate::{api, creds};
use cui_core::{remote_is_newer, should_notify_bad, Raw};
use ksni::Handle;
use std::sync::mpsc::Sender;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Notify;
use wreq::Client;

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
        _ => "network",
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

/// 查一次 GitHub 版本（到间隔 / Refresh 之外的 ntfy 推送 / "Check for updates" 都走这里）：
/// 比当前新且没弹过就弹更新通知，并刷新菜单的 update_available。
#[allow(clippy::too_many_arguments)]
async fn do_version_check(
    client: &Client,
    notify_tx: &Sender<NotifyCmd>,
    lang: &str,
    handle: &Handle<CuiTray>,
    notified_update: &mut Option<String>,
    last_ver_check: &mut Option<Instant>,
) {
    *last_ver_check = Some(Instant::now());
    if let Some(remote) = api::fetch_remote_version(client).await {
        let newer = remote_is_newer(&remote, VERSION);
        if newer && notified_update.as_deref() != Some(remote.as_str()) {
            let _ = notify_tx.send(NotifyCmd::Update { ver: remote.clone(), lang: lang.to_string() });
            *notified_update = Some(remote.clone());
        }
        let upd = if newer { Some(remote) } else { None };
        handle.update(move |t: &mut CuiTray| t.update_available = upd).await;
    }
}

#[allow(clippy::too_many_arguments)]
pub async fn run(
    handle: Handle<CuiTray>,
    client: Client,
    refresh: Arc<Notify>,
    show_error: Arc<Notify>,
    check_update: Arc<Notify>,
    notify_tx: Sender<NotifyCmd>,
    lang: String,
    mut sk: String,
    mut org: String,
) {
    let mut stable = 0u32;
    let mut last_snap: Option<Snap> = None;
    let mut force_creds = sk.is_empty() || org.is_empty();
    let mut last_ver_check: Option<Instant> = None;
    // 通知策略状态
    let mut consecutive = 0u32;
    let mut notified_status = String::new();
    let mut last_notify: Option<Instant> = None;
    let mut notified_update: Option<String> = None;

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

        // —— 通知策略 ——
        let bad = status != "ok" && status != "init";
        consecutive = if bad { consecutive + 1 } else { 0 };
        if !bad {
            if !notified_status.is_empty() {
                let _ = notify_tx.send(NotifyCmd::Close("status")); // 恢复即关
                notified_status.clear();
            }
        } else {
            let secs = last_notify.map_or(f64::INFINITY, |t| t.elapsed().as_secs_f64());
            if should_notify_bad(consecutive, &status, &notified_status, secs, RENOTIFY_BAD_S) {
                let _ = notify_tx.send(NotifyCmd::Status {
                    status: status.clone(),
                    error: error.clone(),
                    lang: lang.clone(),
                });
                last_notify = Some(Instant::now());
                notified_status = status.clone();
            }
        }

        // 版本检查（首次 + 每 UPDATE_CHECK_S 兜底）
        if last_ver_check.map_or(true, |t| t.elapsed() >= Duration::from_secs(UPDATE_CHECK_S)) {
            do_version_check(&client, &notify_tx, &lang, &handle, &mut notified_update, &mut last_ver_check).await;
        }

        let interval = next_interval(&status, changed, &mut stable);
        let tag = if changed { ", changed" } else { "" };
        let extra = if error.is_empty() { String::new() } else { format!(" :: {error}") };
        println!("[poll] {status} (next {interval}s{tag}){extra}");

        let mut fire_error = false;
        let mut do_check = false;
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(interval)) => {}
            _ = refresh.notified() => { force_creds = true; }
            _ = show_error.notified() => { fire_error = true; }
            _ = check_update.notified() => { do_check = true; } // "Check for updates" / ntfy 推送
        }
        if fire_error && bad {
            // Show error details：立即弹当前故障（绕过连续≥2 的门槛），对齐 Python on_show_error
            let _ = notify_tx.send(NotifyCmd::Status { status: status.clone(), error: error.clone(), lang: lang.clone() });
        }
        if do_check {
            do_version_check(&client, &notify_tx, &lang, &handle, &mut notified_update, &mut last_ver_check).await;
        }
    }
}
