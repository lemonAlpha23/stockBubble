"""Vercel 的 Python 函数入口（serverless）。

本地跑的是 `uv run uvicorn stockbubble.api.asgi:app`；在 Vercel 上每个请求都会
冷启动一个函数实例，因此这里只做三件事：把 `src/` 加进 import 路径、把数据目录
钉成绝对路径、复用同一个 ASGI app。

注意：serverless 没有常驻进程，`TTLCache` 只在函数实例「活着」的几十秒内有效，
实例回收后第一次请求会重新去上游取数（加密 ~1.2s，美股 ~6s）。
"""

from __future__ import annotations

import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]

if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

# 默认值在 config.py 里是按「源码树」推导的；函数里显式钉住更稳。
os.environ.setdefault("BUBBLE_UNIVERSES_DIR", str(_ROOT / "config" / "universes"))
os.environ.setdefault("BUBBLE_WEB_DIR", str(_ROOT / "web"))
# 免费额度下不要装 openbb（两百多个包、单只票 1s），默认走 Yahoo 批量源。
os.environ.setdefault("BUBBLE_US_PROVIDER", "yahoo")

from stockbubble.api.asgi import app  # noqa: E402  (路径要先就位)

__all__ = ["app"]
