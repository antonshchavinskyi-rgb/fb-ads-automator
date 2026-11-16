"""Upload a local MP4 to Meta Ads and return its ID and preview image.

Requires requests. Set FB_ACCESS_TOKEN for unattended runs. An interactive
run can also prompt for the token without echoing or saving it.
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path

import requests


def graph_json(response, token):
    try:
        result = response.json()
    except ValueError:
        raise ValueError(f"Meta returned a non-JSON response (HTTP {response.status_code}).") from None
    if not response.ok or result.get("error"):
        error = result.get("error", {})
        message = str(error.get("message", "No error details")).replace(token, "[redacted]")
        raise ValueError(f"Meta HTTP {response.status_code}, code {error.get('code')}: {message}")
    return result


def process_video(session, version, token, video_id, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while True:
        response = session.get(
            f"https://graph.facebook.com/{version}/{video_id}",
            params={"fields": "status,picture"},
            timeout=15,
        )
        result = graph_json(response, token)
        status = result.get("status", {}).get("video_status", "unknown")
        if status == "ready" or status in {"error", "failed"}:
            return {"video_id": video_id, "status": status, "thumbnail_url": result.get("picture")}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"video_id": video_id, "status": status, "thumbnail_url": None}
        time.sleep(min(5, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Local MP4 to upload")
    source.add_argument("--video-id", help="Check a previously uploaded video without uploading again")
    parser.add_argument("--account-id", help="Meta Ads account ID, with or without act_")
    parser.add_argument("--version", default="v25.0")
    parser.add_argument("--wait", type=int, default=180, help="Seconds to wait for processing")
    parser.add_argument("--check", action="store_true", help="Validate the local file without calling Meta")
    args = parser.parse_args()
    if not re.fullmatch(r"v\d+\.\d+", args.version) or args.wait < 0:
        parser.error("Use a Graph API version such as v25.0 and a nonnegative wait time.")
    if args.video_id and (args.check or not re.fullmatch(r"[0-9]+", args.video_id)):
        parser.error("Use a numeric --video-id. --check requires --file.")

    video_id = args.video_id
    try:
        if args.file:
            with args.file.open("rb") as video:
                if args.file.suffix.lower() != ".mp4" or video.read(12)[4:8] != b"ftyp":
                    raise ValueError("Expected a readable MP4 file.")
            if args.check:
                print(json.dumps({"file": args.file.name, "bytes": args.file.stat().st_size}))
                return 0
            account_id = (args.account_id or "").removeprefix("act_")
            if not re.fullmatch(r"[0-9]+", account_id):
                parser.error("Uploading requires a numeric --account-id.")

        token = os.environ.get("FB_ACCESS_TOKEN", "").strip()
        if not token and sys.stdin.isatty():
            token = getpass.getpass("Meta access token: ").strip()
        if not token:
            raise ValueError("Set FB_ACCESS_TOKEN in the local execution environment.")

        with requests.Session() as session:
            session.headers["Authorization"] = f"Bearer {token}"
            if args.file:
                with args.file.open("rb") as video:
                    response = session.post(
                        f"https://graph-video.facebook.com/{args.version}/act_{account_id}/advideos",
                        data={"name": args.file.stem},
                        files={"source": (args.file.name, video, "video/mp4")},
                        timeout=(15, 180),
                    )
                result = graph_json(response, token)
                video_id = str(result.get("id") or result.get("video_id") or "")
                if not video_id.isdigit():
                    raise ValueError("Meta did not return a video ID. Check the account's videos before retrying.")
                print(f"Meta accepted the upload. Video ID: {video_id}", file=sys.stderr, flush=True)
            result = process_video(session, args.version, token, video_id, args.wait)
        print(json.dumps(result))
        return 0 if result["status"] == "ready" else 2
    except requests.RequestException:
        print("Could not complete the Meta request. Check connectivity before retrying.", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
    if video_id:
        print(json.dumps({"video_id": video_id, "status": "unverified"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

