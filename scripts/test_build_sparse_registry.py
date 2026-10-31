import functools
import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import build_sparse_registry as subject

PIN = {"version": "1.5.6", "commit": "d" * 40}
RUN = {"id": 123, "run_attempt": 1, "head_sha": "a" * 40, "html_url": "https://example.com/123"}


def crates():
    result = {}
    for name in subject.CRATES:
        manifest = {'package': {'name': name, 'version': '1.10503.1', 'edition': '2021'}}
        files = {'src/lib.rs': b'pub fn version() -> u32 { 156 }'}
        if name == 'duckdb':
            manifest['dependencies'] = {'libduckdb-sys': {'version': '=1.10503.1'},
                'duckdb-loadable-macros': {'version': '=1.10503.1', 'optional': True}}
            manifest['features'] = {'bundled': [], 'loadable-extension': ['dep:duckdb-loadable-macros']}
            files['src/lib.rs'] = b'pub fn version() -> u32 { libduckdb_sys::version() }'
        if name == 'libduckdb-sys':
            manifest['package']['links'] = 'duckdb'
            manifest['package']['build'] = 'build.rs'
            files['build.rs'] = b'fn main() {}'
        if name == 'duckdb-loadable-macros':
            manifest['lib'] = {'proc-macro': True}
            files['src/lib.rs'] = b''
        files['Cargo.toml'] = ('\n'.join(f'{subject.toml_value(k)} = {subject.toml_value(v)}' for k, v in manifest.items())).encode()
        files['Cargo.lock'] = b'old lockfile'
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for path, data in files.items():
                entry = tarfile.TarInfo(f'{name}-1.10503.1/{path}')
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        result[name] = stream.getvalue()
    return result


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sources = crates()
        self.base = 'https://example.com/cargo'

    def build(self, name='registry', run=None, previous=None):
        output = self.root / name
        version = subject.build_registry(self.sources, output, self.base, PIN, run or RUN, previous)
        return output, version

    def test_metadata_sources_checksums_and_determinism(self):
        output, version = self.build()
        second, _ = self.build('second')
        self.assertEqual('1.10506.123001', version)
        for path in output.rglob('*'):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (second / path.relative_to(output)).read_bytes())
        index = json.loads((output / 'index/du/ck/duckdb').read_text())
        self.assertTrue(all(d['registry'] is None and d['req'] == '=' + version for d in index['deps']))
        for name in subject.CRATES:
            archive = output / f'crates/{name}/{version}/{name}-{version}.crate'
            with tarfile.open(archive) as bundle:
                self.assertFalse(any(p.endswith('/Cargo.lock') for p in bundle.getnames()))
                source = bundle.extractfile(f'{name}-{version}/src/lib.rs').read()
                with tarfile.open(fileobj=io.BytesIO(self.sources[name])) as original:
                    self.assertEqual(original.extractfile(f'{name}-1.10503.1/src/lib.rs').read(), source)
        for line in (output / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())

    def test_retains_old_versions_and_is_idempotent(self):
        first, version = self.build()
        second, newer = self.build('second', {**RUN, 'id': 124}, first)
        entries = [json.loads(line)['vers'] for line in (second / 'index/du/ck/duckdb').read_text().splitlines()]
        self.assertEqual([version, newer], entries)
        third, _ = self.build('third', {**RUN, 'id': 124}, second)
        self.assertEqual((second / 'SHA256SUMS').read_bytes(), (third / 'SHA256SUMS').read_bytes())

    def test_imports_downloaded_artifact_zip(self):
        first, version = self.build()
        archive = self.root / 'saved.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            for path in first.rglob('*'):
                if path.is_file():
                    bundle.write(path, path.relative_to(first).as_posix())
        second, newer = self.build('second', {**RUN, 'id': 124}, archive)
        self.assertTrue((second / f'crates/duckdb/{version}/duckdb-{version}.crate').exists())
        self.assertNotEqual(version, newer)

    def test_rejects_changed_history(self):
        first, _ = self.build()
        (first / 'index/du/ck/duckdb').write_text('corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.build('second', previous=first)

    def test_rejects_insecure_remote_url(self):
        self.base = 'http://example.com/cargo'
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            self.build()

    def test_public_dependencies_and_aliases(self):
        manifest = {'package': {'name': 'duckdb', 'version': '1.0.0'},
                    'dependencies': {'renamed': {'package': 'serde', 'version': '1', 'optional': True}}}
        entry = subject.index_entry(manifest, 'a' * 64, self.base)
        self.assertEqual(subject.CRATES_IO, entry['deps'][0]['registry'])
        self.assertEqual('serde', entry['deps'][0]['package'])
        self.assertEqual(['dep:renamed'], entry['features2']['renamed'])

    @unittest.skipUnless(shutil.which('cargo'), 'Cargo is required for consumer integration')
    def test_cargo_add_authenticated_registry_and_transitive_crates(self):
        requests = []
        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                authorized = self.headers.get('Authorization') == 'Bearer test-token'
                requests.append((self.path, authorized))
                if not authorized:
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Bearer')
                    self.end_headers()
                    return
                super().do_GET()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(self.root / 'registry')))
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_port}'
        output, version = self.build()
        app = self.root / 'app'
        (app / '.cargo').mkdir(parents=True)
        (app / 'src').mkdir()
        (app / 'Cargo.toml').write_text('[package]\nname="consumer"\nversion="0.1.0"\nedition="2021"\n')
        (app / 'src/main.rs').write_text('fn main() { assert_eq!(duckdb::version(), 156); }')
        (app / '.cargo/config.toml').write_bytes((output / 'cargo-client-config.toml').read_bytes())
        env = {**os.environ, 'CARGO_REGISTRIES_PEGASI_TOKEN': 'Bearer test-token', 'RUSTUP_TOOLCHAIN': 'stable',
               'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
               'CARGO_HOME': str(self.root / 'cargo-home'), 'CARGO_TARGET_DIR': str(self.root / 'target')}
        def cargo(*args):
            result = subprocess.run(['cargo', *args], cwd=app, env=env, text=True, capture_output=True, timeout=180)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return result.stdout
        cargo('add', 'duckdb', '--registry', 'pegasi', '--features', 'bundled,loadable-extension')
        metadata = json.loads(cargo('metadata', '--format-version=1'))
        packages = [p for p in metadata['packages'] if p['name'] in subject.CRATES]
        self.assertEqual(3, len(packages))
        self.assertTrue(all(p['version'] == version and self.base in p['source'] for p in packages), packages)
        cargo('run', '--offline')
        self.assertTrue(any(path.endswith('.crate') and authorized for path, authorized in requests))
        self.assertTrue(all(authorized for path, authorized in requests if path.endswith('.crate')))


if __name__ == '__main__':
    unittest.main()
