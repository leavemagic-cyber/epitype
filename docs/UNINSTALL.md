# Uninstall Epitype

Epitype registrations are removable without disabling either host's native memory. The automated path removes only hook entries marked `id: epitype` or `comment: epitype`, removes Epitype's configuration directory, and preserves every native vault and card file.

It also takes back the blocks Epitype wrote into the files your agents read every session — `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. Only the text between the `EPITYPE RULES` and `EPITYPE INDEX` markers is removed, everything you wrote around it is left byte for byte, and the file is backed up as `<name>.epitype-bak` first. If a sync ever replaced text inside those blocks that Epitype did not recognise as its own, the original is kept in `<name>.epitype-replaced.md` beside the file, each entry saying who replaced it and when; uninstall leaves that file and names it. A file that turns out to be entirely ours — `~/.codex/AGENTS.md` usually is, since the installer creates it — is deleted rather than left empty. Without this step those blocks would stay in your instruction files forever, saying "generated from cards, do not edit here" with nothing left that generates them.

To take back only the blocks, without removing anything else:

```powershell
python -m epitype.host_sync --remove
```

## Automated removal

From the repository root, close active Claude Code and Codex sessions, then run:

```powershell
python install/graft.py uninstall
```

For a PyPI installation, remove Epitype-owned registrations before uninstalling the package:

```powershell
epitype-graft uninstall
pip uninstall epitype
```

For a sandbox or non-default home directory:

```powershell
python install/graft.py uninstall --home C:\path\to\home
```

Preview every affected file and JSON location without writing anything:

```powershell
python install/graft.py uninstall --dry-run
```

The command edits `~/.claude/settings.json` and `~/.codex/hooks.json` only when marked Epitype entries exist. It takes the generated blocks back out of `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` before removing the configuration directory, since the state it needs lives there. It then removes `~/.epitype/`, including the five generated launchers under `~/.epitype/hooks/` (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop`), `config.json` (including its `dream` block), and the bounded fail-open breadcrumb file `shim_status.json`. It does not delete `~/.epitype-vault`, `~/.codex/memories`, Claude project memory directories, or any card file. Timestamped backups remain beside every edited host file.

If `config.json`'s `dream.mode` is `nightly`, uninstall also unregisters the scheduled task before removing the config directory: `schtasks /Delete /TN Epitype\Dream /F` on Windows, or the crontab line marked `# epitype-dream` elsewhere. `piggyback` and `off` modes have no system-level schedule to remove.

A legacy generated index at `<vault>/.cairn/` is renamed to `<vault>/.epitype/` on the first read when the current index directory is absent. These per-vault index directories are separate from the user-level `~/.epitype/` configuration directory described above.

## Manual removal

Use this route only if the automated command cannot run.

1. Back up `~/.claude/settings.json` and `~/.codex/hooks.json` before editing.
2. In each file's top-level `hooks` object, inspect `SessionStart`, `UserPromptSubmit`, `PreCompact`, `PreToolUse`, and `Stop`.
3. Remove only array entries whose own `id` or `comment` field equals `epitype`. Preserve every other array entry and field.
4. Remove an event key only if it was created for Epitype and its array is now empty. Remove the top-level `hooks` key only if Epitype created it and it is now empty. When uncertain, leave the empty object in place.
5. Do not edit Claude's `permissions` or `deny` sections. Do not disable native memory, recall, or history settings.
6. Delete `~/.epitype/`, which contains the Epitype config (including its `dream` block), install ownership metadata, generated `hooks/` shim directory, and `shim_status.json` fail-open breadcrumbs. Keep all vault directories and card files.
7. In `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, delete the two generated blocks including their marker lines: everything from `<!-- EPITYPE RULES BEGIN ... -->` to `<!-- EPITYPE RULES END -->`, and from `<!-- EPITYPE INDEX BEGIN ... -->` to `<!-- EPITYPE INDEX END -->`. Installations made before 2026-09-17 use `AGENT_CONTRACT_CORE` and `SHARED_INDEX` in place of those names. Keep everything outside the markers. If a file contains nothing but those blocks it was created by the installer and can be deleted.
8. Before deleting the config, check `dream.mode` in `~/.epitype/config.json`. If it is `nightly`, also remove the scheduled task the automated path would have unregistered: `schtasks /Delete /TN Epitype\Dream /F` on Windows, or delete the crontab line marked `# epitype-dream` (`crontab -l`, edit, `crontab -` the result) elsewhere. Skip this step for `piggyback` or `off`.

If installation used `--apply-billing-guard`, the two Codex context-limit settings in `~/.codex/config.toml` were an explicit, separate change. Restore them only if you intend to undo that choice; ordinary hook removal does not alter them.

## Restore from a backup

Each changed host file is backed up beside the original as:

```text
<filename>.bak_epitype_<UTC timestamp>
```

To restore, stop the affected host, identify the backup created immediately before the relevant install or uninstall, and compare it with the current file. Copy the backup over the active file only when doing so will not erase legitimate changes made afterward. A direct backup restore is byte-exact; if the file has received later edits, prefer the automated or manual marked-entry removal above.

After removal, run a host normally and confirm its native memory behavior remains available. `python install/graft.py doctor` is an installation check and is expected to report missing Epitype configuration after a complete uninstall.
