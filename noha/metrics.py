"""Numeric robot activity only. No text, transcripts, credentials, or audio."""
from contextlib import closing
import datetime
import logging
import os
from pathlib import Path
import sqlite3
import time

log = logging.getLogger(__name__)
METRICS = ("conversations", "greetings", "duration_seconds", "input_tokens", "output_tokens", "cached_tokens", "errors",
           "reconnects", "cost_usd")
# How each visit ended: the visitor talked, nobody answered, Wi-Fi instructions offline, or a failure.
OUTCOMES = ("conversou", "sem_resposta", "wifi", "falha", "sem_chave")
RETENTION = 90 * 86400


def midnight(now=None):
    moment = datetime.datetime.fromtimestamp(now if now is not None else time.time())
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def database():
    return Path(os.environ.get("NOHA_METRICS_DB", "/var/lib/noha-metrics/usage.sqlite3"))


def connect(path):
    db = sqlite3.connect(path, timeout=3)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS activity (at REAL NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL, value REAL NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS activity_at ON activity(at)")
    db.execute("CREATE TABLE IF NOT EXISTS visits (started REAL NOT NULL, ended REAL NOT NULL, outcome TEXT NOT NULL, "
               "model TEXT NOT NULL, cost_usd REAL NOT NULL, replies INTEGER NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS visits_started ON visits(started)")
    return db


def reader(path):
    return closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=3))


def record(model, **values):
    try:
        with closing(connect(database())) as db, db:
            now = time.time()
            db.executemany("INSERT INTO activity VALUES (?, ?, ?, ?)",
                           [(now, model, k, max(0, float(v))) for k, v in values.items()
                            if k in METRICS and v is not None])
            db.execute("DELETE FROM activity WHERE at < ?", (now - RETENTION,))
    except (OSError, sqlite3.Error, ValueError):
        log.warning("Métricas indisponíveis; conversa continua normalmente")


def record_visit(started, ended, outcome, model, cost_usd=0.0, replies=0):
    if outcome not in OUTCOMES:
        raise ValueError("Resultado de visita desconhecido")
    try:
        with closing(connect(database())) as db, db:
            db.execute("INSERT INTO visits VALUES (?, ?, ?, ?, ?, ?)",
                       (started, max(started, ended), outcome, model, max(0.0, float(cost_usd)), max(0, int(replies))))
            db.execute("DELETE FROM visits WHERE started < ?", (ended - RETENTION,))
    except (OSError, sqlite3.Error, ValueError):
        log.warning("Métricas indisponíveis; visita não registrada")


def spent_today(path=None):
    """Estimated USD since local midnight; unreadable metrics count as nothing spent."""
    path = path or database()
    try:
        with reader(path) as db:
            row = db.execute("SELECT SUM(value) FROM activity WHERE kind = 'cost_usd' AND at >= ?", (midnight(),)).fetchone()
        return row[0] or 0.0
    except (OSError, sqlite3.Error):
        return 0.0


def buckets(days, now):
    """(start, label) of each chart bar: the hours of today, or the last `days` days."""
    if days == 1:
        start = midnight(now)
        return [(start + hour * 3600, f"{hour}h") for hour in range(24)]
    today = datetime.date.fromtimestamp(now)
    dates = [today - datetime.timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    return [(datetime.datetime.combine(d, datetime.time()).timestamp(), d.strftime("%d/%m")) for d in dates]


def empty(days):
    return {"days": days, "models": [], "series": [], "visits": [], "outcomes": {o: 0 for o in OUTCOMES},
            "totals": {"visits": 0, **{k: None if k.endswith("tokens") else 0 for k in METRICS}}}


def summary(days=1, path=None, now=None):
    """Totals, per-model rows, chart series and recent visits since midnight (days=1) or for `days` days."""
    path = path or database()
    now = now if now is not None else time.time()
    if not path.exists():
        result = empty(days)
        result["series"] = [{"start": s, "label": label, "visits": 0, "conversations": 0, "cost_usd": 0.0}
                            for s, label in buckets(days, now)]
        return result
    bars = buckets(days, now)
    start = bars[0][0]
    try:
        with reader(path) as db:
            rows = db.execute("SELECT model, kind, SUM(value) FROM activity WHERE at >= ? GROUP BY model, kind",
                              (start,)).fetchall()
            costs = db.execute("SELECT at, value FROM activity WHERE kind = 'cost_usd' AND at >= ?", (start,)).fetchall()
            try:
                visits = db.execute("SELECT started, ended, outcome, model, cost_usd, replies FROM visits "
                                    "WHERE started >= ? ORDER BY started DESC", (start,)).fetchall()
            except sqlite3.OperationalError:
                visits = []  # database from before visits were recorded
    except sqlite3.Error:
        return empty(days)
    grouped = {}
    for model, kind, value in rows:
        if kind in METRICS:
            grouped.setdefault(model, {"model": model, **{k: None if k.endswith("tokens") else 0 for k in METRICS}})[kind] = value
    totals = {k: (sum(row[k] for row in grouped.values() if row[k] is not None)
                  if any(row[k] is not None for row in grouped.values()) else None if k.endswith("tokens") else 0)
              for k in METRICS}
    totals["visits"] = len(visits)
    outcomes = {o: 0 for o in OUTCOMES}
    series = [{"start": s, "label": label, "visits": 0, "conversations": 0, "cost_usd": 0.0} for s, label in bars]
    starts = [s for s, _ in bars]

    def bar(moment):
        index = 0
        for position, value in enumerate(starts):
            if moment >= value:
                index = position
        return series[index]
    for started, _ended, outcome, _model, _cost, _replies in visits:
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        item = bar(started)
        item["visits"] += 1
        item["conversations"] += outcome == "conversou"
    for at, value in costs:
        bar(at)["cost_usd"] += value
    recent = [{"started": started, "ended": ended, "outcome": outcome, "model": model, "cost_usd": round(cost, 5),
               "replies": replies} for started, ended, outcome, model, cost, replies in visits[:50]]
    return {"days": days, "models": list(grouped.values()), "totals": totals, "series": series,
            "visits": recent, "outcomes": outcomes}
