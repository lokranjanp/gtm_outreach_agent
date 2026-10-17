"""Small local repository. Graph checkpoints live in a separate SQLite file."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from outreach.models import ProductProfile, now


class Store:
    def __init__(self, data_dir: str | Path):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "outreach.sqlite"
        self.checkpoints = self.directory / "checkpoints.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_product(self, product: ProductProfile):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO products VALUES (?, ?)",
                (product.id, product.model_dump_json()),
            )

    def products(self) -> list[ProductProfile]:
        with self.connect() as db:
            return [
                ProductProfile.model_validate_json(row[0])
                for row in db.execute("SELECT payload FROM products ORDER BY rowid")
            ]

    def create_run(self, run_id: str, company: str, mode: str):
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, company, mode, "new", now(), now()),
            )

    def update_run(self, run_id: str, status: str, company: str | None = None):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?, updated_at=?, company=COALESCE(?, company) WHERE id=?",
                (status, now(), company, run_id),
            )

    def runs(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY updated_at DESC")]

    def run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Run not found.")
        return dict(row)


def json_data(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
