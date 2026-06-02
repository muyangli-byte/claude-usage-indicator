# Claude Usage Indicator

**中文** · [English](#english)

在 Linux 顶栏（系统托盘）实时显示你的 **claude.ai 用量**：当前会话（5 小时窗口）、本周限额（All models / Sonnet / Opus）以及各自的重置时间。

```
Cur 39% 2h56m | All 5% Mon 7am
```

- ✅ **不用开网页、不用 Tampermonkey、不用任何浏览器插件**
- ✅ 一个后台 Python 进程，开机自启
- ✅ 自动从 Chrome 读取登录态，几乎零维护

---

## 工作原理

1. 用 [`browser_cookie3`](https://github.com/borisbabic/browser_cookie3) 从 Chrome 的 cookie 库自动读取 `sessionKey`
2. 用 [`curl_cffi`](https://github.com/lexiforest/curl_cffi) 伪装 Chrome 的 TLS 指纹，直接请求 claude.ai 的内部用量接口
   （普通 `requests`/`curl` 会被 Cloudflare 在 TLS 指纹层拦截，必须伪装）
3. 解析返回的 JSON，更新 GTK AppIndicator 顶栏

因为读的是 **JSON 接口**而不是抓网页 DOM，所以 claude.ai 改版网页不影响本工具。

### 刷新频率（自适应）

claude.ai 没有推送通道，只能轮询。因此本工具**自适应调节频率**：

- 你**在用 Claude、数据在变**时，快轮询（约每 **5s**）≈ 准实时跟随
- 长时间**无变化**时自动**退避**（10→20→…→90s 封顶），少打接口
- 检测到数据变化立刻回到快轮询；菜单「Refresh now」可手动立即拉取
- 当前会话的重置显示**还剩多久**（如 `2h3m`），由接口**真实的 `resets_at`** 减去当前时间算得（每秒自然倒数）；周限显示绝对重置时刻（`Mon 7am`）

### 健康监测（心跳）

每次轮询都是一次健康检查，出问题会弹**桌面通知**并在顶栏标 `⚠`：

- **接口结构变化**（`schema`）：字段缺失/类型变了 → 提醒「接口结构变了，需要更新」，并把原始响应存到 `diagnostics/`
- **被 Cloudflare 拦截 / 登录过期 / 网络错误**：各自对应提示
- 进入异常**立刻**提醒；持续异常**每 30 分钟**再提醒一次（避免错过）

---

## 前置条件

- **Debian / Ubuntu**（用 apt 装系统依赖）
- 在 **Chrome / Chromium / Brave / Edge** 任一里登录过 `https://claude.ai`（无需常开标签页；浏览器不开也行，只要 cookie 没过期）
- 登录 keyring 已解锁（用密码登录的桌面会自动解锁；自动登录或无密码 keyring 时可能读不到 cookie，会显示 `cookie read failed`）
- 一个带系统托盘的桌面环境。**GNOME 默认不显示托盘图标**，需要 AppIndicator 扩展：

  ```bash
  sudo apt-get install gnome-shell-extension-appindicator
  gnome-extensions enable ubuntu-appindicators@ubuntu.com \
    || gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
  ```

  装完注销重登一次。**Ubuntu 通常已默认启用，无需操作。**

---

## 安装

### 方式一：一行命令（最快）

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/install.sh | bash
```

### 方式二：git clone 后安装（想先看代码就用这个，也更稳）

```bash
git clone https://github.com/muyangli-byte/claude-usage-indicator.git
cd claude-usage-indicator
less install.sh        # 可选：先看脚本干了啥再决定运行
./install.sh           # 等价于 bash install.sh，会提示输一次 sudo 密码
```

> 从**本地文件**运行比 `curl | bash` 更稳：不会触发「管道被 apt 读走 stdin 导致脚本截断」的问题。

两种方式下 `install.sh` 都会：装系统依赖（需要 sudo）→ 把**最新 `main`** 部署到 `~/.local/share/claude-usage-indicator` → 建独立 venv 装 Python 依赖 → 注册并启动 systemd 用户服务 → 安装 `claude-usage-indicator` 命令。

> `install.sh` 总是部署 GitHub 上的最新 `main`，所以 clone 主要用于查看代码；实际运行副本在 `~/.local/share/...`，与你的 clone 目录互不影响。

装好后**在 Chrome 登录 `claude.ai`**（无需常开标签页），几秒内顶栏即显示用量。可立即用 `claude-usage-indicator --once` 验证能否拉到数据。

## 卸载

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash
```

彻底清除（连配置目录 `config.json` 一起删，注意管道传参要用 `bash -s --`）：

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash -s -- --purge
```

若用 git clone 安装，也可在 clone 目录直接 `./uninstall.sh`（或 `./uninstall.sh --purge`）。

诊断数据在安装目录内，默认就会删；系统库默认保留（可能被别的程序使用）。

## 更新

**最省事：托盘菜单点「Update now」一键更新**（后台拉取最新代码 + 依赖并自动重启，无需终端、无需 sudo）。

或在终端：

```bash
claude-usage-indicator --self-update   # 轻量更新：git+pip+重启，无需 sudo（同「Update now」）
claude-usage-indicator --update        # 重跑安装脚本，连系统库一起更新（需 sudo）
claude-usage-indicator --check         # 只检查有没有新版
```

工具每天自动比对仓库版本，有新版会在托盘和桌面通知里提示，点「Update now」即可。一般用 `--self-update`/「Update now」即可；只有当某次更新需要新的系统库时才用 `--update`。

---

## 托盘菜单

| 项 | 说明 |
|---|---|
| Current session / All models / Sonnet / Opus | 各档用量百分比与重置时间 |
| Status | 当前状态（ok / 登录过期 / 接口变了 …） |
| Refresh now | 立即拉取一次 |
| Check for updates | 立即检查新版本 |
| Update now | **仅在 Check for updates 发现新版后才出现**；点它一键更新并自动重启（无需 sudo）|
| Open usage page | 打开 claude.ai 用量页 |
| Notification language | 切换桌面**通知**语言（中文 / English）；菜单本身始终英文 |
| Updated | 最近一次成功拉取的时刻与距今多久 |
| Quit (vX.Y.Z) | 退出；这里也显示当前运行的版本号 |

## 命令行

```bash
claude-usage-indicator --once     # 拉取一次并打印（调试）
claude-usage-indicator --version
```

---

## 故障排查

顶栏出现 `⚠` 时，看托盘的 **Status** 行或桌面通知：

| 状态 | 含义 | 怎么办 |
|---|---|---|
| 登录已过期 (`auth`) | sessionKey 失效 | 在 Chrome 重新登录 claude.ai，自动恢复 |
| Cloudflare 拦截 (`cloudflare`) | TLS 伪装失效 | 多半需要更新：托盘 Update now / `--self-update`（只在需要新系统库时才用 `--update`）|
| 接口结构变了 (`schema`) | 用量接口字段变化 | 需要更新本工具；原始响应已存盘，可据此修脚本 |
| 读 cookie 失败 (`cookie`) | 无法解密浏览器 cookie | 确认已登录、keyring 已解锁 |
| 找不到 org id | 没有 lastActiveOrg cookie | 在 `~/.config/claude-usage-indicator/config.json` 里设 `{"org_id": "你的org-uuid"}` |
| 网络/HTTP 错误 | 临时问题 | 会自动重试 |

异常响应会保存到 `~/.local/share/claude-usage-indicator/diagnostics/`（仅保留最近 20 份），方便定位或提交 issue。

**已登录却一直 `login expired`**（尤其 KDE，弹出 KWallet 让你设密码）：浏览器 cookie 的加密 key 存在系统钥匙环里（GNOME keyring / KDE KWallet），工具解不开就拿不到有效 sessionKey。两个办法：

- **最稳——手动填凭证绕过钥匙环**：浏览器打开已登录的 claude.ai → F12 → Application/存储 → Cookies → `https://claude.ai`，复制 `sessionKey` 和 `lastActiveOrg` 两个值，写进 `~/.config/claude-usage-indicator/config.json`：

  ```json
  {"session_key": "sk-ant-sid02-…", "org_id": "<lastActiveOrg 的值>"}
  ```

  然后 `systemctl --user restart claude-usage-indicator.service`。config.json 里填了就优先用、完全不碰钥匙环。（注意 sessionKey 会过期，过期后重新复制一次。）
- 或者**设置并解锁你的钥匙环**（KDE 用户：创建并解锁 KWallet 钱包），让工具能自动解密浏览器 cookie，免去手动维护。

查看日志：

```bash
journalctl --user -u claude-usage-indicator.service -f
```

---

## 隐私与安全

- `sessionKey` 只在内存中使用，**本工具不会把它写入任何文件**。
- 不上报任何遥测。日常用量请求只发往 `claude.ai`；每日更新检查会拉取本仓库的 `VERSION`；`claude-usage-indicator --update` 会从 GitHub 重新下载安装脚本、并从 PyPI 安装依赖。
- 安装与更新沿用标准的 `curl | bash` 信任模型：你信任本仓库（GitHub）与 PyPI 不投放恶意代码；更新跟随 `main` 且不做签名校验。在意的话用 git clone 路径、先 review 再装/更。

## 局限

- claude.ai 的「Daily included routine runs」「套餐名(Team)」不在用量接口里，本工具不显示。
- 目前只覆盖 Debian/Ubuntu。

## License

[MIT](LICENSE)

<br>

---

<a id="english"></a>

# Claude Usage Indicator — English

[中文](#claude-usage-indicator) · **English**

Show your **claude.ai usage** live in the Linux top bar (system tray): current session (5‑hour window), weekly limits (All models / Sonnet / Opus), and each one's reset time.

```
Cur 39% 2h56m | All 5% Mon 7am
```

- ✅ **No open web page, no Tampermonkey, no browser extension**
- ✅ A single background Python process, auto‑starts on login
- ✅ Reads your login from Chrome automatically — virtually zero maintenance

## How it works

1. Use [`browser_cookie3`](https://github.com/borisbabic/browser_cookie3) to read `sessionKey` from Chrome's cookie store automatically.
2. Use [`curl_cffi`](https://github.com/lexiforest/curl_cffi) to impersonate Chrome's TLS fingerprint and call claude.ai's internal usage API directly
   (plain `requests`/`curl` get blocked by Cloudflare at the TLS‑fingerprint layer, so impersonation is required).
3. Parse the returned JSON and update the GTK AppIndicator in the top bar.

Because it reads a **JSON API** rather than scraping the page DOM, claude.ai redesigning its web UI doesn't affect this tool.

### Refresh rate (adaptive)

claude.ai has no push channel, so we must poll. The tool **adapts its frequency**:

- While you're **using Claude and the numbers change**, it polls fast (~every **5s**) ≈ near‑real‑time.
- After a long period with **no change**, it **backs off** (10→20→…→90s cap) to spare the API.
- On any change it snaps back to fast polling; the **Refresh now** menu item forces an immediate poll.
- The current‑session reset shows **time remaining** (e.g. `2h3m`), computed from the API's **real `resets_at`** minus the current time (counts down every second); the weekly reset shows the absolute reset time (`Mon 7am`).

### Health monitoring (heartbeat)

Every poll is a health check. On a problem it pops a **desktop notification** and marks the top bar with `⚠`:

- **API schema change** (`schema`): a missing/retyped field → "API schema changed, needs an update", and the raw response is saved to `diagnostics/`.
- **Blocked by Cloudflare / login expired / network error**: each has its own message.
- Notifies **immediately** on entering a bad state; while a problem persists it re‑notifies **every 30 minutes** (so you don't miss it).

## Prerequisites

- **Debian / Ubuntu** (system deps installed via apt)
- Logged into `https://claude.ai` in **Chrome / Chromium / Brave / Edge** (no need to keep a tab open; the browser doesn't even need to be running, as long as the cookie hasn't expired)
- The login keyring is unlocked (password logins auto-unlock it; auto-login / passwordless-keyring setups may prevent reading the cookie, shown as `cookie read failed`)
- A desktop with a system tray. **GNOME doesn't show tray icons by default** and needs the AppIndicator extension:

  ```bash
  sudo apt-get install gnome-shell-extension-appindicator
  gnome-extensions enable ubuntu-appindicators@ubuntu.com \
    || gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
  ```

  Log out and back in afterwards. **Ubuntu usually has it enabled already — no action needed.**

## Install

### Option 1: one‑liner (fastest)

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/install.sh | bash
```

### Option 2: git clone, then install (use this if you want to read the code first — also more robust)

```bash
git clone https://github.com/muyangli-byte/claude-usage-indicator.git
cd claude-usage-indicator
less install.sh        # optional: inspect the script before running it
./install.sh           # same as `bash install.sh`; prompts for sudo password once
```

> Running from a **local file** is more robust than `curl | bash`: it can't hit the "apt reads the piped script's stdin and truncates it" problem.

Either way, `install.sh` will: install system deps (needs sudo) → deploy the **latest `main`** to `~/.local/share/claude-usage-indicator` → create an isolated venv and install Python deps → register and start a systemd user service → install the `claude-usage-indicator` command.

> `install.sh` always deploys the latest `main` from GitHub, so the clone is mainly for reading the code; the actual running copy lives in `~/.local/share/...`, independent of your clone.

After installing, **log into `claude.ai` in Chrome** (no need to keep a tab open). The top bar shows usage within seconds. Verify a fetch immediately with `claude-usage-indicator --once`.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash
```

To wipe everything (including the config dir `config.json`; note piping args needs `bash -s --`):

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash -s -- --purge
```

If you installed via git clone, you can also run `./uninstall.sh` (or `./uninstall.sh --purge`) from the clone.

Diagnostics live inside the install dir and are removed by default; system libraries are kept by default (they may be used by other programs).

## Update

**Easiest: click "Update now" in the tray menu** (pulls the latest code + deps in the background and restarts automatically — no terminal, no sudo).

Or in a terminal:

```bash
claude-usage-indicator --self-update   # lightweight: git+pip+restart, no sudo (same as "Update now")
claude-usage-indicator --update        # re-run the installer, updating system libs too (needs sudo)
claude-usage-indicator --check         # just check whether a new version exists
```

The tool compares versions against the repo daily; when a new version exists it's shown in the tray and a desktop notification — just click "Update now". Use `--self-update`/"Update now" normally; only use `--update` when an update needs new system libraries.

## Tray menu

| Item | Description |
|---|---|
| Current session / All models / Sonnet / Opus | Usage percentage and reset time for each |
| Status | Current status (ok / login expired / schema changed …) |
| Refresh now | Fetch once immediately |
| Check for updates | Check for a new version now |
| Update now | **Only appears after Check for updates finds a newer version**; click it to update and auto‑restart (no sudo) |
| Open usage page | Open the claude.ai usage page |
| Notification language | Switch the desktop **notification** language (中文 / English); the menu itself stays English |
| Updated | Time of the last successful fetch and how long ago |
| Quit (vX.Y.Z) | Exit; also shows the running version |

## Command line

```bash
claude-usage-indicator --once     # fetch once and print (debug)
claude-usage-indicator --version
```

## Troubleshooting

When the top bar shows `⚠`, check the **Status** line in the tray or the desktop notification:

| Status | Meaning | What to do |
|---|---|---|
| login expired (`auth`) | sessionKey invalid | Re‑login to claude.ai in Chrome; recovers automatically |
| Cloudflare blocked (`cloudflare`) | TLS impersonation broke | Usually needs an update: tray "Update now" / `--self-update` (use `--update` only if new system libs are needed) |
| schema changed (`schema`) | usage API fields changed | Needs a tool update; the raw response is saved so the parser can be fixed |
| cookie read failed (`cookie`) | can't decrypt browser cookies | Make sure you're logged in and the keyring is unlocked |
| org id not found | no lastActiveOrg cookie | Set `{"org_id": "your-org-uuid"}` in `~/.config/claude-usage-indicator/config.json` |
| network / HTTP error | transient | Retries automatically |

Failed responses are saved to `~/.local/share/claude-usage-indicator/diagnostics/` (last 20 only) to help diagnose or file an issue.

**Persistent `login expired` even though you're logged in** (especially on KDE, where KWallet pops up asking you to set a password): the browser's cookie‑encryption key lives in the system keyring (GNOME keyring / KDE KWallet); if the tool can't read it, it can't decrypt a valid sessionKey. Two options:

- **Most reliable — set credentials manually, bypassing the keyring**: open the logged‑in claude.ai → F12 → Application/Storage → Cookies → `https://claude.ai`, copy the `sessionKey` and `lastActiveOrg` values into `~/.config/claude-usage-indicator/config.json`:

  ```json
  {"session_key": "sk-ant-sid02-…", "org_id": "<lastActiveOrg value>"}
  ```

  then `systemctl --user restart claude-usage-indicator.service`. When config.json has them, they take precedence and the keyring/browser is never touched. (Note: the sessionKey expires; re-copy it when it does.)
- Or **set up and unlock your keyring** (KDE: create and unlock the KWallet wallet) so the tool can decrypt browser cookies automatically.

View logs:

```bash
journalctl --user -u claude-usage-indicator.service -f
```

## Privacy & security

- `sessionKey` is only used in memory; **the tool never writes it to any file**.
- No telemetry. Usage requests go only to `claude.ai`; the daily update check fetches this repo's `VERSION`; `claude-usage-indicator --update` re‑downloads the installer from GitHub and installs deps from PyPI.
- Install and updates use the standard `curl | bash` trust model: you trust this repo (GitHub) and PyPI not to ship malicious code; updates track `main` and are not signature‑verified. If that matters to you, use the git‑clone path and review before installing/updating.

## Limitations

- claude.ai's "Daily included routine runs" and plan name (e.g. Team) aren't in the usage API, so this tool doesn't show them.
- Currently Debian/Ubuntu only.

## License

[MIT](LICENSE)
