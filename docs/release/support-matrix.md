# OpsGraph 1.0 support matrix

OpsGraph 1.0 has a deliberately narrow stable boundary: one trusted operator,
one private self-hosted instance, PostgreSQL sources, and bounded read-only
investigations. “Stable” identifies this versioned contract. It does not imply
an SLA, multi-user identity, tenant isolation, managed hosting, or certified
compatibility with every PostgreSQL deployment and model.

Use an artifact built for the exact operating system, architecture, and CPython
version listed below. Installing a package proves less than exercising a real
database and model path; the evidence level is stated separately.

| Environment | 1.0 release evidence | Boundary |
| --- | --- | --- |
| Ubuntu 24.04 x64, CPython 3.11 | Native bundle install, launch, maintenance, and uninstall lifecycle. The release gate also exercises PostgreSQL 17.7 through a dedicated SELECT-only role and a local deterministic OpenAI-compatible protocol fixture. | The protocol fixture proves the control path, not real-model answer quality. |
| Windows Server 2025 x64, CPython 3.11 | Native bundle install, launch, maintenance, and uninstall lifecycle. | No complete native PostgreSQL/model workflow is claimed. This does not certify Windows 11. |
| macOS 26 arm64, CPython 3.11 | Native bundle install, launch, maintenance, and uninstall lifecycle. | No exact-tag live PostgreSQL/model workflow is claimed by CI. |
| Linux container, amd64 | Image labels, architecture, hardened startup, and `/api/ready` are checked natively on Ubuntu before publication. | PostgreSQL and the model remain external services. |
| Linux container, arm64 | The same image checks run under QEMU on Ubuntu before publication. | Emulation is recorded; it is not native arm64 hardware evidence. |
| Other systems or architectures | Unverified. | No support claim is made without an exact artifact and recorded evidence. |

Docker Desktop on macOS or Windows can run the published Linux image through its
Linux container environment. That does not make the image a macOS or Windows
container and does not replace the native bundle evidence.

## What CI and the release gate cover

The [CI workflow](../../.github/workflows/ci.yml) runs the Python and frontend
suites on Linux, macOS, and Windows Server. It checks supported Python versions,
package installation, source and wheel contents, and the native offline-bundle
lifecycle. Container jobs build and start the Linux image with the documented
hardening flags.

The [stable release workflow](../../.github/workflows/release.yml) adds stronger
tag-bound gates. It requires:

- one clean annotated tag contained in `main` and matching the package version;
- the full test suite and exact source artifacts;
- a real loopback PostgreSQL service with a dedicated SELECT-only role;
- source inspection, bounded readiness, investigation, evidence export, write
  denial, restart persistence, and audit-chain checks;
- one deterministic local model-protocol fixture for probe, plan, and answer;
- native bundle receipts for Ubuntu, Windows Server, and macOS; and
- accepted Linux amd64 and arm64 container images with recorded hashes and
  publication digests.

The GitHub release is created only after those gates pass. The generated
receipts bind the evidence to the exact source commit and artifacts. They do not
prove correctness for an untested external database, provider, schema, or model.

## Model providers

OpsGraph exposes presets for Ollama, LM Studio, vLLM, OpenAI, Anthropic,
OpenRouter, Groq, Together, Mistral, and a manual OpenAI-compatible endpoint.
Presets select protocol defaults; they do not discover models or guarantee that
an account/model supports structured responses.

Automated tests cover configuration, schema handling, bounded errors, and the
OpenAI-compatible and Anthropic adapters. The tag-bound connected smoke uses a
deterministic local protocol fixture. A successful operator-run model probe
checks the exact configured endpoint and model; it still does not guarantee the
interpretation of arbitrary schemas or questions. OpsGraph never switches
providers automatically to obtain a result.

## PostgreSQL and product limits

- PostgreSQL is the only source connector in 1.0.
- Remote PostgreSQL requires `sslmode=verify-full` by default.
- The effective role must be read-only and limited to the configured tables;
  database grants remain the column-level enforcement boundary.
- Source inspection records physical schema, not business meaning, units,
  status semantics, relationships, completeness, or truth.
- Citations and evidence hashes identify recorded bytes. They do not prove that
  a conclusion is correct.
- External inference requires both deployment-level and source-level opt-in.

Report reproducible problems with the OpsGraph version, artifact, platform,
Python version, provider/model, and sanitized diagnostics. Never attach
credentials, workspace configuration, source records, or unredacted exports.

OpsGraph source is Apache-2.0. Bundled dependencies retain their own licenses
and distribution obligations; see the [stable distribution](distribution.md).
