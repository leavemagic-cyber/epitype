"""A37: a product budget must not suppress an authorized current rule."""
import io
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import core_gen, host_sync, memspec


def run():
    with tempfile.TemporaryDirectory(prefix='epitype-core-advice-') as name:
        root = Path(name)
        vault = core_gen._build_vault(root / 'vault')
        out = root / 'core.md'
        with patch.dict('os.environ', {'EPITYPE_CONFIG': str(root / 'config.json')}):
            result = core_gen.generate([vault], out, cap_bytes=10, config={})
            if '--baseline' in sys.argv:
                assert result['status'] == core_gen.STATUS_OVER_CAP
                assert not out.exists()
                print('BEFORE: exact authorized rule output refused solely by product cap.')
                return
            assert out.is_file() and result['status'] == core_gen.STATUS_WRITTEN
            assert result['over_cap'] and result['cap'] == 10
            assert out.read_text(encoding='utf-8') == result['text']
            log = io.StringIO()
            core_gen._print(result, log)
            assert 'NOTE' in log.getvalue() and '10' in log.getvalue()
            with patch.object(memspec, 'config_options', return_value={'core_cap_bytes': 10}):
                plan = host_sync._plan('claude', 'claude', None, root / 'CLAUDE.md',
                    lambda: {memspec.HOST_SYNC_RULES_REGION: result['text']}, home=root)
            assert not plan.problems and len(plan.regions) == 1
            assert plan.notices and 'core_cap_bytes' in plan.notices[0]
            bad = core_gen._build_vault(root / 'bad')
            card = bad / 'floor-1.md'
            card.write_text(core_gen._card_text('floor-1', 'floor', 'base', 10,
                'Synthetic rule missing approval.', None, None,
                approved_by='', approved_at=''), encoding='utf-8')
            denied = core_gen.generate([bad], root / 'bad.md', cap_bytes=10, config={})
            assert denied['status'] == core_gen.STATUS_UNAPPROVED
            assert not (root / 'bad.md').exists()
            damaged = root / 'damaged.md'
            begin, end = memspec.HOST_SYNC_MARKERS['rules']
            damaged.write_text(begin + '\n' + begin + '\n' + end, encoding='utf-8')
            with patch.object(memspec, 'config_options', return_value={'core_cap_bytes': 10}):
                refused = host_sync._plan('claude', 'claude', None, damaged,
                    lambda: {memspec.HOST_SYNC_RULES_REGION: result['text']}, home=root)
            assert refused.damaged and refused.problems and not refused.regions
            print('PASS: over-cap writes complete rules with notice; missing approval and damaged markers still refuse.')


if __name__ == '__main__':
    run()
