"""SQLite storage: alert dedupe, price history, small key/value cache."""
from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .models import Product

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    product_id TEXT NOT NULL,
    price REAL NOT NULL,
    mrp REAL,
    title TEXT,
    url TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(platform, product_id, timestamp);

CREATE TABLE IF NOT EXISTS observations (
    platform TEXT NOT NULL,
    product_id TEXT NOT NULL,
    price REAL NOT NULL,
    mrp REAL,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_key ON observations(platform, product_id, timestamp);

CREATE TABLE IF NOT EXISTS products (
    platform TEXT NOT NULL,
    product_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    last_seen REAL,
    PRIMARY KEY (platform, product_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
"""

# Record at most one observation per product per this many seconds (keeps the DB small).
OBS_MIN_GAP = 30 * 60
HISTORY_DAYS = 30


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- alerts
    def should_alert(self, platform: str, product_id: str, price: float, window_hours: float) -> bool:
        """True unless we alerted this product at the same-or-lower price within the window."""
        since = time.time() - window_hours * 3600
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(price) FROM alerts WHERE platform=? AND product_id=? AND timestamp>=?",
                (platform, product_id, since),
            ).fetchone()
        best = row[0] if row else None
        if best is None:
            return True
        return price < best - 0.005  # dropped further -> alert immediately

    def record_alert(self, p: Product) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO alerts(platform, product_id, price, mrp, title, url, timestamp) VALUES (?,?,?,?,?,?,?)",
                (p.platform, p.product_id, p.price, p.mrp, p.title, p.url, time.time()),
            )
            self._conn.commit()

    def was_alerted(self, platform: str, product_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM alerts WHERE platform=? AND product_id=? LIMIT 1", (platform, product_id)
            ).fetchone()
        return row is not None

    def recent_alerts(self, limit: int = 20) -> List[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT platform, product_id, price, mrp, title, url, timestamp FROM alerts "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return cur.fetchall()

    # ----------------------------------------------------------- observations
    def record_observations(self, products: Sequence[Product]) -> None:
        now = time.time()
        rows = []
        with self._lock:
            self._conn.executemany(
                "INSERT INTO products(platform, product_id, title, url, last_seen) VALUES (?,?,?,?,?) "
                "ON CONFLICT(platform, product_id) DO UPDATE SET title=excluded.title, url=excluded.url, "
                "last_seen=excluded.last_seen",
                [(p.platform, p.product_id, p.title[:300], p.url, now) for p in products],
            )
            for p in products:
                if not p.in_stock:  # out-of-stock prices aren't real prices; keep them out of history
                    continue
                last = self._conn.execute(
                    "SELECT price, timestamp FROM observations WHERE platform=? AND product_id=? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (p.platform, p.product_id),
                ).fetchone()
                if last and abs(last[0] - p.price) < 0.005 and now - last[1] < OBS_MIN_GAP:
                    continue
                rows.append((p.platform, p.product_id, p.price, p.mrp, now))
            if rows:
                self._conn.executemany(
                    "INSERT INTO observations(platform, product_id, price, mrp, timestamp) VALUES (?,?,?,?,?)",
                    rows,
                )
            self._conn.commit()

    def price_history(self, platform: str, product_id: str, before: Optional[float] = None) -> List[tuple]:
        before = before or time.time()
        with self._lock:
            return self._conn.execute(
                "SELECT price, mrp, timestamp FROM observations WHERE platform=? AND product_id=? "
                "AND timestamp<? ORDER BY timestamp",
                (platform, product_id, before),
            ).fetchall()

    def history_stats(self, platform: str, product_id: str) -> Optional[dict]:
        """Summary of earlier sightings (excluding the current cycle), or None if never seen."""
        hist = self.price_history(platform, product_id, before=time.time() - 60)
        if not hist:
            return None
        prices = [h[0] for h in hist]
        mrps = [h[1] for h in hist if h[1]]
        return {
            "count": len(hist),
            "span_h": (hist[-1][2] - hist[0][2]) / 3600,
            "median": statistics.median(prices),
            "max_price": max(prices),
            "min_price": min(prices),
            "max_mrp": max(mrps) if mrps else None,
        }

    def prune(self) -> None:
        cutoff = time.time() - HISTORY_DAYS * 86400
        with self._lock:
            self._conn.execute("DELETE FROM observations WHERE timestamp<?", (cutoff,))
            self._conn.execute("DELETE FROM alerts WHERE timestamp<?", (cutoff,))
            self._conn.commit()

    # ----------------------------------------------------------------- digest
    def biggest_real_drops(self, min_drop_pct: float, min_floor: float, limit: int,
                           window_h: float = 24) -> List[tuple]:
        """Products seen in the last `window_h` hours whose latest price is far below the
        lowest price recorded for them *before* that window (inflated MRPs can't fake this)."""
        since = time.time() - window_h * 3600
        q = """
        WITH m AS (
            SELECT platform, product_id, MAX(timestamp) AS ts FROM observations
            WHERE timestamp >= :since GROUP BY platform, product_id
        ), cur AS (
            SELECT o.platform, o.product_id, o.price, o.mrp FROM observations o
            JOIN m ON o.platform = m.platform AND o.product_id = m.product_id AND o.timestamp = m.ts
        ), old AS (
            SELECT platform, product_id, MIN(price) AS floor, COUNT(*) AS n FROM observations
            WHERE timestamp < :since GROUP BY platform, product_id
        )
        SELECT cur.platform, cur.product_id, cur.price, cur.mrp, old.floor, old.n, pr.title, pr.url
        FROM cur
        JOIN old ON old.platform = cur.platform AND old.product_id = cur.product_id
        LEFT JOIN products pr ON pr.platform = cur.platform AND pr.product_id = cur.product_id
        WHERE old.n >= 2 AND old.floor >= :min_floor AND cur.price <= old.floor * (1 - :drop)
          AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.platform = cur.platform
                          AND a.product_id = cur.product_id AND a.timestamp >= :since)
        ORDER BY cur.price / old.floor ASC
        LIMIT :limit
        """
        with self._lock:
            return self._conn.execute(q, {"since": since, "min_floor": min_floor,
                                          "drop": min_drop_pct / 100, "limit": limit}).fetchall()

    def products_tracked(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT platform, COUNT(DISTINCT product_id) FROM observations GROUP BY platform").fetchall()
        return dict(rows)

    # ------------------------------------------------------------------ stats
    @staticmethod
    def ist_date(offset_days: int = 0) -> str:
        import datetime as _dt
        now = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30, days=offset_days)
        return now.strftime("%Y-%m-%d")

    def bump_stats(self, platform: str, **inc: int) -> None:
        key = f"stats:{self.ist_date()}"
        stats = self.kv_get(key) or {}
        row = stats.setdefault(platform, {})
        for k, v in inc.items():
            row[k] = row.get(k, 0) + int(v)
        self.kv_set(key, stats)

    # ------------------------------------------------------------------ pages
    def known_pages(self, platform: str) -> set:
        return set(self.kv_get(f"pages:{platform}") or [])

    def add_pages(self, platform: str, urls: Sequence[str]) -> None:
        known = self.kv_get(f"pages:{platform}") or []
        new = [u for u in urls if u not in known]
        if new:
            self.kv_set(f"pages:{platform}", (known + new)[-1000:])

    # --------------------------------------------------------------------- kv
    def kv_get(self, key: str, max_age_s: Optional[float] = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value, updated FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if max_age_s is not None and time.time() - row[1] > max_age_s:
            return None
        return json.loads(row[0])

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (key, json.dumps(value), time.time()),
            )
            self._conn.commit()
