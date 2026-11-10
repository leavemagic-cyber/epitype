"""Reconstruct pre-merge vaults read-only and measure duplicate candidate coverage.

Usage: python tests/calibrate_dupes.py --plan MERGE1.md --plan MERGE2.md
       --vault G=/path/governance --vault V=/path/project --vault T=/path/other
       --out /path/summary.json
Only --out and temporary reconstructed copies are written. No card bodies or
local absolute source paths are included in the saved calibration summary.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import dupes


def git(vault, *args):
    return subprocess.run(["git", "-C", str(vault), *args], check=True,
                          capture_output=True).stdout


def read_plans(plans, vaults):
    def canonical(raw):
        raw = raw.strip().replace("\\", "/")
        match = re.match(r"(.+?\.md)\b", raw)
        if not match:
            raise ValueError("missing card path in plan")
        raw = match[1]
        for label, vault in vaults.items():
            prefix = vault.as_posix().rstrip("/") + "/"
            if raw.casefold().startswith(prefix.casefold()):
                return label + "/" + raw[len(prefix):]
        if re.match(r"^[GVT]/", raw):
            return raw
        return "external/" + raw.rsplit("/", 1)[-1]

    groups = []
    for number, plan in enumerate(plans, 1):
        text = plan.read_text(encoding="utf-8-sig")
        for index, section in enumerate(re.split(r"(?m)^## ", text)[1:], 1):
            heading, _, body = section.partition("\n")
            verdict = re.search(r"\|\s*(MERGE|SUPERSEDE|CONFLICT|KEEP|MOVE)\b", heading)
            card_line = re.search(r"(?m)^cards:\s*(.*)", body)
            if not verdict or not card_line:
                continue
            paths = []
            for raw in card_line[1].split(" ; "):
                paths.append(canonical(raw))
            groups.append({"id": f"plan{number}:{index}", "heading": heading.split("|", 1)[0].strip(),
                           "verdict": verdict[1], "paths": sorted(set(paths))})
        # Later amendments are authoritative over the earlier heading. They are
        # not additional judged groups and must not inflate the denominator.
        for amendment in re.split(r"(?m)^### ", text)[1:]:
            title = amendment.split("\n", 1)[0]
            override = re.search(r"(?m)^verdict override:\s*(\w+)", amendment)
            if override:
                targets = [g for g in groups if g["id"].startswith(f"plan{number}:") and g["heading"] in title]
                if len(targets) != 1:
                    raise ValueError("amendment must identify exactly one existing group")
                targets[0]["verdict"] = override[1]
                added = re.search(r"(?m)^cards add:\s*(.*)", amendment)
                if added:
                    targets[0]["paths"] = sorted(set(targets[0]["paths"]) | {canonical(p) for p in added[1].split(" ; ")})
            shared = re.search(r"(?m)^cards add（共同核對一次）:\s*(.*)", amendment)
            if shared:
                # The final amendment names the five contract pointers explicitly.
                pointers = re.findall(r"v[1-5]_\d{8}", amendment)
                targets = [g for g in groups if g["id"].startswith(f"plan{number}:") and
                           g["heading"].startswith("pointer ") and any(p in g["heading"] for p in pointers)]
                if len(targets) != 5:
                    raise ValueError("shared keeper amendment must identify five contract pointers")
                for target in targets:
                    target["paths"] = sorted(set(target["paths"]) | {canonical(shared[1])})
    return groups


def reconstruct(vaults, root):
    versions = {}
    for label, vault in vaults.items():
        commits = git(vault, "log", "--reverse", "--format=%H %s").decode("utf-8").splitlines()
        commit = next(line.split(" ", 1)[0] for line in commits
                      if line.split(" ", 1)[1].startswith("merge(cards): fold duplicate cards"))
        parent = git(vault, "rev-parse", commit + "^").decode().strip()
        target = root / label
        target.mkdir()
        tree = git(vault, "ls-tree", "-rz", parent).split(b"\0")
        count = 0
        for entry in tree:
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            path = Path(raw_path.decode("utf-8"))
            # Do not materialise symlinks, hidden/generated trees or capture records.
            if metadata.split()[0] not in (b"100644", b"100755") or path.suffix.lower() != ".md":
                continue
            if any(p.startswith((".", "_")) for p in path.parts) or set(path.parts) & dupes.CAPTURE_DIRS:
                continue
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(git(vault, "show", f"{parent}:{path.as_posix()}"))
            count += 1
        versions[label] = {"merge": commit, "parent": parent, "markdown_copies": count}
        print(f"reconstructed {label}: {count} markdown files", flush=True)
    return versions


def evaluate(cards, pairs, groups, threshold):
    clusters = dupes.clusters_from_pairs(cards, pairs, threshold)
    memberships = [{c["vault"].replace("\\", "/").rsplit("/", 1)[-1] + "/" +
                    Path(c["path"]).relative_to(c["vault"]).as_posix() for c in cluster["cards"]}
                   for cluster in clusters]
    positives = [g for g in groups if g["verdict"] in ("MERGE", "SUPERSEDE", "CONFLICT")
                 and not any(p.startswith("external/") for p in g["paths"])]
    positive_cards = set().union(*(set(g["paths"]) for g in positives))
    matched = [g for g in positives if len(g["paths"]) >= 2 and any(set(g["paths"]) <= c for c in memberships)]
    pair_hit = [g for g in positives if any(len(set(g["paths"]) & c) >= 2 for c in memberships)]
    matched_ids = {g["id"] for g in matched}
    available = {c.vault.name + "/" + c.path.relative_to(c.vault).as_posix() for c in cards}
    eligible = [g for g in positives if len(g["paths"]) >= 2 and set(g["paths"]) <= available]
    return {"threshold": threshold, "clusters": len(clusters), "groups_found": len(matched),
            "judged_groups": len(positives), "recall": len(matched) / len(positives),
            "groups_with_any_pair": len(pair_hit),
            "eligible_groups": len(eligible), "eligible_recall": len(matched) / len(eligible) if eligible else 0,
            "extra_clusters": sum(not any(set(g["paths"]) <= c for g in positives if len(g["paths"]) >= 2) for c in memberships),
            "clusters_with_no_judged_positive_card": sum(not c.intersection(positive_cards) for c in memberships),
            "largest_cluster": max(map(len, memberships), default=0),
            "keep_groups_co_clustered": sum(any(len(set(g["paths"]) & c) >= 2 for c in memberships)
                                             for g in groups if g["verdict"] == "KEEP"),
            "by_verdict": {v: {"found": sum(g["id"] in matched_ids for g in positives if g["verdict"] == v),
                                  "total": sum(g["verdict"] == v for g in positives)}
                           for v in ("MERGE", "SUPERSEDE", "CONFLICT")},
            "missed_group_ids": [g["id"] for g in positives if g["id"] not in matched_ids]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="append", type=Path, required=True)
    parser.add_argument("--vault", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, help="reuse an owned temporary reconstruction while calibrating")
    args = parser.parse_args(argv)
    vaults = {label: Path(path).resolve() for label, path in (v.split("=", 1) for v in args.vault)}
    groups = read_plans(args.plan, vaults)
    with tempfile.TemporaryDirectory(prefix="epitype-dupes-calibration-") as temp_dir:
        root = args.snapshot_dir.resolve() if args.snapshot_dir else Path(temp_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        manifest = root / "versions.json"
        if manifest.is_file():
            versions = json.loads(manifest.read_text(encoding="utf-8"))
            for label, vault in vaults.items():
                if git(vault, "rev-parse", versions[label]["merge"] + "^").decode().strip() != versions[label]["parent"]:
                    raise ValueError("cached snapshot version mismatch")
        else:
            versions = reconstruct(vaults, root)
            manifest.write_text(json.dumps(versions, indent=2), encoding="utf-8")
        cards, errors = dupes.load_cards([root / label for label in vaults])
        pairs = dupes.pair_scores(cards, 0.1)
        available = {c.vault.name + "/" + c.path.relative_to(c.vault).as_posix() for c in cards}
        output = {"versions": versions, "plans_sha256": [hashlib.sha256(p.read_bytes()).hexdigest() for p in args.plan],
                  "plan_verdicts": dict(Counter(g["verdict"] for g in groups)),
                  "out_of_scope_group_ids": [g["id"] for g in groups if any(p.startswith("external/") for p in g["paths"])],
                  "cards_scanned": len(cards), "scan_error_count": len(errors),
                  "groups_with_unavailable_members": [g["id"] for g in groups if not set(g["paths"]) <= available],
                  "metrics": [evaluate(cards, pairs, groups, t) for t in (0.25, 0.35, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85)]}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        for metric in output["metrics"]:
            print({k: metric[k] for k in ("threshold", "clusters", "groups_found", "judged_groups", "recall", "extra_clusters", "largest_cluster")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
