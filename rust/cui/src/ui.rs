//! 弹窗 UI（fltk,无 GTK）。fltk 在自己的线程跑事件循环(主线程是 tokio+ksni),主程序经 channel 发命令:
//!  - UsageAlert：用量穿过阈值的醒目闪窗;
//!  - AlertSettings：用量提醒设置窗(开关 + 阈值数字);
//!  - MorePanel：把原 More 子菜单的所有动作做成一个弹窗里的按钮列表。
//! 共享原子(lang_zh / alert_*)和 Notify(refresh / check_update)在 spawn 时捕获,窗口里的
//! 按钮据此跨线程触发(notify_one / 写 config / 开子窗)。用 GTK scheme + Yaru 浅色让观感接近原生。
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};

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
    MorePanel { lines: Vec<String>, update: Option<String>, feedback_url: String },
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
    lines_shared: Arc<Mutex<Vec<String>>>, // 托盘每秒写入最新 usage_lines,弹窗据此实时刷新
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
            // 默认字体改成 Noto Sans CJK SC:中英文同一字体 → 语言切换基线一致(不再上下跳);
            // 非等宽且含方块字符 → 进度条渲染贴近托盘菜单(桌面 sans),不再是等宽 Courier 那种突兀样式。
            Font::set_font(Font::Helvetica, "Noto Sans CJK SC");

            let mut alert: Option<Window> = None;
            let mut settings: Option<Window> = None;
            let mut more: Option<Window> = None;
            loop {
                let _ = fltk::app::wait_for(0.1);
                while let Ok(cmd) = rx.try_recv() {
                    match cmd {
                        UiCmd::UsageAlert { pct } => {
                            if alert.as_ref().map_or(true, |w| !w.shown()) {
                                alert = Some(usage_alert(pct));
                            }
                        }
                        UiCmd::AlertSettings => {
                            if settings.as_ref().map_or(false, |w| w.shown()) {
                                continue;
                            }
                            let (en, thr) = (alert_en.load(Ordering::Relaxed), alert_thr.load(Ordering::Relaxed));
                            settings = Some(alert_settings(en, thr, alert_en.clone(), alert_thr.clone()));
                        }
                        UiCmd::MorePanel { lines, update, feedback_url } => {
                            if more.as_ref().map_or(false, |w| w.shown()) {
                                continue;
                            }
                            more = Some(more_panel(
                                lines,
                                update,
                                feedback_url,
                                lang_zh.clone(),
                                refresh.clone(),
                                check_update.clone(),
                                lines_shared.clone(),
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
fn usage_alert(pct: u8) -> Window {
    let (sw, sh) = fltk::app::screen_size();
    let (w, h) = (560, 240);
    let mut win = Window::new(((sw - w as f64) / 2.0) as i32, ((sh - h as f64) / 2.0) as i32, w, h, None);
    win.set_border(false);
    win.set_color(Color::from_rgb(0xe0, 0x31, 0x31));

    let mut title = Frame::new(0, 70, w, 70, None);
    title.set_label(&format!("Current session at {pct}%"));
    title.set_label_size(34);
    title.set_label_font(Font::HelveticaBold);
    title.set_label_color(Color::White);
    title.set_frame(FrameType::NoBox);

    let mut hint = Frame::new(0, 152, w, 28, None);
    hint.set_label("Click anywhere / Esc to dismiss");
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

/// 用量提醒设置窗:第一行开关(CheckButton)、第二行阈值数字(Spinner)、底部 Cancel/Save。
/// 保存时写共享原子 + 持久化。全英文(与托盘/菜单一致,只有通知是双语)。GTK scheme 下观感接近原生。
fn alert_settings(enabled: bool, threshold: u8, en: Arc<AtomicBool>, thr: Arc<AtomicU8>) -> Window {
    let (sw, sh) = fltk::app::screen_size();
    let (w, h) = (440, 230);
    let mut win = Window::new(((sw - w as f64) / 2.0) as i32, ((sh - h as f64) / 2.0) as i32, w, h, None);
    win.set_label("Usage alert");

    let mut head = Frame::new(22, 18, w - 44, 28, None);
    head.set_label("Usage alert");
    head.set_label_size(17);
    head.set_label_font(Font::HelveticaBold);
    head.set_align(Align::Left | Align::Inside);
    head.set_frame(FrameType::NoBox);

    // 第一行:开关
    let mut chk = CheckButton::new(22, 60, w - 44, 30, " Enable current-session usage alert");
    chk.set_checked(enabled);
    chk.set_label_size(14);

    // 第二行:阈值数字
    let mut lbl = Frame::new(22, 108, 210, 32, None);
    lbl.set_label("Alert when usage reaches:");
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
    cancel.set_label("Cancel");
    let mut save = Button::new(w - 108, 176, 88, 34, None);
    save.set_label("Save");

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
    // 前缀恒为英文(与托盘/菜单一致),只切换取值:English ⇄ 中文
    format!("Notification language: {}", if zh { "中文" } else { "English" })
}

/// More 弹窗:顶部是与托盘菜单一模一样的用量进度条(等宽渲染,只读快照),下面是原 More 子菜单的
/// 所有动作按钮(全英文 chrome,与托盘一致)。一次性动作点完即关窗;语言就地切换并改标签;
/// 「Usage alert…」回投 AlertSettings 由事件循环开设置窗。所有跨线程操作走捕获进来的共享句柄。
#[allow(clippy::too_many_arguments)]
fn more_panel(
    lines: Vec<String>,
    update: Option<String>,
    feedback_url: String,
    lang_zh: Arc<AtomicBool>,
    refresh: Arc<Notify>,
    check_update: Arc<Notify>,
    lines_shared: Arc<Mutex<Vec<String>>>,
    tx: Sender<UiCmd>,
) -> Window {
    // 动作按钮数(refresh,[update],check,open,feedback | lang,alert | about,quit/uninstall,close)
    let n: i32 = 4 + i32::from(update.is_some()) + 2 + 3;
    // 窗宽要容下 24 格进度条 + 右侧百分比(否则百分比被截掉),与托盘菜单一致
    let (w, bh, gap, grp) = (440, 34, 8, 12);
    let x = 16;
    let bw = w - 2 * x;
    let line_h = 24; // 每行行高:贴近托盘菜单的菜单行高(multiline 单帧那种紧凑行距太挤)
    let info_y = 10;
    let info_h = lines.len() as i32 * line_h;
    let sep_y = info_y + info_h + 8; // 文本块底部留白再画分隔线
    let top = sep_y + 14; // 首个按钮在分隔线下方 14px
    let h = top + n * (bh + gap) + 2 * grp + 8;

    let (sw, sh) = fltk::app::screen_size();
    let mut win = Window::new(((sw - w as f64) / 2.0) as i32, ((sh - h as f64) / 2.0) as i32, w, h, None);
    win.set_label("Claude usage");

    // 顶部用量行:与托盘菜单同样的文本(bar()+pct())。每行单独一个等高 Frame、垂直居中,行高/间距贴近
    // 托盘菜单的菜单行(比 multiline 单帧的字体固定行距更接近)。灰色仿菜单 disabled 行;默认 sans 渲染方块条。
    let mut info_frames: Vec<Frame> = Vec::with_capacity(lines.len());
    for (i, ln) in lines.iter().enumerate() {
        let mut f = Frame::new(x, info_y + i as i32 * line_h, bw, line_h, None);
        f.set_label(ln);
        f.set_label_size(14);
        f.set_label_color(Color::from_rgb(0x44, 0x47, 0x42));
        f.set_align(Align::Left | Align::Inside); // 垂直居中于该行
        f.set_frame(FrameType::NoBox);
        info_frames.push(f);
    }
    let mut sep = Frame::new(x, sep_y, bw, 1, None);
    sep.set_frame(FrameType::FlatBox);
    sep.set_color(Color::from_rgb(0xd6, 0xd3, 0xce));

    // 所有 y 推进都在闭包里完成(闭包按可变借用持有 y),分组间距通过 gap_before 传入,避免外部再动 y。
    let mut y = top;
    let mut mk = |gap_before: i32, label: &str| -> Button {
        y += gap_before;
        let mut b = Button::new(x, y, bw, bh, None);
        b.set_label(label);
        y += bh + gap;
        b
    };

    // —— 一次性动作:执行后【不】关窗(关闭只通过底部 Close / Esc),否则 Close 没意义 ——
    let mut b_refresh = mk(0, "Refresh now");
    {
        let r = refresh.clone();
        b_refresh.set_callback(move |_| r.notify_one());
    }

    if let Some(ver) = update.clone() {
        let mut b_upd = mk(0, &format!("⬆ Update now → v{ver}"));
        b_upd.set_color(Color::from_rgb(0x2e, 0x7d, 0x32)); // 醒目绿
        b_upd.set_label_color(Color::White);
        b_upd.set_callback(move |_| crate::selfupdate::spawn_detached());
    }

    let mut b_check = mk(0, "Check for updates");
    {
        let c = check_update.clone();
        b_check.set_callback(move |_| c.notify_one());
    }

    let mut b_open = mk(0, "Open Claude Usage page");
    b_open.set_callback(move |_| open(USAGE_PAGE_URL));

    let mut b_fb = mk(0, "Send feedback / report issue");
    {
        let url = feedback_url.clone();
        b_fb.set_callback(move |_| open(&url));
    }

    // —— 设置类:语言就地切换(只换取值,前缀不变)、用量提醒开设置窗 ——
    let mut b_lang = mk(grp, &lang_btn_label(lang_zh.load(Ordering::Relaxed)));
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

    let mut b_alert = mk(0, "Usage alert…");
    {
        let t = tx.clone();
        b_alert.set_callback(move |_| {
            let _ = t.send(UiCmd::AlertSettings); // 开设置窗,More 保持打开
        });
    }

    // —— 关于 / 退出 / 关闭 ——
    let mut b_about = mk(grp, &format!("About (GitHub)  v{VERSION}{BUILD_TAG}"));
    b_about.set_callback(move |_| open(REPO_URL));

    #[cfg(not(feature = "dev"))]
    {
        let mut b_uninstall = mk(0, "Uninstall…");
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

    let mut b_close = mk(0, "Close");
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

    // 用量行随托盘一起动:窗开着时每秒从共享态刷新(倒计时走字、新一轮轮询的数值)。行数变了才跳过(留待重开)。
    {
        let mut frames = info_frames.clone();
        let ls = lines_shared.clone();
        let win_t = win.clone();
        fltk::app::add_timeout3(1.0, move |handle| {
            if !win_t.shown() {
                return; // 窗关了就停,不再 re-arm
            }
            if let Ok(g) = ls.lock() {
                if g.len() == frames.len() {
                    for (f, ln) in frames.iter_mut().zip(g.iter()) {
                        f.set_label(ln);
                        f.redraw();
                    }
                }
            }
            fltk::app::repeat_timeout3(1.0, handle);
        });
    }
    win
}
