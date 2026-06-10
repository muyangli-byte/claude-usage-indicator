//! cui rust-dev 入口：无参 → 托盘 GUI；带子命令 → CLI（--once/--check/--doctor）。
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

use ksni::TrayMethods;
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
            "--self-update" => cmd = "selfupdate",
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
        "version" => println!("cui (rust-dev) v{}", config::VERSION),
        "help" => println!(
            "cui (rust-dev) — Claude usage tray\n\nUSAGE:\n  cui                          run the tray (default)\n  \
             cui --once                   fetch once and print\n  \
             cui --doctor [--lang zh|en]  credential self-check\n  \
             cui --check                  check for updates\n  cui --version"
        ),
        _ => run_gui(lang).await?,
    }
    Ok(())
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

    // 刚自更新过 → 开机弹一次「已更新到 vX」
    if let Some(ver) = selfupdate::consume_breadcrumb() {
        let _ = notify_tx.send(notifier::NotifyCmd::Updated { ver, lang: lang.clone() });
    }

    let tray = tray::CuiTray {
        status: "init".into(),
        lang: lang.clone(),
        refresh: Some(refresh.clone()),
        show_error: Some(show_error.clone()),
        check_update: Some(check_update.clone()),
        ..Default::default()
    };
    let handle = tray.spawn().await?;
    println!("[rust-dev] ksni 托盘已注册 (id={})", config::APP_ID);

    // 常驻订阅 ntfy：发版即时触发版本复核（断线自重连，不影响每天兜底）
    tokio::spawn(ntfy::subscribe(client.clone(), check_update.clone()));

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

    poller::run(handle, client, refresh, show_error, check_update, notify_tx, lang, sk, org).await;
    Ok(())
}
