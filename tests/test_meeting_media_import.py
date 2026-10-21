"""Saved recordings use meeting diarization, durable queuing and safe extraction."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from kwispr_meetings import media, sessions


class MeetingMediaImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'OBS recording.mkv'
        self.source.write_bytes(b'original video')
        self.config = {'KWISPR_API_URL': 'http://127.0.0.1:19650/v1/audio/transcriptions',
                       'KWISPR_VOCABULARY': 'Kwispr', 'KWISPR_MEETING_REMOTE_LANGUAGE': 'ru'}
        self.env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state')}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for target in ('ready', 'advance_queue_locked'):
            context = patch.object(sessions, target)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(sessions.shutil, 'which', return_value='/usr/bin/ffmpeg')
        context.start()
        self.addCleanup(context.stop)

    def enqueue(self, **options):
        return sessions.import_recording(self.source, self.config, destination=str(self.root/'saved'), **options)

    def test_mixed_import_queues_once_with_own_options_and_keeps_original(self):
        result = self.enqueue(speakers=3, audio_stream=1, language='en')
        directory = Path(result['session_dir'])
        manifest = sessions.read_json(directory/'session.json')
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(result['title'], 'OBS recording')
        self.assertEqual(manifest['audio_stream'], 1)
        self.assertEqual(manifest['speakers'], 3)
        self.assertEqual(manifest['transcription_options']['KWISPR_MEETING_REMOTE_LANGUAGE'], 'en')
        self.assertEqual(self.config['KWISPR_MEETING_REMOTE_LANGUAGE'], 'ru')
        self.assertEqual(media.session_tracks(manifest), ('remote',))
        self.assertEqual(len(sessions.read_json(sessions.state_dir()/'queue.json')['jobs']), 1)
        self.assertEqual(self.source.read_bytes(), b'original video')
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory/'session.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('token', result)

    def test_import_keeps_live_recording_pointer_and_stop_target(self):
        live = self.root/'live'
        live.mkdir()
        current = {'session_dir': str(live), 'token': 'live', 'worker_pid': os.getpid(),
                   'worker_identity': sessions.process_identity(os.getpid())}
        sessions.atomic_json(live/'session.json', {'version': 1, 'session_dir': str(live),
                              'token': 'live', 'state': 'recording'})
        sessions.atomic_json(sessions.state_dir()/'current.json', current)
        result = self.enqueue(microphone_stream=1)
        self.assertEqual(sessions.read_json(sessions.state_dir()/'current.json'), current)
        self.assertEqual(result['recording']['session_dir'], str(live))
        self.assertEqual(media.session_tracks(result), ('remote', 'microphone'))
        self.assertEqual(sessions.stop()['session_dir'], str(live))

    def test_invalid_source_tracks_and_language_do_not_create_jobs(self):
        for options in ({'audio_stream': -1}, {'microphone_stream': 0}, {'audio_stream': 64},
                        {'speakers': 17}, {'language': 'not a language'}):
            with self.subTest(options=options), self.assertRaises((sessions.SessionError, ValueError)):
                self.enqueue(**options)
        self.assertFalse((self.root/'saved').exists())

    def test_mixed_retry_uses_extracted_audio_after_source_is_removed(self):
        result = self.enqueue()
        directory = Path(result['session_dir'])
        sessions.atomic_json(sessions.state_dir()/'queue.json', {'jobs': []})
        manifest = sessions.read_json(directory/'session.json')
        manifest['state'] = 'failed'
        sessions.atomic_json(directory/'session.json', manifest)
        (directory/'remote.wav').write_bytes(b'pcm'*50)
        self.source.unlink()
        result = sessions.retry(directory, self.config, speakers=2)
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(result['speakers'], 2)
        self.assertFalse((directory/'microphone.wav').exists())


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required for real media extraction')
class MeetingMediaExtractionTest(unittest.TestCase):
    def test_real_multitrack_video_extracts_selected_tracks_and_resumes_without_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'OBS video.mkv'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-f', 'lavfi', '-i', 'aevalsrc=0.25:s=16000:d=0.2',
                '-f', 'lavfi', '-i', 'aevalsrc=0.5:s=16000:d=0.2',
                '-f', 'lavfi', '-i', 'color=size=16x16:duration=0.2',
                '-map', '2:v', '-map', '0:a', '-map', '1:a', '-c:v', 'ffv1', '-c:a', 'pcm_s16le', str(source)],
                check=True, capture_output=True, timeout=20)
            before = source.read_bytes()
            output = root/'meeting'
            output.mkdir()
            session = {'source_kind': 'media', 'source_path': str(source), 'audio_stream': 1, 'microphone_stream': 0}
            media.prepare_recording(output, session)
            for track, expected in [('remote', 16384), ('microphone', 8192)]:
                with wave.open(str(output/(track+'.wav')), 'rb') as reader:
                    self.assertEqual((reader.getnchannels(), reader.getsampwidth(), reader.getframerate()), (1,2,16000))
                    samples = struct.unpack('<100h', reader.readframes(100))
                    self.assertTrue(all(abs(value-expected) <= 1 for value in samples))
            self.assertEqual(source.read_bytes(), before)
            with self.assertRaisesRegex(RuntimeError, 'no audio track'):
                media.prepare_recording(root, {**session, 'audio_stream': 50, 'microphone_stream': None})
            self.assertFalse((root/'remote.wav').exists())
            source.unlink()
            media.prepare_recording(output, session)  # Already-extracted tracks are durable.
