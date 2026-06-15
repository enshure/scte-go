#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_PUBLIC_MODULE_PREFIX = "github.com/enshure/scte-go"
MODULE_DIRS = ("common_go", "amps_go", "xponder_go")
REPO_ROOT = Path(__file__).resolve().parents[2]


README = """# scte-go

Generated Go protobuf bindings for Enshure SCTE APIs.

This repository intentionally publishes only generated Go packages. The source
protobuf files and internal generation tooling are maintained separately in
`scte-apis`.

## Generate

Run the exporter from this repository and point it at an `scte-apis` checkout:

```bash
python3 scripts/release/export_public_go.py --api-repo ../scte-apis --version 0.1.0
```

The exporter regenerates `common_go/`, `amps_go/`, and `xponder_go/`, writes
`go.work`, and runs `go test ./...` in each generated module.

## Packages

- `github.com/enshure/scte-go/common_go`
- `github.com/enshure/scte-go/amps_go`
- `github.com/enshure/scte-go/xponder_go`

## Install

```bash
go get github.com/enshure/scte-go/common_go
go get github.com/enshure/scte-go/amps_go
go get github.com/enshure/scte-go/xponder_go
```

## Version Tags

This is a multi-module Go repository. Tag releases with the module directory
prefix:

```bash
git tag common_go/v0.1.0
git tag amps_go/v0.1.0
git tag xponder_go/v0.1.0
git push --tags
```

Consumers can then depend on the specific module version they need.
"""

GO_CI_WORKFLOW = """name: Go

on:
  pull_request:
  push:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        module:
          - common_go
          - amps_go
          - xponder_go
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version-file: ${{ matrix.module }}/go.mod
      - run: go test ./...
        working-directory: ${{ matrix.module }}
        env:
          GOWORK: ${{ github.workspace }}/go.work
"""


def fail(message: str) -> None:
    raise SystemExit(message)


@contextlib.contextmanager
def pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def load_generator(api_repo: Path, public_module_prefix: str):
    generator_path = api_repo / "scripts" / "release" / "go_protobuf_release.py"
    if not generator_path.exists():
        fail(f"Failed to find SCTE API generator: {generator_path}")

    os.environ["GO_MODULE_PREFIX"] = public_module_prefix
    spec = importlib.util.spec_from_file_location("go_protobuf_release_public", generator_path)
    if spec is None or spec.loader is None:
        fail(f"Failed to load {generator_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with pushd(api_repo):
        spec.loader.exec_module(module)
    return module


def normalized_version(version: str) -> str:
    return version if version.startswith("v") else f"v{version}"


def generate_public_modules(dest_root: Path, api_repo: Path, public_module_prefix: str, version: str) -> None:
    generator = load_generator(api_repo, public_module_prefix)
    common_version = normalized_version(version)
    with pushd(api_repo):
        generator.build_common_go_output(dest_root / "common_go")
        for tree_spec in generator.TREE_SPECS:
            generator.build_tree_go_output(
                tree_spec,
                dest_root / str(tree_spec["go_output_dir"]),
                common_version=common_version,
            )


def write_workspace(dest_root: Path, public_module_prefix: str, version: str) -> None:
    lines = [
        "go 1.22",
        "",
        "use ./common_go",
        "use ./amps_go",
        "use ./xponder_go",
    ]
    lines.append("")
    lines.append(f"replace {public_module_prefix}/common_go {normalized_version(version)} => ./common_go")
    lines.append("")
    (dest_root / "go.work").write_text("\n".join(lines))


def write_public_files(dest_root: Path) -> None:
    (dest_root / "README.md").write_text(README)
    (dest_root / ".gitignore").write_text(
        "\n".join(
            [
                ".DS_Store",
                ".cache/",
                "*.test",
                "",
            ]
        )
    )
    workflow_dir = dest_root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "go.yml").write_text(GO_CI_WORKFLOW)


def verify(dest_root: Path) -> None:
    env = os.environ.copy()
    env["GOWORK"] = str((dest_root / "go.work").resolve())
    env["GOCACHE"] = str((dest_root / ".cache" / "go-build").resolve())
    env["GOMODCACHE"] = str((dest_root / ".cache" / "go-mod").resolve())
    for module_dir in MODULE_DIRS:
        subprocess.run(["go", "test", "./..."], cwd=dest_root / module_dir, env=env, check=True)


def export_public_go(
    dest_root: Path,
    api_repo: Path,
    public_module_prefix: str,
    version: str,
    skip_verify: bool,
) -> None:
    if not api_repo.exists():
        fail(f"SCTE API repository does not exist: {api_repo}")
    dest_root.mkdir(parents=True, exist_ok=True)
    generate_public_modules(dest_root, api_repo, public_module_prefix, version)
    write_workspace(dest_root, public_module_prefix, version)
    write_public_files(dest_root)
    if not skip_verify:
        verify(dest_root)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate this public scte-go repo from an scte-apis checkout.")
    parser.add_argument(
        "--api-repo",
        default=REPO_ROOT.parent / "scte-apis",
        type=Path,
        help="Source scte-apis checkout. Defaults to ../scte-apis relative to this repository.",
    )
    parser.add_argument(
        "--dest",
        default=REPO_ROOT,
        type=Path,
        help="Destination public repository checkout. Defaults to this scte-go repository.",
    )
    parser.add_argument(
        "--module-prefix",
        default=DEFAULT_PUBLIC_MODULE_PREFIX,
        help=f"Public Go module prefix. Defaults to {DEFAULT_PUBLIC_MODULE_PREFIX}.",
    )
    parser.add_argument(
        "--version",
        default="0.0.0",
        help="Public module dependency version to write for tree modules.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Copy and rewrite files without running go test.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dest_root = args.dest.resolve()
    api_repo = args.api_repo.resolve()
    export_public_go(dest_root, api_repo, args.module_prefix.rstrip("/"), args.version, args.skip_verify)
    print(f"Exported generated Go modules to {dest_root} from {api_repo}")


if __name__ == "__main__":
    main()
