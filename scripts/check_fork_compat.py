#!/usr/bin/env python3
"""Check that the public Rust bindings can target the pinned DuckDB 1.5 fork."""

import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
native = root / "crates/libduckdb-sys"
pin = json.loads((native / "duckdb-fork.json").read_text())
archive = (native / "duckdb.tar.gz").read_bytes()
expected_digest = (native / "duckdb.tar.gz.sha256").read_text().split()[0]
assert hashlib.sha256(archive).hexdigest() == expected_digest, "Bundle checksum mismatch"

with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
    header = bundle.extractfile("duckdb/src/include/duckdb.h").read().decode()
    version_source = bundle.extractfile(
        "duckdb/src/function/table/version/pragma_version.cpp"
    ).read().decode()

header_version = ".".join(
    re.search(rf"#define DUCKDB_API_VERSION_{part} (\d+)", header).group(1)
    for part in ("MAJOR", "MINOR", "PATCH")
)
assert header_version == pin["version"] == "1.5.6", "Expected the DuckDB 1.5.6 C API"
assert f'#define DUCKDB_VERSION "v{pin["version"]}"' in version_source
assert f'#define DUCKDB_SOURCE_ID "{pin["commit"][:11]}"' in version_source

bindings = (native / "src/bindgen_bundled_version.rs").read_text()
functions = set(re.findall(r"pub fn (duckdb_\w+)\s*\(", bindings))
header_functions = set(re.findall(r"\b(duckdb_\w+)\s*\(", header))
assert functions, "No native Rust declarations found"
assert not functions - header_functions, (
    f"Rust declarations missing from the pinned header: {sorted(functions - header_functions)}"
)
fork_section = header.split("// Anybase API functions", 1)
assert len(fork_section) == 2, "Expected the fork-specific API section"
fork_functions = set(re.findall(r"\b(duckdb_\w+)\s*\(", fork_section[1]))
assert not functions & fork_functions, (
    f"Fork-specific API exposed in Rust: {sorted(functions & fork_functions)}"
)
workspace = (root / "Cargo.toml").read_text()
assert '"crates/duckdb-neo"' not in workspace, "DuckDB v2 wrapper requires a different engine"
print(f"DuckDB {pin['version']} ({pin['commit'][:11]}): {len(functions)} standard C bindings checked")
