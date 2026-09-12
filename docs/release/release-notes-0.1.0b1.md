# OpsGraph 0.1.0b1 — beta release notes

OpsGraph is a self-hosted PostgreSQL investigation workspace for one operator.
Configure a read-only source and model, ask a bounded question, follow actual
execution, then inspect the cited SQL and captured records. Saved history,
export, follow-up, fresh retry, cancellation and partial evidence are included.
Ollama provides a local model path without a paid account.

This beta adds guided installation/setup, actual source/schema inspection and
structured model probing. It reuses the existing Anthropic and OpenAI-compatible
adapters. Provider errors are actionable; credentials stay on the backend.
No simulated investigations or automatic provider fallback.

## Limits

The locally exercised configuration is macOS 26.2 arm64 with PostgreSQL 16 and
Ollama 0.34 / `qwen3:8b`. Windows 11 and complete native Ubuntu workflows remain
unverified. Current container evidence covers configuration and health checks.
See the [support matrix](support-matrix.md) before choosing an artifact.

A query permission check, valid citation or matching hash does not prove a
conclusion true. Model interpretations require review, especially missing units,
status definitions, time semantics and joins. PostgreSQL only; no team accounts,
automatic remediation, CDC or automatic replay after a crash. No stable,
production-ready or market-adoption claim.

## Installation assets

Each release artifact must be accompanied by its matching SHA-256 checksum.
Native bundles must be labelled with their operating system, architecture and
Python version; a hosted package check is not a full support claim.

For the validated Mac artifact, after obtaining its matching checksum:

```sh
shasum -a 256 -c opsgraph-0.1.0b1-macos-arm64-cp311.zip.sha256
unzip opsgraph-0.1.0b1-macos-arm64-cp311.zip
cd opsgraph-macos-arm64-cp311
python3.11 -I Install.py install
python3.11 -I Install.py launch
```

Python3.11, authorized PostgreSQL and a model are separate prerequisites. The
bundle installs locked wheels offline; it does not download model weights.
See [quick start](../quickstart.md), [troubleshooting](../installation.md), and
[upgrade, backup/restore and uninstall](../maintenance.md).

OpsGraph's own source remains [Apache-2.0](../../LICENSE). Dependencies retain
their licenses; the integrated bundle's source/notice and GPL/LGPL obligations
are described in [beta distribution](beta-distribution.md).
