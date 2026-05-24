from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import webbrowser

from scripts.pairing import load_or_create, persist

BASE = os.environ.get("CONDUCTORSCORE_BASE_URL", "https://conductorscore.com")


def do_pair() -> int:
    state = load_or_create()
    if state.paired:
        print("Already paired; run without `pair` to upload.")
        return 0
    # Register device
    req = urllib.request.Request(
        f"{BASE}/api/pair/start",
        data=json.dumps({"client_device_id": state.client_device_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req).read()
    except Exception as e:
        print(f"Failed to start pairing: {e}", file=sys.stderr)
        return 1
    url = f"{BASE}/pair?device={state.client_device_id}"
    print(f"Open this URL in your browser to pair:\n  {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    # Poll for completion
    for _ in range(120):  # 2 minutes
        time.sleep(2)
        try:
            with urllib.request.urlopen(
                f"{BASE}/api/pair/status?device={state.client_device_id}"
            ) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue
        if data.get("paired"):
            state.paired = True
            state.pairing_token = data["token"]
            persist(state)
            print(
                "Paired. You can now run /conductorscore to upload your first score."
            )
            return 0
    print("Pairing timed out after 2 minutes. Try again.", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="conductorscore")
    parser.add_argument(
        "command", nargs="?", choices=["pair", "upload"], default="upload"
    )
    args = parser.parse_args()
    if args.command == "pair":
        return do_pair()
    print("upload command not yet implemented (Feature 3)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
