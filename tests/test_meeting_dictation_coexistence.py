"""Opt-in real Pulse/ffmpeg coexistence smoke, with isolated output and fake STT.

Run: KWISPR_MEETING_AUDIO_SMOKE=1 python3 -m unittest \
    tests.test_meeting_dictation_coexistence -v

Only disposable null sinks receive synthetic tones. No physical microphone,
real clipboard, model, or transcription server is used. This proves concurrent
capture/session isolation; the shared STT server's compute scheduling is separate.
"""
from __future__ import annotations

import array
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import wave

from kwispr_meetings.sessions import process_identity

ROOT = Path(__file__).resolve().parents[1]

PIPELINE = '''from pathlib import Path
import os, time
def require_ready(config): pass
def process_session(directory, config, progress_callback=None):
    marker = Path(os.environ['KWISPR_COEXISTENCE_ROOT']) / 'release-processing'
    deadline = time.monotonic() + 45
    while not marker.exists():
        if time.monotonic() >= deadline: raise RuntimeError('Smoke processing timed out')
        time.sleep(.05)
    path = Path(directory) / 'transcript.md'
    path.write_text('Synthetic meeting processing completed.\\n')
    return {'transcript_path': str(path), 'speaker_count': 1}
'''
CURL = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['KWISPR_COEXISTENCE_ROOT'])
args = sys.argv[1:]
with (root / 'requests.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
Path(args[args.index('-o') + 1]).write_text(json.dumps({'text': 'Synthetic dictation completed.'}))
print('200', end='')
'''
CLIPBOARD = '''#!/usr/bin/env python3
import os, sys
from pathlib import Path
(Path(os.environ['KWISPR_COEXISTENCE_ROOT']) / 'fake-clipboard.txt').write_text(sys.stdin.read())
'''


@unittest.skipUnless(os.environ.get('KWISPR_MEETING_AUDIO_SMOKE') == '1',
                     'Set KWISPR_MEETING_AUDIO_SMOKE=1 for disposable audio coexistence smoke')
class MeetingDictationCoexistenceTests(unittest.TestCase):
    def test_dictation_can_capture_during_meeting_capture_and_processing(self):
        for tool in ('ffmpeg', 'pactl', 'paplay', 'jq', 'bash'):
            if not shutil.which(tool):
                self.skipTest(f'{tool} is unavailable')
        pactl, paplay = shutil.which('pactl'), shutil.which('paplay')
        defaults = self.audio_defaults(pactl)
        modules = []
        owned_processes = {}
        meeting_started = False
        temporary = tempfile.TemporaryDirectory(prefix='kwispr-coexistence-')
        root = Path(temporary.name)
        app, commands = root / 'app', root / 'bin'
        app.mkdir()
        commands.mkdir()
        for name in ('kwispr.sh', 'kwispr-meetings.py'):
            shutil.copy2(ROOT / name, app / name)
        shutil.copytree(ROOT / 'kwispr_meetings', app / 'kwispr_meetings',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (app / 'kwispr_meetings/pipeline.py').write_text(PIPELINE)
        for name, source in (('curl', CURL), ('wl-copy', CLIPBOARD),
                             ('notify-send', '#!/bin/sh\nprintf "1\\n"\n'),
                             ('ydotool', '#!/bin/sh\nexit 99\n'),
                             ('paplay', '#!/bin/sh\nexit 99\n')):
            path = commands / name
            path.write_text(source)
            path.chmod(0o700)
        # Capture uses the real ffmpeg even if the invoking environment has wrappers.
        (commands / 'ffmpeg').symlink_to(Path(shutil.which('ffmpeg')).resolve())
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('KWISPR_') and key != 'OPENAI_API_KEY'}
        env.update(PATH=f'{commands}:{os.environ["PATH"]}',
                   XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'),
                   XDG_STATE_HOME=str(root / 'state'), XDG_DATA_HOME=str(root / 'data'),
                   KWISPR_CONFIG_FILE=str(root / 'config.env'),
                   KWISPR_MEETING_RUNTIME_DIR=str(root / 'unused-runtime'),
                   KWISPR_COEXISTENCE_ROOT=str(root))
        cache = root / 'cache/kwispr'
        meeting_entry = [sys.executable, str(app / 'kwispr-meetings.py')]
        log = (root / 'dictation.log').open('w+')

        def meeting(*args):
            result = subprocess.run(meeting_entry + list(args), env=env, capture_output=True,
                                    text=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        def wait(predicate, description, timeout=12):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(.05)
            self.fail(description)

        def toggle():
            result = subprocess.run([str(app / 'kwispr.sh'), 'toggle'], env=env,
                                    stdout=log, stderr=log, timeout=20)
            log.flush()
            self.assertEqual(result.returncode, 0, (root / 'dictation.log').read_text())

        def remember_dictation_processes():
            for line in (cache / 'current.pid').read_text().splitlines():
                pid = int(line)
                owned_processes[pid] = process_identity(pid)

        def tone(sink, filename, frequency, seconds=.7):
            path = root / filename
            samples = array.array('h', [int(10000 * math.sin(2 * math.pi * frequency * index / 48000))
                                       for index in range(int(48000 * seconds))])
            with wave.open(str(path), 'wb') as audio:
                audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                audio.writeframes(samples.tobytes())
            subprocess.run([paplay, f'--device={sink}', str(path)], check=True, timeout=10)

        def start_dictation():
            toggle()
            wait(lambda: (cache / 'current.pid').exists(), 'Dictation did not create its PID file')
            remember_dictation_processes()
            path = Path((cache / 'current.path').read_text().strip())
            wait(path.exists, 'Dictation did not open its WAV file')
            time.sleep(.3)
            return path

        prefix = f'kwispr_coexist_{os.getpid()}'
        mic_sink, remote_sink, mic_source = prefix + '_mic', prefix + '_remote', prefix + '_source'
        try:
            for sink in (mic_sink, remote_sink):
                modules.append(subprocess.check_output([pactl, 'load-module', 'module-null-sink',
                               f'sink_name={sink}', 'sink_properties=device.description=KwisprCoexistenceTest priority.session=0'], text=True).strip())
            modules.append(subprocess.check_output([pactl, 'load-module', 'module-remap-source',
                           f'master={mic_sink}.monitor', f'source_name={mic_source}',
                           'source_properties=device.description=KwisprCoexistenceMicrophone priority.session=0'], text=True).strip())
            self.assertEqual(defaults, self.audio_defaults(pactl), 'Disposable sources changed host defaults')
            (root / 'config.env').write_text(
                'KWISPR_API_URL=http://127.0.0.1:1/v1/audio/transcriptions\n'
                'KWISPR_MODEL=synthetic-smoke-model\nKWISPR_AUTOPASTE=0\nKWISPR_SOUNDS=0\n'
                f'KWISPR_PULSE_SOURCE={mic_source}\nKWISPR_STOP_DELAY_MS=100\n')
            started = meeting('start', '--mic', mic_source, '--monitor', remote_sink + '.monitor',
                              '--output-dir', str(root / 'meetings'))
            meeting_started = True
            self.assertEqual(started['state'], 'recording')
            directory = Path(started['session_dir'])
            pointer_path = root / 'state/kwispr/meetings/current.json'
            pointer_before = json.loads(pointer_path.read_text())
            self.assertNotEqual(directory.parent, cache)
            tone(mic_sink, 'before-dictation.wav', 440)

            first_dictation = start_dictation()
            self.assertTrue((cache / 'current.pid').exists())
            self.assertEqual(meeting('status')['state'], 'recording')
            tone(mic_sink, 'during-dictation.wav', 660)
            tone(remote_sink, 'remote-call.wav', 880)
            toggle()
            self.assertFalse((cache / 'current.pid').exists())
            self.assertEqual(meeting('status')['state'], 'recording')
            self.assertEqual(json.loads(pointer_path.read_text()), pointer_before,
                             'Dictation replaced meeting ownership')
            first_audio = self.wav_samples(first_dictation)
            self.assertGreater(self.energy_windows(first_audio), 0)
            self.assertEqual((root / 'fake-clipboard.txt').read_text().strip(), 'Synthetic dictation completed.')

            # A second tone after normal dictation Stop proves the meeting kept recording.
            tone(mic_sink, 'after-dictation.wav', 550)
            self.assertEqual(meeting('stop')['state'], 'stopping')
            wait(lambda: meeting('status')['state'] == 'processing', 'Meeting did not enter processing')
            microphone = self.wav_samples(directory / 'microphone.wav')
            remote = self.wav_samples(directory / 'remote.wav')
            self.assertGreater(len(microphone), len(first_audio))
            self.assertGreater(self.energy_windows(remote), 0)
            self.assertGreaterEqual(self.voiced_runs(microphone), 3,
                                    'Meeting must contain tones before, during, and after dictation')

            second_dictation = start_dictation()
            self.assertEqual(meeting('status')['state'], 'processing')
            tone(mic_sink, 'during-processing.wav', 770)
            time.sleep(.4)
            toggle()
            self.assertEqual(meeting('status')['state'], 'processing')
            self.assertGreater(self.energy_windows(self.wav_samples(second_dictation)), 0)
            requests = [json.loads(line) for line in (root / 'requests.jsonl').read_text().splitlines()]
            self.assertEqual(len(requests), 2)
            self.assertTrue(all('http://127.0.0.1:1/v1/audio/transcriptions' in request for request in requests))
            (root / 'release-processing').touch()
            wait(lambda: meeting('status')['state'] == 'complete', 'Meeting failed to complete after release')
            self.assertEqual((root / 'fake-clipboard.txt').read_text().strip(), 'Synthetic dictation completed.',
                             'Meeting processing must not overwrite dictation clipboard')
            self.assertTrue((directory / 'transcript.md').is_file())
            print(f'Concurrent capture passed: meeting microphone {len(microphone)/16000:.2f}s, '
                  f'remote {len(remote)/16000:.2f}s, first dictation {len(first_audio)/16000:.2f}s; '
                  'second dictation also captured and reached fake STT during processing.')
        finally:
            (root / 'release-processing').touch()
            if meeting_started:
                try:
                    meeting('stop')
                    wait(lambda: meeting('status')['state'] not in {'starting','recording','stopping','processing'},
                         'Meeting cleanup timed out', timeout=15)
                except (AssertionError, subprocess.SubprocessError):
                    pointer = json.loads(pointer_path.read_text())
                    pid = pointer['worker_pid']
                    if process_identity(pid) == pointer['worker_identity']:
                        os.kill(pid, signal.SIGTERM)
            for pid, identity in owned_processes.items():
                if identity and process_identity(pid) == identity:
                    os.kill(pid, signal.SIGTERM)
            log.close()
            for module in reversed(modules):
                subprocess.run([pactl, 'unload-module', module], check=True)
            self.assertEqual(defaults, self.audio_defaults(pactl), 'Host audio defaults changed')
            temporary.cleanup()

    @staticmethod
    def audio_defaults(pactl):
        return [subprocess.check_output([pactl, command], text=True).strip()
                for command in ('get-default-source', 'get-default-sink')]

    @staticmethod
    def wav_samples(path):
        with wave.open(str(path), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise AssertionError('Expected a real mono 16 kHz PCM capture')
            return array.array('h', audio.readframes(audio.getnframes()))

    @staticmethod
    def energy_windows(samples):
        return sum(any(abs(value) > 1000 for value in samples[index:index + 160])
                   for index in range(0, len(samples), 160))

    @staticmethod
    def voiced_runs(samples):
        # Require sustained sound separated by at least 100 ms of silence.
        runs, sounding, silence = 0, False, 0
        for index in range(0, len(samples), 160):
            voice = any(abs(value) > 1000 for value in samples[index:index + 160])
            if voice:
                if not sounding:
                    runs += 1
                sounding, silence = True, 0
            else:
                silence += 1
                if silence >= 10:
                    sounding = False
        return runs


if __name__ == '__main__':
    unittest.main()
