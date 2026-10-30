# Consume the fork with Cargo

## Manual deployment

Extract this registry artifact ZIP and upload its **contents** to:

```text
{{REGISTRY_BASE_URL}}/
```

Preserve the `index/` and `crates/` directory layout. Upload crate files before
index files. Uploading the ZIP as a single file does not expose a Cargo index.
This is a complete read-only sparse registry snapshot; no Cargo publishing API
or Artifactory Cargo repository is required. The workflow never uploads to
Artifactory and needs no Artifactory credentials.

## Application configuration

Merge `cargo-client-config.toml` into the application's `.cargo/config.toml`,
or your user's Cargo configuration. It registers the name `pegasi` with:

```toml
[registries.pegasi]
index = "{{REGISTRY_INDEX_URL}}"
credential-provider = "cargo:token"
```

Supply read credentials locally, for example through the secret environment
variable `CARGO_REGISTRIES_PEGASI_TOKEN`. Artifactory access-token authentication
expects its value to be `Bearer ` followed by the access token. Do not commit
credentials to the application or include them in the uploaded snapshot.

Then run:

```sh
cargo add duckdb --registry pegasi --features bundled
```

Cargo handles downloads, extraction, transitive fork dependencies and native
compilation. Rust and a C++ toolchain are still required, and normal public Rust
dependencies resolve through crates.io or the application's configured mirror.
No Python setup script, source vendoring, or path dependency is needed.

This snapshot includes version `{{RELEASE_VERSION}}`. To pin it explicitly:

```sh
cargo add duckdb@={{RELEASE_VERSION}} --registry pegasi --features bundled
```

## Release history

Distribution versions encode the DuckDB engine in the minor component and the
native build run/attempt in the patch component. This keeps every verified native
build immutable while `cargo add` selects the latest release. Older versions
from the previous registry snapshot are retained for existing Cargo.lock files.

The packaging workflow imports the most recent retained registry artifact from
GitHub. Do not discard the published registry history or deploy an older index
over a newer one. If GitHub retention removes a previous snapshot, restore a
saved snapshot before preparing the next release. The local builder accepts
`--previous-registry` with an extracted snapshot or its original artifact ZIP.

Applications that already depend on upstream DuckDB through another library
must align that dependency with this fork as well; Cargo permits one native
library with `links = "duckdb"`. This registry routes its own native bindings
and procedural macros to the fork, without exposing fork-specific C APIs.
