"""Backup tests: snapshot integrity, naming, pruning, pre-migration hook."""

from __future__ import annotations

import io
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mail_core import backup, store
from mail_core.__main__ import backup_before_migration, main
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.support import workspace_temporary_directory

OLD_SCHEMA = max(1, store.SCHEMA_VERSION - 1)


def _make_source_db(path: Path, *, version: int = 3) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY, address TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO accounts (id, address) VALUES ('acc_1', 'a@b.com')"
        )
        connection.execute(f"PRAGMA user_version = {int(version)}")
        connection.commit()
    finally:
        connection.close()


def _make_schema_at(path: Path, version: int) -> None:
    """Build a real database that stops exactly at ``version``."""
    connection = sqlite3.connect(str(path))
    try:
        for target in range(1, version + 1):
            migration = store.MIGRATIONS[target]
            if callable(migration):
                migration(connection)
            else:
                connection.executescript(migration)
            connection.execute(f"PRAGMA user_version = {int(target)}")
            connection.commit()
        connection.execute(
            "INSERT INTO accounts (id, address, imap_host, imap_port, imap_security,"
            " username, created_at, updated_at) VALUES"
            " ('acc_legacy', 'old@b.com', 'imap.b.com', 993, 'ssl', 'old@b.com',"
            " '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _user_version(path: Path) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


class BackupFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.db_path = self.data_dir / "mail.db"
        self.backup_dir = self.data_dir / "backups"
        _make_source_db(self.db_path)

    def test_backup_is_openable_and_contains_the_same_schema_and_rows(self):
        result = backup.create_backup(self.db_path, self.backup_dir)
        path = Path(result["path"])

        self.assertTrue(path.is_file())
        self.assertTrue(path.name.startswith("mail-"))
        self.assertTrue(path.name.endswith(".db"))
        self.assertEqual(result["size"], path.stat().st_size)
        self.assertGreater(result["size"], 0)
        self.assertRegex(result["createdAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        self.assertEqual(_tables(path), _tables(self.db_path))
        self.assertEqual(_user_version(path), 3)

        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                connection.execute("SELECT id, address FROM accounts").fetchall(),
                [("acc_1", "a@b.com")],
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            connection.close()

        leftovers = [p.name for p in self.backup_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_repeated_backups_in_the_same_second_get_distinct_files(self):
        fixed = datetime(2026, 11, 5, 0, 15, 0, tzinfo=timezone.utc)
        first = backup.create_backup(self.db_path, self.backup_dir, now=fixed)
        second = backup.create_backup(self.db_path, self.backup_dir, now=fixed)

        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(Path(first["path"]).name, "mail-20261105-001500.db")
        self.assertEqual(Path(second["path"]).name, "mail-20261105-001500-2.db")
        self.assertEqual(first["createdAt"], "2026-11-05T00:15:00Z")
        self.assertEqual(first["createdAt"], second["createdAt"])
        self.assertTrue(Path(first["path"]).is_file())
        self.assertTrue(Path(second["path"]).is_file())

    def test_list_is_newest_first_and_prune_keeps_newest_n(self):
        base = datetime(2026, 11, 5, 1, 0, 0, tzinfo=timezone.utc)
        created = [
            backup.create_backup(
                self.db_path, self.backup_dir, now=base + timedelta(minutes=minute)
            )
            for minute in range(5)
        ]

        def expected_name(minute: int) -> str:
            return f"mail-20261105-01{minute:02d}00.db"

        listed = backup.list_backups(self.backup_dir)
        self.assertEqual([entry["createdAt"] for entry in listed], sorted(
            (entry["createdAt"] for entry in listed), reverse=True
        ))
        self.assertEqual(Path(listed[0]["path"]).name, expected_name(4))

        removed = backup.prune_backups(self.backup_dir, keep=2)
        self.assertEqual(len(removed), 3)
        remaining = {p.name for p in self.backup_dir.glob("mail-*.db")}
        self.assertEqual(remaining, {expected_name(3), expected_name(4)})
        self.assertEqual(
            {Path(path).name for path in removed},
            {expected_name(0), expected_name(1), expected_name(2)},
        )
        self.assertEqual(len(created), 5)

        backup.prune_backups(self.backup_dir, keep=0)
        self.assertEqual(list(self.backup_dir.glob("mail-*.db")), [])
        self.assertEqual(backup.list_backups(self.backup_dir), [])

    def test_missing_source_database_is_an_error(self):
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.data_dir / "missing.db", self.backup_dir)

    def test_failed_rename_leaves_no_temporary_file(self):
        with mock.patch("mail_core.backup.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(backup.BackupError) as context:
                backup.create_backup(self.db_path, self.backup_dir)
        self.assertIn("backup failed", str(context.exception))
        self.assertEqual(list(self.backup_dir.iterdir()), [])

    def test_unusable_backup_directory_is_an_error(self):
        self.backup_dir.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.db_path, self.backup_dir)
        self.assertEqual(backup.list_backups(self.backup_dir), [])


class StartupBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.db_path = self.data_dir / "mail.db"

    def test_old_schema_is_backed_up_before_migration(self):
        _make_schema_at(self.db_path, OLD_SCHEMA)

        result = backup_before_migration(self.data_dir)

        self.assertIsNotNone(result)
        backup_path = Path(result["path"])
        self.assertTrue(backup_path.is_file())
        self.assertEqual(_user_version(backup_path), OLD_SCHEMA)

        connection = sqlite3.connect(str(backup_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT address FROM accounts WHERE id = 'acc_legacy'"
                ).fetchone(),
                ("old@b.com",),
            )
        finally:
            connection.close()

        # The real migration still runs and keeps the legacy row.
        migrated = store.Store(self.db_path)
        try:
            self.assertEqual(migrated.schema_version, store.SCHEMA_VERSION)
        finally:
            migrated.close()
        self.assertEqual(_user_version(self.db_path), store.SCHEMA_VERSION)

        connection = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT address FROM accounts WHERE id = 'acc_legacy'"
                ).fetchone(),
                ("old@b.com",),
            )
        finally:
            connection.close()

        # Backups already current: nothing more to do on the next start.
        self.assertIsNone(backup_before_migration(self.data_dir))
        self.assertEqual(len(backup.list_backups(self.data_dir / "backups")), 1)

    def test_main_backs_up_before_constructing_the_service(self):
        _make_schema_at(self.db_path, OLD_SCHEMA)

        with mock.patch("sys.stdin", io.StringIO("")), mock.patch(
            "sys.stdout", io.StringIO()
        ):
            exit_code = main(["--data-dir", str(self.data_dir)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(_user_version(self.db_path), store.SCHEMA_VERSION)
        backups = backup.list_backups(self.data_dir / "backups")
        self.assertEqual(len(backups), 1)
        self.assertEqual(_user_version(Path(backups[0]["path"])), OLD_SCHEMA)

    def test_new_and_current_databases_need_no_backup(self):
        self.assertIsNone(backup_before_migration(self.data_dir / "missing"))

        current = store.Store(self.db_path)
        current.close()
        self.assertIsNone(backup_before_migration(self.data_dir))
        self.assertFalse((self.data_dir / "backups").exists())

    def test_backup_failure_refuses_migration(self):
        _make_schema_at(self.db_path, OLD_SCHEMA)
        (self.data_dir / "backups").write_text("blocked", encoding="utf-8")

        with self.assertRaises(backup.BackupError) as context:
            backup_before_migration(self.data_dir)
        self.assertIn("unprotected database", str(context.exception))
        # The live database is untouched by the failed attempt.
        self.assertEqual(_user_version(self.db_path), OLD_SCHEMA)


class AdminBackupRpcTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.service = Service(self.data_dir)
        self.addCleanup(self.service.close)

    def test_admin_backup_creates_a_backup_and_lists_it(self):
        self.assertEqual(self.service.dispatch("admin.backups", {}), {"backups": []})

        first = self.service.dispatch("admin.backup", {})
        self.assertEqual(set(first), {"path", "size", "createdAt"})
        backup_path = Path(first["path"])
        self.assertTrue(backup_path.is_file())
        self.assertEqual(backup_path.parent, self.data_dir.resolve() / "backups")
        self.assertEqual(first["size"], backup_path.stat().st_size)

        listed = self.service.dispatch("admin.backups", {})["backups"]
        self.assertEqual([entry["path"] for entry in listed], [first["path"]])

        second = self.service.dispatch("admin.backup", {})
        listed = self.service.dispatch("admin.backups", {})["backups"]
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0]["path"], second["path"])
        self.assertEqual(listed[1]["path"], first["path"])

    def test_admin_backup_keeps_only_the_latest_ten(self):
        for _ in range(backup.DEFAULT_KEEP + 2):
            self.service.dispatch("admin.backup", {})

        listed = self.service.dispatch("admin.backups", {})["backups"]
        self.assertEqual(len(listed), backup.DEFAULT_KEEP)
        on_disk = list((self.data_dir / "backups").glob("mail-*.db"))
        self.assertEqual(len(on_disk), backup.DEFAULT_KEEP)
        self.assertEqual(
            [entry["createdAt"] for entry in listed],
            sorted((entry["createdAt"] for entry in listed), reverse=True),
        )

    def test_admin_backup_failure_is_internal_and_keeps_the_service_usable(self):
        (self.data_dir / "backups").write_text("blocked", encoding="utf-8")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("admin.backup", {})
        self.assertEqual(context.exception.code, "INTERNAL")
        self.assertIn("backup", context.exception.message.lower())

        self.assertEqual(self.service.dispatch("sync.status", {})["counters"]["accounts"], 0)


if __name__ == "__main__":
    unittest.main()
