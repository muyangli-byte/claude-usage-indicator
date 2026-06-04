"""Claude 用量顶栏指示器（纯 Python，方案 B）。

包结构（按依赖方向分层）：
  config       常量 / 路径 / 版本 / IS_DEV / 配置读写 / 语言 / 通知文案    —— 无内部依赖
  model        UsageData / UsageStore / 纯格式化（_bar/_fmt_*）/ json 解析  —— 无内部依赖
  api          curl_cffi 拉取 / 错误分类 / 诊断转储 / 版本检查              —— 依赖 model
  credentials  browser_cookie3 / KWallet 解密 / load_credentials           —— 依赖 config
  poller       自适应轮询线程 + ntfy 订阅                                  —— 依赖 api/credentials/model
  tray         GTK AppIndicator 顶栏（build_app）                          —— 依赖 model/api/config
  cli          命令行子命令 + run_gui + main                               —— 依赖以上全部

原理与刷新策略详见 cli.main / poller。运行入口是仓库根目录的 claude_usage_indicator.py。
"""
