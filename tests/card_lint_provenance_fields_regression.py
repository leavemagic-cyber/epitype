"""card_lint and core_gen must accept the same rule approval credential; all records are synthetic."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import json
import tempfile
import unittest
from datetime import date

from epitype import card_lint, memspec, rule_provenance
from rule_source_fixtures import source_bound_card

TODAY = date(2026, 10, 10)


def rule_text(name, *, layer='resident', order=True, status='active', extra=''):
    return ('---\nname: ' + name + '\ndescription: 2026-10-10 合成規則卡\n'
            'layer: ' + layer + '\nsection: alpha\n' + ('order: 7\n' if order else '')
            + 'status: ' + status + '\ntext: Synthetic rule sentence for the fixtures.\n'
            'unenforceable: 判斷型（合成）\naliases:\n  - 合成規則\n  - synthetic rule\n'
            + extra + 'metadata:\n  type: rule\n---\nbody\n')


def with_approval(text, approval):
    fields, _ = memspec.frontmatter_text(text)
    document = json.loads(fields[rule_provenance.FIELD])
    document['approval'] = approval
    old = 'rule_provenance: ' + json.dumps(fields[rule_provenance.FIELD], ensure_ascii=False)
    assert old in text
    return text.replace(old, 'rule_provenance: '
                        + json.dumps(rule_provenance.serialize(document), ensure_ascii=False), 1)


class ProvenanceApprovalFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text, *, bound=True):
        path = self.vault / (name + '.md')
        if bound:
            text = source_bound_card(text, path, source_root=self.root / 'src', case_id=name)
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def findings(self, path):
        _type, findings, _derived, _layer = card_lint._check_card(path, path.name, TODAY)
        return findings

    def required(self, path):
        prefix = memspec.CARD_LINT_MISSING_REQUIRED_REASON.format(fields='')
        return {reason[len(prefix):] for level, rule, reason in self.findings(path)
                if level == card_lint.FAIL and rule == 'required'}

    def test_verified_approval_satisfies_decider_and_approval_fields(self):
        path = self.write('approved', rule_text('approved'))
        path.write_text(with_approval(path.read_text(encoding='utf-8'),
                                      {'by': 'owner', 'at': '2026-10-04'}), encoding='utf-8', newline='\n')
        self.assertEqual(self.required(path), set())
        self.assertFalse([f for f in self.findings(path) if f[1] == 'rule-source'])

    def test_missing_approval_still_fails_each_field(self):
        path = self.write('unapproved', rule_text('unapproved'))
        self.assertEqual(self.required(path), {'decided_by', 'approved_by', 'approved_at'})

    def test_malformed_approval_date_is_not_a_credential(self):
        path = self.write('baddate', rule_text('baddate'))
        path.write_text(with_approval(path.read_text(encoding='utf-8'),
                                      {'by': 'owner', 'at': '昨天'}), encoding='utf-8', newline='\n')
        self.assertEqual(self.required(path), {'decided_by', 'approved_by', 'approved_at'})

    def test_unverified_provenance_grants_no_credential(self):
        path = self.write('tampered', rule_text('tampered'))
        text = with_approval(path.read_text(encoding='utf-8'), {'by': 'owner', 'at': '2026-10-04'})
        # Approval cannot authenticate a different generated rule sentence.
        path.write_text(text.replace('\ntext: Synthetic rule sentence for the fixtures.',
                                     '\ntext: Changed rule sentence for the fixtures.', 1),
                        encoding='utf-8', newline='\n')
        self.assertTrue([f for f in self.findings(path) if f[1] == 'rule-source'])
        self.assertEqual(self.required(path), {'decided_by', 'approved_by', 'approved_at'})

    def test_flat_fields_without_provenance_still_pass(self):
        path = self.write('flat', rule_text('flat', extra='decided_by: three-way\n'
                                             'approved_by: synthetic-pair\napproved_at: 2026-09-09\n'),
                          bound=False)
        self.assertEqual(self.required(path), set())

    def test_order_required_only_for_generated_layers(self):
        flat = 'decided_by: three-way\napproved_by: synthetic-pair\napproved_at: 2026-09-09\n'
        recall = self.write('recall', rule_text('recall', layer='recall', order=False, extra=flat), bound=False)
        resident = self.write('resident', rule_text('resident', order=False, extra=flat), bound=False)
        floor = self.write('floor', rule_text('floor', layer='floor', order=False, extra=flat), bound=False)
        self.assertEqual(self.required(recall), set())
        self.assertEqual(self.required(resident), {'order'})
        self.assertEqual(self.required(floor), {'order'})

    def test_enforce_mismatch_ignored_only_for_silent_statuses(self):
        def card(name, status):
            return ('---\nname: ' + name + '\ndescription: 2026-10-10 合成行為卡\nstatus: ' + status
                    + '\nenforce: text\naliases:\n  - 合成\nmetadata:\n  type: feedback\n---\nbody\n')
        active = self.write('active', card('active', 'active'), bound=False)
        retired = self.write('retired', card('retired', 'superseded'), bound=False)
        self.assertTrue([f for f in self.findings(active) if f[1] == 'enforce'])
        self.assertFalse([f for f in self.findings(retired) if f[1] == 'enforce'])


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.argv.remove('--selftest')
    unittest.main()
