#!/usr/bin/env bash
# 迁移回退看门狗（systemd --user timer 周期触发）。把已迁移到 Rust 的客户端在以下情况恢复到 Python：
#   (a) manifest kill-switch（rollback_all:true）；(b) Rust 服务进入 failed；(c) 服务 active 但心跳长时间不更新（卡死/崩溃循环）。
# 纯 bash —— 不依赖 Rust 二进制能跑，所以即便「坏掉的正是 Rust」也能恢复。恢复 = 删哨兵（run.sh 自动回落 Python）
# + 必要时重建 venv + 写时间盒 PIN（migrate.py 冷却期内不再尝试）+ 重启服务。
# 设 CUI_WATCHDOG_DRY=1 只打印决策、不动手（测试用）。
set -u
DRY="${CUI_WATCHDOG_DRY:-0}"
SENTINEL="${HOME}/.config/claude-usage-indicator/use-rust"
ALIVE="${HOME}/.cache/claude-usage-indicator/alive"
STATE_DIR="${HOME}/.local/state/claude-usage-indicator"
PIN="${STATE_DIR}/pinned-python"
INSTALL_DIR="${HOME}/.local/share/claude-usage-indicator"
SERVICE="claude-usage-indicator.service"
MANIFEST_URL="https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/migration.json"
STALE_S=300   # 心跳超过此秒数未更新 ⇒ 视为卡死（须 > 最大轮询间隔 90s + 余量）
LOG="${STATE_DIR}/watchdog.log"

log() { mkdir -p "$STATE_DIR"; echo "$(date '+%F %T')  $*" >> "$LOG"; }

# 只有已迁移到 Rust 才需要看门狗。
[ -f "$SENTINEL" ] || exit 0

reason=""
if command -v curl >/dev/null 2>&1 \
   && curl -fsS -m 8 "$MANIFEST_URL" 2>/dev/null | grep -q '"rollback_all"[[:space:]]*:[[:space:]]*true'; then
    reason="kill-switch"
fi
if [ -z "$reason" ] && systemctl --user is-failed "$SERVICE" >/dev/null 2>&1; then
    reason="service-failed"
fi
if [ -z "$reason" ] && systemctl --user is-active "$SERVICE" >/dev/null 2>&1; then
    now=$(date +%s); mt=$(stat -c %Y "$ALIVE" 2>/dev/null || echo 0)
    if [ ! -f "$ALIVE" ] || [ "$(( now - mt ))" -gt "$STALE_S" ]; then
        reason="heartbeat-stale"
    fi
fi
[ -n "$reason" ] || exit 0

if [ "$DRY" = "1" ]; then
    log "DRY-RUN: would restore Python (reason: $reason)"
    echo "DRY-RUN: would restore Python (reason: $reason)"
    exit 0
fi

log "restoring Python (reason: $reason)"
rm -f "$SENTINEL"                       # run.sh 立刻回落 Python
PY="${INSTALL_DIR}/venv/bin/python"
if ! "$PY" -c pass >/dev/null 2>&1; then  # Rust 期间 venv 可能因 OS 升级烂掉，重建
    log "venv broken — rebuilding"
    rm -rf "${INSTALL_DIR}/venv"
    /usr/bin/python3 -m venv "${INSTALL_DIR}/venv" >/dev/null 2>&1
    "${INSTALL_DIR}/venv/bin/pip" install -q --index-url https://pypi.org/simple \
        -r "${INSTALL_DIR}/requirements.txt" >/dev/null 2>&1
fi
date +%s > "$PIN"                       # 时间盒：migrate.py 冷却期内不再迁移
systemctl --user restart "$SERVICE"     # → run.sh → Python
log "restore done"
