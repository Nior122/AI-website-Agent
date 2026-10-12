"""Explicit local commands for inspecting and recovering orchestration state."""
from __future__ import annotations
import argparse
import json
from .api import serve
from .jobs import JobService
from tracking.store import TrackingStore


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Website Agent orchestration controls")
    parser.add_argument("command", choices=["serve", "recover-stale", "list-jobs"])
    parser.add_argument("--state-path", default="data/tracking/state.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--older-than-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.command == "serve":
        serve(state_path=args.state_path, host=args.host, port=args.port)
        return 0
    service = JobService(TrackingStore(args.state_path))
    data = service.recover_stale(older_than_seconds=args.older_than_seconds) if args.command == "recover-stale" else list(service.store.state["jobs"].values())
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
