"""云上启动入口 —— 发布为在线应用时用这个。

⚠️ 为什么不写 `uvicorn ... --port $PORT`：
   发布侧的启动命令**不经 shell 展开**，`$PORT` 会变成空串，
   uvicorn 抛 "Error: Option '--port' requires an argument."，服务起不来、
   60 秒后判定"不可达"——看起来像端口问题，其实是展开问题。踩过一次。
   所以改成在 Python 里读环境变量，读不到就回落到发布侧默认端口 3000。

本机**不要**用这个 —— 本机用 scripts/serve.py（它还要判定端口占用、挑局域网地址、
按需关掉口令门）。这个只服务于"整个项目发到云上"那一条路。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    import uvicorn

    raw = (os.environ.get("PORT") or "").strip()
    port = int(raw) if raw.isdigit() else 3000
    print(f"[cloud] 监听 0.0.0.0:{port}（PORT 环境变量={raw!r}）", flush=True)
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")
