# scte-go

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
