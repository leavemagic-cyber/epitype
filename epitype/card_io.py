"""Card writes with explicit conflict detection and no-clobber publication."""

import os
from pathlib import Path
import tempfile

try:
    from . import memspec
except ImportError:
    import memspec


class CardConflict(OSError):
    """The card changed since the operation read it."""


def publish(target, payload):
    """Publish complete bytes only if target is absent, including racing writers.

    Stage on the destination filesystem. A filesystem without hard-link support
    fails safely; replacing an existing destination is never a fallback.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def move(source, target, payload=None, expected=None):
    """Move a snapshot without clobbering either name; preserve a failed claim.

    Claim the source by rename before publishing. A concurrent replacement at
    the old name then belongs to its writer and must never be unlinked. If a
    failed move cannot restore that name, the original stays at the reported
    recovery path. Card writers must honor the source lock while editing.
    """
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        return False
    with memspec.file_lock(source, timeout=1.0) as held:
        if not held:
            raise CardConflict(f"card busy: {source}")
        original = source.read_bytes()
        if expected is not None and original != expected:
            raise CardConflict(f"card changed: {source}")
        descriptor, recovery = tempfile.mkstemp(prefix=f".{source.name}.", suffix=".recovery", dir=source.parent)
        os.close(descriptor)
        recovery = Path(recovery)
        claimed = False
        try:
            os.replace(source, recovery)
            claimed = True
            if recovery.read_bytes() != original:
                raise CardConflict(f"card changed while moving: {source}")
            publish(target, original if payload is None else payload)
        except OSError as exc:
            if claimed:
                try:
                    os.link(recovery, source)
                except OSError as restore_error:
                    raise CardConflict(f"move failed; original preserved at {recovery}: {restore_error}") from exc
            raise
        finally:
            # Keep the recovery file when a newer writer owns the source name.
            if not claimed or source.exists() and os.path.samefile(source, recovery):
                recovery.unlink(missing_ok=True)
        recovery.unlink()
        return True


def replace_if_unchanged(target, payload, expected):
    """Serialize cooperating writers and reject a stale read before replacement."""
    target = Path(target)
    with memspec.file_lock(target, timeout=1.0) as held:
        if not held:
            raise CardConflict(f"card busy: {target}")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if target.read_bytes() != expected:
                raise CardConflict(f"card changed: {target}")
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
