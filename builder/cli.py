"""Build or serve a local preview artifact from one qualified record; no deployment occurs."""

from __future__ import annotations

import argparse
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from .website_builder import WebsiteBuilder


def serve_build(directory: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    directory = directory.resolve()
    if not directory.is_dir() or not (directory / "index.html").is_file():
        raise ValueError(f"Directory {directory} does not contain an index.html file.")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Serving preview at http://{host}:{port}/ (bound to {host})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or serve a local website preview.")
    parser.add_argument("--lead", type=Path, help="Path to qualified lead JSON object.")
    parser.add_argument("--output-root", type=Path, default=Path("output"))
    parser.add_argument("--serve", type=Path, help="Serve a build directory locally.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind (default: 8080)")
    args = parser.parse_args()
    if args.serve:
        serve_build(args.serve, host=args.host, port=args.port)
        return 0
    if not args.lead:
        parser.error("Either --lead or --serve must be specified.")
    lead = json.loads(args.lead.read_text(encoding="utf-8"))
    if not isinstance(lead, dict):
        parser.error("--lead must contain one qualified lead object, not an array.")
    print(json.dumps(WebsiteBuilder(args.output_root).build(lead).to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
