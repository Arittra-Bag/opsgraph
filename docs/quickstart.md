# First real investigation

OpsGraph 1.0 is a single-operator, self-hosted PostgreSQL investigation
workspace. Start with an authorized, non-sensitive source while learning the
workflow. The bundle does not create a database,
install a model, download an ISO, or transmit your source records during setup.

## Start from a source checkout

After cloning and entering the repository, run `python3 Start.py` on macOS/Linux
or `py Start.py` on Windows. The installer explains downloads and prepares the
locked application runtime. Choose Quick for recommended defaults, or Advanced
to configure schema scope, endpoint and inference options. Review before saving.
The workspace key is generated privately and the browser connects automatically.

For hosted inference, choose the provider, supply its exact structured-output
model identifier and hidden API key, and approve external model egress. Questions,
scoped schema and bounded evidence may be sent to that provider. For local inference,
install a model runtime and download a model separately. These are alternative
inference paths, not simultaneous requirements. The browser workflow below applies
to both. See [installation](installation.md) for prerequisites and recovery.

## Before installing an offline bundle

- A matching CPython 3.11 bundle for a platform/architecture listed in the
  [support matrix](release/support-matrix.md). Do not infer support
  from the filename: use only an artifact whose platform status is listed.
  The installer rejects the wrong OS, architecture, Python version
  and 32-bit interpreter. Python itself remains a separate prerequisite.
- A modern browser and a free loopback port (8000 by default).
- At least 2 GiB free for installation headroom, **in addition to model storage**.
  Check the download size and checksum supplied with the release artifact.
- A PostgreSQL login restricted to SELECT on the intended objects. Know its
  schema/table names and business definitions; the application cannot invent
  credentials, units, status meanings or timezone semantics.
- A local OpenAI-compatible model runtime. The tested local path is Ollama 0.34.0
  with `qwen3:8b`: its main model layer is approximately **5.23 GB decimal**.
  Allow several additional GB for runtime/workspace storage. Latency varies by
  hardware, question and available memory.

If the model is absent, review [Ollama's installation guide](https://docs.ollama.com/quickstart),
then deliberately run `ollama pull qwen3:8b`. That command downloads several GB
from the model registry. OpsGraph never runs it for you. Keep inference local;
cloud-model names and externally hosted endpoints require separate informed
configuration. Our local adapter uses [Ollama's OpenAI-compatible endpoint](https://docs.ollama.com/api/openai-compatibility).

## Install once, launch whenever needed

Download a version-matched bundle from [releases](https://github.com/Arittra-Bag/opsgraph/releases)
when that version is published. Do not mix source and bundle versions.
Alternatively, use the [source checkout instructions](../README.md#get-started);
the browser steps below are the same. Read the platform limits in the
[support matrix](release/support-matrix.md) before installing.

1. Download the ZIP for your OS, architecture and CPython 3.11, together with its
   adjacent `.sha256` file. Check the checksum before extracting:

   | System | Verification command |
   | --- | --- |
   | macOS | `shasum -a 256 -c FILE.zip.sha256` |
   | Linux | `sha256sum -c FILE.zip.sha256` |
   | Windows PowerShell | `Get-FileHash .\FILE.zip -Algorithm SHA256` and compare the hash with `Get-Content .\FILE.zip.sha256` |

   Replace `FILE.zip` with the downloaded filename. A checksum detects changed
   bytes; authenticity depends on trusting the source of both files. Release bundles
   are unsigned. Do not disable system security globally to run a launcher.
2. Extract the ZIP to a permanent folder in your user profile. Open a terminal
   **inside the extracted folder containing `Install.py`**, then install and launch:

   | System | Install once | Launch |
   | --- | --- | --- |
   | macOS / Linux | `python3.11 -I Install.py install` | `python3.11 -I Install.py launch` |
   | Windows PowerShell | `py -3.11 -I Install.py install` | `py -3.11 -I Install.py launch` |

   macOS also provides `Install.command` and `Launch.command`. Follow your
   organization's policy if an unsigned downloaded launcher is blocked.
3. On first launch, choose Quick or Advanced setup, enter the hidden read-only
   DSN and model settings, and review before saving. Advanced also asks for the
   approved schema ceiling. A DSN is your database connection string; request a dedicated
   read-only login from whoever manages the database. Pressing Enter at an empty
   DSN skips database setup, so source inspection will not work until configured.
   Port occupied? Add `--port 8010` to the launch command.
4. The browser connects through a one-use, short-lived fragment token. Neither
   the DSN nor workspace key appears in the URL. If the browser does not open,
   relaunch or connect manually with the key in your private workspace `.env`.
   Do not paste credentials into issue reports, source labels or questions.

Future launches reuse the same per-user directory:

| OS | Default data directory |
| --- | --- |
| macOS | `~/Library/Application Support/OpsGraph` |
| Linux | `$XDG_DATA_HOME/opsgraph`, otherwise `~/.local/share/opsgraph` |
| Windows | `%LOCALAPPDATA%\OpsGraph` |

Installer/lifecycle checks run in CI on macOS, Ubuntu and Windows Server; full
live workflow coverage differs by platform. See the [support matrix](release/support-matrix.md).
For an isolated workspace, add `--directory` followed by an absolute private path
(in quotes if it contains spaces). Use the same directory for future launches.
Stop with Ctrl+C in the launch terminal; closing the browser does not stop the
backend. Never delete the workspace to stop it.

Add `--configure` to reconfigure. Answer `y` when asked to change existing
settings; `N` preserves them. Guided setup asks for a provider and requires
explicit permission for external model egress. A literal loopback model disables
external egress. Existing unknown settings are preserved. Model choices saved
in browser Settings override initial setup; use Settings for later model changes.

## Select the actual scope

In **Sources**, save a source using backend reference `OPSGRAPH_SOURCE_DSN` and
explicit schema-qualified tables, such as `reporting.jobs`. The schema ceiling
from setup can only be narrowed. Leave specialist bindings empty for the general
read-only playbook. Select **Save and inspect source** to check the real connection, role,
permissions and columns. This does not query another database or infer semantic
relationships. Unsupported types, permissions and stale schema produce errors.

If you need a database administrator to create the login, use the role guide to
generate exact SQL for the selected lowercase role, database, and tables. Review
it with the administrator and set a password through their normal secure process.
OpsGraph does not execute the guide, create credentials, grant every table, or
alter default privileges.

The inspected schema includes accessible column names and types. Relationships,
join cardinality, business units, status definitions and time semantics remain
unavailable unless the operator supplies them.
Include needed definitions in your question; request clarification when they
are missing. A successful inspection does not prove the dataset complete or true.

In **Settings**, choose the connection preset, model identifier, endpoint,
structured-output profile, reasoning option, timeout and output-token bound,
then save. OpsGraph derives the protocol adapter from the preset. Presets supply
protocol defaults; they do not discover available
models or promise that a model supports the contract.
API keys are write-only: leave the field blank to keep an existing key for the
same provider and endpoint, or explicitly clear it. Changing endpoints does not
forward the previous key. External processing requires deployment permission
and your explicit approval. Saving sends no model request and does not download
a model. Wait for unfinished investigations before changing configuration.

Saved browser settings persist across restart and override the model settings
from initial setup. Use Settings for subsequent model changes. Then run the
structured model probe. This calls the actual model
without source records. A failure does not disable source configuration. Check
runtime, model identifier, URL and timeout; never select fake output to continue.

The vLLM preset uses `http://127.0.0.1:8001/v1` so it does not collide with
OpsGraph's default port 8000. Start vLLM on port 8001 or enter its exact address
through the custom OpenAI-compatible preset.

Return to **Sources** and approve the bounded readiness check for one exact
table. It executes a one-row read to prove the configured route and permissions,
while returning and retaining no selected source value. Changing the source,
schema snapshot, or relevant policy invalidates that approval.

## Ask, inspect, challenge

Choose your source and the general read-only playbook. Ask a narrow question,
for example: “Which rows have status 'failed'? Treat this as a recorded status,
not a root cause. Cite the rows and explain what remains unknown.” Adapt column
names to your inspected schema. Do not use that example if those columns or
meanings do not exist.

Progress comes from persisted backend events. Open a finding's citation to see
SQL, captured records, source, timestamps and limits. Compare interpretations
against the records: SQL permission, a citation and a matching hash each check
different things, and none proves causality. Review contradictions, missing
records and uncertainty. Export includes sensitive evidence; share deliberately.

If a generated answer conflicts with a narrow capture fact or omits a recognized
explicit requirement, OpsGraph may ask the model once to correct only the answer
using the same evidence. It never reruns SQL for this correction. A second
inconsistent answer fails visibly; inspect the preserved evidence. This guard
does not validate arbitrary business meaning.

Follow-up creates a linked run. Retry collects fresh evidence in a new attempt.
Reload/reconnect attaches to the existing run. Interrupted work is preserved
and never automatically replayed; cancellation waits for active calls to exit.
Persistent errors identify the failed operation and next action. See
[troubleshooting](installation.md#errors-and-recovery) and
[backup, restore, upgrade and uninstall](maintenance.md).

## Guided connections and readable reports

Choose a PostgreSQL hosting route in Sources, follow its private connection
guide, then inspect and approve a bounded readiness check. See
[hosted PostgreSQL onboarding](hosted-postgresql.md).
Saved investigations can produce a selectable Markdown report with a reviewed
preview. See [incident reports](incident-reports.md).

## No-code setup

After cloning, run `python3 Start.py --no-code` on macOS/Linux or
`py Start.py --no-code` on Windows. Python 3.9 or newer is required to start the
installer. No SQL or configuration-file editing is required.

1. Approve the application downloads on the first run. They go into
   `.no-code-runtime`, separate from the existing `.venv` runtime.
2. Choose **Try with practice data** or **Connect my database**.
3. Choose a model service and enter its model name. Hosted services need your
   own API key and explicit permission to send investigation data. Local model
   services must already be running with a downloaded model.
4. Review and save. For practice mode, approve the two invented-data tables and
   a small test read. The launcher registers and inspects them through the same
   authenticated, audited APIs used by the browser. Declining leaves source
   setup for the browser. Changed source choices are preserved rather than replaced.
5. In Settings, test the actual model connection. An installed client, successful
   download or healthy API never stands in for this test. Start an investigation
   only after the source and model checks pass.

When connecting your own database, choose its hosting company, then type `?`
at the visible connection menu for short setup steps and an example. Choose
**Paste my connection string** only when it is ready. The paste prompt hides
its password. **Set up the database later** lets you continue without a login.
Optional help explains read-only access, certificate files and password symbols.
Choose **Open provider instructions in my browser** for the official setup guide.
Opening a guide does not authorise access or change your database.

Interactive setup uses a full-screen interface with keyboard selection, masked
credential fields, scrollable guidance and a review before saving. Use arrow keys
and Enter, or a number for a menu choice. Tab changes focus, Page Up and Page Down
scroll help, F1 opens help, and Escape or Ctrl+C cancels without saving. Wide
terminals show the setup steps beside the form. Smaller terminals use one column.

Set `OPSGRAPH_PLAIN=1` for line-based setup, including screen-reader workflows.
Redirected output, `NO_COLOR`, `TERM=dumb` and very small terminals also use the
line-based flow. The source installer stays dependency-free until the application
is installed. Both presentations use the same configuration validation and save
logic. No password or API key is added to an input history.

Provider browser authorisation and a PostgreSQL read-only login are separate.
OpsGraph currently uses the login provided by your database administrator.
Supabase Management API OAuth is not implemented. It would require a registered
integration, protected token exchange and a separately reviewed approach to
provisioning database access. A provider API key or MCP session is not accepted
as a PostgreSQL password. Manual setup remains available for private networks,
self-hosted databases and deployments without provider authorisation services.

Practice mode creates 1,000 orders and 1,000 payments for an invented shop.
Amounts are integer cents and timestamps use UTC. There are 50 failed fastpay
payments with `gateway_timeout`, 450 succeeded fastpay payments and 500 succeeded
steadypay payments. These deliberately reproducible records support a first
investigation, not a claim of general model accuracy or production validation.

Ask: "Count payments by processor, status and error_code. Which processor has
failed payments, how many are there, and what error is recorded? Cite the evidence."
Review the captured grouped counts and SQL before trusting the answer.

The database uses a separate cluster directory, generated credentials and a
loopback-only port. PostgreSQL 15-18 tools are detected from PATH or conventional
installation locations. When no supported local runtime is installed, setup can
use local Docker Desktop and the versioned `postgres:17.11` practice image after
approval. A remote Docker service is not supported. No existing database service,
Docker image, model runtime or application workspace is upgraded or reconfigured.
If neither prerequisite is available, setup provides installation links and can
be rerun, or you can choose an existing database instead.

On normal shutdown or cancelled configuration, the owned practice database is
stopped. Settings, records and investigation history are retained. Rerun the same
command to resume. Forced process termination may leave the practice database
running. After closing its application, stop it from the checkout with:

```sh
.no-code-runtime/bin/opsgraph no-code --stop
```

On Windows, use `.no-code-runtime\Scripts\opsgraph.exe no-code --stop`.
Pass the same `--directory` if you chose a custom workspace. This stops only the
owned practice service and never deletes data. One process at a time can use a
given No-code workspace.

Default workspaces are siblings of the normal OpsGraph workspace:
`OpsGraph-Practice` for practice data and `OpsGraph-NoCode` for real connections.
Use `--directory` for a separate private location and `--port` for a specific free
browser port. Without `--port`, No-code setup chooses a free loopback port.
Use `--configure` to review saved settings. Secrets stay in private files outside
the checkout. Never share the practice service configuration or workspace `.env`.

Practice setup never provisions cloud accounts, changes a real database or
creates hosted model keys. Connecting your own database requires an authorized
read-only login and explicit table selection in Sources.

### Continue from terminal setup

Terminal setup saves your private database connection and model settings. In Sources,
select **Choose tables and check connection** to approve the exact tables that may be
read. Setup automatically lists readable table names in the approved schemas when
you open the new source form. Select the tables you want to use. No records are read
and nothing is approved automatically. If discovery fails, retry or enter exact
table names manually. The saved connection is reused, so you do not need to paste it again. Table
approval and connection checks are separate from saving credentials.

In Settings, your provider and model are already filled in. The key field stays blank
because stored keys are never displayed again. **Model untested** means the real
connection check has not passed yet. Run **Test actual model connection** before
starting an investigation.
