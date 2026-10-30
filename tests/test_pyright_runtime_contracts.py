"""Behavior at runtime boundaries clarified while enabling the typecheck gate."""

from collections import Counter

from graphify.callflow_html import node_degree_scores
from graphify.file_slice import FileSlice, expand_oversized_files


def test_expansion_preserves_an_existing_slice_without_reading_parent(tmp_path):
    unit = FileSlice(tmp_path / "absent.md", 10, 20, 1, 2)
    assert expand_oversized_files([unit], max_chars=5) == [unit]
    assert expand_oversized_files([unit], max_chars=5)[0] is unit


def test_fractional_degree_scores_keep_counter_behavior():
    scores = node_degree_scores([
        {"source": "a", "target": "b", "relation": "calls", "confidence": "INFERRED",
         "confidence_score": 0.9},
    ])
    assert isinstance(scores, Counter)
    assert scores["a"] == scores["b"] > 0
    assert scores["missing"] == 0
    assert set(dict(scores.most_common())) == {"a", "b"}
