//! 纯逻辑层：与 Python `cui/model.py` 等价（解析 / 格式化 / 版本比较 / 通知策略）。
//! 无任何 IO / GUI / 网络依赖，可独立编译与单测——用于在迁移中锁定与 Python 完全一致的行为。
use chrono::{DateTime, Local, Timelike, Utc};

/// 接口原始值（对应 Python json_to_raw 的输出 + UsageData 的数值/时间字段）。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Raw {
    pub five_hour_util: Option<f64>,
    pub five_hour_reset: Option<DateTime<Utc>>,
    pub seven_day_util: Option<f64>,
    pub seven_day_reset: Option<DateTime<Utc>>,
    pub sonnet_util: Option<f64>,
    pub sonnet_reset: Option<DateTime<Utc>>,
    pub opus_util: Option<f64>,
    pub opus_reset: Option<DateTime<Utc>>,
}

/// 接口结构不符（对应 Python SchemaError）。
#[derive(Debug, Clone, PartialEq)]
pub struct SchemaError(pub String);

// ===================== 解析 =====================
/// 解析 ISO8601（兼容 'Z' 与无时区→按 UTC），对应 Python _parse_iso。
pub fn parse_iso(s: Option<&str>) -> Option<DateTime<Utc>> {
    let s = s?.trim();
    if s.is_empty() {
        return None;
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
        return Some(dt.with_timezone(&Utc));
    }
    if let Ok(ndt) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S") {
        return Some(ndt.and_utc()); // 无时区按 UTC，避免 aware/naive 相减崩溃
    }
    None
}

fn util(o: &serde_json::Value) -> Option<f64> {
    o.get("utilization").and_then(|x| x.as_f64())
}
fn reset(o: &serde_json::Value) -> Option<DateTime<Utc>> {
    parse_iso(o.get("resets_at").and_then(|x| x.as_str()))
}

/// 抽取原始数值/时间（对应 Python json_to_raw）。
pub fn json_to_raw(j: &serde_json::Value) -> Raw {
    let g = |k: &str| j.get(k);
    Raw {
        five_hour_util: g("five_hour").and_then(util),
        five_hour_reset: g("five_hour").and_then(reset),
        seven_day_util: g("seven_day").and_then(util),
        seven_day_reset: g("seven_day").and_then(reset),
        sonnet_util: g("seven_day_sonnet").and_then(util),
        sonnet_reset: g("seven_day_sonnet").and_then(reset),
        opus_util: g("seven_day_opus").and_then(util),
        opus_reset: g("seven_day_opus").and_then(reset),
    }
}

/// 校验 JSON 契约并抽取（对应 Python validate_and_extract）。
pub fn validate_and_extract(data: &serde_json::Value) -> Result<Raw, SchemaError> {
    if !data.is_object() {
        return Err(SchemaError("top level is not an object".into()));
    }
    for key in ["five_hour", "seven_day"] {
        match data.get(key) {
            Some(o) if o.is_object() => {
                if !o.get("utilization").map(|u| u.is_number()).unwrap_or(false) {
                    return Err(SchemaError(format!(
                        "{key}.utilization is not a number (API schema changed?)"
                    )));
                }
            }
            _ => {
                return Err(SchemaError(format!(
                    "missing required field {key} (API schema changed?)"
                )))
            }
        }
    }
    Ok(json_to_raw(data))
}

// ===================== 格式化（渲染层即时计算） =====================
/// 百分比：None→"--"，否则四舍五入整数%（对应 Python _pct）。
pub fn pct(u: Option<f64>) -> String {
    match u {
        None => "--".to_string(),
        Some(v) => format!("{}%", v.round() as i64),
    }
}

/// 文字进度条 ▕████░░░░▏（对应 Python _bar）。None / 0 都为全空。
pub fn bar(u: Option<f64>, n: usize) -> String {
    let fill = match u {
        None => 0,
        Some(v) => ((n as f64) * v.clamp(0.0, 100.0) / 100.0).round() as usize,
    };
    let mut s = String::from("▕");
    s.push_str(&"█".repeat(fill));
    s.push_str(&"░".repeat(n - fill));
    s.push('▏');
    s
}

/// 倒计时短格式 '2h3m' / '45m'（对应 Python _fmt_countdown）。
pub fn fmt_countdown(dt: Option<DateTime<Utc>>) -> String {
    match dt {
        None => "--".to_string(),
        Some(d) => {
            let secs = (d - Utc::now()).num_seconds();
            if secs <= 0 {
                return "0m".to_string();
            }
            let (h, m) = (secs / 3600, (secs % 3600) / 60);
            if h > 0 {
                format!("{h}h{m}m")
            } else {
                format!("{m}m")
            }
        }
    }
}

/// 重置绝对时刻短格式 'Mon 7am' / 'Tue 3:30pm'（对应 Python _fmt_resetday）。
pub fn fmt_resetday(dt: Option<DateTime<Utc>>) -> String {
    match dt {
        None => "--".to_string(),
        Some(d) => {
            let loc = d.with_timezone(&Local);
            let (pm, h12) = loc.hour12();
            let ap = if pm { "pm" } else { "am" };
            let wd = loc.format("%a");
            let m = loc.minute();
            if m != 0 {
                format!("{wd} {h12}:{m:02}{ap}")
            } else {
                format!("{wd} {h12}{ap}")
            }
        }
    }
}

/// 菜单用全格式倒计时 '3 hr 17 min'（对应 Python _fmt_countdown_long）。
pub fn fmt_countdown_long(dt: Option<DateTime<Utc>>) -> String {
    match dt {
        None => String::new(),
        Some(d) => {
            let secs = (d - Utc::now()).num_seconds();
            if secs <= 0 {
                return "0 min".to_string();
            }
            let (h, m) = (secs / 3600, (secs % 3600) / 60);
            let mut parts: Vec<String> = Vec::new();
            if h > 0 {
                parts.push(format!("{h} hr"));
            }
            if m > 0 || h == 0 {
                parts.push(format!("{m} min"));
            }
            parts.join(" ")
        }
    }
}

/// 菜单用全格式重置时刻 'Mon 7:00 AM'（对应 Python _fmt_resetday_long）。
pub fn fmt_resetday_long(dt: Option<DateTime<Utc>>) -> String {
    match dt {
        None => String::new(),
        Some(d) => {
            let loc = d.with_timezone(&Local);
            let (pm, h12) = loc.hour12();
            let ap = if pm { "PM" } else { "AM" };
            format!("{} {h12}:{:02} {ap}", loc.format("%a"), loc.minute())
        }
    }
}

// ===================== 版本比较 =====================
/// "2.10.1" -> [2,10,1]；非纯数字/空 -> []（对应 Python _ver_tuple）。
pub fn ver_tuple(s: &str) -> Vec<u64> {
    s.trim()
        .split('.')
        .map(|x| x.parse::<u64>())
        .collect::<Result<Vec<u64>, _>>()
        .unwrap_or_default()
}

/// 远端是否更新（对应 Python remote_is_newer）。
pub fn remote_is_newer(remote: &str, local: &str) -> bool {
    let r = ver_tuple(remote);
    !r.is_empty() && r > ver_tuple(local)
}

// ===================== 通知策略 =====================
/// 故障分级（对应 Python status_level）：需用户处理→critical，瞬时自愈→normal。
pub fn status_level(status: &str) -> &'static str {
    match status {
        "auth" | "cookie" | "cloudflare" | "schema" => "critical",
        _ => "normal",
    }
}

/// 是否（再）弹故障告警（对应 Python should_notify_bad）。
pub fn should_notify_bad(
    consecutive_failures: u32,
    status: &str,
    notified_status: &str,
    secs_since_last: f64,
    renotify_s: f64,
) -> bool {
    if consecutive_failures < 2 {
        return false;
    }
    status != notified_status || secs_since_last > renotify_s
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    #[test]
    fn test_pct() {
        assert_eq!(pct(None), "--");
        assert_eq!(pct(Some(0.0)), "0%");
        assert_eq!(pct(Some(50.0)), "50%");
        assert_eq!(pct(Some(49.6)), "50%");
        assert_eq!(pct(Some(99.4)), "99%");
    }

    #[test]
    fn test_bar() {
        assert_eq!(bar(None, 24), format!("▕{}▏", "░".repeat(24)));
        assert_eq!(bar(Some(0.0), 24), format!("▕{}▏", "░".repeat(24)));
        assert_eq!(bar(Some(100.0), 24), format!("▕{}▏", "█".repeat(24)));
        assert_eq!(bar(Some(50.0), 24), format!("▕{}{}▏", "█".repeat(12), "░".repeat(12)));
        assert_eq!(bar(Some(50.0), 10), format!("▕{}{}▏", "█".repeat(5), "░".repeat(5)));
    }

    #[test]
    fn test_ver_tuple() {
        assert_eq!(ver_tuple("2.10.1"), vec![2, 10, 1]);
        assert_eq!(ver_tuple("0.0.0"), vec![0, 0, 0]);
        assert_eq!(ver_tuple(""), Vec::<u64>::new());
        assert_eq!(ver_tuple("1.2.x"), Vec::<u64>::new());
    }

    #[test]
    fn test_remote_is_newer() {
        assert!(remote_is_newer("2.10.2", "2.10.1"));
        assert!(remote_is_newer("2.11.0", "2.10.1"));
        assert!(remote_is_newer("3.0.0", "2.10.1"));
        assert!(!remote_is_newer("2.10.1", "2.10.1"));
        assert!(!remote_is_newer("2.10.0", "2.10.1"));
        assert!(!remote_is_newer("2.9.9", "2.10.1"));
        assert!(!remote_is_newer("", "2.10.1"));
    }

    #[test]
    fn test_status_level() {
        for s in ["auth", "cookie", "cloudflare", "schema"] {
            assert_eq!(status_level(s), "critical");
        }
        for s in ["network", "http", "ok", "init", "whatever"] {
            assert_eq!(status_level(s), "normal");
        }
    }

    #[test]
    fn test_should_notify_bad() {
        assert!(!should_notify_bad(1, "cloudflare", "", 0.0, 1800.0));
        assert!(!should_notify_bad(0, "cloudflare", "", 9999.0, 1800.0));
        assert!(should_notify_bad(2, "cloudflare", "", 0.0, 1800.0));
        assert!(!should_notify_bad(5, "cloudflare", "cloudflare", 10.0, 1800.0));
        assert!(should_notify_bad(5, "cloudflare", "cloudflare", 1801.0, 1800.0));
        assert!(should_notify_bad(3, "cloudflare", "auth", 5.0, 1800.0));
    }

    #[test]
    fn test_parse_iso() {
        assert_eq!(parse_iso(None), None);
        assert_eq!(parse_iso(Some("")), None);
        assert_eq!(parse_iso(Some("garbage")), None);
        let dt = parse_iso(Some("2025-01-02T03:04:05Z")).unwrap();
        assert_eq!(dt.format("%Y-%m-%d %H").to_string(), "2025-01-02 03");
        assert!(parse_iso(Some("2025-01-02T03:04:05")).is_some());
    }

    #[test]
    fn test_fmt_countdown_edges() {
        assert_eq!(fmt_countdown(None), "--");
        assert_eq!(fmt_countdown(Some(Utc::now() - Duration::hours(1))), "0m");
        let s = fmt_countdown(Some(Utc::now() + Duration::minutes(124)));
        assert!(s.ends_with('m') && s.contains('h'));
    }

    #[test]
    fn test_json_to_raw() {
        let j = serde_json::json!({
            "five_hour": {"utilization": 39, "resets_at": "2025-01-02T03:04:05Z"},
            "seven_day": {"utilization": 5, "resets_at": "2025-01-06T07:00:00Z"},
            "seven_day_sonnet": {"utilization": 12},
            "seven_day_opus": {"utilization": 0}
        });
        let raw = json_to_raw(&j);
        assert_eq!(raw.five_hour_util, Some(39.0));
        assert_eq!(raw.seven_day_util, Some(5.0));
        assert_eq!(raw.sonnet_util, Some(12.0));
        assert_eq!(raw.opus_util, Some(0.0));
        assert!(raw.five_hour_reset.is_some());
        assert_eq!(raw.opus_reset, None);
        assert_eq!(json_to_raw(&serde_json::json!({})).five_hour_util, None);
    }

    #[test]
    fn test_validate_and_extract() {
        let ok = serde_json::json!({"five_hour": {"utilization": 50}, "seven_day": {"utilization": 10}});
        assert_eq!(validate_and_extract(&ok).unwrap().five_hour_util, Some(50.0));
        assert!(validate_and_extract(&serde_json::json!(["nope"])).is_err());
        assert!(validate_and_extract(&serde_json::json!({"five_hour": {"utilization": 1}})).is_err());
        assert!(validate_and_extract(
            &serde_json::json!({"five_hour": {"utilization": "x"}, "seven_day": {"utilization": 1}})
        )
        .is_err());
    }
}
