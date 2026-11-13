# Operating guide

## Concurrency model

Total remote pressure is approximately `workers × connections`. Begin around 32 simultaneous connections and scale only after observing sustained throughput, file growth, error rate, open-file limits, memory, load, and storage latency.

- Many ordinary FASTQs: 8 workers × 4 connections, then 16–24 × 4 if healthy.
- A few very large FASTQs: fewer file workers and 4–8 connections per file.
- Shared management hosts: respect administrator policy and leave headroom for login and scheduler services.

More workers do not help after the remote host, WAN, or shared filesystem saturates. Reduce concurrency when retries rise or throughput per connection collapses.

## LSF array pattern

Use one manifest row per array element and cap active elements. Quote the runtime array variable so the submission shell does not expand it prematurely.

```bash
rows=$(($(wc -l < manifest.tsv) - 1))
bsub -q DOWNLOAD_QUEUE \
  -J "fastq[1-${rows}]%24" \
  -n 1 -R 'rusage[mem=512]' -W 24:00 \
  -oo 'logs/download.%I.%J.out' -eo 'logs/download.%I.%J.err' \
  "python3 scripts/download_fastq.py manifest.tsv fastq --task-index \$LSB_JOBINDEX --workers 1 --connections 4"
```

Submit QC as a separate job depending on successful completion of the download array. If a download recovery replaces an old or aged-out job, cancel only permanently invalid descendants and rebuild their dependency chain against the replacement job.

## Recovery rules

1. Re-run the downloader against the unchanged manifest. Existing validated outputs are skipped.
2. An aria2 partial accompanied by its `.aria2` control file may resume with aria2.
3. A partial without that control file resumes sequentially with curl; do not reinterpret it as an aria2 partial.
4. Size, MD5, or gzip failures are quarantined with an audit record and retried from a clean partial.
5. Stop automatic recovery after the configured attempts. Diagnose URL changes, quota, remote throttling, storage faults, or manifest errors before resubmission.

## Stalls and failures

- A running job with growing `.part` files is active even if no PASS record appears yet.
- A pending QC job with `done(download_job)` is a normal dependency wait.
- `Dependency condition invalid or never satisfied` is permanent: identify the failed or aged-out ancestor and rebuild only the affected chain.
- Repeated exact-size but wrong-MD5 files indicate content corruption or stale/inconsistent source data. Preserve evidence, compare against authoritative metadata, and change route before retrying.
- Never accept a converted SRA-derived FASTQ as byte-identical to an ENA FASTQ. If a fallback conversion is scientifically acceptable, record it as a different provenance path and validate reads independently.

## Provenance to retain

- accession input and normalized manifest
- source URL, bytes, checksum, and retrieval time
- downloader and versions (`aria2c`, `curl`, Python)
- worker/connection settings and scheduler job IDs
- atomic status JSON files and independent validation report
- quarantined failures and the reason for recovery
- explicit exclusions with scientific justification
