"""Dedicated entrypoint for the no-network reviewed exercise broker."""

from __future__ import annotations

import argparse
from pathlib import Path

from .exercise import serve


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--task-sha256", required=True)
    parser.add_argument("--attestation", required=True)
    args = parser.parse_args()
    serve(Path(args.socket), Path(args.evidence), Path(args.scratch), args.execution_id, args.task_sha256, Path(args.attestation))


if __name__ == "__main__":
    main()
