#!/usr/bin/env python3
"""Build an upload-ready, read-only Cargo sparse registry without publishing it."""

import argparse
import copy
import datetime
import gzip
import hashlib
import io
import json
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

CRATES = ("libduckdb-sys", "duckdb-loadable-macros", "duckdb")
CRATES_IO = "https://github.com/rust-lang/crates.io-index"
DEFAULT_BASE_URL = "https://gart.software.gevernova.com/artifactory/data-fabric-generic/duckdb-rs/cargo"
ARCHIVE_MTIME = 946684800


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def index_path(name):
    name = name.lower()
    if len(name) < 3:
        return f"{len(name)}/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def release_version(pin, run_id, attempt):
    parts = pin["version"].lstrip("v").split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError("Expected a three-component DuckDB version.")
    major, minor, patch = map(int, parts)
    if minor >= 100 or patch >= 100 or run_id <= 0 or not 1 <= attempt <= 999:
        raise ValueError("Invalid release version inputs.")
    build = run_id * 1000 + attempt
    if build >= 2**64:
        raise ValueError("Native build ID is too large for a Cargo version.")
    return f"1.{major * 10000 + minor * 100 + patch}.{build}"


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_value(key)} = {toml_value(item)}" for key, item in value.items()) + " }"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    raise ValueError(f"Unsupported normalized Cargo manifest type: {type(value).__name__}")


def dependency_tables(manifest):
    for key, kind in [("dependencies", "normal"), ("build-dependencies", "build"), ("dev-dependencies", "dev")]:
        yield manifest.get(key, {}), kind, None
    for target, tables in manifest.get("target", {}).items():
        for key, kind in [("dependencies", "normal"), ("build-dependencies", "build"), ("dev-dependencies", "dev")]:
            yield tables.get(key, {}), kind, target


def prepare_manifest(original, version, index_url, pin, run):
    manifest = copy.deepcopy(original)
    manifest["package"]["version"] = version
    manifest["package"].setdefault("metadata", {})["pegasi"] = {
        "duckdb-version": pin["version"], "duckdb-commit": pin["commit"],
        "native-run-id": run["id"], "native-run-attempt": run["run_attempt"],
        "source-package-version": original["package"]["version"],
    }
    for table, _, _ in dependency_tables(manifest):
        for alias, dependency in table.items():
            if not isinstance(dependency, dict):
                raise ValueError("Expected a normalized Cargo package manifest.")
            package = dependency.get("package", alias)
            if package in CRATES:
                dependency["version"] = "=" + version
                dependency["registry-index"] = index_url
                dependency.pop("registry", None)
            if any(key in dependency for key in ["path", "git", "workspace", "registry"]):
                raise ValueError(f"Dependency {alias} is not normalized for registry distribution.")
    return manifest


def index_entry(manifest, digest, index_url):
    dependencies = []
    optional = set()
    for table, kind, target in dependency_tables(manifest):
        for alias, dependency in table.items():
            registry = dependency.get("registry-index", CRATES_IO)
            if dependency.get("optional", False):
                optional.add(alias)
            dependencies.append({
                "name": alias, "req": dependency["version"],
                "features": dependency.get("features", []),
                "optional": dependency.get("optional", False),
                "default_features": dependency.get("default-features", True),
                "target": target, "kind": kind,
                "registry": None if registry == index_url else registry,
                "package": dependency.get("package"),
            })
    features = copy.deepcopy(manifest.get("features", {}))
    explicit = {value[4:] for values in features.values() for value in values if value.startswith("dep:")}
    for alias in optional - explicit - features.keys():
        features[alias] = ["dep:" + alias]
    extended = {name: values for name, values in features.items()
                if any(value.startswith("dep:") or "?/" in value for value in values)}
    simple = {name: values for name, values in features.items() if name not in extended}
    return {"name": manifest["package"]["name"], "vers": manifest["package"]["version"],
            "deps": dependencies, "cksum": digest, "features": simple,
            "features2": extended, "yanked": False, "links": manifest["package"].get("links"),
            "rust_version": manifest["package"].get("rust-version"), "v": 2}


def repackage(data, name, version, index_url, pin, run):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
        members = source.getmembers()
        manifests = [item for item in members if PurePosixPath(item.name).name == "Cargo.toml"
                     and len(PurePosixPath(item.name).parts) == 2]
        if len(manifests) != 1:
            raise ValueError("Expected one normalized package Cargo.toml.")
        original = tomllib.loads(source.extractfile(manifests[0]).read().decode())
        if original["package"]["name"] != name:
            raise ValueError("Unexpected source crate name.")
        old_root = f"{name}-{original['package']['version']}"
        manifest = prepare_manifest(original, version, index_url, pin, run)
        serialized = "# Generated registry distribution metadata; Rust and native sources are unchanged.\n"
        serialized += "\n".join(f"{toml_value(key)} = {toml_value(value)}" for key, value in manifest.items()) + "\n"
        if tomllib.loads(serialized) != manifest:
            raise ValueError("Cargo manifest serialization changed its meaning.")
        output = io.BytesIO()
        with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for item in sorted(members, key=lambda entry: entry.name):
                    path = PurePosixPath(item.name)
                    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != old_root or not item.isfile():
                        raise ValueError("Unexpected source crate archive entry.")
                    relative = PurePosixPath(*path.parts[1:]).as_posix()
                    # Library consumers resolve their own lockfile. The native
                    # build's lockfile contains the original registry identities.
                    if relative == "Cargo.lock":
                        continue
                    content = serialized.encode() if relative == "Cargo.toml" else source.extractfile(item).read()
                    entry = tarfile.TarInfo(f"{name}-{version}/{relative}")
                    entry.size, entry.mtime = len(content), ARCHIVE_MTIME
                    entry.mode = 0o755 if item.mode & 0o111 else 0o644
                    archive.addfile(entry, io.BytesIO(content))
    return output.getvalue(), manifest


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError("Unsafe registry snapshot path.")
    return path


def import_previous(previous, output, base_url):
    if previous is None:
        return []
    if previous.is_dir():
        read = lambda name: previous.joinpath(*safe_name(name).parts).read_bytes()
        close = lambda: None
    else:
        archive = zipfile.ZipFile(previous)
        read = archive.read
        close = archive.close
    try:
        sums = {}
        for line in read("SHA256SUMS").decode().splitlines():
            digest, name = line.split("  ", 1)
            safe_name(name)
            if name in sums or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid previous registry checksums.")
            sums[name] = digest
        if "registry-manifest.json" not in sums:
            raise ValueError("Previous registry manifest is not checksummed.")
        contents = {name: read(name) for name in sums}
        if any(checksum(data) != sums[name] for name, data in contents.items()):
            raise ValueError("Previous registry snapshot checksum mismatch.")
        manifest = json.loads(contents["registry-manifest.json"])
        if manifest["base_url"] != base_url:
            raise ValueError("Cannot merge registry snapshots with different base URLs.")
        for name, data in contents.items():
            target = output.joinpath(*safe_name(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return manifest["releases"]
    finally:
        close()


def build_registry(crate_files, output, base_url, pin, run, previous=None, auth_required=True):
    base_url = base_url.rstrip("/")
    parsed = urlparse(base_url)
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})) or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Registry base URL must use HTTPS (HTTP is allowed only for local tests).")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Registry output directory must be empty.")
    output.mkdir(parents=True, exist_ok=True)
    index_url = "sparse+" + base_url + "/index/"
    releases = import_previous(previous, output, base_url)
    version = release_version(pin, int(run["id"]), int(run["run_attempt"]))
    records = []
    for name in CRATES:
        data, manifest = repackage(crate_files[name], name, version, index_url, pin, run)
        relative = f"crates/{name}/{version}/{name}-{version}.crate"
        target = output / relative
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Immutable Cargo version {name} {version} has different contents. Use a new native build.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entry = index_entry(manifest, checksum(data), index_url)
        path = output / "index" / index_path(name)
        old_entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        matching = [item for item in old_entries if item["vers"] == version]
        if matching and (len(matching) != 1 or matching[0] != entry):
            raise ValueError("Cannot change an existing Cargo index version.")
        if not matching:
            old_entries.append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in old_entries))
        records.append({"name": name, "version": version, "path": relative,
                        "sha256": checksum(data), "source_crate_sha256": checksum(crate_files[name])})
    config = output / "index/config.json"
    config.write_text(json.dumps({"dl": base_url + "/crates/{crate}/{version}/{crate}-{version}.crate",
                                  "auth-required": auth_required}, indent=2) + "\n")
    release = {"version": version, "native_run_id": run["id"], "native_run_attempt": run["run_attempt"],
               "source_commit": run["head_sha"], "source_url": run["html_url"], "duckdb": pin, "crates": records}
    existing = [item for item in releases if item["version"] == version]
    if existing and existing != [release]:
        raise ValueError("Release provenance conflicts with an existing version.")
    if not existing:
        releases.append(release)
    (output / "registry-manifest.json").write_text(json.dumps({"schema": 1, "base_url": base_url,
        "index_url": index_url, "releases": releases}, indent=2) + "\n")
    (output / "cargo-client-config.toml").write_text(
        f'[registries.pegasi]\nindex = {json.dumps(index_url)}\ncredential-provider = "cargo:token"\n'
    )
    guide = (Path(__file__).parent / "CARGO_REGISTRY_SETUP.md").read_text()
    (output / "CARGO_REGISTRY_SETUP.md").write_text(guide.replace("{{REGISTRY_BASE_URL}}", base_url).replace("{{REGISTRY_INDEX_URL}}", index_url).replace("{{RELEASE_VERSION}}", version))
    paths = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (output / "SHA256SUMS").write_text("".join(f"{checksum(path.read_bytes())}  {path.relative_to(output).as_posix()}\n" for path in paths))
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crates-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--run-info", type=Path, required=True)
    parser.add_argument("--fork-pin", type=Path, required=True)
    parser.add_argument("--previous-registry", type=Path)
    args = parser.parse_args()
    crates = {}
    for name in CRATES:
        candidates = list(args.crates_dir.glob(name + "-[0-9]*.crate"))
        if len(candidates) != 1:
            parser.error(f"Expected exactly one package for {name}")
        crates[name] = candidates[0].read_bytes()
    version = build_registry(crates, args.output_dir, args.base_url,
        json.loads(args.fork_pin.read_text()), json.loads(args.run_info.read_text()), args.previous_registry)
    print(f"Prepared Cargo registry version {version} at {args.output_dir}; nothing was uploaded.")


if __name__ == "__main__":
    main()
