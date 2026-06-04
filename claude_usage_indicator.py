#!/usr/bin/env python3
"""Claude 用量顶栏指示器 —— 入口薄壳。真正的代码在 cui/ 包里（见 cui/__init__.py）。

保持这个文件名不变：run.sh、systemd 服务、以及自更新/卸载命令都按
`python <安装目录>/claude_usage_indicator.py [args...]` 调用它。
"""
import os
import sys

# 兜底：确保能 import 同目录下的 cui 包（以脚本方式运行时 sys.path[0] 通常已是本目录，
# 这里再插一次，覆盖以异常 cwd 启动的情形）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cui.cli import main  # noqa: E402  (path 调整必须在 import 之前)

if __name__ == "__main__":
    main()
