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

/// 查 GitHub 上的最新版本（contents API raw media type，~60s 缓存；失败回退 raw CDN）。对应 Python fetch_remote_version。
pub async fn fetch_remote_version(client: &Client) -> Option<String> {
    let ua = format!("claude-usage-indicator/{}", crate::config::VERSION);
    let api = "https://api.github.com/repos/muyangli-byte/claude-usage-indicator/contents/VERSION?ref=main";
    if let Ok(r) = client.get(api).header("user-agent", &ua).header("accept", "application/vnd.github.raw+json").send().await {
        if r.status().as_u16() == 200 {
            if let Ok(t) = r.text().await {
                let t = t.trim().to_string();
                if !t.is_empty() && !t.starts_with('{') {
                    return Some(t);
                }
            }
        }
    }
    let raw = "https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/VERSION";
    if let Ok(r) = client.get(raw).header("user-agent", &ua).send().await {
        if r.status().as_u16() == 200 {
            if let Ok(t) = r.text().await {
                let t = t.trim().to_string();
                if !t.is_empty() {
                    return Some(t);
                }
            }
        }
    }
    None
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
    // 错误消息带前缀（auth/cloudflare/schema/http），poller 据此分类（对齐 Python 的错误分类）。
    let is_challenge = txt.contains("Just a moment") || txt.contains("challenge-platform") || txt.contains("cf-chl");
    if status == 401 || status == 403 {
        return Err(if is_challenge { anyhow!("cloudflare: HTTP {status} challenge") } else { anyhow!("auth: HTTP {status}") });
    }
    if status != 200 {
        return Err(if is_challenge { anyhow!("cloudflare: HTTP {status} challenge") } else { anyhow!("http: HTTP {status}") });
    }
    let j: serde_json::Value = serde_json::from_str(&txt)
        .map_err(|_| if is_challenge { anyhow!("cloudflare: HTTP 200 challenge") } else { anyhow!("schema: response is not JSON") })?;
    cui_core::validate_and_extract(&j).map_err(|e| anyhow!("schema: {}", e.0))
}
