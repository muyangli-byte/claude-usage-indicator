//! 常量 + dev/prod 标识（迁移期：固定 -rust-dev，托盘可与 Python 正式版并存）。
pub const APP_ID: &str = "claude-usage-indicator-rust-dev";
pub const VERSION: &str = "2.11.0"; // 与 Python 当前版本对齐（迁移期硬编码）

pub const REPO_URL: &str = "https://github.com/muyangli-byte/claude-usage-indicator";
pub const SERVICE: &str = "claude-usage-indicator-rust-dev.service"; // 自更新后重启的 systemd --user 单元
pub const USAGE_PAGE_URL: &str = "https://claude.ai/new#settings/usage";

// 自适应轮询参数（对齐 Python）
pub const POLL_FAST_S: u64 = 5; // 数据在变时
pub const POLL_SLOW_S: u64 = 90; // 长时间无变化退避封顶
pub const POLL_ERROR_S: u64 = 60; // 出错重试
pub const RENOTIFY_BAD_S: f64 = 1800.0; // 持续异常每 30 分钟再提醒
pub const UPDATE_CHECK_S: u64 = 86400; // 版本检查间隔（兜底）

// 发布即时通知：发版时 GitHub Action 往这个公开 ntfy 主题发一条信号，客户端常驻订阅、
// 收到就立刻去 GitHub 复核版本（GitHub 仍是唯一真相源，ntfy 只当触发器）。与 Python 同一主题。
pub const NTFY_TOPIC: &str = "claude-usage-indicator-muyangli-byte-7c1e9a";
