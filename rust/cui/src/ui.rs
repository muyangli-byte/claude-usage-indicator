//! 弹窗 UI（fltk,无 GTK）。fltk 在自己的线程跑事件循环(主线程是 tokio+ksni),主程序经 channel 发命令:
//!  - UsageAlert：用量穿过阈值的醒目闪窗;
//!  - AlertSettings：用量提醒设置窗(开关 + 阈值数字);
//!  - MorePanel：把原 More 子菜单的所有动作做成一个弹窗里的按钮列表。
//! 共享原子(lang_zh / alert_*)和 Notify(refresh / check_update)在 spawn 时捕获,窗口里的
//! 按钮据此跨线程触发(notify_one / 写 config / 开子窗)。用 GTK scheme + Yaru 浅色让观感接近原生。
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::Arc;

use crate::config::{BUILD_TAG, REPO_URL, USAGE_PAGE_URL, VERSION};
use fltk::button::{Button, CheckButton};
use fltk::enums::{Align, Color, Event, Font, FrameType, Key};
use fltk::frame::Frame;
use fltk::misc::Spinner;
use fltk::prelude::*;
use fltk::window::Window;
use tokio::sync::Notify;

pub enum UiCmd {
    UsageAlert { pct: u8 },
    AlertSettings,
    MorePanel { update: Option<String>, feedback_url: String },
}

fn open(url: &str) {
    let _ = std::process::Command::new("xdg-open").arg(url).spawn();
}

#[allow(clippy::too_many_arguments)]
pub fn spawn(
    alert_en: Arc<AtomicBool>,
    alert_thr: Arc<AtomicU8>,
    lang_zh: Arc<AtomicBool>,
    refresh: Arc<Notify>,
    check_update: Arc<Notify>,
) -> Sender<UiCmd> {
    let (tx, rx) = mpsc::channel::<UiCmd>();
    let tx_self = tx.clone(); // 给窗口里的按钮用(如「用量提醒…」回投 AlertSettings)
    std::thread::Builder::new()
        .name("cui-ui".into())
        .spawn(move || {
            let _app = fltk::app::App::default();
            // 现代观感:GTK scheme + Yaru 风浅色背景 + 深色文字,去掉焦点虚线框
            fltk::app::set_scheme(fltk::app::Scheme::Gtk);
            fltk::app::background(0xf6, 0xf5, 0xf4);
            fltk::app::background2(0xff, 0xff, 0xff);
            fltk::app::foreground(0x2e, 0x34, 0x36);
            fltk::app::set_visible_focus(false);

            let mut alert: Option<Window> = None;
            let mut settings: Option<Window> = None;
            let mut more: Option<Window> = None;
            loop {
                let _ = fltk::app::wait_for(0.1);
                while let Ok(cmd) = rx.try_recv() {
                    let zh = lang_zh.load(Ordering::Relaxed);
                    match cmd {
                        UiCmd::UsageAlert { pct } => {
                            if alert.as_ref().map_or(true, |w| !w.shown()) {
                                alert = Some(usage_alert(pct, zh));
                            }
                        }
                        UiCmd::AlertSettings => {
                            if settings.as_ref().map_or(false, |w| w.shown()) {
                                continue;
                            }
                            let (en, thr) = (alert_en.load(Ordering::Relaxed), alert_thr.load(Ordering::Relaxed));
                            settings = Some(alert_settings(en, thr, zh, alert_en.clone(), alert_thr.clone()));
                        }
                        UiCmd::MorePanel { update, feedback_url } => {
                            if more.as_ref().map_or(false, |w| w.shown()) {
                                continue;
                            }
                            more = Some(more_panel(
                                update,
                                feedback_url,
                                lang_zh.clone(),
                                refresh.clone(),
                                check_update.clone(),
                                tx_self.clone(),
                            ));
                        }
                    }
                }
            }
        })
        .expect("spawn cui-ui thread");
    tx
}

/// 用量提醒:居中无边框小窗,红黑闪烁,大白字。点任意处 / Esc / 120s 关。(故意自定义配色,醒目)
fn usage_alert(pct: u8, zh: bool) -> Window {
    let (sw, sh) = fltk::app::screen_size();
    let (w, h) = (560, 240);
    let mut win = Window::new(((sw - w as f64) / 2.0) as i32, ((sh - h as f64) / 2.0) as i32, w, h, None);
    win.set_border(false);
    win.set_color(Color::from_rgb(0xe0, 0x31, 0x31));

    let big = if zh { format!("Current session 已使用 {pct}%") } else { format!("Current session at {pct}%") };
    let mut title = Frame::new(0, 70, w, 70, None);
    title.set_label(&big);
    title.set_label_size(34);
    title.set_label_font(Font::HelveticaBold);
    title.set_label_color(Color::White);
    title.set_frame(FrameType::NoBox);

    let mut hint = Frame::new(0, 152, w, 28, None);
    hint.set_label(if zh { "点任意处 / 按 Esc 关闭" } else { "Click anywhere / Esc to dismiss" });
    hint.set_label_size(12);
    hint.set_label_color(Color::from_rgb(0xf0, 0xc8, 0xc8));
    hint.set_frame(FrameType::NoBox);

    win.end();
    win.show();

    let mut wf = win.clone();
    let on = std::rc::Rc::new(std::cell::Cell::new(true));
    fltk::app::add_timeout3(0.55, move |handle| {
        if !wf.shown() {
            return;
        }
        wf.set_color(if on.get() { Color::from_rgb(0x14, 0x14, 0x14) } else { Color::from_rgb(0xe0, 0x31, 0x31) });
        wf.redraw();
        on.set(!on.get());
        fltk::app::repeat_timeout3(0.55, handle);
    });
    win.handle(|w, ev| match ev {
        Event::Push => { w.hide(); true }
        Event::KeyDown if fltk::app::event_key() == Key::Escape => { w.hide(); true }
        _ => false,
    });
    let mut wa = win.clone();
    fltk::app::add_timeout3(120.0, move |_| wa.hide());
    win
}

/// 用量提醒设置窗:第一行开关(CheckButton)、第二行阈值数字(Spinner)、底部 取消/保存。
/// 保存时写共享原子 + 持久化(菜单标签下轮渲染即反映)。GTK scheme 下观感接近原生。
fn alert_settings(enabled: bool, threshold: u8, zh: bool, en: Arc<AtomicBool>, thr: Arc<AtomicU8>) -> Window {
    let (sw, sh) = fltk::app::screen_size();
    let (w, h) = (440, 230);
    let mut win = Window::new(((sw - w as f64) / 2.0) as i32, ((sh - h as f64) / 2.0) as i32, w, h, None);
    win.set_label(if zh { "用量提醒" } else { "Usage alert" });

    let mut head = Frame::new(22, 18, w - 44, 28, None);
    head.set_label(if zh { "用量提醒" } else { "Usage alert" });
    head.set_label_size(17);
    head.set_label_font(Font::HelveticaBold);
    head.set_align(Align::Left | Align::Inside);
    head.set_frame(FrameType::NoBox);

    // 第一行:开关
    let mut chk = CheckButton::new(22, 60, w - 44, 30, if zh { " 开启当前会话用量提醒" } else { " Enable current-session usage alert" });
    chk.set_checked(enabled);
    chk.set_label_size(14);

    // 第二行:阈值数字
    let mut lbl = Frame::new(22, 108, 210, 32, None);
    lbl.set_label(if zh { "当前会话用量达到：" } else { "Alert when usage reaches:" });
    lbl.set_label_size(14);
    lbl.set_align(Align::Left | Align::Inside);
    lbl.set_frame(FrameType::NoBox);
    let mut sp = Spinner::new(236, 108, 86, 32, None);
    sp.set_minimum(1.0);
    sp.set_maximum(100.0);
    sp.set_step(1.0);
    sp.set_value(threshold as f64);
    sp.set_text_size(15);
    let mut pctf = Frame::new(324, 108, 24, 32, None);
    pctf.set_label("%");
    pctf.set_label_size(15);
    pctf.set_align(Align::Left | Align::Inside);
    pctf.set_frame(FrameType::NoBox);

    let mut cancel = Button::new(w - 210, 176, 92, 34, None);
    cancel.set_label(if zh { "取消" } else { "Cancel" });
    let mut save = Button::new(w - 108, 176, 88, 34, None);
    save.set_label(if zh { "保存" } else { "Save" });

    win.end();
    win.show();

    {
        let mut w2 = win.clone();
        cancel.set_callback(move |_| w2.hide());
    }
    {
        let mut w2 = win.clone();
        let chk2 = chk.clone();
        let sp2 = sp.clone();
        save.set_callback(move |_| {
            let v = (sp2.value().round() as i64).clamp(1, 100) as u8;
            let on = chk2.is_checked();
            en.store(on, Ordering::Relaxed);
            thr.store(v, Ordering::Relaxed);
            crate::creds::write_alert_cfg(on, v);
            println!("[alert] settings saved: enabled={on} threshold={v}%");
            w2.hide();
        });
    }
    win
}

fn lang_btn_label(zh: bool) -> String {
    if zh { "通知语言：中文".into() } else { "Notification language: English".into() }
}

/// More 弹窗:把原 More 子菜单的所有动作做成竖排按钮。一次性动作点完即关窗;语言就地切换并改标签;
/// 「用量提醒…」回投 AlertSettings 由事件循环开设置窗。所有跨线程操作走捕获进来的共享句柄。
#[allow(clippy::too_many_arguments)]
fn more_panel(
    update: Option<String>,
    feedback_url: String,
    lang_zh: Arc<AtomicBool>,
    refresh: Arc<Notify>,
    check_update: Arc<Notify>,
    tx: Sender<UiCmd>,
) -> Window {
    let zh = lang_zh.load(Ordering::Relaxed);
    // 动作按钮数 → 算窗高(refresh,[update],check,open,feedback | lang,alert | about,quit/uninstall,close)
    let n: i32 = 4 + i32::from(update.is_some()) + 2 + 3;
    let (w, bh, gap, grp) = (320, 34, 8, 12);
    let x = 16;
    let bw = w - 2 * x;
    let head_h = 30;
    let h = 14 + head_h + 6 + n * (bh + gap) + 2 * grp + 8;

    let (sw, sh) = fltk::app::screen_size();
    let mut win = Window::new(((sw - w as f64) / 2.0) as i32, ((sh - h as f64) / 2.0) as i32, w, h, None);
    win.set_label(if zh { "Claude 用量" } else { "Claude usage" });

    let mut head = Frame::new(x, 12, bw, head_h, None);
    head.set_label(if zh { "Claude 用量" } else { "Claude usage" });
    head.set_label_size(16);
    head.set_label_font(Font::HelveticaBold);
    head.set_align(Align::Left | Align::Inside);
    head.set_frame(FrameType::NoBox);

    // 所有 y 推进都在闭包里完成(闭包按可变借用持有 y),分组间距通过 gap_before 传入,避免外部再动 y。
    let mut y = 14 + head_h + 6;
    let mut mk = |gap_before: i32, label: &str| -> Button {
        y += gap_before;
        let mut b = Button::new(x, y, bw, bh, None);
        b.set_label(label);
        y += bh + gap;
        b
    };

    // —— 一次性动作:执行后关窗 ——
    let mut b_refresh = mk(0, if zh { "立即刷新" } else { "Refresh now" });
    {
        let r = refresh.clone();
        let mut wc = win.clone();
        b_refresh.set_callback(move |_| { r.notify_one(); wc.hide(); });
    }

    if let Some(ver) = update.clone() {
        let mut b_upd = mk(0, &if zh { format!("⬆ 更新到 v{ver}") } else { format!("⬆ Update now → v{ver}") });
        b_upd.set_color(Color::from_rgb(0x2e, 0x7d, 0x32)); // 醒目绿
        b_upd.set_label_color(Color::White);
        let mut wc = win.clone();
        b_upd.set_callback(move |_| { crate::selfupdate::spawn_detached(); wc.hide(); });
    }

    let mut b_check = mk(0, if zh { "检查更新" } else { "Check for updates" });
    {
        let c = check_update.clone();
        let mut wc = win.clone();
        b_check.set_callback(move |_| { c.notify_one(); wc.hide(); });
    }

    let mut b_open = mk(0, if zh { "打开 Claude 用量页面" } else { "Open Claude Usage page" });
    {
        let mut wc = win.clone();
        b_open.set_callback(move |_| { open(USAGE_PAGE_URL); wc.hide(); });
    }

    let mut b_fb = mk(0, if zh { "反馈 / 报告问题" } else { "Send feedback / report issue" });
    {
        let url = feedback_url.clone();
        let mut wc = win.clone();
        b_fb.set_callback(move |_| { open(&url); wc.hide(); });
    }

    // —— 设置类:语言就地切换、用量提醒开设置窗 ——
    let mut b_lang = mk(grp, &lang_btn_label(zh));
    {
        let lz = lang_zh.clone();
        b_lang.set_callback(move |b| {
            let nz = !lz.load(Ordering::Relaxed);
            lz.store(nz, Ordering::Relaxed);
            crate::creds::write_lang(nz);
            b.set_label(&lang_btn_label(nz));
            b.redraw();
        });
    }

    let mut b_alert = mk(0, if zh { "用量提醒…" } else { "Usage alert…" });
    {
        let t = tx.clone();
        let mut wc = win.clone();
        b_alert.set_callback(move |_| { let _ = t.send(UiCmd::AlertSettings); wc.hide(); });
    }

    // —— 关于 / 退出 / 关闭 ——
    let mut b_about = mk(grp, &format!("About (GitHub)  v{VERSION}{BUILD_TAG}"));
    {
        let mut wc = win.clone();
        b_about.set_callback(move |_| { open(REPO_URL); wc.hide(); });
    }

    #[cfg(not(feature = "dev"))]
    {
        let mut b_uninstall = mk(0, if zh { "卸载…" } else { "Uninstall…" });
        b_uninstall.set_color(Color::from_rgb(0xc0, 0x39, 0x2b)); // 危险红
        b_uninstall.set_label_color(Color::White);
        b_uninstall.set_callback(move |_| {
            crate::uninstall::spawn_detached_uninstall();
            std::process::exit(0);
        });
    }
    #[cfg(feature = "dev")]
    {
        let mut b_quit = mk(0, "Quit (rust-dev)");
        b_quit.set_callback(move |_| std::process::exit(0));
    }

    let mut b_close = mk(0, if zh { "关闭" } else { "Close" });
    {
        let mut wc = win.clone();
        b_close.set_callback(move |_| wc.hide());
    }

    win.end();
    win.show();
    win.handle(|w, ev| match ev {
        Event::KeyDown if fltk::app::event_key() == Key::Escape => { w.hide(); true }
        _ => false,
    });
    win
}
