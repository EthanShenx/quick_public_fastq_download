#!/usr/bin/env python3
"""Resolve public ENA run accessions into a checksum-bearing FASTQ manifest."""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath


RUN_RE = re.compile(r"^[SED]RR\d+$", re.IGNORECASE)
FIELDS = [
    "run_accession",
    "study_accession",
    "species",
    "scientific_name",
    "fastq_url",
    "filename",
    "md5",
    "bytes",
    "output_relpath",
]


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return cleaned or "unknown"


def species_key(scientific_name: str) -> str:
    known = {"Homo sapiens": "human", "Mus musculus": "mouse"}
    return known.get(scientific_name, slug(scientific_name).lower())


def read_accessions(path: Path) -> list[str]:
    accessions: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text().splitlines():
        value = raw.split("#", 1)[0].strip()
        if not value or value.lower() in {"run_accession", "accession"}:
            continue
        value = value.upper()
        if not RUN_RE.fullmatch(value):
            raise ValueError(f"invalid run accession: {value}")
        if value not in seen:
            accessions.append(value)
            seen.add(value)
    if not accessions:
        raise ValueError("accession file contains no run accessions")
    return accessions


def request_report(accession: str, timeout: int, attempts: int) -> list[dict[str, str]]:
    params = urllib.parse.urlencode(
        {
            "accession": accession,
            "result": "read_run",
            "fields": "run_accession,study_accession,scientific_name,fastq_ftp,fastq_md5,fastq_bytes",
            "format": "tsv",
        }
    )
    url = f"https://www.ebi.ac.uk/ena/portal/api/filereport?{params}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "quick-public-fastq-download/1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
            records = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
            if len(records) != 1 or records[0].get("run_accession", "").upper() != accession:
                raise ValueError(f"ENA returned no unique read_run record for {accession}")
            record = records[0]
            urls = [part for part in record.get("fastq_ftp", "").split(";") if part]
            md5s = [part.lower() for part in record.get("fastq_md5", "").split(";") if part]
            sizes = [part for part in record.get("fastq_bytes", "").split(";") if part]
            if not urls or not (len(urls) == len(md5s) == len(sizes)):
                raise ValueError(f"ENA has no complete direct FASTQ metadata for {accession}")
            scientific_name = record.get("scientific_name", "").strip() or "unknown"
            study = record.get("study_accession", "").strip() or "unknown_study"
            species = species_key(scientific_name)
            rows: list[dict[str, str]] = []
            for remote, md5, size in zip(urls, md5s, sizes):
                if not re.fullmatch(r"[0-9a-f]{32}", md5):
                    raise ValueError(f"invalid ENA MD5 for {accession}: {md5}")
                if not size.isdigit() or int(size) <= 0:
                    raise ValueError(f"invalid ENA byte count for {accession}: {size}")
                fastq_url = remote if remote.startswith("https://") else f"https://{remote}"
                filename = PurePosixPath(urllib.parse.urlparse(fastq_url).path).name
                if not filename.endswith((".fastq.gz", ".fq.gz")):
                    raise ValueError(f"unexpected FASTQ filename for {accession}: {filename}")
                relpath = PurePosixPath(species, slug(study), accession, filename)
                rows.append(
                    {
                        "run_accession": accession,
                        "study_accession": study,
                        "species": species,
                        "scientific_name": scientific_name,
                        "fastq_url": fastq_url,
                        "filename": filename,
                        "md5": md5,
                        "bytes": str(int(size)),
                        "output_relpath": str(relpath),
                    }
                )
            return rows
        except Exception as exc:  # network and schema failures share bounded retry behavior
            last_error = exc
            if attempt < attempts:
                time.sleep(min(30, 2 ** (attempt - 1)))
    assert last_error is not None
    raise RuntimeError(f"failed to resolve {accession}: {last_error}") from last_error


def write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with tmp.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("accessions", type=Path, help="one SRR/ERR/DRR accession per line")
    parser.add_argument("output", type=Path, help="output manifest TSV")
    parser.add_argument("--workers", type=int, default=8, help="concurrent ENA metadata requests")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32 or not 1 <= args.attempts <= 10:
        parser.error("workers must be 1..32 and attempts must be 1..10")

    accessions = read_accessions(args.accessions)
    resolved: dict[str, list[dict[str, str]]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(args.workers, len(accessions))) as executor:
        futures = {
            executor.submit(request_report, accession, args.timeout, args.attempts): accession
            for accession in accessions
        }
        for future in as_completed(futures):
            accession = futures[future]
            try:
                resolved[accession] = future.result()
                print(f"RESOLVED\t{accession}\t{len(resolved[accession])}", flush=True)
            except Exception as exc:
                errors.append(str(exc))
                print(f"FAILED\t{accession}\t{exc}", flush=True)
    if errors:
        raise SystemExit("manifest not written; " + " | ".join(errors))
    rows = [row for accession in accessions for row in resolved[accession]]
    write_manifest(args.output, rows)
    print(f"WROTE\t{args.output}\truns={len(accessions)}\tfiles={len(rows)}")


if __name__ == "__main__":
    main()
