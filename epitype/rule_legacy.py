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
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re

from . import memspec

PATH_OPTION = "rule_source_baseline"
PIN_OPTION = "rule_source_baseline_sha256"
VERSION = 1
MATCHED = "MATCHED"
UNVERIFIED = "UNVERIFIED"
INVALID = "INVALID"
_HASH = re.compile(r"[0-9a-fA-F]{64}")


@dataclass
class LegacyCheck:
    status: str
    issues: list[str] = field(default_factory=list)
    configured: bool = False
    normalized_path: str | None = None
    baseline_path: str | None = None
    baseline_sha256: str | None = None
    baseline_contains: bool | None = None

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


def build_baseline(entries):
    """Pure generator from explicit root-selected paths and caller-made hashes.

    This does not search vaults, read rule text, write configuration, or enroll new
    paths. An empty explicit mapping deliberately admits no legacy cards.
    """
    return {"version": VERSION, "entries": _entries(entries)}


def _parse_baseline(raw):
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("baseline must be a JSON object")
    if type(value.get("version")) is not int or value["version"] != VERSION:
        raise ValueError("unsupported rule baseline version; expected integer 1")
    if set(value) != {"version", "entries"}:
        raise ValueError("baseline schema requires only version and entries")
    return _entries(value["entries"])


def check(path, meaning_sha256, *, options=None):
    """Freshly compare one legacy rule to the frozen, pinned baseline.

    Missing both options preserves compatibility as UNVERIFIED. Partial or bad
    configuration fails closed. Each enabled call rereads baseline bytes and
    checks the fixed pin before parsing; no mtime/size or positive cache is used.
    """
    options = memspec.config_options() if options is None else options
    if not isinstance(options, dict):
        return LegacyCheck(INVALID, ["rule baseline configuration must be an object"])
    has_path = PATH_OPTION in options
    has_pin = PIN_OPTION in options
    if not has_path and not has_pin:
        return LegacyCheck(UNVERIFIED, [
            "legacy rule baseline is not configured; original source and scope remain UNVERIFIED"])
    result = LegacyCheck(INVALID, configured=True)
    if not has_path or not has_pin:
        result.issues.append("configured legacy baseline requires both " + PATH_OPTION + " and " + PIN_OPTION)
        return result
    try:
        result.normalized_path = normalize_path(path)
        meaning_sha256 = _sha256(meaning_sha256, "current meaning_sha256")
        configured_path = options[PATH_OPTION]
        # Validate absolute identity before native I/O; retain actual path casing.
        normalize_path(configured_path)
        result.baseline_path = os.fspath(configured_path)
        pin = _sha256(options[PIN_OPTION], PIN_OPTION)
    except ValueError as exc:
        result.issues.append(str(exc))
        return result
    try:
        raw = Path(result.baseline_path).read_bytes()
    except OSError as exc:
        result.issues.append("configured legacy baseline cannot be read: " + str(exc))
        return result
    result.baseline_sha256 = hashlib.sha256(raw).hexdigest()
    if result.baseline_sha256 != pin:
        result.issues.append("legacy baseline bytes differ from fixed " + PIN_OPTION + "; do not auto-update the pin")
        return result
    try:
        entries = _parse_baseline(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        result.issues.append("invalid legacy baseline: " + str(exc))
        return result
    expected = entries.get(result.normalized_path)
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
    args = parser.parse_args(argv)
    try:
        raw = Path(args.entries_json).read_bytes()
        entries = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs)
        document = build_baseline(entries)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        parser.error(str(exc))
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
