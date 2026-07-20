"""Dedicated process entrypoint for the credential-free public-read broker."""

from __future__ import annotations

import argparse
from pathlib import Path

from .public_read import serve


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--receipts", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--task-sha256", required=True)
    parser.add_argument("--attestation", required=True)
    parser.add_argument(
        "--isolation-mode",
        choices=("bubblewrap", "macos-sandbox", "local-process-test"),
        required=True,
    )
    args = parser.parse_args()
    serve(
        socket_path=Path(args.socket),
        receipt_dir=Path(args.receipts),
        home=Path(args.home),
        execution_id=args.execution_id,
        task_sha256=args.task_sha256,
        attestation_path=Path(args.attestation),
        isolation_mode=args.isolation_mode,
    )


if __name__ == "__main__":
    main()
