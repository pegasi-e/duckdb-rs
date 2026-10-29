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

import archive_artifacts as subject


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        capture = patch("sys.stdout", new_callable=io.StringIO)
        capture.start()
        self.addCleanup(capture.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = argparse.Namespace(run_id=123, repository="pegasi-e/duckdb-rs",
                                       output_dir=Path(self.tmp.name), dry_run=True)
        self.run = {"status": "completed", "conclusion": "success",
                    "path": ".github/workflows/rust.yaml", "head_sha": "a" * 40,
                    "run_attempt": 1, "html_url": "https://github.com/pegasi-e/duckdb-rs/actions/runs/123"}
        self.payloads = {}
        self.artifacts = []
        for artifact_id, name in enumerate(sorted(subject.ARTIFACT_NAMES), 1):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as bundle:
                bundle.writestr("crates/libduckdb-sys/duckdb-fork.json",
                                json.dumps({"version": "1.5.6", "commit": "d" * 40}))
            payload = data.getvalue()
            self.payloads[artifact_id] = payload
            self.artifacts.append({"id": artifact_id, "name": name, "expired": False,
                                   "digest": "sha256:" + hashlib.sha256(payload).hexdigest()})

    def download(self, command, **kwargs):
        artifact_id = int(command[-1].split("/")[-2])
        kwargs["stdout"].write(self.payloads[artifact_id])
        return subprocess.CompletedProcess(command, 0)

    def perform(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(subject, "gh_json", side_effect=[self.run, {"artifacts": self.artifacts}]), \
             patch.object(subject.subprocess, "run", side_effect=self.download), \
             patch.object(subject, "upload") as upload:
            try:
                subject.archive(self.args)
            finally:
                upload.assert_not_called()

    def test_dry_run_preserves_original_zips_and_records_checksums(self):
        self.perform()
        output = self.args.output_dir / "123"
        for artifact in self.artifacts:
            self.assertEqual(self.payloads[artifact["id"]], (output / (artifact["name"] + ".zip")).read_bytes())
        manifest = json.loads((output / "archive-manifest.json").read_text())
        self.assertEqual(4, len(manifest["files"]))
        self.assertTrue(manifest["destination"].endswith("/" + "a" * 40 + "/run-123/attempt-1"))
        self.assertEqual(5, len((output / "SHA256SUMS").read_text().splitlines()))

    def test_rejects_failed_source_run(self):
        self.run["conclusion"] = "failure"
        with self.assertRaisesRegex(RuntimeError, "successful"):
            self.perform()

    def test_requires_all_four_platforms(self):
        self.artifacts.pop()
        with self.assertRaisesRegex(RuntimeError, "all four"):
            self.perform()

    def test_rejects_changed_download_bytes(self):
        self.artifacts[0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            self.perform()

    def test_publish_requires_secret_before_network_calls(self):
        self.args.dry_run = False
        with patch.dict(os.environ, {}, clear=True), patch.object(subject, "gh_json") as github:
            with self.assertRaisesRegex(RuntimeError, "ARTIFACTORY_ACCESS_TOKEN"):
                subject.archive(self.args)
            github.assert_not_called()

    def test_publish_uploads_zips_before_manifest_and_checksums(self):
        self.args.dry_run = False
        with patch.dict(os.environ, {"ARTIFACTORY_ACCESS_TOKEN": "test-publish-token"}, clear=True), \
             patch.object(subject, "gh_json", side_effect=[self.run, {"artifacts": self.artifacts}]), \
             patch.object(subject.subprocess, "run", side_effect=self.download), \
             patch.object(subject, "upload") as upload:
            subject.archive(self.args)
        names = [call.args[0].name for call in upload.call_args_list]
        self.assertEqual(sorted(name + ".zip" for name in subject.ARTIFACT_NAMES), names[:4])
        self.assertEqual(["archive-manifest.json", "SHA256SUMS"], names[4:])
        for call in upload.call_args_list:
            self.assertTrue(call.args[1].startswith("https://gart.software.gevernova.com/artifactory/data-fabric-generic/duckdb-rs/"))

    def test_upload_failure_does_not_expose_token(self):
        path = self.args.output_dir / "sample.zip"
        path.write_bytes(b"original zip bytes")
        response = subprocess.CompletedProcess([], 22, stdout="401", stderr="private token: example-secret")
        with patch.object(subject.subprocess, "run", return_value=response):
            with self.assertRaises(RuntimeError) as error:
                subject.upload(path, "https://example.com/artifactory/repo/file.zip", "example-secret")
        self.assertNotIn("example-secret", str(error.exception))
        self.assertIn("401", str(error.exception))


if __name__ == "__main__":
    unittest.main()
