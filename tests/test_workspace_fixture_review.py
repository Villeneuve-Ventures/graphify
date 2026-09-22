"""New fixture race regressions; run directly with the stdlib unittest runner."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphify.workspace.contracts import ContractError
from tools.workspace_artifacts import candidate


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
                     "descriptor capture primitives unavailable")
class FixtureReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "input"
        self.path.write_bytes(b"original")

    def test_regular_file(self):
        self.assertEqual(candidate._read(self.path), b"original")

    def test_symlink_swap_after_lstat(self):
        other = self.root / "other"
        other.write_bytes(b"substitute")
        lstat = Path.lstat
        swapped = False

        def swap(path):
            nonlocal swapped
            info = lstat(path)
            if path == self.path and not swapped:
                swapped = True
                path.unlink()
                path.symlink_to(other)
            return info

        with patch.object(Path, "lstat", swap), self.assertRaises(ContractError):
            candidate._read(self.path)

    def test_identity_check_still_refuses_symlink_without_nofollow_flag(self):
        with patch.object(os, "O_NOFOLLOW", 0):
            self.test_symlink_swap_after_lstat()

    def test_regular_replacement_after_lstat(self):
        other = self.root / "other"
        other.write_bytes(b"substitute")
        lstat = Path.lstat
        swapped = False

        def swap(path):
            nonlocal swapped
            info = lstat(path)
            if path == self.path and not swapped:
                swapped = True
                other.replace(path)
            return info

        with patch.object(Path, "lstat", swap), self.assertRaises(ContractError):
            candidate._read(self.path)

    def test_fifo_swap_opens_nonblocking_and_never_reads(self):
        original_open = os.open

        def swap(path, flags, *args, **kwargs):
            if Path(path) == self.path:
                self.assertTrue(flags & os.O_NONBLOCK)
                self.path.unlink()
                os.mkfifo(self.path)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(os, "open", swap), patch.object(os, "read", side_effect=AssertionError("FIFO read")), self.assertRaises(ContractError):
            candidate._read(self.path)

    def test_mutation_during_read(self):
        original_read = os.read
        changed = False

        def change(fd, size):
            nonlocal changed
            data = original_read(fd, size)
            if not changed:
                changed = True
                self.path.write_bytes(b"changed!")
            return data

        with patch.object(os, "read", change), self.assertRaises(ContractError):
            candidate._read(self.path)

    def test_growth_during_read_remains_bounded(self):
        original_read = os.read
        requests, received = [], []

        def growing(fd, size):
            requests.append(size)
            data = original_read(fd, size)
            received.append(len(data))
            if len(requests) == 1:
                with self.path.open("ab") as stream:
                    stream.write(b"more bytes")
            return data

        with patch.object(candidate, "MAX_INPUT_BYTES", 8), patch.object(os, "read", growing), self.assertRaises(ContractError):
            candidate._read(self.path)
        self.assertLessEqual(max(requests), 9)
        self.assertLessEqual(sum(received), 9)


@unittest.skipIf(os.name == "nt", "POSIX capability publication checks")
class FixturePublicationTests(unittest.TestCase):
    def setUp(self):
        from graphify import transaction
        self.transaction = transaction
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.parent = self.root / "parent"
        self.parent.mkdir()
        self.detached = self.root / "detached"
        self.payload = self.parent / "stage" / "payload"
        self.payload.mkdir(parents=True)
        (self.payload / "member").write_bytes(b"keep")
        self.output = self.parent / "fixture"

    def detach(self):
        self.parent.rename(self.detached)
        self.parent.mkdir()
        (self.parent / "other-owner").write_bytes(b"preserve")

    def test_parent_detached_before_publication(self):
        original_pin = self.transaction.pin_output

        def detach_after_pin(*args, **kwargs):
            capability = original_pin(*args, **kwargs)
            self.detach()
            return capability

        with patch.object(self.transaction, "pin_output", detach_after_pin), self.assertRaises(self.transaction.PendingTransactionError):
            candidate._publish_fixture(self.payload, self.output)
        self.assertFalse((self.detached / "fixture").exists())
        self.assertEqual((self.detached / "stage/payload/member").read_bytes(), b"keep")
        self.assertEqual((self.parent / "other-owner").read_bytes(), b"preserve")

    def test_parent_detached_during_publication(self):
        original_rename = self.transaction._atomic_rename_no_replace

        def detach_then_publish(*args):
            self.detach()
            original_rename(*args)

        with patch.object(self.transaction, "_atomic_rename_no_replace", detach_then_publish), self.assertRaises(self.transaction.PendingTransactionError):
            candidate._publish_fixture(self.payload, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual((self.detached / "fixture/member").read_bytes(), b"keep")
        self.assertEqual((self.parent / "other-owner").read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
