//! 常量 + dev/prod 身份。默认(prod)= 与 Python 逐字一致；`--features dev` 保留 -rust-dev 身份
//! 供本机与 Python 正式版并存测试。所有带 rust-dev/[rust] 的串都 cfg 到 dev，prod 二进制里没有。

#[cfg(not(feature = "dev"))]
pub const APP_ID: &str = "claude-usage-indicator";
#[cfg(feature = "dev")]
pub const APP_ID: &str = "claude-usage-indicator-rust-dev";

#[cfg(not(feature = "dev"))]
pub const SERVICE: &str = "claude-usage-indicator.service"; // 自更新后重启的 systemd --user 单元
#[cfg(feature = "dev")]
pub const SERVICE: &str = "claude-usage-indicator-rust-dev.service";

// 顶栏内联标签前缀：prod 无，dev 加 "[rust] " 以便肉眼区分。
#[cfg(not(feature = "dev"))]
pub const LABEL_PREFIX: &str = "";
#[cfg(feature = "dev")]
pub const LABEL_PREFIX: &str = "[rust] ";

// 版本号显示后缀（--version / About）：prod 无，dev "-rust-dev"。
#[cfg(not(feature = "dev"))]
pub const BUILD_TAG: &str = "";
#[cfg(feature = "dev")]
pub const BUILD_TAG: &str = "-rust-dev";

// 括号身份后缀（feedback 正文 / doctor 标题）：prod 无，dev " (rust-dev)"。
#[cfg(not(feature = "dev"))]
pub const ID_SUFFIX: &str = "";
#[cfg(feature = "dev")]
pub const ID_SUFFIX: &str = " (rust-dev)";

// 版本号由 build.rs 从仓库根 VERSION 文件注入，与 Python 同源、永不漂移。
pub const VERSION: &str = env!("CUI_VERSION");

pub const REPO_URL: &str = "https://github.com/muyangli-byte/claude-usage-indicator";
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
