from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import webbrowser

from scripts.extractor import extract
from scripts.pairing import load_or_create, persist
from scripts.uploader import upload

BASE = os.environ.get("CONDUCTORSCORE_BASE_URL", "https://conductorscore.com")
CLIENT_VERSION = "0.1.0"


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


def do_upload() -> int:
    state = load_or_create()
    if not state.paired or not state.pairing_token:
        print(
            "Not paired. Run `/conductorscore pair` first.",
            file=sys.stderr,
        )
        return 1
    out = extract(
        device_id=state.client_device_id,
        client_version=CLIENT_VERSION,
    )
    try:
        upload(out, token=state.pairing_token, base_url=BASE)
    except PermissionError as e:
        print(
            f"Unauthorized: {e}. Run `/conductorscore pair` to re-pair.",
            file=sys.stderr,
        )
        return 2
    except ValueError as e:
        print(f"Server rejected payload: {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        print(f"Upload failed: {e}", file=sys.stderr)
        return 4
    state.last_upload_ms = int(time.time() * 1000)
    persist(state)
    print(
        f"Uploaded {len(out.sessions)} sessions; "
        "visit https://conductorscore.com/dashboard to see your score."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="conductorscore")
    parser.add_argument(
        "command", nargs="?", choices=["pair", "upload"], default="upload"
    )
    args = parser.parse_args()
    if args.command == "pair":
        return do_pair()
    return do_upload()


if __name__ == "__main__":
    sys.exit(main())
