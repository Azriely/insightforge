"""SQLite persistence layer for InsightForge."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def _db_path() -> str:
    path = Path(os.environ.get("DATABASE_PATH", "data/insightforge.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@contextmanager
def get_db():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                access_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                client_name TEXT,
                client_email TEXT,
                company_name TEXT NOT NULL,
                industry TEXT NOT NULL,
                analysis_type TEXT NOT NULL DEFAULT 'comprehensive',
                question TEXT NOT NULL,
                source TEXT DEFAULT 'direct',
                stripe_session_id TEXT,
                stripe_payment_id TEXT,
                amount_cents INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                report_id TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                company_name TEXT NOT NULL,
                industry TEXT NOT NULL,
                question TEXT NOT NULL,
                analysis_type TEXT NOT NULL,
                executive_summary TEXT,
                full_report TEXT,
                sections_json TEXT DEFAULT '{}',
                key_insights_json TEXT DEFAULT '[]',
                recommendations_json TEXT DEFAULT '[]',
                estimated_tokens_used INTEGER DEFAULT 0,
                generation_time_seconds REAL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                joined_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_reports INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                revenue_cents INTEGER DEFAULT 0
            );

            INSERT OR IGNORE INTO usage_stats (id, total_reports, total_tokens, revenue_cents)
            VALUES (1, 0, 0, 0);
        """)


# ---- Orders ----

def save_order(order: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO orders
               (id, access_key, status, client_name, client_email, company_name,
                industry, analysis_type, question, source, stripe_session_id,
                stripe_payment_id, amount_cents, created_at, completed_at, report_id, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order["id"], order["access_key"], order["status"],
                order.get("client_name"), order.get("client_email"),
                order["company_name"], order["industry"],
                order.get("analysis_type", "comprehensive"),
                order["question"], order.get("source", "direct"),
                order.get("stripe_session_id"), order.get("stripe_payment_id"),
                order.get("amount_cents", 0), order["created_at"],
                order.get("completed_at"), order.get("report_id"),
                order.get("error"),
            ),
        )


def get_order(order_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def get_order_by_stripe_session(session_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE stripe_session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def update_order(order_id: str, **kwargs):
    if not kwargs:
        return
    with get_db() as conn:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [order_id]
        conn.execute(f"UPDATE orders SET {sets} WHERE id = ?", vals)


def list_orders() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


# ---- Reports ----

def save_report(report: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reports
               (id, company_name, industry, question, analysis_type, executive_summary,
                full_report, sections_json, key_insights_json, recommendations_json,
                estimated_tokens_used, generation_time_seconds, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                report["id"], report["company_name"], report["industry"],
                report["question"], report.get("analysis_type", "comprehensive"),
                report.get("executive_summary", ""),
                report.get("full_report", ""),
                json.dumps(report.get("sections", {})),
                json.dumps(report.get("key_insights", [])),
                json.dumps(report.get("recommendations", [])),
                report.get("estimated_tokens_used", 0),
                report.get("generation_time_seconds", 0),
                report["created_at"],
            ),
        )


def get_report(report_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not row:
            return None
        r = dict(row)
        r["sections"] = json.loads(r.pop("sections_json"))
        r["key_insights"] = json.loads(r.pop("key_insights_json"))
        r["recommendations"] = json.loads(r.pop("recommendations_json"))
        return r


def list_reports() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, company_name, industry, analysis_type, created_at "
            "FROM reports ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---- Waitlist ----

def add_to_waitlist(email: str, joined_at: str) -> bool:
    """Returns True if added, False if already exists."""
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO waitlist (email, joined_at) VALUES (?, ?)",
                (email, joined_at),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_waitlist() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT email, joined_at FROM waitlist ORDER BY id").fetchall()
        return [dict(r) for r in rows]


# ---- Usage Stats ----

def increment_stats(reports: int = 0, tokens: int = 0, revenue_cents: int = 0):
    with get_db() as conn:
        conn.execute(
            """UPDATE usage_stats SET
                total_reports = total_reports + ?,
                total_tokens = total_tokens + ?,
                revenue_cents = revenue_cents + ?
               WHERE id = 1""",
            (reports, tokens, revenue_cents),
        )


def get_stats() -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM usage_stats WHERE id = 1").fetchone()
        return dict(row) if row else {"total_reports": 0, "total_tokens": 0, "revenue_cents": 0}
