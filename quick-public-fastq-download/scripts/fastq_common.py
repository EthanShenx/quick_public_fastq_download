"""Shared manifest and integrity helpers for the FASTQ download skill."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any


REQUIRED_FIELDS = {
    "run_accession",
    "study_accession",
    "species",
    "scientific_name",
    "fastq_url",
    "filename",
    "md5",
    "bytes",
    "output_relpath",
}


def safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise ValueError(f"unsafe output_relpath: {value!r}")
    return path


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = set(reader.fieldnames or [])
        missing = REQUIRED_FIELDS - fields
        if missing:
            raise ValueError(f"manifest missing columns: {sorted(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError("manifest contains no FASTQ rows")
    seen_paths: set[str] = set()
    for index, row in enumerate(rows, 1):
        relpath = str(safe_relative_path(row["output_relpath"]))
        if relpath in seen_paths:
            raise ValueError(f"duplicate output_relpath at row {index}: {relpath}")
        seen_paths.add(relpath)
        if not row["fastq_url"].startswith(("https://", "http://")):
            raise ValueError(f"unsupported URL at row {index}: {row['fastq_url']}")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", row["md5"]):
            raise ValueError(f"invalid MD5 at row {index}")
        try:
            size = int(row["bytes"])
        except ValueError as exc:
            raise ValueError(f"invalid byte count at row {index}") from exc
        if size <= 0:
            raise ValueError(f"invalid byte count at row {index}")
        if PurePosixPath(relpath).name != row["filename"]:
            raise ValueError(f"filename/output_relpath mismatch at row {index}")
        row["md5"] = row["md5"].lower()
        row["bytes"] = str(size)
    return rows


def target_path(root: Path, relpath: str) -> Path:
    root = root.resolve()
    target = root.joinpath(*safe_relative_path(relpath).parts).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"output path escaped root: {relpath}")
    return target


def md5_file(path: Path, block_size: int = 16 * 1024 * 1024) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def gzip_ok(path: Path) -> tuple[bool, str]:
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(16 * 1024 * 1024):
                pass
        return True, ""
    except (OSError, EOFError) as exc:
        return False, str(exc)


def validate_file(path: Path, expected_bytes: int, expected_md5: str, test_gzip: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "expected_bytes": expected_bytes, "expected_md5": expected_md5}
    if not path.is_file():
        return result | {"status": "FAIL", "error": "missing"}
    actual_bytes = path.stat().st_size
    result["actual_bytes"] = actual_bytes
    if actual_bytes != expected_bytes:
        return result | {"status": "FAIL", "error": "size mismatch"}
    actual_md5 = md5_file(path)
    result["actual_md5"] = actual_md5
    if actual_md5 != expected_md5.lower():
        return result | {"status": "FAIL", "error": "MD5 mismatch"}
    if test_gzip:
        passed, error = gzip_ok(path)
        result["gzip_pass"] = passed
        if not passed:
            return result | {"status": "FAIL", "error": f"gzip integrity failure: {error}"}
    return result | {"status": "PASS"}


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
