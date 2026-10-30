#!/usr/bin/env python3
"""Install the fork from a manual-upload ZIP and configure an application's Cargo files.

Requires Python 3.11+, Cargo, and a C++ toolchain for building bundled DuckDB.
Only downloads are performed; this script never uploads to Artifactory.
"""

import argparse
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

CRATES = ("duckdb", "libduckdb-sys", "duckdb-loadable-macros")
BEGIN = "# BEGIN PEGASI DUCKDB (managed by bootstrap_duckdb.py)"
END = "# END PEGASI DUCKDB"


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def toml(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        destination = urllib.parse.urlparse(new_url)
        if destination.scheme != "https" or destination.username or destination.password:
            raise RuntimeError("Bundle redirects must use HTTPS.")
        redirected = super().redirect_request(request, fp, code, message, headers, new_url)
        if urllib.parse.urlparse(request.full_url).netloc != urllib.parse.urlparse(new_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


def obtain_bundle(source, directory):
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in {"https", "http"}:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError("The bundle file does not exist.")
        return path
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise RuntimeError("Use an HTTPS bundle URL without embedded credentials.")
    headers = {}
    if os.environ.get("DUCKDB_BUNDLE_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["DUCKDB_BUNDLE_TOKEN"].strip()
    target = directory / "bundle.zip"
    try:
        request = urllib.request.Request(source, headers=headers)
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=120) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"Bundle download failed (HTTP {error.code}). Use a downloaded ZIP or set "
            "DUCKDB_BUNDLE_TOKEN locally for authenticated downloads."
        ) from None
    except urllib.error.URLError:
        raise RuntimeError("Bundle download failed. Check connectivity or supply a local ZIP.") from None
    return target


def unpack_source(bundle_path, directory, expected_sha=None):
    bundle_sha = sha256(bundle_path)
    if expected_sha and bundle_sha != expected_sha.lower():
        raise RuntimeError("Bundle SHA-256 does not match --sha256.")
    with zipfile.ZipFile(bundle_path) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("Duplicate entries in the bundle.")
        checksums = {}
        for line in bundle.read("SHA256SUMS").decode().splitlines():
            checksum, name = line.split("  ", 1)
            if not re.fullmatch(r"[0-9a-f]{64}", checksum) or name in checksums or PurePosixPath(name).name != name or "\\" in name:
                raise RuntimeError("Invalid checksum entry in the bundle.")
            checksums[name] = checksum
        for name, expected in checksums.items():
            value = hashlib.sha256()
            with bundle.open(name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            if value.hexdigest() != expected:
                raise RuntimeError(f"Bundle content checksum mismatch: {name}")
        if "archive-manifest.json" not in checksums:
            raise RuntimeError("The archive manifest must have a checksum.")
        manifest = json.loads(bundle.read("archive-manifest.json"))
        files = manifest["files"]
        if len(files) != 4 or len({item["name"] for item in files}) != 4:
            raise RuntimeError("Expected four platform ZIPs.")
        for item in files:
            if checksums.get(item["name"]) != item["sha256"] or not item["name"].endswith(".zip"):
                raise RuntimeError("Platform ZIPs must match the manifest and checksums.")
        # The complete source workspace is architecture-independent. Select one
        # canonical ZIP so the installed source is identical on every host.
        with zipfile.ZipFile(io.BytesIO(bundle.read(sorted(item["name"] for item in files)[0]))) as platform:
            sources = [name for name in platform.namelist() if PurePosixPath(name).name == "duckdb-rs-anybase-source.tar.gz"]
            if len(sources) != 1:
                raise RuntimeError("Expected one complete source workspace archive.")
            source_bytes = platform.read(sources[0])
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(source_bytes), mode="r:gz") as archive:
        entries = archive.getmembers()
        if sum(item.size for item in entries) > 512 * 1024 * 1024:
            raise RuntimeError("Source workspace is unexpectedly large.")
        members = {}
        for item in entries:
            path = PurePosixPath(item.name)
            if path.is_absolute() or ".." in path.parts or "\\" in item.name or ":" in item.name or not path.parts or path.parts[0] != "duckdb-rs-anybase":
                raise RuntimeError("Unsafe source archive path.")
            if item.name in members or not (item.isdir() or item.isfile() or item.issym() or item.islnk()):
                raise RuntimeError("Unsupported source archive entry.")
            members[item.name] = item

        def regular_file(item, seen=None):
            seen = set() if seen is None else seen
            if item.name in seen:
                raise RuntimeError("Cyclic source archive link.")
            seen.add(item.name)
            if item.isfile():
                return item
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(item.name), item.linkname) if item.issym() else item.linkname
            )
            if target not in members or members[target].isdir():
                raise RuntimeError("Source archive link escapes the workspace.")
            return regular_file(members[target], seen)

        for item in entries:
            target = directory.joinpath(*PurePosixPath(item.name).parts)
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                original = regular_file(item)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(original) as stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(original.mode & 0o755)
    source = directory / "duckdb-rs-anybase"
    for name in CRATES:
        if toml(source / "crates" / name / "Cargo.toml")["package"]["name"] != name:
            raise RuntimeError("Unexpected package in the source workspace.")
    native = source / "crates/libduckdb-sys"
    if json.loads((native / "duckdb-fork.json").read_text()) != manifest["duckdb"]:
        raise RuntimeError("The source workspace does not match the bundle's DuckDB provenance.")
    expected_native = (native / "duckdb.tar.gz.sha256").read_text().split()[0]
    if sha256(native / "duckdb.tar.gz") != expected_native:
        raise RuntimeError("The bundled DuckDB source checksum does not match.")
    return source, {"source_commit": manifest["source_commit"], "source_archive_sha256": source_sha,
                    "bundle_sha256": bundle_sha, "duckdb": manifest["duckdb"]}


def configure_root(text, relative_vendor, exclude_path):
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            raise RuntimeError("Malformed managed DuckDB section in Cargo.toml.")
        section = re.search(r"(?m)^" + re.escape(BEGIN) + r"\n([\s\S]*?)^" + re.escape(END) + r"\n?", text)
        if section is None:
            raise RuntimeError("Malformed managed DuckDB section.")
        managed = tomllib.loads(section.group(1))
        managed_patches = managed.get("patch", {}).get("crates-io", {})
        if set(managed) != {"patch"} or set(managed["patch"]) != {"crates-io"} or set(managed_patches) != set(CRATES) or any(set(value) != {"path"} for value in managed_patches.values()):
            raise RuntimeError("The managed DuckDB section contains other Cargo settings; no files were changed.")
        text = text[:section.start()] + text[section.end():]
    parsed = tomllib.loads(text)
    patches = parsed.get("patch", {}).get("crates-io", {})
    if any(name in patches for name in CRATES):
        raise RuntimeError("Existing DuckDB [patch.crates-io] overrides conflict with bootstrap; no project files were changed.")
    if exclude_path:
        if "workspace" not in parsed:
            text += "\n[workspace]\nexclude = " + json.dumps([exclude_path], ensure_ascii=False) + "\n"
        elif exclude_path not in parsed["workspace"].get("exclude", []):
            headers = list(re.finditer(r"(?m)^\s*\[workspace\]\s*(?:#.*)?$", text))
            if len(headers) != 1:
                raise RuntimeError("Cannot safely locate [workspace] to exclude the vendored dependency.")
            start = headers[0].end()
            if "exclude" in parsed["workspace"]:
                end = re.search(r"(?m)^\s*\[", text[start:])
                section_end = start + end.start() if end else len(text)
                fields = list(re.finditer(r"(?m)^\s*(?:exclude|\"exclude\"|'exclude')\s*=\s*\[", text[start:section_end]))
                if len(fields) != 1:
                    raise RuntimeError("Cannot safely update workspace.exclude.")
                index = start + fields[0].end()
                addition = json.dumps(exclude_path, ensure_ascii=False)
                if parsed["workspace"]["exclude"]:
                    addition += ", "
                text = text[:index] + addition + text[index:]
            else:
                text = text[:start] + "\nexclude = " + json.dumps([exclude_path], ensure_ascii=False) + text[start:]
    block = [BEGIN]
    for name in CRATES:
        block += [f"[patch.crates-io.{name}]", "path = " + json.dumps(f"{relative_vendor}/crates/{name}", ensure_ascii=False), ""]
    text = text.rstrip() + "\n\n" + "\n".join(block) + END + "\n"
    updated = tomllib.loads(text)
    if exclude_path and exclude_path not in updated["workspace"]["exclude"]:
        raise RuntimeError("Failed to configure workspace exclusion safely.")
    return text


def install(args):
    if not shutil.which("cargo"):
        raise RuntimeError("Install Rust/Cargo before running this bootstrap.")
    manifest = args.manifest_path.resolve()
    application = toml(manifest)
    if "package" not in application:
        raise RuntimeError("Point --manifest-path at an application/package Cargo.toml, not a virtual workspace root.")
    root_manifest = Path(subprocess.check_output(
        ["cargo", "locate-project", "--workspace", "--message-format", "plain", "--manifest-path", str(manifest)],
        cwd=manifest.parent, text=True,
    ).strip()).resolve()
    root = root_manifest.parent
    with tempfile.TemporaryDirectory(prefix="duckdb-bootstrap-") as temporary:
        staging = Path(temporary)
        bundle = obtain_bundle(args.bundle, staging)
        source, info = unpack_source(bundle, staging, args.sha256)
        if not re.fullmatch(r"[0-9a-f]{40}", info["source_commit"]):
            raise RuntimeError("Invalid Rust source commit in the bundle.")
        container = (args.vendor_dir if args.vendor_dir else root / "vendor").resolve()
        vendor = container / ("duckdb-rs-" + info["source_commit"][:12])
        try:
            relative = Path(os.path.relpath(vendor, root)).as_posix()
        except ValueError:  # An explicitly selected vendor directory on another Windows drive.
            relative = vendor.as_posix()
        new_container = not container.exists()
        exclusion = None
        if vendor.is_relative_to(root):
            existing_excludes = toml(root_manifest).get("workspace", {}).get("exclude", [])
            container_excluded = container.relative_to(root).as_posix() in existing_excludes
            exclusion = (container if new_container or container_excluded else vendor).relative_to(root).as_posix()
        root_text = root_manifest.read_text(encoding="utf-8")
        # Give cargo add an existing dependency table. Otherwise toml_edit may
        # insert a new table before the trailing END comment and accidentally
        # place unrelated dependencies inside our managed patch section.
        if root_manifest == manifest and "dependencies" not in application:
            root_text += "\n[dependencies]\n"
        configured = configure_root(root_text, relative, exclusion)
        workspace_dependencies = toml(root_manifest).get("workspace", {}).get("dependencies", {})
        choices = []
        for name, value in application.get("dependencies", {}).items():
            inherited_value = workspace_dependencies.get(name, {}) if isinstance(value, dict) and value.get("workspace") else {}
            if name == "duckdb" or any(isinstance(dep, dict) and dep.get("package") == "duckdb" for dep in [value, inherited_value]):
                choices.append((name, value, inherited_value))
        if len(choices) > 1:
            raise RuntimeError("Multiple DuckDB dependency aliases found; choose one before bootstrapping.")
        alias, existing, inherited = choices[0] if choices else ("duckdb", {}, {})
        features = {"bundled"}
        for dependency in [existing, inherited]:
            if isinstance(dependency, dict):
                features.update(dependency.get("features", []))
        cmake_features = features & {"bundled-cmake", "autocomplete", "icu", "tpcds", "tpch"}
        if cmake_features and not (source / "crates/libduckdb-sys/duckdb-sources/CMakeLists.txt").exists():
            raise RuntimeError(
                "This bundle lacks the full DuckDB checkout needed for CMake-only features: "
                + ", ".join(sorted(cmake_features)) + ". Use bundled/json/parquet or a full source checkout."
            )
        snapshots = {path: path.read_bytes() if path.exists() else None
                     for path in {root_manifest, manifest, root / "Cargo.lock"}}
        created = False
        try:
            if vendor.exists():
                marker = vendor / ".duckdb-bootstrap.json"
                if not marker.is_file() or json.loads(marker.read_text()).get("source_archive_sha256") != info["source_archive_sha256"]:
                    raise RuntimeError("The vendor directory already exists with different contents; choose another --vendor-dir.")
            else:
                container.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(vendor))
                created = True
                (vendor / ".duckdb-bootstrap.json").write_text(json.dumps(info, indent=2) + "\n")
            root_manifest.write_text(configured, encoding="utf-8")
            command = ["cargo", "add", "duckdb", "--path", str(vendor / "crates/duckdb"),
                       "--features", ",".join(sorted(features)), "--manifest-path", str(manifest)]
            if alias != "duckdb":
                command += ["--rename", alias]
            if any(isinstance(dep, dict) and dep.get("default-features") is False for dep in [existing, inherited]):
                command.append("--no-default-features")
            if args.offline:
                command.append("--offline")
            subprocess.run(command, cwd=root, check=True)
            configure_root(root_manifest.read_text(encoding="utf-8"), relative, exclusion)
            command = ["cargo", "metadata", "--format-version", "1", "--manifest-path", str(manifest)]
            if args.offline:
                command.append("--offline")
            metadata = json.loads(subprocess.check_output(command, cwd=root, text=True))
            found = set()
            for package in metadata["packages"]:
                if package["name"] in CRATES:
                    found.add(package["name"])
                    expected = vendor / "crates" / package["name"] / "Cargo.toml"
                    if package["source"] is not None or Path(package["manifest_path"]).resolve() != expected.resolve():
                        raise RuntimeError("Cargo resolved a different DuckDB package; the project's previous Cargo files have been restored.")
            if not {"duckdb", "libduckdb-sys"}.issubset(found):
                raise RuntimeError("Cargo did not resolve the bundled fork correctly.")
        except Exception:
            for path, contents in snapshots.items():
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(contents)
            if created:
                shutil.rmtree(vendor)
            if new_container and container.exists() and not any(container.iterdir()):
                container.rmdir()
            raise
    print(f"Configured DuckDB {info['duckdb']['version']} ({info['duckdb']['commit'][:11]}) for {manifest}")
    print("Keep the managed vendor directory, or rerun this bootstrap before Cargo on a fresh checkout.")
    if args.build:
        command = ["cargo", "build", "--manifest-path", str(manifest)]
        if args.offline:
            command.append("--offline")
        subprocess.run(command, cwd=root, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, help="Local manual-upload ZIP or HTTPS URL")
    parser.add_argument("--manifest-path", type=Path, default=Path("Cargo.toml"))
    parser.add_argument("--vendor-dir", type=Path, help="Optional parent directory for managed fork sources")
    parser.add_argument("--sha256", help="Optional expected SHA-256 of the complete bundle")
    parser.add_argument("--offline", action="store_true", help="Resolve Rust dependencies using Cargo's local cache")
    parser.add_argument("--build", action="store_true", help="Build the application after configuring it")
    args = parser.parse_args()
    try:
        install(args)
    except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        sys.exit(f"DuckDB bootstrap failed: {error}")


if __name__ == "__main__":
    main()
