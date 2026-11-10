"""Synthetic assertions and shell operands; no private transcript fixtures."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parents[1] / 'adapters' / 'claude'),
                str(Path(__file__).resolve().parent)]
import unittest
from epitype import shell_text_exec, tool_evidence
import stop_gate as stop
import pretooluse_gate as pre
from turn_check_regression import decision


class ClaimPrecision(unittest.TestCase):
    def gap(self, text, **kwargs):
        card = decision(require_when=r'修好了', require_text=r'根因', **kwargs)
        return stop._requirement_gap(card, text, [])

    def test_shared_non_assertions(self):
        for text in ('「修好了」', '"修好了"', "'修好了'", '`修好了`',
                     '```text\n修好了\n```', '如果修好了就交付。', '假設修好了。',
                     '我尚未修好了。', '我沒有說修好了。', '我不會說修好了。',
                     '產品修好了。', '使用者修好了。', '攻擊者修好了。',
                     'if 修好了 then publish', 'I have not 修好了', 'user 修好了'):
            with self.subTest(text=text):
                self.assertIsNone(self.gap(text))

    def test_current_assertion_and_contrast(self):
        for text in ('我修好了。', '使用者沒修好，但我修好了。',
                     '我沒有說修好了，但我修好了。', '「修好了」是舊說法。我修好了。'):
            with self.subTest(text=text):
                self.assertIsNotNone(self.gap(text))

    def test_required_evidence_still_works(self):
        self.assertIsNone(self.gap('我修好了，根因是索引錯位。'))

    def test_root_boundary_and_current_refusal(self):
        card = decision(key='root-fix-no-evasion', require_when=r'做不到|沒辦法|查不到|請你執行',
                        require_text='試過')
        for text in ('背景工作正在跑，我繼續等待。', '我查不到舊連結，已改用本機資料。',
                     '我沒辦法確定來源，現有證據不足。', '使用者做不到這個操作。',
                     '我之前說做不到任務，現在撤回。'):
            self.assertIsNone(stop._requirement_gap(card, text, []))
        self.assertIsNotNone(stop._requirement_gap(card, '我做不到這個任務。', []))
        self.assertIsNotNone(stop._requirement_gap(card, '請你執行剩下的工作。', []))

    def test_owner_quote_claim_only(self):
        card = decision(key='owner-words-verbatim', require_when=r'owner 原話', require_text='「')
        for text in ('owner 原話不得寫入公開測試。', '我在討論 owner 原話欄位。',
                     'owner 原話是「採用方案丙」。', 'owner 原話是 "採用方案丙"。',
                     'owner 原話如下：\n「採用方案丙。」'):
            self.assertIsNone(stop._requirement_gap(card, text, []))
        self.assertIsNotNone(stop._requirement_gap(card, 'owner 原話是採用方案丙。', []))
        self.assertIsNotNone(stop._requirement_gap(card, '「另一件事」。owner 原話是採用方案丙。', []))
        self.assertIsNotNone(stop._requirement_gap(card, '「另一件事」，owner 原話是採用方案丙。', []))

    def test_correction_requires_self_and_accepts_completed_read(self):
        card = decision(key='correction-carries-a-recheck', require_when='判斷錯了|我錯了', require_text='重查')
        self.assertIsNone(stop._requirement_gap(card, '只有判斷錯了才重開工作。', []))
        self.assertIsNone(stop._requirement_gap(card, '同事判斷錯了。', []))
        self.assertIsNotNone(stop._requirement_gap(card, '我錯了。', []))
        call = tool_evidence.Call('read-1', 'Read', {'file_path': 'sample.txt'}, '', 1,
                                  finished=2, success=True)
        turn = stop._turn()._replace(evidence=(call,))
        self.assertIsNone(stop._requirement_gap(card, '我錯了。', [], turn))
        call.success = False
        self.assertIsNotNone(stop._requirement_gap(card, '我錯了。', [], turn))
        call = tool_evidence.Call('shell-1', 'Bash', {'command': "echo 'cat sample.txt'"}, 'C:/example',
                                  1, finished=2, success=True)
        turn = turn._replace(evidence=(call,))
        self.assertIsNotNone(stop._requirement_gap(card, '我錯了。', [], turn))
        call.payload = {'command': 'cat sample.txt'}
        self.assertIsNone(stop._requirement_gap(card, '我錯了。', [], turn))

    def test_technical_request_and_explicit_plain_language(self):
        card = decision(key='plain-words-to-owner', forbidden=('PreToolUse',))
        technical = stop._turn(prompt='請診斷工具執行前的檢查。')
        self.assertIsNone(stop._forbidden_fragment(card, 'PreToolUse 負責檢查工具。', [], turn=technical))
        self.assertIsNotNone(stop._forbidden_fragment(card, 'PreToolUse 負責檢查工具。', []))
        short = stop._turn(prompt='可以')._replace(owner_task='請診斷工具檢查。\n可以')
        self.assertIsNone(stop._forbidden_fragment(card, 'PreToolUse 負責檢查工具。', [], turn=short))
        plain = short._replace(prompt='不要用專有名詞。')
        self.assertIsNotNone(stop._forbidden_fragment(card, 'PreToolUse 負責檢查工具。', [], turn=plain))

    def test_acceptance_requires_actual_lowering(self):
        card = decision(key='no-green-by-lowering-the-bar', forbidden=('為了測試通過',))
        for text in ('不是為了測試通過而提高預算。', '為了測試通過，我修復錯位。'):
            self.assertIsNone(stop._forbidden_fragment(card, text, []))
        self.assertIsNotNone(stop._forbidden_fragment(card, '為了測試通過，我降低標準。', []))

    def test_third_attempt_is_not_third_edit_of_same_file(self):
        card = decision(key='no-third-try-same-way', forbidden=(r'第三輪[^。]{0,20}同一個', r'第三次[^。]{0,20}同樣'))
        self.assertIsNone(stop._forbidden_fragment(card, '第三輪修補同一個檔案。', []))
        self.assertIsNotNone(stop._forbidden_fragment(card, '失敗兩次，我第三次使用同樣的方法。', []))

    def test_third_attempt_uses_identical_calls_and_failure_results(self):
        card = decision(key='no-third-try-same-way', forbidden=(r'第三次.{0,20}同樣',))
        calls = tuple(tool_evidence.Call(str(i), 'Bash', {'command': 'check sample.txt'},
                      '/example', i * 2, finished=i * 2 + 1, success=False,
                      response='same failure') for i in range(3))
        turn = stop._turn()._replace(evidence=calls)
        text = '我第三次使用同樣的方法。'
        self.assertIsNotNone(stop._forbidden_fragment(card, text, [], turn=turn))
        calls[1].response = 'different failure'
        self.assertIsNone(stop._forbidden_fragment(card, text, [], turn=turn))
        calls[1].response = 'same failure'
        calls[2].payload = {'command': 'check --updated sample.txt'}
        self.assertIsNone(stop._forbidden_fragment(card, text, [], turn=turn))

    def test_symptom_requires_current_completion(self):
        for text in ('現在還不能說修好了。', '如果修好了就回報。'):
            self.assertIsNone(self.gap(text, key='symptom-suppression-is-not-a-fix'))
        self.assertIsNotNone(self.gap('我修好了。', key='symptom-suppression-is-not-a-fix'))


class JsonlDeletion(unittest.TestCase):
    def hit(self, command, ps=False):
        card = 'scar-keep-diagnostic-jsonl-' + ('ps-' if ps else '') + '20260916'
        return pre._jsonl_delete_matches(card, {'command': command,
                'shell': 'powershell' if ps else 'bash'}, 'Bash', [])

    def test_bash_data_is_not_deletion(self):
        for command in ("rg 'rm *.jsonl' notes.txt", "echo 'rm sample.jsonl'",
                        "python -c 'print(\"rm sample.jsonl\")'",
                        "cat <<'END'\nrm sample.jsonl\nEND", 'rm sample.log; echo sample.jsonl',
                        '# rm sample.jsonl\necho done', "'rm sample.jsonl'",
                        'rm --help sample.jsonl'):
            with self.subTest(command=command):
                self.assertFalse(self.hit(command))

    def test_bash_real_operands(self):
        for command in ('rm sample.jsonl', 'rm -f -- "sample data.jsonl"',
                        'cat sample.txt | rm *.jsonl', 'echo "$(rm sample.jsonl)"'):
            with self.subTest(command=command):
                self.assertTrue(self.hit(command))

    def test_powershell_data_is_not_deletion(self):
        for command in ("Select-String -Pattern 'Remove-Item sample.jsonl' -Path notes.txt",
                        "Write-Output 'Remove-Item sample.jsonl'",
                        "@'\nRemove-Item sample.jsonl\n'@ | Set-Content sample.txt",
                        'Remove-Item sample.log; Write-Output sample.jsonl', '# Remove-Item sample.jsonl',
                        "Remove-Item -Exclude '*.jsonl' sample.log", 'Remove-Item -WhatIf sample.jsonl'):
            with self.subTest(command=command):
                self.assertFalse(self.hit(command, True))

    def test_powershell_real_operands(self):
        for command in ('Remove-Item sample.jsonl', 'Remove-Item -LiteralPath "sample data.jsonl" -Force',
                        'Get-Date; Remove-Item *.jsonl'):
            self.assertTrue(self.hit(command, True))

    def test_existing_shell_parser_keeps_command_groups_separate(self):
        self.assertEqual(shell_text_exec.simple_commands("echo 'rm x.jsonl'; rm y.jsonl"),
                         [['echo', 'rm x.jsonl'], ['rm', 'y.jsonl']])


if __name__ == '__main__':
    unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
