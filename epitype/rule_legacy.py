"""Freeze legacy rule membership and meaning without granting source authority.

The caller hashes the complete rule meaning, including body and execution fields,
with source metadata excluded. This module never reads cards or originals, and
does not import the rule pipeline. A match proves only equality to the configured
baseline, not Owner authorship, semantic approval, or permission to act.

Both baseline path and a fixed raw-byte SHA256 pin are required to enable checking.
Configuration remains under the root's authorized change process; this is not OS
tamper protection. Nothing here enrolls cards or updates a baseline or its pin.
"""
import argparse
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
from types import MappingProxyType

from . import memspec

PATH_OPTION = "rule_source_baseline"
PIN_OPTION = "rule_source_baseline_sha256"
VERSION = 1
NATIVE_VERSION = 2
MATCHED = "MATCHED"
UNVERIFIED = "UNVERIFIED"
INVALID = "INVALID"
_HASH = re.compile(r"[0-9a-fA-F]{64}")
_UNSET = object()


@dataclass
class LegacyCheck:
    status: str
    issues: list[str] = field(default_factory=list)
    configured: bool = False
    normalized_path: str | None = None
    baseline_path: str | None = None
    baseline_sha256: str | None = None
    baseline_contains: bool | None = None
    baseline_version: int | None = None
    native_paths: frozenset | None = None

    @property
    def allowed(self):
        """Legacy compatibility or frozen membership only; not action authority."""
        return self.status in {UNVERIFIED, MATCHED}

    @property
    def matched(self):
        return self.status == MATCHED

    def to_dict(self):
        return {"status": self.status, "issues": list(self.issues),
                "configured": self.configured, "allowed": self.allowed,
                "matched": self.matched, "normalized_path": self.normalized_path,
                "baseline_path": self.baseline_path,
                "baseline_sha256": self.baseline_sha256,
                "baseline_contains": self.baseline_contains,
                "baseline_version": self.baseline_version,
                "native_paths": sorted(self.native_paths) if self.native_paths is not None else None,
                "source_verification": UNVERIFIED,
                "semantic_entailment": "REVIEW_REQUIRED",
                "action_authorization": False}


def normalize_path(path):
    """Absolute lexical identity; Windows drives/UNC are case insensitive.

    No rule file is opened or resolved through filesystem aliases. Callers that
    use resolved file paths should use that same identity when building the root
    baseline. Windows identities remain portable in offline regression fixtures.
    """
    try:
        value = os.fspath(path)
    except TypeError as exc:
        raise ValueError("rule path must be an absolute filesystem path") from exc
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("rule path must be an absolute filesystem path")
    windows = value.replace("/", "\\")
    if windows.lower().startswith("\\\\?\\unc\\"):
        windows = "\\\\" + windows[8:]
    elif windows.startswith("\\\\?\\"):
        windows = windows[4:]
    if windows.startswith("\\\\.\\"):
        raise ValueError("device paths are not rule filesystem identities")
    if PureWindowsPath(windows).is_absolute():
        return ntpath.normcase(ntpath.normpath(windows))
    if Path(value).is_absolute():
        return os.path.normpath(value)
    raise ValueError("rule path must be absolute, not relative or drive relative")


def _sha256(value, label):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError(label + " must be a 64-character hexadecimal SHA256")
    return value.lower()


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key in rule baseline")
        value[key] = item
    return value


def _entries(value):
    if not isinstance(value, dict):
        raise ValueError("baseline entries must be a path-to-meaning-SHA256 object")
    result = {}
    for path, meaning_sha256 in value.items():
        normalized = normalize_path(path)
        if normalized in result:
            raise ValueError("duplicate normalized absolute path in rule baseline")
        result[normalized] = _sha256(meaning_sha256, "meaning_sha256")
    return dict(sorted(result.items()))


def _native_paths(value, entries):
    if not isinstance(value, list):
        raise ValueError("baseline native_paths must be an explicit path list")
    selected = set()
    for path in value:
        normalized = normalize_path(path)
        if normalized in selected:
            raise ValueError("duplicate normalized native path in rule baseline")
        if normalized not in entries:
            raise ValueError("baseline native_paths must be a subset of entries")
        selected.add(normalized)
    return frozenset(selected)


def build_baseline(entries, *, native_paths=_UNSET):
    """Pure generator from explicit root-selected paths and caller-made hashes.

    This does not search vaults, read rule text, write configuration, or enroll new
    paths. An empty explicit mapping deliberately admits no legacy cards.
    """
    entries = _entries(entries)
    if native_paths is _UNSET:
        return {"version": VERSION, "entries": entries}
    return {"version": NATIVE_VERSION, "entries": entries,
            "native_paths": sorted(_native_paths(native_paths, entries))}


@dataclass(frozen=True)
class _Baseline:
    version: int
    entries: object
    native_paths: frozenset | None


@lru_cache(maxsize=1)
def _parse_baseline(raw):
    # Reuse pure normalization only for byte-identical input. Callers still
    # reread bytes and verify the fixed pin on every use; verdicts are not cached.
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("baseline must be a JSON object")
    version = value.get("version")
    if type(version) is not int or version not in (VERSION, NATIVE_VERSION):
        raise ValueError("unsupported rule baseline version; expected integer 1 or 2")
    required = {"version", "entries"} | ({"native_paths"} if version == NATIVE_VERSION else set())
    if set(value) != required:
        raise ValueError("baseline schema requires exactly " + ", ".join(sorted(required)))
    entries = _entries(value["entries"])
    native_paths = _native_paths(value["native_paths"], entries) if version == NATIVE_VERSION else None
    return _Baseline(version, MappingProxyType(entries), native_paths)


def _load_baseline(*, options=None):
    options = memspec.config_options() if options is None else options
    if not isinstance(options, dict):
        return LegacyCheck(INVALID, ["rule baseline configuration must be an object"]), None
    has_path = PATH_OPTION in options
    has_pin = PIN_OPTION in options
    if not has_path and not has_pin:
        return LegacyCheck(UNVERIFIED, [
            "legacy rule baseline is not configured; original source and scope remain UNVERIFIED"]), None
    result = LegacyCheck(INVALID, configured=True)
    if not has_path or not has_pin:
        result.issues.append("configured legacy baseline requires both " + PATH_OPTION + " and " + PIN_OPTION)
        return result, None
    try:
        configured_path = options[PATH_OPTION]
        # Validate absolute identity before native I/O; retain actual path casing.
        normalize_path(configured_path)
        result.baseline_path = os.fspath(configured_path)
        pin = _sha256(options[PIN_OPTION], PIN_OPTION)
    except ValueError as exc:
        result.issues.append(str(exc))
        return result, None
    try:
        raw = Path(result.baseline_path).read_bytes()
    except OSError as exc:
        result.issues.append("configured legacy baseline cannot be read: " + str(exc))
        return result, None
    result.baseline_sha256 = hashlib.sha256(raw).hexdigest()
    if result.baseline_sha256 != pin:
        result.issues.append("legacy baseline bytes differ from fixed " + PIN_OPTION + "; do not auto-update the pin")
        return result, None
    try:
        baseline = _parse_baseline(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        result.issues.append("invalid legacy baseline: " + str(exc))
        return result, None
    result.baseline_version = baseline.version
    result.native_paths = baseline.native_paths
    result.status = MATCHED
    return result, baseline


def native_membership(*, options=None):
    """Fresh pinned membership for native consumers; never source authority.

    None native_paths preserves v1/unconfigured behavior. Invalid metadata has
    no fallback. The full entries meaning fence in check() remains unchanged.
    """
    return _load_baseline(options=options)[0]


def check(path, meaning_sha256, *, options=None):
    """Fresh full-membership/meaning comparison; v2 scopes no other consumer."""
    result, baseline = _load_baseline(options=options)
    if baseline is None:
        return result
    result.status = INVALID
    try:
        result.normalized_path = normalize_path(path)
        meaning_sha256 = _sha256(meaning_sha256, "current meaning_sha256")
    except ValueError as exc:
        result.issues.append(str(exc))
        return result
    expected = baseline.entries.get(result.normalized_path)
    result.baseline_contains = expected is not None
    if expected is None:
        result.issues.append("legacy rule path is not in the frozen baseline; no automatic enrollment")
    elif expected != meaning_sha256:
        result.issues.append("legacy rule body or execution meaning differs from the frozen baseline")
    else:
        result.status = MATCHED
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=
        "Generate a baseline candidate from explicit path/hash entries; stdout only, never adopt it.")
    parser.add_argument("--entries-json", required=True,
                        help="Explicit JSON object: selected absolute rule paths => caller-computed meaning SHA256")
    parser.add_argument("--native-paths-json", help="Explicit JSON path list for a v2 candidate; never inferred or adopted")
    args = parser.parse_args(argv)
    try:
        raw = Path(args.entries_json).read_bytes()
        entries = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs)
        selected = json.loads(Path(args.native_paths_json).read_bytes().decode("utf-8-sig"),
                              object_pairs_hook=_unique_pairs) if args.native_paths_json else None
        document = (build_baseline(entries, native_paths=selected) if args.native_paths_json
                    else build_baseline(entries))
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        parser.error(str(exc))
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
