# Installation and troubleshooting

OpsGraph runs a single-operator backend and a responsive browser UI. Keep one
backend process per local state database. Standalone desktop apps, team
accounts and automatic continuation of crashed queries are outside this version.
Use Python 3.11–3.13 and a modern browser. Start with an authorized read-only
source whose scope and business meaning you understand.

## Guided source installation

After cloning, run `python3 Start.py` on macOS/Linux or `py Start.py` on Windows.
The source installer needs Python 3.9 or newer. It uses or downloads a supported
Python 3.11–3.13 for the application. Git and a browser remain prerequisites.

The terminal shows installation progress, then three setup steps: your database,
your model service, and a review before saving. Choose menu items by number.
Defaults are shown before entry. Type `?` where offered for an explanation,
including the hidden database connection prompt. Passwords and API keys do not
appear while typing. Press Ctrl+C to cancel without replacing saved settings.
Terminal headings and questions use color when supported, with plain output when
redirected or when `NO_COLOR` is set. Long instructions wrap to the terminal width.

The installation plan is shown before any download. Accepting it installs pinned
uv in `.bootstrap` only when uv is missing, synchronizes locked runtime/provider
dependencies in `.venv`, and builds the application using hashed build constraints.
It needs internet access to PyPI and, when Python is missing, uv's Python
build distribution service. No administrator privileges, global Python package
changes, shell activation, or execution-policy changes are required.
Package-manager environment overrides are excluded so they cannot redirect the
runtime or silently select another dependency index. The bootstrap uses standard
PyPI. For a managed package mirror or an offline environment, use the explicit
wheel/bundle installation path below.

Quick setup uses the existing schema ceiling or `public`, a 300-second model
timeout for a new workspace, and provider-specific endpoint/output defaults. It
keeps explicitly saved reasoning options and omits them for a new provider.
Advanced setup also asks for schema scope, endpoint, structured-output profile,
reasoning and timeout. Both require explicit external-egress consent and a
credential-free save review. Setup never opens a database connection or probes a
model. Use Sources inspection/readiness and the Settings probe afterwards.

```sh
python3 Start.py --flow quick
python3 Start.py --configure --flow advanced
python3 Start.py --port 8010
```

Use `py` instead of `python3` on Windows. `--install-only` prepares dependencies
without opening setup. `--yes` approves installation downloads only. It does not
approve model egress, database access or a setup save. Installation errors suppress
raw subprocess output, which can contain proxy credentials. Fix network,
certificate or disk-space problems and rerun the same command. A failed install
preserves private workspace configuration and history.

If uv is already installed, the direct path remains available:

```sh
uv sync --locked --all-extras
uv run --locked opsgraph launch
```

`opsgraph setup --flow quick` and `opsgraph launch --configure --flow advanced`
work from an installed application, including offline bundles. Cancelling at the
save review preserves the previous configuration. Rerun setup to change choices.
If a read-only database login is not available, skip the hidden connection-string prompt and
use Sources' administrator role guide. It generates reviewable SQL and never
executes it. Install a local model separately or choose a hosted provider and its
exact model identifier. Provider presets do not guarantee model compatibility.

## Guided offline bundle

Use a version-matched bundle from [releases](https://github.com/Arittra-Bag/opsgraph/releases)
when one is published for the version you need, then follow [quick start](quickstart.md).
Check its release version, manifest and checksums. The latest published release
may differ from the source checkout, so do not mix bundle components across versions.
An offline bundle includes launch scripts and exact locked dependency wheels.
CPython and the model runtime remain explicit prerequisites for that path.
`opsgraph launch` stores configuration in a stable private directory, opens the
connected browser and reuses history across launches. No source checkout or
configuration-file editing is needed. See [maintenance](maintenance.md) for
backup/restore and uninstall.

## Native installation (advanced wheel/source path)

From a checkout, install pinned runtime/provider dependencies and the package.
The application wheel is platform-independent; its binary dependencies still
need compatible wheels for the target OS and architecture.

macOS/Linux (POSIX shell):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/opsgraph init
.venv/bin/opsgraph doctor
.venv/bin/opsgraph serve
```

Windows (PowerShell, no activation or execution-policy change required):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps .
.\.venv\Scripts\opsgraph.exe init
.\.venv\Scripts\opsgraph.exe doctor
.\.venv\Scripts\opsgraph.exe serve
```

Doctor initially fails until the source is configured; this is expected. It
reports only local configuration readiness and makes no database/model request.
To install a built wheel, replace `pip install --no-deps .` with
`pip install --no-deps /path/to/opsgraph-<version>-py3-none-any.whl`, using the
matching `requirements.lock`. Build a wheel from the locked checkout with
`uv build --build-constraints requirements-build.lock --require-hashes`.
`python scripts/wheel_smoke.py` installs that wheel and hashed runtime
dependencies into a fresh temporary environment on any of the three OS families.
It does not run live connector/model acceptance.

Run CLI commands from the directory holding `.env`. The CLI loads that file
without overriding environment values set by the shell or service manager.
`opsgraph init` preserves existing `.env` files and generates a new random key
only for new configuration. On POSIX, newly created `.env` and `.opsgraph`
receive modes 0600 and 0700. On Windows, keep the directory within your private
user profile and verify inherited ACLs; POSIX mode bits are not a Windows ACL.

## Source and model setup

### Model connection checks

Saving model settings does not check whether the model exists or can answer.
In Settings, run **Test actual model connection**. This sends a small structured
request without database records, schema, or an investigation question.

A passing check stays valid for 15 minutes in the running backend. A new check
clears the earlier result immediately. Failure, expiry, saved configuration
changes, and backend restarts require another check. Reloading the browser can
reuse a still-valid check from the same backend. No check runs automatically.
Source inspection and its approved readiness read remain separate requirements.
A connection check does not qualify full investigation behavior or answer quality.

For Ollama, run `ollama list` on the computer running the model service. If the
selected model is absent, download that exact model with `ollama pull MODEL_NAME`,
then repeat the connection check. Choose a model that fits the available RAM.
OpsGraph does not install a model runtime or download model files.

`GET /api/health` remains a liveness response. Its legacy `investigation_ready`
field now means a configured real provider has a recent successful connection
check. `readiness_scope` explicitly limits this to model connectivity. It does
not establish that any selected source is approved or reachable. Authenticated
`GET /api/providers/current` reports the connection check state, configuration
revision, check time, expiry, and remaining validity without invoking the model.
Use `/api/ready` for local service health, not model or database qualification.

1. Provision a dedicated PostgreSQL login without elevated privileges, role
   inheritance granting write access, or ownership of target objects. Grant
   schema USAGE and SELECT only on the intended tables. Set its backend DSN in
   `OPSGRAPH_SOURCE_DSN`; use TLS and certificate verification for nonlocal
   database connections. Remote TCP connections require
   `sslmode=verify-full` by default. The compatibility override
   `OPSGRAPH_ALLOW_INSECURE_REMOTE_POSTGRES=true` deliberately accepts weaker
   transport and should remain off. Never enter a DSN in the browser or source
   name.
2. Leave `OPSGRAPH_POSTGRES_SECRET_REF=OPSGRAPH_SOURCE_DSN` and include that
   reference in `OPSGRAPH_ALLOWED_POSTGRES_SECRET_REFS`. Multiple source
   references must use `OPSGRAPH_*_DSN` names and be explicitly allowlisted.
   The backend schema ceiling defaults to `OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS=public`.
   For another schema, explicitly set a comma-separated list (for example,
   `public,reporting`) and restart the backend. Source selection and playbooks
   can only narrow that ceiling. Use simple lowercase PostgreSQL identifiers;
   quoted or mixed-case identifiers are not supported in this release.
3. Install [Ollama](https://docs.ollama.com/quickstart) for your OS and run
   `ollama pull qwen3:8b`. Keep the model service private. Native defaults are
   `OPSGRAPH_MODEL_PROVIDER=openai_compatible`,
   `OPSGRAPH_LOCAL_MODEL_URL=http://127.0.0.1:11434/v1` and
   `OPSGRAPH_LOCAL_MODEL=qwen3:8b`. For Qwen3 on Ollama, set
   `OPSGRAPH_LOCAL_REASONING_EFFORT=none` to disable thinking during bounded
   structured inference. Omit this option for endpoints/models without
   `reasoning_effort` support; it is not sent unless configured.
   Set `OPSGRAPH_LOCAL_SCHEMA_PROFILE=ollama` for Ollama's grammar compatibility:
   generation omits string-length constraints that Ollama 0.34.0 rejected in
   local testing; OpsGraph still enforces the full original schema, including
   those bounds, before accepting plans or answers. Other providers default
   to `standard`. There is no automatic retry with a weaker schema.
   The model download consumes several GB;
   inference feasibility depends on available memory and hardware. No paid
   model account is needed. Pulling a model does not prove its output is valid.
4. Start OpsGraph and open [the local UI](http://127.0.0.1:8000). Copy the key
   privately from `.env` into workspace-key configuration. Save a PostgreSQL
   source with the DSN variable name and explicit schema-qualified tables. If a
   database administrator still needs to create the login, enter the lowercase
   role, database, and table identifiers in the role guide. It emits exact,
   reviewable SQL but never executes it, chooses a password, grants broad access,
   or changes default privileges. Inspect the source to validate credentials,
   role and schema access.
   Review the returned columns, types, timestamp and warnings. A source marked
   stale needs reinspection and review before a fresh attempt.
5. Approve the source readiness check. It executes one bounded read against an
   exact inspected table, returns no selected source value, and is invalidated
   when the source, schema snapshot, or relevant policy changes.
6. Run the model probe. It makes an actual structured inference call without
   source data. Source setup remains available when the model is unavailable.
   Select the general read-only playbook, then ask a real question.

Browser Settings supports Ollama, LM Studio, vLLM, OpenAI, Anthropic,
OpenRouter, Groq, Together, Mistral, and a manual OpenAI-compatible endpoint.
Choose the exact model identifier, structured-output profile, reasoning option,
timeout, and output-token bound. These presets provide protocol defaults and
official hosted endpoints where applicable; they do not discover models or
guarantee compatibility. Saving does not test a provider, and OpsGraph never
falls back to another provider automatically.

Ollama's [OpenAI compatibility](https://docs.ollama.com/openai) supports a local
`/v1` endpoint. Configure `OPSGRAPH_PROVIDER_TIMEOUT_SECONDS` between 0.1 and
600 seconds (default 30); slow local hardware may need 300 seconds. Cancellation
waits for an in-flight model call to exit, up to that configured timeout. A timeout
or malformed response is a visible failure, never a replay fallback. Runtime
schema validation is still required even when the endpoint accepts a JSON schema.

External inference is optional and requires both deployment egress opt-in and
source-level approval. It can send approved schema metadata, questions and
bounded evidence to that provider. Credentials must stay in backend environment
variables. `LANGSMITH_TRACING=false` avoids optional tracing by default.

## Containers

Use Docker Engine or Docker Desktop with Compose v2 and Linux containers.
Start from the source checkout in the [README](../README.md#get-started).
These steps run **OpsGraph only**; supply an existing authorized test PostgreSQL
server and a model runtime separately. Do not apply them to a shared or production
Compose deployment.

### 1. Create private configuration

From the repository root, after `uv sync --locked --all-extras`:

```sh
uv run opsgraph init
```

This creates a private `.env` containing a random workspace key. An existing
`.env` is preserved. Open it privately in your editor and keep the generated
`OPSGRAPH_API_KEY`; do not copy `.env.example` over it or commit it.

Set these values for your own services:

| Variable | What to enter |
| --- | --- |
| `OPSGRAPH_SOURCE_DSN` | Your dedicated read-only PostgreSQL connection string, with host, port, database and user explicit; use an address reachable from the container |
| `OPSGRAPH_POSTGRES_ALLOWED_SCHEMAS` | Your approved schemas, for example `public` |
| `OPSGRAPH_LOCAL_MODEL_URL` | The model's container-reachable OpenAI-compatible URL; see the routing notes below |
| `OPSGRAPH_LOCAL_MODEL` | An installed model identifier, such as `qwen3:8b` |
| `OPSGRAPH_LOCAL_SCHEMA_PROFILE` | `ollama` for the tested Ollama path; `standard` for other compatible endpoints |
| `OPSGRAPH_PROVIDER_TIMEOUT_SECONDS` | `300` for the documented slow local-model path; tune within the supported 0.1–600 second range |

Keep the generated source-reference names unchanged. Quote `.env` values that
contain special characters using Docker Compose's environment-file rules;
credentials containing URL-reserved characters also need URL encoding in a DSN.
See [Docker's environment-file syntax](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax).

### 2. Check the service addresses and permission

Inside a container, `127.0.0.1` means that container. Set the model URL to
`http://host.docker.internal:11434/v1` when using a host service; Compose adds
`host-gateway` on Linux. Names such as `host.docker.internal` and `ollama` are
**not loopback** under the provider policy, even if the model runs on your own
machine. Model access through these names requires explicit
`OPSGRAPH_EGRESS_ENABLED=true`; investigations also require source-level
external-egress approval and an egress-compatible playbook (the general
read-only playbook permits it). These defaults remain disabled. Do not enable
them before reviewing the exact endpoint and what evidence it will receive.
Adjust the database DSN for container reachability separately.
The host services must actually accept connections from the container network;
Docker address translation does not make a loopback-only service reachable on
all platforms. Use a dedicated model service on a private container network if
needed, and restrict access with host firewall rules. Do not expose a model
server publicly to solve connectivity problems.

Compose forwards `OPSGRAPH_LOCAL_REASONING_EFFORT` only when configured in the
environment or `.env`. The default Qwen3 setup created by `init` uses `none`.
Remove this setting entirely for endpoints without `reasoning_effort` support;
do not assign an empty string. A working network route or a successful static
Compose check does not prove that the complete database/model workflow works in
that environment. Review the [support matrix](release/support-matrix.md) before
relying on a container path.

### 3. Start and connect

```sh
docker compose --env-file .env -f deploy/compose.yaml config --quiet
docker compose --env-file .env -f deploy/compose.yaml up --build -d
docker compose --env-file .env -f deploy/compose.yaml ps
```

`config --quiet` checks configuration without printing resolved secrets. The
build downloads the base image and locked dependencies. Open
`http://127.0.0.1:8000`, then enter the `OPSGRAPH_API_KEY` from your private `.env`
in Connect workspace. Unlike the native launcher, Compose does not open an
authenticated browser automatically. Follow the [source inspection and model
test steps](quickstart.md#select-the-actual-scope) before starting an investigation.
For the host-model route above, explicitly approve the source's external-model
permission after reviewing the endpoint; otherwise investigations remain blocked.

The API binds to host loopback only. History lives in the Compose-managed
`opsgraph-state` volume at `/data/state.db`; its full name normally includes the
Compose project prefix. Native workspace history is separate and is not copied
into the container. A relative native `OPSGRAPH_STATE_PATH` is intentionally ignored.

### 4. Logs, stop and resume

```sh
# Inspect startup errors locally; review logs before sharing them.
docker compose --env-file .env -f deploy/compose.yaml logs --tail=100 api
# Stop while preserving the container and saved history.
docker compose --env-file .env -f deploy/compose.yaml stop
# Resume the same container.
docker compose --env-file .env -f deploy/compose.yaml start
```

After editing `.env`, use `up -d` again to recreate the service with the changed
configuration. `restart` alone does not apply new environment values. Model
choices saved in browser Settings override initial environment settings; change
those choices in Settings. `down`
removes containers but preserves the named state volume; **do not add `--volumes`
if you want to keep history**. Stop before backing up the volume and private
configuration. Keep the same checkout/project name when resuming so Compose uses
the same volume.

The image runs without root privileges, with a read-only filesystem, writable
state volume, bounded temporary directory and dropped capabilities. Database
and model routes still need host/network controls. Container installation is
an option, not proof of Windows/macOS compatibility. See
[Docker's installation paths](https://docs.docker.com/compose/install/).

## Errors and recovery

| Observation | Action |
| --- | --- |
| Workspace key rejected | Use the current private backend key; a changed key requires browser re-entry. |
| Source variable missing | Set the DSN on the backend and restart; saving a reference does not create a secret. |
| Role or table scope rejected | Use a dedicated least-privilege role and explicit authorized tables; do not weaken the policy. |
| No tables survive playbook restrictions | Choose a compatible playbook or correct source scope; empty intersections deny execution. |
| Model probe fails | Check service URL, installed model, provider package and timeout; retry the probe explicitly. |
| Model answer or citations rejected | Inspect preserved evidence, narrow the question or select a model suited to structured responses. One narrow inconsistency may receive one answer-only correction against the same evidence; no SQL is rerun. A second inconsistency or invalid citation fails visibly, preserving captures. |
| Evidence exceeds a payload limit | Select fewer rows/columns or narrow the question; earlier captures remain available. |
| Stream disconnected | Reconnect to the saved run; connection heartbeats do not imply query progress. |
| Run interrupted after restart | Inspect preserved evidence; retry creates a new attempt and rechecks current authorization. |
| Cancellation pending | Active driver/model call must exit before terminal cancellation; configured timeouts remain in force. |
| Evidence incomplete or claim unsupported | Inspect capture limits, contradictions and missing evidence before drawing conclusions. |

## Storage, backup and upgrade

The state database and exports contain source metadata, questions, SQL, captured
rows and model outputs. Treat them as sensitive records. The workspace key and
DSNs must not be persisted in that state or exports. SQLite contents are not
automatically encrypted; protect the filesystem and backups. A content hash
checks integrity, not confidentiality or truth. Configure retention explicitly
outside the application until a supported retention feature exists.

Stop the backend cleanly before an offline backup. Back up the entire
`.opsgraph` directory (or stopped container state volume), including any SQLite
sidecar files and the private `state.db.provider` model-settings directory, to
private storage. Store `.env` separately with restricted
permissions; never include it in shared evidence exports. Restore only while
the backend is stopped, using a fresh directory and a compatible application
version. Keep the original backup unchanged. Test restoration by reopening
history and verifying exports before resuming work. Avoid network-mounted SQLite
files and multiple backend processes sharing the same state path.

Before upgrading, back up state and configuration, read
[migration notes](migration.md), install the reviewed wheel/dependency lock in
a new virtual environment, then start against a copy of the backup. Validate
history and a fresh source/model probe before switching your local instance.
An upgrade must not silently re-enable sample execution or replay interrupted SQL.

## Platform status

Use only an artifact listed for your exact operating system, architecture and
Python version. The [support matrix](release/support-matrix.md) distinguishes
complete database/model workflow evidence from package installation checks.
Compatibility observed on a hosted runner or in a container does not establish
native support for another operating system.

## Guided connections and readable reports

Choose a PostgreSQL hosting route in Sources, follow its private connection
guide, then inspect and approve a bounded readiness check. See
[hosted PostgreSQL onboarding](hosted-postgresql.md).
Saved investigations can produce a selectable Markdown report with a reviewed
preview. See [incident reports](incident-reports.md).
