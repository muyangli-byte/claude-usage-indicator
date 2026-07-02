//! 自适应轮询（对齐 Python Poller）：status≠ok→60s、changed→5s、无变化→指数退避封顶 90s；
//! 定期查 GitHub 版本；可被 Refresh now 唤醒（并强制重读 cookie）。
//! 通知策略（对齐 Python）：恢复即关故障横幅、连续≥2 次失败才弹、版本变化弹更新、Show error details 立即弹。
use crate::config::{POLL_ERROR_S, POLL_FAST_S, POLL_SLOW_S, RENOTIFY_BAD_S, UPDATE_CHECK_S, VERSION};
use crate::notifier::NotifyCmd;
use crate::tray::CuiTray;
use crate::{api, creds};
use cui_core::{remote_is_newer, should_notify_bad, Raw};
use ksni::Handle;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::Notify;
use wreq::Client;

type Snap = (Option<f64>, Option<f64>, Vec<Option<f64>>);
fn snap(r: &Raw) -> Snap {
    (r.five_hour_util, r.seven_day_util, r.scoped.iter().map(|s| s.util).collect())
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

/// 心跳：每轮循环更新 ~/.cache/<APP_ID>/alive 的 mtime。迁移后的 bash 看门狗据此判断
/// Rust 进程是否还在正常轮询（mtime 久不更新 = 卡死/崩溃 → 看门狗恢复 Python）。
fn touch_heartbeat() {
    let base = std::env::var("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".cache"));
    let dir = base.join(crate::config::APP_ID);
    if std::fs::create_dir_all(&dir).is_ok() {
        let _ = std::fs::write(dir.join("alive"), VERSION.as_bytes());
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
    user_initiated: bool, // 用户主动点「检查更新」→ 无论结果都给反馈(已是最新/有更新);定期兜底则只在有新版时弹
) {
    *last_ver_check = Some(Instant::now());
    if let Some(remote) = api::fetch_remote_version(client).await {
        let newer = remote_is_newer(&remote, VERSION);
        if newer && (user_initiated || notified_update.as_deref() != Some(remote.as_str())) {
            let _ = notify_tx.send(NotifyCmd::Update { ver: remote.clone(), lang: lang.to_string() });
            *notified_update = Some(remote.clone());
        } else if !newer && user_initiated {
            // 已是最新且用户主动查 → 瞬时确认(对齐 Python),否则点完没任何反馈
            let _ = notify_tx.send(NotifyCmd::UpToDate { ver: VERSION.to_string(), lang: lang.to_string() });
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
    alert_en: Arc<AtomicBool>,
    alert_thr: Arc<AtomicU8>,
    alert_fired: Arc<AtomicBool>, // 去重/武装标志(与设置窗共享):穿过阈值置位,跌回阈值下清零,改设置时被重置
    cur_util: Arc<AtomicU8>,      // 最近一次 current session 用量,写给设置窗"保存即评估"用
    ui_tx: Sender<crate::ui::UiCmd>,
    lang_zh: Arc<AtomicBool>, // 通知语言(共享):菜单里切换即时生效,无需重启
    lines_shared: Arc<Mutex<Vec<String>>>, // 取数后立即写,弹窗据此低延迟刷新(不必等 1s 定时器)
    active: Arc<Mutex<Option<creds::Account>>>, // 当前账号(托盘/More 面板可随时切换),每轮读
) {
    let mut stable = 0u32;
    let mut last_snap: Option<Snap> = None;
    let mut last_ver_check: Option<Instant> = None;
    // 通知策略状态
    let mut consecutive = 0u32;
    let mut notified_status = String::new();
    let mut last_notify: Option<Instant> = None;
    let mut notified_update: Option<String> = None;

    loop {
        touch_heartbeat();
        let lang = if lang_zh.load(Ordering::Relaxed) { "zh" } else { "en" }; // 每轮取最新(语言可被菜单切换)
        // 当前账号(可被托盘/More 面板随时切换)：每轮读共享态，切换后下一轮(refresh 唤醒)即生效。
        let acct = active.lock().ok().and_then(|g| g.clone());
        let (status, error, raw): (String, String, Option<Raw>) = match &acct {
            None => ("cookie".into(), "no valid sessionKey (login? keyring locked?)".into(), None),
            Some(a) => match api::fetch_usage(&client, &a.session_key, &a.org_id).await {
                Ok(r) => ("ok".into(), String::new(), Some(r)),
                Err(e) => {
                    let m = e.to_string();
                    (classify(&m).into(), m, None)
                }
            },
        };

        let changed = matches!((&raw, &last_snap), (Some(r), Some(prev)) if snap(r) != *prev);
        if let Some(r) = &raw {
            last_snap = Some(snap(r));
        }

        // 先算连续失败次数：托盘 Status 行 >1 时显示 (xN)（对齐 Python），通知策略也用它。
        let bad = status != "ok" && status != "init";
        consecutive = if bad { consecutive + 1 } else { 0 };

        let (st, er, rw, cons) = (status.clone(), error.clone(), raw.clone(), consecutive);
        let ls = lines_shared.clone();
        handle
            .update(move |t: &mut CuiTray| {
                t.status = st;
                t.error = er;
                t.consecutive = cons;
                if let Some(r) = rw {
                    t.raw = Some(r);
                    t.received_at = Some(Instant::now());
                }
                // 取数即写共享态 → 弹窗下一次(0.25s)轮询即可见,不必等每秒定时器
                if let Ok(mut g) = ls.lock() {
                    *g = t.usage_lines();
                }
            })
            .await;

        // 用量阈值提醒：current session 穿过阈值 → 弹一次醒目闪窗;跌回阈值下(或改设置)才重新武装。
        // alert_fired 与设置窗共享:保存设置会重置它,使新阈值立即有机会触发。cur_util 写给设置窗"保存即评估"。
        if let Some(r) = &raw {
            if let Some(u) = r.five_hour_util {
                cur_util.store(u.round().clamp(0.0, 100.0) as u8, Ordering::Relaxed);
                let thr = alert_thr.load(Ordering::Relaxed) as f64;
                if alert_en.load(Ordering::Relaxed) && u >= thr {
                    if !alert_fired.load(Ordering::Relaxed) {
                        alert_fired.store(true, Ordering::Relaxed);
                        println!("[alert] current session {}% >= {}% → popup", u.round() as i64, thr as u8);
                        let _ = ui_tx.send(crate::ui::UiCmd::UsageAlert { pct: u.round().clamp(0.0, 100.0) as u8 });
                    }
                } else if u < thr {
                    alert_fired.store(false, Ordering::Relaxed); // 跌回阈值下 → 重新武装
                }
            }
        }

        // —— 通知策略 ——
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
                    lang: lang.to_string(),
                });
                last_notify = Some(Instant::now());
                notified_status = status.clone();
            }
        }

        // 版本检查（首次 + 每 UPDATE_CHECK_S 兜底）
        if last_ver_check.map_or(true, |t| t.elapsed() >= Duration::from_secs(UPDATE_CHECK_S)) {
            do_version_check(&client, &notify_tx, lang, &handle, &mut notified_update, &mut last_ver_check, false).await;
        }

        let interval = next_interval(&status, changed, &mut stable);
        let tag = if changed { ", changed" } else { "" };
        let extra = if error.is_empty() { String::new() } else { format!(" :: {error}") };
        println!("[poll] {status} (next {interval}s{tag}){extra}");

        let mut fire_error = false;
        let mut do_check = false;
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(interval)) => {}
            _ = refresh.notified() => {} // 唤醒即可：循环顶部会重读 active(可能已被切换)并重拉
            _ = show_error.notified() => { fire_error = true; }
            _ = check_update.notified() => { do_check = true; } // "Check for updates" / ntfy 推送
        }
        // 等待期间用户可能在弹窗里切了语言 → 交互式即时通知(Show error / Check updates)取最新值
        let lang = if lang_zh.load(Ordering::Relaxed) { "zh" } else { "en" };
        if fire_error && bad {
            // Show error details：立即弹当前故障（绕过连续≥2 的门槛），对齐 Python on_show_error
            let _ = notify_tx.send(NotifyCmd::Status { status: status.clone(), error: error.clone(), lang: lang.to_string() });
        }
        if do_check {
            // do_check 来自「检查更新」按钮或 ntfy 推送,均按用户主动处理 → 已是最新也给提示
            do_version_check(&client, &notify_tx, lang, &handle, &mut notified_update, &mut last_ver_check, true).await;
        }
    }
}
