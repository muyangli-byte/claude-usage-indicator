//! cui rust-dev 入口：自读凭证 → ksni 托盘（含 XAyatanaLabel 内联标签）→ 自适应轮询。
//! 与 Python 正式版并存（独立 APP_ID）。凭证/拉取/托盘全自包含，无 GTK、单二进制。
mod api;
mod config;
mod creds;
mod poller;
mod tray;

use ksni::TrayMethods;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Notify;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // 自读凭证（GNOME Secret Service + 自解密）。sk 绝不打印。
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

    let tray = tray::CuiTray {
        status: "init".into(),
        lang: creds::load_lang(),
        refresh: Some(refresh.clone()),
        ..Default::default()
    };
    let handle = tray.spawn().await?;
    println!("[rust-dev] ksni 托盘已注册 (id={})", config::APP_ID);

    // 每秒重绘：让倒计时 / “Ns ago” / 顶栏标签平滑走动（ksni 按哈希去重，未变化不发 D-Bus）。
    {
        let h = handle.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(1)).await;
                let _ = h.update(|_| {}).await;
            }
        });
    }

    // 自适应轮询（阻塞驱动；Refresh now 通过 refresh 唤醒）。
    poller::run(handle, client, refresh, sk, org).await;
    Ok(())
}
