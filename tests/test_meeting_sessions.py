from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from kwispr_meetings import sessions

ROOT = Path(__file__).resolve().parents[1]

FAKE_PACTL = '''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[-1] == 'sources':
    print(json.dumps([{'name':'test-mic','description':'Fake mic','monitor_of_sink':None},
                     {'name':'test.monitor','description':'Fake remote','monitor_of_sink':1}]))
elif args[-1] == 'sinks':
    print(json.dumps([{'name':'test','monitor_source':'test.monitor'}]))
elif args[-1] == 'get-default-source': print('test-mic')
else: print('test')
'''
FAKE_FFMPEG = '''#!/usr/bin/env python3
import os, sys, wave
from pathlib import Path
if os.environ.get('FAIL_CAPTURE'): raise SystemExit(3)
Path(os.environ['FAKE_CAPTURE_PID']).write_text(str(os.getpid()))
outputs = [Path(arg) for arg in sys.argv if arg.endswith('.wav')]
for path in outputs:
    with wave.open(str(path), 'wb') as out:
        out.setparams((1,2,16000,0,'NONE','not compressed'))
        out.writeframes(b'\\x01\\x00'*16000)
sys.stdin.readline()
'''
FAKE_PIPELINE = '''from pathlib import Path
import os, json, time, fcntl
def require_ready(config):
    if os.environ.get('FAIL_READY'): raise RuntimeError('Install the meeting models first.')
def process_session(directory, config, progress_callback=None):
    if progress_callback: progress_callback('Transcribing test audio')
    if os.environ.get('QUEUE_TEST_DIR'):
        root = Path(os.environ['QUEUE_TEST_DIR'])
        title = json.loads((Path(directory)/'session.json').read_text())['title']
        # A real OS lock makes accidental parallel pipeline execution observable.
        guard = (root/'processor.lock').open('w')
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (root/'events').open('a') as log: log.write(title+'\\n')
        deadline = time.monotonic()+20
        while not (root/('release-'+title)).exists() and not (root/'release-all').exists():
            if time.monotonic() > deadline: raise RuntimeError('Queue test timed out')
            time.sleep(.02)
        if title == os.environ.get('QUEUE_FAIL_TITLE'): raise RuntimeError('Deliberate queued failure')
    if os.environ.get('FAIL_PIPELINE'): raise RuntimeError('Deliberate processing failure')
    path = Path(directory) / 'transcript.md'
    path.write_text('00:00 Me: test\\n00:01 Speaker 1: response\\n')
    return {'transcript_path':str(path),'speaker_count':1}
'''


class MeetingSessionUnitTests(unittest.TestCase):
    def test_retry_journal_recovers_before_manifest_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root/'meeting'
            directory.mkdir()
            before = {'version':1, 'token':'old', 'state':'complete', 'session_dir':str(directory)}
            queued = {**before, 'token':'new', 'state':'queued'}
            sessions.atomic_json(directory/'session.json',before)
            sessions.atomic_json(root/'current.json',{'session_dir':str(directory),'token':'old'})
            job = {'session_dir':str(directory),'token':'new','previous_token':'old','session':queued}
            sessions.atomic_json(root/'queue.json',{'jobs':[job]})
            with patch.object(sessions, 'launch_locked') as launch:
                sessions.advance_queue_locked(root)
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[2]['token'],'new')
            self.assertEqual(sessions.read_json(directory/'session.json')['state'],'processing')
            self.assertEqual(sessions.queue_locked(root),[])

    def test_live_owner_deduplicates_journal_after_launch_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root/'meeting'
            directory.mkdir()
            pointer = {'session_dir':str(directory),'token':'job','worker_pid':os.getpid(),
                       'worker_identity':sessions.process_identity(os.getpid())}
            sessions.atomic_json(directory/'session.json',{'token':'job','state':'processing'})
            sessions.atomic_json(root/'processing.json',pointer)
            sessions.atomic_json(root/'queue.json',{'jobs':[{'session_dir':str(directory),'token':'job'}]})
            with patch.object(sessions,'launch_locked') as launch:
                sessions.advance_queue_locked(root)
                launch.assert_not_called()
            self.assertEqual(sessions.queue_locked(root),[])

    def test_retry_does_not_replace_legacy_live_processor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = root/'active';active.mkdir()
            target = root/'target';target.mkdir()
            sessions.atomic_json(active/'session.json',{'version':1,'token':'old','state':'processing','session_dir':str(active)})
            pointer = {'session_dir':str(active),'token':'old','worker_pid':os.getpid(),
                       'worker_identity':sessions.process_identity(os.getpid())}
            sessions.atomic_json(root/'current.json',pointer)
            sessions.atomic_json(target/'session.json',{'version':1,'token':'saved','state':'complete'})
            with patch.object(sessions,'state_dir',return_value=root), patch.object(sessions,'ready'):
                with self.assertRaisesRegex(sessions.SessionError,'older version'):
                    sessions.retry(target,{})
            self.assertEqual(sessions.read_json(root/'current.json'),pointer)
            self.assertEqual(sessions.queue_locked(root),[])

    def test_pid_identity_stale_pid_is_not_a_live_worker(self):
        self.assertTrue(sessions.owner_alive({'worker_pid':os.getpid(), 'worker_identity':sessions.process_identity(os.getpid())}))
        self.assertFalse(sessions.owner_alive({'worker_pid':os.getpid(), 'worker_identity':'wrong-start-ticks'}))

    def test_capture_command_keeps_common_timeline_and_separate_outputs(self):
        command = sessions.ffmpeg_command({'mic_source':'mic', 'monitor_source':'monitor'}, Path('/private/meeting'))
        self.assertIn('-isync', command)
        self.assertIn('-copyts', command)
        self.assertEqual(command.count('aresample=16000:async=1:first_pts=0'), 2)
        self.assertIn('/private/meeting/microphone.wav', command)
        self.assertIn('/private/meeting/remote.wav', command)
        self.assertNotIn('default', command)

    def test_stop_timeout_terminates_only_owned_child(self):
        from unittest.mock import Mock
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('ffmpeg', 8), 0]
        with self.assertRaises(sessions.SessionError):
            sessions.finish_capture(child)
        child.terminate.assert_called_once()
        child.kill.assert_not_called()


class DetachedMeetingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        shutil.copy2(ROOT / 'kwispr-meetings.py', self.app)
        shutil.copytree(ROOT / 'kwispr_meetings', self.app / 'kwispr_meetings', ignore=shutil.ignore_patterns('__pycache__'))
        (self.app / 'kwispr_meetings/pipeline.py').write_text(FAKE_PIPELINE)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name, source in [('pactl',FAKE_PACTL),('ffmpeg',FAKE_FFMPEG)]:
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o700)
        config = self.root / 'config.env'
        config.write_text('KWISPR_API_URL=http://127.0.0.1:19650/v1/audio/transcriptions\n')
        self.env = {key:value for key,value in os.environ.items() if not key.startswith('KWISPR_')}
        self.env.update(PATH=f'{self.bin}:{os.environ["PATH"]}', XDG_STATE_HOME=str(self.root / 'state'),
                        KWISPR_CONFIG_FILE=str(config), KWISPR_MEETING_RUNTIME_DIR=str(self.root / 'runtime'),
                        KWISPR_MEETING_OUTPUT_DIR=str(self.root / 'meetings'), FAKE_CAPTURE_PID=str(self.root / 'capture.pid'))
        self.entry = [sys.executable, str(self.app / 'kwispr-meetings.py')]

    def tearDown(self):
        try:
            if (self.root/'queue-probe').exists():
                (self.root/'queue-probe/release-all').touch()
            self.run_cli('stop')
            self.wait_state({'complete','failed','idle'}, timeout=5)
        finally:
            self.temp.cleanup()

    def run_cli(self, *args, ok=True):
        response = subprocess.run(self.entry + list(args), env=self.env, text=True, capture_output=True, timeout=25)
        self.assertEqual(response.returncode, 0 if ok else 1, response.stderr + response.stdout)
        return json.loads(response.stdout if ok else response.stderr)

    def wait_state(self, targets, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.run_cli('status','--json')
            if result['state'] in targets:
                return result
            time.sleep(.05)
        self.fail(f'Never reached {targets}; last={result}')

    def test_detached_record_stop_transcript_and_private_files(self):
        devices = self.run_cli('sources','--json')
        self.assertEqual(devices['default_microphone'], 'test-mic')
        state = self.run_cli('start','--title','../../title is metadata','--speakers','2')
        self.assertEqual(state['state'], 'recording')
        directory = Path(state['session_dir'])
        self.assertEqual(directory.parent, self.root / 'meetings')
        self.assertEqual(state['title'], '../../title is metadata')
        self.assertEqual(self.run_cli('stop')['state'],'stopping')
        final = self.wait_state({'complete'})
        self.assertTrue(Path(final['transcript_path']).is_file())
        self.assertEqual(directory.stat().st_mode & 0o777,0o700)
        for name in ['session.json','microphone.wav','remote.wav','transcript.md','worker.log']:
            self.assertEqual((directory/name).stat().st_mode & 0o777,0o600,name)
        self.assertNotIn('token',final)

    def test_concurrent_start_only_one_wins(self):
        commands = [subprocess.Popen(self.entry+['start'],env=self.env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        results = [(process.communicate(timeout=25),process.returncode) for process in commands]
        self.assertEqual(sorted(code for _,code in results),[0,1])
        self.assertEqual(len(list((self.root/'meetings').iterdir())),1)
        self.assertIn('already recording',next(data[1] for data,code in results if code))

    def test_processing_failure_retains_audio_and_retry_works(self):
        self.env['FAIL_PIPELINE']='1'
        state = self.run_cli('start', '--speakers', '2')
        self.run_cli('stop')
        failed = self.wait_state({'failed'})
        self.assertIn('Deliberate processing failure',failed['message'])
        directory = Path(state['session_dir'])
        before = {name:(directory/name).read_bytes() for name in ['microphone.wav','remote.wav']}
        saved_session = (directory/'session.json').read_bytes()
        for count in ('-1', '17'):
            self.assertIn('Speaker count', self.run_cli('process', str(directory), '--speakers', count, ok=False)['message'])
            self.assertEqual((directory/'session.json').read_bytes(), saved_session)
            self.assertEqual(before, {name:(directory/name).read_bytes() for name in before})
        del self.env['FAIL_PIPELINE']
        self.assertEqual(self.run_cli('process',str(directory))['speakers'], 2)
        self.wait_state({'complete'})
        self.assertEqual(self.run_cli('process',str(directory),'--speakers','3')['speakers'], 3)
        self.assertEqual(self.wait_state({'complete'})['speakers'], 3)
        self.assertEqual(before,{name:(directory/name).read_bytes() for name in before})

    def test_start_failure_and_missing_setup_never_capture(self):
        self.env['FAIL_READY']='1'
        result = self.run_cli('start',ok=False)
        self.assertIn('Install the meeting models',result['message'])
        self.assertFalse((self.root/'capture.pid').exists())
        del self.env['FAIL_READY']
        self.env['FAIL_CAPTURE']='1'
        result = self.run_cli('start',ok=False)
        self.assertIn('exited unexpectedly',result['message'])
        self.assertEqual(self.run_cli('status')['state'],'failed')

    def test_existing_output_directory_permissions_are_preserved(self):
        output = self.root / 'shared-output'
        output.mkdir(mode=0o755)
        self.run_cli('start', '--output-dir', str(output))
        self.assertEqual(output.stat().st_mode & 0o777, 0o755)

    def test_invalid_devices_do_not_start_recording(self):
        result = self.run_cli('start','--mic','missing-device',ok=False)
        self.assertIn('available microphone',result['message'])
        self.assertFalse((self.root/'capture.pid').exists())

    def test_dead_supervisor_fails_state_and_stops_capture_child(self):
        self.run_cli('start')
        pointer = json.loads((self.root/'state/kwispr/meetings/current.json').read_text())
        capture_pid = int((self.root/'capture.pid').read_text())
        os.kill(pointer['worker_pid'],signal.SIGKILL)
        state = self.wait_state({'failed'})
        self.assertIn('stopped unexpectedly',state['message'])
        deadline = time.monotonic()+3
        while sessions.process_identity(capture_pid) and time.monotonic()<deadline:
            time.sleep(.05)
        self.assertIsNone(sessions.process_identity(capture_pid),'capture survived dead supervisor')

    def enable_queue_probe(self):
        probe = self.root/'queue-probe'
        probe.mkdir()
        self.env['QUEUE_TEST_DIR'] = str(probe)
        return probe

    def wait_processing(self, title, timeout=5):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            result = self.run_cli('status')
            if (result.get('transcription') or {}).get('title') == title:
                return result
            time.sleep(.03)
        self.fail(f'Never processed {title}: {result}')

    def test_capture_during_processing_and_fifo_queue(self):
        probe = self.enable_queue_probe()
        a = self.run_cli('start','--title','A')
        self.run_cli('stop')
        self.wait_processing('A')
        b = self.run_cli('start','--title','B')
        self.assertEqual(b['state'], 'recording')
        self.assertEqual(b['recording']['title'], 'B')
        self.assertEqual(b['transcription']['title'], 'A')
        self.assertIn('already recording',self.run_cli('start',ok=False)['message'])
        self.assertIn('already queued or transcribing',self.run_cli('process',a['session_dir'],ok=False)['message'])
        self.assertIn('Stop recording',self.run_cli('process',b['session_dir'],ok=False)['message'])
        self.run_cli('stop')
        queued = self.wait_state({'queued'})
        self.assertEqual([x['title'] for x in queued['queue']], ['B'])
        self.assertEqual(queued['queue_length'],1)
        self.assertIn('already queued',self.run_cli('process',b['session_dir'],ok=False)['message'])
        c = self.run_cli('start','--title','C')
        self.run_cli('stop')
        queued = self.wait_state({'queued'})
        self.assertEqual([x['title'] for x in queued['queue']], ['B','C'])
        before = {str(Path(s['session_dir'])/n):(Path(s['session_dir'])/n).read_bytes()
                  for s in (a,b,c) for n in ('microphone.wav','remote.wav')}
        self.assertEqual((probe/'events').read_text().splitlines(), ['A'])
        (probe/'release-A').touch()
        next_status = self.wait_processing('B')
        self.assertEqual(next_status['state'],'queued')
        self.assertEqual([x['title'] for x in next_status['queue']],['C'])
        (probe/'release-B').touch()
        self.assertEqual(self.wait_processing('C')['queue_length'],0)
        (probe/'release-C').touch()
        final = self.wait_state({'complete'})
        self.assertEqual(final['title'],'C')
        self.assertEqual((probe/'events').read_text().splitlines(),['A','B','C'])
        self.assertTrue(all(Path(p).read_bytes()==data for p,data in before.items()))

    def test_failed_job_continues_queue(self):
        probe = self.enable_queue_probe()
        self.env['QUEUE_FAIL_TITLE'] = 'A'
        a = self.run_cli('start','--title','A')
        self.run_cli('stop')
        self.wait_processing('A')
        self.run_cli('start','--title','B')
        self.run_cli('stop')
        self.wait_state({'queued'})
        (probe/'release-A').touch()
        self.wait_processing('B')
        self.assertEqual(json.loads((Path(a['session_dir'])/'session.json').read_text())['state'],'failed')
        (probe/'release-B').touch()
        self.assertEqual(self.wait_state({'complete'})['queue_length'],0)

    def test_dead_processor_recovers_queue_without_stopping_new_capture(self):
        probe = self.enable_queue_probe()
        a = self.run_cli('start','--title','A')
        self.run_cli('stop')
        self.wait_processing('A')
        processor = json.loads((self.root/'state/kwispr/meetings/processing.json').read_text())
        self.run_cli('start','--title','B')
        self.run_cli('stop')
        self.wait_state({'queued'})
        self.run_cli('start','--title','C')
        capture_pid = int((self.root/'capture.pid').read_text())
        os.kill(processor['worker_pid'],signal.SIGKILL)
        state = self.wait_processing('B')
        self.assertEqual(state['state'],'recording')
        self.assertEqual(state['recording']['title'],'C')
        self.assertTrue(sessions.process_identity(capture_pid))
        self.assertEqual(json.loads((Path(a['session_dir'])/'session.json').read_text())['state'],'failed')
        (probe/'release-all').touch()
        self.run_cli('stop')
        self.wait_state({'complete'})

    def test_retry_other_meeting_preserves_capture_owner(self):
        a = self.run_cli('start','--title','A')
        self.run_cli('stop')
        self.wait_state({'complete'})
        probe = self.enable_queue_probe()
        b = self.run_cli('start','--title','B')
        self.run_cli('process',a['session_dir'])
        state = self.wait_processing('A')
        self.assertEqual(state['recording']['session_dir'], b['session_dir'])
        self.assertEqual(self.run_cli('stop')['title'],'B')
        self.wait_state({'queued'})
        (probe/'release-all').touch()
        self.wait_state({'complete'})


@unittest.skipUnless(os.environ.get('KWISPR_MEETING_AUDIO_SMOKE') == '1',
                     'Set KWISPR_MEETING_AUDIO_SMOKE=1 to test a disposable PulseAudio null sink')
class PulseCaptureSmokeTests(unittest.TestCase):
    def test_real_capture_has_separate_tracks_on_shared_timeline(self):
        import array
        import math
        import wave
        for tool in ('ffmpeg', 'pactl', 'paplay'):
            if not shutil.which(tool):
                self.skipTest(f'{tool} is unavailable')
        name = f'kwispr_smoke_{os.getpid()}'
        defaults = [subprocess.check_output(['pactl', command], text=True).strip()
                    for command in ('get-default-source', 'get-default-sink')]
        module = subprocess.check_output(['pactl', 'load-module', 'module-null-sink',
                                         f'sink_name={name}',
                                         'sink_properties=device.description=KwisprDisposableTest'], text=True).strip()
        recorder = None
        try:
            with tempfile.TemporaryDirectory(prefix='kwispr-audio-smoke-') as temporary:
                directory = Path(temporary)
                tone = directory / 'tone.wav'
                with wave.open(str(tone), 'wb') as audio:
                    audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                    samples = array.array('h', [int(12000 * math.sin(2 * math.pi * 440 * index / 48000))
                                                 for index in range(24000)])
                    audio.writeframes(samples.tobytes())
                command = sessions.ffmpeg_command({'mic_source': f'{name}.monitor',
                                                    'monitor_source': f'{name}.monitor'}, directory)
                recorder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE)
                deadline = time.monotonic() + 10
                while not all((directory / name).exists() for name in ('microphone.wav', 'remote.wav')):
                    if recorder.poll() is not None:
                        self.fail(recorder.stderr.read().decode())
                    if time.monotonic() > deadline:
                        self.fail('Disposable capture did not start')
                    time.sleep(.05)
                time.sleep(.5)
                subprocess.run(['paplay', f'--device={name}', str(tone)], check=True, timeout=10)
                time.sleep(.5)
                sessions.finish_capture(recorder)
                onsets = []
                for filename in ('microphone.wav', 'remote.wav'):
                    with wave.open(str(directory / filename), 'rb') as audio:
                        self.assertEqual((audio.getnchannels(), audio.getframerate()), (1, 16000))
                        data = array.array('h', audio.readframes(audio.getnframes()))
                        self.assertGreater(len(data), 16000)
                        onsets.append(next(index for index, value in enumerate(data) if abs(value) > 1000) / 16000)
                # Pulse input buffering is not sample-perfect; bound it to one short fragment.
                self.assertLess(abs(onsets[0] - onsets[1]), .060, onsets)
                print(f'Pulse shared-tone onset difference: {abs(onsets[0] - onsets[1]):.6f}s')
        finally:
            if recorder:
                if recorder.poll() is None:
                    sessions.finish_capture(recorder)
                recorder.stdin.close()
                recorder.stderr.close()
            subprocess.run(['pactl', 'unload-module', module], check=True)
            self.assertEqual(defaults, [subprocess.check_output(['pactl', command], text=True).strip()
                                        for command in ('get-default-source', 'get-default-sink')])


if __name__ == '__main__':
    unittest.main()
