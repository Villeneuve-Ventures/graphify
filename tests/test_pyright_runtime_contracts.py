"""Behavior at runtime boundaries clarified while enabling the typecheck gate."""

from collections import Counter
import sys

import pytest

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


def test_empty_live_ollama_url_does_not_reuse_cached_loopback(tmp_path, monkeypatch, capsys):
    from graphify import cli, llm

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "source.txt").write_text("A document that needs semantic extraction.")
    monkeypatch.setitem(llm.BACKENDS["ollama"], "base_url", "http://localhost:11434/v1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    monkeypatch.setattr(llm, "_get_backend_api_key", lambda _backend: None)
    monkeypatch.setattr(sys, "argv", [
        "graphify", "extract", str(corpus), "--backend", "ollama",
        "--out", str(tmp_path / "output"), "--no-cluster",
    ])

    def unexpected_provider(*args, **kwargs):
        raise AssertionError("An empty live URL must not authorize provider extraction")

    monkeypatch.setattr(llm, "extract_corpus_parallel", unexpected_provider)
    with pytest.raises(SystemExit) as exit_info:
        cli._dispatch_extract()
    assert exit_info.value.code == 1
    assert "backend 'ollama' requires OLLAMA_API_KEY" in capsys.readouterr().err
