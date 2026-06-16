//! 常量 + dev/prod 身份。默认(prod)= 沿用原 Python 应用的身份（无缝迁移）；`--features dev` 用 -dev 身份
//! 供 dev 构建与 prod 并存测试。所有带 -dev/[dev] 的串都 cfg 到 dev，prod 二进制里没有。

#[cfg(not(feature = "dev"))]
pub const APP_ID: &str = "claude-usage-indicator";
#[cfg(feature = "dev")]
pub const APP_ID: &str = "claude-usage-indicator-dev";

#[cfg(not(feature = "dev"))]
pub const SERVICE: &str = "claude-usage-indicator.service"; // 自更新后重启的 systemd --user 单元
#[cfg(feature = "dev")]
pub const SERVICE: &str = "claude-usage-indicator-dev.service";

// 顶栏内联标签前缀：prod 无，dev 加 "[dev] " 以便肉眼区分。
#[cfg(not(feature = "dev"))]
pub const LABEL_PREFIX: &str = "";
#[cfg(feature = "dev")]
pub const LABEL_PREFIX: &str = "[dev] ";

// 版本号显示后缀（--version / About）：prod 无，dev "-dev"。
#[cfg(not(feature = "dev"))]
pub const BUILD_TAG: &str = "";
#[cfg(feature = "dev")]
pub const BUILD_TAG: &str = "-dev";

// 括号身份后缀（feedback 正文 / doctor 标题）：prod 无，dev " (dev)"。
#[cfg(not(feature = "dev"))]
pub const ID_SUFFIX: &str = "";
#[cfg(feature = "dev")]
pub const ID_SUFFIX: &str = " (dev)";

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
// 收到就立刻去 GitHub 复核版本（GitHub 仍是唯一真相源，ntfy 只当触发器）。
// prod 与原 Python 同主题；dev 用独立主题 → prod 发版不会惊动 dev，dev 发版也不碰 prod。
#[cfg(not(feature = "dev"))]
pub const NTFY_TOPIC: &str = "claude-usage-indicator-muyangli-byte-7c1e9a";
#[cfg(feature = "dev")]
pub const NTFY_TOPIC: &str = "claude-usage-indicator-dev-muyangli-byte-7c1e9a";

// ── 更新通道（两条独立 app 链的关键）─────────────────────────────────────────
// prod：检测读 main/VERSION（contents API → raw 兜底），下载 releases/latest 资产。
// dev ：检测读 `dev` 预发布上的 VERSION 资产，下载同一 `dev` 预发布的二进制。
// 稳定版客户端只读 main/VERSION + releases/latest，按定义跳过 prerelease → 普通用户永远看不到 dev 通道。
// （dev 预发布尚未建立时取不到版本 → 不自更新，靠本地 rebuild+redeploy，fail-closed 安全。）
#[cfg(not(feature = "dev"))]
pub const VERSION_URLS: &[&str] = &[
    "https://api.github.com/repos/muyangli-byte/claude-usage-indicator/contents/VERSION?ref=main",
    "https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/VERSION",
];
#[cfg(feature = "dev")]
pub const VERSION_URLS: &[&str] =
    &["https://github.com/muyangli-byte/claude-usage-indicator/releases/download/dev/VERSION"];

#[cfg(not(feature = "dev"))]
pub const DOWNLOAD_BASE: &str =
    "https://github.com/muyangli-byte/claude-usage-indicator/releases/latest/download";
#[cfg(feature = "dev")]
pub const DOWNLOAD_BASE: &str =
    "https://github.com/muyangli-byte/claude-usage-indicator/releases/download/dev";
