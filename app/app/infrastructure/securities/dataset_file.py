"""把数据集的各张表写成一个 SQLite 文件。"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


class SqliteDatasetFileWriter:
    def write(
        self, tables: dict[str, tuple[list, list]], metadata: dict[str, str]
    ) -> bytes:
        with tempfile.TemporaryDirectory(prefix="security-dataset-") as directory:
            path = Path(directory) / "dataset.sqlite"
            connection = sqlite3.connect(path)
            try:
                for name, (columns, rows) in tables.items():
                    spec = ", ".join(f'"{c}" {t}' for c, t in columns)
                    connection.execute(f'CREATE TABLE "{name}" ({spec})')
                    marks = ", ".join("?" for _ in columns)
                    connection.executemany(
                        f'INSERT INTO "{name}" VALUES ({marks})', rows
                    )
                connection.execute(
                    "CREATE TABLE dataset_metadata (key TEXT, value TEXT)"
                )
                connection.executemany(
                    "INSERT INTO dataset_metadata VALUES (?, ?)",
                    sorted(metadata.items()),
                )
                connection.commit()
                connection.execute("VACUUM")
            finally:
                connection.close()
            return path.read_bytes()
