import sys; sys.dont_write_bytecode = True
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import harvest


class OwnerText(unittest.TestCase):
    def row(self, host, text):
        if host == 'Claude':
            return {'type': 'user', 'message': {'content': text}}
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': text}]}}

    def test_both_hosts_keep_owner_after_machine_block(self):
        for host in ('Claude', 'Codex'):
            self.assertEqual(harvest.owner_text(self.row(host,
                '<pasted_content id="1">舊資料</pasted_content>\n請先查正本')), '請先查正本')
            self.assertIsNone(harvest.owner_text(self.row(host, '<system-reminder>機器</system-reminder>')))
            self.assertIsNone(harvest.owner_text(self.row(host, '<pasted_content>沒有收尾')))

    def test_codex_request_and_claude_sidechain(self):
        self.assertEqual(harvest.owner_text(self.row('Codex',
            '<environment_context>環境</environment_context>\n## My request:\n保留原始數字')), '保留原始數字')
        row = self.row('Claude', '請修改規則')
        row['isSidechain'] = True
        self.assertIsNone(harvest.owner_text(row))


if __name__ == '__main__':
    unittest.main()
