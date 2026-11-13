#!/usr/bin/env python3
"""Independently verify FASTQ size, MD5, gzip integrity, and path safety."""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from fastq_common import load_manifest, target_path, validate_file


FIELDS = [
    "task_id",
    "run_accession",
    "species",
    "path",
    "status",
    "error",
    "expected_bytes",
    "actual_bytes",
    "expected_md5",
    "actual_md5",
    "gzip_pass",
    "verified_at",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--task-index", type=int, help="verify one one-based manifest row")
    parser.add_argument("--skip-gzip", action="store_true", help="only for a quick diagnostic, never a release validation")
    args = parser.parse_args()
    rows = load_manifest(args.manifest)
    indexed = list(enumerate(rows, 1))
    if args.task_index is not None:
        if not 1 <= args.task_index <= len(indexed):
            parser.error(f"task-index must be 1..{len(indexed)}")
        indexed = [indexed[args.task_index - 1]]
    elif args.limit is not None:
        if args.limit < 1:
            parser.error("limit must be positive")
        indexed = indexed[: args.limit]
    root = args.output_root.resolve()
    output: list[dict[str, object]] = []
    for task_id, row in indexed:
        path = target_path(root, row["output_relpath"])
        validation = validate_file(path, int(row["bytes"]), row["md5"], test_gzip=not args.skip_gzip)
        item: dict[str, object] = {
            "task_id": task_id,
            "run_accession": row["run_accession"],
            "species": row["species"],
            "path": str(path),
            "status": validation["status"],
            "error": validation.get("error", ""),
            "expected_bytes": int(row["bytes"]),
            "actual_bytes": validation.get("actual_bytes", ""),
            "expected_md5": row["md5"],
            "actual_md5": validation.get("actual_md5", ""),
            "gzip_pass": validation.get("gzip_pass", ""),
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        output.append(item)
        print(f"{item['status']}\t{task_id}\t{row['run_accession']}\t{path}")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.report.with_name(f"{args.report.name}.tmp.{os.getpid()}")
        with tmp.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, delimiter="\t", fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, args.report)
    failed = sum(item["status"] != "PASS" for item in output)
    print(f"SUMMARY\tpass={len(output) - failed}\tfail={failed}\ttotal={len(output)}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
