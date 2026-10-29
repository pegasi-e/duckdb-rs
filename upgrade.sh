#!/bin/bash
# Refresh the pinned fork bundle without changing the public Rust bindings.
set -euo pipefail
cd "$(dirname "$0")"
if [ "$#" -ne 0 ]; then
    echo "Edit duckdb-fork.json and the DuckDB submodule pin before regenerating." >&2
    exit 1
fi
git submodule update --init --checkout
exec python3 crates/libduckdb-sys/update_sources.py
