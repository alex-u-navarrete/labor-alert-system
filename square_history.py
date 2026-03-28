"""
SquareHistoryClient — briefing-specific Square data methods.
Pulls yesterday's recap and same-weekday comparison for the morning briefing.
Wraps a SquareDataClient instance to reuse its API client and utilities.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from square_client import SquareDataClient

log = logging.getLogger(__name__)


class SquareHistoryClient:
    """Fetches historical Square data for the daily morning briefing."""

    def __init__(self, square: SquareDataClient) -> None:
        self._sq     = square._sq
        self._config = square._config
        self._fmt    = square.fmt_utc

    def _day_range(self, day: datetime) -> tuple[str, str] | None:
        weekday = day.weekday()
        if weekday not in self._config.BUSINESS_HOURS:
            return None
        oh, om = map(int, self._config.BUSINESS_HOURS[weekday][0].split(":"))
        ch, cm = map(int, self._config.BUSINESS_HOURS[weekday][1].split(":"))
        return (
            self._fmt(day.replace(hour=oh, minute=om, second=0, microsecond=0)),
            self._fmt(day.replace(hour=ch, minute=cm, second=0, microsecond=0)),
        )

    def _fetch_sales(self, begin: str, end: str) -> float:
        total, cursor = 0.0, None
        while True:
            kwargs = dict(begin_time=begin, end_time=end,
                          location_id=self._config.square_location, sort_order="ASC")
            if cursor:
                kwargs["cursor"] = cursor
            result = self._sq.payments.list_payments(**kwargs)
            if not result.is_success():
                break
            for p in result.body.get("payments", []):
                total += (p.get("total_money", {}).get("amount", 0)
                          - p.get("tip_money", {}).get("amount", 0))
            cursor = result.body.get("cursor")
            if not cursor:
                break
        return total

    def get_yesterday_summary(self) -> dict:
        """Sales, labor %, top/slow items for yesterday."""
        yesterday = datetime.now(self._config.tz) - timedelta(days=1)
        rng = self._day_range(yesterday)
        if not rng:
            return {}
        begin, end = rng

        sales_cents = self._fetch_sales(begin, end)

        labor_cents = 0.0
        body = {"query": {"filter": {
            "location_ids": [self._config.square_location],
            "start": {"start_at": begin, "end_at": end},
        }}}
        result = self._sq.labor.search_shifts(body)
        if result.is_success():
            for shift in result.body.get("shifts", []):
                s_at = shift.get("start_at", end)
                e_at = shift.get("end_at") or end
                hrs  = max(0.0, (
                    datetime.fromisoformat(e_at.replace("Z", "+00:00"))
                    - datetime.fromisoformat(s_at.replace("Z", "+00:00"))
                ).total_seconds() / 3600)
                labor_cents += shift.get("wage", {}).get("hourly_rate", {}).get("amount", 0) * hrs

        items: dict = defaultdict(float)
        body2 = {
            "location_ids": [self._config.square_location],
            "query": {"filter": {
                "date_time_filter": {"created_at": {"start_at": begin, "end_at": end}},
                "state_filter": {"states": ["COMPLETED"]},
            }},
        }
        r2 = self._sq.orders.search_orders(body2)
        if r2.is_success():
            for order in r2.body.get("orders", []):
                for line in order.get("line_items", []):
                    items[line.get("name", "Unknown")] += float(line.get("quantity", "1"))

        sorted_items = sorted(items.items(), key=lambda x: x[1], reverse=True)
        return {
            "date":        yesterday.strftime("%A, %b %d"),
            "weekday":     yesterday.weekday(),
            "sales_cents": sales_cents,
            "labor_cents": labor_cents,
            "labor_pct":   (labor_cents / sales_cents * 100) if sales_cents > 0 else 0.0,
            "top_items":   sorted_items[:3],
            "slow_items":  sorted_items[-2:] if len(sorted_items) > 4 else [],
        }

    def get_same_weekday_last_week_sales(self) -> float | None:
        """Total sales for the same weekday last week — for the morning briefing comparison."""
        last_week = datetime.now(self._config.tz) - timedelta(weeks=1)
        rng = self._day_range(last_week)
        if not rng:
            return None
        total = self._fetch_sales(*rng)
        return total if total > 0 else None
