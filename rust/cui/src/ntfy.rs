//! 常驻订阅 ntfy 主题做「发布即时通知」（对齐 Python _ntfy_loop）：
//! 收到任意 message 事件 → 触发立即复核 GitHub 版本（GitHub 仍是唯一真相源，
//! 公开主题被人发垃圾也只是多查一次、不会误报）。断线指数退避重连 5→300s；
//! ntfy 不可达完全不影响每天一次的轮询兜底。
use crate::config::{NTFY_TOPIC, VERSION};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Notify;
use wreq::Client;

const READ_TIMEOUT_S: u64 = 120; // ntfy keepalive ~45s；超时即视为断线重连

pub async fn subscribe(client: Client, check_update: Arc<Notify>) {
    let url = format!("https://ntfy.sh/{NTFY_TOPIC}/json");
    let ua = format!("claude-usage-indicator/{VERSION}"); // 与 Python + 版本检查 UA 一致，不泄漏 build
    let mut backoff = 5u64;
    loop {
        match client.get(&url).header("User-Agent", ua.as_str()).send().await {
            Ok(mut resp) => {
                backoff = 5; // 连上即重置退避
                let mut buf: Vec<u8> = Vec::new();
                loop {
                    match tokio::time::timeout(Duration::from_secs(READ_TIMEOUT_S), resp.chunk()).await {
                        Ok(Ok(Some(chunk))) => {
                            buf.extend_from_slice(&chunk);
                            drain_lines(&mut buf, &check_update);
                        }
                        Ok(Ok(None)) => break,                  // 流正常结束
                        Ok(Err(e)) => { eprintln!("[ntfy] read error: {e}"); break; }
                        Err(_) => break,                        // 读超时 → 重连
                    }
                }
            }
            Err(e) => eprintln!("[ntfy] connect failed: {e}"),
        }
        eprintln!("[ntfy] disconnected, reconnecting in {backoff}s");
        tokio::time::sleep(Duration::from_secs(backoff)).await;
        backoff = (backoff * 2).min(300);
    }
}

/// 从缓冲区切出完整行（每条消息一行 JSON，外加周期性 keepalive 行），逐行解析。
fn drain_lines(buf: &mut Vec<u8>, check_update: &Arc<Notify>) {
    while let Some(pos) = buf.iter().position(|&b| b == b'\n') {
        let line: Vec<u8> = buf.drain(..=pos).collect();
        let line = String::from_utf8_lossy(&line);
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(ev) = serde_json::from_str::<serde_json::Value>(line) {
            if ev.get("event").and_then(|v| v.as_str()) == Some("message") {
                println!("[ntfy] release signal → re-checking GitHub version");
                check_update.notify_one();
            }
        }
    }
}
