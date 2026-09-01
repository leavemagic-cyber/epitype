# Uninstall Epitype

Epitype registrations are removable without disabling either host's native memory. The automated path removes only hook entries marked `id: epitype` or `comment: epitype`, removes Epitype's configuration directory, and preserves every native vault and card file.

## Automated removal

From the repository root, close active Claude Code and Codex sessions, then run:

```powershell
python install/graft.py uninstall
```

For a sandbox or non-default home directory:

```powershell
python install/graft.py uninstall --home C:\path\to\home
```

Preview every affected file and JSON location without writing anything:

```powershell
python install/graft.py uninstall --dry-run
```

The command edits `~/.claude/settings.json` and `~/.codex/hooks.json` only when marked Epitype entries exist. It then removes `~/.epitype/`. It does not delete `~/.epitype-vault`, `~/.codex/memories`, Claude project memory directories, or any card file. Timestamped backups remain beside every edited host file.

## Manual removal

Use this route only if the automated command cannot run.

1. Back up `~/.claude/settings.json` and `~/.codex/hooks.json` before editing.
2. In each file's top-level `hooks` object, inspect `SessionStart`, `UserPromptSubmit`, `PreCompact`, and `PreToolUse`.
3. Remove only array entries whose own `id` or `comment` field equals `epitype`. Preserve every other array entry and field.
4. Remove an event key only if it was created for Epitype and its array is now empty. Remove the top-level `hooks` key only if Epitype created it and it is now empty. When uncertain, leave the empty object in place.
5. Do not edit Claude's `permissions` or `deny` sections. Do not disable native memory, recall, or history settings.
6. Delete `~/.epitype/`, which contains the Epitype config and install ownership metadata. Keep all vault directories and card files.

If installation used `--apply-billing-guard`, the two Codex context-limit settings in `~/.codex/config.toml` were an explicit, separate change. Restore them only if you intend to undo that choice; ordinary hook removal does not alter them.

## Restore from a backup

Each changed host file is backed up beside the original as:

```text
<filename>.bak_epitype_<UTC timestamp>
```

To restore, stop the affected host, identify the backup created immediately before the relevant install or uninstall, and compare it with the current file. Copy the backup over the active file only when doing so will not erase legitimate changes made afterward. A direct backup restore is byte-exact; if the file has received later edits, prefer the automated or manual marked-entry removal above.

After removal, run a host normally and confirm its native memory behavior remains available. `python install/graft.py doctor` is an installation check and is expected to report missing Epitype configuration after a complete uninstall.
