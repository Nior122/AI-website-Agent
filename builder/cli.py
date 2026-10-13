"""Build or serve a local preview artifact from one qualified record; no deployment occurs."""

from __future__ import annotations

import argparse
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from .website_builder import WebsiteBuilder

ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def validate_serve_target(directory: Path, host: str = "127.0.0.1") -> Path:
    cleaned_host = str(host or "").strip()
    if cleaned_host not in ALLOWED_LOCAL_HOSTS:
        raise ValueError(
            f"Refusing to bind local preview server to '{cleaned_host}': only loopback '127.0.0.1' (or 'localhost') is permitted; '0.0.0.0' and public interfaces are forbidden."
        )
    resolved = Path(directory).resolve()
    index_path = resolved / "index.html"
    if not resolved.is_dir() or not index_path.is_file():
        raise ValueError(f"Directory {resolved} does not contain an index.html file.")
    pkg_path = resolved / "review-package.json"
    if pkg_path.is_file():
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        actual_sha = hashlib.sha256(index_path.read_bytes()).hexdigest()
        expected_sha = pkg.get("artifact_sha256")
        if expected_sha and actual_sha != expected_sha:
            raise ValueError(
                f"Review package integrity error in {resolved}: index.html SHA-256 ({actual_sha}) does not match review-package.json ({expected_sha})."
            )
    return resolved


def serve_build(directory: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    resolved = validate_serve_target(directory, host=host)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(resolved))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Serving local review preview from {resolved} at http://{host}:{port}/ (bound to {host} only).")
    print("Press Ctrl+C to stop the local server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
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
