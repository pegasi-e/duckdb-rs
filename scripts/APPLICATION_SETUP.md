# Use the DuckDB fork in an application

`bootstrap_duckdb.py` is a standalone Python 3.11+ file. Copy it into your
application or extract it from the manual-upload bundle. Rust/Cargo and a C++
toolchain must already be installed; the helper does not install system tools.

Run this against the ZIP downloaded from the Generic repository:

```sh
python3 bootstrap_duckdb.py \
  --bundle /path/to/duckdb-rs-manual-upload-RUN-attempt-1.zip \
  --manifest-path /path/to/application/Cargo.toml
```

The helper verifies the bundle checksums and engine provenance, extracts the
complete source workspace, and places it under your workspace's
`vendor/duckdb-rs-<source-commit>` directory. It uses `cargo add` to configure the
application's path dependency with `bundled`, retaining existing features and
an existing dependency alias. It adds matching `[patch.crates-io]` entries and
a workspace exclusion, then checks that Cargo resolves the DuckDB crates to the
vendored fork. In a workspace, select the application's member `Cargo.toml`,
rather than a virtual workspace root.

After setup, run Cargo normally. Add `--build` to the command above to build the
application immediately. For an offline environment, use `--offline` after the
application's ordinary Rust dependencies have been cached or vendored.
The self-contained bundle supports `bundled`, `json`, and `parquet`. CMake-only
features such as `icu` require a full DuckDB checkout and are reported before
changing the application if that checkout is absent.

An HTTPS URL is also accepted by `--bundle`. For authenticated downloads, set
`DUCKDB_BUNDLE_TOKEN` in your local environment or CI secret store; it is sent as
a bearer token and is not written to Cargo files. You can instead download the
ZIP with your usual authenticated tools and supply the local filename.
`--sha256 HEX` optionally pins the complete ZIP's SHA-256 from a trusted source.

Keep the generated vendor directory with the application, or rerun this helper
before Cargo on each fresh checkout. The helper can be run repeatedly. It does
not overwrite unknown vendor directories or existing conflicting DuckDB patches;
configuration failures restore the previous Cargo manifests and lockfile. It
does not change application source files or upload anything to Artifactory.
