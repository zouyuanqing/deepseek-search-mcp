#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口：老配置（command: python deepseek_web_search_mcp.py）无需改动即可使用新版本。
实际实现见 mcp_search 包（models / providers / fetch / server）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_search.server import main  # noqa: E402

if __name__ == "__main__":
    main()
