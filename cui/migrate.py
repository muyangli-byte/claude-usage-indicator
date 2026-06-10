"""Python → Rust 迁移钩子（载体在 Python 桥接版里，对齐 rust/MIGRATION_PLAN.md §5）。

由启动钩子在 Python 托盘已注册之后、以分离进程（systemd-run --collect）调用，所以探测期间
用户始终有一个可用托盘。三条铁律：
  ① 预检只验「Rust 二进制能在本机跑」（下载→校验 SHA256→chmod→`cui --version` rc0），
     绝不拿 creds / 网络 / 登录态当门槛（那些常在登录时失败，且 Python 版有同样依赖）；
  ② manifest 取不到 → 前向迁移 fail-CLOSED（不迁，下次再说），kill-switch 永不会被取数失败绕过；
  ③ 保留 Python 树原封不动作回退；二进制跑不起来就绝不写哨兵 / 绝不切过去。
默认 DRY-RUN（只打印决策、不动服务）；正式启动钩子用 `--commit` 调用才真换栈。
"""
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OWNER, REPO = "muyangli-byte", "claude-usage-indicator"
SERVICE = "claude-usage-indicator.service"

HOME = Path.home()
BIN_DIR = HOME / ".local/share/claude-usage-indicator-bin"   # git 树之外
CUI_BIN = BIN_DIR / "cui"
CUI_NEW = BIN_DIR / "cui.new"
SENTINEL = HOME / ".config/claude-usage-indicator" / "use-rust"
STATE_DIR = HOME / ".local/state/claude-usage-indicator"     # 锁 / 标记 / 日志，git 树之外
LOCK = STATE_DIR / "migrate.lock"
LOG = STATE_DIR / "migrate.log"
PIN = STATE_DIR / "pinned-python"   # 看门狗回退后写它；带时间戳，时间盒内不再尝试迁移
PIN_COOLDOWN_S = 6 * 3600

MANIFEST_URL = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/main/migration.json"
UA = {"User-Agent": "claude-usage-indicator/migrate"}


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def _get(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def machine_bucket() -> int:
    """0..99，按稳定的每机种子哈希分桶；/etc/machine-id 缺失则用持久化的随机 id。"""
    seed = ""
    try:
        seed = Path("/etc/machine-id").read_text().strip()
    except OSError:
        pass
    if not seed or set(seed) <= {"0"}:
        idf = STATE_DIR / "install-id"
        try:
            seed = idf.read_text().strip()
        except OSError:
            seed = ""
        if not seed:
            seed = os.urandom(16).hex()
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                idf.write_text(seed)
            except OSError:
                pass
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 100


def already_rust() -> bool:
    if not SENTINEL.exists() or not os.access(CUI_BIN, os.X_OK):
        return False
    try:
        return subprocess.run([str(CUI_BIN), "--version"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def pinned() -> bool:
    """看门狗在回退后写 PIN（时间盒）；冷却期内不再尝试迁移，避免反复抖动。"""
    try:
        ts = float(PIN.read_text().strip())
        return (time.time() - ts) < PIN_COOLDOWN_S
    except (OSError, ValueError):
        return False


def fetch_manifest():
    override = os.environ.get("CUI_MIGRATE_MANIFEST")  # 测试/本地覆盖：读本地 json 文件
    if override:
        try:
            return json.loads(Path(override).read_text())
        except Exception as e:
            log(f"manifest override unreadable ({type(e).__name__}) — fail-closed")
            return None
    try:
        return json.loads(_get(MANIFEST_URL))
    except Exception as e:
        log(f"manifest unreachable ({type(e).__name__}) — fail-closed, stay Python")
        return None


def preflight(arch: str) -> bool:
    """下载本架构二进制到 cui.new、校验 SHA256、chmod、跑 `--version`。证明它能在本机跑。"""
    asset = f"https://github.com/{OWNER}/{REPO}/releases/latest/download/cui-{arch}-linux"
    try:
        data = _get(asset, timeout=60)
    except Exception as e:
        log(f"preflight: asset download failed ({type(e).__name__}) — stay Python")
        return False
    if len(data) < 1_000_000:
        log(f"preflight: asset too small ({len(data)} B) — stay Python")
        return False
    # SHA256（fail-closed）
    try:
        sha_txt = _get(asset + ".sha256", timeout=20).decode()
        expected = sha_txt.split()[0].lower()
        actual = hashlib.sha256(data).hexdigest()
        if not expected or actual != expected:
            log(f"preflight: sha256 mismatch (want {expected[:12]}…, got {actual[:12]}…) — refuse")
            return False
    except Exception as e:
        log(f"preflight: checksum unavailable ({type(e).__name__}) — refuse (fail-closed)")
        return False
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    CUI_NEW.write_bytes(data)
    CUI_NEW.chmod(0o755)
    try:
        r = subprocess.run([str(CUI_NEW), "--version"], capture_output=True, timeout=15, text=True)
    except Exception as e:
        log(f"preflight: binary won't run ({type(e).__name__}) — stay Python")
        _rm(CUI_NEW)
        return False
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        log(f"preflight: `cui --version` rc={r.returncode} — stay Python")
        _rm(CUI_NEW)
        return False
    if "rust-dev" in out:
        log("preflight: binary is a DEV build (contains rust-dev) — refuse")
        _rm(CUI_NEW)
        return False
    log(f"preflight OK: {out.strip()} (sha256 verified)")
    return True


def sni_watcher_present() -> bool:
    """咨询性：会话总线上有没有 StatusNotifierWatcher（真正渲染托盘的东西）。仅记录，不当硬门——
    没有 watcher 时 Python 托盘同样不可见，换栈不构成回退。"""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus", "NameHasOwner",
                            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                            None, Gio.DBusCallFlags.NONE, 2000, None)
        return bool(res.unpack()[0])
    except Exception:
        return False


def do_swap(dry: bool) -> int:
    if dry:
        log("DRY-RUN: would now: disable --now Python service → mv cui.new→cui → write sentinel → "
            "enable --now (run.sh execs Rust). Python tree kept for rollback.")
        _rm(CUI_NEW)
        return 0
    log("swap: disabling Python service (stop+disable so Restart=always can't relaunch)…")
    subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE], capture_output=True)
    for _ in range(20):   # 等进程真正退出
        if subprocess.run(["systemctl", "--user", "is-active", SERVICE],
                          capture_output=True).returncode != 0:
            break
        time.sleep(0.3)
    os.replace(CUI_NEW, CUI_BIN)            # 同盘原子
    SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    SENTINEL.write_text(f"migrated {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE], capture_output=True)
    log("swap done: sentinel written, service re-enabled → run.sh now execs Rust. Python tree intact.")
    return 0


def _rm(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def main() -> int:
    commit = "--commit" in sys.argv[1:]
    dry = not commit
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # 互斥：整个迁移用 flock 包住，禁止并发/重入
    import fcntl
    lockf = open(LOCK, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another migrate is running — exit")
        return 0

    log(f"migrate start (mode={'COMMIT' if commit else 'dry-run'})")
    if already_rust():
        log("already on Rust — nothing to do")
        return 0
    if pinned():
        log("pinned to Python (watchdog cooldown) — skip")
        return 0
    m = fetch_manifest()
    if m is None:
        return 0                                  # fail-closed
    if m.get("rollback_all"):
        log("manifest rollback_all=true — kill-switch active, do not migrate")
        return 0
    arch = platform.machine()                     # x86_64 / aarch64 …
    allowed = m.get("arch", ["x86_64"])
    if arch not in allowed:
        log(f"arch {arch} not in {allowed} — excluded, stay Python")
        return 0
    bucket, pct = machine_bucket(), int(m.get("percent", 0))
    if bucket >= pct:
        log(f"bucket {bucket} >= rollout {pct}% — not my turn yet")
        return 0
    log(f"in canary (bucket {bucket} < {pct}%); running preflight…")
    if not preflight(arch):
        return 0
    log(f"SNI watcher present: {sni_watcher_present()} (advisory)")
    return do_swap(dry)


if __name__ == "__main__":
    sys.exit(main())
