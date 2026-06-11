//! 弹窗 UI（fltk,无 GTK）。fltk 要在自己的线程跑事件循环(主线程是 tokio+ksni),
//! 所以起一个专用线程拥有 fltk app,主程序经 channel 发命令(用量阈值提醒 / 更新内容窗口)。
use std::sync::mpsc::{self, Sender};

use fltk::enums::{Color, Event, Font, FrameType, Key};
use fltk::frame::Frame;
use fltk::prelude::*;
use fltk::window::Window;

pub enum UiCmd {
    /// 用量穿过阈值：醒目小闪窗（红黑交替）+ 大字「Current session 已使用 N%」。
    UsageAlert { pct: u8, lang: String },
}

pub fn spawn() -> Sender<UiCmd> {
    let (tx, rx) = mpsc::channel::<UiCmd>();
    std::thread::Builder::new()
        .name("cui-ui".into())
        .spawn(move || {
            let _app = fltk::app::App::default();
            let mut alert: Option<Window> = None; // 当前告警窗(避免叠弹)
            loop {
                // 处理 ~0.1s 内的 fltk 事件/定时器,再排空命令队列
                let _ = fltk::app::wait_for(0.1);
                while let Ok(cmd) = rx.try_recv() {
                    match cmd {
                        UiCmd::UsageAlert { pct, lang } => {
                            if alert.as_ref().map_or(true, |w| !w.shown()) {
                                alert = Some(usage_alert(pct, &lang));
                            }
                        }
                    }
                }
            }
        })
        .expect("spawn cui-ui thread");
    tx
}

/// 居中无边框小窗,红黑闪烁,大白字。点任意处 / Esc / 120s 关。
fn usage_alert(pct: u8, lang: &str) -> Window {
    let (sw, sh) = fltk::app::screen_size();
    let (w, h) = (560, 240);
    let x = ((sw - w as f64) / 2.0) as i32;
    let y = ((sh - h as f64) / 2.0) as i32;
    let mut win = Window::new(x, y, w, h, None);
    win.set_border(false);
    win.set_color(Color::from_rgb(0xe0, 0x31, 0x31));

    let big = if lang == "zh" {
        format!("Current session 已使用 {pct}%")
    } else {
        format!("Current session at {pct}%")
    };
    let mut title = Frame::new(0, 70, w, 70, None);
    title.set_label(&big);
    title.set_label_size(34);
    title.set_label_font(Font::HelveticaBold);
    title.set_label_color(Color::White);
    title.set_frame(FrameType::NoBox);

    let mut hint = Frame::new(0, 152, w, 28, None);
    hint.set_label(if lang == "zh" {
        "点任意处 / 按 Esc 关闭"
    } else {
        "Click anywhere / Esc to dismiss"
    });
    hint.set_label_size(12);
    hint.set_label_color(Color::from_rgb(0xe6, 0xe6, 0xe6));
    hint.set_frame(FrameType::NoBox);

    win.end();
    win.show();

    // 闪烁:每 0.55s 红黑切换;窗口关了就停。
    let mut wf = win.clone();
    let on = std::rc::Rc::new(std::cell::Cell::new(true));
    fltk::app::add_timeout3(0.55, move |handle| {
        if !wf.shown() {
            return;
        }
        wf.set_color(if on.get() {
            Color::from_rgb(0x14, 0x14, 0x14)
        } else {
            Color::from_rgb(0xe0, 0x31, 0x31)
        });
        wf.redraw();
        on.set(!on.get());
        fltk::app::repeat_timeout3(0.55, handle);
    });

    // 关闭:点任意处 / Esc
    win.handle(|w, ev| match ev {
        Event::Push => {
            w.hide();
            true
        }
        Event::KeyDown if fltk::app::event_key() == Key::Escape => {
            w.hide();
            true
        }
        _ => false,
    });
    // 兜底自动关
    let mut wa = win.clone();
    fltk::app::add_timeout3(120.0, move |_| wa.hide());

    win
}
