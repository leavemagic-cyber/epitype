---
name: safe-git-changes
description: Use before staging, committing, or updating a Git repository when other local changes may be present.
---

# Safe Git changes

1. Run `git status --short` in the intended repository and identify the paths that belong to this task.
2. Inspect the diff for those paths before staging. Preserve changes whose owner or purpose is unknown.
3. Stage the explicit paths you changed, for example `git add path/to/file.py path/to/test.py`. Check the staged diff, then commit only those paths.
4. Recheck status after committing and report the commit ID and any remaining local changes.
5. Ask before destructive Git operations or deleting another person's work. Do not use a whole-tree stage such as `git add -A` as a shortcut in a dirty checkout.

Example: If `git status --short` lists your `src/parser.py` and an unrelated `notes.txt`, stage `src/parser.py` by name and leave `notes.txt` visible in status.

The instructions here do not install hooks or run automatically. Apply them when the user has asked for Git work and the tool surface can run Git commands.
