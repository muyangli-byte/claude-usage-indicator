//! 自更新（对齐 Python --self-update 的意图，但改为「下载预编译二进制」）：
//! 查 GitHub 最新版 → 下载本架构的 release 二进制 → 原子替换当前 exe → 重启 systemd 服务。
//! 需要发版 CI 产出 release 资产 cui-<arch>-linux（见 .github/workflows/rust-release.yml）。
use crate::config::{APP_ID, DOWNLOAD_BASE, SERVICE, VERSION};
use crate::api;
use cui_core::remote_is_newer;
use sha2::{Digest, Sha256};
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

/// 校验下载的二进制 SHA256 是否匹配 release 附带的 .sha256（完整性，迁移决策 §11.3）。
/// fail-closed：取不到校验和 / 不匹配都返回 false（绝不安装未经校验的二进制）。
async fn sha256_ok(client: &wreq::Client, bin_url: &str, bytes: &[u8]) -> bool {
    let sha_url = format!("{bin_url}.sha256");
    let txt = match client.get(&sha_url).send().await {
        Ok(r) if r.status().as_u16() == 200 => r.text().await.unwrap_or_default(),
        _ => {
            eprintln!("self-update: checksum unavailable, refusing to install");
            return false;
        }
    };
    let expected = txt.split_whitespace().next().unwrap_or("").to_lowercase();
    let mut h = Sha256::new();
    h.update(bytes);
    let actual = h.finalize().iter().map(|b| format!("{b:02x}")).collect::<String>();
    if expected.is_empty() || actual != expected {
        eprintln!("self-update: sha256 mismatch (want {expected}, got {actual}), refusing");
        return false;
    }
    println!("self-update: sha256 verified");
    true
}

/// 面包屑：自更新进程写下新版本号；重启后的新进程开机读到 → 弹「已更新」→ 删除。
fn breadcrumb_path() -> PathBuf {
    let base = std::env::var("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".cache"));
    base.join(APP_ID).join("updated")
}

/// 触发自更新：在独立 systemd 瞬时单元里跑 `cui --self-update`，这样它重启本服务时不会把更新进程一起杀掉。
/// 供菜单 "Update now" 和通知上的 "Update now" 按钮共用。
pub fn spawn_detached() {
    if let Ok(exe) = std::env::current_exe() {
        let exe = exe.to_string_lossy().into_owned();
        if std::process::Command::new("systemd-run")
            .args(["--user", "--collect", &exe, "--self-update"])
            .spawn()
            .is_err()
        {
            let _ = std::process::Command::new(&exe).arg("--self-update").spawn();
        }
    }
}

/// 新进程开机调用：若上次刚自更新过，返回新版本号并清掉面包屑。
pub fn consume_breadcrumb() -> Option<String> {
    let p = breadcrumb_path();
    let ver = std::fs::read_to_string(&p).ok()?.trim().to_string();
    let _ = std::fs::remove_file(&p);
    if ver.is_empty() {
        None
    } else {
        Some(ver)
    }
}

pub async fn cmd_self_update() -> i32 {
    let client = match api::client() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("self-update: {e}");
            return 1;
        }
    };
    let remote = match api::fetch_remote_version(&client).await {
        Some(r) => r,
        None => {
            eprintln!("self-update: could not fetch remote version");
            return 1;
        }
    };
    if !remote_is_newer(&remote, VERSION) {
        println!("self-update: already up to date (v{VERSION})");
        return 0;
    }

    let arch = std::env::consts::ARCH; // x86_64 / aarch64
    let url = format!("{DOWNLOAD_BASE}/cui-{arch}-linux"); // prod=releases/latest；dev=`dev` 预发布
    println!("self-update: v{VERSION} → v{remote}; downloading {url}");
    let resp = match client.get(&url).send().await {
        Ok(r) => r,
        Err(e) => {
            eprintln!("self-update: download failed: {e}");
            return 1;
        }
    };
    if resp.status().as_u16() != 200 {
        eprintln!("self-update: no release asset for {arch} (HTTP {})", resp.status().as_u16());
        return 1;
    }
    let bytes = match resp.bytes().await {
        Ok(b) => b,
        Err(e) => {
            eprintln!("self-update: read failed: {e}");
            return 1;
        }
    };
    if bytes.len() < 1_000_000 {
        eprintln!("self-update: asset too small ({} bytes), aborting", bytes.len());
        return 1;
    }
    if !sha256_ok(&client, &url, &bytes).await {
        return 1;
    }

    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("self-update: {e}");
            return 1;
        }
    };
    // 写到同目录的 .new，chmod，再 rename 覆盖（同盘原子；可覆盖正在运行的二进制）。
    let tmp = exe.with_extension("new");
    if let Err(e) = std::fs::write(&tmp, &bytes) {
        eprintln!("self-update: write failed: {e}");
        return 1;
    }
    if let Err(e) = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o755)) {
        eprintln!("self-update: chmod failed: {e}");
        return 1;
    }
    if let Err(e) = std::fs::rename(&tmp, &exe) {
        eprintln!("self-update: replace failed: {e}");
        let _ = std::fs::remove_file(&tmp);
        return 1;
    }
    // 写面包屑：重启后的新进程读到它 → 弹「已更新到 vX」。
    let crumb = breadcrumb_path();
    if let Some(dir) = crumb.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&crumb, &remote);

    println!("self-update: installed v{remote}, restarting {SERVICE}…");
    let _ = std::process::Command::new("systemctl")
        .args(["--user", "restart", SERVICE])
        .status();
    0
}
