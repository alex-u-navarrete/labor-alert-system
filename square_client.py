"""
SquareDataClient — all Square API interactions.
Handles labor shifts, payments, orders, and team member lookups.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from square.client import Client as SquareClient

from config import Config

log = logging.getLogger(__name__)


class SquareDataClient:
    """Fetches labor, sales, and order data from the Square API."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._sq     = SquareClient(access_token=config.square_token, environment="production")
        self._team_cache: dict = {}

    # ── Utilities ─────────────────────────────────────────────────────────────

    def fmt_utc(self, dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def today_range_utc(self) -> tuple:
        now   = datetime.now(self._config.tz)
        start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return self.fmt_utc(start), self.fmt_utc(end)

    def get_member_name(self, team_member_id: str) -> str:
        if team_member_id in self._team_cache:
            return self._team_cache[team_member_id]
        try:
            result = self._sq.team_members.retrieve_team_member(team_member_id)
            if result.is_success():
                m    = result.body.get("team_member", {})
                name = f"{m.get('given_name', '')} {m.get('family_name', '')}".strip()
                self._team_cache[team_member_id] = name or "Staff"
            else:
                self._team_cache[team_member_id] = "Staff"
        except Exception as exc:
            log.warning("Could not fetch team member %s: %s", team_member_id, exc)
            self._team_cache[team_member_id] = "Staff"
        return self._team_cache[team_member_id]

    # ── Labor ─────────────────────────────────────────────────────────────────

    def get_labor_data(self) -> tuple:
        """Returns (active_count, total_labor_cents, shift_details) for today."""
        begin, end = self.today_range_utc()
        now_str    = self.fmt_utc(datetime.now(timezone.utc))
        location   = self._config.square_location

        body = {"query": {"filter": {
            "location_ids": [location],
            "start": {"start_at": begin, "end_at": end},
        }}}

        active_count, total_labor_cents = 0, 0.0
        shift_details, cursor = [], None

        while True:
            if cursor:
                body["cursor"] = cursor
            result = self._sq.labor.search_shifts(body)
            if not result.is_success():
                log.error("Square Labor API error: %s", result.errors)
                return 0, 0.0, []

            for shift in result.body.get("shifts", []):
                status   = shift.get("status", "")
                start_at = shift.get("start_at", now_str)
                end_at   = shift.get("end_at") or now_str

                if status == "OPEN":
                    active_count += 1
                    end_at = now_str

                hourly_cents = shift.get("wage", {}).get("hourly_rate", {}).get("amount", 0)
                if hourly_cents == 0:
                    log.warning("Shift %s has no wage — labor cost understated.", shift.get("id", "?"))

                start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                end_dt   = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
                hours    = max(0.0, (end_dt - start_dt).total_seconds() / 3600)
                cost     = hourly_cents * hours
                total_labor_cents += cost

                member_id = shift.get("team_member_id", "")
                name      = self.get_member_name(member_id) if member_id else "Staff"
                shift_details.append({"name": name, "hours": hours, "cost_cents": cost, "status": status})

            cursor = result.body.get("cursor")
            if not cursor:
                break

        shift_details.sort(key=lambda x: x["cost_cents"], reverse=True)
        return active_count, total_labor_cents, shift_details

    # ── Sales ─────────────────────────────────────────────────────────────────

    def get_sales_cents(self) -> float | None:
        """Total sales today excluding tips. Returns None on API error."""
        begin, end = self.today_range_utc()
        total, cursor = 0.0, None

        while True:
            kwargs = dict(begin_time=begin, end_time=end,
                          location_id=self._config.square_location, sort_order="ASC")
            if cursor:
                kwargs["cursor"] = cursor
            result = self._sq.payments.list_payments(**kwargs)
            if not result.is_success():
                log.error("Square Payments API error: %s", result.errors)
                return None
            for p in result.body.get("payments", []):
                total += p.get("total_money", {}).get("amount", 0) - p.get("tip_money", {}).get("amount", 0)
            cursor = result.body.get("cursor")
            if not cursor:
                break
        return total

    def get_item_sales(self) -> dict:
        """Returns {item_name: quantity} for all completed orders today."""
        begin, end = self.today_range_utc()
        items, cursor = defaultdict(float), None

        while True:
            body = {
                "location_ids": [self._config.square_location],
                "query": {"filter": {
                    "date_time_filter": {"created_at": {"start_at": begin, "end_at": end}},
                    "state_filter": {"states": ["COMPLETED"]},
                }},
            }
            if cursor:
                body["cursor"] = cursor
            result = self._sq.orders.search_orders(body)
            if not result.is_success():
                log.error("Square Orders API error: %s", result.errors)
                return {}
            for order in result.body.get("orders", []):
                for line in order.get("line_items", []):
                    items[line.get("name", "Unknown item")] += float(line.get("quantity", "1"))
            cursor = result.body.get("cursor")
            if not cursor:
                break
        return dict(items)

    def get_historical_pace(self) -> float | None:
        """Average sales for the same weekday over the last 4 weeks up to now."""
        now, totals = datetime.now(self._config.tz), []

        for weeks_back in range(1, 5):
            past_day = now - timedelta(weeks=weeks_back)
            weekday  = past_day.weekday()
            if weekday not in self._config.BUSINESS_HOURS:
                continue
            oh, om     = map(int, self._config.BUSINESS_HOURS[weekday][0].split(":"))
            day_start  = past_day.replace(hour=oh, minute=om, second=0, microsecond=0)
            day_cutoff = past_day.replace(hour=now.hour, minute=now.minute, second=0, microsecond=0)
            if day_cutoff <= day_start:
                continue

            day_total, cursor = 0.0, None
            while True:
                kwargs = dict(begin_time=self.fmt_utc(day_start), end_time=self.fmt_utc(day_cutoff),
                              location_id=self._config.square_location, sort_order="ASC")
                if cursor:
                    kwargs["cursor"] = cursor
                result = self._sq.payments.list_payments(**kwargs)
                if not result.is_success():
                    break
                for p in result.body.get("payments", []):
                    day_total += (p.get("total_money", {}).get("amount", 0)
                                  - p.get("tip_money", {}).get("amount", 0))
                cursor = result.body.get("cursor")
                if not cursor:
                    break
            if day_total > 0:
                totals.append(day_total)

        return sum(totals) / len(totals) if totals else None

    def get_hourly_sales_history(self, hours_ahead: int = 2) -> dict:
        """
        Returns average sales per hour-of-day for the same weekday over last 4 weeks.
        Keys are hour integers (0–23), values are average sales in cents.
        Used by Claude to judge whether the next 1–2 hours are likely to pick up.
        """
        now     = datetime.now(self._config.tz)
        buckets: dict = defaultdict(list)

        for weeks_back in range(1, 5):
            past_day = now - timedelta(weeks=weeks_back)
            if past_day.weekday() not in self._config.BUSINESS_HOURS:
                continue
            oh, om    = map(int, self._config.BUSINESS_HOURS[past_day.weekday()][0].split(":"))
            ch, cm    = map(int, self._config.BUSINESS_HOURS[past_day.weekday()][1].split(":"))
            day_start = past_day.replace(hour=oh, minute=om, second=0, microsecond=0)
            day_end   = past_day.replace(hour=ch, minute=cm, second=0, microsecond=0)

            hourly: dict = defaultdict(float)
            cursor = None
            while True:
                kwargs = dict(begin_time=self.fmt_utc(day_start), end_time=self.fmt_utc(day_end),
                              location_id=self._config.square_location, sort_order="ASC")
                if cursor:
                    kwargs["cursor"] = cursor
                result = self._sq.payments.list_payments(**kwargs)
                if not result.is_success():
                    break
                for p in result.body.get("payments", []):
                    ts  = p.get("created_at", "")
                    hr  = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(self._config.tz).hour
                    amt = p.get("total_money", {}).get("amount", 0) - p.get("tip_money", {}).get("amount", 0)
                    hourly[hr] += amt
                cursor = result.body.get("cursor")
                if not cursor:
                    break
            for hr, amt in hourly.items():
                buckets[hr].append(amt)

        return {hr: sum(v) / len(v) for hr, v in buckets.items() if v}

    def get_weekly_history(self, weeks: int = 8) -> tuple:
        """Returns (daily_sales, daily_labor) dicts for the last N weeks."""
        now      = datetime.now(self._config.tz)
        end_dt   = now.replace(hour=23, minute=59, second=59, microsecond=0)
        start_dt = end_dt - timedelta(weeks=weeks)
        begin, end = self.fmt_utc(start_dt), self.fmt_utc(end_dt)

        daily_sales: dict = defaultdict(float)
        cursor = None
        while True:
            kwargs = dict(begin_time=begin, end_time=end,
                          location_id=self._config.square_location, sort_order="ASC")
            if cursor:
                kwargs["cursor"] = cursor
            result = self._sq.payments.list_payments(**kwargs)
            if not result.is_success():
                log.error("Weekly history: payments API error %s", result.errors)
                return {}, {}
            for p in result.body.get("payments", []):
                date_str = p.get("created_at", "")[:10]
                daily_sales[date_str] += (p.get("total_money", {}).get("amount", 0)
                                          - p.get("tip_money", {}).get("amount", 0))
            cursor = result.body.get("cursor")
            if not cursor:
                break

        daily_labor: dict = defaultdict(float)
        now_str = self.fmt_utc(datetime.now(timezone.utc))
        body    = {"query": {"filter": {
            "location_ids": [self._config.square_location],
            "start": {"start_at": begin, "end_at": end},
        }}}
        cursor = None
        while True:
            if cursor:
                body["cursor"] = cursor
            result = self._sq.labor.search_shifts(body)
            if not result.is_success():
                log.error("Weekly history: labor API error %s", result.errors)
                return {}, {}
            for shift in result.body.get("shifts", []):
                start_at = shift.get("start_at", now_str)
                end_at   = shift.get("end_at") or now_str
                date_str = start_at[:10]
                hourly   = shift.get("wage", {}).get("hourly_rate", {}).get("amount", 0)
                s_dt     = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                e_dt     = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
                hours    = max(0.0, (e_dt - s_dt).total_seconds() / 3600)
                daily_labor[date_str] += hourly * hours
            cursor = result.body.get("cursor")
            if not cursor:
                break

        return dict(daily_sales), dict(daily_labor)
