#!/usr/bin/env python3
"""Start the route mapper.

    python run.py                  # http://127.0.0.1:8000
    python run.py --port 9000
    python run.py --offline        # map and route only, no live data
    python run.py --host 0.0.0.0   # reachable from your phone on the same wifi

Binding to 127.0.0.1 by default is deliberate: the app has no authentication,
so it should not be reachable from the wider network unless you say so.
"""

from __future__ import annotations

import argparse
import os
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(description="Motorcycle route mapper")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: localhost only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--offline", action="store_true",
                        help="skip all live data requests")
    parser.add_argument("--reload", action="store_true",
                        help="restart on code changes (for development)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window on start")
    args = parser.parse_args()

    if args.offline:
        os.environ["MOTO_OFFLINE"] = "1"

    # Imported after the environment is set, so settings pick the flag up.
    import uvicorn

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}"
    print(f"Route mapper running at {url}")
    if args.host == "0.0.0.0":
        print("Listening on all interfaces — anyone on your network can reach this.")

    if not args.no_browser and not args.reload:
        webbrowser.open(url)

    uvicorn.run(
        "moto_route.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
