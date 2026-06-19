//! 网络层：wreq 伪装 Chrome 过 Cloudflare，拉 claude.ai 内部用量接口 → cui_core::Raw。
//! 对应 Python cui/api.py 的 fetch_usage（错误分类后续再细化）。
use anyhow::{anyhow, Result};
use cui_core::Raw;
use wreq::Client;
use wreq_util::Emulation;

/// 构建一个伪装 Chrome 的客户端（BoringSSL，过 Cloudflare）。
/// 跟随重定向(默认不跟随):GitHub 的 releases/latest/download/ 会 302 跳到 CDN,
/// 自更新下载二进制/校验和必须跟随,否则拿到 302 就当「无资产」放弃(更新点了没反应的根因)。
pub fn client() -> Result<Client> {
    Ok(Client::builder()
        .emulation(Emulation::Chrome137)
        .redirect(wreq::redirect::Policy::limited(10))
        .build()?)
}

/// 查通道对应的最新版本（按 config::VERSION_URLS 顺序逐个尝试，取第一个成功的）。
/// prod = contents API(raw media，~60s 缓存) → raw CDN 兜底；dev = `dev` 预发布的 VERSION 资产。
/// 对应 Python fetch_remote_version。
pub async fn fetch_remote_version(client: &Client) -> Option<String> {
    let ua = format!("claude-usage-indicator/{}", crate::config::VERSION);
    for url in crate::config::VERSION_URLS {
        let mut req = client.get(*url).header("user-agent", &ua);
        // GitHub contents API 需 raw media type 才直接回文本而非 JSON；其它 URL 本就是纯文本。
        if url.contains("api.github.com") {
            req = req.header("accept", "application/vnd.github.raw+json");
        }
        if let Ok(r) = req.send().await {
            if r.status().as_u16() == 200 {
                if let Ok(t) = r.text().await {
                    let t = t.trim().to_string();
                    if !t.is_empty() && !t.starts_with('{') {
                        return Some(t);
                    }
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

/// 一个组织(公司/个人账号)。多账号枚举与切换用。
#[derive(Clone, Debug)]
pub struct Org {
    pub uuid: String,
    pub name: String,
}

/// 列出该 sessionKey 可访问的所有组织。对应 claude.ai web 的 `GET /api/organizations`（返回数组）。
/// sessionKey 仅放 cookie，绝不落日志。失败(网络/挑战/非200)返回 Err，调用方应离线兜底。
pub async fn fetch_organizations(client: &Client, sk: &str) -> Result<Vec<Org>> {
    let r = client
        .get("https://claude.ai/api/organizations")
        .header("accept", "*/*")
        .header("anthropic-client-platform", "web_claude_ai")
        .header("referer", "https://claude.ai/new")
        .header("cookie", format!("sessionKey={sk}"))
        .send()
        .await?;
    let status = r.status().as_u16();
    if status != 200 {
        return Err(anyhow!("http: HTTP {status}"));
    }
    let txt = r.text().await?;
    let j: serde_json::Value =
        serde_json::from_str(&txt).map_err(|_| anyhow!("schema: organizations not JSON"))?;
    let arr = j.as_array().ok_or_else(|| anyhow!("schema: organizations not an array"))?;
    let mut out = Vec::new();
    for o in arr {
        if let Some(uuid) = o.get("uuid").and_then(|v| v.as_str()) {
            // 只保留有 claude.ai 「chat」能力的 org —— 那才是有 5h/7天用量的真实账号。
            // Team/Enterprise 会给成员自动建一个「Individual Org」，但它是纯 API/Console org
            // (capabilities=["api","api_individual"]、无 "chat")，没有 claude.ai 用量 → 不进切换列表。
            let has_chat = o
                .get("capabilities")
                .and_then(|v| v.as_array())
                .map_or(false, |a| a.iter().any(|c| c.as_str() == Some("chat")));
            if !has_chat {
                continue;
            }
            let name = o.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string();
            out.push(Org { uuid: uuid.to_string(), name });
        }
    }
    Ok(out)
}
