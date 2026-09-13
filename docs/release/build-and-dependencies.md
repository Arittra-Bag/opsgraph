# Reproducible maintainer builds — 0.1.0b1

These instructions prepare unsigned artifacts; they do not publish them or
establish platform support. Record the source revision, source inventory, tool
versions, artifact hashes and validation outcomes with each release. See the
[support matrix](support-matrix.md) before advertising an installation path.

## Source identity and public documentation

Build from the intended reviewed source snapshot. A commit ID alone does not
identify uncommitted changes: retain the exact source bytes and their inventory.
[`scripts/build_release.py`](../../scripts/build_release.py) records each allowed
tracked or nonignored untracked product file's path, byte length and SHA-256,
using the current file contents. Deleted files are absent. The inventory is
scoped to product inputs; it is not an inventory of the developer's machine.

Documentation is opt-in: the `tool.hatch.build.targets.sdist.force-include`
allowlist in [`pyproject.toml`](../../pyproject.toml) is shared by the source
archive and bundle builder. Adding a file under `docs/` does not automatically
package it. Review each allowlist addition for relevance and public suitability.
Keep private working notes, credentials, environment inventories and raw run
exports outside the repository and distribution archives.

The inventory contains hashes, not source contents. Preserve the source snapshot
and inspect the wheel, sdist and installer member lists before distribution.
Reject unintended runtime state, configuration, logs or personal files. The
allowlist and archive checks complement review; they cannot recognize every
sensitive value placed in an otherwise approved file.

## Build inputs and commands

The example below targets **native macOS arm64 with CPython 3.11**. Record the
actual Python, pip, uv, platform and compression-library versions used. Other
platform bundles require compatible dependency wheels and separate validation. The bundle installer requires
CPython 3.11 even though the general application package allows Python 3.11–3.13.

[`requirements-build.lock`](../../requirements-build.lock) is the candidate
build constraint: it pins Hatchling 1.32.0 and its selected base dependency
closure with reviewed SHA-256 hashes. Supply it to `uv build` with
`--require-hashes`. [`build-constraints.txt`](../../build-constraints.txt) is the
matching human-readable version list; it is not sufficient for a candidate
build because it contains no hashes. Preserve the actual cached tool
distributions and record their hashes for independent reproduction.
`pyproject.toml` alone contains an unpinned `hatchling` build requirement.

Use a new output directory and a dedicated, already populated build cache.
`OPSGRAPH_BUILD_CACHE` below must name that cache; an offline cache miss is a
failure, not permission to fetch a new backend silently.

```sh
OPSGRAPH_RELEASE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/opsgraph-0.1.0b1.XXXXXX")"
mkdir "$OPSGRAPH_RELEASE_DIR/dist" "$OPSGRAPH_RELEASE_DIR/wheelhouse"
mkdir "$OPSGRAPH_RELEASE_DIR/repeat"
python3.11 --version
python3.11 -m pip --version
uv --version
python3.11 -c 'import platform, sys; assert sys.platform == "darwin" and platform.machine() == "arm64"'
```

The dependency download below is a separate, explicitly networked preparation
step for the maintainer. Use only the unchanged, reviewed
[`requirements.lock`](../../requirements.lock); it includes the runtime and
provider extras. Do not add an index, relax hashes or fall back to source builds
when a compatible wheel is missing. The native interpreter selects compatible
cp311/ABI and macOS tags, including universal wheels where applicable.

```sh
PIP_CONFIG_FILE=/dev/null python3.11 -I -m pip --isolated --disable-pip-version-check download \
  --index-url https://pypi.org/simple --only-binary=:all: --require-hashes \
  --no-cache-dir --dest "$OPSGRAPH_RELEASE_DIR/wheelhouse" -r requirements.lock
```

If an already verified wheelhouse is supplied, reuse its exact bytes and skip
that download. It must contain dependency wheels only. Preserve the lock hash
and every selected wheel hash; a package/version list alone is insufficient.
`uv.lock` is the broader project resolution, `requirements.lock` is the hashed
runtime/provider export, `requirements-build.lock` is the hashed build-tool
constraint, and `build-constraints.txt` is its readable pin list. Updating any
of them creates a new candidate requiring review.

Build both distribution forms with the same source epoch and constrained tools:

```sh
SOURCE_DATE_EPOCH=1789171200 uv build --offline --no-python-downloads \
  --cache-dir "${OPSGRAPH_BUILD_CACHE:?Set the dedicated populated build-cache path}" \
  --python python3.11 --build-constraints requirements-build.lock \
  --require-hashes \
  --wheel --sdist --out-dir "$OPSGRAPH_RELEASE_DIR/dist"

python3.11 scripts/build_release.py \
  --wheel "$OPSGRAPH_RELEASE_DIR/dist/opsgraph-0.1.0b1-py3-none-any.whl" \
  --wheelhouse "$OPSGRAPH_RELEASE_DIR/wheelhouse" \
  --output "$OPSGRAPH_RELEASE_DIR/opsgraph-0.1.0b1-macos-arm64-cp311.zip" \
  --platform macos-arm64-cp311
```

Expected distribution names are `opsgraph-0.1.0b1-py3-none-any.whl` and
`opsgraph-0.1.0b1.tar.gz`. Confirm the actual names and embedded version. The
builder checks that the wheel's `opsgraph/` package bytes match the inventoried
`src/opsgraph/` exactly. It also rejects dependency wheels whose bytes are not
among the lock's approved SHA-256 values. It does not install dependencies or
prove that the wheelhouse is complete for an operating system.

## Identity, repeatability and installation

The ZIP contains one `opsgraph-macos-arm64-cp311/` directory with the application
wheel, dependency wheelhouse, lock, license/docs, installer and launchers:

| File | Meaning |
| --- | --- |
| `source-inventory.json` | The scoped source path/size/hash inventory and base commit. |
| `build-identity.json` | Inventory hash, exact application/dependency wheels, Python/platform target and derived build ID. `validation` starts as `not_assessed`. |
| `manifest.json` | Installer contract and hashes/sizes for its listed payload files. |
| `SHA256SUMS` | Checksums for delivered payload and manifest; excludes itself. |
| Adjacent `*.zip.sha256` | Checksum of the complete ZIP bytes. |

Keep the wheel and sdist separately alongside the ZIP, with their own SHA-256
values. The ZIP embeds the wheel; it does not embed the sdist. Hashes establish
byte identity relative to a trusted reference, not authorship, signing, licensing
clearance or correctness.

To check repeatability, repeat the `uv build` command with the same frozen source,
tool cache, hashed constraints, Python and `SOURCE_DATE_EPOCH`, changing only its
output directory to `$OPSGRAPH_RELEASE_DIR/repeat`. Compare both wheel and sdist
hashes. Then run the bundle builder again to a new ZIP path with the same inputs
and compare complete ZIP hashes. It fixes archive ordering, member timestamps
and modes. Record Python/zlib versions as well; do not generalize a same-toolchain
ZIP comparison into universal byte reproducibility. Never overwrite an earlier
attempt or rewrite a failed comparison into a pass.

After extraction into a separate test directory, run its `Install.command` or
`python3.11 -I Install.py install`. The installer checks manifested hashes before
venv/pip work, disables pip configuration files, and uses `--no-index`, a local
`--find-links` wheelhouse, `--require-hashes` and binary-only dependencies. It
installs the exact application wheel with `--no-deps`. Python, PostgreSQL, Ollama
and model weights are separate prerequisites; the installer downloads none of them.

Use the extracted launcher's dedicated acceptance arguments and the acceptance
tests for actual source/model, UI and lifecycle validation.
`scripts/wheel_smoke.py` is a separate packaging/configuration smoke
check that can install dependencies through pip; it is neither the offline-bundle
acceptance path nor real connector/model acceptance.

## Dependency and license record

Generate the record from the **actual application wheel and selected wheelhouse**,
not package names inferred from project metadata. Inspect each wheel as a ZIP
without executing it or extracting arbitrary paths. For every artifact record:

1. Filename, SHA-256, Python/platform tags and its `*.dist-info/METADATA` path.
2. Declared `Name`, `Version`, `Requires-Dist`, `License-Expression`, legacy
   `License`, license classifiers and every `License-File` header.
3. Actual license/notice members, including `*.dist-info/licenses/`, `LICENSE*`,
   `COPYING*` and `NOTICE*`; record member paths and content hashes. Retain the
   original wheel, where these texts remain available.
4. Manual review status, reviewer/date and unresolved questions. Disclose missing
   metadata, missing declared files, ambiguous legacy values or conflicting
   declarations as **unknown/unresolved**. Preserve original declarations instead
   of inventing SPDX identifiers or silently treating a classifier as complete.

Use this record schema for each selected artifact:

```json
{
  "artifact": "<exact wheel filename>",
  "sha256": "<artifact SHA-256>",
  "distribution": "<METADATA Name or unknown>",
  "version": "<METADATA Version or unknown>",
  "metadata_path": "<member path or null>",
  "declared_license_expression": null,
  "declared_legacy_license": null,
  "declared_license_classifiers": [],
  "declared_license_files": [],
  "observed_license_members": [{"path": "<member path>", "sha256": "<content SHA-256>"}],
  "review_status": "unreviewed",
  "unresolved": ["Populate from inspection; absence is not a permissive grant"]
}
```

Review bundled native libraries and vendored components separately where their
notices require it; Python distribution metadata may not describe every included
component. Record CPython, ensurepip/pip, uv and build tools outside the application
wheelhouse. Ollama/model weights and PostgreSQL are separately obtained components
and must not be presented as included in this ZIP. OpsGraph's Apache-2.0 license
does not replace dependency licenses. This procedure produces evidence for review;
it does not assert that a dependency-license audit has already passed.

## Container identity is separate

[`deploy/container/Dockerfile`](../../deploy/container/Dockerfile) builds the
application from source, not from the native release wheel. It has a pinned
multiarchitecture Python base reference, installs the hashed runtime lock and
then builds the source package. Its current pip build does not consume
`requirements-build.lock`; do not claim it shares the constrained wheel-build
toolchain or is offline.

For a separately authorized local image build, retain the same source inventory
plus the Dockerfile, `.dockerignore`, Compose and declarative-resource hashes.
Record Docker/BuildKit versions, target OS/architecture, base manifest and resolved
platform image digest, build command, resulting image ID/digest, dependency/license
record and actual runtime configuration boundaries. A mutable image tag is not an
identity. A Linux/arm64 container result cannot establish Windows 11 x64 or native
Ubuntu 24.04 x64 support. Image construction, package identity, real application
acceptance and publication remain separate recorded outcomes.

## Rebuild a source packet without a Git checkout

A release packet must include a verified hashed build toolchain and a trusted
source inventory. Reuse cached toolchain wheels only after checking every wheel
against the pinned hashes. Verify the packet's
SHA256SUMS against the independently supplied checksum before using any included
code. Set `OPSGRAPH_PACKET` to its absolute path and use a new build directory.
Extract the application sdist there.

Create an isolated CPython 3.11 environment, then install the included toolchain:

```sh
python3.11 -m venv build-env
PIP_CONFIG_FILE=/dev/null build-env/bin/python -I -m pip --isolated \
  --disable-pip-version-check install --no-index --only-binary=:all: \
  --find-links "$OPSGRAPH_PACKET/build-tools/wheels" --require-hashes \
  -r "$OPSGRAPH_PACKET/build-tools/requirements-build.lock"
```

From the extracted source directory, invoke that environment's Python with
`SOURCE_DATE_EPOCH=1789171200` and `-m hatchling build -t sdist -t wheel -d OUTPUT`.
Use an absolute executable/output path. The Hatchling CLI uses the same pinned
backend; no dependency or Python download is needed. Compare both outputs with
the packet before claiming byte reproducibility.

Extract the installer ZIP separately to obtain its unchanged dependency wheelhouse.
The ZIP can then be reproduced from the verified source without Git:

```sh
python3.11 scripts/build_release.py \
  --source . --source-inventory "$OPSGRAPH_PACKET/source-inventory.json" \
  --source-inventory-sha256 "$OPSGRAPH_INVENTORY_SHA256" \
  --wheel "$OPSGRAPH_REBUILT_WHEEL" \
  --wheelhouse "$OPSGRAPH_EXTRACTED_BUNDLE/wheelhouse" \
  --platform macos-arm64-cp311 --output "$OPSGRAPH_REBUILT_ZIP"
```

The expected inventory hash must come from the verified packet's independent
manifest/checksum, not an untrusted replacement inventory. The builder checks
bounded, unique, allowed source paths and each byte count/hash, rejects links and
changed/missing files, and includes exactly the verified source set. Unlisted
sdist-generated `PKG-INFO` is deliberately outside that inventory. It does not mutate Git. Byte-identical output in the recorded host/toolchain
does not establish reproducibility or native support on other environments.

The sdist target excludes an existing root `PKG-INFO` input; Hatchling generates
its own metadata. This prevents a generated metadata file being included twice
when rebuilding from an extracted sdist. See [Hatch's target file-selection
rules](https://github.com/pypa/hatch/blob/master/docs/config/build.md).

## Distribute matching source and notices

Supply the matching application source archive and checksums alongside binary
artifacts. Include the applicable third-party source supplement and notices as
described in [beta distribution](beta-distribution.md). Preserve original license
files inside dependency wheels; OpsGraph's Apache-2.0 grant does not replace
third-party licenses. A successfully built ZIP alone does not establish complete
source, notice or license compliance.

## Beta 1 source supplement

Use `docs/release/notices/beta-source-manifest.json` to fetch each original
archive or build file from its recorded URL and verify its SHA-256 before use.
The manifest uses direct archive or raw-content URLs, not HTML file views.
Keep the archives unchanged under `sources/`; retain the original wheel notice
and SBOM members under `wheel-notices/<platform>/<wheel>/`, and copy the release
notice/provenance records under `notices/`. Include the upstream image manifest
and configuration used to trace libxcrypt, plus a `SHA256SUMS` over packet files.

Package that directory as `opsgraph-0.1.0b1-third-party-sources.tar.gz` and publish
it beside the application wheel, matching application source and native bundles.
The source RPMs contain downstream patches and build recipes; do not install them
as runtime dependencies. Upstream build scripts are supplied as source, not run
by the OpsGraph installer. A final outer `SHA256SUMS` covers all published assets.
