#!/usr/bin/env python3
"""Run the standalone evidence-and-coordination exercise service.

The service owns only fictional training records. It does not mount c2-core's
sensor-tasking, engagement, or command endpoints.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", type=Path, default=ROOT / "work" / "exercise.sqlite3")
    parser.add_argument("--tak-config", type=Path, help="Private TAK TLS configuration JSON")
    parser.add_argument("--setup-key-file", type=Path, help="Private file containing the room-creation setup key")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.setup_key_file:
        key = args.setup_key_file.read_text(encoding="utf-8").strip()
        if len(key) < 32:
            parser.error("setup key must have at least 32 characters")
        os.environ["EXERCISE_SETUP_TOKEN"] = key
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get("EXERCISE_SETUP_TOKEN"):
        parser.error("network hosting requires EXERCISE_SETUP_TOKEN or --setup-key-file; keep the default host for a local pilot")
    os.environ["EXERCISE_DB_PATH"] = str(args.db.resolve())
    if args.tak_config:
        if not args.tak_config.is_file():
            parser.error("TAK configuration file was not found")
        os.environ["EXERCISE_TAK_CONFIG"] = str(args.tak_config.resolve())
    sys.path.insert(0, str(ROOT))
    try:
        import uvicorn
    except ImportError:
        print("Install the runtime first: python -m pip install -r services/exercise_service/requirements.txt", file=sys.stderr)
        return 1
    print(f"Shared exercise: http://{args.host}:{args.port}/exercise.html", flush=True)
    print("Fictional training records. TAK transmission is manual and disabled until configured.", flush=True)
    uvicorn.run("services.exercise_service.app:app", host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
