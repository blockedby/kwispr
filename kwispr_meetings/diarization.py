"""Conservative identity refinement for an explicitly known participant count.

The native segmenter remains the source of speech coverage. Only clean 2--5 s
windows train identities; tiny turns never become independent speaker models.
Scores below are cosine geometry checks, not calibrated probabilities. Auto
diarization is deliberately unchanged until its speaker-count selection is
validated independently.
"""
from __future__ import annotations

import math

REFINEMENT_VERSION = 1
UNKNOWN = "speaker_unknown"
MIN_REFERENCE_SECONDS = 2.0
MAX_REFERENCE_SECONDS = 5.0
MIN_PROFILE_REFERENCES = 3
MIN_PROFILE_SECONDS = 6.0
MIN_CENTROID_DISTANCE = 0.2
MIN_ASSIGNMENT_SIMILARITY = 0.5
MIN_ASSIGNMENT_MARGIN = 0.15


def reference_windows(intervals):
    """Preserve every boundary; exclude overlaps from identity training."""
    windows = []
    previous = -math.inf
    for index, row in enumerate(intervals):
        start, end = float(row["start"]), float(row["end"])
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start or start < previous:
            raise ValueError("Diarization intervals must be finite, ordered and disjoint")
        previous = end
        duration = end - start
        if len(row["speakers"]) != 1 or duration < MIN_REFERENCE_SECONDS:
            continue
        count = max(1, math.ceil((duration - 1e-9) / MAX_REFERENCE_SECONDS))
        for part in range(count):
            windows.append({"interval": index,
                            "start": start + duration * part / count,
                            "end": end if part + 1 == count else start + duration * (part + 1) / count})
    return windows


def _spherical_clusters(vectors, count):
    """Deterministic multiple starts avoid reserving a profile for an outlier."""
    import numpy as np
    rng = np.random.default_rng(37)
    best = None
    for _ in range(24):
        centers = [vectors[int(rng.integers(len(vectors)))]]
        for _ in range(1, count):
            distance = np.maximum(0, 1 - np.max(vectors @ np.asarray(centers).T, axis=1)) ** 2
            if float(distance.sum()) < 1e-12:
                break
            centers.append(vectors[int(rng.choice(len(vectors), p=distance / distance.sum()))])
        if len(centers) != count:
            continue
        centers = np.asarray(centers)
        for _ in range(100):
            labels = np.argmax(vectors @ centers.T, axis=1)
            if len(set(labels.tolist())) != count:
                break
            candidate = np.asarray([vectors[labels == label].mean(axis=0) for label in range(count)])
            norms = np.linalg.norm(candidate, axis=1, keepdims=True)
            if np.any(norms < 1e-10):
                break
            candidate /= norms
            converged = float(np.max(np.abs(candidate - centers))) < 1e-6
            centers = candidate
            if converged:
                break
        labels = np.argmax(vectors @ centers.T, axis=1)
        if len(set(labels.tolist())) != count:
            continue
        score = float(np.max(vectors @ centers.T, axis=1).mean())
        if best is None or score > best[0]:
            best = score, labels, centers
    return best


def refine_from_embeddings(intervals, windows, embeddings, count):
    """Model-free refinement seam; returns (coverage-preserving rows, report).

    Missing/invalid embeddings and uncertain identities are retained as UNKNOWN.
    If two requested profiles are indistinguishable, a well-supported common
    profile may be retained with an explicit warning; this is not evidence that
    only one person attended or spoke.
    """
    import numpy as np
    if not isinstance(count, int) or not 0 <= count <= 16:
        raise ValueError("Expected speaker count must be between 0 and 16")
    report = {"version": REFINEMENT_VERSION, "requested_speakers": count,
              "supported_profiles": 0, "status": "not_applied", "warnings": [],
              "reference_windows": len(windows), "unknown_seconds": 0.0,
              "speech_seconds": sum(row["end"] - row["start"] for row in intervals)}
    if count == 0:
        return [dict(row) for row in intervals], report
    # Also validates ordered/disjoint ranges for caller-supplied references.
    reference_windows(intervals)
    if count == 1:
        report.update(status="explicit_single", supported_profiles=1 if intervals else 0)
        return [{**row, "source_speakers": list(row["speakers"]), "speakers": ["speaker_01"], "speaker_assignment": "explicit"} for row in intervals], report
    if len(windows) != len(embeddings):
        raise ValueError("Every reference window must have one embedding")
    valid_windows, vectors = [], []
    for window, embedding in zip(windows, embeddings):
        index = window["interval"]
        if not isinstance(index, int) or not 0 <= index < len(intervals):
            raise ValueError("Reference window has an invalid interval")
        row = intervals[index]
        if (len(row["speakers"]) != 1 or not row["start"] <= window["start"] < window["end"] <= row["end"]
                or not MIN_REFERENCE_SECONDS - 1e-8 <= window["end"] - window["start"] <= MAX_REFERENCE_SECONDS + 1e-8):
            raise ValueError("Reference must be a clean window inside its speech interval")
        vector = np.asarray(embedding, dtype=np.float32)
        if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
            continue
        norm = float(np.linalg.norm(vector))
        if norm < 1e-10:
            continue
        if vectors and vector.shape != vectors[0].shape:
            raise ValueError("Reference embeddings have inconsistent dimensions")
        valid_windows.append(window)
        vectors.append(vector / norm)
    report["valid_reference_windows"] = len(vectors)
    labels, centers = None, None
    matrix = np.asarray(vectors)
    if len(vectors) >= count * MIN_PROFILE_REFERENCES:
        fitted = _spherical_clusters(matrix, count)
        if fitted is not None:
            _, candidate_labels, candidate_centers = fitted
            distances = 1 - candidate_centers @ candidate_centers.T
            separation = float(distances[np.triu_indices(count, 1)].min())
            report["minimum_centroid_distance"] = separation
            supported = all(
                int((candidate_labels == label).sum()) >= MIN_PROFILE_REFERENCES
                and float(np.median(matrix[candidate_labels == label] @ candidate_centers[label])) >= 0.65
                and sum(w["end"] - w["start"] for w, value in zip(valid_windows, candidate_labels) if value == label) >= MIN_PROFILE_SECONDS
                for label in range(count))
            if supported and separation >= MIN_CENTROID_DISTANCE:
                labels, centers = candidate_labels, candidate_centers
                report.update(status="supported", supported_profiles=count)
    if centers is None:
        report["warnings"].append("The requested number of voices is not reliably separated by clean speech references.")
        report["status"] = "uncertain"
        # Retain a common audible profile only when it explains most clean
        # references. Outliers remain unknown; never infer absent participants.
        if len(vectors) >= MIN_PROFILE_REFERENCES:
            centroid = matrix.mean(axis=0)
            norm = float(np.linalg.norm(centroid))
            if norm > 1e-10:
                centroid /= norm
                similarities = matrix @ centroid
                if float(np.mean(similarities >= 0.65)) >= 0.9 and float(np.median(similarities)) >= 0.75:
                    centers = centroid[None, :]
                    labels = np.zeros(len(vectors), dtype=int)
                    report.update(status="limited", supported_profiles=1)
                    report["warnings"].append("One common voice profile is supported; additional voices may be silent or indistinguishable. Ambiguous speech remains unassigned.")
    references = []
    if centers is not None:
        # Stable names use first confident appearance, not random k-means IDs.
        scores = matrix @ centers.T
        assigned = np.argmax(scores, axis=1)
        margins = (np.sort(scores, axis=1)[:, -1] - np.sort(scores, axis=1)[:, -2]
                   if len(centers) > 1 else np.ones(len(matrix)))
        names = {}
        for window, best, similarity, margin in zip(valid_windows, assigned, scores.max(axis=1), margins):
            minimum = MIN_ASSIGNMENT_SIMILARITY if len(centers) > 1 else 0.65
            confident = similarity >= minimum and margin >= MIN_ASSIGNMENT_MARGIN
            if confident:
                names.setdefault(int(best), f"speaker_{len(names) + 1:02d}")
            references.append({**window, "speaker": names.get(int(best), UNKNOWN) if confident else UNKNOWN})
    by_interval = {}
    for reference in references:
        by_interval.setdefault(reference["interval"], []).append(reference)
    result = []
    for index, row in enumerate(intervals):
        row = {**row, "source_speakers": list(row["speakers"])}
        own = sorted(by_interval.get(index, []), key=lambda value: value["start"])
        position = row["start"]
        for reference in own:
            if reference["start"] < position:
                raise ValueError("Reference windows overlap")
            if reference["start"] > position:
                result.append({**row, "start": position, "end": reference["start"], "speakers": [UNKNOWN], "speaker_assignment": "uncertain"})
            result.append({**row, "start": reference["start"], "end": reference["end"],
                           "speakers": [reference["speaker"]], "speaker_assignment": "reference" if reference["speaker"] != UNKNOWN else "uncertain"})
            position = reference["end"]
        if position < row["end"]:
            speaker, assignment = UNKNOWN, "uncertain"
            # Never use global native-cluster purity: long native clusters can
            # contain minority voices. Require close references on BOTH sides,
            # from the same native single-speaker identity, with matching labels.
            if not own and len(row["speakers"]) == 1 and row["end"] - row["start"] < MIN_REFERENCE_SECONDS:
                left = by_interval.get(index - 1, [])
                right = by_interval.get(index + 1, [])
                if left and right:
                    before, after = left[-1], right[0]
                    if (intervals[index - 1]["speakers"] == row["speakers"] == intervals[index + 1]["speakers"]
                            and 0 <= row["start"] - before["end"] <= 0.4
                            and 0 <= after["start"] - row["end"] <= 0.4
                            and before["speaker"] == after["speaker"] != UNKNOWN):
                        speaker, assignment = before["speaker"], "temporal_agreement"
            result.append({**row, "start": position, "speakers": [speaker], "speaker_assignment": assignment})
    # Coalesce only touching ranges; no previously silent audio is introduced.
    merged = []
    for row in result:
        if (merged and merged[-1]["end"] == row["start"] and merged[-1]["speakers"] == row["speakers"]
                and merged[-1]["source_speakers"] == row["source_speakers"]
                and merged[-1]["speaker_assignment"] == row["speaker_assignment"]):
            merged[-1]["end"] = row["end"]
        else:
            merged.append(dict(row))
    report["unknown_seconds"] = sum(row["end"] - row["start"] for row in merged if row["speakers"] == [UNKNOWN])
    report["speech_seconds"] = sum(row["end"] - row["start"] for row in intervals)
    return merged, report


def refine_known_speakers(audio, intervals, count, model_path, callback=None):
    """Extract references with the installed sherpa model; no audio/file writes."""
    if not isinstance(count, int) or not 0 <= count <= 16:
        raise ValueError("Expected speaker count must be between 0 and 16")
    if count in (0, 1):
        return refine_from_embeddings(intervals, [], [], count)
    import numpy as np
    import sherpa_onnx
    windows = reference_windows(intervals)
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path), num_threads=2)
    if not config.validate():
        raise RuntimeError("Invalid meeting speaker embedding model")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    embeddings = []
    for index, window in enumerate(windows):
        stream = extractor.create_stream()
        stream.accept_waveform(16000, audio[int(window["start"] * 16000):int(window["end"] * 16000)])
        stream.input_finished()
        embeddings.append(extractor.compute(stream) if extractor.is_ready(stream) else np.asarray([]))
        if callback and (index % 100 == 0 or index + 1 == len(windows)):
            callback(f"Checking speaker identities: {index + 1}/{len(windows)}")
    return refine_from_embeddings(intervals, windows, embeddings, count)
