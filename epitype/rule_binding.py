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
    from . import rule_provenance
    path = _path(namespace, identity)
    try:
        raw = path.read_bytes()
        rule_provenance.record_validation_file(path, raw)
        value = json.loads(raw.decode('utf-8'))
        return value == {"version": 1, "sha256": digest}
    except FileNotFoundError:
        rule_provenance.record_validation_file(path, None)
        return False
    except OSError:
        if rule_provenance.validation_inputs_active():
            raise  # A temporarily unreadable input cannot establish a reusable chain verdict.
        return False
    except ValueError:
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
        from . import rule_provenance
        rule_provenance.record_validation_file(target, target.read_bytes())
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
