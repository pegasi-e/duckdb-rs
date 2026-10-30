import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import bootstrap_duckdb as subject


def fixture_bundle(path, unsafe_path=None, link_target="../../LICENSE"):
    pin = {"version": "1.5.6", "commit": "d" * 40}
    files = {
        "Cargo.toml": '[workspace]\nmembers=["crates/duckdb","crates/libduckdb-sys","crates/duckdb-loadable-macros"]\nresolver="3"\n[workspace.package]\nversion="1.10503.1"\n[workspace.dependencies]\nlibduckdb-sys={path="crates/libduckdb-sys",version="=1.10503.1"}\n',
        "LICENSE": "MIT fixture license\n",
        "crates/duckdb/Cargo.toml": '[package]\nname="duckdb"\nversion.workspace=true\nedition="2024"\n[features]\nbundled=["libduckdb-sys/bundled"]\njson=[]\n[dependencies]\nlibduckdb-sys.workspace=true\n',
        "crates/duckdb/src/lib.rs": "pub fn answer() -> i32 { 42 }\n",
        "crates/libduckdb-sys/Cargo.toml": '[package]\nname="libduckdb-sys"\nversion.workspace=true\nedition="2024"\n[features]\nbundled=[]\n',
        "crates/libduckdb-sys/src/lib.rs": "",
        "crates/libduckdb-sys/duckdb-fork.json": json.dumps(pin),
        "crates/libduckdb-sys/duckdb.tar.gz": "native source fixture",
        "crates/libduckdb-sys/duckdb.tar.gz.sha256": hashlib.sha256(b"native source fixture").hexdigest() + "  duckdb.tar.gz\n",
        "crates/duckdb-loadable-macros/Cargo.toml": '[package]\nname="duckdb-loadable-macros"\nversion.workspace=true\nedition="2024"\n',
        "crates/duckdb-loadable-macros/src/lib.rs": "",
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, value in files.items():
            entry = tarfile.TarInfo("duckdb-rs-anybase/" + name)
            payload = value.encode()
            entry.size, entry.mode = len(payload), 0o644
            archive.addfile(entry, io.BytesIO(payload))
        link = tarfile.TarInfo("duckdb-rs-anybase/crates/libduckdb-sys/LICENSE")
        link.type, link.linkname = tarfile.SYMTYPE, link_target
        archive.addfile(link)
        if unsafe_path:
            entry = tarfile.TarInfo(unsafe_path)
            archive.addfile(entry, io.BytesIO())
    platform_bytes = io.BytesIO()
    with zipfile.ZipFile(platform_bytes, "w") as platform:
        platform.writestr("target/package/duckdb-rs-anybase-source.tar.gz", data.getvalue())
    platform = platform_bytes.getvalue()
    entries = {f"duckdb-rs-fork-{name}.zip": platform
               for name in ["Linux-ARM64", "Linux-X64", "macOS-ARM64", "Windows-X64"]}
    manifest = {"source_commit": "a" * 40, "duckdb": pin,
                "files": [{"name": name, "sha256": hashlib.sha256(value).hexdigest()}
                          for name, value in entries.items()]}
    entries["archive-manifest.json"] = json.dumps(manifest).encode()
    entries["SHA256SUMS"] = "".join(f"{hashlib.sha256(value).hexdigest()}  {name}\n"
                                     for name, value in entries.items()).encode()
    with zipfile.ZipFile(path, "w") as bundle:
        for name, value in entries.items():
            bundle.writestr(name, value)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "bundle.zip"
        fixture_bundle(self.bundle)

    def unpack(self):
        return subject.unpack_source(self.bundle, self.root / "extracted")

    def test_extracts_complete_workspace_and_materializes_file_links(self):
        source, info = self.unpack()
        self.assertEqual("1.5.6", info["duckdb"]["version"])
        self.assertEqual("MIT fixture license\n", (source / "crates/libduckdb-sys/LICENSE").read_text())
        self.assertFalse((source / "crates/libduckdb-sys/LICENSE").is_symlink())

    def test_rejects_wrong_outer_checksum_before_extraction(self):
        with self.assertRaisesRegex(RuntimeError, "--sha256"):
            subject.unpack_source(self.bundle, self.root / "extracted", "0" * 64)
        self.assertFalse((self.root / "extracted").exists())

    def test_rejects_source_traversal(self):
        fixture_bundle(self.bundle, unsafe_path="duckdb-rs-anybase/../../escape")
        with self.assertRaisesRegex(RuntimeError, "Unsafe"):
            self.unpack()
        self.assertFalse((self.root / "escape").exists())

    def test_rejects_escaping_source_links(self):
        fixture_bundle(self.bundle, link_target="../../../outside")
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            self.unpack()

    def test_preserves_existing_workspace_excludes_and_comments(self):
        original = '# keep this\n[workspace]\nmembers=["app"]\nexclude=[\n  "old", # keep this too\n]\n'
        result = subject.configure_root(original, "vendor/fork", "vendor")
        parsed = subject.tomllib.loads(result)
        self.assertEqual(["vendor", "old"], parsed["workspace"]["exclude"])
        self.assertIn("# keep this too", result)
        self.assertEqual("vendor/fork/crates/libduckdb-sys", parsed["patch"]["crates-io"]["libduckdb-sys"]["path"])
        self.assertEqual(result, subject.configure_root(result, "vendor/fork", "vendor"))

    def test_does_not_replace_existing_duckdb_override(self):
        original = '[package]\nname="app"\nversion="0.1.0"\n[patch.crates-io]\nduckdb={path="custom"}\n'
        with self.assertRaisesRegex(RuntimeError, "Existing DuckDB"):
            subject.configure_root(original, "vendor/fork", "vendor")

    def test_refuses_to_remove_unrelated_settings_inside_managed_markers(self):
        text = subject.configure_root('[package]\nname="app"\nversion="0.1.0"\n', "vendor/fork", "vendor")
        text = text.replace(subject.END, '[dependencies]\nother="1"\n' + subject.END)
        with self.assertRaisesRegex(RuntimeError, "other Cargo settings"):
            subject.configure_root(text, "vendor/fork", "vendor")

    def test_download_redirect_does_not_forward_credentials_to_other_hosts(self):
        request = urllib.request.Request("https://one.example/bundle", headers={"Authorization": "Bearer private-value"})
        redirected = subject.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://two.example/bundle")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaisesRegex(RuntimeError, "HTTPS"):
            subject.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "http://one.example/bundle")

    def test_download_auth_failure_does_not_expose_url_or_token(self):
        url = "https://example.invalid/bundle?private=hidden-value"
        failure = urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        with patch.dict(os.environ, {"DUCKDB_BUNDLE_TOKEN": "private-token"}), \
             patch.object(subject.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaises(RuntimeError) as error:
                subject.obtain_bundle(url, self.root)
        self.assertIn("401", str(error.exception))
        self.assertNotIn("hidden-value", str(error.exception))
        self.assertNotIn("private-token", str(error.exception))

    def application(self, workspace=False):
        root = self.root / "application"
        app = root / "app" if workspace else root
        (app / "src").mkdir(parents=True)
        (app / "src/main.rs").write_text("fn main() {}\n")
        (app / "Cargo.toml").write_text('[package]\nname="consumer"\nversion="0.1.0"\nedition="2024"\n')
        if workspace:
            old = root / "old-duckdb"
            (old / "src").mkdir(parents=True)
            (old / "src/lib.rs").write_text("")
            (old / "Cargo.toml").write_text('[package]\nname="duckdb"\nversion="1.10503.1"\nedition="2024"\n[features]\njson=[]\n[workspace]\n')
            (root / "Cargo.toml").write_text('[workspace]\nmembers=["app"]\nexclude=["old-duckdb"]\nresolver="3"\n[workspace.dependencies]\nduckdb={path="old-duckdb",features=["json"]}\n')
            with (app / "Cargo.toml").open("a") as stream:
                stream.write('[dependencies]\nduckdb={workspace=true}\n')
        return root, app / "Cargo.toml"

    def arguments(self, manifest):
        return argparse.Namespace(bundle=str(self.bundle), manifest_path=manifest, vendor_dir=None,
                                  sha256=None, offline=True, build=False)

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is required for integration tests")
    def test_configures_real_cargo_project_idempotently(self):
        root, manifest = self.application()
        with patch.dict(os.environ, {"RUSTUP_TOOLCHAIN": "stable"}), patch("sys.stdout", new_callable=io.StringIO):
            subject.install(self.arguments(manifest))
            configured = manifest.read_bytes()
            subject.install(self.arguments(manifest))
        self.assertEqual(configured, manifest.read_bytes())
        value = subject.toml(manifest)
        self.assertIn("bundled", value["dependencies"]["duckdb"]["features"])
        self.assertEqual(["vendor"], value["workspace"]["exclude"])

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is required for integration tests")
    def test_repeat_install_preserves_application_dependencies(self):
        root, manifest = self.application()
        other = self.root / "other"
        (other / "src").mkdir(parents=True)
        (other / "src/lib.rs").write_text("")
        (other / "Cargo.toml").write_text('[package]\nname="other"\nversion="0.1.0"\nedition="2024"\n')
        with patch.dict(os.environ, {"RUSTUP_TOOLCHAIN": "stable"}), patch("sys.stdout", new_callable=io.StringIO):
            subject.install(self.arguments(manifest))
            text = manifest.read_text().replace('[dependencies]\n', '[dependencies]\nother={path="../other"}\n')
            manifest.write_text(text)
            subject.install(self.arguments(manifest))
        self.assertEqual("../other", subject.toml(manifest)["dependencies"]["other"]["path"])

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is required for integration tests")
    def test_configures_member_and_preserves_inherited_features(self):
        root, manifest = self.application(workspace=True)
        with patch.dict(os.environ, {"RUSTUP_TOOLCHAIN": "stable"}), patch("sys.stdout", new_callable=io.StringIO):
            subject.install(self.arguments(manifest))
        self.assertEqual({"json", "bundled"}, set(subject.toml(manifest)["dependencies"]["duckdb"]["features"]))
        self.assertEqual(["vendor", "old-duckdb"], subject.toml(root / "Cargo.toml")["workspace"]["exclude"])

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is required for integration tests")
    def test_preserves_existing_dependency_alias(self):
        root, manifest = self.application()
        old = self.root / "old"
        (old / "src").mkdir(parents=True)
        (old / "src/lib.rs").write_text("")
        (old / "Cargo.toml").write_text('[package]\nname="duckdb"\nversion="1.10503.1"\nedition="2024"\n[features]\njson=[]\n')
        with manifest.open("a") as stream:
            stream.write('[dependencies]\nquack={package="duckdb",path="../old",features=["json"]}\n')
        with patch.dict(os.environ, {"RUSTUP_TOOLCHAIN": "stable"}), patch("sys.stdout", new_callable=io.StringIO):
            subject.install(self.arguments(manifest))
        dependencies = subject.toml(manifest)["dependencies"]
        self.assertNotIn("duckdb", dependencies)
        self.assertEqual("duckdb", dependencies["quack"]["package"])
        self.assertIn("json", dependencies["quack"]["features"])

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is required for integration tests")
    def test_restores_manifests_and_lockfile_if_cargo_add_fails(self):
        root, manifest = self.application()
        lock = root / "Cargo.lock"
        lock.write_bytes(b"original lockfile")
        original = manifest.read_bytes()
        with patch.dict(os.environ, {"RUSTUP_TOOLCHAIN": "stable"}), \
             patch.object(subject.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["cargo", "add"])):
            with self.assertRaises(subprocess.CalledProcessError):
                subject.install(self.arguments(manifest))
        self.assertEqual(original, manifest.read_bytes())
        self.assertEqual(b"original lockfile", lock.read_bytes())
        self.assertFalse((root / "vendor").exists())

    @unittest.skipUnless(shutil.which("cargo"), "Cargo is required for integration tests")
    def test_rejects_cmake_only_feature_without_full_checkout(self):
        root, manifest = self.application()
        with manifest.open("a") as stream:
            stream.write('[dependencies]\nduckdb={version="1.10503.1",features=["icu"]}\n')
        original = manifest.read_bytes()
        with patch.dict(os.environ, {"RUSTUP_TOOLCHAIN": "stable"}):
            with self.assertRaisesRegex(RuntimeError, "CMake-only"):
                subject.install(self.arguments(manifest))
        self.assertEqual(original, manifest.read_bytes())
        self.assertFalse((root / "vendor").exists())


if __name__ == "__main__":
    unittest.main()
