//! 把仓库根的 VERSION 文件注入为编译期常量 CUI_VERSION，使 Rust 的版本号
//! 永远与 Python 读的同一个 VERSION 文件一致，杜绝漂移（迁移方案 §11 决策 6）。
use std::path::Path;

fn main() {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR");
    let version_file = Path::new(&manifest).join("../../VERSION");
    let version = std::fs::read_to_string(&version_file)
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| "0.0.0".to_string());
    println!("cargo:rustc-env=CUI_VERSION={version}");
    println!("cargo:rerun-if-changed={}", version_file.display());
}
