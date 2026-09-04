"""Read-only verifier for protected-change Git index reconstruction."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import struct
import sys
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, NoReturn


VERIFIER_VERSION = "graphify.protected-change-verifier.v1"
SCHEMA_SHA256 = "59666f5cf42b4a8e37c0275194f955124a65a3502090502ec8d557e8c5e24c8d"

_SCHEMA_JSON = r'''{"accepted_invariants":["acquisition.open","acquisition.component","acquisition.symlink","acquisition.regular","acquisition.changed","index.size","index.signature","index.version","index.count","index.checksum","entry.prefix","entry.flags","entry.mode","entry.oid","entry.path.terminator","entry.path.length","entry.path.shape","entry.path.utf8","entry.path.order","entry.padding","extension.count","extension.bytes","extension.frame","extension.required","extension.fsmonitor","output.reconstruction_limit","output.manifest_limit"],"candidate_index":{"additional_properties":false,"array_order":{"supported_modes":["100644","100755","120000"],"supported_stages":[0]},"digest_boundary":"source_manifest_sha256 hashes exact canonical source object bytes; records_sha256 hashes decoded records_base64 bytes.","fields":{"records_base64":"base64_string","records_count":"nonnegative_integer","records_sha256":"sha256","schema":"graphify.protected-change-review.candidate-index.v1","schema_sha256":"sha256","source_manifest_sha256":"sha256","source_raw_index_sha256":"sha256","supported_modes":"exact_array[six_digit_git_mode]","supported_stages":"exact_array[nonnegative_integer]","verifier":"graphify.protected-change-verifier.v1"}},"canonical_json":{"encoding":"UTF-8","profile":"graphify.protected-change-review.policy.v2 section 5","terminal_newline":false,"vector_sha256":"2abd1782cb53952044e4239efb441e7f2c17c487ce1a9376aa433845070bf4cd"},"emission_order":["validate acquisition and raw bytes","build and bound reconstruction stream","canonicalize source","canonicalize candidate_index and bind source digest","canonicalize evidence and bind source/candidate digests","canonicalize result"],"error":{"additional_properties":false,"detail_fields":{"actual":"nonnegative_integer","limit":"nonnegative_integer","offset":"nonnegative_integer","size":"nonnegative_integer"},"fields":{"details":"object[subset_of_detail_fields]","invariant":"one_of_invariant_ids","result":"rejected","schema":"graphify.protected-change-review.verifier-error.v1","schema_sha256":"sha256","verifier":"graphify.protected-change-verifier.v1"}},"evidence":{"additional_properties":false,"array_order":{"accepted_invariants":"exact accepted_invariants array from this descriptor; it is the exhaustive ordered successful validation checklist for v1"},"digest_boundary":"candidate_index_manifest_sha256 hashes exact canonical candidate_index object bytes; source_manifest_sha256 hashes exact canonical source object bytes. Evidence is emitted only after complete read-only verification and reconstruction-stream generation.","fields":{"accepted_invariants":"exact_array[invariant_ids]","candidate_index_manifest_sha256":"sha256","result":"accepted","schema":"graphify.protected-change-review.evidence.v1","schema_sha256":"sha256","source_manifest_sha256":"sha256","verifier":"graphify.protected-change-verifier.v1"}},"invariant_ids":["acquisition.changed","acquisition.component","acquisition.open","acquisition.regular","acquisition.symlink","attestation.invalid","entry.flags","entry.mode","entry.oid","entry.padding","entry.path.length","entry.path.order","entry.path.shape","entry.path.terminator","entry.path.utf8","entry.prefix","extension.bytes","extension.count","extension.frame","extension.fsmonitor","extension.required","index.checksum","index.count","index.signature","index.size","index.version","internal.failure","output.manifest_limit","output.reconstruction_limit"],"limits":{"extension_count":{"maximum":128,"semantics":"total extension frame count"},"extension_payload_bytes":{"maximum":16777216,"semantics":"maximum payload bytes for one extension frame"},"extension_total_bytes":{"maximum":16777216,"semantics":"aggregate signature, size, and payload bytes across all extension frames"},"index_bytes":{"maximum":33554432,"semantics":"aggregate raw index bytes including checksum"},"index_entries":{"maximum":250000,"semantics":"declared DIRC entry count"},"manifest_bytes":{"maximum":50331648,"semantics":"exact canonical success-result bytes after Base64 expansion"},"path_bytes":{"maximum":1048576,"semantics":"one raw entry path before UTF-8 decoding"},"read_chunk_bytes":{"maximum":1048576,"semantics":"largest single descriptor read request"},"reconstruction_bytes":{"maximum":33554432,"semantics":"aggregate decoded update-index record-stream bytes"}},"parser_source":{"acquisition":"Open the installed module __file__ with the same component-by-component no-follow regular-file reader before hashing exact bytes.","claim":"Binds the observed checked-in verifier source bytes; it does not prove interpreter, import-cache, process, caller, or host authority."},"result":{"additional_properties":false,"digest_boundary":"Nested digests never include the enclosing object; the result itself has no self-digest.","fields":{"candidate_index":"candidate_index","evidence":"evidence","schema":"graphify.protected-change-verifier.result.v1","schema_sha256":"sha256","source":"source","verifier":"graphify.protected-change-verifier.v1"}},"schema":"graphify.protected-change-verifier.manifest-schemas.v1","source":{"additional_properties":false,"array_order":{"extensions":"on-disk extension frame order"},"extension_fields":{"payload_sha256":"sha256","signature":"four ASCII characters","size":"nonnegative_integer"},"field_semantics":{"acquisition.component_count":"number of opened path components including the final index file and excluding the starting directory descriptor","acquisition.mode":"full POSIX st_mode integer from final fstat","acquisition.times":"nanoseconds from final stable fstat observation"},"fields":{"acquisition":"{component_count,ctime_ns,device,inode,mode,mtime_ns,size}:nonnegative_integer","checksum_sha1":"full_git_oid","entry_count":"nonnegative_integer","extensions":"array[extension_fields]","index_version":"2|3","inventory_count":"nonnegative_integer","inventory_sha256":"sha256","limits":"exact numeric maximums keyed like this descriptor limits object","parser_source_sha256":"sha256","raw_index_sha256":"sha256","schema":"graphify.protected-change-review.source.v1","schema_sha256":"sha256","verifier":"graphify.protected-change-verifier.v1"}}}'''


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (bool, int)):
        if isinstance(value, int) and value < 0:
            raise ValueError("canonical JSON integers must be nonnegative")
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("canonical JSON strings must contain Unicode scalar values")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON object keys must be strings")
            _validate_json_value(key)
            _validate_json_value(item)
        return
    raise ValueError("unsupported canonical JSON value")


def canonical_json(value: Any) -> bytes:
    """Encode a value with the protected-review canonical JSON profile."""
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _AsciiStringLength:
    length: int


def _canonical_string_size(value: str) -> int:
    size = 2
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\", "\b", "\t", "\n", "\f", "\r"}:
            size += 2
        elif codepoint < 0x20:
            size += 6
        else:
            size += len(character.encode("utf-8"))
    return size


def _canonical_json_size(value: Any) -> int:
    if value is None:
        return 4
    if value is True:
        return 4
    if value is False:
        return 5
    if type(value) is int:
        return len(str(value))
    if isinstance(value, str):
        return _canonical_string_size(value)
    if isinstance(value, _AsciiStringLength):
        return value.length + 2
    if isinstance(value, list | tuple):
        return 2 + max(0, len(value) - 1) + sum(
            _canonical_json_size(item) for item in value
        )
    if isinstance(value, Mapping):
        return 2 + max(0, len(value) - 1) + sum(
            _canonical_string_size(key) + 1 + _canonical_json_size(item)
            for key, item in value.items()
        )
    raise ValueError("unsupported canonical JSON value")


SCHEMA_DESCRIPTOR = json.loads(_SCHEMA_JSON)
SCHEMA_BYTES = canonical_json(SCHEMA_DESCRIPTOR)
if hashlib.sha256(SCHEMA_BYTES).hexdigest() != SCHEMA_SHA256:
    raise RuntimeError("embedded protected-change schema digest mismatch")

ACCEPTED_INVARIANTS = tuple(SCHEMA_DESCRIPTOR["accepted_invariants"])
INVARIANT_IDS = frozenset(SCHEMA_DESCRIPTOR["invariant_ids"])
SUPPORTED_MODES = ("100644", "100755", "120000")
SUPPORTED_STAGES = (0,)


@dataclass(frozen=True, slots=True)
class Limits:
    index_bytes: int = 33_554_432
    index_entries: int = 250_000
    path_bytes: int = 1_048_576
    extension_count: int = 128
    extension_payload_bytes: int = 16_777_216
    extension_total_bytes: int = 16_777_216
    reconstruction_bytes: int = 33_554_432
    manifest_bytes: int = 50_331_648
    read_chunk_bytes: int = 1_048_576

    def as_dict(self) -> dict[str, int]:
        return {
            "extension_count": self.extension_count,
            "extension_payload_bytes": self.extension_payload_bytes,
            "extension_total_bytes": self.extension_total_bytes,
            "index_bytes": self.index_bytes,
            "index_entries": self.index_entries,
            "manifest_bytes": self.manifest_bytes,
            "path_bytes": self.path_bytes,
            "read_chunk_bytes": self.read_chunk_bytes,
            "reconstruction_bytes": self.reconstruction_bytes,
        }


LIMITS = Limits()
if LIMITS.as_dict() != {
    key: value["maximum"] for key, value in SCHEMA_DESCRIPTOR["limits"].items()
}:
    raise RuntimeError("embedded protected-change limits mismatch")


class VerificationError(RuntimeError):
    """A stable, path-redacted verifier rejection."""

    __slots__ = ("details", "invariant")

    def __init__(self, invariant: str, **details: int) -> None:
        if invariant not in INVARIANT_IDS:
            invariant = "internal.failure"
            details = {}
        allowed = {"actual", "limit", "offset", "size"}
        safe_details = {
            key: value
            for key, value in details.items()
            if key in allowed and type(value) is int and value >= 0
        }
        super().__init__(invariant)
        self.invariant = invariant
        self.details = MappingProxyType(safe_details)

    def as_dict(self) -> dict[str, Any]:
        return {
            "details": dict(self.details),
            "invariant": self.invariant,
            "result": "rejected",
            "schema": "graphify.protected-change-review.verifier-error.v1",
            "schema_sha256": SCHEMA_SHA256,
            "verifier": VERIFIER_VERSION,
        }


@dataclass(frozen=True, slots=True)
class Acquisition:
    component_count: int
    ctime_ns: int
    device: int
    inode: int
    mode: int
    mtime_ns: int
    size: int

    @classmethod
    def from_stat(cls, info: os.stat_result, component_count: int) -> Acquisition:
        return cls(
            component_count=component_count,
            ctime_ns=info.st_ctime_ns,
            device=info.st_dev,
            inode=info.st_ino,
            mode=info.st_mode,
            mtime_ns=info.st_mtime_ns,
            size=info.st_size,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "component_count": self.component_count,
            "ctime_ns": self.ctime_ns,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class IndexEntry:
    mode: str
    oid: bytes
    raw_path: bytes


@dataclass(frozen=True, slots=True)
class IndexExtension:
    payload_sha256: str
    signature: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "payload_sha256": self.payload_sha256,
            "signature": self.signature,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class ParsedIndex:
    checksum_sha1: str
    entries: tuple[IndexEntry, ...]
    extensions: tuple[IndexExtension, ...]
    inventory: bytes
    inventory_sha256: str
    raw_index_sha256: str
    reconstruction_records: bytes
    records_sha256: str
    version: int


_ATTESTATION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class VerifiedIndexSnapshot:
    """Opaque, immutable output from :func:`verify_index`."""

    reconstruction_records: bytes
    _acquisition: Acquisition
    _canonical_result: bytes
    _parsed: ParsedIndex
    _parser_source_sha256: str
    _raw_index: bytes
    _schema_sha256: str
    _token: object
    _verifier: str

    @property
    def canonical_result(self) -> bytes:
        return self._canonical_result

    def result(self) -> dict[str, Any]:
        return json.loads(self._canonical_result)


def _reject(invariant: str, **details: int) -> NoReturn:
    raise VerificationError(invariant, **details)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
    )


def _same_stable_file(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_identity(left, right) and (
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _path_parts(path: os.PathLike[str] | str) -> tuple[str, tuple[str, ...]]:
    try:
        raw = os.fspath(path)
    except TypeError:
        _reject("acquisition.component")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        _reject("acquisition.component")
    if raw.startswith(os.sep):
        anchor = os.sep
        parts = tuple(raw[len(os.sep) :].split(os.sep))
    else:
        anchor = "."
        parts = tuple(raw.split(os.sep))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        _reject("acquisition.component")
    return anchor, parts


def _lstat_at(name: str, directory_fd: int, *, final: bool) -> os.stat_result:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        _reject("acquisition.open" if final else "acquisition.component")
    if stat.S_ISLNK(info.st_mode):
        _reject("acquisition.symlink")
    return info


def _open_at(name: str, flags: int, directory_fd: int, *, final: bool) -> int:
    try:
        return os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        _reject("acquisition.open" if final else "acquisition.component")


def _acquire_regular_file(
    path: os.PathLike[str] | str,
    *,
    maximum_bytes: int,
    chunk_bytes: int,
) -> tuple[bytes, Acquisition]:
    if not (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and getattr(os, "O_DIRECTORY", 0)
        and getattr(os, "O_NOFOLLOW", 0)
    ):
        _reject("acquisition.component")

    anchor, parts = _path_parts(path)
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        directory_fd = os.open(anchor, directory_flags)
    except OSError:
        _reject("acquisition.component")

    file_fd: int | None = None
    try:
        for component in parts[:-1]:
            before = _lstat_at(component, directory_fd, final=False)
            if not stat.S_ISDIR(before.st_mode):
                _reject("acquisition.component")
            next_fd = _open_at(component, directory_flags, directory_fd, final=False)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode) or not _same_identity(before, opened):
                os.close(next_fd)
                _reject("acquisition.changed")
            os.close(directory_fd)
            directory_fd = next_fd

        leaf = parts[-1]
        directory_entry = _lstat_at(leaf, directory_fd, final=True)
        if not stat.S_ISREG(directory_entry.st_mode):
            _reject("acquisition.regular")
        file_flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_fd = _open_at(leaf, file_flags, directory_fd, final=True)
        before = os.fstat(file_fd)
        if not _same_identity(directory_entry, before):
            _reject("acquisition.changed")
        if not stat.S_ISREG(before.st_mode):
            _reject("acquisition.regular")
        if before.st_size < 0 or before.st_size > maximum_bytes:
            _reject("index.size", actual=max(before.st_size, 0), limit=maximum_bytes)

        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            request_size = min(remaining, chunk_bytes)
            try:
                chunk = os.read(file_fd, request_size)
            except OSError:
                _reject("acquisition.changed")
            if not chunk:
                _reject("index.size", actual=before.st_size - remaining, size=before.st_size)
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            if os.read(file_fd, 1):
                _reject("acquisition.changed")
            after = os.fstat(file_fd)
        except OSError:
            _reject("acquisition.changed")
        current_entry = _lstat_at(leaf, directory_fd, final=True)
        if not _same_stable_file(before, after) or not _same_identity(after, current_entry):
            _reject("acquisition.changed")
        return b"".join(chunks), Acquisition.from_stat(after, len(parts))
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _parse_index_bytes(raw: bytes, limits: Limits = LIMITS) -> ParsedIndex:
    """Parse one immutable SHA-1 DIRC v2/v3 byte buffer."""
    if type(raw) is not bytes:
        _reject("index.size")
    if len(raw) > limits.index_bytes:
        _reject("index.size", actual=len(raw), limit=limits.index_bytes)
    if len(raw) < 12:
        _reject("index.size", actual=len(raw))
    if raw[:4] != b"DIRC":
        _reject("index.signature")
    version, entry_count = struct.unpack_from(">II", raw, 4)
    if version not in (2, 3):
        _reject("index.version", actual=version)
    if entry_count > limits.index_entries:
        _reject("index.count", actual=entry_count, limit=limits.index_entries)
    if len(raw) < 32:
        _reject("index.checksum", actual=max(0, len(raw) - 12), size=20)

    checksum_offset = len(raw) - 20
    checksum = raw[checksum_offset:]
    if hashlib.sha1(raw[:checksum_offset]).digest() != checksum:
        _reject("index.checksum")
    if entry_count > 1 and entry_count > max(0, checksum_offset - 12) // 64:
        _reject("index.count", actual=entry_count)

    entries: list[IndexEntry] = []
    inventory_parts: list[bytes] = []
    reconstruction_parts: list[bytes] = []
    reconstruction_size = 0
    offset = 12
    previous_path: bytes | None = None

    for _ in range(entry_count):
        entry_start = offset
        prefix_end = entry_start + 62
        if prefix_end > checksum_offset:
            _reject("entry.prefix", offset=entry_start)
        mode = struct.unpack_from(">I", raw, entry_start + 24)[0]
        oid = raw[entry_start + 40 : entry_start + 60]
        flags = struct.unpack_from(">H", raw, entry_start + 60)[0]
        if flags & 0xF000:
            _reject("entry.flags", offset=entry_start)
        mode_text = f"{mode:06o}"
        if mode_text not in SUPPORTED_MODES:
            _reject("entry.mode", offset=entry_start)
        if oid == b"\x00" * 20:
            _reject("entry.oid", offset=entry_start)

        terminator = raw.find(b"\x00", prefix_end, checksum_offset)
        if terminator < 0:
            _reject("entry.path.terminator", offset=prefix_end)
        raw_path = raw[prefix_end:terminator]
        if len(raw_path) > limits.path_bytes:
            _reject("entry.path.length", actual=len(raw_path), limit=limits.path_bytes)
        encoded_length = flags & 0x0FFF
        if encoded_length != min(len(raw_path), 0x0FFF):
            _reject("entry.path.length", actual=len(raw_path))
        if not raw_path:
            _reject("entry.path.shape")
        try:
            raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _reject("entry.path.utf8")
        components = raw_path.split(b"/")
        if (
            raw_path.startswith(b"/")
            or raw_path.endswith(b"/")
            or any(component in {b"", b".", b"..", b".git"} for component in components)
        ):
            _reject("entry.path.shape")
        if previous_path is not None and raw_path <= previous_path:
            _reject("entry.path.order")
        previous_path = raw_path

        entry_size = 62 + len(raw_path) + 1
        padding_size = (-entry_size) % 8
        padding_end = terminator + 1 + padding_size
        if padding_end > checksum_offset or any(raw[terminator + 1 : padding_end]):
            _reject("entry.padding", offset=terminator + 1)
        offset = padding_end

        reconstruction_record_size = 51 + len(raw_path)
        next_reconstruction_size = reconstruction_size + reconstruction_record_size
        if next_reconstruction_size > limits.reconstruction_bytes:
            _reject(
                "output.reconstruction_limit",
                actual=next_reconstruction_size,
                limit=limits.reconstruction_bytes,
            )
        reconstruction_size = next_reconstruction_size
        inventory_record = len(raw_path).to_bytes(8, "big") + raw_path + (0).to_bytes(4, "big")
        reconstruction_record = _reconstruction_record(mode_text, oid, raw_path)
        inventory_parts.append(inventory_record)
        reconstruction_parts.append(reconstruction_record)
        entries.append(IndexEntry(mode=mode_text, oid=oid, raw_path=raw_path))

    extensions: list[IndexExtension] = []
    extension_total = 0
    while offset < checksum_offset:
        if len(extensions) >= limits.extension_count:
            _reject(
                "extension.count",
                actual=len(extensions) + 1,
                limit=limits.extension_count,
            )
        if checksum_offset - offset < 8:
            _reject("extension.frame", offset=offset)
        signature_bytes = raw[offset : offset + 4]
        payload_size = struct.unpack_from(">I", raw, offset + 4)[0]
        if payload_size > limits.extension_payload_bytes:
            _reject(
                "extension.bytes",
                actual=payload_size,
                limit=limits.extension_payload_bytes,
            )
        frame_size = 8 + payload_size
        extension_total += frame_size
        if extension_total > limits.extension_total_bytes:
            _reject(
                "extension.bytes",
                actual=extension_total,
                limit=limits.extension_total_bytes,
            )
        payload_start = offset + 8
        payload_end = payload_start + payload_size
        if payload_end > checksum_offset:
            _reject("extension.frame", offset=offset, size=payload_size)
        if signature_bytes == b"FSMN":
            _reject("extension.fsmonitor", offset=offset)
        if not signature_bytes or not 0x41 <= signature_bytes[0] <= 0x5A:
            _reject("extension.required", offset=offset)
        if any(byte > 0x7F for byte in signature_bytes):
            _reject("extension.frame", offset=offset)
        payload = raw[payload_start:payload_end]
        extensions.append(
            IndexExtension(
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                signature=signature_bytes.decode("ascii"),
                size=payload_size,
            )
        )
        offset = payload_end

    if offset != checksum_offset:
        _reject("extension.frame", offset=offset)
    inventory = b"".join(inventory_parts)
    reconstruction = b"".join(reconstruction_parts)
    return ParsedIndex(
        checksum_sha1=checksum.hex(),
        entries=tuple(entries),
        extensions=tuple(extensions),
        inventory=inventory,
        inventory_sha256=hashlib.sha256(inventory).hexdigest(),
        raw_index_sha256=hashlib.sha256(raw).hexdigest(),
        reconstruction_records=reconstruction,
        records_sha256=hashlib.sha256(reconstruction).hexdigest(),
        version=version,
    )


def _reconstruction_record(mode: str, oid: bytes, raw_path: bytes) -> bytes:
    return (
        mode.encode("ascii")
        + b" "
        + oid.hex().encode("ascii")
        + b" 0\t"
        + raw_path
        + b"\x00"
    )


def _build_result(
    parsed: ParsedIndex,
    acquisition: Acquisition,
    parser_source_sha256: str,
) -> bytes:
    encoded_size = 4 * ((len(parsed.reconstruction_records) + 2) // 3)
    source = {
        "acquisition": acquisition.as_dict(),
        "checksum_sha1": parsed.checksum_sha1,
        "entry_count": len(parsed.entries),
        "extensions": [extension.as_dict() for extension in parsed.extensions],
        "index_version": parsed.version,
        "inventory_count": len(parsed.entries),
        "inventory_sha256": parsed.inventory_sha256,
        "limits": LIMITS.as_dict(),
        "parser_source_sha256": parser_source_sha256,
        "raw_index_sha256": parsed.raw_index_sha256,
        "schema": "graphify.protected-change-review.source.v1",
        "schema_sha256": SCHEMA_SHA256,
        "verifier": VERIFIER_VERSION,
    }
    source_bytes = canonical_json(source)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    candidate_index_shape = {
        "records_base64": _AsciiStringLength(encoded_size),
        "records_count": len(parsed.entries),
        "records_sha256": parsed.records_sha256,
        "schema": "graphify.protected-change-review.candidate-index.v1",
        "schema_sha256": SCHEMA_SHA256,
        "source_manifest_sha256": source_sha256,
        "source_raw_index_sha256": parsed.raw_index_sha256,
        "supported_modes": list(SUPPORTED_MODES),
        "supported_stages": list(SUPPORTED_STAGES),
        "verifier": VERIFIER_VERSION,
    }
    evidence_shape = {
        "accepted_invariants": list(ACCEPTED_INVARIANTS),
        "candidate_index_manifest_sha256": "0" * 64,
        "result": "accepted",
        "schema": "graphify.protected-change-review.evidence.v1",
        "schema_sha256": SCHEMA_SHA256,
        "source_manifest_sha256": source_sha256,
        "verifier": VERIFIER_VERSION,
    }
    result_shape = {
        "candidate_index": candidate_index_shape,
        "evidence": evidence_shape,
        "schema": "graphify.protected-change-verifier.result.v1",
        "schema_sha256": SCHEMA_SHA256,
        "source": source,
        "verifier": VERIFIER_VERSION,
    }
    exact_result_size = _canonical_json_size(result_shape)
    if exact_result_size > LIMITS.manifest_bytes:
        _reject(
            "output.manifest_limit",
            actual=exact_result_size,
            limit=LIMITS.manifest_bytes,
        )

    candidate_index = dict(candidate_index_shape)
    candidate_index["records_base64"] = base64.b64encode(
        parsed.reconstruction_records
    ).decode("ascii")
    candidate_bytes = canonical_json(candidate_index)
    evidence = dict(evidence_shape)
    evidence["candidate_index_manifest_sha256"] = hashlib.sha256(candidate_bytes).hexdigest()
    result = {
        "candidate_index": candidate_index,
        "evidence": evidence,
        "schema": "graphify.protected-change-verifier.result.v1",
        "schema_sha256": SCHEMA_SHA256,
        "source": source,
        "verifier": VERIFIER_VERSION,
    }
    encoded = canonical_json(result)
    if len(encoded) != exact_result_size:
        _reject("internal.failure")
    return encoded


def verify_index(path: os.PathLike[str] | str) -> VerifiedIndexSnapshot:
    """Acquire and validate one index, returning a frozen same-buffer attestation."""
    raw_index, acquisition = _acquire_regular_file(
        path,
        maximum_bytes=LIMITS.index_bytes,
        chunk_bytes=LIMITS.read_chunk_bytes,
    )
    parsed = _parse_index_bytes(raw_index)
    parser_source, _ = _acquire_regular_file(
        __file__,
        maximum_bytes=LIMITS.index_bytes,
        chunk_bytes=LIMITS.read_chunk_bytes,
    )
    parser_source_sha256 = hashlib.sha256(parser_source).hexdigest()
    canonical_result = _build_result(parsed, acquisition, parser_source_sha256)
    return VerifiedIndexSnapshot(
        reconstruction_records=parsed.reconstruction_records,
        _acquisition=acquisition,
        _canonical_result=canonical_result,
        _parsed=parsed,
        _parser_source_sha256=parser_source_sha256,
        _raw_index=raw_index,
        _schema_sha256=SCHEMA_SHA256,
        _token=_ATTESTATION_TOKEN,
        _verifier=VERIFIER_VERSION,
    )


def build_reconstruction_stream(snapshot: VerifiedIndexSnapshot) -> bytes:
    """Return digest-revalidated records from an exact module-issued snapshot."""
    if type(snapshot) is not VerifiedIndexSnapshot:
        _reject("attestation.invalid")
    if (
        snapshot._token is not _ATTESTATION_TOKEN
        or snapshot._schema_sha256 != SCHEMA_SHA256
        or snapshot._verifier != VERIFIER_VERSION
        or type(snapshot._raw_index) is not bytes
        or type(snapshot.reconstruction_records) is not bytes
    ):
        _reject("attestation.invalid")
    try:
        reparsed = _parse_index_bytes(snapshot._raw_index)
        rebuilt = _build_result(
            reparsed,
            snapshot._acquisition,
            snapshot._parser_source_sha256,
        )
    except Exception as exc:
        raise VerificationError("attestation.invalid") from exc
    if (
        reparsed != snapshot._parsed
        or reparsed.reconstruction_records != snapshot.reconstruction_records
        or hashlib.sha256(snapshot.reconstruction_records).hexdigest()
        != reparsed.records_sha256
        or rebuilt != snapshot._canonical_result
    ):
        _reject("attestation.invalid")
    return snapshot.reconstruction_records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one SHA-1 DIRC v2/v3 index without mutating it."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--index", metavar="PATH", help="index file to verify")
    group.add_argument("--schema", action="store_true", help="emit the embedded schema")
    group.add_argument("--version", action="store_true", help="emit the verifier version")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.schema:
        sys.stdout.buffer.write(SCHEMA_BYTES)
        return 0
    if arguments.version:
        sys.stdout.buffer.write(VERIFIER_VERSION.encode("ascii"))
        return 0
    try:
        snapshot = verify_index(arguments.index)
        output = snapshot.canonical_result
    except VerificationError as exc:
        output = canonical_json(exc.as_dict())
        sys.stdout.buffer.write(output)
        return 1
    except Exception:
        output = canonical_json(VerificationError("internal.failure").as_dict())
        sys.stdout.buffer.write(output)
        return 1
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
