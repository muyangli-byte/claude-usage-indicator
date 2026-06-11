//! cui 入口：无参 → 托盘 GUI；带子命令 → CLI（--once/--check/--doctor/--self-update）。
//! 与 Python 正式版并存（独立 APP_ID）。凭证/拉取/托盘/通知全自包含，无 GTK、单二进制。
mod api;
mod cli;
mod config;
mod creds;
mod kwallet;
mod notifier;
mod ntfy;
mod poller;
mod selfupdate;
mod tray;
mod ui;
mod uninstall;

use ksni::TrayMethods;
use std::sync::atomic::{AtomicBool, AtomicU8};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Notify;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let mut cmd = "gui";
    let mut lang = creds::load_lang();
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--once" => cmd = "once",
            "--check" => cmd = "check",
            "--doctor" => cmd = "doctor",
            "--self-update" | "--update" => cmd = "selfupdate", // Rust 客户端的"更新"=自更新二进制
            "--uninstall" => cmd = "uninstall",
            // 调试子命令仅 dev 构建可用:prod 里 More 面板含真实「卸载」按钮,不能被调试旗触达
            #[cfg(feature = "dev")]
            "--test-alert" => cmd = "testalert", // 弹用量提醒闪窗 + 设置窗
            #[cfg(feature = "dev")]
            "--test-settings" => cmd = "testsettings", // 只弹设置窗
            #[cfg(feature = "dev")]
            "--test-more" => cmd = "testmore", // 弹 More 动作面板
            "--version" | "-V" => cmd = "version",
            "--help" | "-h" => cmd = "help",
            "--lang" => {
                if let Some(v) = it.next() {
                    lang = v;
                }
            }
            other if other.starts_with("--lang=") => lang = other[7..].to_string(),
            _ => {}
        }
    }

    match cmd {
        "once" => std::process::exit(cli::cmd_once().await),
        "check" => std::process::exit(cli::cmd_check().await),
        "doctor" => std::process::exit(cli::cmd_doctor(&lang).await),
        "selfupdate" => std::process::exit(selfupdate::cmd_self_update().await),
        "uninstall" => {
            uninstall::spawn_detached_uninstall();
            println!("uninstall started in a detached unit");
        }
        #[cfg(feature = "dev")]
        "testalert" => {
            let tx = test_ui(&lang, true, 80);
            // 演示两个窗口:用量提醒闪窗 + 设置窗(开关 + 阈值)
            let _ = tx.send(ui::UiCmd::UsageAlert { pct: 80 });
            let _ = tx.send(ui::UiCmd::AlertSettings);
            std::thread::sleep(Duration::from_secs(130)); // 保持进程存活让窗口显示
        }
        #[cfg(feature = "dev")]
        "testsettings" => {
            let tx = test_ui(&lang, false, 80);
            let _ = tx.send(ui::UiCmd::AlertSettings);
            std::thread::sleep(Duration::from_secs(130));
        }
        #[cfg(feature = "dev")]
        "testmore" => {
            let tx = test_ui(&lang, false, 80);
            let _ = tx.send(ui::UiCmd::MorePanel {
                update: Some("9.9.9".into()), // 演示「更新到 vX」按钮
                feedback_url: format!("{}/issues/new", config::REPO_URL),
            });
            std::thread::sleep(Duration::from_secs(130));
        }
        "version" => println!("claude-usage-indicator {}{}", config::VERSION, config::BUILD_TAG),
        "help" => println!(
            "claude-usage-indicator{} — Claude usage tray\n\nUSAGE:\n  cui                          run the tray (default)\n  \
             cui --once                   fetch once and print\n  \
             cui --doctor [--lang zh|en]  credential self-check\n  \
             cui --check                  check for updates\n  \
             cui --update                 update to the latest release\n  \
             cui --uninstall              remove Claude Usage Indicator\n  cui --version",
            config::ID_SUFFIX
        ),
        _ => run_gui(lang).await?,
    }
    Ok(())
}

/// 调试:起一个 ui 线程并带上哑 refresh/check_update 句柄,供 --test-* 子命令独立弹窗(仅 dev)。
#[cfg(feature = "dev")]
fn test_ui(lang: &str, alert_en: bool, alert_thr: u8) -> std::sync::mpsc::Sender<ui::UiCmd> {
    ui::spawn(
        Arc::new(AtomicBool::new(alert_en)),
        Arc::new(AtomicU8::new(alert_thr)),
        Arc::new(AtomicBool::new(lang == "zh")),
        Arc::new(Notify::new()),
        Arc::new(Notify::new()),
    )
}

async fn run_gui(lang: String) -> anyhow::Result<()> {
    let (sk, org) = match creds::load_credentials().await {
        Ok(v) => {
            println!("[creds] 已自读凭证 (org={})", v.1);
            v
        }
        Err(e) => {
            eprintln!("[creds] {e}");
            (String::new(), String::new())
        }
    };
    let client = api::client()?;
    let refresh = Arc::new(Notify::new());
    let show_error = Arc::new(Notify::new());
    let check_update = Arc::new(Notify::new());
    let notify_tx = notifier::spawn();

    // 用量阈值提醒：从 config 载入开关/阈值,共享原子供 poller 读、设置窗写;通知语言也用共享原子
    // (弹窗里切换即时生效、连 poller 的通知语言一起变)。ui 线程跑 fltk,捕获这些句柄+Notify 供窗里按钮跨线程触发。
    let (alert_en0, alert_thr0) = creds::read_alert_cfg();
    let alert_enabled = Arc::new(AtomicBool::new(alert_en0));
    let alert_threshold = Arc::new(AtomicU8::new(alert_thr0));
    let lang_zh = Arc::new(AtomicBool::new(lang == "zh"));
    let ui_tx = ui::spawn(
        alert_enabled.clone(),
        alert_threshold.clone(),
        lang_zh.clone(),
        refresh.clone(),
        check_update.clone(),
    );

    // 刚自更新过 → 开机弹一次「已更新到 vX」
    if let Some(ver) = selfupdate::consume_breadcrumb() {
        let _ = notify_tx.send(notifier::NotifyCmd::Updated { ver, lang: lang.clone() });
    }

    let tray = tray::CuiTray {
        status: "init".into(),
        show_error: Some(show_error.clone()),
        ui_tx: Some(ui_tx.clone()),
        ..Default::default()
    };
    let handle = tray.spawn().await?;
    println!("[cui] ksni tray registered (id={})", config::APP_ID);

    // 常驻订阅 ntfy：发版即时触发版本复核（断线自重连，不影响每天兜底）
    tokio::spawn(ntfy::subscribe(client.clone(), check_update.clone()));
    // 监听通知上的按钮点击（Open page / Update now），经 D-Bus ActionInvoked 信号派发
    tokio::spawn(notifier::listen_actions());

    // 每秒重绘：倒计时 / “Ns ago” / 顶栏标签平滑走动（ksni 按哈希去重，未变不发 D-Bus）。
    {
        let h = handle.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(1)).await;
                let _ = h.update(|_| {}).await;
            }
        });
    }

    poller::run(handle, client, refresh, show_error, check_update, notify_tx,
                alert_enabled, alert_threshold, ui_tx, lang_zh, sk, org).await;
    Ok(())
}
