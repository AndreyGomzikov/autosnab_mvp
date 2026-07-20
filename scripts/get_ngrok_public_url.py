#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import URLError
from urllib.request import urlopen


def _load_tunnels(api_base_url: str) -> dict:
    with urlopen(f"{api_base_url.rstrip('/')}/api/tunnels", timeout=5) as response:
        return json.load(response)


def _select_public_url(payload: dict, tunnel_name: str | None) -> str | None:
    tunnels = payload.get("tunnels") or []
    if tunnel_name:
        for tunnel in tunnels:
            if tunnel.get("name") == tunnel_name:
                return tunnel.get("public_url")

    https_tunnels = [
        tunnel.get("public_url")
        for tunnel in tunnels
        if tunnel.get("proto") == "https" and tunnel.get("public_url")
    ]
    if https_tunnels:
        return https_tunnels[0]

    for tunnel in tunnels:
        public_url = tunnel.get("public_url")
        if public_url:
            return public_url
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print the current ngrok public URL from the local inspection API."
    )
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:4040",
        help="ngrok local inspection API base URL",
    )
    parser.add_argument(
        "--tunnel-name",
        default=None,
        help="optional ngrok tunnel name to select explicitly",
    )
    args = parser.parse_args()

    try:
        payload = _load_tunnels(args.api_base_url)
    except URLError as exc:
        print(
            f"Could not reach ngrok local API at {args.api_base_url}: {exc}",
            file=sys.stderr,
        )
        return 1

    public_url = _select_public_url(payload, args.tunnel_name)
    if not public_url:
        print("No public ngrok tunnel is currently available.", file=sys.stderr)
        return 2

    print(public_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
