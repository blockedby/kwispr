"""The application meeting pipeline must retain speaker/timing and paid-request state."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import wave

from kwispr_meetings import config, openrouter as cloud, pipeline, sessions


class MeetingOpenRouterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'KWISPR_MEETING_PROVIDER':'openrouter', 'KWISPR_MEETING_OPENROUTER_KEY':'fixture-key',
                       'KWISPR_MEETING_CLOUD_MODEL':'google/gemini-test','KWISPR_MEETING_CLOUD_BUDGET_USD':'1.5'}
        self.intervals = [{'start':0,'end':4,'speakers':['speaker_01']},
                          {'start':4,'end':8,'speakers':['speaker_02']}]
        self.audio = [0] * (90*16000)

    def reply(self, raw, turns, config):
        return {'text':json.dumps({'turns':[{'id':turn['id'],'text':'Реплика '+turn['id']} for turn in turns]}),
                'finish_reason':'stop','usage':{'cost':.01},'generation_id':'fixture'}

    def transcribe(self, token='one', config=None):
        with patch.object(pipeline,'_wav_slice',return_value=b'whole context window'):
            return cloud.transcribe_track(self.root,self.audio,self.intervals,config or self.config,
                                          'remote',2.5,token)

    def test_explicit_provider_does_not_inherit_dictation_cloud_opt_in(self):
        inherited={'KWISPR_BACKEND':'openrouter-chat','KWISPR_API_URL':cloud.ENDPOINT,
                   'KWISPR_API_KEY':'dictation-key'}
        with self.assertRaises(config.ConfigError): config.validate_meeting_backend(inherited)
        config.validate_meeting_backend({**inherited,**self.config})
        snapshot=sessions.transcription_options(self.config)
        self.assertEqual(snapshot['KWISPR_MEETING_PROVIDER'],'openrouter')
        self.assertNotIn('KWISPR_MEETING_OPENROUTER_KEY',snapshot)
        self.assertNotIn('fixture-key',json.dumps(snapshot))

    def test_key_file_is_literal_and_cost_settings_are_checked(self):
        key=self.root/'key'
        key.write_text('OPENROUTER_API_KEY=fixture-file-secret\nMODELS:\ngoogle/model\n')
        values={**self.config,'KWISPR_MEETING_OPENROUTER_KEY':'','KWISPR_MEETING_OPENROUTER_KEY_FILE':str(key)}
        self.assertEqual(config.openrouter_key(values),'fixture-file-secret')
        for budget in ('nan','inf','0','-1','invalid'):
            with self.subTest(budget=budget),self.assertRaises(config.ConfigError):
                config.validate_meeting_backend({**self.config,'KWISPR_MEETING_CLOUD_BUDGET_USD':budget})

    def test_context_audio_is_decoded_once_and_keeps_stable_speakers_offsets_and_cache(self):
        with patch.object(cloud,'_request',side_effect=self.reply) as request:
            rows,info=self.transcribe()
            rows2,_=self.transcribe('two')
        self.assertEqual(request.call_count,1)
        self.assertEqual(request.call_args.args[0],b'whole context window')
        self.assertEqual([r['speakers'] for r in rows],[['speaker_01'],['speaker_02']])
        self.assertEqual([(r['start'],r['end']) for r in rows],[(2.5,6.5),(6.5,10.5)])
        self.assertEqual(rows,rows2)
        self.assertEqual(info['backend'],'openrouter')
        receipt=next((self.root/'openrouter').glob('*.json'))
        self.assertEqual(receipt.stat().st_mode & 0o777,0o600)
        self.assertNotIn('fixture-key',receipt.read_text())

    def test_unknown_paid_request_is_never_blindly_repeated(self):
        with patch.object(cloud,'_request',side_effect=TimeoutError('private response')) as request:
            with self.assertRaisesRegex(RuntimeError,'unknown'): self.transcribe()
            with self.assertRaisesRegex(RuntimeError,'unknown'): self.transcribe('two')
        self.assertEqual(request.call_count,1)
        self.assertEqual(len(list((self.root/'openrouter').glob('*.attempt'))),1)

    def test_known_incomplete_response_requires_explicit_new_run_and_accounts_for_both(self):
        bad={'text':'cut','finish_reason':'length','usage':{'cost':.02},'generation_id':'bad'}
        with patch.object(cloud,'_request',return_value=bad) as request:
            with self.assertRaisesRegex(RuntimeError,'incomplete'): self.transcribe()
            with self.assertRaisesRegex(RuntimeError,'incomplete'): self.transcribe()
            self.assertEqual(request.call_count,1)
        with patch.object(cloud,'_request',side_effect=self.reply) as request:
            rows,_=self.transcribe('user-retry')
        self.assertEqual(len(rows),2)
        self.assertEqual(request.call_count,1)
        self.assertAlmostEqual(cloud.spent(self.root/'openrouter'),.03)

    def test_missing_or_reordered_turns_fail_without_publishing_wrong_speakers(self):
        def invalid(raw,turns,config):
            result=self.reply(raw,turns,config)
            values=json.loads(result['text'])
            values['turns'].reverse()
            result['text']=json.dumps(values)
            return result
        with patch.object(cloud,'_request',side_effect=invalid):
            with self.assertRaisesRegex(RuntimeError,'alignment'): self.transcribe()

    def test_budget_stops_before_new_paid_requests(self):
        with patch.object(cloud,'_request',side_effect=self.reply) as request:
            with self.assertRaisesRegex(RuntimeError,'budget'):
                self.transcribe(config={**self.config,'KWISPR_MEETING_CLOUD_BUDGET_USD':'.01'})
        request.assert_not_called()

    def test_existing_successful_results_can_publish_even_after_budget_is_lowered(self):
        with patch.object(cloud,'_request',side_effect=self.reply) as request:
            self.transcribe()
            rows,_=self.transcribe('two',{**self.config,'KWISPR_MEETING_CLOUD_BUDGET_USD':'.001'})
        self.assertEqual(request.call_count,1)
        self.assertEqual(len(rows),2)

    def test_explicit_retry_reserves_budget_for_known_failed_paid_response(self):
        bad={'text':'cut','finish_reason':'content_filter','usage':{'cost':.02},'generation_id':'bad'}
        with patch.object(cloud,'_request',return_value=bad):
            with self.assertRaises(RuntimeError): self.transcribe()
        with patch.object(cloud,'_request',side_effect=self.reply) as request:
            with self.assertRaisesRegex(RuntimeError,'budget'):
                self.transcribe('two',{**self.config,'KWISPR_MEETING_CLOUD_BUDGET_USD':'.03'})
        request.assert_not_called()

    def test_real_request_payload_uses_audio_and_schema_without_exposing_key_in_cache(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,*args):
                return json.dumps({'id':'mock-id','choices':[{'finish_reason':'stop','message':{'content':'{"turns":[]}'}}],
                                   'usage':{'cost':.001}}).encode()
        class Opener:
            def open(inner,request,**kwargs):
                self.assertEqual(request.full_url,cloud.ENDPOINT)
                body=json.loads(request.data)
                self.assertEqual(body['response_format']['type'],'json_schema')
                self.assertEqual(body['messages'][0]['content'][1]['type'],'input_audio')
                self.assertNotIn('fixture-key',request.data.decode())
                self.assertEqual(request.get_header('Authorization'),'Bearer fixture-key')
                return Response()
        with patch.object(cloud.urllib.request,'build_opener',return_value=Opener()):
            result=cloud._request(b'wav',[],self.config)
        self.assertEqual(result['finish_reason'],'stop')

    def test_pipeline_routes_cloud_without_local_health_or_asr_and_exports_speaker_timed_rows(self):
        import sys
        if 'numpy' not in sys.modules:
            try: import numpy
            except ImportError: self.skipTest('numpy required for pipeline fixture')
        import numpy as np
        (self.root/'remote.wav').write_bytes(b'fixture')
        pipeline.atomic_json(self.root/'session.json',{'token':'one','source_kind':'media','microphone_stream':None,'speakers':0})
        segments=[{'start':0,'end':4,'speaker':'speaker_01'},{'start':4.1,'end':8,'speaker':'speaker_02'}]
        with patch.object(pipeline,'require_ready'),patch.object(pipeline,'_load_track',return_value=np.zeros(16000*10)), \
             patch.object(pipeline,'_diarize',return_value=segments), \
             patch.object(pipeline,'_request_transcription') as local, \
             patch.object(pipeline,'_prepare_language_policy') as health,patch.object(cloud,'_request',side_effect=self.reply):
            result=pipeline.process_session(self.root,self.config)
        local.assert_not_called();health.assert_not_called()
        document=json.loads(Path(result['transcript_json']).read_text())
        self.assertEqual(set(document['speakers']),{'speaker_01','speaker_02'})
        self.assertEqual(document['language_policy']['backend'],'openrouter')
        self.assertTrue(all(row['text'] for row in document['utterances']))
