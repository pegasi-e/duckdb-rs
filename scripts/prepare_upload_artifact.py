#!/usr/bin/env python3
"""Prepare a Cargo sparse registry from verified native builds for manual upload."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from build_sparse_registry import CRATES, DEFAULT_BASE_URL, build_registry

ARTIFACT_NAMES = {
    "duckdb-rs-fork-Linux-ARM64", "duckdb-rs-fork-Linux-X64",
    "duckdb-rs-fork-macOS-ARM64", "duckdb-rs-fork-Windows-X64",
}


def gh_json(endpoint):
    return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True))


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def previous_registry(args):
    """Keep the latest successful snapshot, including all older crate versions."""
    if args.previous_registry:
        return args.previous_registry
    page = 1
    while True:
        response = gh_json(f"repos/{args.repository}/actions/artifacts?per_page=100&page={page}")
        candidates = sorted(response["artifacts"], key=lambda item: item["id"], reverse=True)
        for item in candidates:
            if not item["name"].startswith("duckdb-rs-cargo-registry-"):
                continue
            run_id = item["workflow_run"]["id"]
            if str(run_id) == os.environ.get("GITHUB_RUN_ID"):
                continue
            run = gh_json(f"repos/{args.repository}/actions/runs/{run_id}")
            if run["status"] != "completed" or run["conclusion"] != "success":
                continue
            if run["path"].split("@")[0] != ".github/workflows/release.yaml":
                continue
            if item["expired"]:
                raise RuntimeError("Previous Cargo registry artifact expired. Restore a saved snapshot with --previous-registry; history must not be reset.")
            path = args.output_dir / "previous-registry.zip"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                subprocess.run(["gh", "api", f"repos/{args.repository}/actions/artifacts/{int(item['id'])}/zip"], stdout=stream, check=True)
            if item.get("digest") and item["digest"] != "sha256:" + digest(path):
                raise RuntimeError("Previous registry artifact digest mismatch.")
            return path
        if len(candidates) < 100:
            return None
        page += 1


def prepare(args):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise RuntimeError("Invalid GitHub repository name.")

    api = f"repos/{args.repository}/actions/runs/{args.run_id}"
    run = gh_json(api)
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise RuntimeError("Only completed, successful workflow runs can be archived.")
    if run["path"].split("@")[0] != ".github/workflows/rust.yaml":
        raise RuntimeError("The source run must use .github/workflows/rust.yaml.")
    sha = run["head_sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Invalid source commit.")
    artifacts = []
    page = 1
    while True:
        batch = gh_json(f"{api}/artifacts?per_page=100&page={page}")["artifacts"]
        artifacts.extend(item for item in batch if item["name"] in ARTIFACT_NAMES)
        if len(batch) < 100:
            break
        page += 1
    if len(artifacts) != len(ARTIFACT_NAMES) or {item["name"] for item in artifacts} != ARTIFACT_NAMES:
        raise RuntimeError("The source run must contain all four platform artifacts exactly once.")
    if any(item["expired"] for item in artifacts):
        raise RuntimeError("A source artifact has expired; use a successful run with retained artifacts.")

    output = args.output_dir / str(args.run_id) / f"attempt-{int(run['run_attempt'])}"
    output.mkdir(parents=True, exist_ok=True)
    files = []
    fork_pin = None
    for item in sorted(artifacts, key=lambda entry: entry["name"]):
        path = output / f"{item['name']}.zip"
        endpoint = f"repos/{args.repository}/actions/artifacts/{int(item['id'])}/zip"
        with path.open("wb") as stream:
            subprocess.run(["gh", "api", endpoint], stdout=stream, check=True)
        checksum = digest(path)
        if item.get("digest") and item["digest"] != f"sha256:{checksum}":
            raise RuntimeError(f"GitHub digest mismatch for {path.name}.")
        with zipfile.ZipFile(path) as bundle:
            if bundle.testzip() is not None:
                raise RuntimeError(f"Corrupt ZIP: {path.name}.")
            pins = [name for name in bundle.namelist() if name.endswith("/duckdb-fork.json") or name == "duckdb-fork.json"]
            if len(pins) != 1:
                raise RuntimeError(f"Expected one DuckDB provenance file in {path.name}.")
            pin = json.loads(bundle.read(pins[0]))
            if fork_pin is not None and fork_pin != pin:
                raise RuntimeError("Platform artifacts disagree about the DuckDB source pin.")
            fork_pin = pin
        files.append({"name": path.name, "github_artifact_id": item["id"],
                      "sha256": checksum, "size": path.stat().st_size})
        print(f"Verified {path.name}: {checksum}")

    manifest = output / "archive-manifest.json"
    manifest.write_text(json.dumps({"repository": args.repository, "source_run_id": args.run_id,
        "source_run_attempt": run["run_attempt"], "source_url": run["html_url"],
        "source_commit": sha, "duckdb": fork_pin,
        "files": files}, indent=2) + "\n")
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in files)
                         + f"{digest(manifest)}  {manifest.name}\n")
    expected_pin = json.loads((Path(__file__).resolve().parent.parent / "crates/libduckdb-sys/duckdb-fork.json").read_text())
    if fork_pin != expected_pin:
        raise RuntimeError("Native artifacts do not match this checkout's DuckDB fork pin.")
    crate_files = {}
    with zipfile.ZipFile(output / (sorted(ARTIFACT_NAMES)[0] + ".zip")) as bundle:
        for name in CRATES:
            matches = [entry for entry in bundle.namelist()
                       if re.fullmatch(re.escape(name) + r"-[0-9][^/]*\.crate", Path(entry).name)]
            if len(matches) != 1:
                raise RuntimeError(f"Expected exactly one {name} source crate in the native artifact.")
            crate_files[name] = bundle.read(matches[0])
    registry_output = args.output_dir / "registry" / str(args.run_id) / f"attempt-{int(run['run_attempt'])}"
    version = build_registry(crate_files, registry_output, args.base_url, fork_pin,
                             {**run, "id": args.run_id}, previous_registry(args))
    # Native ZIPs remain available on their original workflow. Carry their
    # verified checksums and platform provenance into this smaller snapshot.
    (registry_output / "native-artifacts.json").write_bytes(manifest.read_bytes())
    sums = registry_output / "SHA256SUMS"
    paths = sorted(path for path in registry_output.rglob("*") if path.is_file() and path != sums)
    sums.write_text("".join(f"{digest(path)}  {path.relative_to(registry_output).as_posix()}\n" for path in paths))
    output = registry_output
    artifact_name = f"duckdb-rs-cargo-registry-{args.run_id}-attempt-{int(run['run_attempt'])}"
    print(f"Prepared Cargo registry {version} for manual upload: {output}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as outputs:
            outputs.write(f"directory={output}\nartifact_name={artifact_name}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(
                f"Prepared **{artifact_name}** from [native build {args.run_id}]({run['html_url']}).\n\n"
                f"Registry version: **{version}**.\n\n"
                "Extract this workflow's artifact ZIP and upload its contents manually to the Generic repository, "
                "preserving the index/ and crates/ directories. Follow CARGO_REGISTRY_SETUP.md in the artifact. "
                "No Artifactory upload is performed by this workflow.\n"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--previous-registry", type=Path, help="Restore registry history from a saved snapshot directory or ZIP")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "pegasi-e/duckdb-rs"))
    parser.add_argument("--output-dir", type=Path, default=Path("target/manual-upload"))
    args = parser.parse_args()
    if args.run_id <= 0:
        parser.error("--run-id must be positive")
    try:
        prepare(args)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
