from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from logreduce.files import FileSystemError, SourceFile
from logreduce.transaction import (
    Transaction,
    TransactionError,
    recover_pending_transactions,
)


def _source(path: Path) -> SourceFile:
    stat_result = path.stat()
    return SourceFile(
        path=path,
        relative_path=path.name,
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        inode=stat_result.st_ino,
        device=stat_result.st_dev,
    )


class TransactionTests(unittest.TestCase):
    def test_commit_publishes_output_and_removes_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "a.log"
            source_path.write_bytes(b"source")
            transaction = Transaction.create(root, "out.log", [_source(source_path)])
            transaction.final_path.write_bytes(b"result")

            transaction.commit(force=False)

            self.assertEqual((root / "out.log").read_bytes(), b"result")
            self.assertFalse(source_path.exists())
            self.assertFalse(transaction.directory.exists())

    def test_commit_failure_restores_source_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "a.log"
            source_path.write_bytes(b"source")
            (root / "out.log").write_bytes(b"previous")
            transaction = Transaction.create(root, "out.log", [_source(source_path)])
            transaction.final_path.write_bytes(b"result")

            with mock.patch(
                "logreduce.transaction._verify_manifest_source",
                side_effect=FileSystemError("simulated race"),
            ):
                with self.assertRaises(TransactionError):
                    transaction.commit(force=True)

            self.assertEqual(source_path.read_bytes(), b"source")
            self.assertEqual((root / "out.log").read_bytes(), b"previous")
            self.assertFalse(transaction.directory.exists())

    def test_refuses_existing_output_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "a.log"
            source_path.write_bytes(b"source")
            (root / "out.log").write_bytes(b"previous")
            transaction = Transaction.create(root, "out.log", [_source(source_path)])
            transaction.final_path.write_bytes(b"result")

            with self.assertRaisesRegex(TransactionError, "--force"):
                transaction.commit(force=False)

            self.assertEqual((root / "out.log").read_bytes(), b"previous")
            self.assertTrue(source_path.exists())
            transaction.abort()

    def test_recovery_rolls_back_interrupted_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "a.log"
            source_path.write_bytes(b"source")
            transaction = Transaction.create(root, "out.log", [_source(source_path)])
            transaction.final_path.write_bytes(b"result")
            transaction.manifest.update(
                {
                    "state": "publishing",
                    "had_output": False,
                    "final_identity": (
                        transaction.final_path.stat().st_dev,
                        transaction.final_path.stat().st_ino,
                        transaction.final_path.stat().st_size,
                    ),
                }
            )
            transaction._write_manifest()
            transaction.final_path.replace(root / "out.log")

            recovered = recover_pending_transactions(root)

            self.assertEqual(len(recovered), 1)
            self.assertFalse((root / "out.log").exists())
            self.assertEqual(source_path.read_bytes(), b"source")
            self.assertFalse(transaction.directory.exists())

    def test_recovery_finishes_published_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "a.log"
            source_path.write_bytes(b"source")
            transaction = Transaction.create(root, "out.log", [_source(source_path)])
            transaction.final_path.write_bytes(b"result")
            transaction.manifest.update(
                {
                    "state": "published",
                    "had_output": False,
                    "final_identity": (
                        transaction.final_path.stat().st_dev,
                        transaction.final_path.stat().st_ino,
                        transaction.final_path.stat().st_size,
                    ),
                }
            )
            transaction._write_manifest()
            transaction.final_path.replace(root / "out.log")

            recover_pending_transactions(root)

            self.assertEqual((root / "out.log").read_bytes(), b"result")
            self.assertFalse(source_path.exists())
            self.assertFalse(transaction.directory.exists())


if __name__ == "__main__":
    unittest.main()
