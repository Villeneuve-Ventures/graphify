"""Streaming schema detection validates JSON without retaining graph payloads."""
import json
import random
import sys
import tracemalloc
from typing import Any

import pytest

from graphify._graph_marker import portable_schema_marker


def chunks(payload, size):
    return (payload[start:start + size] for start in range(0, len(payload), size))


@pytest.mark.parametrize("size", [1, 2, 7, 65536])
@pytest.mark.parametrize("payload,expected", [
    (b'{"graph":{"_graphify_protocol":{"schema":2}}}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":2.0}}}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":2e0}}}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":2.000e+00}}}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":"2"}}}', False),
    (b'{"graph":{"_graphify_protocol":{"schema":true}}}', False),
    (b'{"graph":{"_graphify_protocol":{"schema":null}}}', False),
    (b'{"graph":{"_graphify_protocol":{"schema":NaN}}}', False),
    (b'{"graph":{"_graphify_protocol":{"schema":Infinity}}}', False),
    (b'{"graph":{"_graphify_protocol":{"schema":-Infinity}}}', False),
    (b'{"graph":{"_graphify_protocol":{"schema":2,"schema":1}}}', False),
    (b'{"graph":{"_graphify_protocol":{"schema":1,"schema":2}}}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":2},"_graphify_protocol":{}}}', False),
    (b'{"graph":{"_graphify_protocol":{},"_graphify_protocol":{"schema":2}}}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":2}},"graph":{}}', False),
    (b'{"graph":{},"graph":{"_graphify_protocol":{"schema":2}}}', True),
    (b'{"gr\\u0061ph":{"_graphify_\\u0070rotocol":{"sch\\u0065ma":2}}}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":2}},"note":"caf\xc3\xa9 \xf0\x9f\x90\x88"}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":2}},"note":"\\ud800\\udc00 \\ud800"}', True),
    (b'{"nodes":[{"graph":{"_graphify_protocol":{"schema":2}}}]}', False),
    (b'{"note":"\\\"graph\\\":{\\\"_graphify_protocol\\\":{\\\"schema\\\":2}}"}', False),
    (b'{"graph":{"_graphify_protocol":{"schema":2}},"graphical":null}', True),
    (b'{"graph":{"_graphify_protocol":{"schema":2}},"nodes":[[],{},null,true,false,0,-2,1.5e-2]}', True),
    (b' \r\n\t{"graph":{"_graphify_protocol":{"schema":2}}} \r\n\t', True),
    (b'{}', False),
])
def test_marker_matches_decoded_object_semantics(payload, expected, size):
    decoded = json.loads(payload.decode("utf-8"))
    assert (decoded.get("graph", {}).get("_graphify_protocol", {}).get("schema") == 2) is expected
    assert portable_schema_marker(chunks(payload, size), token_limit=64) is expected


@pytest.mark.parametrize("parent", ["graph", "_graphify_protocol", "schema"])
@pytest.mark.parametrize("replacement", [b"null", b"[]", b"true", b'"text"', b"0"])
def test_relevant_non_object_replacement_resets_marker(parent, replacement):
    schema = b'{"schema":2,"schema":' + replacement + b'}'
    protocol = b'{"_graphify_protocol":{"schema":2},"_graphify_protocol":' + replacement + b'}'
    graph = b'{"graph":{"_graphify_protocol":{"schema":2}},"graph":' + replacement + b'}'
    payload = {"graph": graph, "_graphify_protocol": b'{"graph":' + protocol + b'}',
               "schema": b'{"graph":{"_graphify_protocol":' + schema + b'}}'}[parent]
    assert portable_schema_marker(chunks(payload, 3), token_limit=64) is False


@pytest.mark.parametrize("payload", [b"[]", b"null", b"2", b'"text"', b"true"])
def test_non_object_root_has_no_marker(payload):
    assert portable_schema_marker([payload], token_limit=16) is False


@pytest.mark.parametrize("size", [1, 7, 65536])
@pytest.mark.parametrize("payload", [
    b"", b" ", b"{", b"[", b'{"graph":', b'{"graph":{}}trailing',
    b'{"graph":{"_graphify_protocol":{"schema":2}}} {}',
    b'{"graph":{"_graphify_protocol":{"schema":2}},}',
    b'{"graph":{"_graphify_protocol":{"schema":2}},"nodes":[1,]}',
    b'{"graph":{"_graphify_protocol":{"schema":2}},"note":"unterminated}',
    b'{"note":"\\x41"}', b'{"note":"\\u123"}', b'{"note":"\\u12xz"}',
    b'{"note":"line\nfeed"}', b'{"note":"\xff"}', b'{"note":"\xf0\x9f"}',
    b'{"note":"\xc0\xaf"}', b'{"note":"\xed\xa0\x80"}',
    b'{"schema":01}', b'{"schema":-01}', b'{"schema":1.}', b'{"schema":1e}',
    b'{"schema":1e+}', b'{"schema":+1}', b'{"schema":.1}', b'{"schema":nan}',
    b'{"schema":Inf}', b'{"schema":False}', b'{"schema" 2}', b'{2:3}',
    b'{"nodes":[1 2]}', b'{"nodes":[truefalse]}', b'\xef\xbb\xbf{}',
])
def test_invalid_or_truncated_json_raises_value_error(payload, size):
    with pytest.raises(ValueError):
        portable_schema_marker(chunks(payload, size), token_limit=64)


def test_consumes_eof_after_finding_a_marker():
    consumed = []

    def stream():
        for payload in [b'{"graph":{"_graphify_protocol":{"schema":2}}}', b" ", b"\n"]:
            consumed.append(payload)
            yield payload

    assert portable_schema_marker(stream(), token_limit=16) is True
    assert len(consumed) == 3


def test_empty_chunks_and_split_unicode_escapes():
    payload = b'{"gr\\u0061ph":{"_graphify_protocol":{"schema":2}},"note":"caf\xc3\xa9"}'
    stream = (part for byte in payload for part in (b"", bytes([byte]), b""))
    assert portable_schema_marker(stream, token_limit=1) is True


@pytest.mark.parametrize("size", [1, 7, 65536])
def test_relevant_numeric_tokens_have_an_exact_resource_bound(size):
    payload = b'{"graph":{"_graphify_protocol":{"schema":2.000e+00}}}'
    assert portable_schema_marker(chunks(payload, size), token_limit=9) is True
    with pytest.raises(RuntimeError, match="token"):
        portable_schema_marker(chunks(payload, size), token_limit=8)


def test_large_relevant_number_refuses_without_large_retained_storage():
    stream = iter([b'{"graph":{"_graphify_protocol":{"schema":', b"2" + b"0" * 65536, b'}}}'])
    with pytest.raises(RuntimeError, match="token"):
        portable_schema_marker(stream, token_limit=32)


def test_relevant_integer_runtime_bound_is_a_resource_refusal():
    limit = sys.get_int_max_str_digits()
    if not limit:
        pytest.skip("integer conversion runtime limit is disabled")
    payload = b'{"graph":{"_graphify_protocol":{"schema":' + b"2" + b"0" * limit + b'}}}'
    with pytest.raises(RuntimeError, match="runtime limits"):
        portable_schema_marker(chunks(payload, 7), token_limit=limit + 64)


@pytest.mark.parametrize("kind", ["string", "key", "escaped-key"])
def test_skipped_large_strings_and_keys_do_not_use_token_budget(kind):
    prefix = b'{"nodes":["' if kind == "string" else b'{"'
    suffix = b'"],"graph":{"_graphify_protocol":{"schema":2}}}' if kind == "string" else b'":0,"graph":{"_graphify_protocol":{"schema":2}}}'
    unit = b"x" * 65536 if kind != "escaped-key" else b"\\u0078" * 1024
    stream = iter([prefix, *([unit] * 32), suffix])
    assert portable_schema_marker(stream, token_limit=1) is True


def test_target_key_prefix_with_extra_decoded_data_cannot_match():
    payload = b'{"graph":{"_graphify_protocol":{"schema":2}},"graph\\u0078":null}'
    assert portable_schema_marker(chunks(payload, 1), token_limit=1) is True
    payload = b'{"graph\\u0078":{"_graphify_protocol":{"schema":2}}}'
    assert portable_schema_marker(chunks(payload, 1), token_limit=1) is False


@pytest.mark.parametrize("container", [b"[", b'{"x":'])
def test_nesting_has_a_distinct_resource_failure(container):
    close = b"]" if container == b"[" else b"}"
    assert portable_schema_marker([container * 1024 + b"0" + close * 1024], token_limit=1) is False
    with pytest.raises(RuntimeError, match="depth"):
        portable_schema_marker([container * 1025 + b"0" + close * 1025], token_limit=1)


def test_large_skipped_graph_string_has_bounded_parser_memory():
    unit = b"x" * 65536
    stream = iter([b'{"nodes":["', *([unit] * 512), b'"],"graph":{"_graphify_protocol":{"schema":2}}}'])
    tracemalloc.start()
    try:
        assert portable_schema_marker(stream, token_limit=1) is True
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


def test_matches_stdlib_on_deterministic_mixed_documents():
    rng = random.Random(201)
    keys = ["graph", "_graphify_protocol", "schema", "nodes", "note", "café", "猫"]

    def value(depth: int) -> Any:
        if depth > 3 or rng.randrange(3) == 0:
            return rng.choice([None, True, False, 0, 2, 2.0, 1.25, "2", "猫\\\"\n", ""])
        if rng.randrange(2):
            return {rng.choice(keys): value(depth + 1) for _ in range(rng.randrange(5))}
        return [value(depth + 1) for _ in range(rng.randrange(5))]

    for _ in range(250):
        document = value(0)
        if not isinstance(document, dict):
            document = {"nodes": document}
        if rng.randrange(2):
            document["graph"] = {"_graphify_protocol": {"schema": rng.choice([2, 2.0, "2", None])}}
        payload = json.dumps(document, ensure_ascii=rng.choice([True, False])).encode("utf-8")
        graph = document.get("graph")
        marker = graph.get("_graphify_protocol") if isinstance(graph, dict) else None
        expected = isinstance(marker, dict) and marker.get("schema") == 2
        for size in (1, 11, 65536):
            assert portable_schema_marker(chunks(payload, size), token_limit=64) is expected
