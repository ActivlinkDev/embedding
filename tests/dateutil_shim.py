"""A purchase date roughly N whole months before today, for age fixtures."""

from datetime import date


def months_ago(months: int) -> str:
    today = date.today()
    y, m = divmod((today.year * 12 + today.month - 1) - months, 12)
    return date(y, m + 1, min(today.day, 28)).strftime("%Y-%m-%d")
