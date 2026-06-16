//! 卸载（对齐 Python cui.cli.cmd_uninstall）：在分离的 systemd 瞬时单元里跑 Python 树的
//! uninstall.sh --purge —— 它清 Python + Rust 全部产物（服务/命令/安装目录/配置/哨兵/兄弟bin/看门狗，
//! 见仓库 uninstall.sh）。uninstall.sh 缺失时退化为内联最小拆除，至少保证把 Rust 停掉、删掉哨兵。
//! 在独立单元里跑，这样它停掉本服务时不会把卸载进程一起杀掉。
use std::path::PathBuf;
use std::process::Command;

fn home() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default())
}

#[cfg(not(feature = "dev"))]
pub fn spawn_detached_uninstall() {
    let script = home().join(".local/share/claude-usage-indicator/uninstall.sh");
    if script.exists() {
        let s = script.to_string_lossy().into_owned();
        if Command::new("systemd-run")
            .args(["--user", "--collect", "bash", &s, "--purge"])
            .spawn()
            .is_err()
        {
            let _ = Command::new("bash").arg(&s).arg("--purge").spawn();
        }
    } else {
        inline_teardown();
    }
}

/// uninstall.sh 不在时的兜底：停服务 + 删哨兵/兄弟bin（run.sh 会回落 Python，或彻底停掉）。
#[cfg(not(feature = "dev"))]
fn inline_teardown() {
    let sc = |args: &[&str]| {
        let _ = Command::new("systemctl").args(args).status();
    };
    sc(&["--user", "disable", "--now", "claude-usage-indicator.service"]);
    sc(&["--user", "disable", "--now", "claude-usage-indicator-watchdog.timer"]);
    let _ = std::fs::remove_file(home().join(".config/claude-usage-indicator/use-rust"));
    let _ = std::fs::remove_dir_all(home().join(".local/share/claude-usage-indicator-bin"));
}

/// dev 链是纯 Rust（无 Python / run.sh / 看门狗）：在分离单元里只拆掉 dev 自己的产物
/// （服务 + 兄弟 bin + dev 配置/缓存目录 + unit 文件），绝不碰 prod。路径全部按 APP_ID/SERVICE 派生。
#[cfg(feature = "dev")]
pub fn spawn_detached_uninstall() {
    use crate::config::{APP_ID, SERVICE};
    let h = home();
    let h = h.to_string_lossy();
    let script = format!(
        "systemctl --user disable --now {SERVICE}; \
         rm -rf '{h}/.local/share/{APP_ID}-bin' '{h}/.config/{APP_ID}' '{h}/.cache/{APP_ID}' \
                '{h}/.config/systemd/user/{SERVICE}'; \
         systemctl --user daemon-reload"
    );
    if Command::new("systemd-run")
        .args(["--user", "--collect", "bash", "-c", &script])
        .spawn()
        .is_err()
    {
        let _ = Command::new("bash").args(["-c", &script]).spawn();
    }
}
