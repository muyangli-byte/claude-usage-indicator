//! 命令行子命令（对齐 Python cli.py）：--once / --check / --doctor。
use crate::config::VERSION;
use crate::{api, creds};
use cui_core::{fmt_countdown, fmt_resetday, pct, remote_is_newer, valid_org, valid_sk};

/// 拉取一次并打印（调试），对齐 Python cmd_once 的输出格式。
pub async fn cmd_once() -> i32 {
    let (sk, org) = match creds::load_credentials().await {
        Ok(v) => v,
        Err(e) => {
            println!("cookie error: {e}");
            return 2;
        }
    };
    if sk.is_empty() {
        println!("auth: no sessionKey (log into claude.ai)");
        return 2;
    }
    if org.is_empty() {
        println!("error: no org id (set org_id in config.json)");
        return 2;
    }
    let client = match api::client() {
        Ok(c) => c,
        Err(e) => {
            println!("{e}");
            return 1;
        }
    };
    match api::fetch_usage(&client, &sk, &org).await {
        Ok(r) => {
            println!("  current session : {}  (reset {})", pct(r.five_hour_util), fmt_countdown(r.five_hour_reset));
            println!("  all models (wk) : {}  (reset {})", pct(r.seven_day_util), fmt_resetday(r.seven_day_reset));
            println!("  sonnet (wk)     : {}", pct(r.sonnet_util));
            println!("  opus (wk)       : {}", pct(r.opus_util));
            0
        }
        Err(e) => {
            println!("{e}");
            1
        }
    }
}

/// 检查是否有新版本。
pub async fn cmd_check() -> i32 {
    let client = match api::client() {
        Ok(c) => c,
        Err(_) => return 1,
    };
    match api::fetch_remote_version(&client).await {
        Some(r) if remote_is_newer(&r, VERSION) => println!("update available: v{VERSION} → v{r}"),
        Some(r) => println!("up to date (v{VERSION}; remote v{r})"),
        None => println!("could not fetch remote version (local v{VERSION})"),
    }
    0
}

/// 登录态自检（双语，不泄露密钥），对齐 Python cmd_doctor。
pub async fn cmd_doctor(lang: &str) -> i32 {
    let zh = lang == "zh";
    let line = |z: &str, e: &str| println!("{}", if zh { z } else { e });
    let rule = "=".repeat(52);

    println!("{rule}");
    line(" Claude 用量指示器 —— 登录态自检 (rust)", " Claude Usage Indicator — login self-check (rust)");
    println!("{rule}");
    let user = std::env::var("USER").unwrap_or_else(|_| "?".into());
    let desktop = std::env::var("XDG_CURRENT_DESKTOP").unwrap_or_else(|_| "?".into());
    line(&format!("系统用户：{user}"), &format!("System user: {user}"));
    line(&format!("桌面环境：{desktop}"), &format!("Desktop:     {desktop}"));
    println!();
    line("扫描浏览器 profile（找 claude.ai 登录 cookie）：", "Scanning browser profiles for a claude.ai login cookie:");
    let mut any = false;
    for (browser, label, prefix) in creds::scan_profiles() {
        match prefix {
            Some(p) => {
                any = true;
                line(&format!("  ✓ [{browser}] {label} —— 有登录 cookie（加密 {p}）"), &format!("  ✓ [{browser}] {label} — has login cookie (enc {p})"));
            }
            None => line(&format!("  · [{browser}] {label} —— 无"), &format!("  · [{browser}] {label} — none")),
        }
    }
    if !any {
        line("  （没找到任何 claude.ai 登录 cookie）", "  (no claude.ai login cookie found anywhere)");
    }
    println!();

    // 钥匙环：GNOME 用 Secret Service（隐式）；KDE 报告 KWallet daemon/解锁状态，定位"钱包锁着"这一最常见失败。
    match crate::kwallet::kwallet_status().await {
        Some((d, enabled, open)) => {
            let yn = |b: bool| if b { "✓" } else { "✗" };
            line(
                &format!("KWallet：{d}（启用 {} / 已解锁 {}）", yn(enabled), yn(open)),
                &format!("KWallet: {d} (enabled {} / unlocked {})", yn(enabled), yn(open)),
            );
            if enabled && !open {
                line(
                    "  · 钱包锁着 → 读不到钥匙（本程序绝不弹解锁框）。解锁 KWallet 后重试。",
                    "  · Wallet is locked → can't read the key (we never pop the unlock dialog). Unlock KWallet and retry.",
                );
            }
        }
        None => line(
            "KWallet：未运行（走 GNOME 钥匙环或 config.json）",
            "KWallet: not running (using GNOME keyring or config.json)",
        ),
    }
    println!();

    let (sk, org) = creds::load_credentials().await.unwrap_or((String::new(), String::new()));
    let (sk_ok, org_ok) = (valid_sk(&sk), valid_org(&org));
    line(
        &format!("sessionKey：{}", if sk_ok { "✓ 已获取并通过格式校验" } else { "✗ 未获取到有效值" }),
        &format!("sessionKey: {}", if sk_ok { "✓ obtained and validated" } else { "✗ not obtained" }),
    );
    line(
        &format!("org_id    ：{}", if org_ok { format!("✓ {org}") } else { "✗ 未获取".into() }),
        &format!("org_id    : {}", if org_ok { format!("✓ {org}") } else { "✗ not obtained".into() }),
    );
    if !(sk_ok && org_ok) {
        println!();
        line(
            "→ 登录态还没就绪：在 Chrome/Chromium/Brave/Edge 登录 claude.ai 并解锁钥匙环；或在 config.json 填 session_key+org_id。",
            "→ Login not ready: log into claude.ai in Chrome/Chromium/Brave/Edge and unlock your keyring; or set session_key+org_id in config.json.",
        );
        return 1;
    }
    println!();
    line("用拿到的凭证试拉一次用量……", "Trying a live usage fetch…");
    let client = match api::client() {
        Ok(c) => c,
        Err(e) => {
            println!("{e}");
            return 1;
        }
    };
    match api::fetch_usage(&client, &sk, &org).await {
        Ok(r) => {
            line(
                &format!("  ✓ 成功！Current session {}，All models {}", pct(r.five_hour_util), pct(r.seven_day_util)),
                &format!("  ✓ Success! Current session {}, All models {}", pct(r.five_hour_util), pct(r.seven_day_util)),
            );
            println!();
            line("✓ 一切就绪。", "✓ All set.");
            0
        }
        Err(e) => {
            line(&format!("  ✗ 拉取失败：{e}"), &format!("  ✗ fetch failed: {e}"));
            1
        }
    }
}
