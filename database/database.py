"""Minimal SQLite settings storage for future application settings."""
from __future__ import annotations

import sqlite3
from typing import Any

from config.config import DATABASE_PATH, ensure_runtime_directories


class Database:
    def initialize(self) -> None:
        ensure_runtime_directories()
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thang TEXT,
                    a TEXT,
                    khmhd TEXT,
                    hoa_don TEXT,
                    date TEXT,
                    dia_chi TEXT,
                    mst1 TEXT,
                    mst2 TEXT,
                    "row" INTEGER NOT NULL,
                    status INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                )
                """
            )
            invoice_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(invoices)")
            }
            if "ten_khach_hang" not in invoice_columns:
                connection.execute("ALTER TABLE invoices ADD COLUMN ten_khach_hang TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_invoices_hoa_don ON invoices(hoa_don)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processing_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
                    current_step TEXT NOT NULL DEFAULT 'Chờ xử lý',
                    started_at TEXT,
                    finished_at TEXT,
                    duration_seconds REAL,
                    error TEXT,
                    FOREIGN KEY(invoice_id) REFERENCES invoices(id)
                )
                """
            )
            job_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(processing_jobs)")
            }
            if "duration_seconds" not in job_columns:
                connection.execute("ALTER TABLE processing_jobs ADD COLUMN duration_seconds REAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    level TEXT NOT NULL DEFAULT 'INFO',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES processing_jobs(id)
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id, id)")

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def insert_invoices(self, invoices: list[dict[str, Any]]) -> int:
        if not invoices:
            return 0
        columns = "thang, a, khmhd, hoa_don, date, ten_khach_hang, dia_chi, mst1, mst2, \"row\", status, error"
        values = [
            (
                item["thang"], item["a"], item["khmhd"], item["hoa_don"], item["date"],
                item["ten_khach_hang"], item["dia_chi"], item["mst1"], item["mst2"], item["row"], 0, None,
            )
            for item in invoices
        ]
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.executemany(f"INSERT INTO invoices ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
        return len(values)

    def get_invoices(
        self,
        search: str,
        limit: int,
        offset: int,
        mst2_filter: str = "all",
        status_filter: int | None = None,
    ) -> tuple[list[sqlite3.Row], int]:
        term = f"%{search.strip()}%"
        conditions = ["(hoa_don LIKE ? OR khmhd LIKE ? OR dia_chi LIKE ? OR mst1 LIKE ? OR mst2 LIKE ?)"]
        params: list[object] = [term] * 5
        if mst2_filter == "with":
            conditions.append("TRIM(COALESCE(mst2, '')) <> ''")
        elif mst2_filter == "without":
            conditions.append("TRIM(COALESCE(mst2, '')) = ''")
        if status_filter is not None:
            conditions.append("status = ?")
            params.append(status_filter)
        where = " AND ".join(conditions)
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory = sqlite3.Row
            total = connection.execute(f"SELECT COUNT(*) FROM invoices WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM invoices WHERE {where} ORDER BY id ASC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return rows, total

    def get_first_pending_invoice(self) -> sqlite3.Row | None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                """
                SELECT * FROM invoices
                WHERE status = 0 AND TRIM(COALESCE(mst2, '')) <> ''
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()

    def claim_next_invoice(
        self, run_mode: str, run_limit: int, retry_errors_only: bool = False
    ) -> sqlite3.Row | None:
        """Atomically claim one eligible invoice for a worker."""
        with sqlite3.connect(DATABASE_PATH, isolation_level=None) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                if run_mode == "custom":
                    started = connection.execute(
                        "SELECT COUNT(*) FROM invoices WHERE status = 1"
                    ).fetchone()[0]
                    if started >= run_limit:
                        connection.execute("COMMIT")
                        return None
                retry_filter = """
                      AND EXISTS (
                          SELECT 1 FROM processing_jobs AS retry_job
                          WHERE retry_job.invoice_id = candidate.id
                            AND retry_job.status = 'queued'
                      )
                """ if retry_errors_only else ""
                duplicate_filter = """
                      AND NOT EXISTS (
                          SELECT 1
                          FROM invoices AS claimed
                          WHERE claimed.hoa_don = candidate.hoa_don
                            AND claimed.status IN (1, 2)
                      )
                """
                row = connection.execute(
                    f"""
                    SELECT candidate.*
                    FROM invoices AS candidate
                    WHERE candidate.status = 0
                      AND TRIM(COALESCE(candidate.mst2, '')) <> ''
                    {duplicate_filter}
                    {retry_filter}
                    ORDER BY candidate.id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                updated = connection.execute(
                    "UPDATE invoices SET status = 1 WHERE id = ? AND status = 0", (row["id"],)
                ).rowcount
                if updated != 1:
                    connection.execute("ROLLBACK")
                    return None
                connection.execute("COMMIT")
                return row
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def get_invoice(self, invoice_id: int) -> sqlite3.Row | None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()

    def count_started_invoices(self) -> int:
        with sqlite3.connect(DATABASE_PATH) as connection:
            return connection.execute("SELECT COUNT(*) FROM invoices WHERE status = 1").fetchone()[0]

    def count_pending_invoices(self) -> int:
        with sqlite3.connect(DATABASE_PATH) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM invoices WHERE status = 0 AND TRIM(COALESCE(mst2, '')) <> ''"
            ).fetchone()[0]

    def get_invoice_status_counts(self) -> dict[int, int]:
        counts = {0: 0, 1: 0, 2: 0}
        with sqlite3.connect(DATABASE_PATH) as connection:
            for status, total in connection.execute("SELECT status, COUNT(*) FROM invoices GROUP BY status"):
                if status in counts:
                    counts[status] = total
        return counts

    def clear_invoices(self) -> None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute("DELETE FROM job_logs")
            connection.execute("DELETE FROM processing_jobs")
            connection.execute("DELETE FROM invoices")

    def delete_invoices_by_ids(self, invoice_ids: list[int]) -> int:
        """Delete imported invoices and all jobs/logs that belong to them."""
        ids = [int(invoice_id) for invoice_id in invoice_ids]
        if not ids:
            return 0

        placeholders = ", ".join("?" for _ in ids)
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute(
                f"""
                DELETE FROM job_logs
                WHERE job_id IN (
                    SELECT id FROM processing_jobs
                    WHERE invoice_id IN ({placeholders})
                )
                """,
                ids,
            )
            connection.execute(
                f"DELETE FROM processing_jobs WHERE invoice_id IN ({placeholders})",
                ids,
            )
            cursor = connection.execute(
                f"DELETE FROM invoices WHERE id IN ({placeholders})",
                ids,
            )
            return cursor.rowcount

    def reset_all_invoice_statuses(self) -> int:
        with sqlite3.connect(DATABASE_PATH) as connection:
            # Reset must also remove the per-invoice error text; otherwise the
            # table still looks failed even though its status was returned to 0.
            cursor = connection.execute("UPDATE invoices SET status = 0, error = NULL")
            connection.execute("DELETE FROM job_logs")
            connection.execute("DELETE FROM processing_jobs")
            return cursor.rowcount

    def sync_processing_jobs(self) -> int:
        """Create one queued job per imported invoice without changing invoice status."""
        with sqlite3.connect(DATABASE_PATH) as connection:
            before = connection.total_changes
            connection.execute(
                """
                INSERT OR IGNORE INTO processing_jobs(invoice_id, status, progress, current_step)
                SELECT id, 'queued', 0, 'Chờ xử lý'
                FROM invoices
                WHERE status = 0
                """
            )
            return connection.total_changes - before

    def get_processing_jobs(self, status: str, limit: int, offset: int) -> tuple[list[sqlite3.Row], int]:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory = sqlite3.Row
            total = connection.execute(
                "SELECT COUNT(*) FROM processing_jobs WHERE status = ?", (status,)
            ).fetchone()[0]
            jobs = connection.execute(
                """
                SELECT jobs.*, invoices.hoa_don, invoices.date, invoices.ten_khach_hang
                FROM processing_jobs AS jobs
                JOIN invoices ON invoices.id = jobs.invoice_id
                WHERE jobs.status = ?
                ORDER BY jobs.id ASC
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            ).fetchall()
        return jobs, total

    def get_job_counts(self) -> dict[str, int]:
        counts = {"running": 0, "completed": 0, "error": 0}
        with sqlite3.connect(DATABASE_PATH) as connection:
            for status, total in connection.execute(
                "SELECT status, COUNT(*) FROM processing_jobs GROUP BY status"
            ):
                if status in counts:
                    counts[status] = total
        return counts

    def get_job_logs(self, job_id: int) -> list[sqlite3.Row]:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM job_logs WHERE job_id = ? ORDER BY id ASC", (job_id,)
            ).fetchall()

    def get_processing_job(self, job_id: int) -> sqlite3.Row | None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()

    def recover_interrupted_jobs(self) -> int:
        """Mark jobs left running by an unexpected app shutdown as errors."""
        with sqlite3.connect(DATABASE_PATH) as connection:
            rows = connection.execute(
                "SELECT id FROM processing_jobs WHERE status = 'running'"
            ).fetchall()
            for (job_id,) in rows:
                connection.execute(
                    """
                    UPDATE processing_jobs
                    SET status = 'error', current_step = 'Bị gián đoạn',
                        error = 'Ứng dụng hoặc trình duyệt bị tắt đột ngột.',
                        finished_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (job_id,),
                )
                connection.execute(
                    "INSERT INTO job_logs(job_id, level, message) VALUES (?, 'ERROR', ?)",
                    (job_id, "Job bị gián đoạn do ứng dụng hoặc trình duyệt tắt đột ngột."),
                )
            return len(rows)

    def retry_job(self, job_id: int) -> int | None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            row = connection.execute(
                "SELECT invoice_id FROM processing_jobs WHERE id = ? AND status = 'error'", (job_id,)
            ).fetchone()
            if row is None:
                return None
            invoice_id = row[0]
            connection.execute("UPDATE invoices SET status = 0 WHERE id = ?", (invoice_id,))
            connection.execute(
                """
                UPDATE processing_jobs
                SET status = 'queued', progress = 0, current_step = 'Chờ chạy lại', error = NULL,
                    started_at = NULL, finished_at = NULL, duration_seconds = NULL
                WHERE id = ?
                """,
                (job_id,),
            )
            connection.execute(
                "INSERT INTO job_logs(job_id, level, message) VALUES (?, 'INFO', ?)",
                (job_id, "Đã yêu cầu chạy lại job."),
            )
            return invoice_id

    def retry_all_failed_jobs(self) -> int:
        with sqlite3.connect(DATABASE_PATH) as connection:
            rows = connection.execute(
                """
                SELECT retry_job.id, retry_job.invoice_id
                FROM processing_jobs AS retry_job
                JOIN invoices AS candidate ON candidate.id = retry_job.invoice_id
                WHERE retry_job.status = 'error'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM invoices AS duplicate_invoice
                      WHERE duplicate_invoice.hoa_don = candidate.hoa_don
                        AND duplicate_invoice.id <> candidate.id
                        AND duplicate_invoice.status IN (1, 2)
                  )
                """
            ).fetchall()
            if not rows:
                return 0
            invoice_ids = [row[1] for row in rows]
            placeholders = ", ".join("?" for _ in invoice_ids)
            connection.execute(
                f"UPDATE invoices SET status = 0 WHERE id IN ({placeholders})", invoice_ids
            )
            job_ids = [row[0] for row in rows]
            job_placeholders = ", ".join("?" for _ in job_ids)
            connection.execute(
                f"""
                UPDATE processing_jobs
                SET status = 'queued', progress = 0, current_step = 'Chờ chạy lại', error = NULL,
                    started_at = NULL, finished_at = NULL, duration_seconds = NULL
                WHERE id IN ({job_placeholders})
                """,
                job_ids,
            )
            connection.executemany(
                "INSERT INTO job_logs(job_id, level, message) VALUES (?, 'INFO', ?)",
                [(job_id, "Đã yêu cầu chạy lại cùng nhóm job lỗi.") for job_id in job_ids],
            )
            return len(rows)

    def start_demo_job(self, invoice_id: int) -> int:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute("UPDATE invoices SET status = 1, error = NULL WHERE id = ?", (invoice_id,))
            connection.execute(
                """
                INSERT INTO processing_jobs(invoice_id, status, progress, current_step, started_at, finished_at, error)
                VALUES (?, 'running', 5, 'Đang khởi tạo', CURRENT_TIMESTAMP, NULL, NULL)
                ON CONFLICT(invoice_id) DO UPDATE SET
                    status = 'running', progress = 5, current_step = 'Đang khởi tạo',
                    started_at = CURRENT_TIMESTAMP, finished_at = NULL, duration_seconds = NULL, error = NULL
                """,
                (invoice_id,),
            )
            return connection.execute(
                "SELECT id FROM processing_jobs WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()[0]

    def reset_demo_job(self, invoice_id: int, duration_seconds: float, job_status: str, error: str | None = None) -> None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            invoice_status = 1 if job_status == "completed" else 2
            connection.execute(
                "UPDATE invoices SET status = ?, error = ? WHERE id = ?",
                (invoice_status, error, invoice_id),
            )
            connection.execute(
                """
                UPDATE processing_jobs
                SET status = ?, progress = CASE WHEN ? = 'completed' THEN 100 ELSE progress END,
                    current_step = CASE WHEN ? = 'completed' THEN 'Hoàn tất' ELSE 'Lỗi xử lý' END,
                    finished_at = CURRENT_TIMESTAMP, duration_seconds = ?, error = ?
                WHERE invoice_id = ?
                """,
                (job_status, job_status, job_status, duration_seconds, error, invoice_id),
            )

    def add_job_log(self, job_id: int, level: str, message: str) -> None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute(
                "INSERT INTO job_logs(job_id, level, message) VALUES (?, ?, ?)",
                (job_id, level, message),
            )

    def update_job_progress(self, job_id: int, progress: int, current_step: str) -> None:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute(
                """
                UPDATE processing_jobs
                SET progress = ?, current_step = ?
                WHERE id = ? AND status = 'running'
                """,
                (progress, current_step, job_id),
            )
