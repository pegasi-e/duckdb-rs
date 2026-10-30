#!/usr/bin/env python3
"""Prepare verified native-build ZIPs for a later manual upload."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

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
    artifact_name = f"duckdb-rs-manual-upload-{args.run_id}-attempt-{int(run['run_attempt'])}"
    print(f"Prepared {len(files)} verified platform ZIPs for manual upload: {output}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as outputs:
            outputs.write(f"directory={output}\nartifact_name={artifact_name}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(
                f"Prepared **{artifact_name}** from [native build {args.run_id}]({run['html_url']}).\n\n"
                "Download this workflow's artifact ZIP and upload it manually to the Generic repository. "
                "It contains the four original platform ZIPs, archive-manifest.json, and SHA256SUMS.\n"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "pegasi-e/duckdb-rs"))
    parser.add_argument("--output-dir", type=Path, default=Path("target/manual-upload"))
    args = parser.parse_args()
    if args.run_id <= 0:
        parser.error("--run-id must be positive")
    try:
        prepare(args)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
