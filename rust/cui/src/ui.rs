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
use crate::creds::Account;
use fltk::button::{Button, CheckButton};
use fltk::enums::{Align, Color, Event, Font, FrameType, Key};
use fltk::frame::Frame;
use fltk::input::IntInput;
use fltk::prelude::*;
use fltk::window::Window;

/// 阈值输入框 ±delta(钳到 1..100)。−/+ 按钮和键盘上下键共用。
fn bump(inp: &mut IntInput, delta: i64) {
    let v = (inp.value().trim().parse::<i64>().unwrap_or(80) + delta).clamp(1, 100);
    inp.set_value(&v.to_string());
}

/// 窗口图标(任务栏 / WM 标题栏):珊瑚圆角底,左上是真 Claude 符号(白),右上短条 + 下方长条。
/// 预合成 PNG(assets/icon.png:珊瑚底 + 真 Claude 符号[白]居左上 + 右上短条 + 下方长条),编进二进制。
/// 必须在 show() 之前 set_icon,WM 才会在映射时读取。
fn set_window_icon(win: &mut Window) {
    if let Ok(img) = fltk::image::PngImage::from_data(include_bytes!("../assets/icon.png")) {
        win.set_icon(Some(img));
    }
}
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
    alert_fired: Arc<AtomicBool>, // 去重/武装标志(与 poller 共享):设置窗保存时重置以重新武装
    cur_util: Arc<AtomicU8>,      // 最近一次轮询的 current session 用量(设置窗保存时立即评估用)
    lang_zh: Arc<AtomicBool>,
    refresh: Arc<Notify>,
    check_update: Arc<Notify>,
    lines_shared: Arc<Mutex<Vec<String>>>, // 托盘每秒写入最新 usage_lines,弹窗据此实时刷新
    accounts_shared: Arc<Mutex<Vec<Account>>>, // 全部可选账号(多账号切换列表，与托盘/poller 共享)
    active_shared: Arc<Mutex<Option<Account>>>, // 当前选中账号(切换即写，poller 下一轮生效)
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
            // 注意:不能关全局 visible_focus —— 关了之后只有文本输入框能拿焦点(它强制接受),
            // 于是阈值框开窗即被聚焦、常驻光标且无法移走。保持开启,再用 take_focus 指定初始焦点。
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
                            // 关掉旧窗(可能被别的窗盖住,XMapRaised 在 Mutter 下不生效)→ 重建一个新窗,
                            // 新窗会出现在最上层(与初次打开一致),并重跑去光标的焦点处理。
                            if let Some(w) = settings.as_mut() {
                                w.hide();
                            }
                            let (en, thr) = (alert_en.load(Ordering::Relaxed), alert_thr.load(Ordering::Relaxed));
                            settings = Some(alert_settings(
                                en,
                                thr,
                                alert_en.clone(),
                                alert_thr.clone(),
                                alert_fired.clone(),
                                cur_util.clone(),
                                tx_self.clone(),
                            ));
                        }
                        UiCmd::MorePanel { lines, update, feedback_url } => {
                            // 关掉旧窗 → 重建,使其出现在最上层(再点 More 能把被盖住的弹窗调回最前)
                            if let Some(w) = more.as_mut() {
                                w.hide();
                            }
                            let accts = accounts_shared.lock().ok().map(|g| g.clone()).unwrap_or_default();
                            let active_org = active_shared
                                .lock()
                                .ok()
                                .and_then(|g| g.clone())
                                .map(|a| a.org_id)
                                .unwrap_or_default();
                            more = Some(more_panel(
                                lines,
                                update,
                                feedback_url,
                                accts,
                                active_org,
                                lang_zh.clone(),
                                refresh.clone(),
                                check_update.clone(),
                                lines_shared.clone(),
                                active_shared.clone(),
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

/// 用量提醒设置窗:第一行开关、第二行阈值(IntInput 可直接打字 + −/+ 按钮 + 键盘上下键)、底部 Cancel/Save。
/// 保存时:写共享原子 + 持久化,并【重新武装】(改了设置就允许重新触发)+【按当前用量立即评估一次】——
/// 若已开启且当前用量已达阈值,马上弹,不必等下一次轮询(这正是之前"设了却没弹"的根因)。全英文 chrome。
#[allow(clippy::too_many_arguments)]
fn alert_settings(
    enabled: bool,
    threshold: u8,
    en: Arc<AtomicBool>,
    thr: Arc<AtomicU8>,
    alert_fired: Arc<AtomicBool>,
    cur_util: Arc<AtomicU8>,
    tx: Sender<UiCmd>,
) -> Window {
    let (sw, sh) = fltk::app::screen_size();
    let (w, h) = (440, 230);
    let mut win = Window::new(((sw - w as f64) / 2.0) as i32, ((sh - h as f64) / 2.0) as i32, w, h, None);
    win.set_label("Usage alert");
    set_window_icon(&mut win);

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
    chk.clear_visible_focus(); // 不画虚线焦点框

    // 第二行:阈值。IntInput 直接打字即生效(value() 实时反映,不必回车);−/+ 按钮与键盘上下键各 ±1。
    let mut lbl = Frame::new(22, 108, 200, 32, None);
    lbl.set_label("Alert when usage reaches:");
    lbl.set_label_size(14);
    lbl.set_align(Align::Left | Align::Inside);
    lbl.set_frame(FrameType::NoBox);
    let mut minus = Button::new(228, 108, 30, 32, None);
    minus.set_label("−");
    minus.clear_visible_focus();
    let mut input = IntInput::new(260, 108, 56, 32, None);
    input.set_value(&threshold.to_string());
    input.set_text_size(15);
    let mut plus = Button::new(318, 108, 30, 32, None);
    plus.set_label("+");
    plus.clear_visible_focus();
    let mut pctf = Frame::new(352, 108, 24, 32, None);
    pctf.set_label("%");
    pctf.set_label_size(15);
    pctf.set_align(Align::Left | Align::Inside);
    pctf.set_frame(FrameType::NoBox);
    {
        let mut inp = input.clone();
        minus.set_callback(move |_| bump(&mut inp, -1));
    }
    {
        let mut inp = input.clone();
        plus.set_callback(move |_| bump(&mut inp, 1));
    }
    input.handle(|inp, ev| match ev {
        Event::KeyDown => match fltk::app::event_key() {
            Key::Up => { bump(inp, 1); true }
            Key::Down => { bump(inp, -1); true }
            _ => false,
        },
        _ => false,
    });

    let mut cancel = Button::new(w - 210, 176, 92, 34, None);
    cancel.set_label("Cancel");
    cancel.clear_visible_focus();
    let mut save = Button::new(w - 108, 176, 88, 34, None);
    save.set_label("Save");

    win.end();
    win.show();
    // 延后到映射之后再移焦(同步 take_focus 会被映射时的自动聚焦覆盖)→ 焦点落 Save(默认按钮),
    // 阈值输入框不再常驻光标(点它打字时才获焦出现光标)。
    {
        let mut save_f = save.clone();
        fltk::app::add_timeout3(0.05, move |_| {
            let _ = save_f.take_focus(); // 全局开焦点 → 能拿到焦点
            save_f.clear_visible_focus(); // 但清掉它的焦点框标志 → 不画虚线框
            save_f.redraw();
        });
    }

    {
        let mut w2 = win.clone();
        cancel.set_callback(move |_| w2.hide());
    }
    {
        let mut w2 = win.clone();
        let chk2 = chk.clone();
        let inp2 = input.clone();
        save.set_callback(move |_| {
            let v = inp2.value().trim().parse::<i64>().unwrap_or(threshold as i64).clamp(1, 100) as u8;
            let on = chk2.is_checked();
            en.store(on, Ordering::Relaxed);
            thr.store(v, Ordering::Relaxed);
            crate::creds::write_alert_cfg(on, v);
            alert_fired.store(false, Ordering::Relaxed); // 改了设置 → 重新武装,新阈值有机会触发
            let u = cur_util.load(Ordering::Relaxed);
            let fire_now = on && u >= v;
            if fire_now {
                alert_fired.store(true, Ordering::Relaxed); // 当前已达阈值 → 立刻弹,并置位避免 poller 重复弹
                let _ = tx.send(UiCmd::UsageAlert { pct: u });
            }
            println!("[alert] saved: enabled={on} threshold={v}% (cur {u}% → {})", if fire_now { "fire now" } else { "armed" });
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
    accounts: Vec<Account>,
    active_org: String,
    lang_zh: Arc<AtomicBool>,
    refresh: Arc<Notify>,
    check_update: Arc<Notify>,
    lines_shared: Arc<Mutex<Vec<String>>>,
    active_shared: Arc<Mutex<Option<Account>>>,
    tx: Sender<UiCmd>,
) -> Window {
    // >1 个账号才显示「账号切换」组(组0)
    let n_acct: i32 = if accounts.len() > 1 { accounts.len() as i32 } else { 0 };
    // 动作按钮数:[账号*N]+[update?]+refresh+open+check | alert+lang | feedback+about | uninstall/quit+close
    let n: i32 = n_acct + 3 + i32::from(update.is_some()) + 2 + 2 + 2;
    let groups: i32 = 3 + i32::from(n_acct > 0); // 账号组存在时多一处分组间距
    // 窗宽要容下 24 格进度条 + 右侧百分比(否则百分比被截掉),与托盘菜单一致
    let (w, bh, gap, grp) = (440, 34, 8, 12);
    let x = 16;
    let bw = w - 2 * x;
    let line_h = 24; // 每行行高:贴近托盘菜单的菜单行高(multiline 单帧那种紧凑行距太挤)
    let info_y = 10;
    let info_h = lines.len() as i32 * line_h;
    let sep_y = info_y + info_h + 8; // 文本块底部留白再画分隔线
    let top = sep_y + 14; // 首个按钮在分隔线下方 14px
    let h = top + n * (bh + gap) + groups * grp + 8;

    let (sw, sh) = fltk::app::screen_size();
    let mut win = Window::new(((sw - w as f64) / 2.0) as i32, ((sh - h as f64) / 2.0) as i32, w, h, None);
    win.set_label("Claude usage");
    set_window_icon(&mut win);

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
        b.clear_visible_focus(); // 不画虚线焦点框(全局焦点仍开,take_focus 仍可用)
        y += bh + gap;
        b
    };

    // 组0 多账号(公司/个人)切换:● = 当前选中。点选即切 active + 持久化 active_org + 立即刷新 + 关窗
    //（重开/托盘即显示新选中；窗顶用量行也会在刷新后跟着变成新账号）。>1 账号才有此组。
    if n_acct > 0 {
        for a in &accounts {
            let active = a.org_id == active_org;
            let dot = if active { "●" } else { "○" };
            let mut b = mk(0, &format!("{dot}  {}", crate::tray::account_label(&accounts, a)));
            if active {
                b.set_label_color(Color::from_rgb(0x2e, 0x7d, 0x32)); // 选中绿
            }
            let chosen = a.clone();
            let act = active_shared.clone();
            let r = refresh.clone();
            let mut wc = win.clone();
            b.set_callback(move |_| {
                if let Ok(mut g) = act.lock() {
                    *g = Some(chosen.clone());
                }
                crate::creds::write_active_org(&chosen.org_id);
                r.notify_one();
                wc.hide();
            });
        }
    }
    let g0 = if n_acct > 0 { grp } else { 0 }; // 账号组与主动作组之间的分组间距

    // 按钮顺序(用户选定·分组):动作执行后【不】关窗,关闭只通过底部 Close / Esc。
    // 组1 主要动作:[Update(有更新才显示,置顶高亮)] Refresh / Open page / Check for updates
    if let Some(ver) = update.clone() {
        let mut b_upd = mk(g0, &format!("⬆ Update now → v{ver}"));
        b_upd.set_color(Color::from_rgb(0x2e, 0x7d, 0x32)); // 醒目绿
        b_upd.set_label_color(Color::White);
        b_upd.set_callback(move |_| crate::selfupdate::spawn_detached());
    }

    let mut b_refresh = mk(if update.is_some() { 0 } else { g0 }, "Refresh now");
    {
        let r = refresh.clone();
        b_refresh.set_callback(move |_| r.notify_one());
    }

    let mut b_open = mk(0, "Open Claude Usage page");
    b_open.set_callback(move |_| open(USAGE_PAGE_URL));

    let mut b_check = mk(0, "Check for updates");
    {
        let c = check_update.clone();
        b_check.set_callback(move |_| c.notify_one());
    }

    // 组2 设置:用量提醒 / 通知语言(就地切换,只换取值前缀不变)
    let mut b_alert = mk(grp, "Usage alert…");
    {
        let t = tx.clone();
        b_alert.set_callback(move |_| {
            let _ = t.send(UiCmd::AlertSettings); // 开设置窗,More 保持打开
        });
    }

    let mut b_lang = mk(0, &lang_btn_label(lang_zh.load(Ordering::Relaxed)));
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

    // 组3 信息/反馈:反馈 / About
    let mut b_fb = mk(grp, "Send feedback / report issue");
    {
        let url = feedback_url.clone();
        b_fb.set_callback(move |_| open(&url));
    }

    let mut b_about = mk(0, &format!("About (GitHub)  v{VERSION}{BUILD_TAG}"));
    b_about.set_callback(move |_| open(REPO_URL));

    // 组4 危险操作 + 关闭:Uninstall(prod,红)/ Quit(dev) 紧挨 Close
    #[cfg(not(feature = "dev"))]
    {
        let mut b_uninstall = mk(grp, "Uninstall…");
        b_uninstall.set_color(Color::from_rgb(0xc0, 0x39, 0x2b)); // 危险红
        b_uninstall.set_label_color(Color::White);
        b_uninstall.set_callback(move |_| {
            crate::uninstall::spawn_detached_uninstall();
            std::process::exit(0);
        });
    }
    #[cfg(feature = "dev")]
    {
        let mut b_quit = mk(grp, "Quit (dev)");
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

    // 开窗后把焦点放到 Close(而非首个动作按钮)→ 回车不会误触 Update/Refresh,焦点框也不落在显眼动作上。
    {
        let mut bc = b_close.clone();
        fltk::app::add_timeout3(0.05, move |_| {
            let _ = bc.take_focus();
        });
    }

    // 用量行随托盘一起动:窗开着时每 0.25s 从共享态刷新(倒计时走字、Refresh 后低延迟反映新数据)。
    // 只在文本变了才重绘(避免每秒空刷)。行数变了跳过(留待重开)。
    {
        let mut frames = info_frames.clone();
        let ls = lines_shared.clone();
        let win_t = win.clone();
        fltk::app::add_timeout3(0.25, move |handle| {
            if !win_t.shown() {
                return; // 窗关了就停,不再 re-arm
            }
            if let Ok(g) = ls.lock() {
                if g.len() == frames.len() {
                    for (f, ln) in frames.iter_mut().zip(g.iter()) {
                        if f.label() != *ln {
                            f.set_label(ln);
                            f.redraw();
                        }
                    }
                }
            }
            fltk::app::repeat_timeout3(0.25, handle);
        });
    }
    win
}
