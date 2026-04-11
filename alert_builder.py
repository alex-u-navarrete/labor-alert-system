"""
AlertBuilder — constructs alert messages from Square data.
Handles labor alerts, sales pace, and weekly insight formatting.
"""

from collections import defaultdict
from datetime import datetime

from config import Config


class AlertBuilder:
    """Builds formatted alert strings for email delivery."""

    STAGE_LABELS = {1: "LABOR ALERT", 2: "LABOR WARNING", 3: "URGENT LABOR WARNING"}

    def __init__(self, config: Config) -> None:
        self._config = config

    def build_labor_alert(
        self,
        labor_pct: float,
        labor_cents: float,
        sales_cents: float,
        shift_details: list,
        item_sales: dict,
        hist_pace: float | None,
        stage: int,
        claude_section: str = "",
    ) -> str:
        """Returns a single email body with labor breakdown, sales pace, and optional AI section."""
        now           = datetime.now(self._config.tz)
        time_str      = now.strftime("%I:%M %p")
        labor_dollars = labor_cents / 100
        sales_dollars = sales_cents / 100
        header        = self.STAGE_LABELS.get(stage, "LABOR ALERT")

        # ── Labor breakdown ───────────────────────────────────────────────────
        lines = [
            f"LA FLOR BLANCA - {header}",
            f"{time_str} | Labor: {labor_pct*100:.1f}% (target: {self._config.labor_threshold*100:.0f}%)",
            f"Labor cost: ${labor_dollars:,.2f} | Sales: ${sales_dollars:,.2f}",
            "",
            "ON THE CLOCK RIGHT NOW:",
        ]

        open_shifts = [s for s in shift_details if s["status"] == "OPEN"]
        for s in open_shifts:
            lines.append(f"  {s['name']}: {s['hours']:.1f}h = ${s['cost_cents']/100:.2f}")

        if open_shifts:
            cut     = open_shifts[0]
            new_pct = (labor_cents - cut["cost_cents"]) / sales_cents * 100
            lines += [
                "",
                f"IF {cut['name'].upper()} LEFT NOW: saves ~${cut['cost_cents']/100:.2f}/hr, "
                f"drops labor to ~{new_pct:.1f}%",
            ]

        # ── Projected labor % by close ────────────────────────────────────────
        open_hour, open_min = map(int, self._config.BUSINESS_HOURS[now.weekday()][0].split(":"))
        close_hour, close_min = map(int, self._config.BUSINESS_HOURS[now.weekday()][1].split(":"))
        open_dt  = now.replace(hour=open_hour, minute=open_min, second=0, microsecond=0)
        close_dt = now.replace(hour=close_hour, minute=close_min, second=0, microsecond=0)
        elapsed_hours = max((now - open_dt).total_seconds() / 3600, 0.1)
        total_hours   = (close_dt - open_dt).total_seconds() / 3600
        if elapsed_hours > 0 and total_hours > elapsed_hours:
            projected_sales = sales_cents * (total_hours / elapsed_hours)
            projected_pct   = labor_cents / projected_sales * 100
            lines.append(f"Projected by close: ~{projected_pct:.1f}% labor if pace holds")

        # ── Sales pace + items ────────────────────────────────────────────────
        lines += ["", f"SALES PACE - {time_str}"]

        if hist_pace and hist_pace > 0:
            diff_pct  = (sales_cents - hist_pace) / hist_pace * 100
            direction = "above" if diff_pct >= 0 else "below"
            day_name  = self._config.DAY_NAMES.get(now.weekday(), "today")
            lines += [
                f"Today: ${sales_dollars:,.2f}",
                f"Typical {day_name} by now: ${hist_pace/100:,.2f}",
                f"Running {abs(diff_pct):.0f}% {direction} your usual pace",
            ]
        else:
            lines.append(f"Sales today: ${sales_dollars:,.2f}")

        if item_sales:
            sorted_items = sorted(item_sales.items(), key=lambda x: x[1], reverse=True)
            lines += ["", "TOP SELLERS TODAY:"]
            for name, qty in sorted_items[:3]:
                lines.append(f"  {name}: {int(qty)} sold")
            if len(sorted_items) > 4:
                lines += ["", "SLOWEST TODAY:"]
                for name, qty in sorted_items[-2:]:
                    lines.append(f"  {name}: {int(qty)} sold")

        suggestion = self._marketing_suggestion(item_sales, hist_pace, sales_cents, now)
        if suggestion:
            lines += ["", f"SUGGESTION: {suggestion}"]

        # ── Claude AI advisor section ─────────────────────────────────────────
        if claude_section:
            lines += ["", "─" * 40, "AI ADVISOR", "─" * 40, "", claude_section]

        return "\n".join(lines)

    def build_weekly_insight(self, daily_sales: dict, daily_labor: dict) -> str | None:
        """Returns the weekly insight message string, or None if insufficient data."""
        now = datetime.now(self._config.tz)

        weekday_pcts: dict = defaultdict(list)
        for date_str, sales in daily_sales.items():
            if sales <= 0:
                continue
            labor = daily_labor.get(date_str, 0)
            if labor <= 0:
                continue
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                if dt.weekday() in self._config.BUSINESS_HOURS:
                    weekday_pcts[dt.weekday()].append(labor / sales * 100)
            except ValueError:
                pass

        if not weekday_pcts:
            return None

        day_avgs    = {d: sum(p) / len(p) for d, p in weekday_pcts.items() if p}
        sorted_days = sorted(day_avgs.items(), key=lambda x: x[1], reverse=True)
        worst_day, worst_pct = sorted_days[0]
        best_day,  best_pct  = sorted_days[-1]

        lines = [
            "LA FLOR BLANCA - WEEKLY LABOR INSIGHT",
            "Based on last 8 weeks of data:", "",
            "AVG LABOR % BY DAY:",
        ]
        for day, pct in sorted(day_avgs.items()):
            flag = " << HIGH" if day == worst_day else ""
            lines.append(f"  {self._config.DAY_NAMES.get(day, '?')}: {pct:.1f}%{flag}")

        lines += [
            "",
            f"WATCH: {self._config.DAY_NAMES.get(worst_day, '?')} consistently runs highest "
            f"at {worst_pct:.1f}% avg labor. Consider trimming your schedule that day.",
            "",
            f"STRONG: {self._config.DAY_NAMES.get(best_day, '?')} is your most efficient day "
            f"at {best_pct:.1f}% avg.",
        ]

        from datetime import timedelta
        cutoff       = (now - timedelta(weeks=2)).strftime("%Y-%m-%d")
        recent_pcts, earlier_pcts = [], []
        for date_str, sales in daily_sales.items():
            if sales <= 0:
                continue
            labor = daily_labor.get(date_str, 0)
            if labor <= 0:
                continue
            pct = labor / sales * 100
            (recent_pcts if date_str >= cutoff else earlier_pcts).append(pct)

        if recent_pcts and earlier_pcts:
            recent_avg  = sum(recent_pcts)  / len(recent_pcts)
            earlier_avg = sum(earlier_pcts) / len(earlier_pcts)
            diff = recent_avg - earlier_avg
            if abs(diff) >= 1.0:
                direction = "UP" if diff > 0 else "DOWN"
                lines += [
                    "",
                    f"TREND: Overall labor is {direction} {abs(diff):.1f} points "
                    f"vs 2 weeks ago ({earlier_avg:.1f}% -> {recent_avg:.1f}%).",
                ]

        return "\n".join(lines)

    def _marketing_suggestion(
        self, item_sales: dict, hist_pace: float | None, sales_cents: float, now: datetime
    ) -> str:
        total_items = sum(item_sales.values()) if item_sales else 0
        drink_keywords = ["coffee", "drink", "juice", "soda", "water", "tea",
                          "latte", "agua", "jugo", "cafe", "bebida", "horchata"]
        drink_qty = sum(qty for name, qty in item_sales.items()
                        if any(k in name.lower() for k in drink_keywords))

        if total_items >= 5 and (drink_qty / total_items) < 0.15:
            return "Drinks are slow today. Offer a $1 upgrade at the counter to lift ticket size."

        if hist_pace and sales_cents < hist_pace * 0.80:
            hour = now.hour
            if hour < 13:
                return "Morning is running behind. A breakfast combo or daily special could drive traffic now."
            elif hour < 16:
                return "Slow afternoon. A limited-time lunch deal could bring in walk-ins."
            else:
                return "Evening is slow. Push a high-margin item or notify regulars of a special."

        if item_sales:
            top_item = max(item_sales, key=item_sales.get)
            return f"Your {top_item} is your top seller today — suggest it as an add-on to every order."

        return "Consider a combo deal or daily special to increase your average ticket size."
