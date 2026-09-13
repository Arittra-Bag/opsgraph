# First real investigation

This is a single-operator, self-hosted beta. Use an authorized, non-sensitive
PostgreSQL source while evaluating it. The bundle does not create a database,
install a model, download an ISO, or transmit your source records during setup.

## Before installing

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

Download the matching bundle from [Beta 1 release assets](https://github.com/Arittra-Bag/opsgraph/releases/tag/v0.1.0b1).
Alternatively, use the [source checkout instructions](../README.md#linux-macos-and-windows);
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
   bytes; authenticity depends on trusting the source of both files. Beta bundles
   are unsigned. Do not disable system security globally to run a launcher.
2. Extract the ZIP to a permanent folder in your user profile. Open a terminal
   **inside the extracted folder containing `Install.py`**, then install and launch:

   | System | Install once | Launch |
   | --- | --- | --- |
   | macOS / Linux | `python3.11 -I Install.py install` | `python3.11 -I Install.py launch` |
   | Windows PowerShell | `py -3.11 -I Install.py install` | `py -3.11 -I Install.py launch` |

   macOS also provides `Install.command` and `Launch.command`. Follow your
   organization's policy if an unsigned downloaded launcher is blocked.
3. On first launch, enter the hidden read-only DSN, approved schemas and model
   settings. A DSN is your database connection string; request a dedicated
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
read-only playbook. Select **Inspect** to check the real connection, role,
permissions and columns. This does not query another database or infer semantic
relationships. Unsupported types, permissions and stale schema produce errors.

The inspected schema includes accessible column names and types. Relationships,
join cardinality, business units, status definitions and time semantics remain
unavailable unless the operator supplies them.
Include needed definitions in your question; request clarification when they
are missing. A successful inspection does not prove the dataset complete or true.

In **Settings**, choose your provider, model identifier and endpoint, then save.
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
