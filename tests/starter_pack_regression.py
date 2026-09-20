# -*- coding: utf-8 -*-
"""出貨的那幾張卡，在真機上真的擋得住，也真的不誤擋。

陌生人 `pip install epitype && epitype install` 之後拿到的是空庫。starter 是他的第一批卡，
所以這幾張的錯誤特別貴：第一次見面就誤擋，他會把整套東西拔掉，而不是去修那張卡。

因此這裡不另外複製一份比對邏輯——每一句 `example_blocks` 都送進真的閘（說話規則走
stop_gate._handle，守衛走 pretooluse_gate._handle），每一句 `example_allows` 也是。
自己重寫一份判斷會讓測試通過、真機照樣誤擋，那是最貴的一種綠燈。

整個測試在臨時目錄裡跑：HOME、USERPROFILE、EPITYPE_CONFIG 全部改指到那裡，所以不會碰到
真的記憶庫，也不會在真的 _GATE_LOG.jsonl 上寫任何一列。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]

import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from epitype import card_lint, memspec, starter

import _hook_common as common
import pretooluse_gate as pretool
import stop_gate


def _fields(path):
    fields, _problem = memspec.frontmatter_fields(path)
    return fields


def _sequence(path, field):
    text = path.read_text(encoding="utf-8-sig")
    front, _closing = memspec.split_frontmatter(text)
    return list(memspec.sequence_items(front or (), field))


class StarterCardsAreRealRules(unittest.TestCase):
    """卡片本身：體檢零 FAIL、例句兩向都對、記號齊全。"""

    def setUp(self):
        self.cards = starter.shipped_cards()
        self.assertTrue(self.cards, "出貨目錄裡一張卡都沒有，陌生人拿到的還是空庫")

    def test_every_card_passes_the_real_lint(self):
        for card in self.cards:
            _type, findings = card_lint.check_card(card, card.name)
            bad = [item for item in findings if item[0] == "FAIL"]
            self.assertEqual(bad, [], f"{card.name} 體檢有 FAIL")

    def test_no_card_has_an_example_warning(self):
        # 例句那一族的 WARN 只有一種意思：這張卡擋得住東西卻沒附例句。出貨的卡不准有。
        for card in self.cards:
            _type, findings = card_lint.check_card(card, card.name)
            noisy = [item for item in findings if item[1] == "example"]
            self.assertEqual(noisy, [], f"{card.name} 的例句檢查有話要說")

    def test_every_card_carries_the_starter_marker(self):
        for card in self.cards:
            self.assertTrue(
                starter.carries_marker(card),
                f"{card.name} 沒有 {memspec.STARTER_FIELD} 記號，移除那條路徑就認不出它",
            )
            self.assertEqual(
                _fields(card).get(memspec.STARTER_FIELD, "").strip(), memspec.STARTER_MARK)

    def test_every_card_declares_both_directions(self):
        for card in self.cards:
            for field in (memspec.EXAMPLE_BLOCKS_FIELD, memspec.EXAMPLE_ALLOWS_FIELD):
                self.assertTrue(_sequence(card, field), f"{card.name} 缺 {field}")

    def test_the_set_covers_both_gates_and_the_note_disposition(self):
        # 出貨的意義是「讓人看見規則的三種形狀」，少一種就少一種說明力。
        kinds = set()
        for card in self.cards:
            fields = _fields(card)
            if fields.get(memspec.ACTION_GUARD_TOOL_FIELD, "").strip():
                kinds.add("guard")
            if _sequence(card, memspec.FORBIDDEN_FIELD):
                kinds.add("forbidden")
            if fields.get(memspec.REQUIRE_WHEN_FIELD, "").strip():
                kinds.add("require")
            if fields.get(memspec.ON_HIT_FIELD, "").strip() == memspec.ON_HIT_NOTE:
                kinds.add("note")
        self.assertEqual(kinds, {"guard", "forbidden", "require", "note"})


class _VaultCase(unittest.TestCase):
    """臨時家目錄 + 臨時庫；真機的設定檔與閘門紀錄一個位元組都不會被碰到。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-starter-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = self.root / "config.json"
        common.write_config(self.config, [self.vault])
        environment = patch.dict(os.environ, {
            memspec.EPITYPE_CONFIG_ENV: str(self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        marker = patch.object(
            stop_gate, "recall_marker_directory",
            lambda session: self.root / "markers" / common.session_component(session))
        marker.start()
        self.addCleanup(marker.stop)

    def install_one(self, card):
        (self.vault / card.name).write_bytes(card.read_bytes())
        cache = pretool._guard_cache(self.vault)
        if cache.exists():
            cache.unlink()

    def log_kinds(self):
        path = self.vault / memspec.GATE_LOG_FILENAME
        if not path.is_file():
            return []
        return [json.loads(line)["kind"]
                for line in path.read_text(encoding="utf-8").splitlines()]


class StarterCardsBlockThroughTheRealGates(_VaultCase):
    """每一句例句都走真的閘。這一題紅了，代表卡片在真機上的行為跟它自己的宣告不一樣。"""

    def speak(self, message, session):
        return stop_gate._handle(
            {"session_id": session, "cwd": str(self.root),
             "last_assistant_message": message}, time.monotonic(), [])

    def shell(self, tool, command):
        return pretool._handle(
            {"tool_name": tool, "tool_input": {"command": command},
             "session_id": "starter-guard"}, time.monotonic(), [])

    def denial(self, value):
        return (value or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason")

    def test_speech_cards_block_their_own_blocking_examples(self):
        for index, card in enumerate(starter.shipped_cards()):
            fields = _fields(card)
            if fields.get(memspec.ACTION_GUARD_TOOL_FIELD, "").strip():
                continue
            if fields.get(memspec.ON_HIT_FIELD, "").strip() == memspec.ON_HIT_NOTE:
                continue  # 只提醒的卡不擋回合；它自己那一題在下面
            with self.subTest(card=card.name):
                self.setUp()
                self.install_one(card)
                for number, sentence in enumerate(
                        _sequence(card, memspec.EXAMPLE_BLOCKS_FIELD)):
                    value = self.speak(sentence, f"block-{index}-{number}")
                    self.assertIsNotNone(value, f"{card.name} 擋不到自己的例句：{sentence}")
                    self.assertEqual(value["decision"], "block")

    def test_speech_cards_let_their_own_allowing_examples_through(self):
        for index, card in enumerate(starter.shipped_cards()):
            if _fields(card).get(memspec.ACTION_GUARD_TOOL_FIELD, "").strip():
                continue
            with self.subTest(card=card.name):
                self.setUp()
                self.install_one(card)
                for number, sentence in enumerate(
                        _sequence(card, memspec.EXAMPLE_ALLOWS_FIELD)):
                    self.assertIsNone(
                        self.speak(sentence, f"allow-{index}-{number}"),
                        f"{card.name} 誤擋：{sentence}")

    def test_the_whole_set_together_still_lets_the_allowing_examples_through(self):
        # 一張一張測過關，五張一起裝仍可能互相誤擋——陌生人拿到的是五張一起。
        for card in starter.shipped_cards():
            self.install_one(card)
        allowed = [
            sentence for card in starter.shipped_cards()
            for sentence in _sequence(card, memspec.EXAMPLE_ALLOWS_FIELD)
        ]
        for number, sentence in enumerate(allowed):
            self.assertIsNone(self.speak(sentence, f"together-{number}"),
                              f"五張一起裝時誤擋：{sentence}")

    def test_guard_cards_deny_their_own_blocking_examples(self):
        for card in starter.shipped_cards():
            tool = _fields(card).get(memspec.ACTION_GUARD_TOOL_FIELD, "").strip()
            if not tool:
                continue
            with self.subTest(card=card.name):
                self.setUp()
                self.install_one(card)
                for command in _sequence(card, memspec.EXAMPLE_BLOCKS_FIELD):
                    reason = self.denial(self.shell(tool, command))
                    self.assertIsNotNone(reason, f"{card.name} 擋不到：{command}")
                    self.assertIn(_fields(card)[memspec.NAME_FIELD], reason)
                for command in _sequence(card, memspec.EXAMPLE_ALLOWS_FIELD):
                    self.assertIsNone(self.denial(self.shell(tool, command)),
                                      f"{card.name} 誤擋：{command}")

    def test_a_guard_card_does_not_reach_into_another_tool(self):
        # Bash 卡與 PowerShell 卡是兩張，正因為一張守衛只認一個工具家族。
        bash = next(card for card in starter.shipped_cards()
                    if _fields(card).get(memspec.ACTION_GUARD_TOOL_FIELD) == "Bash")
        self.install_one(bash)
        command = _sequence(bash, memspec.EXAMPLE_BLOCKS_FIELD)[0]
        self.assertIsNotNone(self.denial(self.shell("Bash", command)))
        self.assertIsNone(self.denial(self.shell("PowerShell", command)),
                          "Bash 卡不該管到 PowerShell；那是另一張卡的事")

    def test_the_note_card_leaves_a_note_instead_of_ending_the_turn(self):
        card = next(card for card in starter.shipped_cards()
                    if _fields(card).get(memspec.ON_HIT_FIELD, "").strip()
                    == memspec.ON_HIT_NOTE)
        self.install_one(card)
        sentence = _sequence(card, memspec.EXAMPLE_BLOCKS_FIELD)[0]
        self.assertIsNone(self.speak(sentence, "note-1"),
                          "只提醒的卡不准為了用詞多跑一輪")
        self.assertEqual(self.log_kinds(), [memspec.STOP_NOTE_LOG_KIND],
                         "不擋不等於不記：夜間統計要看得到這一次")


class StarterCopyIsSafe(_VaultCase):
    """複製與移除：使用者的檔案永遠優先，dry-run 不留痕。"""

    def run_starter(self, *arguments):
        output = io.StringIO()
        with patch.object(starter.sys, "stdout", output):
            code = starter.main(["--vault", str(self.vault), *arguments])
        return code, output.getvalue()

    def names(self):
        return sorted(path.name for path in self.vault.glob("*.md"))

    def test_copy_writes_every_shipped_card(self):
        code, text = self.run_starter()
        self.assertEqual(code, 0)
        self.assertEqual(self.names(),
                         sorted(card.name for card in starter.shipped_cards()))
        self.assertIn("STARTER: wrote 5, skipped 0", text)
        self.assertIn("TRY:", text, "裝完要告訴他怎麼安全地看一次攔截")

    def test_copy_is_idempotent_and_never_overwrites(self):
        self.run_starter()
        mine = self.vault / starter.shipped_cards()[0].name
        mine.write_text("---\nname: mine\n---\nI edited this.\n", encoding="utf-8")
        before = mine.read_bytes()
        _code, text = self.run_starter()
        self.assertEqual(mine.read_bytes(), before, "使用者改過的卡被蓋掉了")
        self.assertIn("STARTER: wrote 0, skipped 5", text)
        self.assertIn("already in the vault", text)

    def test_dry_run_writes_nothing(self):
        code, text = self.run_starter("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), [])
        self.assertIn("DRY-RUN write", text)
        self.assertNotIn("TRY:", text, "什麼都沒寫就不要叫人去試")

    def test_remove_takes_back_only_what_is_still_ours(self):
        self.run_starter()
        edited = self.vault / starter.shipped_cards()[0].name
        text = edited.read_text(encoding="utf-8")
        edited.write_text(text + "\nMy own note.\n", encoding="utf-8")
        unmarked = self.vault / starter.shipped_cards()[1].name
        unmarked.write_text("---\nname: unmarked\n---\nno marker here\n", encoding="utf-8")
        code, report = self.run_starter("--remove")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), sorted([edited.name, unmarked.name]))
        self.assertIn("edited since it was installed", report)
        self.assertIn(f"no {memspec.STARTER_FIELD} marker", report)
        self.assertIn("STARTER: removed 3, kept 2", report)

    def test_remove_dry_run_deletes_nothing(self):
        self.run_starter()
        before = self.names()
        _code, report = self.run_starter("--remove", "--dry-run")
        self.assertEqual(self.names(), before)
        self.assertIn("DRY-RUN remove", report)

    def test_list_reports_state_without_touching_the_vault(self):
        _code, report = self.run_starter("--list")
        self.assertEqual(self.names(), [])
        self.assertIn("[not installed]", report)
        self.run_starter()
        _code, second = self.run_starter("--list")
        self.assertNotIn("[not installed]", second)

    def test_the_default_vault_comes_from_the_configured_governance_vault(self):
        # --vault 沒給的時候不准去猜，也不准寫到別的地方去：EPITYPE_CONFIG 說了算。
        self.assertEqual(starter.default_vault(), self.vault)
        output = io.StringIO()
        with patch.object(starter.sys, "stdout", output):
            self.assertEqual(starter.main([]), 0)
        self.assertEqual(self.names(),
                         sorted(card.name for card in starter.shipped_cards()))


class TheInstallerPointsAtAnEmptyVault(_VaultCase):
    """空庫裝完什麼都不會擋。那一行提示是陌生人唯一會看到的線索，所以它要真的會出現，
    也要在庫裡已經有武裝卡時真的閉嘴——每次都喊的提示等於沒有提示。"""

    def hint(self, vaults):
        from install import graft

        output = io.StringIO()
        graft._starter_hint(vaults, ROOT, output)
        return output.getvalue()

    def test_an_empty_vault_gets_exactly_one_line(self):
        text = self.hint([self.vault])
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("epitype starter", text)

    def test_a_vault_with_an_armed_card_says_nothing(self):
        self.install_one(starter.shipped_cards()[0])
        self.assertEqual(self.hint([self.vault]), "")

    def test_an_unarmed_card_is_not_mistaken_for_a_rule(self):
        # 一張只有 name／description 的卡讀得到、喚得回，但一個字都擋不住。
        (self.vault / "notes.md").write_text(
            "---\nname: notes\ndescription: just a note\n---\nbody\n", encoding="utf-8")
        self.assertIn("epitype starter", self.hint([self.vault]))


class StarterShipsInTheWheel(unittest.TestCase):
    """打包設定沒帶到這個目錄的話，陌生人裝完還是空手——讀 pyproject 就看得出來。"""

    def test_package_data_covers_every_shipped_card(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'epitype = ["{memspec.STARTER_DIRECTORY}/*.md"]', text,
                      "pyproject 的 package-data 沒有把 starter 卡納進輪子")
        self.assertIn('"epitype",', text, "epitype 套件本身要在 packages 清單裡")
        for card in starter.shipped_cards():
            self.assertEqual(card.suffix, ".md",
                             f"{card.name} 不是 .md，那一行 glob 帶不走它")

    def test_the_card_directory_resolves_beside_the_module(self):
        # 輪子裡沒有 repo 根目錄，所以路徑只能從 __file__ 推。
        self.assertEqual(starter.CARD_DIRECTORY.parent,
                         Path(starter.__file__).resolve().parent)
        self.assertEqual(starter.CARD_DIRECTORY.name, memspec.STARTER_DIRECTORY)


def _selftest():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(case)
        for case in (
            StarterCardsAreRealRules,
            StarterCardsBlockThroughTheRealGates,
            StarterCopyIsSafe,
            TheInstallerPointsAtAnEmptyVault,
            StarterShipsInTheWheel,
        )
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    bad = len(result.failures) + len(result.errors)
    print("starter_pack_regression: %s (%d/%d)"
          % ("OK" if not bad else "FAIL", result.testsRun - bad, result.testsRun))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
