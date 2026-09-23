"""Identity and audio-coverage contracts without model downloads or private data."""
import importlib.util
import unittest

from kwispr_meetings.diarization import (
    UNKNOWN, reference_windows, refine_from_embeddings,
)


def interval(start, end, speaker="old_1"):
    return {"start": start, "end": end, "speakers": [speaker]}


@unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy is provided by the meeting runtime")
class DiarizationRefinementTest(unittest.TestCase):
    def assert_coverage(self, before, after):
        def union(rows):
            merged = []
            for row in rows:
                if merged and merged[-1][1] == row["start"]:
                    merged[-1][1] = row["end"]
                else:
                    merged.append([row["start"], row["end"]])
            return merged
        self.assertEqual(union(before), union(after))
        self.assertTrue(all(row["end"] > row["start"] for row in after))

    def test_reference_windows_exclude_short_and_overlapping_speech(self):
        rows = [interval(0, 1), interval(2, 12.5),
                {"start": 13, "end": 16, "speakers": ["old_1", "old_2"]}]
        windows = reference_windows(rows)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0]["start"], 2)
        self.assertEqual(windows[-1]["end"], 12.5)
        self.assertTrue(all(2 <= w["end"] - w["start"] <= 5 for w in windows))

    def test_auto_is_identical_and_single_count_uses_explicit_identity(self):
        rows = [interval(0, 1, "old_37"), interval(2, 8)]
        automatic, report = refine_from_embeddings(rows, [], [], 0)
        self.assertEqual(automatic, rows)
        self.assertEqual(report["status"], "not_applied")
        single, report = refine_from_embeddings(rows, [], [], 1)
        self.assertTrue(all(row["speakers"] == ["speaker_01"] for row in single))
        self.assertEqual(report["status"], "explicit_single")
        self.assert_coverage(rows, single)

    def test_clean_references_split_merged_native_identity_without_losing_samples(self):
        rows = [interval(0, 30)]
        windows = reference_windows(rows)
        result, report = refine_from_embeddings(rows, windows, [[1, 0]] * 3 + [[0, 1]] * 3, 2)
        self.assertEqual(report["status"], "supported")
        self.assertEqual([(r["start"], r["end"], r["speakers"]) for r in result],
                         [(0, 15, ["speaker_01"]), (15, 30, ["speaker_02"])])
        self.assert_coverage(rows, result)

    def test_global_native_majority_does_not_relabel_short_minority_turn(self):
        # Both speakers share one flawed native identity. The short middle turn
        # has disagreeing immediate references despite the first voice majority.
        rows = [interval(0, 50), interval(50.1, 50.8), interval(50.9, 65.9)]
        windows = reference_windows(rows)
        result, report = refine_from_embeddings(rows, windows, [[1, 0]] * 10 + [[0, 1]] * 3, 2)
        short = next(r for r in result if r["start"] == 50.1)
        self.assertEqual(short["speakers"], [UNKNOWN])
        self.assertEqual(report["supported_profiles"], 2)
        self.assert_coverage(rows, result)

    def test_short_turn_requires_close_both_sides_and_same_native_identity(self):
        rows = [interval(0, 15), interval(15.1, 15.8), interval(15.9, 30.9), interval(40, 55, "old_2")]
        windows = reference_windows(rows)
        vectors = [[1, 0]] * 6 + [[0, 1]] * 3
        result, _ = refine_from_embeddings(rows, windows, vectors, 2)
        short = next(r for r in result if r["start"] == 15.1)
        self.assertEqual(short["speakers"], ["speaker_01"])
        self.assertEqual(short["speaker_assignment"], "temporal_agreement")
        rows[1]["speakers"] = ["old_other"]
        result, _ = refine_from_embeddings(rows, windows, vectors, 2)
        self.assertEqual(next(r for r in result if r["start"] == 15.1)["speakers"], [UNKNOWN])
        self.assert_coverage(rows, result)

    def test_weak_two_way_split_retains_one_profile_with_explicit_warning(self):
        rows = [interval(0, 40)]
        windows = reference_windows(rows)
        vectors = [[1, 0.02], [1, -0.02]] * 4
        result, report = refine_from_embeddings(rows, windows, vectors, 2)
        self.assertEqual(report["status"], "limited")
        self.assertEqual(report["requested_speakers"], 2)
        self.assertEqual(report["supported_profiles"], 1)
        self.assertTrue(report["warnings"])
        self.assertEqual({tuple(r["speakers"]) for r in result}, {("speaker_01",)})
        self.assert_coverage(rows, result)

    def test_single_outlier_does_not_create_requested_second_voice(self):
        rows = [interval(0, 50)]
        windows = reference_windows(rows)
        result, report = refine_from_embeddings(rows, windows, [[1, 0]] * 9 + [[0, 1]], 2)
        self.assertEqual(report["status"], "limited")
        self.assertEqual(result[-1]["speakers"], [UNKNOWN])
        self.assertEqual(report["unknown_seconds"], 5)
        self.assert_coverage(rows, result)

    def test_inadequate_references_preserve_every_turn_as_unknown(self):
        rows = [interval(0, 1), interval(1.1, 4), interval(4.1, 5)]
        windows = reference_windows(rows)
        result, report = refine_from_embeddings(rows, windows, [[1, 0]], 2)
        self.assertEqual(report["status"], "uncertain")
        self.assertTrue(all(r["speakers"] == [UNKNOWN] for r in result))
        self.assert_coverage(rows, result)

    def test_invalid_embedding_keeps_its_original_speech(self):
        rows = [interval(0, 35)]
        windows = reference_windows(rows)
        vectors = [[1, 0]] * 3 + [[float("nan"), 0]] + [[0, 1]] * 3
        result, report = refine_from_embeddings(rows, windows, vectors, 2)
        self.assertEqual(report["status"], "supported")
        self.assertEqual(next(r for r in result if r["start"] == 15)["speakers"], [UNKNOWN])
        self.assert_coverage(rows, result)

    def test_overlap_is_not_used_as_a_clean_speaker_reference(self):
        rows = [interval(0, 15), {"start": 15, "end": 15.5, "speakers": ["old_1", "old_2"]}, interval(15.5, 30.5, "old_2")]
        result, _ = refine_from_embeddings(rows, reference_windows(rows), [[1, 0]] * 3 + [[0, 1]] * 3, 2)
        self.assertEqual(next(r for r in result if r["start"] == 15)["speakers"], [UNKNOWN])
        self.assertEqual(next(r for r in result if r["start"] == 15)["source_speakers"], ["old_1", "old_2"])
        self.assert_coverage(rows, result)

    def test_ambiguous_transition_is_unknown_without_dropping_its_audio(self):
        rows = [interval(0, 35)]
        vectors = [[1, 0]] * 3 + [[1, 1]] + [[0, 1]] * 3
        result, report = refine_from_embeddings(rows, reference_windows(rows), vectors, 2)
        self.assertEqual(report["status"], "supported")
        self.assertEqual(next(r for r in result if r["start"] == 15)["speakers"], [UNKNOWN])
        self.assert_coverage(rows, result)

    def test_unreliable_profiles_do_not_invent_people(self):
        rows = [interval(0, 30)]
        vectors = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 0], [-1, 0]]
        result, report = refine_from_embeddings(rows, reference_windows(rows), vectors, 3)
        self.assertEqual(report["supported_profiles"], 0)
        self.assertTrue(all(r["speakers"] == [UNKNOWN] for r in result))
        self.assert_coverage(rows, result)

    def test_invalid_ranges_are_rejected_instead_of_silently_dropped(self):
        for rows in [[interval(2, 1)], [interval(0, 3), interval(2, 4)], [interval(0, float("inf"))]]:
            with self.assertRaises(ValueError):
                reference_windows(rows)


if __name__ == "__main__":
    unittest.main()
