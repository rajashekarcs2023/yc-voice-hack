"""SQLite data layer for Sam, the AI dispatcher.

Single-file SQLite DB used by every component:
  * Inbound flow (customer emergency calls → jobs)
  * Outbound flow (parts sourcing → calls to suppliers)
  * Dashboard (reads live state)
  * Supplier bots (read their own inventory + price book)

Connections are short-lived (one per query) so the DB is safe under multi-process
access — Sam, supplier bots, and the dashboard can all run as separate processes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(os.environ.get("SAM_DB_PATH", Path(__file__).parent / "sam.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    phone           TEXT,
    personality     TEXT NOT NULL DEFAULT 'friendly',
    greeting        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parts (
    id              INTEGER PRIMARY KEY,
    sku             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    aliases         TEXT NOT NULL DEFAULT '[]',
    category        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_inventory (
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id),
    part_id         INTEGER NOT NULL REFERENCES parts(id),
    in_stock        INTEGER NOT NULL DEFAULT 0,
    quantity        INTEGER NOT NULL DEFAULT 0,
    price_cents     INTEGER NOT NULL,
    lead_time_days  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (supplier_id, part_id)
);

CREATE TABLE IF NOT EXISTS techs (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    phone           TEXT NOT NULL,
    on_call         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS customers (
    id              INTEGER PRIMARY KEY,
    phone           TEXT UNIQUE,
    name            TEXT,
    address         TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    customer_id     INTEGER REFERENCES customers(id),
    description     TEXT NOT NULL,
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new',
    assigned_tech_id INTEGER REFERENCES techs(id),
    scheduled_at    TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    id              INTEGER PRIMARY KEY,
    direction       TEXT NOT NULL,
    counterpart     TEXT NOT NULL,
    counterpart_id  INTEGER,
    started_at      REAL NOT NULL,
    ended_at        REAL,
    transcript      TEXT NOT NULL DEFAULT '[]',
    summary         TEXT,
    outcome         TEXT,
    parts_request_id INTEGER REFERENCES parts_requests(id),
    job_id          INTEGER REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS parts_requests (
    id              INTEGER PRIMARY KEY,
    part_id         INTEGER NOT NULL REFERENCES parts(id),
    quantity        INTEGER NOT NULL DEFAULT 1,
    max_price_cents INTEGER,
    needed_by       TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    best_supplier_id INTEGER REFERENCES suppliers(id),
    best_price_cents INTEGER,
    notes           TEXT,
    created_at      REAL NOT NULL
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db(reset: bool = False) -> None:
    """Create tables and seed with demo data. If reset=True, wipe first."""
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0:
            _seed(conn)


def _seed(conn: sqlite3.Connection) -> None:
    """Seed with realistic plumbing supply houses, parts, and inventory."""
    suppliers = [
        (
            "ABC Supply",
            "+15551110001",
            "friendly",
            "ABC Supply, parts desk, this is Mike, how can I help?",
        ),
        (
            "Ferguson",
            "+15551110002",
            "busy",
            "Ferguson plumbing, parts. What do you need?",
        ),
        (
            "Western Plumbing",
            "+15551110003",
            "gruff",
            "Western. Go ahead.",
        ),
    ]
    conn.executemany(
        "INSERT INTO suppliers(name, phone, personality, greeting) VALUES (?, ?, ?, ?)",
        suppliers,
    )

    parts = [
        (
            "RIN-RU199i",
            "Rinnai RU199i tankless water heater",
            ["rinnai ru199", "rinnai tankless", "rinnai 199"],
            "water_heater",
        ),
        (
            "MOEN-1225",
            "Moen 1225 single-handle cartridge",
            ["moen cartridge", "moen 1225", "1225 cartridge"],
            "cartridge",
        ),
        (
            "SHARK-SB3-12",
            "SharkBite 1/2 inch push-to-connect coupling",
            ["sharkbite half inch", "sharkbite coupling", "1/2 sharkbite"],
            "fitting",
        ),
        (
            "AOSMITH-GCV40",
            "A.O. Smith GCV40 40-gallon gas water heater",
            ["ao smith 40 gallon", "gcv40", "ao smith gas heater"],
            "water_heater",
        ),
        (
            "DELTA-RP19804",
            "Delta RP19804 shower cartridge",
            ["delta cartridge", "delta rp19804", "delta shower cartridge"],
            "cartridge",
        ),
    ]
    conn.executemany(
        "INSERT INTO parts(sku, name, aliases, category) VALUES (?, ?, ?, ?)",
        [(sku, name, json.dumps(aliases), cat) for sku, name, aliases, cat in parts],
    )

    inventory = [
        # supplier_name, sku, in_stock, qty, price_cents, lead_days
        ("ABC Supply", "RIN-RU199i", 1, 4, 115000, 0),
        ("ABC Supply", "MOEN-1225", 1, 22, 1899, 0),
        ("ABC Supply", "SHARK-SB3-12", 1, 80, 549, 0),
        ("ABC Supply", "AOSMITH-GCV40", 1, 2, 89900, 0),
        ("ABC Supply", "DELTA-RP19804", 1, 14, 4599, 0),
        ("Ferguson", "RIN-RU199i", 0, 0, 108900, 5),
        ("Ferguson", "MOEN-1225", 1, 60, 1750, 0),
        ("Ferguson", "SHARK-SB3-12", 1, 200, 489, 0),
        ("Ferguson", "AOSMITH-GCV40", 1, 8, 84900, 0),
        ("Ferguson", "DELTA-RP19804", 0, 0, 4399, 3),
        ("Western Plumbing", "RIN-RU199i", 0, 0, 119900, 7),
        ("Western Plumbing", "MOEN-1225", 1, 5, 2099, 0),
        ("Western Plumbing", "SHARK-SB3-12", 1, 30, 599, 0),
        ("Western Plumbing", "AOSMITH-GCV40", 0, 0, 88500, 4),
        ("Western Plumbing", "DELTA-RP19804", 1, 3, 4799, 0),
    ]
    for s_name, sku, in_stock, qty, price, lead in inventory:
        s_id = conn.execute("SELECT id FROM suppliers WHERE name=?", (s_name,)).fetchone()[0]
        p_id = conn.execute("SELECT id FROM parts WHERE sku=?", (sku,)).fetchone()[0]
        conn.execute(
            "INSERT INTO supplier_inventory(supplier_id, part_id, in_stock, quantity, price_cents, lead_time_days) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (s_id, p_id, in_stock, qty, price, lead),
        )

    techs = [
        ("Mike Rivera", "+15552220001", 1),
        ("Sara Chen", "+15552220002", 0),
        ("Diego Lopez", "+15552220003", 0),
    ]
    conn.executemany(
        "INSERT INTO techs(name, phone, on_call) VALUES (?, ?, ?)",
        techs,
    )

    customers = [
        ("+14155551234", "Alex Kim", "742 Evergreen Terrace, Springfield", "Repeat customer, water heater replaced 2025-08"),
        ("+14155555678", "Jordan Patel", "1313 Mockingbird Lane, Springfield", None),
    ]
    conn.executemany(
        "INSERT INTO customers(phone, name, address, notes) VALUES (?, ?, ?, ?)",
        customers,
    )


# --- Read helpers used by the bots ---------------------------------------------

def find_part(query: str) -> dict | None:
    """Fuzzy-match a part by name, sku, or alias. Returns the part row or None."""
    q = query.strip().lower()
    with connect() as conn:
        for row in conn.execute("SELECT * FROM parts").fetchall():
            if q == row["sku"].lower() or q in row["name"].lower():
                return dict(row)
            aliases = [a.lower() for a in json.loads(row["aliases"])]
            if any(q in a or a in q for a in aliases):
                return dict(row)
    return None


def get_supplier(supplier_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        return dict(row) if row else None


def get_supplier_by_name(name: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        return dict(row) if row else None


def list_suppliers() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()]


def get_inventory(supplier_id: int, part_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM supplier_inventory WHERE supplier_id=? AND part_id=?",
            (supplier_id, part_id),
        ).fetchone()
        return dict(row) if row else None


def get_customer_by_phone(phone: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        return dict(row) if row else None


def upsert_customer(phone: str, name: str | None = None, address: str | None = None) -> int:
    with connect() as conn:
        existing = conn.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
        if existing:
            if name or address:
                conn.execute(
                    "UPDATE customers SET name=COALESCE(?, name), address=COALESCE(?, address) WHERE id=?",
                    (name, address, existing[0]),
                )
            return existing[0]
        cur = conn.execute(
            "INSERT INTO customers(phone, name, address) VALUES (?, ?, ?)",
            (phone, name, address),
        )
        return cur.lastrowid


def on_call_tech() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM techs WHERE on_call=1 LIMIT 1").fetchone()
        return dict(row) if row else None


# --- Write helpers -------------------------------------------------------------

def create_job(
    customer_id: int | None,
    description: str,
    severity: str,
    scheduled_at: str | None = None,
    assigned_tech_id: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs(customer_id, description, severity, scheduled_at, assigned_tech_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (customer_id, description, severity, scheduled_at, assigned_tech_id, time.time()),
        )
        return cur.lastrowid


def create_parts_request(
    part_id: int,
    quantity: int = 1,
    max_price_cents: int | None = None,
    needed_by: str | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO parts_requests(part_id, quantity, max_price_cents, needed_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (part_id, quantity, max_price_cents, needed_by, time.time()),
        )
        return cur.lastrowid


def update_parts_request(request_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE parts_requests SET {sets} WHERE id=?", (*fields.values(), request_id))


def start_call(
    direction: str,
    counterpart: str,
    counterpart_id: int | None = None,
    parts_request_id: int | None = None,
    job_id: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls(direction, counterpart, counterpart_id, started_at, parts_request_id, job_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (direction, counterpart, counterpart_id, time.time(), parts_request_id, job_id),
        )
        return cur.lastrowid


def append_call_turn(call_id: int, role: str, text: str) -> None:
    """Append one transcript turn to a call. role is 'sam' | 'customer' | 'supplier'."""
    with connect() as conn:
        row = conn.execute("SELECT transcript FROM calls WHERE id=?", (call_id,)).fetchone()
        if not row:
            return
        transcript = json.loads(row["transcript"])
        transcript.append({"role": role, "text": text, "t": time.time()})
        conn.execute(
            "UPDATE calls SET transcript=? WHERE id=?",
            (json.dumps(transcript), call_id),
        )


def end_call(call_id: int, summary: str | None = None, outcome: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE calls SET ended_at=?, summary=COALESCE(?, summary), outcome=COALESCE(?, outcome) WHERE id=?",
            (time.time(), summary, outcome, call_id),
        )


# --- Dashboard read helpers ----------------------------------------------------

def recent_calls(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def recent_jobs(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT j.*, c.name AS customer_name, c.phone AS customer_phone, t.name AS tech_name "
            "FROM jobs j LEFT JOIN customers c ON j.customer_id=c.id "
            "LEFT JOIN techs t ON j.assigned_tech_id=t.id "
            "ORDER BY j.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_parts_requests(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT pr.*, p.name AS part_name, p.sku AS sku, s.name AS best_supplier_name "
            "FROM parts_requests pr JOIN parts p ON pr.part_id=p.id "
            "LEFT JOIN suppliers s ON pr.best_supplier_id=s.id "
            "ORDER BY pr.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys
    reset = "--reset" in sys.argv
    init_db(reset=reset)
    with connect() as conn:
        print(f"DB at {DB_PATH}")
        print(f"  suppliers: {conn.execute('SELECT COUNT(*) FROM suppliers').fetchone()[0]}")
        print(f"  parts:     {conn.execute('SELECT COUNT(*) FROM parts').fetchone()[0]}")
        print(f"  inventory: {conn.execute('SELECT COUNT(*) FROM supplier_inventory').fetchone()[0]}")
        print(f"  techs:     {conn.execute('SELECT COUNT(*) FROM techs').fetchone()[0]}")
        print(f"  customers: {conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]}")
