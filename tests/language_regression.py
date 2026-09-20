# -*- coding: utf-8 -*-
"""顯示語言：英文裝機看得懂，繁中裝機一個位元組都沒變。

Epitype 的說明是英文出貨的，擋下來的那一句以前寫死繁中——陌生人裝完第一次被擋，讀到的
是一句他看不懂的話。這裡驗三件事：

1. `_EN` 每一把鍵的格式佔位符跟繁中原文完全一樣。對不上的話 KeyError 會在擋人的當下丟
   出來，而那一刻沒有人在看 traceback。
2. `EPITYPE_LANG=en` 時，出貨卡在**真的掛鉤行程**裡擋出來的理由不含任何中日韓字。行程內
   呼叫 `_handle` 量不到真實行為：語言是匯入時解析的。
3. 不設語言時，同樣兩個案例的理由逐字等於這個檔裡釘住的繁中原文。這幾串是 owner 機器
   上現在看到的那一句；改動任何接縫字都會讓它紅。

起子行程的題目刻意留到最少：run_all 是平行跑的，這個檔多起一個 Python 就等於在
SessionStart 的 5 秒預算上多壓一份負載，把別人的題目壓紅。解析規則那一族改用同一支
`_resolve_language()` 在行程內驗，只留一題子行程釘住「LANGUAGE 真的在匯入時綁定」。

整個測試在臨時目錄裡跑：HOME、USERPROFILE、TMP、EPITYPE_CONFIG 全部改指到那裡，所以不會
碰到真的記憶庫，也不會在真的 _GATE_LOG.jsonl 上寫任何一列。
"""
import sys

sys.dont_write_bytecode = True
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "adapters" / "claude")]

import io
import json
import os
import re
import string
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from epitype import memspec
from install import graft

ADAPTERS = ROOT / "adapters" / "claude"
PRETOOLUSE = ADAPTERS / "pretooluse_gate.py"
STOP = ADAPTERS / "stop_gate.py"
# 中日韓統一表意文字＋全形標點：翻漏的中文一定落在這兩段裡。
CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")

GUARD_CARD = "starter-stage-explicit-paths-bash.md"
SPEECH_CARD = "starter-no-hedged-completion.md"

# owner 機器上現在看到的那兩句，逐字。片段與建議取自出貨卡，所以卡片改了這裡也要改——
# 那正是重點：預設語言的輸出不准因為這次的改動而變形。
ZH_GUARD_REASON = (
    "🛑 傷疤卡（Stage explicit paths, not the whole tree）：這次 Bash 同時含有 "
    "「git add -A」——Run git status first, then name each path you actually changed"
)
ZH_STOP_REASON = (
    "⚖ 不要說「that should fix it」，請改寫。"
    "（No hedged completion：should work now is a guess wearing the clothes of a result）"
    "（剛才那一段 owner 已經看到了：只補缺的部分，不要整段重貼。）"
)


def _placeholders(value):
    """字串裡的欄位名集合（`{overlap:.0%}` 只算 overlap，`{{0,12}}` 不算）。"""
    return {name for _text, name, _spec, _conv in string.Formatter().parse(value)
            if name is not None}


def _card(name):
    return ROOT / "epitype" / "starter_cards" / name


def _sequence(path, field):
    front, _closing = memspec.split_frontmatter(path.read_text(encoding="utf-8-sig"))
    return list(memspec.sequence_items(front or (), field))


class PlaceholderParity(unittest.TestCase):
    """英文表跟繁中原文是同一組佔位符，而且每一把鍵真的存在。"""

    def test_every_key_names_an_existing_constant(self):
        for key in memspec._EN:
            self.assertIn(key, vars(memspec), f"_EN 的 {key} 不是 memspec 裡的名字")

    def test_placeholders_match_the_chinese_original(self):
        for key, english in memspec._EN.items():
            chinese = getattr(memspec, key)
            self.assertIs(type(english), type(chinese), f"{key} 的型別變了")
            if isinstance(english, dict):
                # 鍵是欄名，被閘門拿去比對；只有值是顯示字。
                self.assertEqual(set(english), set(chinese), f"{key} 的鍵變了")
                continue
            with self.subTest(key=key):
                self.assertEqual(_placeholders(english), _placeholders(chinese))

    def test_the_english_table_carries_no_chinese(self):
        for key, english in memspec._EN.items():
            values = english.values() if isinstance(english, dict) else [english]
            for value in values:
                self.assertIsNone(CJK.search(value), f"{key} 的英文版還有中文：{value}")

    def test_nothing_the_gates_match_on_is_translated(self):
        # 樣式翻掉＝閘擋的東西變了。這條是那個錯誤的防線。
        for key in memspec._EN:
            self.assertFalse(key.endswith(("_PATTERN", "_REGEX", "_FIELD", "_LOG_KIND")),
                             f"{key} 是拿去比對的東西，不准換語言")


class LanguageResolution(unittest.TestCase):
    """環境變數 → 設定檔 → 預設，而且設定檔壞掉不准讓任何一支停擺。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-lang-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "config.json"

    def resolve(self, config_text=None, language=None):
        """用的是產品自己那一支解析函式——另寫一份判斷等於沒驗到。"""
        environment = {}
        if config_text is None:
            # 指到一個不存在的路徑＝這台機器還沒有設定檔。
            environment[memspec.EPITYPE_CONFIG_ENV] = str(self.root / "absent.json")
        else:
            self.config.write_text(config_text, encoding="utf-8")
            environment[memspec.EPITYPE_CONFIG_ENV] = str(self.config)
        if language is None:
            removed = [memspec.EPITYPE_LANG_ENV]
        else:
            removed = []
            environment[memspec.EPITYPE_LANG_ENV] = language
        with patch.dict(os.environ, environment):
            for name in removed:
                os.environ.pop(name, None)
            return memspec._resolve_language()

    def test_no_config_and_no_env_is_traditional_chinese(self):
        self.assertEqual(self.resolve(), memspec.LANGUAGE_ZH)

    def test_config_without_the_field_is_traditional_chinese(self):
        self.assertEqual(self.resolve('{"vaults": []}'), memspec.LANGUAGE_ZH)

    def test_the_config_field_is_honoured(self):
        self.assertEqual(self.resolve('{"language": "en"}'), memspec.LANGUAGE_EN)
        self.assertEqual(self.resolve('﻿{"language": "en"}'), memspec.LANGUAGE_EN)

    def test_the_environment_variable_beats_the_config(self):
        self.assertEqual(self.resolve('{"language": "en"}', language="zh-TW"),
                         memspec.LANGUAGE_ZH)
        self.assertEqual(self.resolve('{"language": "zh-TW"}', language="en"),
                         memspec.LANGUAGE_EN)

    def test_an_unsupported_value_falls_back_instead_of_failing(self):
        self.assertEqual(self.resolve('{"language": "fr"}'), memspec.LANGUAGE_ZH)
        self.assertEqual(self.resolve('{"language": "en"}', language="fr"),
                         memspec.LANGUAGE_ZH)
        self.assertEqual(self.resolve('{"language": 7}'), memspec.LANGUAGE_ZH)

    def test_a_broken_config_falls_back_silently(self):
        # 設定檔壞掉的機器上，掛鉤要照跑：顯示錯語言遠比整支掛掉便宜。
        self.assertEqual(self.resolve("{not json at all"), memspec.LANGUAGE_ZH)
        self.assertEqual(self.resolve('["not", "an", "object"]'), memspec.LANGUAGE_ZH)

    def test_the_module_binds_the_language_once_at_import(self):
        # 唯一一題子行程：解析對了但沒綁進模組，上面那些全是空的。
        self.config.write_text('{"language": "en"}', encoding="utf-8")
        environment = dict(os.environ)
        environment.pop(memspec.EPITYPE_LANG_ENV, None)
        environment[memspec.EPITYPE_CONFIG_ENV] = str(self.config)
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); " % str(ROOT)
             + "from epitype import memspec; "
             + "print(memspec.LANGUAGE, memspec.DECISION_PREFIX.strip())"],
            capture_output=True, text=True, encoding="utf-8",
            env=environment, cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(memspec.LANGUAGE_EN), result.stdout)
        self.assertIsNone(CJK.search(result.stdout), result.stdout)


class _HookCase(unittest.TestCase):
    """臨時家目錄 + 臨時庫；真機的設定檔與閘門紀錄一個位元組都不會被碰到。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-lang-hook-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.temp = self.root / "temp"
        self.temp.mkdir()
        self.config = self.root / "config.json"
        self.write_config()

    def write_config(self, language=None):
        value = {memspec.CONFIG_VAULTS_FIELD: [str(self.vault)],
                 memspec.CONFIG_BUDGET_BYTES_FIELD: memspec.HOOK_DEFAULT_BUDGET_BYTES}
        if language is not None:
            value[memspec.CONFIG_LANGUAGE_FIELD] = language
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def environment(self, language=None, config=None):
        value = dict(os.environ)
        value.pop(memspec.EPITYPE_LANG_ENV, None)
        value.update({
            memspec.EPITYPE_CONFIG_ENV: str(config or self.config),
            memspec.DREAM_MODE_ENV: memspec.DREAM_MODE_OFF,
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "TMPDIR": str(self.temp), "TEMP": str(self.temp), "TMP": str(self.temp),
        })
        if language is not None:
            value[memspec.EPITYPE_LANG_ENV] = language
        return value

    def install(self, card_name):
        source = _card(card_name)
        (self.vault / source.name).write_bytes(source.read_bytes())

    def run_hook(self, script, event, environment):
        result = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(event, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8",
            env=environment, cwd=str(self.root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), f"{script.name} 沒有擋下來：{result.stderr}")
        return json.loads(result.stdout)

    def guard_reason(self, language=None, config_language=None):
        self.install(GUARD_CARD)
        if config_language is not None:
            self.write_config(config_language)
        command = _sequence(_card(GUARD_CARD), memspec.EXAMPLE_BLOCKS_FIELD)[0]
        value = self.run_hook(PRETOOLUSE, {
            "tool_name": "Bash", "tool_input": {"command": command},
            "session_id": "lang-guard", "cwd": str(self.root),
        }, self.environment(language))
        return value["hookSpecificOutput"]["permissionDecisionReason"]

    def stop_reason(self, language=None):
        self.install(SPEECH_CARD)
        sentence = _sequence(_card(SPEECH_CARD), memspec.EXAMPLE_BLOCKS_FIELD)[0]
        value = self.run_hook(STOP, {
            "hook_event_name": "Stop", "stop_hook_active": False,
            "last_assistant_message": sentence,
            "session_id": "lang-stop", "cwd": str(self.root),
        }, self.environment(language))
        self.assertEqual(value["decision"], "block")
        return value["reason"]


class EnglishHooksSayNothingInChinese(_HookCase):
    """真的掛鉤行程，英文設定，擋下來的那一句不含任何中日韓字。"""

    def test_a_starter_guard_block_is_english(self):
        # 語言從設定檔來：陌生人不會設環境變數，他拿到的是安裝器寫的那一行。
        reason = self.guard_reason(config_language=memspec.LANGUAGE_EN)
        self.assertIn("Bash", reason)
        self.assertIsNone(CJK.search(reason), reason)

    def test_a_starter_stop_block_is_english(self):
        reason = self.stop_reason(language=memspec.LANGUAGE_EN)
        self.assertIsNone(CJK.search(reason), reason)


class DefaultLanguageOutputIsUnchanged(_HookCase):
    """不設語言＝owner 現在看到的那一句，逐字。"""

    def test_the_guard_block_is_byte_identical(self):
        self.assertEqual(self.guard_reason(), ZH_GUARD_REASON)

    def test_the_stop_block_is_byte_identical(self):
        self.assertEqual(self.stop_reason(), ZH_STOP_REASON)

    def test_a_broken_config_does_not_stop_the_hook(self):
        # 設定檔壞掉時掛鉤讀不到 vault，本來就不會擋；這裡要的是「不會炸」。
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PRETOOLUSE)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"},
                              "session_id": "lang-broken", "cwd": str(self.root)}),
            capture_output=True, text=True, encoding="utf-8",
            env=self.environment(config=broken), cwd=str(self.root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class InstallerWritesTheFieldOnlyOnce(unittest.TestCase):
    """全新安裝寫一行；既有設定檔一個位元組都不碰。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-lang-install-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()

    def config_value(self, path):
        return json.loads(graft._config_bytes(path, [self.vault], ROOT).decode("utf-8-sig"))

    def test_a_fresh_install_writes_a_supported_language(self):
        value = self.config_value(self.root / "missing.json")
        self.assertIn(value[graft.CONFIG_LANGUAGE_FIELD], memspec.SUPPORTED_LANGUAGES)

    def test_an_existing_config_without_the_field_is_left_alone(self):
        # 沒有欄位＝這台機器現在就是繁中；補寫進去等於替使用者改行為。
        path = self.root / "config.json"
        path.write_text(json.dumps({"vaults": [], "budget_bytes": 10240}), encoding="utf-8")
        self.assertNotIn(graft.CONFIG_LANGUAGE_FIELD, self.config_value(path))

    def test_an_existing_language_is_preserved(self):
        path = self.root / "config.json"
        path.write_text(
            json.dumps({"vaults": [], "budget_bytes": 10240,
                        graft.CONFIG_LANGUAGE_FIELD: memspec.LANGUAGE_EN}),
            encoding="utf-8")
        self.assertEqual(self.config_value(path)[graft.CONFIG_LANGUAGE_FIELD],
                         memspec.LANGUAGE_EN)

    def _doctor_output(self, config_value):
        home = self.root / "home"
        (home / graft.CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)
        (home / graft.CONFIG_DIRECTORY / graft.CONFIG_FILENAME).write_text(
            json.dumps(config_value), encoding="utf-8")
        output = io.StringIO()
        # 這台機器沒有裝好，doctor 後面一定失敗；語言那一行在讀完設定檔就印，所以
        # 「出了事還查得到現在用哪一種語言」這件事本身就是要驗的。
        graft._doctor(home, output=output)
        return output.getvalue()

    def test_the_doctor_prints_the_configured_language(self):
        text = self._doctor_output({"vaults": [str(self.vault)],
                                    graft.CONFIG_LANGUAGE_FIELD: memspec.LANGUAGE_EN})
        self.assertIn(f"LANGUAGE: {memspec.LANGUAGE_EN}", text)

    def test_the_doctor_names_the_default_when_the_field_is_absent(self):
        text = self._doctor_output({"vaults": [str(self.vault)]})
        self.assertIn(f"LANGUAGE: {memspec.LANGUAGE_ZH} (default", text)


def _selftest():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"SELFTEST {'PASS' if not failed else 'FAIL'} {total - failed}/{total}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
