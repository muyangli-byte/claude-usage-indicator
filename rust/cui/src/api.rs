//! 网络层：wreq 伪装 Chrome 过 Cloudflare，拉 claude.ai 内部用量接口 → cui_core::Raw。
//! 对应 Python cui/api.py 的 fetch_usage（错误分类后续再细化）。
use anyhow::{anyhow, Result};
use cui_core::Raw;
use wreq::Client;
use wreq_util::Emulation;

/// 构建一个伪装 Chrome 的客户端（BoringSSL，过 Cloudflare）。
pub fn client() -> Result<Client> {
    Ok(Client::builder().emulation(Emulation::Chrome137).build()?)
}

/// 拉一次用量并解析。sessionKey 仅放进 cookie 头，绝不落日志。
pub async fn fetch_usage(client: &Client, sk: &str, org: &str) -> Result<Raw> {
    let url = format!("https://claude.ai/api/organizations/{org}/usage");
    let r = client
        .get(&url)
        .header("accept", "*/*")
        .header("anthropic-client-platform", "web_claude_ai")
        .header("referer", "https://claude.ai/new")
        .header("cookie", format!("sessionKey={sk}"))
        .send()
        .await?;
    let status = r.status().as_u16();
    let txt = r.text().await?;
    let j: serde_json::Value = serde_json::from_str(&txt)
        .map_err(|_| anyhow!("HTTP {status}: 非 JSON（Cloudflare 拦截或登录失效？）"))?;
    cui_core::validate_and_extract(&j).map_err(|e| anyhow!("schema: {}", e.0))
}
