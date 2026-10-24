"""
app/db/seed.py

Creates the V1 schema (orders, order_items, shipments, payments,
product_catalog, returns, tickets, workflow_runs, action_proposals,
approvals, audit_events) in data/customer_support.sqlite3, and populates
it with synthetic data layered on top of the real customer IDs found in
customer_shopping_data.csv.

The CSV is read-only input. The existing `purchases` table (owned by
ShoppingDatabase in the base version) is never touched by this script.

Run:
    python -m app.db.seed

Idempotent: safe to re-run. New-table data is dropped and regenerated
each time; purchases/customers from the CSV are not altered.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent.parent  # project root
DATA_DIR = ROOT_DIR / "data"
CSV_PATH = DATA_DIR / "customer_shopping_data.csv" / "customer_shopping_data.csv"
DB_PATH = DATA_DIR / "customer_support.sqlite3"

SEED = 42
NUM_ORDERS = 200
RETURN_RATE = 0.30  # fraction of delivered orders that also get a return
CURRENCY = "INR"

CATEGORIES = ["Electronics", "Clothing", "Cosmetics", "Food", "Shoes", "Toys"]

# 30-product catalog. Prices are in minor units (paise) -> 1 INR = 100 paise.
PRODUCT_CATALOG = [
    # Electronics (6)
    ("Wireless Headphones",        "Electronics", 299900),
    ("Bluetooth Speaker",          "Electronics", 189900),
    ("Smartwatch",                 "Electronics", 499900),
    ("Power Bank 20000mAh",        "Electronics", 149900),
    ("USB-C Charging Cable",       "Electronics",  39900),
    ("Wireless Mouse",             "Electronics",  79900),
    # Clothing (5)
    ("Cotton T-Shirt",             "Clothing",      79900),
    ("Denim Jacket",               "Clothing",     249900),
    ("Formal Shirt",               "Clothing",     129900),
    ("Hooded Sweatshirt",          "Clothing",     159900),
    ("Chino Trousers",             "Clothing",     189900),
    # Cosmetics (5)
    ("Moisturizing Face Cream",    "Cosmetics",      59900),
    ("Matte Lipstick",             "Cosmetics",      39900),
    ("Sunscreen SPF 50",           "Cosmetics",      49900),
    ("Herbal Shampoo 400ml",       "Cosmetics",      34900),
    ("Perfume 100ml",              "Cosmetics",     249900),
    # Food (5)
    ("Assorted Dry Fruits 500g",   "Food",           59900),
    ("Organic Honey 250g",         "Food",           34900),
    ("Green Tea Box (25 bags)",    "Food",           19900),
    ("Protein Bar Pack (6)",       "Food",           44900),
    ("Extra Virgin Olive Oil 1L",  "Food",           89900),
    # Shoes (5)
    ("Running Shoes",              "Shoes",         349900),
    ("Leather Formal Shoes",       "Shoes",         399900),
    ("Canvas Sneakers",            "Shoes",         179900),
    ("Flip Flops",                 "Shoes",          29900),
    ("Hiking Boots",               "Shoes",         449900),
    # Toys (4)
    ("Building Blocks Set",        "Toys",           99900),
    ("Remote Control Car",         "Toys",          149900),
    ("Puzzle 1000pc",              "Toys",            79900),
    ("Plush Teddy Bear",           "Toys",            59900),
]
assert len(PRODUCT_CATALOG) == 30
assert {c for _, c, _ in PRODUCT_CATALOG} == set(CATEGORIES)

ORDER_STATUSES_WEIGHTED = (
    # status, weight
    ("delivered", 60),
    ("shipped", 15),
    ("confirmed", 10),
    ("pending", 10),
    ("cancelled", 5),
)

SCHEMA_SQL = """
CREATE TABLE product_catalog (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    price_minor INTEGER NOT NULL,
    in_stock    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE orders (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    status        TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'INR',
    total_minor   INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT
);
CREATE INDEX idx_orders_customer_id ON orders(customer_id);

CREATE TABLE order_items (
    id           TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    product_id   TEXT NOT NULL REFERENCES product_catalog(id),
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    price_minor  INTEGER NOT NULL
);
CREATE INDEX idx_order_items_order_id ON order_items(order_id);

CREATE TABLE shipments (
    id                  TEXT PRIMARY KEY,
    order_id            TEXT NOT NULL REFERENCES orders(id),
    carrier             TEXT,
    tracking_id         TEXT,
    status              TEXT NOT NULL,
    estimated_delivery  TEXT,
    last_updated        TEXT NOT NULL
);
CREATE INDEX idx_shipments_order_id ON shipments(order_id);

CREATE TABLE payments (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(id),
    status          TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,
    refunded_minor  INTEGER NOT NULL DEFAULT 0,
    payment_method  TEXT,
    paid_at         TEXT
);
CREATE INDEX idx_payments_order_id ON payments(order_id);

CREATE TABLE returns (
    id                TEXT PRIMARY KEY,
    order_id          TEXT NOT NULL REFERENCES orders(id),
    status            TEXT NOT NULL,
    reason            TEXT,
    received_at       TEXT,
    inspection_status TEXT
);
CREATE INDEX idx_returns_order_id ON returns(order_id);

CREATE TABLE tickets (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    order_id    TEXT,
    category    TEXT NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_tickets_customer_id ON tickets(customer_id);

CREATE TABLE workflow_runs (
    id           TEXT PRIMARY KEY,
    ticket_id    TEXT NOT NULL REFERENCES tickets(id),
    thread_id    TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE action_proposals (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES workflow_runs(id),
    action_type  TEXT NOT NULL,
    payload      TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

CREATE TABLE approvals (
    id          TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES action_proposals(id),
    reviewer_id TEXT NOT NULL,
    decision    TEXT,
    decided_at  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE audit_events (
    id         TEXT PRIMARY KEY,
    run_id     TEXT,
    actor      TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL
);
"""

NEW_TABLES = [
    "audit_events", "approvals", "action_proposals", "workflow_runs",
    "tickets", "returns", "payments", "shipments", "order_items",
    "orders", "product_catalog",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    dt = dt.replace(microsecond=0)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def _weighted_choice(rng: random.Random, pairs):
    items, weights = zip(*pairs)
    return rng.choices(items, weights=weights, k=1)[0]


def load_customer_ids(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found at {csv_path}")
    ids: set[str] = set()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "customer_id" not in (reader.fieldnames or []):
            raise ValueError("CSV missing required column: customer_id")
        for row in reader:
            cid = (row.get("customer_id") or "").strip()
            if cid:
                ids.add(cid)
    if not ids:
        raise ValueError("No customer IDs found in CSV")
    return sorted(ids)


@dataclass
class SeedCounts:
    customers_sampled: int = 0
    products: int = 0
    orders: int = 0
    order_items: int = 0
    shipments: int = 0
    payments: int = 0
    returns: int = 0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def recreate_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = OFF;")  # allow drop in any order
    for table in NEW_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {table};")
    cur.executescript(SCHEMA_SQL)
    cur.execute("PRAGMA foreign_keys = ON;")
    conn.commit()


def insert_catalog(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """Returns list of (product_id, category, price_minor)."""
    cur = conn.cursor()
    rows = []
    for i, (name, category, price_minor) in enumerate(PRODUCT_CATALOG, start=1):
        product_id = f"prod_{i:03d}"
        cur.execute(
            "INSERT INTO product_catalog (id, name, category, price_minor, in_stock) "
            "VALUES (?, ?, ?, ?, 1)",
            (product_id, name, category, price_minor),
        )
        rows.append((product_id, category, price_minor))
    return rows


# ---------------------------------------------------------------------------
# Order generation
# ---------------------------------------------------------------------------

def generate_orders(
    conn: sqlite3.Connection,
    rng: random.Random,
    customer_ids: list[str],
    catalog: list[tuple[str, str, int]],
    counts: SeedCounts,
) -> None:
    cur = conn.cursor()
    sampled_customers = rng.sample(customer_ids, k=min(NUM_ORDERS, len(customer_ids)))
    counts.customers_sampled = len(sampled_customers)

    now = datetime.now(timezone.utc)
    product_lookup = {pid: (cat, price) for pid, cat, price in catalog}
    product_ids = list(product_lookup.keys())

    for i in range(1, NUM_ORDERS + 1):
        order_id = f"ord_{i:04d}"
        customer_id = sampled_customers[(i - 1) % len(sampled_customers)]

        status = _weighted_choice(rng, ORDER_STATUSES_WEIGHTED)
        created_at = now - timedelta(days=rng.randint(1, 120))

        delivered_at = None
        if status == "delivered":
            # 80% of delivered orders: recent (1–10 days), within both windows
            # 20% of delivered orders: aged (35–60 days), outside both windows
            if rng.random() < 0.80:
                delivered_at = now - timedelta(days=rng.randint(1, 10))
            else:
                delivered_at = now - timedelta(days=rng.randint(35, 60))
            

        # 1-3 items per order
        n_items = rng.randint(1, 3)
        chosen_products = rng.sample(product_ids, k=n_items)
        total_minor = 0
        item_rows = []
        for j, pid in enumerate(chosen_products, start=1):
            cat, price = product_lookup[pid]
            qty = rng.randint(1, 2)
            total_minor += price * qty
            item_rows.append((f"{order_id}_item_{j}", order_id, pid, cat, qty, price))

        cur.execute(
            "INSERT INTO orders (id, customer_id, status, currency, total_minor, "
            "created_at, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                order_id, customer_id, status, CURRENCY, total_minor,
                _iso(created_at), _iso(delivered_at) if delivered_at else None,
            ),
        )
        counts.orders += 1

        for item in item_rows:
            item_id, item_order_id, product_id, category, qty, price = item
            cur.execute(
                "INSERT INTO order_items (id, order_id, product_id, product_name, "
                "category, quantity, price_minor) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id, item_order_id, product_id, _product_name(product_id),
                    category, qty, price,
                ),
            )
            counts.order_items += 1

        generate_shipment(cur, order_id, status, created_at, delivered_at, rng, counts)
        generate_payment(cur, order_id, status, total_minor, created_at, rng, counts)
        if status == "delivered" and rng.random() < RETURN_RATE:
            generate_return(cur, order_id, delivered_at, rng, counts)


_PRODUCT_NAME_BY_ID = {f"prod_{i:03d}": name for i, (name, _, _) in enumerate(PRODUCT_CATALOG, start=1)}


def _product_name(product_id: str) -> str:
    return _PRODUCT_NAME_BY_ID[product_id]


def generate_shipment(cur, order_id, order_status, created_at, delivered_at, rng, counts):
    shipment_id = f"ship_{order_id}"
    carrier = rng.choice(["BlueDart", "Delhivery", "DTDC", "IndiaPost"])
    tracking_id = f"TRK{rng.randint(10**9, 10**10 - 1)}"

    if order_status == "delivered":
        s_status = "delivered"
        est = delivered_at
    elif order_status == "shipped":
        s_status = rng.choice(["in_transit", "out_for_delivery", "delayed"])
        est = created_at + timedelta(days=rng.randint(3, 9))
    elif order_status == "confirmed":
        s_status = "processing"
        est = created_at + timedelta(days=rng.randint(5, 10))
    elif order_status == "cancelled":
        s_status = "processing"
        est = None
    else:  # pending
        s_status = "processing"
        est = None

    last_updated = delivered_at if delivered_at else created_at
    cur.execute(
        "INSERT INTO shipments (id, order_id, carrier, tracking_id, status, "
        "estimated_delivery, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            shipment_id, order_id, carrier, tracking_id, s_status,
            _iso(est) if est else None, _iso(last_updated),
        ),
    )
    counts.shipments += 1


PAYMENT_METHODS = ["Credit Card", "Debit Card", "Cash", "UPI"]


def generate_payment(cur, order_id, order_status, total_minor, created_at, rng, counts):
    payment_id = f"pay_{order_id}"
    method = rng.choice(PAYMENT_METHODS)

    if order_status == "cancelled":
        p_status = "failed"
        paid_at = None
    elif order_status == "pending":
        p_status = rng.choice(["pending", "paid"])
        paid_at = created_at if p_status == "paid" else None
    else:
        p_status = "paid"
        paid_at = created_at

    cur.execute(
        "INSERT INTO payments (id, order_id, status, amount_minor, refunded_minor, "
        "payment_method, paid_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
        (
            payment_id, order_id, p_status, total_minor, method,
            _iso(paid_at) if paid_at else None,
        ),
    )
    counts.payments += 1


RETURN_REASONS = [
    "Item damaged on arrival",
    "Wrong item received",
    "No longer needed",
    "Does not match description",
    "Found a better price elsewhere",
]


def generate_return(cur, order_id, delivered_at, rng, counts):
    return_id = f"ret_{order_id}"
    received_at = delivered_at + timedelta(days=rng.randint(2, 20))
    inspection_status = _weighted_choice(rng, [("passed", 70), ("failed", 30)])
    status = "passed" if inspection_status == "passed" else "failed"
    # 'status' here mirrors inspection outcome; "received" state is implied
    # by received_at being set. Use 'inspecting' in rare cases to simulate
    # in-progress returns (not yet finally resolved).
    final_status = rng.choices(
        ["passed", "failed", "inspecting", "received"],
        weights=[55, 20, 15, 10],
        k=1,
    )[0]

    cur.execute(
        "INSERT INTO returns (id, order_id, status, reason, received_at, "
        "inspection_status) VALUES (?, ?, ?, ?, ?, ?)",
        (
            return_id, order_id, final_status, rng.choice(RETURN_REASONS),
            _iso(received_at),
            inspection_status if final_status in ("passed", "failed") else None,
        ),
    )
    counts.returns += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_seed() -> SeedCounts:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    customer_ids = load_customer_ids(CSV_PATH)

    rng = random.Random(SEED)
    counts = SeedCounts()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        recreate_schema(conn)
        catalog = insert_catalog(conn)
        counts.products = len(catalog)
        generate_orders(conn, rng, customer_ids, catalog, counts)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return counts


def main() -> None:
    try:
        counts = run_seed()
    except Exception as exc:  # noqa: BLE001
        print(f"[SEED FAILED] {exc}", file=sys.stderr)
        sys.exit(1)

    print("[SEED OK]")
    print(f"  DB path:            {DB_PATH}")
    print(f"  Customers sampled:  {counts.customers_sampled}")
    print(f"  Products:           {counts.products}")
    print(f"  Orders:             {counts.orders}")
    print(f"  Order items:        {counts.order_items}")
    print(f"  Shipments:          {counts.shipments}")
    print(f"  Payments:           {counts.payments}")
    print(f"  Returns:            {counts.returns}")
    print("Run verify_seed.py to inspect sample rows.")


if __name__ == "__main__":
    main()