---
name: quick-public-fastq-download
description: Build and operate fast, resumable, checksum-verified FASTQ intake from public ENA/SRA run accessions. Use for manifest creation, download acceleration, smoke testing, recovery, validation, and scheduler-aware scaling; not for private or controlled-access sequencing data.
---

# Quick Public FASTQ Download

Use direct ENA FASTQ objects when authoritative byte counts and MD5 checksums are available. Optimize wall time with bounded parallelism, but treat validation—not process exit status—as completion.

## What makes this skill fast

- It downloads ENA's ready-made FASTQs directly, avoiding slower SRA-to-FASTQ conversion when direct objects exist.
- `download_fastq.py` runs multiple files concurrently and uses aria2 segmented connections within each file.
- Validated files are checkpointed and skipped on every recovery run, so retries touch only missing or invalid data.
- LSF arrays spread independent files across available hosts while keeping a concurrency ceiling.

The fast path requires `aria2c`; without it, the script deliberately falls back to single-stream curl for correctness. After the smoke test, a well-provisioned server can explicitly use the proven high-throughput profile:

```bash
python3 scripts/download_fastq.py manifest.tsv fastq --workers 24 --connections 4
```

Scale this profile down if retries rise, per-connection throughput collapses, or shared storage becomes saturated.

## Required invariants

- Never embed credentials, hostnames, private paths, or project-specific identifiers in reusable output.
- Keep species and assemblies separate whenever either affects downstream interpretation.
- Write to `.part` files and promote atomically only after exact byte count, MD5, and `gzip -t` all pass.
- Preserve validated outputs. Quarantine invalid or incompatible partials instead of deleting them.
- Do not combine partial files produced by different download engines.
- Use HTTPS by default. Permit HTTP only when the user explicitly accepts it and the authoritative final checksum is enforced.
- Run a one-file smoke test before batch submission.
- Keep download, QC, and downstream processing as separate dependency stages so recovery does not recompute successful work.

## Workflow

1. Resolve run accessions into a normalized manifest:

   ```bash
   python3 scripts/fetch_ena_manifest.py accessions.txt manifest.tsv
   ```

2. Inspect the manifest, required storage, pairing, species, and output paths. Read [references/manifest-schema.md](references/manifest-schema.md) when adapting another manifest source.

3. Smoke-test one row with one connection:

   ```bash
   python3 scripts/download_fastq.py manifest.tsv fastq --limit 1 --workers 1 --connections 1
   python3 scripts/verify_release.py manifest.tsv fastq --limit 1
   ```

4. Start conservatively, observe throughput and failures, then scale. A useful first batch is 8 workers × 4 connections; increase toward 16–24 workers only when storage, network, and scheduler headroom remain healthy. Do not exceed site policy or the user's resource ceiling.

5. Verify the full release independently:

   ```bash
   python3 scripts/verify_release.py manifest.tsv fastq --report validation.tsv
   ```

6. Record manifest, validation report, tool versions, job IDs, failures, recoveries, and exclusions with the release.

For LSF arrays, adaptive scaling, stalled-job diagnosis, dependency recovery, and alternate-source decisions, read [references/operations.md](references/operations.md).

## Completion criteria

Report success only when every intended manifest row has an atomic PASS status and the independent release verifier passes. Distinguish transient retries, valid dependency waits, permanent failures, and scientifically excluded runs.
