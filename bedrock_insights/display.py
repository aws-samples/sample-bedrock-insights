from __future__ import annotations

from datetime import datetime, timedelta


def period_label(period: str) -> str:
    now = datetime.now()
    if period == "today":
        return f"Today ({now.strftime('%Y-%m-%d')})"
    if period == "yesterday":
        y = now - timedelta(days=1)
        return f"Yesterday ({y.strftime('%Y-%m-%d')})"
    if period == "week":
        return "Past 7 Days"
    return period


def since_label(value: str) -> str:
    unit_names = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    v = value.strip().lower()
    unit = unit_names.get(v[-1], v[-1])
    amount = v[:-1].rstrip()
    amount = amount.rstrip("0").rstrip(".") if "." in amount else amount
    return f"Last {amount} {unit}"
