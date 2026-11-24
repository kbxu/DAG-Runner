from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.database import (
    Mysql8StateDatabase,
    StateDatabase,
    create_state_database,
    load_database_config,
    reset_state_database,
)
from dagrunner.database.base import SCHEMA
from dagrunner.database.mysql8 import _format_placeholders


class DatabaseBackendTests(unittest.TestCase):
    def test_factory_defaults_to_sqlite(self) -> None:
        with TemporaryDirectory() as directory:
            database = create_state_database(Path(directory) / "state.db")
            self.assertIsInstance(database, StateDatabase)
            self.assertEqual(database.state_dir, Path(directory))

    def test_factory_accepts_sqlite_mapping(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            database = create_state_database({"type": "sqlite", "path": str(path)})
            self.assertIsInstance(database, StateDatabase)

    def test_factory_rejects_unknown_type(self) -> None:
        with self.assertRaises(ValueError):
            create_state_database({"type": "oracle"})

    def test_load_database_config_reads_sqlite_path(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("db: state.db\nlogs: logs\n", encoding="utf-8")
            self.assertEqual(load_database_config(path), "state.db")

    def test_load_database_config_reads_mapping_and_state_dir(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "db:\n"
                "  type: mysql8\n"
                "  host: 127.0.0.1\n"
                "  database: dagr\n"
                "state_dir: var\n",
                encoding="utf-8",
            )
            self.assertEqual(
                load_database_config(path),
                {
                    "type": "mysql8",
                    "host": "127.0.0.1",
                    "database": "dagr",
                    "state_dir": "var",
                },
            )

    def test_load_database_config_rejects_missing_file(self) -> None:
        with self.assertRaises(ValueError):
            load_database_config(Path("does-not-exist-config.yaml"))

    def test_reset_state_database_sqlite(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            database = create_state_database(path)
            database.upsert_user("u", "h")
            reset_state_database(path)
            self.assertFalse(path.exists())
            self.assertFalse(Path(str(path) + "-wal").exists())
            self.assertFalse(Path(str(path) + "-shm").exists())

    def test_reset_state_database_sqlite_mapping(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            create_state_database(path).upsert_user("u", "h")
            reset_state_database({"type": "sqlite", "path": str(path)})
            self.assertFalse(path.exists())

    def test_placeholder_formatting_ignores_string_literals(self) -> None:
        sql = "SELECT * FROM t WHERE a = ? AND b = '?' AND c = ?"
        self.assertEqual(
            _format_placeholders(sql),
            "SELECT * FROM t WHERE a = %s AND b = '?' AND c = %s",
        )

    def test_placeholder_formatting_escapes_percent_in_literals(self) -> None:
        sql = "SELECT * FROM t WHERE name NOT LIKE '【%】%' AND a = ?"
        self.assertEqual(
            _format_placeholders(sql),
            "SELECT * FROM t WHERE name NOT LIKE '【%%】%%' AND a = %s",
        )

    def test_sqlite_schema_keeps_native_types(self) -> None:
        database = object.__new__(StateDatabase)
        database.dialect = "sqlite"
        ddl = "\n".join(database._render_create_table(table) for table in SCHEMA)
        self.assertIn("id INTEGER PRIMARY KEY AUTOINCREMENT", ddl)
        self.assertIn("TEXT NOT NULL DEFAULT 'manual'", ddl)

    def test_mysql_schema_uses_portable_types(self) -> None:
        database = object.__new__(Mysql8StateDatabase)
        database.dialect = "mysql8"
        ddl = "\n".join(database._render_create_table(table) for table in SCHEMA)
        self.assertIn("id INT AUTO_INCREMENT", ddl)
        self.assertIn("PRIMARY KEY (id)", ddl)
        self.assertIn("VARCHAR(255)", ddl)
        self.assertIn("depends_json VARCHAR(4000) NOT NULL DEFAULT '[]'", ddl)
        self.assertNotIn("AUTOINCREMENT", ddl)
        self.assertNotIn("TEXT NOT NULL DEFAULT", ddl)

    def test_mysql_upsert_uses_duplicate_key_update(self) -> None:
        database = object.__new__(Mysql8StateDatabase)
        database.dialect = "mysql8"
        database.supports_on_conflict = False
        recorded: dict[str, object] = {}

        class Connection:
            def execute(self, sql: str, params: object = ()) -> None:
                recorded["sql"] = sql
                recorded["params"] = params

        database._upsert(
            Connection(),
            "users",
            ("username", "password_hash"),
            ("u", "h"),
            ("username",),
            ("password_hash",),
        )
        self.assertIn("ON DUPLICATE KEY UPDATE", str(recorded["sql"]))
        self.assertIn("VALUES(password_hash)", str(recorded["sql"]))


if __name__ == "__main__":
    unittest.main()
