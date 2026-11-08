from __future__ import annotations

import argparse

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Pair a device and print its worker token.")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--name", required=True)
    parser.add_argument("--access-token", default="", help="server ACCESS_TOKEN")
    args = parser.parse_args()
    headers = {}
    if args.access_token:
        headers["Authorization"] = f"Bearer {args.access_token}"
    response = httpx.post(
        args.server.rstrip("/") + "/api/devices",
        json={"name": args.name},
        headers=headers,
        timeout=20,
    )
    if response.status_code >= 400:
        raise SystemExit(f"pairing failed: {response.status_code} {response.text[:300]}")
    data = response.json()
    device = data["device"]
    print(f"device: {device['name']} ({device['id']})")
    print(f"token:  {data['token']}")
    print()
    print("Run this on the device:")
    print(
        f'  python -m mvp.worker.agent --server {args.server} '
        f'--token {data["token"]} --name "{device["name"]}" '
        f'--workspace "C:\\\\path\\\\to\\\\workspace"'
    )


if __name__ == "__main__":
    main()
