"""Numeric robot activity only. No text, transcripts, credentials, or audio."""
import logging
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time

log = logging.getLogger(__name__)
METRICS = ("conversations", "duration_seconds", "input_tokens", "output_tokens", "cached_tokens", "errors", "reconnects")


def database():
    return Path(os.environ.get("NOHA_METRICS_DB", "/var/lib/noha-metrics/usage.sqlite3"))


def connect(path):
    db = sqlite3.connect(path, timeout=3)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS activity (at REAL NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL, value REAL NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS activity_at ON activity(at)")
    return db


def record(model, **values):
    try:
        with closing(connect(database())) as db, db:
            now = time.time()
            db.executemany("INSERT INTO activity VALUES (?, ?, ?, ?)",
                           [(now, model, k, max(0, float(v))) for k, v in values.items()
                            if k in METRICS and v is not None])
            db.execute("DELETE FROM activity WHERE at < ?", (now - 90 * 86400,))
    except (OSError, sqlite3.Error, ValueError):
        log.warning("Métricas indisponíveis; conversa continua normalmente")


def summary(days=1, path=None):
    path = path or database()
    if not path.exists():
        return {"days": days, "models": [], "totals": {k: None if k.endswith("tokens") else 0 for k in METRICS}}
    now = time.time()
    start = now - days * 86400
    if days == 1:
        import datetime
        start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=3)) as db:
        rows = db.execute("SELECT model, kind, SUM(value) FROM activity WHERE at >= ? GROUP BY model, kind", (start,)).fetchall()
    grouped = {}
    for model, kind, value in rows:
        grouped.setdefault(model, {"model": model, **{k: None if k.endswith("tokens") else 0 for k in METRICS}})[kind] = value
    totals = {k: (sum(row[k] for row in grouped.values() if row[k] is not None)
                  if any(row[k] is not None for row in grouped.values()) else None if k.endswith("tokens") else 0)
              for k in METRICS}
    return {"days": days, "models": list(grouped.values()), "totals": totals}
