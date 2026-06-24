#!/usr/bin/env python

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


PACKAGE_NAME = os.environ.get("PACKAGE_NAME", "scte-apis-go-protobuf")
DEFAULT_VERSION_TRACK_PACKAGES = ("scte-apis-go-protobuf",)
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
MANUAL_PIPELINE_SOURCES = {"web", "api"}
PROTOBUF_RUNTIME_VERSION = "v1.36.11"
DEFAULT_GO_MODULE_HOST = "gitlab.com"
COMMON_GO_PACKAGE = "sctecommon"
TREE_GO_PACKAGES = {
    "amps": "scteamp",
    "xponder": "sctexponder",
}
PROTO_PACKAGES = {
    "common": "scte.common",
    "amps": "scte.amp",
    "xponder": "scte.xponder",
}


def resolve_go_module_prefix() -> str:
    explicit = os.environ.get("GO_MODULE_PREFIX")
    if explicit:
        return explicit

    ci_fqdn = os.environ.get("CI_SERVER_FQDN")
    if ci_fqdn:
        return f"{ci_fqdn}/enshure/scte-rd/scte-apis"

    ci_host = os.environ.get("CI_SERVER_HOST")
    if ci_host:
        host = ci_host if "." in ci_host else f"{ci_host}.local"
        return f"{host}/enshure/scte-rd/scte-apis"

    return f"{DEFAULT_GO_MODULE_HOST}/enshure/scte-rd/scte-apis"


GO_MODULE_PREFIX = resolve_go_module_prefix()

COMMON_PROTO_FILES = [
    "device_common.proto",
    "error_common.proto",
    "event_common.proto",
    "pagination.proto",
    "reset_common.proto",
    "sensor_common.proto",
    "status_common.proto",
    "system_common.proto",
    "version_common.proto",
    "vendor_common.proto",
]

COMMON_SPEC = {
    "name": "common",
    "source_dir": Path("proto/common"),
    "release_dir": Path("dist") / "common-<version>",
    "go_output_dir": "common_go",
    "module_path": f"{GO_MODULE_PREFIX}/common_go",
    "proto_files": COMMON_PROTO_FILES,
}

TREE_SPECS = [
    {
        "name": "amps",
        "source_dir": Path("proto/amps"),
        "release_dir": Path("dist") / "amps-<version>",
        "go_output_dir": "amps_go",
        "module_path": f"{GO_MODULE_PREFIX}/amps_go",
        "tree_proto_files": [
            "common.proto",
            "debug.proto",
            "transport.proto",
            "controller.proto",
            "api.proto",
            "system_grp.proto",
            "rf_cfg.proto",
            "rf_status.proto",
            "rf_capabilities_grp.proto",
            "spectrum.proto",
        ],
    },
    {
        "name": "xponder",
        "source_dir": Path("proto/xponder"),
        "release_dir": Path("dist") / "xponder-<version>",
        "go_output_dir": "xponder_go",
        "module_path": f"{GO_MODULE_PREFIX}/xponder_go",
        "tree_proto_files": [
            "transport.proto",
            "controller.proto",
            "api.proto",
            "system.proto",
        ],
    },
]


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def parse_semver(version: str) -> tuple[int, int, int] | None:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def semver_to_str(version: tuple[int, int, int]) -> str:
    return f"{version[0]}.{version[1]}.{version[2]}"


def semver_gt(left: tuple[int, int, int], right: tuple[int, int, int]) -> bool:
    return left > right


def fetch_json(url: str, headers: dict[str, str]) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    next_url = url
    while next_url:
        request = urllib.request.Request(next_url, headers=headers)
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, list):
                results.extend(payload)
            else:
                fail(f"Unexpected package list payload: {payload!r}")

            next_page = response.headers.get("X-Next-Page", "")
            if next_page:
                parsed = urllib.parse.urlsplit(next_url)
                query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
                query["page"] = next_page
                next_url = urllib.parse.urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
                )
            else:
                next_url = ""
    return results


def resolve_version_track_packages() -> tuple[str, ...]:
    raw = os.environ.get("VERSION_TRACK_PACKAGES", ",".join(DEFAULT_VERSION_TRACK_PACKAGES))
    packages = tuple(package.strip() for package in raw.split(",") if package.strip())
    if not packages:
        fail("VERSION_TRACK_PACKAGES must include at least one package name")
    return packages


def resolve_current_version(api_base_url: str, project_id: str, job_token: str) -> tuple[int, int, int]:
    headers = {"JOB-TOKEN": job_token}
    versions: list[tuple[int, int, int]] = []
    for package_name in resolve_version_track_packages():
        packages_url = (
            f"{api_base_url}/projects/{project_id}/packages?package_type=generic"
            f"&package_name={urllib.parse.quote(package_name)}&per_page=100"
        )
        try:
            packages = fetch_json(packages_url, headers)
        except urllib.error.HTTPError as exc:
            fail(f"Failed to query package registry for {package_name}: {exc}")

        versions.extend(
            parsed
            for package in packages
            if isinstance((version := package.get("version")), str)
            and (parsed := parse_semver(version)) is not None
        )
    return max(versions, default=(0, 0, 0))


def compute_next_version(current: tuple[int, int, int]) -> tuple[int, int, int]:
    return (current[0], current[1], current[2] + 1)


def compute_manual_version(current: tuple[int, int, int]) -> tuple[int, int, int]:
    major = os.environ.get("API_VERSION_MAJOR")
    minor = os.environ.get("API_VERSION_MINOR")
    if major is None and minor is None:
        return compute_next_version(current)

    try:
        major_value = current[0] if major is None else int(major)
        if major is not None and minor is None:
            minor_value = 0
        elif major is None and minor is not None:
            minor_value = int(minor)
        elif major is not None and minor is not None:
            minor_value = int(minor)
        else:
            minor_value = 0
    except ValueError as exc:
        fail(f"Invalid manual version override: {exc}")
        raise AssertionError from exc

    candidate = (major_value, minor_value, 0)
    if not semver_gt(candidate, current):
        fail(f"Manual version override must be greater than current published version {semver_to_str(current)}")
    return candidate


def write_go_mod(release_dir: Path, module_path: str, common_version: str = "v0.0.0") -> None:
    release_dir.joinpath("go.mod").write_text("")
    lines = [
        f"module {module_path}",
        "",
        "go 1.22",
        "",
        f"require google.golang.org/protobuf {PROTOBUF_RUNTIME_VERSION}",
    ]
    if module_path != COMMON_SPEC["module_path"]:
        lines.extend(
            [
                f"require {COMMON_SPEC['module_path']} {common_version}",
            ]
        )
    release_dir.joinpath("go.mod").write_text("\n".join(lines) + "\n")


def flatten_generated_outputs(go_output_dir: Path, subdir: str) -> None:
    namespace_dir = go_output_dir / subdir
    if not namespace_dir.exists():
        return
    for pb_file in sorted(namespace_dir.rglob("*.pb.go")):
        shutil.move(str(pb_file), go_output_dir / pb_file.name)
    shutil.rmtree(namespace_dir)


def common_go_package() -> str:
    return f"{COMMON_SPEC['module_path']};{COMMON_GO_PACKAGE}"


def tree_go_package(spec: dict[str, object]) -> str:
    return f"{spec['module_path']};{TREE_GO_PACKAGES[str(spec['name'])]}"


def common_symbols() -> list[str]:
    symbols: set[str] = set()
    for proto_name in COMMON_PROTO_FILES:
        content = COMMON_SPEC["source_dir"].joinpath(proto_name).read_text()
        for match in re.finditer(r"^(?:message|enum)\s+([A-Za-z_]\w*)\s*\{", content, flags=re.MULTILINE):
            symbols.add(match.group(1))
    return sorted(symbols, key=len, reverse=True)


def inject_proto_headers(content: str, proto_package: str, go_package: str) -> str:
    content = re.sub(
        r"^package\s+[A-Za-z_.][\w.]*;\n",
        f"package {proto_package};\n",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if not re.search(r"^package\s+", content, flags=re.MULTILINE):
        content = re.sub(
            r'^(syntax = "proto2";\n)',
            rf"\1\npackage {proto_package};\n",
            content,
            count=1,
            flags=re.MULTILINE,
        )

    go_package_line = f'option go_package = "{go_package}";\n'
    if re.search(r"^option\s+go_package\s*=", content, flags=re.MULTILINE):
        return re.sub(
            r'^option\s+go_package\s*=\s*"[^"]+";\n',
            go_package_line,
            content,
            count=1,
            flags=re.MULTILINE,
        )
    return re.sub(
        r"^(package\s+[A-Za-z_.][\w.]*;\n)",
        rf"\1{go_package_line}",
        content,
        count=1,
        flags=re.MULTILINE,
    )


def rewrite_tree_imports(content: str, namespace: str, tree_proto_files: list[str]) -> str:
    tree_files = set(tree_proto_files)

    def replace_import(match: re.Match[str]) -> str:
        prefix = match.group(1)
        target = match.group(2)
        if target == "common/transport.proto" and "transport.proto" in tree_files:
            return f'{prefix}"{namespace}/transport.proto";'
        if target in tree_files and not target.startswith(f"{namespace}/"):
            return f'{prefix}"{namespace}/{target}";'
        return match.group(0)

    return re.sub(r'^(import(?: public)? )"([^"]+)";', replace_import, content, flags=re.MULTILINE)


def qualify_common_references(content: str, symbols: list[str]) -> str:
    updated = content
    for symbol in symbols:
        updated = re.sub(rf"(?<![\w.]){symbol}(?![\w])", f"scte.common.{symbol}", updated)
    return updated


def unqualify_tree_references(content: str) -> str:
    return re.sub(r"\bscte\.(HRDC_(?:Get|Set)API)\b", r"\1", content)


def demote_public_imports(content: str) -> str:
    return re.sub(r"^import public ", "import ", content, flags=re.MULTILINE)


def build_common_stage(stage_root: Path) -> list[Path]:
    common_root = stage_root / "common"
    common_root.mkdir(parents=True, exist_ok=True)
    staged_files: list[Path] = []
    for proto_name in COMMON_PROTO_FILES:
        dest = common_root / proto_name
        content = COMMON_SPEC["source_dir"].joinpath(proto_name).read_text()
        dest.write_text(inject_proto_headers(content, PROTO_PACKAGES["common"], common_go_package()))
        staged_files.append(dest.relative_to(stage_root))
    return staged_files


def build_tree_stage(stage_root: Path, spec: dict[str, object], shared_symbols: list[str]) -> list[Path]:
    tree_name = str(spec["name"])
    tree_root = stage_root / tree_name
    tree_root.mkdir(parents=True, exist_ok=True)
    staged_files: list[Path] = []
    for proto_name in spec["tree_proto_files"]:
        dest = tree_root / proto_name
        if proto_name in {"api.proto", "transport.proto"}:
            content = Path("proto/common/transport.proto").read_text()
            if proto_name == "api.proto":
                content = Path("proto/common/api.proto").read_text()
        else:
            content = spec["source_dir"].joinpath(proto_name).read_text()
        content = rewrite_tree_imports(content, tree_name, spec["tree_proto_files"])
        content = qualify_common_references(content, shared_symbols)
        content = unqualify_tree_references(content)
        if proto_name == "api.proto":
            content = demote_public_imports(content)
        content = inject_proto_headers(content, PROTO_PACKAGES[tree_name], tree_go_package(spec))
        dest.write_text(content)
        staged_files.append(dest.relative_to(stage_root))
    return staged_files


def common_go_opts(common_files: list[Path]) -> list[str]:
    return ["--go_opt=paths=source_relative"] + [f"--go_opt=M{path}={common_go_package()}" for path in common_files]


def tree_go_opts(spec: dict[str, object], common_files: list[Path], tree_files: list[Path]) -> list[str]:
    opts = ["--go_opt=paths=source_relative"]
    opts.extend(f"--go_opt=M{path}={common_go_package()}" for path in common_files)
    opts.extend(f"--go_opt=M{path}={tree_go_package(spec)}" for path in tree_files)
    return opts


def build_common_go_output(go_output_dir: Path) -> None:
    stage_root = (Path("dist") / ".common_go_stage").resolve()
    if stage_root.exists():
        shutil.rmtree(stage_root)
    if go_output_dir.exists():
        shutil.rmtree(go_output_dir)
    go_output_dir.mkdir(parents=True, exist_ok=True)
    write_go_mod(go_output_dir, str(COMMON_SPEC["module_path"]))
    staged_files = build_common_stage(stage_root)
    subprocess.run(
        [
            "protoc",
            f"--proto_path={stage_root}",
            f"--go_out={go_output_dir}",
            *common_go_opts(staged_files),
            *[str(path) for path in staged_files],
        ],
        check=True,
    )
    flatten_generated_outputs(go_output_dir, "common")


def build_tree_go_output(spec: dict[str, object], go_output_dir: Path, common_version: str = "v0.0.0") -> None:
    stage_root = (Path("dist") / f".{spec['name']}_go_stage").resolve()
    if stage_root.exists():
        shutil.rmtree(stage_root)
    if go_output_dir.exists():
        shutil.rmtree(go_output_dir)
    go_output_dir.mkdir(parents=True, exist_ok=True)
    write_go_mod(go_output_dir, str(spec["module_path"]), common_version=common_version)
    shared_symbols = common_symbols()
    common_files = build_common_stage(stage_root)
    tree_files = build_tree_stage(stage_root, spec, shared_symbols)
    subprocess.run(
        [
            "protoc",
            f"--proto_path={stage_root}",
            f"--go_out={go_output_dir}",
            *tree_go_opts(spec, common_files, tree_files),
            *[str(path) for path in tree_files],
        ],
        check=True,
    )
    flatten_generated_outputs(go_output_dir, str(spec["name"]))


def write_go_work(work_root: Path) -> None:
    lines = [
        "go 1.22",
        "",
        "use ./common_go",
        "use ./amps_go",
        "use ./xponder_go",
        "",
    ]
    work_root.joinpath("go.work").write_text("\n".join(lines))


def add_local_replace(module_dir: Path, target_module: str, replacement: str) -> None:
    subprocess.run(
        ["go", "mod", "edit", "-replace", f"{target_module}={replacement}"],
        cwd=module_dir,
        check=True,
    )


def prepare_verification_workspace(work_root: Path) -> None:
    target_module = str(COMMON_SPEC["module_path"])
    add_local_replace(work_root / "amps_go", target_module, "../common_go")
    add_local_replace(work_root / "xponder_go", target_module, "../common_go")


def verify_go_modules(work_root: Path) -> None:
    env = os.environ.copy()
    env["GOWORK"] = str((work_root / "go.work").resolve())
    for module_name in ("common_go", "amps_go", "xponder_go"):
        subprocess.run(["go", "build", "./..."], cwd=work_root / module_name, env=env, check=True)


def build_release_outputs(version: str) -> None:
    release_modules: dict[str, Path] = {}
    common_release_dir = Path(str(COMMON_SPEC["release_dir"]).replace("<version>", version))
    if common_release_dir.exists():
        shutil.rmtree(common_release_dir)
    common_release_dir.mkdir(parents=True, exist_ok=True)
    release_modules["common_go"] = common_release_dir / "common_go"
    build_common_go_output(release_modules["common_go"])
    common_tag_version = f"v{version}"

    for spec in TREE_SPECS:
        release_dir = Path(str(spec["release_dir"]).replace("<version>", version))
        if release_dir.exists():
            shutil.rmtree(release_dir)
        release_dir.mkdir(parents=True, exist_ok=True)
        module_dir = release_dir / str(spec["go_output_dir"])
        release_modules[str(spec["go_output_dir"])] = module_dir
        build_tree_go_output(spec, module_dir, common_version=common_tag_version)

    workspace_root = Path("dist") / f"go-modules-{version}"
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)
    for module_name, source_dir in release_modules.items():
        copy_tree(source_dir, workspace_root / module_name)
    write_go_work(workspace_root)
    prepare_verification_workspace(workspace_root)
    verify_go_modules(workspace_root)


def resolve_local_module_common_version(version: str | None) -> str:
    if not version:
        return "v0.0.0"
    return version if version.startswith("v") else f"v{version}"


def build_local_output(module_name: str, version: str | None = None) -> None:
    if module_name == "common":
        build_common_go_output(Path("common_go"))
        return
    spec = get_spec(module_name)
    build_tree_go_output(spec, Path(str(spec["go_output_dir"])), common_version=resolve_local_module_common_version(version))


def build_tree_outputs(version: str) -> None:
    build_release_outputs(version)


def get_spec(tree_name: str) -> dict[str, object]:
    for spec in TREE_SPECS:
        if spec["name"] == tree_name:
            return spec
    fail(f"Unknown tree name: {tree_name}")
    raise AssertionError


def generate_local_module(tree_name: str, version: str | None = None) -> None:
    build_local_output(tree_name, version=version)


def copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def tarinfo_for_path(path: Path, arcname: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(arcname)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if path.is_dir():
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
        return info

    info.mode = 0o644
    info.size = path.stat().st_size
    return info


def add_path_to_tar(archive: tarfile.TarFile, path: Path, arcname: str) -> None:
    info = tarinfo_for_path(path, arcname)
    if path.is_dir():
        archive.addfile(info)
        return
    with path.open("rb") as handle:
        archive.addfile(info, handle)


def iter_release_entries(root: Path) -> list[tuple[Path, str]]:
    entries: list[tuple[Path, str]] = [(root, root.name)]
    children = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    entries.extend((path, path.relative_to(root).as_posix()) for path in children if path.is_dir())
    entries.extend((path, path.relative_to(root).as_posix()) for path in children if path.is_file())
    return entries


def package_release(version: str, current_version: str) -> Path:
    dist_dir = Path("dist")
    release_root = dist_dir / f"{PACKAGE_NAME}-{version}"
    if release_root.exists():
        shutil.rmtree(release_root)
    release_root.mkdir(parents=True)
    (release_root / "common").mkdir(parents=True, exist_ok=True)
    (release_root / "amps").mkdir(parents=True, exist_ok=True)
    (release_root / "xponder").mkdir(parents=True, exist_ok=True)

    copy_tree(dist_dir / f"common-{version}" / "common_go", release_root / "common" / "common_go")
    copy_tree(dist_dir / f"amps-{version}" / "amps_go", release_root / "amps" / "amps_go")
    copy_tree(dist_dir / f"xponder-{version}" / "xponder_go", release_root / "xponder" / "xponder_go")

    metadata = {
        "package_name": PACKAGE_NAME,
        "version": version,
        "previous_version": current_version,
    }
    (release_root / "release-metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")

    archive_path = dist_dir / f"{PACKAGE_NAME}-{version}.tar.gz"
    if archive_path.exists():
        archive_path.unlink()
    with archive_path.open("wb") as raw_archive:
        with gzip.GzipFile(fileobj=raw_archive, mode="wb", mtime=0) as gz_archive:
            with tarfile.open(fileobj=gz_archive, mode="w") as archive:
                for path, arcname in iter_release_entries(release_root):
                    add_path_to_tar(archive, path, arcname)
    return archive_path


def upload_archive(archive_path: Path, version: str) -> None:
    api_base_url = os.environ["CI_API_V4_URL"]
    project_id = os.environ["CI_PROJECT_ID"]
    job_token = os.environ["CI_JOB_TOKEN"]
    url = (
        f"{api_base_url}/projects/{project_id}/packages/generic/"
        f"{urllib.parse.quote(PACKAGE_NAME)}/{urllib.parse.quote(version)}/{urllib.parse.quote(archive_path.name)}"
    )
    request = urllib.request.Request(
        url,
        data=archive_path.read_bytes(),
        headers={"JOB-TOKEN": job_token, "Content-Type": "application/gzip"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request) as response:
            if response.status not in {200, 201}:
                fail(f"Unexpected upload response: {response.status}")
    except urllib.error.HTTPError as exc:
        fail(f"Failed to upload archive: {exc}")


def write_release_env(current_version: str, published_version: str, next_version: str, archive_path: Path) -> None:
    lines = [
        f"CURRENT_VERSION={current_version}",
        f"PUBLISHED_VERSION={published_version}",
        f"NEXT_VERSION={next_version}",
        f"RELEASE_ARCHIVE={archive_path.as_posix()}",
    ]
    Path("release.env").write_text("\n".join(lines) + "\n")


def resolve_release_versions(
    api_base_url: str, project_id: str, job_token: str, pipeline_source: str
) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    current_override = os.environ.get("CURRENT_VERSION")
    published_override = os.environ.get("PUBLISHED_VERSION")
    next_override = os.environ.get("NEXT_VERSION")
    override_values = [current_override, published_override, next_override]
    if any(value is not None for value in override_values):
        if not all(value is not None for value in override_values):
            fail("CURRENT_VERSION, PUBLISHED_VERSION, and NEXT_VERSION must be provided together")
        current_version = parse_semver(current_override)
        published_version = parse_semver(published_override)
        next_version = parse_semver(next_override)
        if current_version is None or published_version is None or next_version is None:
            fail("CURRENT_VERSION, PUBLISHED_VERSION, and NEXT_VERSION must be valid semantic versions")
        return current_version, published_version, next_version

    override_present = os.environ.get("API_VERSION_MAJOR") is not None or os.environ.get("API_VERSION_MINOR") is not None
    if override_present and pipeline_source not in MANUAL_PIPELINE_SOURCES:
        fail("API_VERSION_MAJOR/API_VERSION_MINOR overrides are only allowed for manual pipeline sources")

    current_version = resolve_current_version(api_base_url, project_id, job_token)
    published_version = compute_manual_version(current_version) if override_present else compute_next_version(current_version)
    next_version = compute_next_version(published_version)
    return current_version, published_version, next_version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-module", choices=["common", *[str(spec["name"]) for spec in TREE_SPECS]])
    parser.add_argument("--version")
    args = parser.parse_args()

    if args.local_module:
        generate_local_module(args.local_module, version=args.version)
        return

    api_base_url = os.environ.get("CI_API_V4_URL")
    project_id = os.environ.get("CI_PROJECT_ID")
    job_token = os.environ.get("CI_JOB_TOKEN")
    pipeline_source = os.environ.get("CI_PIPELINE_SOURCE", "")

    if not api_base_url or not project_id or not job_token:
        fail("CI_API_V4_URL, CI_PROJECT_ID, and CI_JOB_TOKEN are required")

    current_version, published_version, next_version = resolve_release_versions(
        api_base_url, project_id, job_token, pipeline_source
    )
    version = semver_to_str(published_version)
    next_version_str = semver_to_str(next_version)
    current_version_str = semver_to_str(current_version)

    build_tree_outputs(version)
    archive_path = package_release(version, current_version_str)
    upload_archive(archive_path, version)
    write_release_env(current_version_str, version, next_version_str, archive_path)

    print(f"PUBLISHED_VERSION={version}")
    print(f"NEXT_VERSION={next_version_str}")


if __name__ == "__main__":
    main()
