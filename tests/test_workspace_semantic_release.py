from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from graphify.workspace.contracts import canonical_json_bytes
import graphify.workspace.semantic_release as semantic_release
from graphify.workspace.semantic_release import (
    BUNDLE_ARTIFACT_MAX_BYTES,
    BUNDLE_MANIFEST_MAX_BYTES,
    CORE_SECRETS_PROFILE,
    FIELD_MAX_BYTES,
    MAX_CATEGORIES,
    MAX_PROFILES,
    MAX_RULES,
    PROVIDER_CREDENTIALS_PROFILE,
    ProfileCoordinate,
    SemanticReleaseBundleError,
    classify_canonical_bytes,
    load_installed_semantic_release_bundle,
)


PACKAGE_ROOT = Path(semantic_release.__file__).parent.parent
MANIFEST_RELATIVE = Path("workspace/semantic_release_manifest.json")
DATA_RELATIVE = Path("workspace/semantic_release_data")
REALISTIC_RSA_PRIVATE_KEY_WITH_SHORT_FINAL_LINE = (
    b"-----BEGIN PRIVATE KEY-----\n"
    b"MIICdwIBADANBgkqhkiG9w0BAQEFAASCAmEwggJdAgEAAoGBAOAzUw256nvNMJYL\n"
    b"Q575z7b9PsJpvVGGmtFnNvZtw0mFdUkfs1A+3RfZFsMbmww/u2+iAMUl+yM43Uk4\n"
    b"SD0eHJX2PZTvbwmBH3Eq6OQGG9EJmjhspH4B6CUG6InbezC4+/Q7Q4STcL40SP6/\n"
    b"kbYn3mJlmDtN1n/sQf7ZmNDOPUPPAgMBAAECgYEA1Ub90yjxLyRa++FrSmhKeMEg\n"
    b"WsFMH6n0zQ9q8bIo/F/A2vcVFVk36d/SD3jLXjOikueB5AnlhfQqTeUEk195wEbR\n"
    b"y58e8Jrb0UaKj9izA3yPa0weuyi/4WnpdtS7XeUfjuG7IA6Q/V5x1Gbb0ZTyYM18\n"
    b"zfJeh1X+vnLGZlU2poECQQDyB39sLhi7rKEC/5VyFOxh+yys0j4RYbeMcpujYNJs\n"
    b"99j6Yu8XlUDSNAOjK/ZzPxRrZvT5tkIeEgszktzpfMr5AkEA7SReLFJzYB72g9e9\n"
    b"I06N/4x8edesR+0sbZJXvaETTGNKDzMpVa5vr9/AkcUDE+djAHoF+pdhoE3C5JUk\n"
    b"KxAvBwJAHGRUxlQCAsIVgUyKM3/Q2w2kCAIB1fgomAk5yMiq5q2MfpLsiU+w8ve3\n"
    b"FYUqvApCUvcY9dIzn2NufPZVg+5nwQJBALiNNAj0RbwJfLnQXO6sRNAbSggcs4Pq\n"
    b"bUf8uvHl+Dnbj5hSrZlzvpG15YzMMP/9dEu7qwmBZEW4HrN76gDlgGMCQG1M9vi5\n"
    b"FnE3CGf1muz5to0vddP7OS/7NZfjyMXAPw4c+jPlURk0a/e6tBNCsfRVl8V0ow+t\n"
    b"pIlQI18OlGV4aHI=\n"
    b"-----END PRIVATE KEY-----"
)
STANDARD_OPENSSH_PRIVATE_KEY_WITH_70_COLUMN_WRAPPING = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    + (b"A" * 70 + b"\n") * 5
    + b"AAAAAA\n"
    + b"-----END OPENSSH PRIVATE KEY-----"
)
ED25519_PKCS8_PRIVATE_KEY_WITH_SINGLE_64_COLUMN = (
    b"-----BEGIN PRIVATE KEY-----\n"
    b"MC4CAQAwBQYDK2VwBCIEIBERERERERERERERERERERERERERERERERERERERERER\n"
    b"-----END PRIVATE KEY-----"
)


def _canonical_load(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert raw == canonical_json_bytes(parsed)
    return parsed


def _copy_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "graphify"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    shutil.copy2(PACKAGE_ROOT / "workspace" / "semantic_release.py", workspace)
    shutil.copy2(PACKAGE_ROOT / MANIFEST_RELATIVE, workspace)
    shutil.copytree(PACKAGE_ROOT / DATA_RELATIVE, workspace / DATA_RELATIVE.name)
    return root


def _select_bundle(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(
        semantic_release,
        "__file__",
        str(root / "workspace" / "semantic_release.py"),
    )


def _manifest_path(root: Path) -> Path:
    return root / MANIFEST_RELATIVE


def _manifest(root: Path) -> dict[str, object]:
    return _canonical_load(_manifest_path(root))


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    path = _manifest_path(root)
    path.write_bytes(canonical_json_bytes(manifest))
    path.chmod(0o644)


def _entries(manifest: dict[str, object]) -> list[dict[str, object]]:
    entries = manifest["artifacts"]
    assert isinstance(entries, list)
    return entries  # type: ignore[return-value]


def _entry(manifest: dict[str, object], *, kind: str) -> dict[str, object]:
    matches = [entry for entry in _entries(manifest) if entry["artifact_kind"] == kind]
    assert len(matches) == 1
    return matches[0]


def _profile_entry(manifest: dict[str, object], profile_id: str) -> dict[str, object]:
    matches = [
        entry
        for entry in _entries(manifest)
        if entry["artifact_kind"] == "profile" and entry["profile_id"] == profile_id
    ]
    assert len(matches) == 1
    return matches[0]


def _artifact_path(root: Path, entry: dict[str, object]) -> Path:
    return root / str(entry["path"])


def _refresh_entry(root: Path, entry: dict[str, object]) -> None:
    path = _artifact_path(root, entry)
    raw = path.read_bytes()
    entry["byte_count"] = len(raw)
    entry["mode"] = f"{stat.S_IMODE(path.stat().st_mode):04o}"
    entry["sha256"] = hashlib.sha256(raw).hexdigest()


def _write_artifact_json(root: Path, entry: dict[str, object], value: object) -> None:
    path = _artifact_path(root, entry)
    path.write_bytes(canonical_json_bytes(value))
    path.chmod(0o644)
    _refresh_entry(root, entry)


def _load_copied_bundle(monkeypatch: pytest.MonkeyPatch, root: Path):
    _select_bundle(monkeypatch, root)
    return load_installed_semantic_release_bundle()


def test_installed_bundle_manifest_and_inventory_are_exact() -> None:
    bundle = load_installed_semantic_release_bundle()
    manifest = _canonical_load(PACKAGE_ROOT / MANIFEST_RELATIVE)

    assert inspect.signature(load_installed_semantic_release_bundle).parameters == {}
    assert set(manifest) == {
        "artifacts",
        "compatibility_version",
        "contract",
        "format_version",
        "manifest_id",
    }
    assert manifest["contract"] == "graphify.workspace.semantic_release_manifest.internal"
    assert manifest["format_version"] == 1
    assert manifest["compatibility_version"] == 1
    assert manifest["manifest_id"] == "graphify.semantic_release_bundle.v1"
    assert bundle.manifest_bytes == canonical_json_bytes(manifest)
    assert bundle.manifest_sha256 == hashlib.sha256(bundle.manifest_bytes).hexdigest()

    artifacts = bundle.artifacts
    assert [artifact.path for artifact in artifacts] == sorted(
        (artifact.path for artifact in artifacts), key=lambda value: value.encode("utf-8")
    )
    assert [artifact.artifact_kind for artifact in artifacts].count("classifier") == 1
    assert [artifact.artifact_kind for artifact in artifacts].count("classifier_abi") == 1
    assert [artifact.artifact_kind for artifact in artifacts].count("taxonomy") == 1
    assert [artifact.artifact_kind for artifact in artifacts].count("normalization") == 1
    assert [artifact.artifact_kind for artifact in artifacts].count("ruleset") == 1
    assert [artifact.artifact_kind for artifact in artifacts].count("profile") == 2
    assert {profile.coordinate for profile in bundle.profiles} == {
        CORE_SECRETS_PROFILE,
        PROVIDER_CREDENTIALS_PROFILE,
    }

    inventoried_paths = {artifact.path for artifact in artifacts}
    data_paths = {
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in (PACKAGE_ROOT / DATA_RELATIVE).rglob("*")
        if path.is_file()
    }
    assert inventoried_paths == {
        "workspace/semantic_release.py",
        *data_paths,
    }
    assert sum(artifact.byte_count for artifact in artifacts) <= BUNDLE_ARTIFACT_MAX_BYTES
    for artifact in artifacts:
        path = PACKAGE_ROOT / artifact.path
        raw = path.read_bytes()
        assert artifact.byte_count == len(raw) > 0
        assert artifact.sha256 == hashlib.sha256(raw).hexdigest()
        assert artifact.mode == f"{stat.S_IMODE(path.stat().st_mode):04o}" == "0644"


def test_missing_parsed_profile_document_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = semantic_release._canonical_json_document

    def missing_profile(raw: bytes, label: str) -> dict[str, object] | None:
        if label.endswith("profiles/provider_credentials.v1.json"):
            return None
        return original(raw, label)

    monkeypatch.setattr(semantic_release, "_canonical_json_document", missing_profile)

    with pytest.raises(SemanticReleaseBundleError, match="profile document is unavailable"):
        load_installed_semantic_release_bundle()


@pytest.mark.parametrize(
    ("field", "category"),
    [
        (
            b"-----BEGIN PRIVATE KEY-----\n"
            b"QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB\n"
            b"QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJC\n"
            b"-----END PRIVATE KEY-----",
            "secret.private_key_material",
        ),
        (REALISTIC_RSA_PRIVATE_KEY_WITH_SHORT_FINAL_LINE, "secret.private_key_material"),
        (
            ED25519_PKCS8_PRIVATE_KEY_WITH_SINGLE_64_COLUMN,
            "secret.private_key_material",
        ),
        (
            STANDARD_OPENSSH_PRIVATE_KEY_WITH_70_COLUMN_WRAPPING,
            "secret.private_key_material",
        ),
        (b"postgresql://service:swordfish@example.test/db", "secret.credential_uri"),
        (b"postgresql://service:x@example.test/db", "secret.credential_uri"),
        (b"postgresql://service:changeme@example.test/db", "secret.credential_uri"),
        (
            b"Authorization: Basic dXNlcjpzdXBlcnNlY3JldA==",
            "secret.authorization_credential",
        ),
        (b"Authorization: Basic dTpw", "secret.authorization_credential"),
        (
            b"authorization: bearer abcdefghijklmnopqrstuvwxyz.0123456789",
            "secret.authorization_credential",
        ),
        (b"Authorization: Bearer abc123", "secret.authorization_credential"),
        (b"client_secret = production-secret-42", "secret.credential_assignment"),
        (b"token: 'production-token-42'", "secret.credential_assignment"),
        (b"ghp_abcdefghijklmnopqrstuvwxyz0123456789", "secret.provider_credential"),
        (b"AKIAABCDEFGHIJKLMNOP", "secret.provider_credential"),
        (b"sk_" + b"live_" + b"abcdefghijklmnop12345678", "secret.provider_credential"),
        (b"postgresql://service:secret@[2001:db8::1]/db", "secret.credential_uri"),
        (
            b"seed phrase: alpha bravo cedar delta ember frost giant harbor "
            b"island jungle kernel lunar",
            "secret.seed_or_recovery_material",
        ),
    ],
)
def test_core_profile_matches_only_complete_explicit_credential_formats(
    field: bytes,
    category: str,
) -> None:
    result = classify_canonical_bytes(field, (CORE_SECRETS_PROFILE,))
    assert result.outcome == "MATCH"
    assert category in result.category_ids
    assert tuple(result.category_ids) == tuple(
        sorted(result.category_ids, key=lambda value: value.encode("utf-8"))
    )
    assert tuple(result.rule_ids) == tuple(
        sorted(result.rule_ids, key=lambda value: value.encode("utf-8"))
    )


@pytest.mark.parametrize(
    "field",
    [
        b"550e8400-e29b-41d4-a716-446655440000",
        b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        b"dXNlcjpzdXBlcnNlY3JldA==",
        b"this paragraph contains something secret-looking",
        b"api_key = your_api_key",
        b"password: placeholder",
        b"Authorization: Basic abc",
        b"Authorization: Basic dTpw=",
        b"Authorization: Bearer abc=123",
        b"postgresql://service:secret@[2001:db8::1/db",
        b"postgresql://service:secret@[]/db",
        b"-----BEGIN PRIVATE KEY----- incomplete",
        (
            b"-----BEGIN PRIVATE KEY-----\n"
            b"QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB\n"
            b"-----END RSA PRIVATE KEY-----"
        ),
        (
            b"-----BEGIN PRIVATE KEY-----\n"
            b"QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB\n\n"
            b"-----END PRIVATE KEY-----"
        ),
        (
            b"-----BEGIN PRIVATE KEY-----\n"
            b"not-valid-base64-material-not-valid\n"
            b"-----END PRIVATE KEY-----"
        ),
        (b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 64 + b"\nQU=J\n-----END PRIVATE KEY-----"),
        (b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 63 + b"\n-----END PRIVATE KEY-----"),
        (b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 68 + b"\nQUJDRA==\n-----END PRIVATE KEY-----"),
        (
            b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + b"A" * 69 + b"\nAAAAAAA=\n"
            b"-----END OPENSSH PRIVATE KEY-----"
        ),
        (
            b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + b"A" * 70 + b"\nAAAA=\n"
            b"-----END OPENSSH PRIVATE KEY-----"
        ),
        (
            b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + b"A" * 70 + b"\nAAAAAA\n"
            b"-----END PRIVATE KEY-----"
        ),
        b"seed words might appear in this ordinary sentence",
    ],
)
def test_core_profile_does_not_broaden_to_heuristics_or_vague_evidence(field: bytes) -> None:
    result = classify_canonical_bytes(field, (CORE_SECRETS_PROFILE,))
    assert result.outcome == "NO_MATCH"
    assert result.category_ids == ()
    assert result.rule_ids == ()


def test_examples_and_test_context_do_not_exempt_complete_credentials() -> None:
    field = b"test fixture token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    result = classify_canonical_bytes(field, (CORE_SECRETS_PROFILE,))
    assert result.outcome == "MATCH"
    assert result.category_ids == ("secret.provider_credential",)


def test_profiles_are_explicit_validated_input_and_never_ambient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    field = b"ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setenv("GRAPHIFY_SEMANTIC_RELEASE_PROFILE", "core_secrets.v1")
    monkeypatch.setenv("GRAPHIFY_SEMANTIC_RELEASE_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert classify_canonical_bytes(field, ()).outcome == "NO_MATCH"
    assert classify_canonical_bytes(field, (PROVIDER_CREDENTIALS_PROFILE,)).outcome == "MATCH"
    duplicate = (PROVIDER_CREDENTIALS_PROFILE, PROVIDER_CREDENTIALS_PROFILE)
    assert classify_canonical_bytes(field, duplicate).outcome == "INDETERMINATE"
    reversed_profiles = (PROVIDER_CREDENTIALS_PROFILE, CORE_SECRETS_PROFILE)
    assert classify_canonical_bytes(field, reversed_profiles).outcome == "INDETERMINATE"
    unknown = (ProfileCoordinate("unknown.v1", 1),)
    assert classify_canonical_bytes(field, unknown).outcome == "INDETERMINATE"
    bad_suffix = (ProfileCoordinate("core_secrets.v2", 1),)
    assert classify_canonical_bytes(field, bad_suffix).outcome == "INDETERMINATE"
    too_many = tuple(
        ProfileCoordinate(f"profile_{index}.v1", 1) for index in range(MAX_PROFILES + 1)
    )
    assert classify_canonical_bytes(field, too_many).outcome == "INDETERMINATE"


@pytest.mark.parametrize(
    "field",
    [
        b"",
        b" leading",
        b"trailing ",
        b"line\x00break",
        b"line\xc2\x85break",
        b"\xff",
        b"x" * (FIELD_MAX_BYTES + 1),
    ],
)
def test_invalid_or_out_of_contract_field_bytes_are_indeterminate(field: bytes) -> None:
    result = classify_canonical_bytes(field, (CORE_SECRETS_PROFILE,))
    assert result.outcome == "INDETERMINATE"
    assert result.category_ids == ()
    assert result.rule_ids == ()


def test_field_validation_does_not_consult_host_unicode_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_normalization(*_args: object) -> str:
        raise AssertionError("field validation consulted the host Unicode database")

    unicode_probe = type("UnicodeProbe", (), {"normalize": staticmethod(unexpected_normalization)})
    monkeypatch.setattr(semantic_release, "unicodedata", unicode_probe)
    field = b"ghp_abcdefghijklmnopqrstuvwxyz0123456789\ncafe\xcc\x81"
    assert semantic_release._valid_field_bytes(field) == field


def test_classification_is_byte_exact_and_repeated_process_deterministic(tmp_path: Path) -> None:
    field = b"Authorization: Basic dXNlcjpzdXBlcnNlY3JldA=="
    expected = classify_canonical_bytes(field, (CORE_SECRETS_PROFILE,)).to_dict()
    assert all(
        classify_canonical_bytes(field, (CORE_SECRETS_PROFILE,)).to_dict() == expected
        for _ in range(100)
    )

    script = (
        "import json\n"
        "from graphify.workspace.semantic_release import "
        "CORE_SECRETS_PROFILE, classify_canonical_bytes\n"
        f"value = {field!r}\n"
        "print(json.dumps(classify_canonical_bytes(value, "
        "(CORE_SECRETS_PROFILE,)).to_dict(), sort_keys=True))\n"
    )
    observed = []
    for _ in range(4):
        proc = subprocess.run(
            [sys.executable, "-P", "-c", script],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        observed.append(json.loads(proc.stdout))
    assert observed == [expected] * 4


@pytest.mark.parametrize(
    "mutation",
    [
        "malformed",
        "duplicate-key",
        "unknown-member",
        "unsupported-format",
        "unsupported-compatibility",
        "unsorted-inventory",
        "duplicate-path",
        "duplicate-coordinate",
        "unknown-kind",
        "bad-mode-token",
        "zero-byte-count",
        "uppercase-digest",
        "profile-suffix-mismatch",
        "missing-singleton",
        "duplicate-singleton",
        "classifier-path-substitution",
        "data-root-escape",
        "profile-root-escape",
        "missing-core-profile",
        "non-profile-coordinate-mismatch",
        "oversized-identifier",
    ],
)
def test_manifest_structure_order_coordinates_and_members_fail_closed(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    entries = _entries(manifest)
    if mutation == "malformed":
        _manifest_path(root).write_bytes(b"{")
    elif mutation == "duplicate-key":
        _manifest_path(root).write_bytes(
            b'{"artifacts":[],"artifacts":[],"compatibility_version":1,'
            b'"contract":"graphify.workspace.semantic_release_manifest.internal",'
            b'"format_version":1,"manifest_id":"graphify.semantic_release_bundle.v1"}\n'
        )
    elif mutation == "unknown-member":
        manifest["extra"] = True
        _write_manifest(root, manifest)
    elif mutation == "unsupported-format":
        manifest["format_version"] = 2
        _write_manifest(root, manifest)
    elif mutation == "unsupported-compatibility":
        manifest["compatibility_version"] = 2
        _write_manifest(root, manifest)
    elif mutation == "unsorted-inventory":
        entries[0], entries[1] = entries[1], entries[0]
        _write_manifest(root, manifest)
    elif mutation == "duplicate-path":
        entries[1]["path"] = entries[0]["path"]
        _write_manifest(root, manifest)
    elif mutation == "duplicate-coordinate":
        profiles = [entry for entry in entries if entry["artifact_kind"] == "profile"]
        profiles[1]["profile_id"] = profiles[0]["profile_id"]
        profiles[1]["profile_version"] = profiles[0]["profile_version"]
        _write_manifest(root, manifest)
    elif mutation == "unknown-kind":
        entries[0]["artifact_kind"] = "policy"
        _write_manifest(root, manifest)
    elif mutation == "bad-mode-token":
        entries[0]["mode"] = "0600"
        _write_manifest(root, manifest)
    elif mutation == "zero-byte-count":
        entries[0]["byte_count"] = 0
        _write_manifest(root, manifest)
    elif mutation == "uppercase-digest":
        entries[0]["sha256"] = str(entries[0]["sha256"]).upper()
        _write_manifest(root, manifest)
    elif mutation == "profile-suffix-mismatch":
        profile = _profile_entry(manifest, "core_secrets.v1")
        profile["profile_version"] = 2
        _write_manifest(root, manifest)
    elif mutation == "missing-singleton":
        entries.remove(_entry(manifest, kind="classifier_abi"))
        _write_manifest(root, manifest)
    elif mutation == "duplicate-singleton":
        duplicate = dict(_entry(manifest, kind="classifier_abi"))
        duplicate["path"] = "workspace/semantic_release_data/classifier_abi_extra.v1.json"
        duplicate["abi_id"] = "graphify.semantic_release.byte_abi_extra.v1"
        entries.append(duplicate)
        entries.sort(key=lambda entry: str(entry["path"]).encode("utf-8"))
        _write_manifest(root, manifest)
    elif mutation == "classifier-path-substitution":
        _entry(manifest, kind="classifier")["path"] = (
            "workspace/semantic_release_data/classifier.py"
        )
        _write_manifest(root, manifest)
    elif mutation == "data-root-escape":
        _entry(manifest, kind="normalization")["path"] = "workspace/normalization.v1.json"
        entries.sort(key=lambda entry: str(entry["path"]).encode("utf-8"))
        _write_manifest(root, manifest)
    elif mutation == "profile-root-escape":
        profile = _profile_entry(manifest, "core_secrets.v1")
        profile["path"] = "workspace/semantic_release_data/core_secrets.v1.json"
        entries.sort(key=lambda entry: str(entry["path"]).encode("utf-8"))
        _write_manifest(root, manifest)
    elif mutation == "missing-core-profile":
        entries.remove(_profile_entry(manifest, "core_secrets.v1"))
        _write_manifest(root, manifest)
    elif mutation == "non-profile-coordinate-mismatch":
        _entry(manifest, kind="classifier_abi")["abi_id"] = (
            "graphify.semantic_release.byte_abi_other.v1"
        )
        _write_manifest(root, manifest)
    elif mutation == "oversized-identifier":
        _entry(manifest, kind="ruleset")["ruleset_id"] = "x" * 257
        _write_manifest(root, manifest)

    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError):
        load_installed_semantic_release_bundle()
    assert classify_canonical_bytes(b"ordinary", (CORE_SECRETS_PROFILE,)).outcome == (
        "INDETERMINATE"
    )


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        (None, "format_version"),
        (None, "compatibility_version"),
        ("classifier_abi", "format_version"),
        ("classifier_abi", "abi_version"),
        ("normalization", "format_version"),
        ("normalization", "normalization_version"),
        ("taxonomy", "format_version"),
        ("taxonomy", "taxonomy_version"),
        ("ruleset", "format_version"),
        ("ruleset", "ruleset_version"),
        ("ruleset", "taxonomy_version"),
        ("profile", "format_version"),
        ("profile", "profile_version"),
        ("profile", "taxonomy_version"),
    ],
)
def test_boolean_versions_fail_closed(
    kind: str | None,
    field: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    if kind is None:
        manifest[field] = True
    else:
        entry = (
            _profile_entry(manifest, "core_secrets.v1")
            if kind == "profile"
            else _entry(manifest, kind=kind)
        )
        document = _canonical_load(_artifact_path(root, entry))
        document[field] = True
        _write_artifact_json(root, entry, document)
    _write_manifest(root, manifest)

    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="positive integer"):
        load_installed_semantic_release_bundle()
    assert classify_canonical_bytes(b"ordinary", (CORE_SECRETS_PROFILE,)).outcome == (
        "INDETERMINATE"
    )


@pytest.mark.parametrize(
    ("location", "field", "expected"),
    [
        ("manifest", "artifact_kind", "unknown artifact kind"),
        ("manifest", "mode", "unsupported mode"),
        ("ruleset", "credential_group", "credential group is invalid"),
    ],
)
def test_unhashable_closed_vocabulary_values_fail_closed(
    location: str,
    field: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    if location == "manifest":
        _entries(manifest)[0][field] = []
    else:
        entry = _entry(manifest, kind="ruleset")
        document = _canonical_load(_artifact_path(root, entry))
        rules = document["rules"]
        assert isinstance(rules, list)
        rule = rules[0]
        assert isinstance(rule, dict)
        rule[field] = []
        _write_artifact_json(root, entry, document)
    _write_manifest(root, manifest)

    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match=expected):
        load_installed_semantic_release_bundle()
    assert classify_canonical_bytes(b"ordinary", (CORE_SECRETS_PROFILE,)).outcome == (
        "INDETERMINATE"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.json",
        "",
        ".",
        "workspace/../semantic_release.py",
        "workspace//semantic_release.py",
        "workspace\\semantic_release.py",
        "workspace/semantic_release.py/",
        "workspace/semantic_release.py\x00suffix",
        "workspace/./semantic_release.py",
    ],
)
def test_manifest_paths_reject_every_noncanonical_form(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    _entries(manifest)[0]["path"] = path
    _write_manifest(root, manifest)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError):
        load_installed_semantic_release_bundle()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "file-symlink",
        "directory-symlink",
        "hard-link",
        "mode-drift",
        "size-drift",
        "digest-drift",
        "unlisted-artifact",
    ],
)
def test_descriptor_relative_file_identity_and_inventory_fail_closed(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    target_entry = _entry(manifest, kind="taxonomy")
    target = _artifact_path(root, target_entry)
    if mutation == "missing":
        target.unlink()
    elif mutation == "file-symlink":
        replacement = tmp_path / "replacement.json"
        replacement.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(replacement)
    elif mutation == "directory-symlink":
        profile_entry = _profile_entry(manifest, "core_secrets.v1")
        profile = _artifact_path(root, profile_entry)
        replacement_dir = tmp_path / "profiles"
        shutil.copytree(profile.parent, replacement_dir)
        shutil.rmtree(profile.parent)
        profile.parent.symlink_to(replacement_dir, target_is_directory=True)
    elif mutation == "hard-link":
        replacement = tmp_path / "linked.json"
        replacement.write_bytes(target.read_bytes())
        target.unlink()
        os.link(replacement, target)
    elif mutation == "mode-drift":
        target.chmod(0o600)
    elif mutation == "size-drift":
        target.write_bytes(target.read_bytes() + b" ")
    elif mutation == "digest-drift":
        raw = bytearray(target.read_bytes())
        raw[-2] = ord(" ") if raw[-2] != ord(" ") else ord("x")
        target.write_bytes(bytes(raw))
    elif mutation == "unlisted-artifact":
        extra = root / DATA_RELATIVE / "foreign.json"
        extra.write_bytes(b"{}\n")
        extra.chmod(0o644)

    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError):
        load_installed_semantic_release_bundle()


def test_data_inventory_depth_limit_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    nested = root / DATA_RELATIVE / "hostile"
    nested.mkdir()
    for _ in range(semantic_release._MAX_DIRECTORY_DEPTH + 1):
        nested /= "d"
        nested.mkdir()

    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="inventory exceeds depth limit"):
        load_installed_semantic_release_bundle()
    assert classify_canonical_bytes(b"ordinary", (CORE_SECRETS_PROFILE,)).outcome == (
        "INDETERMINATE"
    )


def test_data_inventory_limit_is_enforced_before_sorting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_details = (PACKAGE_ROOT / DATA_RELATIVE).stat()
    data_identity = (data_details.st_dev, data_details.st_ino)
    original_scandir = semantic_release.os.scandir

    class OverflowingEntries:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self):
            for index in range(semantic_release._MAX_DIRECTORY_ENTRIES + 1):
                yield type("Entry", (), {"name": f"entry-{index:05d}"})()
            raise AssertionError("inventory iterator was consumed past the hard limit")

    def bounded_scandir(path: int | str | bytes | os.PathLike[str]):
        if isinstance(path, int):
            details = os.fstat(path)
            if (details.st_dev, details.st_ino) == data_identity:
                return OverflowingEntries()
        return original_scandir(path)

    monkeypatch.setattr(semantic_release.os, "scandir", bounded_scandir)
    with pytest.raises(SemanticReleaseBundleError, match="inventory exceeds limit"):
        semantic_release._scan_data_inventory(PACKAGE_ROOT)


def test_excessive_json_nesting_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    entry = _entry(manifest, kind="taxonomy")
    depth = sys.getrecursionlimit() + 50
    path = _artifact_path(root, entry)
    path.write_bytes(b'{"a":' * depth + b"0" + b"}" * depth + b"\n")
    path.chmod(0o644)
    _refresh_entry(root, entry)
    _write_manifest(root, manifest)

    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="JSON nesting exceeds supported depth"):
        load_installed_semantic_release_bundle()
    assert classify_canonical_bytes(b"ordinary", (CORE_SECRETS_PROFILE,)).outcome == (
        "INDETERMINATE"
    )


def test_unexecutable_regex_overflow_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    entry = _entry(manifest, kind="ruleset")
    ruleset = _canonical_load(_artifact_path(root, entry))
    rules = ruleset["rules"]
    assert isinstance(rules, list)
    rule = rules[0]
    assert isinstance(rule, dict)
    rule["pattern"] = "a{999999999999999999999}"
    _write_artifact_json(root, entry, ruleset)
    _write_manifest(root, manifest)

    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="pattern is unexecutable"):
        load_installed_semantic_release_bundle()
    assert classify_canonical_bytes(b"ordinary", (CORE_SECRETS_PROFILE,)).outcome == (
        "INDETERMINATE"
    )


def test_post_read_identity_revalidation_rejects_a_read_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    target = _artifact_path(root, _entry(manifest, kind="ruleset"))
    original_read = semantic_release._read_chunks
    raced = False

    def racing_read(descriptor: int, max_bytes: int) -> bytes:
        nonlocal raced
        raw = original_read(descriptor, max_bytes)
        if not raced and raw.startswith(
            b'{"contract":"graphify.workspace.semantic_release_ruleset'
        ):
            raced = True
            target.write_bytes(target.read_bytes() + b" ")
        return raw

    monkeypatch.setattr(semantic_release, "_read_chunks", racing_read)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="changed during read"):
        load_installed_semantic_release_bundle()
    assert raced


def test_final_inventory_revalidation_rejects_an_unlisted_file_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    original_read = semantic_release._read_chunks
    raced = False

    def racing_read(descriptor: int, max_bytes: int) -> bytes:
        nonlocal raced
        raw = original_read(descriptor, max_bytes)
        if not raced and raw.startswith(b'"""Internal P5B2 semantic-release'):
            raced = True
            extra = root / DATA_RELATIVE / "foreign.json"
            extra.write_bytes(b"{}\n")
            extra.chmod(0o644)
        return raw

    monkeypatch.setattr(semantic_release, "_read_chunks", racing_read)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="missing or unlisted"):
        load_installed_semantic_release_bundle()
    assert raced


def test_return_time_artifact_revalidation_rejects_previous_artifact_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    target = _artifact_path(root, _entry(manifest, kind="classifier_abi"))
    original_read = semantic_release._read_chunks
    raced = False

    def racing_read(descriptor: int, max_bytes: int) -> bytes:
        nonlocal raced
        raw = original_read(descriptor, max_bytes)
        if not raced and raw.startswith(
            b'{"contract":"graphify.workspace.semantic_release_ruleset'
        ):
            raced = True
            target.write_bytes(b'{"contract":"tampered.after-read"}\n')
            target.chmod(0o644)
        return raw

    monkeypatch.setattr(semantic_release, "_read_chunks", racing_read)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="artifact size differs from manifest"):
        load_installed_semantic_release_bundle()
    assert raced


def test_return_time_validation_keeps_artifacts_bound_through_whole_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    target = _artifact_path(root, _entry(manifest, kind="classifier"))
    original_read = semantic_release._read_chunks
    abi_reads = 0
    raced = False

    def racing_read(descriptor: int, max_bytes: int) -> bytes:
        nonlocal abi_reads, raced
        raw = original_read(descriptor, max_bytes)
        if raw.startswith(b'{"abi_id":"graphify.semantic_release.byte_abi.v1"'):
            abi_reads += 1
            if abi_reads == 2:
                raced = True
                target.write_bytes(b"# replaced after earlier return-time artifact read\n")
                target.chmod(0o644)
        return raw

    monkeypatch.setattr(semantic_release, "_read_chunks", racing_read)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="changed during validation"):
        load_installed_semantic_release_bundle()
    assert raced


def test_return_time_inventory_scan_runs_after_artifact_rereads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    original_read = semantic_release._read_chunks
    classifier_reads = 0
    raced = False

    def racing_read(descriptor: int, max_bytes: int) -> bytes:
        nonlocal classifier_reads, raced
        raw = original_read(descriptor, max_bytes)
        if raw.startswith(b'"""Internal P5B2 semantic-release'):
            classifier_reads += 1
            if classifier_reads == 2:
                raced = True
                extra = root / DATA_RELATIVE / "foreign-after-return-scan.json"
                extra.write_bytes(b"{}\n")
                extra.chmod(0o644)
        return raw

    monkeypatch.setattr(semantic_release, "_read_chunks", racing_read)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="missing or unlisted"):
        load_installed_semantic_release_bundle()
    assert raced


def test_return_time_manifest_revalidation_rejects_manifest_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    original_read = semantic_release._read_chunks
    raced = False

    def racing_read(descriptor: int, max_bytes: int) -> bytes:
        nonlocal raced
        raw = original_read(descriptor, max_bytes)
        if not raced and raw.startswith(
            b'{"contract":"graphify.workspace.semantic_release_ruleset'
        ):
            raced = True
            manifest["compatibility_version"] = 2
            _write_manifest(root, manifest)
        return raw

    monkeypatch.setattr(semantic_release, "_read_chunks", racing_read)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="manifest changed during validation"):
        load_installed_semantic_release_bundle()
    assert raced


def test_installed_package_root_symlink_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_root = _copy_bundle(tmp_path / "real")
    alias_parent = tmp_path / "alias"
    alias_parent.mkdir()
    alias_root = alias_parent / "graphify"
    alias_root.symlink_to(real_root, target_is_directory=True)
    _select_bundle(monkeypatch, alias_root)
    with pytest.raises(SemanticReleaseBundleError):
        load_installed_semantic_release_bundle()
    assert classify_canonical_bytes(b"ordinary", (CORE_SECRETS_PROFILE,)).outcome == (
        "INDETERMINATE"
    )


def test_manifest_and_declared_bundle_size_limits_fail_before_artifact_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest_path = _manifest_path(root)
    manifest_path.write_bytes(b" " * (BUNDLE_MANIFEST_MAX_BYTES + 1))
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="manifest exceeds"):
        load_installed_semantic_release_bundle()

    root = _copy_bundle(tmp_path / "declared")
    manifest = _manifest(root)
    _entries(manifest)[0]["byte_count"] = BUNDLE_ARTIFACT_MAX_BYTES + 1
    _write_manifest(root, manifest)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="bundle byte limit"):
        load_installed_semantic_release_bundle()


@pytest.mark.parametrize("kind", ["taxonomy", "ruleset"])
def test_taxonomy_and_rule_count_limits_are_independent_and_fail_closed(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    entry = _entry(manifest, kind=kind)
    artifact = _canonical_load(_artifact_path(root, entry))
    if kind == "taxonomy":
        artifact["categories"] = [
            {"category_id": f"category.{index}", "definition": "closed"}
            for index in range(MAX_CATEGORIES + 1)
        ]
    else:
        artifact["rules"] = [
            {
                "ascii_case_insensitive": False,
                "category_id": "secret.provider_credential",
                "credential_group": None,
                "matcher": "byte_regex_search_v1",
                "pattern": "x",
                "rule_id": f"rule.{index}",
            }
            for index in range(MAX_RULES + 1)
        ]
    _write_artifact_json(root, entry, artifact)
    _write_manifest(root, manifest)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match=f"{kind} limit"):
        load_installed_semantic_release_bundle()


def test_artifact_specific_members_coordinates_and_canonical_json_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _copy_bundle(tmp_path)
    manifest = _manifest(root)
    entry = _entry(manifest, kind="normalization")
    artifact = _canonical_load(_artifact_path(root, entry))
    artifact["ambient_locale"] = True
    _write_artifact_json(root, entry, artifact)
    _write_manifest(root, manifest)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="unexpected member"):
        load_installed_semantic_release_bundle()

    root = _copy_bundle(tmp_path / "coordinate")
    manifest = _manifest(root)
    entry = _profile_entry(manifest, "core_secrets.v1")
    artifact = _canonical_load(_artifact_path(root, entry))
    artifact["profile_version"] = 2
    _write_artifact_json(root, entry, artifact)
    _write_manifest(root, manifest)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="coordinate"):
        load_installed_semantic_release_bundle()

    root = _copy_bundle(tmp_path / "duplicate")
    manifest = _manifest(root)
    entry = _entry(manifest, kind="taxonomy")
    path = _artifact_path(root, entry)
    raw = path.read_bytes().replace(
        b'{"categories":',
        b'{"categories":[],"categories":',
        1,
    )
    path.write_bytes(raw)
    path.chmod(0o644)
    _refresh_entry(root, entry)
    _write_manifest(root, manifest)
    _select_bundle(monkeypatch, root)
    with pytest.raises(SemanticReleaseBundleError, match="duplicate JSON member"):
        load_installed_semantic_release_bundle()
