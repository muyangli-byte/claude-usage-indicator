//! 迁移探测 main：① 验证 wreq 能否过 Cloudflare；② 带真实 cookie 拉用量、与 Python 对数。
//! sessionKey 只从环境变量 CUI_SK 读取，绝不打印（org_id 不敏感，doctor 本就显示）。
use std::env;
use wreq::Client;
use wreq_util::Emulation;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = Client::builder().emulation(Emulation::Chrome137).build()?;

    // ① 无凭证打 claude.ai：wreq 若过了 Cloudflare，会进到真 API（返回 JSON），而非 "Just a moment" 挑战页
    let r = client
        .get("https://claude.ai/api/organizations")
        .header("accept", "*/*")
        .send()
        .await?;
    let st = r.status().as_u16();
    let body = r.text().await?;
    let challenged = body.contains("Just a moment") || body.contains("challenge-platform");
    let snippet: String = body.chars().take(90).collect();
    println!("[① Cloudflare 探测] HTTP {st} | 挑战页?={challenged} | body[:90]={snippet}");

    // ② 带真实 cookie 拉用量，和 Python --once 对数
    match (env::var("CUI_SK"), env::var("CUI_ORG")) {
        (Ok(sk), Ok(org)) if !sk.is_empty() && !org.is_empty() => {
            let url = format!("https://claude.ai/api/organizations/{org}/usage");
            let r2 = client
                .get(&url)
                .header("accept", "*/*")
                .header("anthropic-client-platform", "web_claude_ai")
                .header("referer", "https://claude.ai/new")
                .header("cookie", format!("sessionKey={sk}"))
                .send()
                .await?;
            let st2 = r2.status().as_u16();
            let txt = r2.text().await?;
            match serde_json::from_str::<serde_json::Value>(&txt) {
                Ok(j) => match cui_core::validate_and_extract(&j) {
                    Ok(raw) => println!(
                        "[② 用量 via wreq] HTTP {st2} | current session {} (reset {}) | all models {} (reset {})",
                        cui_core::pct(raw.five_hour_util),
                        cui_core::fmt_countdown(raw.five_hour_reset),
                        cui_core::pct(raw.seven_day_util),
                        cui_core::fmt_resetday(raw.seven_day_reset),
                    ),
                    Err(e) => println!("[② 用量] HTTP {st2} | schema 错误: {e:?}"),
                },
                Err(_) => println!(
                    "[② 用量] HTTP {st2} | 非 JSON | 挑战页?={}",
                    txt.contains("Just a moment")
                ),
            }
        }
        _ => println!("[② 用量] 未设 CUI_SK/CUI_ORG，跳过对数"),
    }
    Ok(())
}
