//! KDE 凭证回退：非交互地从 KWallet 读浏览器的 "Safe Storage" 钥匙（对齐 Python _kwallet_password）。
//! 纯 zbus，无 GTK。三条铁律：
//!  ① 用 NameHasOwner 探测 daemon，绝不靠 D-Bus activation 把 kwalletd 拉起来（那会弹解锁框）；
//!  ② isOpen 为假（钱包没解锁）就放弃，绝不 open() 触发"创建/解锁密码"弹框；
//!  ③ 整体超时包住，任一调用挂死也不卡住凭证读取。
use std::time::Duration;

const APP: &str = "claude-usage-indicator";

/// kwalletd6 优先、再 kwalletd5；返回明文密码或 None。
pub async fn kwallet_password(folder: &str, entry: &str) -> Option<String> {
    let conn = zbus::Connection::session().await.ok()?;
    for (svc, path) in [
        ("org.kde.kwalletd6", "/modules/kwalletd6"),
        ("org.kde.kwalletd5", "/modules/kwalletd5"),
    ] {
        if let Some(pw) = try_daemon(&conn, svc, path, folder, entry).await {
            return Some(pw);
        }
    }
    None
}

async fn try_daemon(
    conn: &zbus::Connection,
    svc: &str,
    path: &str,
    folder: &str,
    entry: &str,
) -> Option<String> {
    tokio::time::timeout(Duration::from_secs(3), async {
        // ① daemon 没在跑就跳过（NameHasOwner 不会触发 activation）
        let owned: bool = call(conn, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", &(svc,)).await?;
        if !owned {
            return None;
        }
        let iface = "org.kde.KWallet";
        let enabled: bool = call(conn, svc, path, iface, "isEnabled", &()).await?;
        if !enabled {
            return None;
        }
        let wallet: String = call(conn, svc, path, iface, "networkWallet", &()).await?;
        // ② 未解锁就放弃，绝不 open() 弹框
        let is_open: bool = call(conn, svc, path, iface, "isOpen", &(wallet.clone(),)).await?;
        if !is_open {
            return None;
        }
        let handle: i32 = call(conn, svc, path, iface, "open", &(wallet, 0i64, APP)).await?;
        if handle < 0 {
            return None;
        }
        let has: bool = call(conn, svc, path, iface, "hasFolder", &(handle, folder, APP)).await.unwrap_or(false);
        let pw: Option<String> = if has {
            call(conn, svc, path, iface, "readPassword", &(handle, folder, entry, APP)).await
        } else {
            None
        };
        let _: Option<i32> = call(conn, svc, path, iface, "close", &(handle, false, APP)).await;
        pw.filter(|s| !s.is_empty())
    })
    .await
    .ok()
    .flatten()
}

/// 诊断用（--doctor）：报告 KWallet daemon 状态，不读任何密码。
/// 返回 (daemon 名, 是否启用, 网络钱包是否已解锁)；无可用 daemon 时返回 None。
pub async fn kwallet_status() -> Option<(String, bool, bool)> {
    let conn = zbus::Connection::session().await.ok()?;
    for (svc, path) in [
        ("org.kde.kwalletd6", "/modules/kwalletd6"),
        ("org.kde.kwalletd5", "/modules/kwalletd5"),
    ] {
        let owned: Option<bool> = call(&conn, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", &(svc,)).await;
        if owned != Some(true) {
            continue;
        }
        let iface = "org.kde.KWallet";
        let enabled: bool = call(&conn, svc, path, iface, "isEnabled", &()).await.unwrap_or(false);
        let open = if enabled {
            match call::<String, _>(&conn, svc, path, iface, "networkWallet", &()).await {
                Some(w) => call(&conn, svc, path, iface, "isOpen", &(w,)).await.unwrap_or(false),
                None => false,
            }
        } else {
            false
        };
        return Some((svc.to_string(), enabled, open));
    }
    None
}

/// 一次 D-Bus 方法调用，反序列化回 T；任何错误都吞成 None（凭证读取必须永不抛/永不阻塞）。
async fn call<T, B>(conn: &zbus::Connection, dest: &str, path: &str, iface: &str, method: &str, body: &B) -> Option<T>
where
    T: for<'d> serde::Deserialize<'d> + zbus::zvariant::Type,
    B: serde::Serialize + zbus::zvariant::DynamicType,
{
    let msg = conn.call_method(Some(dest), path, Some(iface), method, body).await.ok()?;
    msg.body().deserialize::<T>().ok()
}
