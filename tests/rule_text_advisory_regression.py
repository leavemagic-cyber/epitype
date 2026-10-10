"""Rule text cost is advisory; malformed rule fields still fail."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from datetime import date
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from epitype import card_lint, memspec
from rule_source_fixtures import source_bound_card


class RuleTextAdvisory(unittest.TestCase):
    def test_utf8_byte_boundary_is_advisory(self):
        limit = memspec.RULE_TEXT_MAX_BYTES
        for prefix in ('x' * limit, '規' * (limit // 3) + 'x' * (limit % 3)):
            with self.subTest(prefix=prefix[:1]):
                self.assertEqual(len(prefix.encode('utf-8')), limit)
                self.assertEqual(list(card_lint._rule_findings({'text': prefix}, [])), [])
                findings = list(card_lint._rule_findings({'text': prefix + 'x'}, []))
                self.assertEqual(findings, [(card_lint.WARN, 'rule-text',
                    memspec.RULE_TEXT_TOO_LONG_REASON.format(
                        field=memspec.RULE_TEXT_FIELD, size=limit + 1, limit=limit))])

    def test_long_rule_passes_strict_cli_with_full_text_preserved(self):
        with tempfile.TemporaryDirectory(prefix='epitype-rule-text-') as temp_dir:
            root = Path(temp_dir).resolve()
            vault = root / 'vault'
            vault.mkdir()
            config = root / 'config.json'
            config.write_text('{}\n', encoding='utf-8')
            path = vault / 'rule-long.md'
            sentence = 'x' * (memspec.RULE_TEXT_MAX_BYTES + 1)
            text = ('---\nname: rule-long\ndescription: 2026-10-10 合成規則卡\n'
                    'layer: resident\nsection: alpha\norder: 1\ntext: ' + sentence + '\n'
                    'decided_by: three-way\napproved_by: synthetic-pair\n'
                    'approved_at: 2026-10-10\nunenforceable: 判斷型（合成）\n'
                    'aliases:\n  - 合成規則\n  - synthetic rule\n'
                    'metadata:\n  type: rule\n---\nbody\n')
            text = source_bound_card(text, path, source_root=root / 'src', case_id='rule-long')
            path.write_text(text, encoding='utf-8')
            before = path.read_bytes()
            output = io.StringIO()
            with patch.dict('os.environ', {'EPITYPE_CONFIG': str(config)}):
                code = card_lint.main([str(vault), '--strict', '--json',
                                       '--today', date(2026, 10, 10).isoformat()], output=output)
            report = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(report['fail'], 0)
            self.assertEqual(report['warn'], 1)
            self.assertEqual([(f['level'], f['rule']) for c in report['cards']
                              for f in c['findings']], [(card_lint.WARN, 'rule-text')])
            self.assertEqual(path.read_bytes(), before)
            fields, problem = memspec.frontmatter_text(path.read_text(encoding='utf-8'))
            self.assertIsNone(problem)
            self.assertEqual(fields['text'], sentence)

    def test_long_multiline_rule_keeps_shape_failures(self):
        findings = list(card_lint._rule_findings({
            'text': 'x' * (memspec.RULE_TEXT_MAX_BYTES + 1) + '\nsecond line',
            'layer': 'nowhere', 'approved_at': 'yesterday', 'order': 'not-an-integer',
        }, []))
        self.assertEqual([(level, rule) for level, rule, _ in findings], [
            (card_lint.FAIL, 'layer'), (card_lint.FAIL, 'date'),
            (card_lint.FAIL, 'field-shape'), (card_lint.WARN, 'rule-text'),
            (card_lint.FAIL, 'field-shape'),
        ])


if __name__ == '__main__':
    unittest.main(argv=[arg for arg in sys.argv if arg != '--selftest'])
