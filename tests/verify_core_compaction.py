"""Read-only live-vault verification: python tests/verify_core_compaction.py <vault>."""
import hashlib
import io
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import core_gen, host_sync, memspec


def verify(vault):
    vault = Path(vault).resolve()
    rules, errors = core_gen.collect_rules([vault])
    if errors:
        raise AssertionError(errors)
    selected = [rule for rule in rules if rule['layer'] in memspec.RULE_GENERATED_LAYERS]
    expected = {re.match(r'CORE-\d+', rule['text']).group(): rule for rule in selected}
    # The count follows the vault (26 in 2026-10, 18 after later merges); growth is judged by
    # the accepted core baseline, not a number pinned here. Each rule's text is compared below.
    assert len(expected) == len(selected), 'duplicate CORE numbers in the vault'
    # No configured byte caps any more (owner 2026-11-17): host_sync judges the whole host
    # file by host facts, and core growth is compared with the accepted baseline.
    snapshots, summary, union = {}, {}, set()
    for host in ('claude', 'codex'):
        path = host_sync.host_path(host)
        snapshots[path] = path.read_bytes()
        raw = snapshots[path].decode('utf-8-sig').replace('\r\n', '\n')
        old = host_sync._locate(raw, 'rules')[0][1]
        recovery = {}
        new = host_sync.rules_text([vault], host, current=old, recovery=recovery)
        old_rules = dict(re.findall(r'^- (CORE-\d+)\. (.*)$', old, re.M))
        new_rules = dict(re.findall(r'^- (CORE-\d+)\. (.*)$', new, re.M))
        required = {key: rule['text'].split('. ', 1)[1] for key, rule in expected.items()
                    if not rule['hosts'] or host in rule['hosts']}
        assert old_rules == new_rules == required, f'{host}: rule text or membership changed'
        union.update(new_rules)
        assert '<!--r:' not in new and new.count(core_gen.RECOVERY_PREFIX) == 1
        assert '範圍覆寫：{}' not in new and '來源範圍基底：' not in new
        entries = host_sync._previous_rules(new, host, recovery=recovery)
        assert len(entries) == len(required)
        old_bytes, new_bytes = (len((text + '\n').encode('utf-8')) for text in (old, new))
        plan = host_sync.plan_for(host, [vault])
        assert not plan.problems, plan.problems
        index = next(region for region in plan.regions if region.name == 'index')
        summary[host] = {'old_bytes': old_bytes, 'new_bytes': new_bytes,
                         'saved_bytes': old_bytes - new_bytes, 'exact_rules': len(required),
                         'index_bytes': len(index.text.encode('utf-8')),
                         'host_sha256': hashlib.sha256(snapshots[path]).hexdigest()}
    assert len(union) == len(expected)
    # Same entrypoint as `python -m epitype sync <vault>`; no --apply.
    output = io.StringIO()
    sync_code = host_sync.main([str(vault)], output=output)
    assert sync_code in (host_sync.EXIT_OK, host_sync.EXIT_DRIFT), output.getvalue()
    for path, before in snapshots.items():
        assert path.read_bytes() == before, f'Host file changed: {path}'
    summary.update(unique_rules=len(expected), sync_exit=sync_code, sync_output=output.getvalue(),
                   limits='host facts + core baseline')
    return summary


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(verify(sys.argv[1]), ensure_ascii=False, indent=2))
