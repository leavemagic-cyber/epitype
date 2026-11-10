import sys;sys.dont_write_bytecode=True
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'adapters/claude')]
from epitype import hook_context, memspec
import _hook_common as common
import precompact_hook as before
import sessionstart_hook as after

class CompactReceiptRecovery(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='epitype-receipt-recovery-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.vault=self.root/'memory';self.vault.mkdir()
        config=self.root/'config.json';common.write_config(config,[self.vault],budget=5000)
        for instrument in (patch.dict(os.environ,{memspec.EPITYPE_CONFIG_ENV:str(config),
            memspec.DREAM_MODE_ENV:memspec.DREAM_MODE_OFF,'HOME':str(self.root/'home'),'USERPROFILE':str(self.root/'home')}),
            patch.object(common,'recall_marker_directory',lambda session:self.root/'markers'/session)):
            instrument.start();self.addCleanup(instrument.stop)
    def markers(self,event):
        directory=common.recall_marker_directory(hook_context.identity(event));directory.mkdir(parents=True,exist_ok=True)
        for name in ('digest',memspec.CONTEXT_METER_MARKER):
            (directory/name).write_text('previous context delivery',encoding='utf-8')
        return directory
    def test_compact_sessionstart_resets_even_when_precompact_config_or_transcript_failed(self):
        for transcript in ('.codex/rollout-missing.jsonl','.claude/missing.jsonl'):
            with self.subTest(host_shape=transcript):
                event={'source':'compact','session_id':'context','transcript_path':str(self.root/transcript)}
                directory=self.markers(event)
                trace={}
                with patch.object(after,'load_config',return_value=None):
                    self.assertIsNone(after._handle(event,time.monotonic(),trace=trace))
                self.assertFalse(directory.exists())
                self.assertTrue(trace['receipt_reset'])
    def test_startup_and_explicit_child_compact_preserve_main_receipts(self):
        for extra in ({'source':'startup'},{'source':'compact','agent_id':'child'}):
            event={'session_id':'context','transcript_path':str(self.root/'trace.jsonl')}
            directory=self.markers(event)
            with patch.object(after,'load_config',return_value=None):
                after._handle({**event,**extra},time.monotonic())
            self.assertTrue((directory/'digest').is_file())
            self.assertTrue((directory/memspec.CONTEXT_METER_MARKER).is_file())
    def test_real_precompact_child_keeps_main_then_main_clears_own_receipts(self):
        transcript=self.root/'trace.jsonl'
        transcript.write_text(json.dumps({'type':'user','sessionId':'context',
            'message':{'role':'user','content':'Synthetic fixture only'}})+'\n',encoding='utf-8')
        event={'session_id':'context','transcript_path':str(transcript),'cwd':str(self.vault)}
        directory=self.markers(event)
        child_transcript=self.root/'subagents'/'agent-child.jsonl'
        child_transcript.parent.mkdir()
        child_transcript.write_text(transcript.read_text(encoding='utf-8'),encoding='utf-8')
        child={**event,'agent_id':'child','transcript_path':str(child_transcript)}
        child_directory=self.markers(child)
        trace={};before._handle(child,time.monotonic(),learn=False,trace=trace)
        self.assertTrue(trace['cleared'])
        self.assertFalse(child_directory.exists())
        self.assertTrue((directory/'digest').is_file())
        self.assertTrue((directory/memspec.CONTEXT_METER_MARKER).is_file())
        trace={};before._handle(event,time.monotonic(),learn=False,trace=trace)
        self.assertTrue(trace['cleared'])
        self.assertFalse(directory.exists())

    def test_unconfirmed_child_transcript_preserves_both_contexts_receipts(self):
        transcript=self.root/'trace.jsonl'
        transcript.write_text('{}\n',encoding='utf-8')
        event={'session_id':'context','transcript_path':str(transcript),'cwd':str(self.vault)}
        directory=self.markers(event)
        child={**event,'agent_id':'child'}
        child_directory=self.markers(child)
        trace={};before._handle(child,time.monotonic(),learn=False,trace=trace)
        self.assertEqual(trace['outcome'],'subagent-transcript-unconfirmed')
        for markers in (directory,child_directory):
            self.assertTrue((markers/'digest').is_file())
            self.assertTrue((markers/memspec.CONTEXT_METER_MARKER).is_file())
        self.assertFalse(before._map_destination(self.vault,child,transcript).exists())

if __name__=='__main__':unittest.main(argv=[arg for arg in sys.argv if arg != "--selftest"])
