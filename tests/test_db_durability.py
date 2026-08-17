"""Committed data must survive reconnects, lock errors, and WAL sidecar loss."""

import sqlite3
from pathlib import Path

import pytest

from skysnap import db
from skysnap.db import LeadStatus


def _seed_lead(conn) -> int:
    return db.upsert_lead(
        conn,
        source="kompass_email",
        source_message_id="m1",
        source_received_at=None,
        project_name="Test project",
        company_name="Acme",
        country="PL",
        city="Warsaw",
        project_value=None,
        project_phase="Realizacja",
        project_url="https://example.com/p",
        raw_payload_json={},
        icp_score=70,
        icp_reason="Good fit",
    )


def test_checkpoint_keeps_exports_after_wal_sidecars_are_lost(tmp_path):
    db_path = str(tmp_path / "skysnap.sqlite")
    conn = db.connect(db_path)
    lead_id = _seed_lead(conn)
    db.set_status(conn, lead_id, LeadStatus.processed_success, last_error=None)
    db.save_lead_export(conn, lead_id, enrichment=None)
    db.mark_hubspot_synced(conn, lead_id, deal_id="111", company_id="222")
    db.checkpoint(conn)
    conn.close()

    # Simulate the sidecars disappearing (crash, cleanup script, repair step).
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    conn = db.connect(db_path)
    assert db.lead_export_hubspot_counts(conn)["total"] == 1
    assert db.iter_leads_hubspot_resync(conn) == [lead_id]


def test_connect_never_quarantines_sidecars_on_lock_error(tmp_path, monkeypatch):
    db_path = str(tmp_path / "skysnap.sqlite")
    conn = db.connect(db_path)
    lead_id = _seed_lead(conn)
    db.save_lead_export(conn, lead_id, enrichment=None)
    conn.close()

    wal = Path(f"{db_path}-wal")
    wal.write_bytes(b"pretend-uncheckpointed-transactions")

    def _locked(_path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_open_connection", _locked)
    with pytest.raises(sqlite3.OperationalError):
        db.connect(db_path)

    assert wal.exists(), "a transient lock must never discard the write-ahead log"


def test_status_warns_when_database_came_from_another_machine(tmp_path, monkeypatch):
    db_path = str(tmp_path / "skysnap.sqlite")
    conn = db.connect(db_path)
    conn.execute("UPDATE db_meta SET value='OTHER-PC' WHERE key='created_on_host'")
    conn.commit()

    assert db.get_meta(conn, "created_on_host") == "OTHER-PC"
    monkeypatch.setattr(db, "_foreign_origin_warned", False)
    captured: list[str] = []
    monkeypatch.setattr(db.sys, "stderr", type("S", (), {"write": captured.append})())
    db._warn_if_foreign_origin(conn)
    assert any("OTHER-PC" in line for line in captured)


def test_adopt_database_clears_foreign_origin(tmp_path):
    db_path = str(tmp_path / "skysnap.sqlite")
    conn = db.connect(db_path)
    db.set_meta(conn, "created_on_host", "OTHER-PC")

    result = db.adopt_database(conn)

    assert result["previous_host"] == "OTHER-PC"
    assert db.get_meta(conn, "created_on_host") == result["host"]


def test_quarantine_moves_sidecars_instead_of_deleting(tmp_path):
    db_path = str(tmp_path / "skysnap.sqlite")
    db.connect(db_path).close()
    wal = Path(f"{db_path}-wal")
    wal.write_bytes(b"data")

    moved = db._quarantine_wal_sidecars(db_path)

    assert not wal.exists()
    assert len(moved) == 1
    assert Path(moved[0]).read_bytes() == b"data"
