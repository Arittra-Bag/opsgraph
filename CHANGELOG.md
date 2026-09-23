# Changelog

## 1.0.0 — first stable release

- Guided first-run checks for workspace access, exact PostgreSQL scope, a real
  model probe, and an operator-approved bounded database readiness read.
- Least-privilege PostgreSQL role guidance, remote certificate verification,
  effective privilege checks, read-only repeatable-read execution, and schema
  revision binding.
- Durable investigation state with explicit recovery, terminal-state and audit
  failures, plus linked follow-ups, fresh retries, cancellation, and export.
- Backend-held provider configuration for local and hosted adapters, with exact
  model, endpoint, profile, reasoning, timeout, output-token, and egress control.
- Tag-bound release gates for a connected PostgreSQL control path, native
  Linux/macOS/Windows bundles, matching source material, and multiarchitecture
  Linux container images.

See [1.0 release notes](docs/release/release-notes-1.0.0.md), the
[support matrix](docs/release/support-matrix.md), and the
[stable distribution contract](docs/release/distribution.md).

## 0.1.0b1 — 2026-09-13 (beta prerelease)

- Investigation workspace documentation with a real full-page screenshot,
  native setup for Linux/macOS/Windows and a step-by-step Docker path.

- Guided local installation and backend-held source/provider configuration,
  explicit PostgreSQL scope inspection and a structured model compatibility probe.
- Durable investigations with backend progress, cited SQL and records, export,
  fresh retry, linked follow-up, cancellation and preserved partial evidence.
- Local Ollama, OpenAI-compatible and Anthropic adapters with actionable errors;
  no automatic provider switching or external fallback.
- Bounded plan correction for explicit join/count contradictions and clarification
  for unsupported measurement-unit assumptions, without relaxing SQL permissions,
  citation validation or evidence-integrity checks.
- Offline installer lifecycle checks and documented backup, restore and uninstall.
- Public documentation curated for users and contributors. Installer and source
  archives include only explicitly approved documentation and required notices.

See the [Beta 1 release](https://github.com/Arittra-Bag/opsgraph/releases/tag/v0.1.0b1) and the
[support matrix](docs/release/support-matrix.md) for tested capabilities and limits.
Own source remains Apache-2.0; bundled dependencies retain their own licenses.
