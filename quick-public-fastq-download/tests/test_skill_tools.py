from __future__ import annotations

import csv
import gzip
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fastq_common import load_manifest, safe_relative_path, validate_file  # noqa: E402
from fetch_ena_manifest import read_accessions, species_key  # noqa: E402
from download_fastq import process_row  # noqa: E402


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


class SkillToolsTest(unittest.TestCase):
    def test_path_safety(self) -> None:
        self.assertEqual(str(safe_relative_path("human/SRP1/SRR1/a.fastq.gz")), "human/SRP1/SRR1/a.fastq.gz")
        for bad in ("/tmp/a.fastq.gz", "../a.fastq.gz", "human/../../a.fastq.gz", ""):
            with self.assertRaises(ValueError):
                safe_relative_path(bad)

    def test_accession_and_species_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.txt"
            path.write_text("run_accession\nSRR123\n# note\nerr456\nSRR123\n")
            self.assertEqual(read_accessions(path), ["SRR123", "ERR456"])
        self.assertEqual(species_key("Homo sapiens"), "human")
        self.assertEqual(species_key("Mus musculus"), "mouse")
        self.assertEqual(species_key("Canis lupus familiaris"), "canis_lupus_familiaris")

    def test_integrity_and_end_to_end_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source_dir = workspace / "source"
            source_dir.mkdir()
            source = source_dir / "SRR123.fastq.gz"
            with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as handle:
                handle.write(b"@read1\nACGT\n+\nIIII\n")
            md5 = hashlib.md5(source.read_bytes()).hexdigest()
            validation = validate_file(source, source.stat().st_size, md5)
            self.assertEqual(validation["status"], "PASS")

            manifest = workspace / "manifest.tsv"
            row = {
                "run_accession": "SRR123",
                "study_accession": "SRP1",
                "species": "human",
                "scientific_name": "Homo sapiens",
                "fastq_url": "https://example.invalid/SRR123.fastq.gz",
                "filename": source.name,
                "md5": md5,
                "bytes": str(source.stat().st_size),
                "output_relpath": f"human/SRP1/SRR123/{source.name}",
            }
            with manifest.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(len(load_manifest(manifest)), 1)

            output = workspace / "output"
            status_dir = output / ".fastq-status"

            def fake_curl(_curl: str, _url: str, partial: Path, _timeout: int) -> subprocess.CompletedProcess[str]:
                partial.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, partial)
                return subprocess.CompletedProcess(["curl"], 0, "", "")

            with mock.patch("download_fastq.run_curl", side_effect=fake_curl):
                result = process_row(1, row, output, status_dir, None, "curl", 1, 1, 60, False)
            self.assertEqual(result["status"], "PASS")
            verify = subprocess.run(
                [sys.executable, str(SCRIPTS / "verify_release.py"), str(manifest), str(output)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            target = output / row["output_relpath"]
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertTrue((status_dir / "task_000001.json").is_file())


if __name__ == "__main__":
    unittest.main()
