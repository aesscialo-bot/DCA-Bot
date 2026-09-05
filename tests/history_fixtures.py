"""Valid, hash-bound market history evidence for isolated decision tests."""

from datetime import datetime, timedelta, timezone

from dca_config import history_summary_hash


def ready_history(target="BTC_GBP", analyzed_at=None):
    reference = analyzed_at or datetime(2026, 8, 5, 21, tzinfo=timezone.utc)
    if isinstance(reference, str):
        reference = datetime.fromisoformat(reference.replace("Z", "+00:00"))
    cutoff = reference.astimezone(timezone.utc).replace(second=0, microsecond=0)
    cutoff -= timedelta(minutes=cutoff.minute % 15)
    iso = lambda value: value.isoformat().replace("+00:00", "Z")
    real = cutoff - timedelta(minutes=15)
    summary = {
        "VERSION": 2, "STATUS": "READY", "PAIR": target.replace("_", "/"),
        "FROM": iso(cutoff - timedelta(days=65)), "THROUGH": iso(cutoff),
        "COVERAGE_THROUGH": iso(cutoff), "VERIFIED_AT": iso(cutoff),
        "LAST_REAL_CANDLE_AT": iso(real), "CANDLE_COUNT": 6240,
        "NO_TRADE_INTERVALS": 0, "ANALYSIS_CANDLE_COUNT": 6240,
        "CARRIED_NO_TRADE_INTERVALS": 0,
        "OVERLAP": {"STATUS": "VERIFIED", "CANDLES": 96,
                    "FROM": iso(cutoff - timedelta(days=1)), "THROUGH": iso(real)},
        "PARTITIONS_HASH": "a" * 64,
    }
    summary["HASH"] = history_summary_hash(summary)
    return summary
