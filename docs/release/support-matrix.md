# Beta 0.1.0b1 support matrix

OpsGraph is beta software. Use an artifact built for your exact operating system,
architecture and CPython version. A package installing successfully does not by
itself prove that a PostgreSQL and model workflow works on that platform.

| Environment | Status | Scope |
| --- | --- | --- |
| macOS 26.2 arm64, CPython 3.11 | Beta-tested configuration | Offline bundle lifecycle and an actual PostgreSQL 16 plus Ollama 0.34 / `qwen3:8b` investigation, export, retry and follow-up were exercised locally. |
| Ubuntu 24.04 x64 | Package compatibility only | Installation and lifecycle checks passed in a hosted Ubuntu environment. A complete native PostgreSQL/local-model workflow remains unverified. |
| Windows Server 2025 x64 | Package compatibility only | Installation and lifecycle checks passed in a hosted Windows Server environment. This does not establish Windows 11 support. |
| Windows 11 x64 | Unverified | No complete native PostgreSQL/local-model workflow has been recorded. |
| Container deployment | Configuration checked | Image configuration and health checks passed. The complete PostgreSQL/model workflow for the current image remains unverified. |
| Other systems | Unverified | No support claim is made without an exact artifact and complete workflow check. |

## Model providers

- **Ollama 0.34 / `qwen3:8b`**, through the OpenAI-compatible adapter with the
  Ollama schema profile and reasoning disabled, completed the local workflow
  above. Latency and feasibility depend on the machine, schema and question.
- **Anthropic** configuration, structured-schema handling and errors have
  automated coverage. A live hosted Anthropic model was not tested for this beta.
- **Other OpenAI-compatible endpoints** have adapter and configuration coverage.
  Each endpoint/model combination still needs its own real structured probe.

A successful model probe checks response compatibility. It does not guarantee
correct interpretation of arbitrary schemas. Valid SQL, citations and evidence
hashes establish different properties and do not prove that a conclusion is true.
OpsGraph never switches providers automatically to obtain a result.

## Support expectations

This beta supports one operator per instance, PostgreSQL sources and bounded
read-only investigations. It has no SLA or stable/production-ready claim. Report
reproducible problems with the OpsGraph version, platform, Python version, model
provider/model and sanitized diagnostics. Never attach credentials, workspace
configuration, source records or unredacted exports.

OpsGraph source is Apache-2.0. Bundled dependencies retain their own licenses and
distribution obligations; see [beta distribution](beta-distribution.md).
