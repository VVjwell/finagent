"""`python -m server` 启动入口。开发用，正式部署用 uvicorn / gunicorn 直接拉 server.main:app。"""

from __future__ import annotations

import argparse


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="finAgent web server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="dev 热重载")
    args = p.parse_args()

    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
