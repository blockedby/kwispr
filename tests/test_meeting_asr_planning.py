"""Keep acoustic context without discarding or inventing speaker assignments."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from kwispr_meetings import pipeline


def span(start, end, *speakers):
    return {"start": start, "end": end, "speakers": list(speakers)}


class MeetingAsrPlanningTests(unittest.TestCase):
    def test_known_single_groups_native_id_changes_without_crossing_silence(self):
        native = [span(0, 2, "old_01"), span(2, 2.3, "old_01", "old_02"),
                  span(2.3, 5, "old_02"), span(5, 7, "old_03"),
                  span(7.4, 8, "old_03"), span(8, 9, "old_04")]
        before = copy.deepcopy(native)
        identities = [{**row, "speakers": ["speaker_01"], "source_speakers": row["speakers"],
                       "speaker_assignment": "explicit"} for row in native]
        planned = pipeline.assign_transcription_speakers(pipeline.plan_single_speaker_intervals(native), identities)
        self.assertEqual([(row["start"], row["end"]) for row in planned], [(0, 7), (7.4, 9)])
        self.assertEqual([row["speakers"] for row in planned], [["speaker_01"], ["speaker_01"]])
        self.assertEqual([source for row in planned for source in row["source_intervals"]], identities)
        self.assertEqual(native, before)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Optional meeting numerical runtime is not installed")
    def test_known_single_long_phrase_splits_without_losing_or_repeating_speech_samples(self):
        import numpy as np
        native = [span(0, 22, "old_01"), span(22, 22.3, "old_01", "old_02"),
                  span(22.3, 90, "old_02")]
        identities = [{**row, "speakers": ["speaker_01"]} for row in native]
        planned = pipeline.assign_transcription_speakers(pipeline.plan_single_speaker_intervals(native), identities)
        chunks = pipeline.split_intervals(planned, np.zeros(90 * 16000))
        self.assertTrue(all(row["end"] - row["start"] <= 45 for row in chunks))
        self.assertEqual(chunks[0]["start"], 0)
        self.assertEqual(chunks[-1]["end"], 90)
        self.assertTrue(all(left["end"] == right["start"] for left, right in zip(chunks, chunks[1:])))
        self.assertTrue(all(row["speakers"] == ["speaker_01"] for row in chunks))
        sources = [source for row in chunks for source in row["source_intervals"]]
        sample_ranges = [(int(row["start"] * 16000), int(row["end"] * 16000)) for row in sources]
        self.assertEqual(sample_ranges[0][0], 0)
        self.assertEqual(sample_ranges[-1][1], 90 * 16000)
        self.assertTrue(all(left[1] == right[0] for left, right in zip(sample_ranges, sample_ranges[1:])))

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Optional meeting numerical runtime is not installed")
    def test_known_single_processing_reuses_cached_native_spans(self):
        native = [span(0, 2, "old_01"), span(2, 2.3, "old_01", "old_02"),
                  span(2.3, 5, "old_02"), span(5.5, 8, "old_03")]
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            for name in ["microphone.wav", "remote.wav"]:
                (root / name).write_bytes(b"source-audio")
            pipeline.atomic_json(root / "session.json", {"speakers": 1})
            config = {"KWISPR_API_URL": "http://127.0.0.1:19650/v1/audio/transcriptions"}
            signature = pipeline._fingerprint(root, config, {"speakers": 1})
            pipeline.atomic_json(root / "diarization.json", {"signature": signature,
                                                               "tracks": {"remote": native, "microphone": []}})
            stack.enter_context(patch.object(pipeline, "require_ready"))
            stack.enter_context(patch.object(pipeline, "_load_track", return_value=[0] * (9 * 16000)))
            diarize = stack.enter_context(patch.object(pipeline, "_diarize"))
            stack.enter_context(patch.object(pipeline, "_wav_slice", return_value=b"test-pcm"))
            request = stack.enter_context(patch.object(pipeline, "_request_transcription",
                                                       return_value={"text": "hello", "language": "en"}))
            stack.enter_context(patch.object(pipeline.time, "sleep"))
            result = pipeline.process_session(root, config)
            self.assertEqual(diarize.call_count, 0)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(result["speaker_count"], 1)
            rows = json.loads((root / "transcript.json").read_text())["utterances"]
            self.assertEqual([(row["start"], row["end"]) for row in rows], [(0, 5), (5.5, 8)])
            self.assertEqual([row["speakers"] for row in rows], [["speaker_01"], ["speaker_01"]])
            self.assertEqual([source for row in rows for source in row["source_intervals"]],
                             [{**row, "source_speakers": row["speakers"], "speakers": ["speaker_01"],
                               "speaker_assignment": "explicit"} for row in native])
            self.assertEqual((root / "remote.wav").read_bytes(), b"source-audio")

    def test_refinement_annotations_do_not_break_a_grouped_acoustic_phrase(self):
        native = [span(0, 2, "a"), span(2, 2.3, "a", "b"), span(2.3, 5, "a")]
        refined = [span(0, 1.9, "speaker_01"), span(1.9, 2, "speaker_unknown"),
                   span(2, 2.3, "speaker_unknown"), span(2.3, 2.4, "speaker_unknown"),
                   span(2.4, 5, "speaker_01")]
        before = copy.deepcopy(refined)
        planned = pipeline.assign_transcription_speakers(pipeline.plan_transcription_intervals(native), refined)
        self.assertEqual(len(planned), 1)
        self.assertEqual((planned[0]["start"], planned[0]["end"]), (0, 5))
        self.assertEqual(planned[0]["source_intervals"], refined)
        self.assertEqual(planned[0]["speakers"], ["speaker_01", "speaker_unknown"])
        self.assertEqual(refined, before)

    def test_identity_overlay_clips_shared_range_to_each_acoustic_phrase(self):
        planned = [span(0, 2, "native_a"), span(3, 5, "native_b")]
        identities = [span(0, 2, "speaker_01"), span(3, 4, "speaker_02"), span(4, 5, "speaker_unknown")]
        result = pipeline.assign_transcription_speakers(planned, identities)
        self.assertEqual([r["source_intervals"] for r in result], [identities[:1], identities[1:]])
        self.assertFalse(result[0]["speaker_grouped"])
        self.assertTrue(result[1]["speaker_grouped"])
        self.assertEqual(pipeline._slice_transcription_interval(result[1], 4, 5)["speakers"], ["speaker_unknown"])

    def test_short_uncertain_identity_keeps_context_and_exact_unknown_span(self):
        sources = [span(0, 2, "speaker_01"), span(2, 2.3, "speaker_unknown"), span(2.3, 4, "speaker_01")]
        planned = pipeline.plan_transcription_intervals(sources)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["source_intervals"], sources)
        self.assertEqual(planned[0]["speakers"], ["speaker_01", "speaker_unknown"])
        sources[-1]["speakers"] = ["speaker_02"]
        self.assertEqual(pipeline.plan_transcription_intervals(sources), sources)

    def test_short_overlap_bridges_keep_exact_sources_and_continuing_voice(self):
        intervals = [span(0, 2, "speaker_01"), span(2, 2.3, "speaker_01", "speaker_02"),
                     span(2.3, 2.4, "speaker_01"), span(2.4, 2.8, "speaker_01", "speaker_03"),
                     span(2.8, 5, "speaker_01")]
        before = copy.deepcopy(intervals)
        planned = pipeline.plan_transcription_intervals(intervals)
        self.assertEqual(intervals, before)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["source_intervals"], intervals)
        self.assertEqual(planned[0]["speakers"], ["speaker_01", "speaker_02", "speaker_03"])
        self.assertEqual((planned[0]["start"], planned[0]["end"]), (0, 5))
        self.assertTrue(planned[0]["speaker_grouped"])

    def test_real_changes_long_overlaps_and_pauses_are_not_grouped(self):
        variants = [
            [span(0, 2, "a"), span(2, 2.3, "b"), span(2.3, 4, "a")],
            [span(0, 2, "a"), span(2, 2.3, "a", "b"), span(2.3, 4, "b")],
            [span(0, 2, "a"), span(2, 3, "a", "b"), span(3, 4, "a")],
            [span(0, 2, "a"), span(2.5, 2.8, "a", "b"), span(2.8, 4, "a")],
            [span(0, 2, "a"), span(2, 2.3, "a", "b"), span(2.8, 4, "a")],
        ]
        for intervals in variants:
            with self.subTest(intervals=intervals):
                self.assertEqual(pipeline.plan_transcription_intervals(intervals), intervals)

    def test_chunk_metadata_only_contains_sources_and_speakers_inside_that_chunk(self):
        sources = [span(0, 2, "a"), span(2, 2.3, "a", "b"), span(2.3, 7, "a")]
        grouped = pipeline.plan_transcription_intervals(sources)[0]
        before = copy.deepcopy(grouped)
        first = pipeline._slice_transcription_interval(grouped, 0, 2.15)
        last = pipeline._slice_transcription_interval(grouped, 2.3, 7)
        self.assertEqual(first["source_intervals"], [sources[0], span(2, 2.15, "a", "b")])
        self.assertEqual(first["speakers"], ["a", "b"])
        self.assertTrue(first["speaker_grouped"])
        self.assertEqual(last["source_intervals"], [sources[2]])
        self.assertEqual(last["speakers"], ["a"])
        self.assertFalse(last["speaker_grouped"])
        self.assertEqual(grouped, before)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Optional meeting numerical runtime is not installed")
    def test_long_group_splits_with_full_timeline_coverage_and_local_speakers(self):
        import numpy as np
        sources = [span(0, 39.2, "a"), span(39.2, 39.7, "a", "b"), span(39.7, 90, "a")]
        planned = pipeline.plan_transcription_intervals(sources)
        chunks = pipeline.split_intervals(planned, np.zeros(90 * 16000))
        self.assertEqual(chunks[0]["start"], 0)
        self.assertEqual(chunks[-1]["end"], 90)
        self.assertTrue(all(chunk["end"] - chunk["start"] <= 45 for chunk in chunks))
        self.assertTrue(all(left["end"] == right["start"] for left, right in zip(chunks, chunks[1:])))
        self.assertEqual(chunks[0]["speakers"], ["a", "b"])
        self.assertTrue(all(chunk["speakers"] == ["a"] for chunk in chunks[1:]))
        self.assertAlmostEqual(sum(s["end"] - s["start"] for c in chunks for s in c["source_intervals"]), 90)

    def test_processing_keeps_text_and_source_times_without_gain_or_vad_override(self):
        segments = [{"start": 0, "end": 4, "speaker": "speaker_01"},
                    {"start": 2, "end": 2.3, "speaker": "speaker_02"}]
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            for name in ["microphone.wav", "remote.wav"]:
                (root / name).write_bytes(b"source-audio")
            pipeline.atomic_json(root / "session.json", {"speakers": 0, "remote_offset_seconds": 0.125})
            config = {"KWISPR_API_URL": "http://127.0.0.1:19650/v1/audio/transcriptions"}
            for context in [patch.object(pipeline, "require_ready"),
                            patch.object(pipeline, "_load_track", return_value=[0] * 80000),
                            patch.object(pipeline, "_diarize", side_effect=[segments, []]),
                            patch.object(pipeline, "split_intervals", side_effect=lambda spans, audio: spans),
                            patch.object(pipeline.time, "sleep")]:
                stack.enter_context(context)
            wav = stack.enter_context(patch.object(pipeline, "_wav_slice", return_value=b"unchanged-pcm"))
            request = stack.enter_context(patch.object(pipeline, "_request_transcription",
                                                       return_value={"text": "Проверим React component.", "language": "ru"}))
            result = pipeline.process_session(root, config)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], b"unchanged-pcm")
            self.assertEqual(request.call_args.kwargs, {"language": ""})
            self.assertEqual(wav.call_args.args[1:], (0, 4.2))
            document = json.loads((root / "transcript.json").read_text())
            row = document["utterances"][0]
            self.assertEqual(row["text"], "Проверим React component.")
            self.assertEqual(row["source_intervals"], [span(.125, 2.125, "speaker_01"),
                                                      span(2.125, 2.425, "speaker_01", "speaker_02"),
                                                      span(2.425, 4.125, "speaker_01")])
            self.assertTrue(row["speaker_grouped"])
            markdown = (root / "transcript.md").read_text()
            self.assertIn("несколько голосов внутри фрагмента", markdown)
            self.assertNotIn("говорят одновременно", markdown)
            self.assertEqual((root / "remote.wav").read_bytes(), b"source-audio")
            self.assertEqual(result["speaker_count"], 2)

    def test_unknown_speaker_is_preserved_without_inventing_a_number(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [{**span(0, 1, "speaker_unknown"), "track": "remote", "text": "Сохранённый текст"}]
            result = pipeline.write_outputs(root, {}, rows)
            self.assertIn("голос не определён", (root / "transcript.md").read_text())
            self.assertEqual(result["speaker_count"], 0)
            self.assertTrue(result["has_unknown_speaker"])
            self.assertEqual(json.loads((root / "transcript.json").read_text())["utterances"][0]["speakers"], ["speaker_unknown"])

    def test_planner_version_invalidates_old_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ["microphone.wav", "remote.wav"]:
                (root / name).write_bytes(b"fixture")
            signature = pipeline._fingerprint(root, {}, {})
            with patch.object(pipeline, "PIPELINE_VERSION", 2):
                self.assertNotEqual(signature, pipeline._fingerprint(root, {}, {}))


if __name__ == "__main__":
    unittest.main()
