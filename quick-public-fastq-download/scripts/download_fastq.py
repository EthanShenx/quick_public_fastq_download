#!/usr/bin/env python3
"""Download FASTQs concurrently with resumability and strict promotion checks."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastq_common import atomic_json, load_manifest, target_path, validate_file


def quarantine(path: Path, root: Path, reason: str) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        relpath = path.resolve().relative_to(root.resolve())
    except ValueError:
        relpath = Path(path.name)
    destination = root / ".quarantine" / relpath.parent / f"{relpath.name}.{reason}.{stamp}.{time.time_ns()}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, destination)
    return destination


def tool_version(command: str | None, flag: str) -> str | None:
    if not command:
        return None
    try:
        result = subprocess.run([command, flag], text=True, capture_output=True, timeout=15)
        return (result.stdout or result.stderr).splitlines()[0][:300]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def run_aria2(aria2c: str, url: str, partial: Path, connections: int, timeout: int) -> subprocess.CompletedProcess[str]:
    command = [
        aria2c,
        "--continue=true",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--file-allocation=none",
        f"--max-connection-per-server={connections}",
        f"--split={connections}",
        "--min-split-size=1M",
        "--max-tries=5",
        "--retry-wait=10",
        "--connect-timeout=30",
        f"--timeout={timeout}",
        "--summary-interval=0",
        f"--dir={partial.parent}",
        f"--out={partial.name}",
        url,
    ]
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def run_curl(curl: str, url: str, partial: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    command = [
        curl,
        "--fail",
        "--location",
        "--show-error",
        "--silent",
        "--connect-timeout",
        "30",
        "--max-time",
        str(timeout),
        "--retry",
        "5",
        "--retry-delay",
        "10",
        "--retry-all-errors",
    ]
    if partial.exists() and partial.stat().st_size:
        command.extend(["--continue-at", "-"])
    command.extend(["--output", str(partial), url])
    return subprocess.run(command, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def process_row(
    task_id: int,
    row: dict[str, str],
    root: Path,
    status_dir: Path,
    aria2c: str | None,
    curl: str | None,
    connections: int,
    attempts: int,
    timeout: int,
    allow_http: bool,
) -> dict[str, Any]:
    started = time.time()
    target = target_path(root, row["output_relpath"])
    partial = target.with_name(target.name + ".part")
    control = partial.with_name(partial.name + ".aria2")
    status_path = status_dir / f"task_{task_id:06d}.json"
    base: dict[str, Any] = {
        "task_id": task_id,
        "run_accession": row["run_accession"],
        "url": row["fastq_url"],
        "target": str(target),
        "expected_bytes": int(row["bytes"]),
        "expected_md5": row["md5"],
    }
    if row["fastq_url"].startswith("http://") and not allow_http:
        result = base | {"status": "FAIL", "error": "HTTP URL rejected; use HTTPS or --allow-http"}
        atomic_json(status_path, result)
        return result

    existing = validate_file(target, int(row["bytes"]), row["md5"])
    if existing["status"] == "PASS":
        result = base | existing | {"cached": True, "seconds": time.time() - started}
        atomic_json(status_path, result)
        return result
    if target.exists():
        quarantined = quarantine(target, root, "invalid_final")
        base["quarantined_final"] = str(quarantined)

    target.parent.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        # Only aria2 may resume a partial carrying aria2 state. A plain partial
        # is resumed sequentially by curl to avoid mixing segment semantics.
        use_aria2 = bool(aria2c) and (not partial.exists() or control.exists())
        method = "aria2c" if use_aria2 else "curl"
        if use_aria2:
            completed = run_aria2(aria2c or "aria2c", row["fastq_url"], partial, connections, timeout)
            detail = completed.stdout[-4000:]
        elif curl:
            completed = run_curl(curl, row["fastq_url"], partial, timeout)
            detail = completed.stderr[-4000:]
        else:
            result = base | {"status": "FAIL", "error": "neither aria2c nor curl is available"}
            atomic_json(status_path, result)
            return result

        validation = validate_file(partial, int(row["bytes"]), row["md5"])
        if completed.returncode == 0 and validation["status"] == "PASS":
            os.replace(partial, target)
            control.unlink(missing_ok=True)
            result = base | validation | {
                "status": "PASS",
                "attempt": attempt,
                "method": method,
                "connections": connections if method == "aria2c" else 1,
                "seconds": time.time() - started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_json(status_path, result)
            return result

        error = {
            "attempt": attempt,
            "method": method,
            "returncode": completed.returncode,
            "validation": validation,
            "detail": detail,
        }
        errors.append(error)
        # A completed but invalid object must restart cleanly. When the command
        # itself failed, preserve the partial for compatible resume behavior.
        if completed.returncode == 0 or validation.get("actual_bytes", 0) > int(row["bytes"]):
            failed_partial = quarantine(partial, root, "integrity_failure")
            failed_control = quarantine(control, root, "integrity_failure")
            error["quarantined_partial"] = str(failed_partial) if failed_partial else None
            error["quarantined_control"] = str(failed_control) if failed_control else None
        atomic_json(status_path, base | {"status": "RETRY", "errors": errors})
        if attempt < attempts:
            time.sleep(min(120, 5 * 2 ** (attempt - 1)))

    result = base | {"status": "FAIL", "errors": errors, "seconds": time.time() - started}
    atomic_json(status_path, result)
    return result


def required_remaining(rows: list[dict[str, str]], root: Path) -> int:
    total = 0
    for row in rows:
        target = target_path(root, row["output_relpath"])
        partial = target.with_name(target.name + ".part")
        have = target.stat().st_size if target.exists() else partial.stat().st_size if partial.exists() else 0
        total += max(0, int(row["bytes"]) - have)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--connections", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=7200, help="per downloader invocation in seconds")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--task-index", type=int, help="one-based manifest row for scheduler arrays")
    parser.add_argument("--status-dir", type=Path)
    parser.add_argument("--aria2c", default=shutil.which("aria2c"))
    parser.add_argument("--curl", default=shutil.which("curl"))
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reserve-gib", type=float, default=10.0, help="free-space reserve after missing bytes")
    args = parser.parse_args()
    if not 1 <= args.workers <= 64 or not 1 <= args.connections <= 16 or not 1 <= args.attempts <= 20:
        parser.error("workers must be 1..64, connections 1..16, and attempts 1..20")

    rows = load_manifest(args.manifest)
    indexed = list(enumerate(rows, 1))
    if args.task_index is not None:
        if not 1 <= args.task_index <= len(indexed):
            parser.error(f"task-index must be 1..{len(indexed)}")
        indexed = [indexed[args.task_index - 1]]
        args.workers = 1
    elif args.limit is not None:
        if args.limit < 1:
            parser.error("limit must be positive")
        indexed = indexed[: args.limit]

    root = args.output_root.resolve()
    status_dir = (args.status_dir or (root / ".fastq-status")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    selected_rows = [row for _, row in indexed]
    needed = required_remaining(selected_rows, root)
    free = shutil.disk_usage(root).free
    reserve = int(args.reserve_gib * 1024**3)
    if free < needed + reserve:
        raise SystemExit(f"insufficient free space: need {needed + reserve} bytes including reserve, have {free}")

    engine = "aria2c-multisegment" if args.aria2c else "curl-single-stream"
    print(
        f"PLAN\tfiles={len(indexed)}\tmissing_bytes={needed}\tworkers={min(args.workers, len(indexed))}"
        f"\tconnections={args.connections if args.aria2c else 1}\tengine={engine}"
        f"\thttps_default={not args.allow_http}"
    )
    if not args.aria2c:
        print("NOTICE\taria2c not found; using the safe single-stream curl fallback", file=sys.stderr)
    if args.dry_run:
        return
    if not args.aria2c and not args.curl:
        raise SystemExit("aria2c or curl is required")
    provenance = {
        "manifest": str(args.manifest.resolve()),
        "output_root": str(root),
        "workers": args.workers,
        "connections": args.connections,
        "aria2c": args.aria2c,
        "aria2c_version": tool_version(args.aria2c, "--version"),
        "curl": args.curl,
        "curl_version": tool_version(args.curl, "--version"),
        "python": sys.version,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(status_dir / "run_provenance.json", provenance)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(args.workers, len(indexed))) as executor:
        futures = {
            executor.submit(
                process_row,
                task_id,
                row,
                root,
                status_dir,
                args.aria2c,
                args.curl,
                args.connections,
                args.attempts,
                args.timeout,
                args.allow_http,
            ): task_id
            for task_id, row in indexed
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"{result['status']}\t{result['task_id']}\t{result['run_accession']}\t{result['target']}", flush=True)
    failed = [result for result in results if result["status"] != "PASS"]
    print(f"SUMMARY\tpass={len(results) - len(failed)}\tfail={len(failed)}\ttotal={len(results)}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
