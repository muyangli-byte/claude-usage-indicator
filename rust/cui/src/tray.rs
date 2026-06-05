//! ksni 托盘（纯 StatusNotifierItem over D-Bus，无 GTK）。对应 Python cui/tray.py 的展示部分。
//! 注意：SNI 没有 AppIndicator 的内联文字标签——用量文字放进 tooltip + 菜单；
//! 顶栏内联数字需后续把文字渲染进图标(icon_pixmap)，这里先用图标表健康、tooltip/菜单显示数字。
use crate::config::APP_ID;
use ksni::menu::StandardItem;
use ksni::{MenuItem, ToolTip, Tray};

#[derive(Default)]
pub struct CuiTray {
    pub summary: String,      // "Cur 39% 2h56m | All 5% Mon 7am"（进 tooltip 标题）
    pub rows: Vec<String>,    // 菜单里的分项行（名称 | reset、进度条+%）
    pub status_line: String,  // "Status: ok" / "Status: <err>"
    pub healthy: bool,
}

impl Tray for CuiTray {
    fn id(&self) -> String {
        APP_ID.into()
    }
    fn title(&self) -> String {
        "Claude Usage".into()
    }
    // 顶栏内联文字标签（XAyatanaLabel）——这才是"常态显示数字"那行，等价 Python AppIndicator.set_label
    fn label(&self) -> String {
        self.summary.clone()
    }
    // 健康用图标表达：正常=收发图标，异常=警告图标（SNI 无内联文字，先靠图标 + tooltip）
    fn icon_name(&self) -> String {
        if self.healthy {
            "network-transmit-receive".into()
        } else {
            "dialog-warning".into()
        }
    }
    fn tool_tip(&self) -> ToolTip {
        ToolTip {
            title: self.summary.clone(),
            description: self.status_line.clone(),
            ..Default::default()
        }
    }
    fn menu(&self) -> Vec<MenuItem<Self>> {
        let dim = |s: &str| -> MenuItem<Self> {
            StandardItem {
                label: s.to_string(),
                enabled: false,
                ..Default::default()
            }
            .into()
        };
        let mut items: Vec<MenuItem<Self>> = self.rows.iter().map(|s| dim(s)).collect();
        items.push(dim(&self.status_line));
        items.push(MenuItem::Separator);
        items.push(
            StandardItem {
                label: "Quit (rust-dev)".into(),
                activate: Box::new(|_: &mut Self| std::process::exit(0)),
                ..Default::default()
            }
            .into(),
        );
        items
    }
}
