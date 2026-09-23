# Stable distribution: OpsGraph 1.0.0

The `v1.0.0` release is one tag-bound set of application packages, native
offline bundles, source material, validation receipts, checksums, and a Linux
container image. Do not mix files from another release or from an earlier CI
run.

## GitHub release files

The release contains exactly these downloadable files:

| File | Purpose |
| --- | --- |
| `opsgraph-1.0.0-py3-none-any.whl` | Platform-independent OpsGraph application wheel. Dependencies are separate. |
| `opsgraph-1.0.0.tar.gz` | Matching OpsGraph application source distribution. |
| `opsgraph-1.0.0-third-party-sources.tar.gz` | Verified upstream source archives and supplemental notices for the locked runtime/provider dependency set. |
| `opsgraph-1.0.0-ubuntu-x64-cp311.zip` | Offline CPython 3.11 bundle for Ubuntu 24.04 x64. |
| `opsgraph-1.0.0-windows-x64-cp311.zip` | Offline CPython 3.11 bundle for Windows Server 2025 x64. |
| `opsgraph-1.0.0-macos-arm64-cp311.zip` | Offline CPython 3.11 bundle for macOS 26 arm64. |
| `opsgraph-1.0.0-ubuntu-x64-cp311.zip.sha256` | Checksum for the complete Ubuntu bundle ZIP. |
| `opsgraph-1.0.0-windows-x64-cp311.zip.sha256` | Checksum for the complete Windows bundle ZIP. |
| `opsgraph-1.0.0-macos-arm64-cp311.zip.sha256` | Checksum for the complete macOS bundle ZIP. |
| `acceptance-ubuntu-x64-cp311.json` | Passing build/install/launch/maintenance acceptance receipt for the Ubuntu bundle. |
| `acceptance-windows-x64-cp311.json` | Passing build/install/launch/maintenance acceptance receipt for the Windows bundle. |
| `acceptance-macos-arm64-cp311.json` | Passing build/install/launch/maintenance acceptance receipt for the macOS bundle. |
| `source-build-receipt.json` | Tag, commit, version, and hashes of the wheel, source distribution, and source supplement. |
| `connected-smoke-receipt.json` | Passing connected PostgreSQL control-path receipt. Its provider is a deterministic local protocol fixture, not model-quality evidence. |
| `container-smoke-linux-amd64.json` | Native Linux amd64 container acceptance receipt. |
| `container-smoke-linux-arm64.json` | QEMU-emulated Linux arm64 container acceptance receipt. |
| `container-publication-receipt.json` | Published multiarchitecture index digest and the two exact accepted platform-image digests. |
| `SHA256SUMS` | The single top-level checksum list for every other GitHub release file. |

Verify the top-level checksum list from the directory containing all downloaded
release files:

```sh
sha256sum --check SHA256SUMS
```

On macOS, use `shasum -a 256 -c SHA256SUMS`. PowerShell users can compare a
file with its recorded value using `Get-FileHash -Algorithm SHA256`.

Each native ZIP also has an adjacent `.zip.sha256` for verifying that bundle in
isolation. Inside each ZIP, a separate `SHA256SUMS` covers its extracted
payload. The source supplement has its own internal `SHA256SUMS`. These scoped
lists do not replace the release-level `SHA256SUMS`.

## Native bundle identity

Every native ZIP contains the application wheel and the exact dependency
wheelhouse selected on its target runner. Its generated
`dependency-inventory.json` records:

- the target platform;
- the application and dependency wheel filenames, sizes, and SHA-256 hashes;
- declared package name, version, `License-Expression`, and `License-File` metadata;
- the paths and SHA-256 hashes of license, notice, and embedded SBOM members
  observed inside each wheel; and
- the paths and SHA-256 hashes of native-library members observed inside each
  wheel.

The inventory hash is bound into `build-identity.json` and `manifest.json`.
Inventories are generated independently because Linux, Windows, and macOS can
select different platform wheels. They describe the delivered bytes; they are
not a security or legal certification.

An `acceptance-<target>.json` receipt is published only after the corresponding
ZIP completes its full offline install, launch, maintenance, and uninstall
lifecycle. Package acceptance does not establish live model quality or identical
behavior for every external PostgreSQL deployment.

## Matching source and notices

The application source is `opsgraph-1.0.0.tar.gz`. The separate
`opsgraph-1.0.0-third-party-sources.tar.gz` is built from
[`source-manifest-1.0.0.json`](notices/source-manifest-1.0.0.json). It contains:

- the stable source manifest;
- exact upstream source archives and recorded build inputs for all packages in
  `requirements.lock`;
- the supplemental license and notice files listed by the manifest; and
- an internal `SHA256SUMS` for every other member of the supplement.

Original license, notice, and SBOM members remain inside the dependency wheels
in each native bundle. Their identities are recorded by that bundle's generated
dependency inventory. See [third-party notices](third-party-notices.md) for the
component-level pointers and [build documentation](build-and-dependencies.md)
for the maintainer procedure.

## Container image

The container package is published at `ghcr.io/arittra-bag/opsgraph`. The immutable
`v1.0.0` tag resolves to one index containing exactly:

- `linux/amd64`, built and exercised natively on Ubuntu; and
- `linux/arm64`, exercised under QEMU on Ubuntu.

The release pipeline builds each platform image once, starts that exact image
with the hardened runtime flags, checks `/api/ready`, verifies its labels and
architecture, and publishes the accepted platform bytes. The publication
receipt binds the final index digest to those two platform digests.

The release job also publishes immutable `v1.0.0-linux-amd64` and
`v1.0.0-linux-arm64` platform tags used to assemble that index. It does not move
container aliases such as `1.0`, `1`, or `latest`, because GHCR does not offer the
atomic compare-and-swap needed to prevent a concurrent publication from moving an
existing alias backward. GitHub's release-level `latest` marker is advanced only
when the new published stable version is not older than the existing stable set.

These are Linux container images. They are not Windows or macOS images. Docker
Desktop on Windows or macOS can run the Linux image through its Linux container
environment; that does not change the image operating system or replace the
separate native Windows and macOS bundle evidence.

Pull by the immutable release tag:

```sh
docker pull ghcr.io/arittra-bag/opsgraph:v1.0.0
```

For a reproducible deployment, pin the registry digest shown by the package and
`container-publication-receipt.json` instead of relying on a movable convenience
tag.

## Evidence boundaries

Release receipts prove the recorded build or acceptance path for the exact tag.
They do not prove that a model's interpretation is correct, that every provider
or PostgreSQL topology is compatible, or that a dependency is free of
vulnerabilities. Review the [support matrix](support-matrix.md), verify the
source and model from the target environment, and inspect captured evidence
before relying on a finding.
