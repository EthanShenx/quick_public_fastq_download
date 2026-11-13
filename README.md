# Quick Public FASTQ Download

This repository contains the Codex skill [`quick-public-fastq-download`](quick-public-fastq-download/SKILL.md) for fast, resumable, and verified public FASTQ intake.

## Download logic

1. Resolve SRR/ERR/DRR accessions to direct ENA FASTQ URLs, byte counts, and MD5 checksums.
2. Smoke-test one file before scaling.
3. Download many files concurrently; aria2 adds segmented connections within each file. On a suitable server, the proven fast profile is 24 workers × 4 connections.
4. Resume compatible partial files and skip every previously validated FASTQ.
5. Promote `.part` files only after exact size, MD5, and gzip checks pass.
6. Quarantine failures and retry only missing or invalid files; LSF arrays can distribute work across hosts.

The speed comes from direct ENA objects, bounded file-level parallelism, aria2 segmentation, and checkpoint-aware recovery—not from weakening validation. HTTPS is the default; curl is the safe single-stream fallback when aria2 is unavailable.
