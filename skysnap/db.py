from __future__ import annotations

import json
import socket
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Iterator

from skysnap.models import EnrichmentResult, FuzzyDuplicateDecision, ProjectSimilarityDecision
from skysnap.tzutil import get_timezone


class LeadStatus(StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    skipped_duplicate = "skipped_duplicate"
    processed_success = "processed_success"
    processed_failed = "processed_failed"
    enriched_manually = "enriched_manually"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Lead:
    id: int
    source: str
    source_message_id: str | None
    source_received_at: str | None
    project_name: str
    company_name: str | None
    country: str | None
    city: str | None
    project_value: str | None
    project_phase: str | None
    project_url: str | None
    raw_payload_json: dict[str, Any]
    icp_score: int
    icp_reason: str | None
    status: LeadStatus
    created_at: str
    updated_at: str
    last_error: str | None


@dataclass
class LeadExport:
    lead_id: int
    enrichment_json: str
    duplicate_decision_json: str | None
    exported_at: str
    hubspot_deal_id: str | None = None
    hubspot_company_id: str | None = None
    hubspot_contact_id: str | None = None
    hubspot_task_id: str | None = None
    hubspot_ticket_id: str | None = None
    hubspot_synced_at: str | None = None
    hubspot_last_error: str | None = None
    project_similarity_json: str | None = None
    hubspot_note_hash: str | None = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_message_id TEXT,
  source_received_at TEXT,

  project_name TEXT NOT NULL,
  company_name TEXT,
  country TEXT,
  city TEXT,
  project_value TEXT,
  project_phase TEXT,
  project_url TEXT,

  raw_payload_json TEXT NOT NULL,
  icp_score INTEGER NOT NULL,
  icp_reason TEXT,

  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_status_score ON leads(status, icp_score DESC, id ASC);
CREATE INDEX IF NOT EXISTS idx_leads_source_msgid ON leads(source, source_message_id);

CREATE TABLE IF NOT EXISTS ingested_emails (
  message_id TEXT PRIMARY KEY,
  subject TEXT,
  received_at TEXT,
  ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_reveals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL,
  revealed_on TEXT NOT NULL,
  success INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contact_reveals_day ON contact_reveals(revealed_on);
CREATE INDEX IF NOT EXISTS idx_contact_reveals_lead ON contact_reveals(lead_id);

CREATE TABLE IF NOT EXISTS lead_exports (
  lead_id INTEGER PRIMARY KEY,
  enrichment_json TEXT NOT NULL,
  duplicate_decision_json TEXT,
  project_similarity_json TEXT,
  exported_at TEXT NOT NULL,
  hubspot_deal_id TEXT,
  hubspot_company_id TEXT,
  hubspot_contact_id TEXT,
  hubspot_task_id TEXT,
  hubspot_ticket_id TEXT,
  hubspot_synced_at TEXT,
  hubspot_last_error TEXT,
  hubspot_note_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_lead_exports_pending ON lead_exports(hubspot_deal_id);

CREATE TABLE IF NOT EXISTS db_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _stamp_origin(conn: sqlite3.Connection) -> None:
    """Record which machine created this database file.

    A database copied between machines (e.g. syncing the project folder to the
    VM) silently replaces live data, so the origin host is stored once and
    reported by ``status`` to make that mistake visible.
    """
    conn.execute(
        "INSERT OR IGNORE INTO db_meta (key, value) VALUES ('created_on_host', ?)",
        (socket.gethostname(),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO db_meta (key, value) VALUES ('created_at', ?)",
        (utc_now_iso(),),
    )
    conn.commit()


_foreign_origin_warned = False


def _warn_if_foreign_origin(conn: sqlite3.Connection) -> None:
    """Warn loudly when the database file came from another machine."""
    global _foreign_origin_warned
    if _foreign_origin_warned:
        return
    origin = get_meta(conn, "created_on_host")
    host = socket.gethostname()
    if not origin or origin == host:
        return
    _foreign_origin_warned = True
    print(
        f"[skysnap] WARNING: database was created on '{origin}' but this is "
        f"'{host}'. A folder copy has probably overwritten the local database. "
        "Exclude data/ from any file sync to the VM.",
        file=sys.stderr,
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM db_meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO db_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )
    conn.commit()


def adopt_database(conn: sqlite3.Connection) -> dict[str, Any]:
    """Record this machine as the owner of the database file."""
    previous = get_meta(conn, "created_on_host")
    host = socket.gethostname()
    set_meta(conn, "created_on_host", host)
    set_meta(conn, "adopted_at", utc_now_iso())
    return {"previous_host": previous, "host": host}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    for ddl in (
        "ALTER TABLE lead_exports ADD COLUMN hubspot_task_id TEXT",
        "ALTER TABLE lead_exports ADD COLUMN project_similarity_json TEXT",
        "ALTER TABLE lead_exports ADD COLUMN hubspot_ticket_id TEXT",
        "ALTER TABLE lead_exports ADD COLUMN hubspot_note_hash TEXT",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _open_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Wait for other SkySnap processes / DB viewers instead of failing the run.
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(SCHEMA_SQL)
    _migrate_schema(conn)
    _stamp_origin(conn)
    _warn_if_foreign_origin(conn)
    return conn


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the write-ahead log into the main database file.

    Committed data lives in the ``-wal`` sidecar until a checkpoint runs, so
    checkpointing after write-heavy commands keeps it safe from sidecar loss.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except sqlite3.DatabaseError:
        pass


def _quarantine_wal_sidecars(db_path: str) -> list[str]:
    """Move (never delete) WAL sidecars aside.

    A ``-wal`` file holds transactions that are committed but not yet
    checkpointed. Deleting it discards that data, so it is renamed instead and
    can be restored by putting it back next to the database.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved: list[str] = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if not sidecar.exists():
            continue
        backup = Path(f"{db_path}{suffix}.corrupt-{stamp}")
        sidecar.replace(backup)
        moved.append(str(backup.resolve()))
    return moved


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    if not Path(db_path).exists():
        return _open_connection(db_path)
    try:
        conn = _open_connection(db_path)
    except sqlite3.OperationalError:
        # Locked/busy/permission problems are transient — never touch the WAL.
        raise
    except sqlite3.DatabaseError:
        moved = _quarantine_wal_sidecars(db_path)
        if not moved:
            raise
        return _open_connection(db_path)
    if _database_quick_check(conn):
        return conn
    conn.close()
    moved = _quarantine_wal_sidecars(db_path)
    if moved:
        conn = _open_connection(db_path)
        if _database_quick_check(conn):
            return conn
        conn.close()
    raise sqlite3.DatabaseError(
        "database disk image is malformed; run: python -m skysnap repair-db"
    )


def connect_or_repair(db_path: str) -> tuple[sqlite3.Connection, dict[str, Any]]:
    """Open DB; on corruption run repair-db (WAL sidecars, then recreate)."""
    try:
        return connect(db_path), {"action": "none"}
    except sqlite3.DatabaseError:
        repair = repair_database(db_path)
        if repair.get("action") == "recreated":
            raise RuntimeError(
                "SQLite database was corrupt and recreated empty. "
                "Re-import leads (import-kompass / ingest-email) then retry."
            ) from None
        return connect(db_path), repair


def remove_database_files(db_path: str) -> list[str]:
    """Move the SQLite database and its WAL sidecars aside (kept for recovery)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db_path}{suffix}")
        if not path.exists():
            continue
        backup = Path(f"{db_path}{suffix}.corrupt-{stamp}")
        path.replace(backup)
        moved.append(str(backup.resolve()))
    return moved


def _database_quick_check(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")
    except sqlite3.DatabaseError:
        return False


def repair_database(db_path: str) -> dict[str, Any]:
    """Repair corrupt SQLite (WAL sidecars first); recreate empty DB if needed."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _probe() -> bool:
        if not path.exists():
            return False
        try:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            ok = _database_quick_check(conn)
            conn.close()
            return ok
        except sqlite3.DatabaseError:
            return False

    if _probe():
        conn = connect(db_path)
        conn.close()
        return {
            "db_path": str(path.resolve()),
            "healthy": True,
            "action": "ok",
        }

    wal_removed = _quarantine_wal_sidecars(db_path)

    if wal_removed and _probe():
        conn = connect(db_path)
        conn.close()
        return {
            "db_path": str(path.resolve()),
            "healthy": True,
            "action": "repaired_wal_sidecars",
            "wal_sidecars_removed": wal_removed,
        }

    removed = remove_database_files(db_path)
    conn = connect(db_path)
    conn.close()
    return {
        "db_path": str(path.resolve()),
        "healthy": True,
        "action": "recreated",
        "removed_paths": removed,
        "wal_sidecars_removed": wal_removed,
        "note": (
            "Database was recreated empty. Re-import Kompass URLs and re-run ingest-email "
            "if you had local leads."
        ),
    }


def reset_or_recreate_database(db_path: str) -> dict[str, Any]:
    """Clear all leads/ingest history, or recreate the DB file if it is corrupt."""
    try:
        conn = connect(db_path)
        try:
            return reset_all_state(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        removed = remove_database_files(db_path)
        conn = connect(db_path)
        conn.close()
        return {
            "leads_deleted": 0,
            "ingested_emails_deleted": 0,
            "database_recreated": True,
            "database_recreate_reason": str(e),
            "removed_paths": removed,
        }


def _row_to_lead(row: sqlite3.Row) -> Lead:
    return Lead(
        id=int(row["id"]),
        source=row["source"],
        source_message_id=row["source_message_id"],
        source_received_at=row["source_received_at"],
        project_name=row["project_name"],
        company_name=row["company_name"],
        country=row["country"],
        city=row["city"],
        project_value=row["project_value"],
        project_phase=row["project_phase"],
        project_url=row["project_url"],
        raw_payload_json=json.loads(row["raw_payload_json"]),
        icp_score=int(row["icp_score"]),
        icp_reason=row["icp_reason"],
        status=LeadStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_error=row["last_error"],
    )


def upsert_lead(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_message_id: str | None,
    source_received_at: str | None,
    project_name: str,
    company_name: str | None,
    country: str | None,
    city: str | None,
    project_value: str | None,
    project_phase: str | None,
    project_url: str | None,
    raw_payload_json: dict[str, Any],
    icp_score: int,
    icp_reason: str | None,
) -> int:
    now = utc_now_iso()

    # Best-effort de-dupe per email message id + project name.
    existing_id: int | None = None
    if source_message_id:
        row = conn.execute(
            "SELECT id FROM leads WHERE source=? AND source_message_id=? AND project_name=?",
            (source, source_message_id, project_name),
        ).fetchone()
        if row:
            existing_id = int(row["id"])

    if existing_id is None:
        cur = conn.execute(
            """
            INSERT INTO leads (
              source, source_message_id, source_received_at,
              project_name, company_name, country, city, project_value, project_phase, project_url,
              raw_payload_json, icp_score, icp_reason,
              status, created_at, updated_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                source_message_id,
                source_received_at,
                project_name,
                company_name,
                country,
                city,
                project_value,
                project_phase,
                project_url,
                json.dumps(raw_payload_json, ensure_ascii=False),
                int(icp_score),
                icp_reason,
                LeadStatus.pending.value,
                now,
                now,
                None,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    conn.execute(
        """
        UPDATE leads
        SET company_name=?,
            country=?,
            city=?,
            project_value=?,
            project_phase=?,
            project_url=?,
            raw_payload_json=?,
            icp_score=?,
            icp_reason=?,
            updated_at=?
        WHERE id=?
        """,
        (
            company_name,
            country,
            city,
            project_value,
            project_phase,
            project_url,
            json.dumps(raw_payload_json, ensure_ascii=False),
            int(icp_score),
            icp_reason,
            now,
            existing_id,
        ),
    )
    conn.commit()
    return existing_id


def lead_created_on(created_at: str, tz_name: str) -> date:
    dt = datetime.fromisoformat(created_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_timezone(tz_name)).date()


def iter_pending_by_icp(
    conn: sqlite3.Connection,
    *,
    min_score: int,
    include_demo: bool = False,
    batch_size: int = 20,  # kept for API compatibility; no longer used for paging
    skip_ids: Iterable[int] | None = None,
) -> Iterator[Lead]:
    """Yield pending leads ordered by ICP score (highest first).

    Implementation note: callers mutate lead statuses *while* iterating
    (Phase A exports/fails leads mid-loop). Paging with LIMIT/OFFSET over the
    live ``status='pending'`` filter therefore skipped rows as the result set
    shrank. We snapshot the ordered id list once, then re-fetch each lead and
    re-check its status at yield time. ``skip_ids`` is consulted *live* (by
    reference when a set is passed), so ids added by the caller during
    iteration are honored.
    """
    if isinstance(skip_ids, (set, frozenset)):
        skip: Iterable[int] = skip_ids  # live view — caller may add during iteration
    else:
        skip = {int(x) for x in (skip_ids or [])}
    demo_filter = "" if include_demo else " AND source != 'demo'"
    id_rows = conn.execute(
        f"""
        SELECT id FROM leads
        WHERE status=? AND icp_score >= ?{demo_filter}
        ORDER BY icp_score DESC, id ASC
        """,
        (LeadStatus.pending.value, int(min_score)),
    ).fetchall()
    for id_row in id_rows:
        lead_id = int(id_row["id"])
        if lead_id in skip:
            continue
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            continue
        lead = _row_to_lead(row)
        if lead.status != LeadStatus.pending:
            continue  # status changed since the snapshot (exported/failed/deferred)
        if lead.id in skip:
            continue
        yield lead


def get_pending_created_on(
    conn: sqlite3.Connection,
    *,
    day: date,
    tz_name: str,
    exclude_ids: Iterable[int] | None = None,
    min_score: int,
    include_demo: bool = False,
) -> list[Lead]:
    """Pending leads whose created_at falls on `day` in the given timezone."""
    exclude = {int(x) for x in (exclude_ids or [])}
    demo_filter = "" if include_demo else " AND source != 'demo'"
    rows = conn.execute(
        f"""
        SELECT * FROM leads
        WHERE status=? AND icp_score >= ?{demo_filter}
        ORDER BY icp_score DESC, id ASC
        """,
        (LeadStatus.pending.value, int(min_score)),
    ).fetchall()
    out: list[Lead] = []
    for row in rows:
        lead = _row_to_lead(row)
        if lead.id in exclude:
            continue
        if lead_created_on(lead.created_at, tz_name) == day:
            out.append(lead)
    return out


def get_pending_excluding(
    conn: sqlite3.Connection,
    *,
    exclude_ids: Iterable[int] | None = None,
    min_score: int,
    include_demo: bool = False,
) -> list[Lead]:
    """All pending leads ordered by ICP, optionally excluding lead ids."""
    exclude = {int(x) for x in (exclude_ids or [])}
    demo_filter = "" if include_demo else " AND source != 'demo'"
    rows = conn.execute(
        f"""
        SELECT * FROM leads
        WHERE status=? AND icp_score >= ?{demo_filter}
        ORDER BY icp_score DESC, id ASC
        """,
        (LeadStatus.pending.value, int(min_score)),
    ).fetchall()
    out: list[Lead] = []
    for row in rows:
        lead = _row_to_lead(row)
        if lead.id not in exclude:
            out.append(lead)
    return out


def get_top_pending(
    conn: sqlite3.Connection,
    *,
    limit: int,
    min_score: int,
    include_demo: bool = False,
) -> list[Lead]:
    demo_filter = "" if include_demo else " AND source != 'demo'"
    rows = conn.execute(
        f"""
        SELECT * FROM leads
        WHERE status=? AND icp_score >= ?{demo_filter}
        ORDER BY icp_score DESC, id ASC
        LIMIT ?
        """,
        (LeadStatus.pending.value, int(min_score), int(limit)),
    ).fetchall()
    return [_row_to_lead(r) for r in rows]


def purge_demo_leads(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM leads WHERE source='demo'")
    conn.commit()
    return int(cur.rowcount)


def set_status(
    conn: sqlite3.Connection,
    lead_id: int,
    status: LeadStatus,
    *,
    last_error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE leads SET status=?, updated_at=?, last_error=? WHERE id=?",
        (status.value, utc_now_iso(), last_error, int(lead_id)),
    )
    conn.commit()


def patch_lead_icp(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    icp_score: int,
    icp_reason: str | None,
    project_phase: str | None = None,
    project_value: str | None = None,
) -> None:
    """Update ICP fields after Kompass page reveals stage/GW context."""
    sets = ["icp_score=?", "icp_reason=?", "updated_at=?"]
    params: list[Any] = [int(icp_score), icp_reason, utc_now_iso()]
    if project_phase:
        sets.append("project_phase=?")
        params.append(project_phase)
    if project_value:
        sets.append("project_value=?")
        params.append(project_value)
    params.append(int(lead_id))
    conn.execute(
        f"UPDATE leads SET {', '.join(sets)} WHERE id=?",
        params,
    )
    conn.commit()


def iter_leads(
    conn: sqlite3.Connection,
    *,
    status: LeadStatus | None = None,
) -> list[Lead]:
    if status is None:
        rows = conn.execute("SELECT * FROM leads ORDER BY id ASC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM leads WHERE status=? ORDER BY id ASC",
            (status.value,),
        ).fetchall()
    return [_row_to_lead(r) for r in rows]


def backfill_ingested_emails(conn: sqlite3.Connection) -> int:
    """Seed ingested_emails from leads already extracted (one-time migration)."""
    rows = conn.execute(
        """
        SELECT DISTINCT source_message_id, source_received_at,
               json_extract(raw_payload_json, '$.email_subject') AS subject
        FROM leads
        WHERE source_message_id IS NOT NULL AND source_message_id != ''
        """
    ).fetchall()
    added = 0
    for row in rows:
        mid = str(row["source_message_id"])
        if is_email_ingested(conn, mid):
            continue
        mark_email_ingested(
            conn,
            message_id=mid,
            subject=row["subject"],
            received_at=row["source_received_at"],
        )
        added += 1
    return added


def is_email_ingested(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ingested_emails WHERE message_id=?",
        (message_id,),
    ).fetchone()
    return row is not None


def mark_email_ingested(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    subject: str | None,
    received_at: str | None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO ingested_emails (message_id, subject, received_at, ingested_at)
        VALUES (?, ?, ?, ?)
        """,
        (message_id, subject, received_at, utc_now_iso()),
    )
    conn.commit()


def _local_today(tz_name: str = "Europe/Warsaw") -> str:
    """Calendar date for reveal-quota accounting (Kompass resets by local day)."""
    try:
        from skysnap.tzutil import get_timezone

        return datetime.now(get_timezone(tz_name)).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def count_reveals_today(conn: sqlite3.Connection, *, tz_name: str = "Europe/Warsaw") -> int:
    """Number of Kompass get-contact reveals used today (persisted across runs)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM contact_reveals WHERE revealed_on=?",
        (_local_today(tz_name),),
    ).fetchone()
    return int(row["n"]) if row else 0


def lead_already_revealed(conn: sqlite3.Connection, lead_id: int) -> bool:
    """True if a reveal credit was ever spent on this lead (never double-spend)."""
    row = conn.execute(
        "SELECT 1 FROM contact_reveals WHERE lead_id=? LIMIT 1",
        (int(lead_id),),
    ).fetchone()
    return row is not None


def log_reveal(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    success: bool,
    tz_name: str = "Europe/Warsaw",
) -> None:
    conn.execute(
        """
        INSERT INTO contact_reveals (lead_id, revealed_on, success, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (int(lead_id), _local_today(tz_name), 1 if success else 0, utc_now_iso()),
    )
    conn.commit()


def get_lead_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY status"
    ).fetchall()
    by_status = {str(r["status"]): int(r["n"]) for r in rows}
    total = sum(by_status.values())
    top = conn.execute(
        """
        SELECT id, project_name, company_name, icp_score, status
        FROM leads
        WHERE status=?
        ORDER BY icp_score DESC, id ASC
        LIMIT 5
        """,
        (LeadStatus.pending.value,),
    ).fetchall()
    id_row = conn.execute("SELECT MIN(id), MAX(id) FROM leads").fetchone()
    lead_id_min = int(id_row[0]) if id_row[0] is not None else None
    lead_id_max = int(id_row[1]) if id_row[1] is not None else None
    export_total = int(
        conn.execute("SELECT COUNT(*) FROM lead_exports").fetchone()[0]
    )
    export_hubspot_synced = int(
        conn.execute(
            "SELECT COUNT(*) FROM lead_exports WHERE hubspot_deal_id IS NOT NULL"
        ).fetchone()[0]
    )
    hubspot_push_pending = len(iter_leads_pending_hubspot_sync(conn))
    hubspot_push_failed = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM lead_exports
            WHERE hubspot_deal_id IS NULL
              AND hubspot_last_error IS NOT NULL
              AND hubspot_last_error != ''
            """
        ).fetchone()[0]
    )
    hubspot_resync_available = len(iter_leads_hubspot_resync(conn))
    return {
        "total": total,
        "by_status": by_status,
        "lead_id_min": lead_id_min,
        "lead_id_max": lead_id_max,
        "pending_top5": [
            {
                "id": int(r["id"]),
                "project_name": r["project_name"],
                "company_name": r["company_name"],
                "icp_score": int(r["icp_score"]),
            }
            for r in top
        ],
        "ingested_emails": int(
            conn.execute("SELECT COUNT(*) FROM ingested_emails").fetchone()[0]
        ),
        "lead_exports_total": export_total,
        "lead_exports_hubspot_synced": export_hubspot_synced,
        "hubspot_push_pending": hubspot_push_pending,
        "hubspot_push_failed": hubspot_push_failed,
        "hubspot_resync_available": hubspot_resync_available,
    }


def recover_failed_leads(conn: sqlite3.Connection) -> int:
    """Reset processed_failed leads to pending for retry."""
    cur = conn.execute(
        "UPDATE leads SET status=?, updated_at=?, last_error=NULL WHERE status=?",
        (LeadStatus.pending.value, utc_now_iso(), LeadStatus.processed_failed.value),
    )
    conn.commit()
    return int(cur.rowcount)


def requeue_processed_leads(
    conn: sqlite3.Connection,
    *,
    include_success: bool = True,
    include_failed: bool = True,
) -> dict[str, int]:
    """Put exported or failed leads back in the pending queue for another run-daily."""
    counts: dict[str, int] = {"success": 0, "failed": 0}
    if include_failed:
        counts["failed"] = recover_failed_leads(conn)
    if include_success:
        cur = conn.execute(
            "UPDATE leads SET status=?, updated_at=?, last_error=NULL WHERE status=?",
            (LeadStatus.pending.value, utc_now_iso(), LeadStatus.processed_success.value),
        )
        conn.commit()
        counts["success"] = int(cur.rowcount)
    return counts


def recover_stale_in_progress(conn: sqlite3.Connection) -> int:
    """Reset in_progress leads back to pending (e.g. after a crashed run-daily)."""
    cur = conn.execute(
        "UPDATE leads SET status=?, updated_at=? WHERE status=?",
        (LeadStatus.pending.value, utc_now_iso(), LeadStatus.in_progress.value),
    )
    conn.commit()
    return int(cur.rowcount)


def requeue_enriched_manually(conn: sqlite3.Connection) -> int:
    """Put Kompass-deferred leads back in the pending queue for the next run."""
    cur = conn.execute(
        "UPDATE leads SET status=?, updated_at=?, last_error=NULL WHERE status=?",
        (LeadStatus.pending.value, utc_now_iso(), LeadStatus.enriched_manually.value),
    )
    conn.commit()
    return int(cur.rowcount)


def defer_pending_for_kompass_quota(
    conn: sqlite3.Connection,
    *,
    min_score: int,
    include_demo: bool = False,
    exclude_ids: Iterable[int] | None = None,
    exclude_sources: Iterable[str] | None = None,
) -> int:
    """Mark pending leads not attempted this Kompass run as enriched_manually (retry tomorrow).

    Leads in *exclude_sources* (e.g. ``manual_kompass``) stay ``pending`` so Phase B OSINT
    can still run on them the same day.
    """
    exclude = {int(x) for x in (exclude_ids or [])}
    skip_sources = tuple(exclude_sources or ())
    demo_filter = "" if include_demo else " AND source != 'demo'"
    source_filter = ""
    params: list[Any] = [LeadStatus.pending.value, int(min_score)]
    if skip_sources:
        placeholders = ",".join("?" for _ in skip_sources)
        source_filter = f" AND source NOT IN ({placeholders})"
        params.extend(skip_sources)
    rows = conn.execute(
        f"""
        SELECT id FROM leads
        WHERE status=? AND icp_score >= ?{demo_filter}{source_filter}
        """,
        params,
    ).fetchall()
    ids_to_defer = [int(r["id"]) for r in rows if int(r["id"]) not in exclude]
    if not ids_to_defer:
        return 0
    now = utc_now_iso()
    conn.executemany(
        "UPDATE leads SET status=?, updated_at=?, last_error=NULL WHERE id=? AND status=?",
        [
            (LeadStatus.enriched_manually.value, now, lead_id, LeadStatus.pending.value)
            for lead_id in ids_to_defer
        ],
    )
    conn.commit()
    return len(ids_to_defer)


def mark_in_progress_bulk(conn: sqlite3.Connection, lead_ids: Iterable[int]) -> None:
    ids = [int(x) for x in lead_ids]
    if not ids:
        return
    conn.executemany(
        "UPDATE leads SET status=?, updated_at=? WHERE id=?",
        [(LeadStatus.in_progress.value, utc_now_iso(), i) for i in ids],
    )
    conn.commit()


def reset_all_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete every lead and ingested-email record (fresh pipeline state)."""
    exports_deleted = int(conn.execute("DELETE FROM lead_exports").rowcount)
    reveals_deleted = int(conn.execute("DELETE FROM contact_reveals").rowcount)
    leads_deleted = int(conn.execute("DELETE FROM leads").rowcount)
    emails_deleted = int(conn.execute("DELETE FROM ingested_emails").rowcount)
    conn.commit()
    conn.execute("VACUUM")
    conn.commit()
    return {
        "leads_deleted": leads_deleted,
        "ingested_emails_deleted": emails_deleted,
        "lead_exports_deleted": exports_deleted,
        "contact_reveals_deleted": reveals_deleted,
    }


def _row_to_lead_export(row: sqlite3.Row) -> LeadExport:
    return LeadExport(
        lead_id=int(row["lead_id"]),
        enrichment_json=row["enrichment_json"],
        duplicate_decision_json=row["duplicate_decision_json"],
        exported_at=row["exported_at"],
        hubspot_deal_id=row["hubspot_deal_id"],
        hubspot_company_id=row["hubspot_company_id"],
        hubspot_contact_id=row["hubspot_contact_id"],
        hubspot_task_id=row["hubspot_task_id"] if "hubspot_task_id" in row.keys() else None,
        hubspot_ticket_id=row["hubspot_ticket_id"]
        if "hubspot_ticket_id" in row.keys()
        else None,
        hubspot_synced_at=row["hubspot_synced_at"],
        hubspot_last_error=row["hubspot_last_error"],
        project_similarity_json=row["project_similarity_json"]
        if "project_similarity_json" in row.keys()
        else None,
        hubspot_note_hash=row["hubspot_note_hash"]
        if "hubspot_note_hash" in row.keys()
        else None,
    )


def get_lead(conn: sqlite3.Connection, lead_id: int) -> Lead | None:
    row = conn.execute("SELECT * FROM leads WHERE id=?", (int(lead_id),)).fetchone()
    return _row_to_lead(row) if row else None


def save_lead_export(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    enrichment: EnrichmentResult | None,
    decision: FuzzyDuplicateDecision | None = None,
    project_similarity: ProjectSimilarityDecision | None = None,
) -> None:
    """Persist sheet-export payload so HubSpot sync can run later without re-enrichment."""
    enrichment_json = (
        enrichment.model_dump_json() if enrichment is not None else "{}"
    )
    decision_json = decision.model_dump_json() if decision is not None else None
    similarity_json = (
        project_similarity.model_dump_json() if project_similarity is not None else None
    )
    conn.execute(
        """
        INSERT INTO lead_exports (
            lead_id, enrichment_json, duplicate_decision_json,
            project_similarity_json, exported_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(lead_id) DO UPDATE SET
            enrichment_json=excluded.enrichment_json,
            duplicate_decision_json=excluded.duplicate_decision_json,
            project_similarity_json=excluded.project_similarity_json,
            exported_at=excluded.exported_at,
            hubspot_synced_at=NULL,
            hubspot_last_error=NULL
        """,
        (int(lead_id), enrichment_json, decision_json, similarity_json, utc_now_iso()),
    )
    conn.commit()


def patch_lead_export_similarity(
    conn: sqlite3.Connection,
    lead_id: int,
    project_similarity: ProjectSimilarityDecision,
) -> bool:
    """Update stored project similarity without clearing HubSpot sync fields."""
    cur = conn.execute(
        "UPDATE lead_exports SET project_similarity_json=? WHERE lead_id=?",
        (project_similarity.model_dump_json(), int(lead_id)),
    )
    conn.commit()
    return int(cur.rowcount) > 0


def get_lead_export(conn: sqlite3.Connection, lead_id: int) -> LeadExport | None:
    row = conn.execute(
        "SELECT * FROM lead_exports WHERE lead_id=?",
        (int(lead_id),),
    ).fetchone()
    return _row_to_lead_export(row) if row else None


def iter_lead_exports(conn: sqlite3.Connection) -> list[LeadExport]:
    """All persisted export snapshots (any lead status)."""
    rows = conn.execute(
        """
        SELECT *
        FROM lead_exports
        ORDER BY lead_id ASC
        """
    ).fetchall()
    return [_row_to_lead_export(row) for row in rows]


def lead_export_hubspot_counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN hubspot_deal_id IS NOT NULL AND hubspot_company_id IS NOT NULL THEN 1 ELSE 0 END) AS linked,
            SUM(CASE WHEN hubspot_deal_id IS NULL OR hubspot_company_id IS NULL THEN 1 ELSE 0 END) AS unlinked
        FROM lead_exports
        """
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "linked": int(row["linked"] or 0),
        "unlinked": int(row["unlinked"] or 0),
    }


def link_lead_export_hubspot(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    deal_id: str,
    company_id: str,
) -> None:
    """Attach an existing HubSpot deal/company without marking the export synced.

    Linking only records where the data belongs; no properties have been written
    yet, so the export stays queued for the next push.
    """
    conn.execute(
        """
        UPDATE lead_exports
        SET hubspot_deal_id=?,
            hubspot_company_id=?,
            hubspot_last_error=NULL
        WHERE lead_id=?
        """,
        (str(deal_id), str(company_id), int(lead_id)),
    )
    conn.commit()


def iter_leads_pending_hubspot_sync(conn: sqlite3.Connection) -> list[int]:
    """Lead IDs whose current export snapshot has not been pushed to HubSpot yet.

    Covers never-pushed leads and leads re-exported after a previous push; the
    latter keep their HubSpot ids so the push updates the same records.
    """
    rows = conn.execute(
        """
        SELECT e.lead_id
        FROM lead_exports e
        JOIN leads l ON l.id = e.lead_id
        WHERE e.hubspot_synced_at IS NULL
          AND l.status IN (?, ?)
        ORDER BY e.exported_at ASC, e.lead_id ASC
        """,
        (LeadStatus.processed_success.value, LeadStatus.skipped_duplicate.value),
    ).fetchall()
    return [int(r["lead_id"]) for r in rows]


def iter_leads_hubspot_resync(conn: sqlite3.Connection) -> list[int]:
    """Lead IDs already pushed to HubSpot (for PATCH resync)."""
    rows = conn.execute(
        """
        SELECT e.lead_id
        FROM lead_exports e
        JOIN leads l ON l.id = e.lead_id
        WHERE e.hubspot_deal_id IS NOT NULL
          AND e.hubspot_company_id IS NOT NULL
          AND l.status IN (?, ?)
        ORDER BY e.hubspot_synced_at ASC, e.lead_id ASC
        """,
        (LeadStatus.processed_success.value, LeadStatus.skipped_duplicate.value),
    ).fetchall()
    return [int(r["lead_id"]) for r in rows]


def mark_hubspot_synced(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    deal_id: str,
    company_id: str,
    contact_id: str | None = None,
    task_id: str | None = None,
    ticket_id: str | None = None,
    note_hash: str | None = None,
) -> None:
    """Mark export as synced. Prefer task_id; ticket_id kept for backward-compatible callers."""
    followup_id = task_id if task_id is not None else ticket_id
    conn.execute(
        """
        UPDATE lead_exports
        SET hubspot_deal_id=?,
            hubspot_company_id=?,
            hubspot_contact_id=?,
            hubspot_task_id=?,
            hubspot_ticket_id=NULL,
            hubspot_synced_at=?,
            hubspot_last_error=NULL,
            hubspot_note_hash=COALESCE(?, hubspot_note_hash)
        WHERE lead_id=?
        """,
        (
            deal_id,
            company_id,
            contact_id,
            followup_id,
            utc_now_iso(),
            note_hash,
            int(lead_id),
        ),
    )
    conn.commit()


def set_hubspot_sync_error(
    conn: sqlite3.Connection,
    lead_id: int,
    error: str,
) -> None:
    conn.execute(
        "UPDATE lead_exports SET hubspot_last_error=? WHERE lead_id=?",
        (error[:500], int(lead_id)),
    )
    conn.commit()

