# Installation and troubleshooting

OpsGraph runs a single-operator backend and a responsive browser UI. Keep one
backend process per local state database. Standalone desktop apps, team
accounts and automatic continuation of crashed queries are outside this version.
Use Python 3.11–3.13 and a modern browser. Start with an authorized read-only
source whose scope and business meaning you understand.

## Guided bundle (recommended beta path)

The first beta bundle is not published yet; use the [source quickstart](../README.md#linux-macos-and-windows) for now.
For future bundle downloads, start with [quick start](quickstart.md). The versioned offline bundle includes
launch scripts and exact locked dependency wheels; CPython and the model runtime
remain explicit prerequisites. `opsgraph launch` stores configuration in a stable
private directory, opens the connected browser and reuses history across launches.
No source checkout or configuration-file editing is needed for the guided path.
See [maintenance](maintenance.md) for safe backup/restore and uninstall.

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

1. Provision a dedicated PostgreSQL login without elevated privileges, role
   inheritance granting write access, or ownership of target objects. Grant
   schema USAGE and SELECT only on the intended tables. Set its backend DSN in
   `OPSGRAPH_SOURCE_DSN`; use TLS and certificate verification for nonlocal
   database connections. Never enter a DSN in the browser or source name.
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
   source with the DSN variable name and explicit schema-qualified tables.
   Inspect that source to validate credentials, role and schema access.
   Review the returned columns, types, timestamp and warnings. A source marked
   stale needs reinspection and review before a fresh attempt.
5. Run the model probe. It makes an actual structured inference call without
   source data. Source setup remains available when the model is unavailable.
   Select the general read-only playbook, then ask a real question.

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
Start from the source checkout in the [README](../README.md#linux-macos-and-windows).
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
| `OPSGRAPH_PROVIDER_TIMEOUT_SECONDS` | `300` for the documented slow local-model path; tune within the supported 1–600 second range |

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
configuration. `restart` alone does not apply new environment values. `down`
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
