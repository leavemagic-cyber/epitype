"""Offline duplicate candidates; the lead AI settles cards under the owner's rule."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

try:
    from . import cardscan, memsearch, memspec
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from epitype import cardscan, memsearch, memspec


DEFAULT_THRESHOLD = 0.35
SHINGLE_SIZE = 3
MACHINE_FIELDS = frozenset({
    "decision_key", "forbidden", "verify", "on_hit", "rule_provenance",
    "owner_quote", "carried_by",
})
CAPTURE_DIRS = frozenset({"grants", "rulings", "corrections"})
DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[^\]]*)\]\]")
OWNER = re.compile(r"owner|使用者原話|用戶原話|主人原話", re.I)


@dataclass
class Card:
    path: Path
    vault: Path
    name: str
    aliases: set[str]
    fields: dict
    machine_fields: list[str]
    owner_quote: dict | None
    newest: str
    shingles: set[str]
    links: set[str]
    fingerprint: str


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def shingles(text, size=SHINGLE_SIZE):
    value = normalize(text)
    return {value[i:i + size] for i in range(max(0, len(value) - size + 1))}


def body_shingles(text):
    # Shared audit footers and link destinations are not duplicate rule wording.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = WIKILINK.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    return shingles(text)


def body_fingerprint(text):
    front, closing = memspec.split_frontmatter(text)
    if front is None or closing is None:
        raise ValueError("card has no complete frontmatter")
    body = "".join(text.splitlines(keepends=True)[closing + 1:])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def pair_key(left, right):
    return tuple(sorted(os.path.normcase(str(Path(p).resolve())) for p in (left, right)))


def judged_path(governance):
    return Path(governance) / "_drafts" / memspec.DUPES_DIRECTORY / "judged.json"


def load_judged(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("pairs"), list):
        raise ValueError(f"invalid judged record: {path}")
    result = {}
    for row in data["pairs"]:
        if (not isinstance(row, dict)
                or any(not isinstance(row.get(k), str) or not row[k].strip()
                       for k in ("left", "right", "reason", "date"))
                or row.get("verdict") not in ("duplicate", "overlap", "unrelated")
                or any(not isinstance(row.get(k), str) or not re.fullmatch(r"[a-f0-9]{64}", row[k])
                       for k in ("left_fingerprint", "right_fingerprint"))):
            raise ValueError(f"invalid judged pair: {path}")
        date.fromisoformat(row["date"])
        key = pair_key(row["left"], row["right"])
        if key[0] == key[1] or key in result:
            raise ValueError(f"repeated judged pair: {path}")
        result[key] = row
    return result


def parse_verdicts(text, count):
    verdicts = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = re.fullmatch(r"(DUPLICATE|OVERLAP|UNRELATED)\s+(\d+)\s*\|\s*(.+)", line.strip(), re.I)
        if match:
            verdict, number, reason = match.groups()
        else:
            match = re.fullmatch(
                r"(OTHER-VAULT|SKIPPED-BINDING)\s+(\d+)\s*\|\s*(.+)", line.strip(), re.I)
            if not match:
                raise ValueError(f"invalid verdict line {line_number}")
            kind, number, tail = match.groups()
            pattern = (r"(DUPLICATE|OVERLAP|UNRELATED)\s*\|\s*(.+)" if kind.upper() == "OTHER-VAULT"
                       else r"[^|]+\|\s*(DUPLICATE|OVERLAP|UNRELATED)\s*[:：]\s*(.+)")
            match = re.fullmatch(pattern, tail, re.I)
            if not match:
                raise ValueError(f"invalid verdict line {line_number}")
            verdict, reason = match.groups()
        number = int(number)
        if number in verdicts or not 1 <= number <= count or not reason.strip():
            raise ValueError(f"repeated or out-of-range cluster at verdict line {line_number}")
        verdicts[number] = (verdict.casefold(), reason.strip())
    if set(verdicts) != set(range(1, count + 1)):
        raise ValueError("verdicts must cover every report cluster exactly once")
    return verdicts


def import_verdicts(report_path, verdicts_path, governance, judged_on=None):
    report = json.loads(Path(report_path).read_text(encoding="utf-8-sig"))
    if (not isinstance(report, dict) or not isinstance(report.get("clusters"), list)
            or not isinstance(report.get("date"), str)):
        raise ValueError("invalid duplicate report")
    stamp = date.fromisoformat(judged_on or report["date"]).isoformat()
    verdicts = parse_verdicts(Path(verdicts_path).read_text(encoding="utf-8-sig"), len(report["clusters"]))
    path = judged_path(governance)
    records = load_judged(path)
    imported, current = set(), set()
    for number, cluster in enumerate(report["clusters"], 1):
        if (not isinstance(cluster, dict) or not isinstance(cluster.get("cards"), list)
                or not isinstance(cluster.get("pairs"), list) or not cluster["pairs"]):
            raise ValueError("invalid report cluster")
        fingerprints = {}
        for card in cluster["cards"]:
            if not isinstance(card, dict) or not isinstance(card.get("path"), str):
                raise ValueError("invalid report card")
            fingerprint = card.get("body_fingerprint")
            if fingerprint is None:
                # Legacy reports have no snapshot; disclose that these hashes are from import time.
                fingerprint = body_fingerprint(Path(card["path"]).read_text(encoding="utf-8-sig"))
                current.add(card["path"])
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                raise ValueError("invalid report body fingerprint")
            fingerprints[card["path"]] = fingerprint
        verdict, reason = verdicts[number]
        for pair in cluster["pairs"]:
            if not isinstance(pair, dict) or any(not isinstance(pair.get(k), str) for k in ("left", "right")):
                raise ValueError("invalid report pair")
            left, right = pair["left"], pair["right"]
            if left not in fingerprints or right not in fingerprints:
                raise ValueError("report pair is missing a card")
            key = pair_key(left, right)
            if key[0] == key[1] or key in imported:
                raise ValueError("repeated report pair")
            imported.add(key)
            records[key] = {"left": left, "right": right, "verdict": verdict, "reason": reason,
                            "date": stamp, "left_fingerprint": fingerprints[left],
                            "right_fingerprint": fingerprints[right],
                            "fingerprint_source": "import" if left in current or right in current else "report"}
    data = {"schema_version": 1, "pairs": [records[key] for key in sorted(records)]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"path": str(path), "clusters": len(verdicts), "pairs": len(imported),
            "current_fingerprints": len(current)}


def suppress_judged(cards, pairs, records):
    remaining = []
    for pair in pairs:
        left, right = cards[pair["left"]], cards[pair["right"]]
        row = records.get(pair_key(left.path, right.path))
        if row and row["verdict"] in ("overlap", "unrelated"):
            fingerprints = {os.path.normcase(str(Path(row[side]).resolve())): row[side + "_fingerprint"]
                            for side in ("left", "right")}
            if all(fingerprints[os.path.normcase(str(card.path))] == card.fingerprint for card in (left, right)):
                continue
        remaining.append(pair)
    return remaining, len(pairs) - len(remaining)


def machine_fields(front, fields):
    present = set()
    for index, line in enumerate(front):
        match = memspec.TOP_LEVEL_FIELD.match(line)
        if not match:
            continue
        key, _value = match.groups()
        if key not in MACHINE_FIELDS and not key.startswith("host_native"):
            continue
        children = []
        for child in front[index + 1:]:
            if child and not child[0].isspace() and not child.startswith("-"):
                break
            if child.strip() and not child.lstrip().startswith("#"):
                children.append(child)
        if fields.get(key, "").strip() not in ("", "[]", "{}", "null", "~") or children:
            present.add(key)
    return sorted(present)


def _dates(text):
    compact = re.sub(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", r"\1-\2-\3", text)
    return [value for value in DATE.findall(compact) if memspec.is_iso_date(value)]


def newest_owner_quote(fields, body):
    """Keep quote evidence separate from unrelated edit dates and filenames."""
    candidates = []
    raw = fields.get("owner_quote", "").strip()
    if raw:
        dates = _dates(raw)
        if not dates:
            dates = _dates(fields.get("current_decision_at", ""))
        candidates.append({"date": max(dates) if dates else None, "text": raw,
                           "source": "owner_quote"})
    heading = ""
    quote_section = False
    for line in body.splitlines():
        if line.startswith("#"):
            heading = line
            quote_section = bool(OWNER.search(line))
        # Only explicitly attributed quotes count; a dated maintenance note does not.
        if (OWNER.search(line) or quote_section) and ("「" in line or line.lstrip().startswith(">")):
            dates = _dates(line) or _dates(heading)
            candidates.append({"date": max(dates) if dates else None, "text": line.strip(),
                               "source": "body"})
    return max(candidates, key=lambda q: q["date"] or "", default=None)


def load_cards(vaults):
    cards, errors = [], []
    for vault in sorted({Path(v).resolve() for v in vaults}, key=str):
        if not vault.is_dir():
            raise ValueError(f"not a vault directory: {vault}")
        # cardscan already excludes links, generated/private trees and MEMORY.md.
        for path in cardscan.card_files(vault):
            if CAPTURE_DIRS.intersection(p.casefold() for p in path.relative_to(vault).parts[:-1]):
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
                front, closing = memspec.split_frontmatter(text)
                if front is None:
                    continue
                if closing is None:
                    errors.append({"path": str(path), "error": memspec.CARD_LINT_NO_BOUNDARY_REASON})
                    continue
                fields, problem = memspec.frontmatter_text(text)
                if not (fields.get("name") or fields.get("description")):
                    continue
                if fields.get("status", "").casefold() == "superseded" or fields.get("scope", "").casefold() == "historical":
                    continue
                if problem:
                    errors.append({"path": str(path), "error": problem})
                parsed = memsearch._parse_frontmatter("\n".join(front))
                aliases = {normalize(a) for a in parsed["aliases"].splitlines() if normalize(a)}
                body = "\n".join(text.splitlines()[closing + 1:])
                quote = newest_owner_quote(fields, body)
                dated = _dates("\n".join(fields.get(k, "") for k in (
                    "current_decision_at", "last_verified_at", "updated_at", "created_at", "date")))
                dated += _dates(path.stem)
                if quote and quote["date"]:
                    dated.append(quote["date"])
                if not dated:
                    dated.append(date.fromtimestamp(path.stat().st_mtime).isoformat())
                machine = machine_fields(front, fields)
                cards.append(Card(path, vault, fields.get("name") or path.stem, aliases,
                                  fields, machine, quote, max(dated, default=""),
                                  body_shingles(body), {s.casefold().removesuffix(".md") for s in WIKILINK.findall(text)},
                                  body_fingerprint(text)))
            except (OSError, UnicodeError, ValueError) as exc:
                errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return cards, errors


def pair_scores(cards, minimum=0.0):
    """Exact Jaccard and IDF cosine over character shingles; no sampling loss."""
    body_index, alias_index, key_index, name_index = (defaultdict(list) for _ in range(4))
    frequencies = Counter(s for card in cards for s in card.shingles)
    weights = {s: (1 + math.log((len(cards) + 1) / (count + 1))) ** 2
               for s, count in frequencies.items()}
    norms = [math.sqrt(sum(weights[s] for s in c.shingles)) for c in cards]
    identities = defaultdict(set)
    for i, card in enumerate(cards):
        for identity in (card.path.stem, card.path.relative_to(card.vault).with_suffix("").as_posix()):
            identities[identity.casefold()].add(i)
    linked = [set().union(*(identities.get(link, set()) for link in c.links)) if c.links else set() for c in cards]
    rows = []
    for i, card in enumerate(cards):
        overlap = Counter(j for shingle in card.shingles for j in body_index[shingle])
        weighted_overlap = Counter()
        for shingle in card.shingles:
            for j in body_index[shingle]:
                weighted_overlap[j] += weights[shingle]
        shared_alias = set(j for alias in card.aliases for j in alias_index[alias])
        key = card.fields.get("decision_key", "").strip()
        same_key = set(key_index[key]) if key else set()
        name = normalize(card.name)
        name_parts = shingles(name, 3)
        name_overlap = Counter(j for part in name_parts for j in name_index[part])
        name_candidates = {j for j, count in name_overlap.items() if count / max(1, len(name_parts)) >= 0.5}
        link_candidates = {j for j in linked[i] if j < i} | {j for j in range(i) if i in linked[j]}
        for j in sorted(set(overlap) | shared_alias | same_key | name_candidates | link_candidates):
            other = cards[j]
            common = overlap[j]
            union = len(card.shingles) + len(other.shingles) - common
            similarity = round(common / union, 12) if union else 0.0
            smaller = min(len(card.shingles), len(other.shingles))
            containment = round(common / smaller, 12) if smaller else 0.0
            cosine = round(min(1.0, weighted_overlap[j] / (norms[i] * norms[j])), 12) if norms[i] and norms[j] else 0.0
            score = max(similarity, cosine)
            signals = []
            if similarity:
                signals.append("body")
            if j in shared_alias:
                signals.append("shared_alias")
                score = max(score, 0.3 + 0.7 * cosine)
            if j in same_key:
                signals.append("decision_key")
                score = 1.0
            if j in linked[i] and i in linked[j]:
                signals.append("mutual_wikilink")
                score = max(score, 0.3 + 0.7 * cosine)
            elif j in link_candidates:
                signals.append("wikilink")
                score = max(score, 0.15 + 0.85 * cosine)
            if j in name_candidates:
                ratio = SequenceMatcher(None, name, normalize(other.name), autojunk=False).ratio()
                if ratio >= 0.85:
                    signals.append("near_name")
                    score = max(score, 0.2 * ratio + 0.8 * cosine)
            score = round(score, 12)
            if score >= minimum and score > 0:
                rows.append({"left": j, "right": i, "similarity": similarity,
                             "containment": containment, "weighted_cosine": cosine,
                             "score": score, "signals": signals})
        for shingle in card.shingles:
            body_index[shingle].append(i)
        for alias in card.aliases:
            alias_index[alias].append(i)
        if key:
            key_index[key].append(i)
        for part in name_parts:
            name_index[part].append(i)
    return rows


def clusters_from_pairs(cards, pairs, threshold=DEFAULT_THRESHOLD):
    parents = list(range(len(cards)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    accepted = [p for p in pairs if p["score"] >= threshold]
    for pair in accepted:
        a, b = root(pair["left"]), root(pair["right"])
        parents[b] = a
    groups = defaultdict(list)
    for i in range(len(cards)):
        groups[root(i)].append(i)
    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        # Machine checks are the first preference, never a ruling over owner wording.
        keep = sorted(members, key=lambda i: (not bool(cards[i].machine_fields),
                      -(int(cards[i].newest.replace("-", "")) if cards[i].newest else 0), str(cards[i].path)))[0]
        edges = [{**p, "left": str(cards[p["left"]].path), "right": str(cards[p["right"]].path)}
                 for p in accepted if root(p["left"]) == root(members[0])]
        result.append({"cards": [{"path": str(cards[i].path), "vault": str(cards[i].vault),
                                   "body_fingerprint": cards[i].fingerprint,
                                   "machine_fields": cards[i].machine_fields,
                                   "newest_owner_quote": cards[i].owner_quote,
                                   "newest_date": cards[i].newest or None} for i in members],
                       "pairs": edges, "suggested_keep": str(cards[keep].path)})
    return sorted(result, key=lambda c: c["cards"][0]["path"])


def detect(vaults, today=None, threshold=DEFAULT_THRESHOLD, governance=None):
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    from epitype import dream
    vaults = list(vaults)
    source = governance if governance is not None else dream.governance_vault(vaults) if vaults else None
    records = load_judged(judged_path(source)) if source is not None else {}
    cards, errors = load_cards(vaults)
    pairs, suppressed = suppress_judged(cards, pair_scores(cards, threshold), records)
    return {"schema_version": 1, "date": (today or date.today()).isoformat(), "threshold": threshold,
            "shingle_size": SHINGLE_SIZE, "cards_scanned": len(cards), "errors": errors,
            "suppressed_pairs": suppressed, "clusters": clusters_from_pairs(cards, pairs, threshold)}


def render_markdown(report):
    lines = [memspec.DUPES_TITLE, "", memspec.DUPES_POLICY, "",
             memspec.DUPES_SUMMARY.format(date=report["date"], cards=report["cards_scanned"],
                                         clusters=len(report["clusters"]), threshold=report["threshold"]),
             memspec.DUPES_SUPPRESSED.format(pairs=report.get("suppressed_pairs", 0))]
    for number, cluster in enumerate(report["clusters"], 1):
        lines += ["", f"## {number}", "", memspec.DUPES_KEEP.format(path=cluster["suggested_keep"])]
        for card in cluster["cards"]:
            lines += ["", f"- {card['path']}",
                      "  " + memspec.DUPES_FIELDS.format(fields=", ".join(card["machine_fields"]) or "—")]
            quote = card["newest_owner_quote"]
            if quote:
                label = memspec.DUPES_QUOTE if quote["date"] else memspec.DUPES_UNDATED_QUOTE
                lines.append("  " + label.format(date=quote["date"], text=quote["text"]))
            else:
                lines.append("  " + memspec.DUPES_NO_QUOTE)
        for pair in cluster["pairs"]:
            lines += ["", f"  {pair['left']} ↔ {pair['right']}",
                      "  " + memspec.DUPES_PAIR.format(similarity=f"{pair['similarity']:.4f}",
                          score=f"{pair['score']:.4f}", signals=", ".join(pair["signals"]))]
    if report["errors"]:
        lines += ["", memspec.DUPES_ERRORS]
        lines += [f"- {error['path']}: {error['error']}" for error in report["errors"]]
    return "\n".join(lines) + "\n"


def write_report(report, governance):
    directory = Path(governance) / "_drafts" / memspec.DUPES_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (report["date"].replace("-", "") + ".md")
    path.write_text(render_markdown(report), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=memspec.DUPES_HELP)
    parser.add_argument("vaults", nargs="*", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--governance", type=Path, help=memspec.DUPES_GOVERNANCE_HELP)
    parser.add_argument("--judge", nargs=2, type=Path, metavar=("REPORT", "VERDICTS"), help=memspec.DUPES_JUDGE_HELP)
    parser.add_argument("--date", type=date.fromisoformat, help=memspec.DUPES_DATE_HELP)
    args = parser.parse_args(argv)
    try:
        from epitype import dream
        vaults = ([memsearch._resolve_vault(v) for v in args.vaults] if args.vaults
                  else [] if args.judge and args.governance is not None else dream.configured_vaults())
        governance = args.governance if args.governance is not None else dream.governance_vault(vaults)
        if args.judge:
            result = import_verdicts(*args.judge, governance, args.date.isoformat() if args.date else None)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(memspec.DUPES_JUDGED.format(**result))
                if result["current_fingerprints"]:
                    print(memspec.DUPES_LEGACY.format(cards=result["current_fingerprints"]))
            return 0
        report = detect(vaults, today=args.date, threshold=args.threshold, governance=governance)
        write_report(report, governance)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_markdown(report),
              end="\n" if args.json else "")
        return 1 if report["errors"] else 0
    except (OSError, ValueError) as exc:
        print(f"DUPES ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
