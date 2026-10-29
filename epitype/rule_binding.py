"""Read migration for previously matched complete-card fingerprints.

The old digest must match before a narrower digest can be remembered. A cache
entry is scoped to that exact old fingerprint, never to an unvalidated edit.
"""
import hashlib
import json
import os
import tempfile

from . import memspec


def _path(namespace, identity):
    key = hashlib.sha256(json.dumps([namespace, identity], sort_keys=True).encode()).hexdigest()
    return memspec.config_path().resolve().parent / "rule_binding_migrations" / (key + ".json")


def matches(namespace, identity, digest):
    try:
        value = json.loads(_path(namespace, identity).read_text(encoding="utf-8"))
        return value == {"version": 1, "sha256": digest}
    except (OSError, ValueError):
        return False


def remember(namespace, identity, digest):
    target = _path(namespace, identity)
    if matches(namespace, identity, digest):
        return
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "sha256": digest}, stream)
        os.replace(temporary, target)
    except OSError:
        # A read-only installation can still validate the original fingerprint.
        # It cannot migrate later edits until this state directory is writable.
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
