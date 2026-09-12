# Maintain a local workspace

The launcher keeps configuration/history outside the extracted bundle. Never put
`--directory` inside `.venv`; the launcher rejects that location. The default
macOS location is `~/Library/Application Support/OpsGraph`. Exports and state
contain questions, SQL, source metadata and captured records. A backup also
contains **database credentials and the workspace key**. Keep it private and
never attach it to an issue. Disk encryption is the operator's responsibility.

## Shutdown and later launch

Press Ctrl+C in the terminal that launched OpsGraph. Wait for shutdown to finish;
closing the browser alone does not stop it. Run the same `Launch.command` later.
An interrupted attempt preserves prior captures and requires explicit fresh
retry. OpsGraph does not resume or replay queries after a crash.

## Backup and restore

Use the installed executable in the bundle's `.venv/bin/opsgraph` (Windows:
`.venv\Scripts\opsgraph.exe`). Stop the backend before backup. The command
refuses a live coordinator lock and verifies the SQLite copy.

```sh
.venv/bin/opsgraph backup --directory '/absolute/private/workspace' --output '/absolute/private/backup-20260912'
.venv/bin/opsgraph restore --backup '/absolute/private/backup-20260912' --directory '/absolute/private/restored-workspace'
.venv/bin/opsgraph launch --directory '/absolute/private/restored-workspace'
```

Both output directories must be new; existing directories are never overwritten.
Backup contains private `.env`, a consistent `state.db`, and checksummed manifest.
Restore validates checksums/integrity and updates the state path for the new
workspace. It preserves source references, workspace key, history and evidence;
it cannot restore the external PostgreSQL database or model files. Windows
inherited ACL privacy still requires native verification.

Verify restored history and an exported capture before using the restored copy.
Do not run the original and restored workspace simultaneously against the same
source until you have reviewed pending work; accepted queued work is durable.
The default policy preserves interrupted active work without automatic replay.

## Upgrade

1. Keep the previous extracted bundle and make a stopped backup.
2. Extract and verify the new release into a **new** folder. Install there.
3. Restore the backup into a new workspace and launch the new package against
   that copy. Check history/export, source inspection, the model probe and a fresh
   investigation before switching over.
4. Keep the old package and original backup until the new version is accepted.
   Do not point an older application at migrated state: restore its original
   backup if reverting. See [migration notes](migration.md).

## Uninstall

Stop OpsGraph first. `Uninstall.command` removes only the bundle's verified,
owned virtual environment after explicit confirmation. It preserves the bundle,
workspace, database, model runtime and model files. Removing those is a separate
operator decision. The tool never deletes a workspace or a PostgreSQL database.
Keep the backup before manually removing sensitive workspace records.

A container's state volume is separate from native launcher state. Stop only the
intended Compose project and back up that volume before replacing its image.
Do not use `down -v` as routine shutdown; that deletes state. See the
[installation guide](installation.md#containers) for container setup and recovery.
