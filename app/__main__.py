"""服务入口：python -m app [--host H] [--port P]"""

from __future__ import annotations

import argparse

from .service import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="具身智能实验资源排程服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = build_server(args.host, args.port)
    print(f"scheduler listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
