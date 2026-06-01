# Claude Usage Indicator

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

---

## 前置条件

- **Debian / Ubuntu**（用 apt 装系统依赖）
- 在 **Chrome** 里登录过 `https://claude.ai`（无需保持标签页常开；甚至 Chrome 不开也行，只要 cookie 没过期）
- 一个带系统托盘的桌面环境。**GNOME 默认不显示托盘图标**，需要 AppIndicator 扩展：

  ```bash
  sudo apt-get install gnome-shell-extension-appindicator
  gnome-extensions enable ubuntu-appindicators@ubuntu.com \
    || gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
  ```

  装完注销重登一次。**Ubuntu 通常已默认启用，无需操作。**

---

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/install.sh | bash
```

安装脚本会：装系统依赖（需要 sudo）→ 拉代码到 `~/.local/share/claude-usage-indicator` → 建独立 venv 装 Python 依赖 → 注册并启动 systemd 用户服务 → 安装 `claude-usage-indicator` 命令。

约 30 秒后顶栏开始显示用量。

## 卸载

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash
```

彻底清除（连配置目录 `config.json` 一起删，注意管道传参要用 `bash -s --`）：

```bash
curl -fsSL https://raw.githubusercontent.com/muyangli-byte/claude-usage-indicator/main/uninstall.sh | bash -s -- --purge
```

诊断数据在安装目录内，默认就会删；系统库默认保留（可能被别的程序使用）。

## 更新

```bash
claude-usage-indicator --update      # 拉取最新版并重启
claude-usage-indicator --check       # 只检查有没有新版
```

工具每天自动比对仓库版本，有新版会在托盘和桌面通知里提示（**只通知，不自动更新**）。

---

## 托盘菜单

| 项 | 说明 |
|---|---|
| Current session / All models / Sonnet / Opus | 各档用量百分比与重置时间 |
| Status | 当前状态（ok / 登录过期 / 接口变了 …） |
| Refresh now | 立即拉取一次 |
| Check for updates | 立即检查新版本 |
| Open usage page | 打开 claude.ai 用量页 |

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
| Cloudflare 拦截 (`cloudflare`) | TLS 伪装失效 | 多半需要更新本工具：`--update`；原始响应见下方 diagnostics |
| 接口结构变了 (`schema`) | 用量接口字段变化 | 需要更新本工具；原始响应已存盘，可据此修脚本 |
| 读 cookie 失败 (`cookie`) | 无法解密 Chrome cookie | 确认已登录、keyring 已解锁 |
| 网络/HTTP 错误 | 临时问题 | 会自动重试 |

异常响应会保存到 `~/.local/share/claude-usage-indicator/diagnostics/`（仅保留最近 20 份），方便定位或提交 issue。

查看日志：

```bash
journalctl --user -u claude-usage-indicator.service -f
```

---

## 隐私与安全

- `sessionKey` 只在内存中使用，**本工具不会把它写入任何文件**。
- 不上报任何遥测。日常用量请求只发往 `claude.ai`；每日更新检查会拉取本仓库的 `VERSION`；`claude-usage-indicator --update` 会从 GitHub 重新下载安装脚本、并从 PyPI 安装依赖。

## 局限

- claude.ai 的「Daily included routine runs」「套餐名(Team)」不在用量接口里，本工具不显示。
- 目前只覆盖 Debian/Ubuntu。

## License

[MIT](LICENSE)
