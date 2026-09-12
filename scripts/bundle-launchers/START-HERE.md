# Offline OpsGraph beta bundle

This archive installs the application without a source checkout or package-network
access. It does not include Python, PostgreSQL, Ollama or a model. Install
**CPython 3.11** for the bundle's exact OS/architecture first and allow at least
**2 GiB free install space**, plus separate model/storage requirements.

Compare the ZIP SHA-256 with the value supplied through your trusted release
channel. Internal hashes detect changed files relative to the manifest; they are
not a signature or proof of who produced the release. `build-identity.json`
records the exact source inventory, wheel and dependency files. Platform-specific
packaging is not platform validation; see `docs/release/support-matrix.md` for
the tested configurations and remaining gaps. This remains beta software with
no production/customer-data approval.

1. Extract the whole archive into a private, writable folder. Keep all files
   together. Choose its permanent location before installing: Python environments
   cannot safely be moved afterward.
2. On macOS, open `Install.command`. On Windows, run `./Install.ps1` from
   PowerShell. The portable alternative is `python3.11 -I Install.py install`
   (Windows: `py -3.11 -I Install.py install`). No execution-policy or security
   setting is changed by these scripts. If your system blocks a launcher, use
   the Python command after verifying the release.
3. Use `Launch.command` or `./Launch.ps1`. Setup asks for a dedicated test source
   and a local model through the installed application. The launcher opens the
   private workspace in your browser. Stop it with Ctrl+C in its terminal.
   A real database and working model remain prerequisites for an investigation.
4. Runtime files live only in this folder's `.venv`. Application configuration
   and evidence live separately in the application's private user-data location.
   Use the application's backup guidance before changing or removing that data.

The installer verifies every manifested file before creating a runtime or invoking
pip. It installs only pinned dependency wheels offline, then the exact application
wheel. It never downloads a model or substitutes sample investigation results.
If installation fails, preserve its error; run Uninstall to remove its incomplete
runtime before retrying. Do not edit the lock or disable hash checks to force it.

`Uninstall.command`, `./Uninstall.ps1`, or `Install.py uninstall` asks you to type
`REMOVE`, then removes only this bundle's owned `.venv`. It retains your workspace
data, exports and downloaded archive. Stop the application before uninstalling.
An unrelated runtime, symlink or junction is refused.

For dedicated acceptance, launch arguments pass through unchanged, for example:
`./Launch.command --directory /private/test-workspace --port 8767 --no-browser`.
On Windows use `./Launch.ps1 --directory C:\private\test-workspace --port 8767 --no-browser`.
Keep that path private and separate from the bundle. Do not place personal files
or workspace data inside `.venv`.
