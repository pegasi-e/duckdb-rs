#!/usr/bin/env python3
"""Copy successful GitHub build artifact ZIPs unchanged to a Generic repository."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse

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


def upload(path, url, token):
    # Never propagate CalledProcessError here: its command includes the token.
    result = subprocess.run(
        ["curl", "--silent", "--show-error", "--fail", "--retry", "3",
         "--connect-timeout", "30", "--max-time", "600",
         "--request", "PUT", "--upload-file", str(path),
         "--header", f"Authorization: Bearer {token}",
         "--header", f"X-Checksum-Sha256: {digest(path)}",
         "--output", os.devnull, "--write-out", "%{http_code}", url],
        capture_output=True, text=True,
    )
    status = result.stdout.strip()
    if result.returncode or status not in {"200", "201"}:
        raise RuntimeError(
            f"Upload failed for {path.name} (HTTP {status or 'unavailable'}, "
            f"curl exit {result.returncode}). Check connectivity and deploy permission."
        )


def archive(args):
    token = os.environ.get("ARTIFACTORY_ACCESS_TOKEN", "").strip()
    if not args.dry_run and not token:
        raise RuntimeError("Configure GitHub Actions secret ARTIFACTORY_ACCESS_TOKEN before uploading.")
    base = os.environ.get("ARTIFACTORY_BASE_URL", "https://gart.software.gevernova.com/artifactory").rstrip("/")
    repo = os.environ.get("ARTIFACTORY_GENERIC_REPOSITORY", "data-fabric-generic")
    prefix = os.environ.get("ARTIFACTORY_ARCHIVE_PREFIX", "duckdb-rs").strip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("ARTIFACTORY_BASE_URL must be an HTTPS base URL without credentials or query parameters.")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) or part in {".", ".."}
           for part in [repo, *prefix.split("/")]):
        raise RuntimeError("Invalid Generic repository key or archive prefix.")
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

    output = args.output_dir / str(args.run_id)
    output.mkdir(parents=True, exist_ok=True)
    destination = f"{base}/{repo}/{prefix}/{sha}/run-{args.run_id}/attempt-{int(run['run_attempt'])}"
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
        "source_commit": sha, "duckdb": fork_pin, "destination": destination,
        "files": files}, indent=2) + "\n")
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in files)
                         + f"{digest(manifest)}  {manifest.name}\n")
    paths = [output / item["name"] for item in files] + [manifest, checksums]
    if not args.dry_run:
        for path in paths:
            upload(path, f"{destination}/{path.name}", token)
            print(f"Uploaded {path.name}")
    result = f"{'Dry run: verified' if args.dry_run else 'Archived'} {len(files)} original ZIPs.\nDestination: {destination}"
    print(result)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(f"{result}\n\nSource: {run['html_url']}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "pegasi-e/duckdb-rs"))
    parser.add_argument("--output-dir", type=Path, default=Path("target/artifactory-archive"))
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("ARCHIVE_DRY_RUN", "").lower() == "true")
    args = parser.parse_args()
    if args.run_id <= 0:
        parser.error("--run-id must be positive")
    try:
        archive(args)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
