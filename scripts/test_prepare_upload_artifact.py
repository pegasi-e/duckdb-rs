import argparse
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import prepare_upload_artifact as subject


class PackagingTests(unittest.TestCase):
    def setUp(self):
        capture = patch("sys.stdout", new_callable=io.StringIO)
        capture.start()
        self.addCleanup(capture.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = argparse.Namespace(run_id=123, repository="pegasi-e/duckdb-rs",
                                       output_dir=Path(self.tmp.name))
        self.run = {"status": "completed", "conclusion": "success",
                    "path": ".github/workflows/rust.yaml", "head_sha": "a" * 40,
                    "run_attempt": 2, "html_url": "https://github.com/pegasi-e/duckdb-rs/actions/runs/123"}
        self.payloads = {}
        self.artifacts = []
        for artifact_id, name in enumerate(sorted(subject.ARTIFACT_NAMES), 1):
            payload = self.zip_payload("d" * 40)
            self.payloads[artifact_id] = payload
            self.artifacts.append({"id": artifact_id, "name": name, "expired": False,
                                   "digest": "sha256:" + hashlib.sha256(payload).hexdigest()})

    @staticmethod
    def zip_payload(commit):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as bundle:
            bundle.writestr("crates/libduckdb-sys/duckdb-fork.json",
                            json.dumps({"version": "1.5.6", "commit": commit}))
        return data.getvalue()

    def download(self, command, **kwargs):
        self.assertEqual(["gh", "api"], command[:2])
        artifact_id = int(command[-1].split("/")[-2])
        kwargs["stdout"].write(self.payloads[artifact_id])
        return subprocess.CompletedProcess(command, 0)

    def perform(self, environment=None):
        with patch.dict(os.environ, environment or {}, clear=True), \
             patch.object(subject, "gh_json", side_effect=[self.run, {"artifacts": self.artifacts}]), \
             patch.object(subject.subprocess, "run", side_effect=self.download) as downloads:
            subject.prepare(self.args)
            self.assertEqual(4, downloads.call_count)

    def test_preserves_original_zips_and_records_checksums(self):
        self.perform()
        output = self.args.output_dir / "123" / "attempt-2"
        for artifact in self.artifacts:
            self.assertEqual(self.payloads[artifact["id"]], (output / (artifact["name"] + ".zip")).read_bytes())
        manifest = json.loads((output / "archive-manifest.json").read_text())
        self.assertEqual(4, len(manifest["files"]))
        self.assertEqual(2, manifest["source_run_attempt"])
        self.assertEqual("a" * 40, manifest["source_commit"])
        self.assertNotIn("destination", manifest)
        for line in (output / "SHA256SUMS").read_text().splitlines():
            checksum, name = line.split("  ", 1)
            self.assertEqual(checksum, subject.digest(output / name))
        self.assertEqual(8, len(list(output.iterdir())))
        self.assertEqual({"bootstrap_duckdb.py", "APPLICATION_SETUP.md"},
                         {item["name"] for item in manifest["support_files"]})

    def test_github_outputs_identify_only_the_source_run_attempt(self):
        outputs = self.args.output_dir / "outputs.txt"
        summary = self.args.output_dir / "summary.md"
        self.perform({"GITHUB_OUTPUT": str(outputs), "GITHUB_STEP_SUMMARY": str(summary)})
        self.assertIn("artifact_name=duckdb-rs-manual-upload-123-attempt-2", outputs.read_text())
        self.assertIn(f"directory={self.args.output_dir}/123/attempt-2", outputs.read_text())
        self.assertIn("upload it manually", summary.read_text())

    def test_rejects_failed_source_run(self):
        self.run["conclusion"] = "failure"
        with self.assertRaisesRegex(RuntimeError, "successful"):
            self.perform()

    def test_rejects_wrong_workflow(self):
        self.run["path"] = ".github/workflows/release.yaml"
        with self.assertRaisesRegex(RuntimeError, "rust.yaml"):
            self.perform()

    def test_requires_all_four_platforms(self):
        self.artifacts.pop()
        with self.assertRaisesRegex(RuntimeError, "all four"):
            self.perform()

    def test_rejects_expired_artifacts(self):
        self.artifacts[0]["expired"] = True
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.perform()

    def test_rejects_changed_download_bytes(self):
        self.artifacts[0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            self.perform()

    def test_rejects_mixed_duckdb_provenance(self):
        artifact = self.artifacts[-1]
        payload = self.zip_payload("e" * 40)
        self.payloads[artifact["id"]] = payload
        artifact["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "disagree"):
            self.perform()


if __name__ == "__main__":
    unittest.main()
