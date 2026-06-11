//! 凭证读取（对齐 Python cui/credentials.py）：config.json 覆盖 → 遍历浏览器 profile，
//! 从 Secret Service(GNOME) 或 KWallet(KDE) 取 "Safe Storage" 钥匙、自解密 cookie。
//! 绝不调 unlock()/open()（只用已解锁钱包），不弹解锁框；用 cui_core 的形状校验拒绝错钥匙解出的乱码。
use aes::cipher::{block_padding::Pkcs7, BlockModeDecrypt, KeyIvInit};
use anyhow::{anyhow, Result};
use cui_core::{valid_org, valid_sk};
use secret_service::{EncryptionType, SecretService};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

type Aes128CbcDec = cbc::Decryptor<aes::Aes128>;

// (Secret Service application 名, KWallet 产品名, cookie 库基目录)；
// 每个基目录下试 <profile>/Cookies 与 <profile>/Network/Cookies。KWallet 条目为 "<kw> Keys"/"<kw> Safe Storage"。
const BROWSERS: &[(&str, &str, &[&str])] = &[
    ("chrome", "Chrome", &["~/.config/google-chrome", "~/.var/app/com.google.Chrome/config/google-chrome"]),
    ("chromium", "Chromium", &["~/.config/chromium", "~/snap/chromium/common/chromium", "~/.var/app/org.chromium.Chromium/config/chromium"]),
    ("brave", "Brave", &["~/.config/BraveSoftware/Brave-Browser", "~/snap/brave/common/.config/BraveSoftware/Brave-Browser", "~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"]),
    ("microsoft-edge", "Microsoft Edge", &["~/.config/microsoft-edge", "~/.var/app/com.microsoft.Edge/config/microsoft-edge"]),
];

fn home() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default())
}

fn expand(p: &str) -> PathBuf {
    match p.strip_prefix("~/") {
        Some(rest) => home().join(rest),
        None => PathBuf::from(p),
    }
}

fn profile_cookie_files(bases: &[&str]) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for base in bases {
        for sub in ["*/Cookies", "*/Network/Cookies"] {
            if let Some(pat) = expand(base).join(sub).to_str() {
                if let Ok(paths) = glob::glob(pat) {
                    out.extend(paths.flatten());
                }
            }
        }
    }
    out.sort();
    out
}

/// 从 cookie 路径推出 profile 名（Default / Profile 3…），兼容 Network/ 子目录。对齐 Python _profile_label。
fn profile_label(p: &Path) -> String {
    let mut dir = p.parent();
    if dir.and_then(|d| d.file_name()).is_some_and(|n| n == "Network") {
        dir = dir.and_then(|d| d.parent());
    }
    dir.and_then(|d| d.file_name())
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// 只看某 profile 是否有 claude.ai sessionKey + 加密前缀（v10/v11），不解密、不打印密钥。对齐 Python _cookie_presence。
fn cookie_presence(path: &Path) -> Option<String> {
    let tmp = std::env::temp_dir().join(format!("cui-presence-{}.db", std::process::id()));
    if std::fs::copy(path, &tmp).is_err() {
        return None;
    }
    let mut out = None;
    if let Ok(conn) = rusqlite::Connection::open_with_flags(&tmp, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY) {
        if let Ok(ev) = conn.query_row(
            "SELECT encrypted_value FROM cookies WHERE name='sessionKey' AND host_key LIKE '%claude.ai'",
            [],
            |r| r.get::<_, Vec<u8>>(0),
        ) {
            if !ev.is_empty() {
                out = Some(String::from_utf8_lossy(&ev[..ev.len().min(3)]).into_owned());
            }
        }
    }
    let _ = std::fs::remove_file(&tmp);
    out
}

/// 扫描所有浏览器 profile，返回 (浏览器, profile 名, 加密前缀 or None)。供 --doctor 用。
pub fn scan_profiles() -> Vec<(&'static str, String, Option<String>)> {
    let mut out = Vec::new();
    for (app, _kw, bases) in BROWSERS {
        for f in profile_cookie_files(bases) {
            out.push((*app, profile_label(&f), cookie_presence(&f)));
        }
    }
    out
}

fn read_config() -> serde_json::Value {
    std::fs::read_to_string(home().join(".config/claude-usage-indicator/config.json"))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::Value::Null)
}

/// 用量阈值提醒配置：alert_enabled（默认关）+ alert_threshold（默认 80，clamp 到 1..100）。
pub fn read_alert_cfg() -> (bool, u8) {
    let v = read_config();
    let en = v.get("alert_enabled").and_then(|x| x.as_bool()).unwrap_or(false);
    let thr = v.get("alert_threshold").and_then(|x| x.as_u64()).unwrap_or(80).clamp(1, 100) as u8;
    (en, thr)
}

/// 把用量提醒配置写回 config.json（读+合并+写，保留其它键如 lang/session_key）。
pub fn write_alert_cfg(enabled: bool, threshold: u8) {
    let path = home().join(".config/claude-usage-indicator/config.json");
    let mut v = read_config();
    if !v.is_object() {
        v = serde_json::json!({});
    }
    v["alert_enabled"] = serde_json::Value::Bool(enabled);
    v["alert_threshold"] = serde_json::Value::from(threshold);
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&path, serde_json::to_string_pretty(&v).unwrap_or_default());
}

/// 读持久化的通知语言（config.json 的 lang），默认 en。对齐 Python load_lang 的配置部分。
pub fn load_lang() -> String {
    read_config()
        .get("lang")
        .and_then(|v| v.as_str())
        .filter(|s| *s == "zh" || *s == "en")
        .unwrap_or("en")
        .to_string()
}

fn derive_key(pw: &[u8]) -> [u8; 16] {
    let mut key = [0u8; 16];
    pbkdf2::pbkdf2_hmac::<sha1::Sha1>(pw, b"saltysalt", 1, &mut key);
    key
}

/// 按 Chromium Linux 方案解密一个 cookie 值（v11=keyring 钥匙, v10=peanuts）。错钥匙会得到乱码/报错。
fn decrypt_cookie(enc: &[u8], safe_pw: &[u8], db_version: i64, host_key: &str) -> Result<String> {
    if enc.len() < 3 {
        return Err(anyhow!("encrypted value too short"));
    }
    let (prefix, body) = enc.split_at(3);
    let key = match prefix {
        b"v11" => derive_key(safe_pw),
        b"v10" => derive_key(b"peanuts"),
        _ => return Ok(String::from_utf8_lossy(enc).into_owned()),
    };
    let iv = [0x20u8; 16];
    let mut buf = body.to_vec();
    let dec = Aes128CbcDec::new(&key.into(), &iv.into())
        .decrypt_padded::<Pkcs7>(&mut buf)
        .map_err(|_| anyhow!("AES-CBC decrypt failed (wrong key?)"))?
        .to_vec();
    let dec = if db_version >= 24 {
        // Chrome DB v24+ 在明文前加了 sha256(host_key)
        if dec.len() < 32 {
            return Err(anyhow!("plaintext shorter than domain hash"));
        }
        let mut h = Sha256::new();
        h.update(host_key.as_bytes());
        if dec[..32] != h.finalize()[..] {
            return Err(anyhow!("domain hash mismatch"));
        }
        dec[32..].to_vec()
    } else {
        dec
    };
    Ok(String::from_utf8(dec)?)
}

/// 复制 Cookies DB 到临时文件（浏览器可能持写锁）再只读读取并解密，返回 (sessionKey, lastActiveOrg)。
fn read_creds_from_db(path: &Path, pw: &[u8]) -> (Option<String>, Option<String>) {
    let tmp = std::env::temp_dir().join(format!("cui-cookies-{}.db", std::process::id()));
    if std::fs::copy(path, &tmp).is_err() {
        return (None, None);
    }
    let (mut sk, mut org) = (None, None);
    if let Ok(conn) =
        rusqlite::Connection::open_with_flags(&tmp, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
    {
        let db_version: i64 = conn
            .query_row("SELECT value FROM meta WHERE key='version'", [], |r| r.get::<_, String>(0))
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        for (name, is_sk) in [("sessionKey", true), ("lastActiveOrg", false)] {
            let row: rusqlite::Result<(String, Vec<u8>)> = conn.query_row(
                "SELECT host_key, encrypted_value FROM cookies WHERE name=?1 AND host_key LIKE '%claude.ai'",
                [name],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, Vec<u8>>(1)?)),
            );
            if let Ok((hk, ev)) = row {
                if let Ok(val) = decrypt_cookie(&ev, pw, db_version, &hk) {
                    if is_sk {
                        sk = Some(val);
                    } else {
                        org = Some(val);
                    }
                }
            }
        }
    }
    let _ = std::fs::remove_file(&tmp);
    (sk, org)
}

/// 从 Secret Service 取某浏览器的 "Safe Storage" 钥匙。只用已解锁项，绝不 unlock()（不弹框）。
async fn safe_storage_key(ss: &SecretService<'_>, app: &str) -> Option<Vec<u8>> {
    let mut attrs = HashMap::new();
    attrs.insert("application", app);
    let res = ss.search_items(attrs).await.ok()?;
    for item in res.unlocked {
        if let Ok(secret) = item.get_secret().await {
            if !secret.is_empty() {
                return Some(secret);
            }
        }
    }
    None
}

/// 返回 (session_key, org_id)。顺序：① config.json 显式配置 ② 遍历浏览器(Secret Service 钥匙 + 自解密)。
pub async fn load_credentials() -> Result<(String, String)> {
    let cfg = read_config();
    let mut sk = cfg.get("session_key").and_then(|v| v.as_str()).map(String::from).filter(|s| valid_sk(s));
    let mut org = cfg.get("org_id").and_then(|v| v.as_str()).map(String::from).filter(|s| valid_org(s));
    if let (Some(s), Some(o)) = (&sk, &org) {
        return Ok((s.clone(), o.clone()));
    }

    let ss = SecretService::connect(EncryptionType::Plain).await.ok();
    let mut cookie_seen = false;
    for (app, kw, bases) in BROWSERS {
        let files = profile_cookie_files(bases);
        if files.is_empty() {
            continue;
        }
        cookie_seen = true;
        // 钥匙来源：GNOME Secret Service 优先；拿不到（KDE / 无 GNOME 钥匙环）则回退直查 KWallet。
        let mut pw = match &ss {
            Some(s) => safe_storage_key(s, app).await.unwrap_or_default(),
            None => Vec::new(),
        };
        if pw.is_empty() {
            if let Some(k) =
                crate::kwallet::kwallet_password(&format!("{kw} Keys"), &format!("{kw} Safe Storage")).await
            {
                pw = k.into_bytes();
            }
        }
        for f in files {
            let (csk, corg) = read_creds_from_db(&f, &pw);
            if sk.is_none() {
                if let Some(s) = csk.filter(|s| valid_sk(s)) {
                    sk = Some(s);
                }
            }
            if org.is_none() {
                if let Some(o) = corg.filter(|o| valid_org(o)) {
                    org = Some(o);
                }
            }
            if let (Some(s), Some(o)) = (&sk, &org) {
                return Ok((s.clone(), o.clone()));
            }
        }
    }
    match sk {
        Some(s) => Ok((s, org.unwrap_or_default())),
        None if cookie_seen => {
            Err(anyhow!("found browser cookies but no valid sessionKey (keyring locked/absent?)"))
        }
        None => Err(anyhow!("no claude.ai cookie found (logged in? right browser?)")),
    }
}
