# Manifest schema

The bundled tools use one TSV row per physical FASTQ object.

| Column | Required | Meaning |
|---|---:|---|
| `run_accession` | yes | SRR, ERR, or DRR run accession |
| `study_accession` | yes | Public study accession reported by ENA |
| `species` | yes | Stable path key such as `human`, `mouse`, or a sanitized scientific name |
| `scientific_name` | yes | Species name from ENA metadata |
| `fastq_url` | yes | Direct HTTPS FASTQ URL |
| `filename` | yes | Basename of the remote object |
| `md5` | yes | Authoritative lowercase MD5 for the compressed object |
| `bytes` | yes | Authoritative compressed byte count |
| `output_relpath` | yes | Safe relative path below the selected output root |

Paired runs have two rows with the same `run_accession`. Never infer pairing from row order; use filenames and source metadata. Keep each manifest immutable after a batch begins. If metadata changes, create a new versioned manifest and reconcile it explicitly.

## Importing another source

Normalize it to the same schema before using the downloader. Reject rows with missing checksums or ambiguous URLs. `output_relpath` must not be absolute and must not contain `..`. Do not mix assemblies or species merely to simplify directory layout.
