"""
DailyBriefing — assembles and sends the 9 AM morning intelligence email.
Combines yesterday's Square recap, schedule vs. history flag, external
signals (weather, payday cycle, holidays, events), and a Claude narrative.
"""

import logging
from datetime import datetime

import anthropic

from config import Config
from external_signals import (
    get_payday_context,
    get_upcoming_holidays,
    get_upcoming_latino_events,
    get_weather_historical,
    get_weather_today,
)
from notifier import Notifier
from square_client import SquareDataClient
from square_history import SquareHistoryClient

log = logging.getLogger(__name__)


class DailyBriefing:
    """Builds and delivers the morning intelligence briefing email."""

    MODEL = "claude-sonnet-4-6"

    def __init__(
        self,
        config: Config,
        square: SquareDataClient,
        notifier: Notifier,
    ) -> None:
        self._config   = config
        self._square   = square
        self._history  = SquareHistoryClient(square)
        self._notifier = notifier
        self._client   = anthropic.Anthropic(api_key=config.anthropic_api_key) if config.claude_enabled else None

    # ── Data assembly ─────────────────────────────────────────────────────────

    def _build_context(self) -> dict:
        from datetime import timedelta
        now       = datetime.now(self._config.tz)
        yesterday = now - timedelta(days=1)
        return {
            "now":               now,
            "day_name":          self._config.DAY_NAMES.get(now.weekday(), now.strftime("%A")),
            "yesterday_day_name": self._config.DAY_NAMES.get(yesterday.weekday(), yesterday.strftime("%A")),
            "yesterday":         self._history.get_yesterday_summary(),
            "last_week_sales":   self._history.get_same_weekday_last_week_sales(),
            "weekly_history":    self._square.get_weekly_history(weeks=6),
            "weather_today":     get_weather_today(self._config),
            "weather_yesterday": get_weather_historical(
                self._config, yesterday.strftime("%Y-%m-%d")
            ),
            "payday":    get_payday_context(self._config),
            "holidays":  get_upcoming_holidays(self._config, days=10),
            "events":    get_upcoming_latino_events(self._config, days=7),
        }

    # ── Email body ────────────────────────────────────────────────────────────

    def _format_email(self, ctx: dict, claude_narrative: str) -> str:
        now       = ctx["now"]
        yest      = ctx["yesterday"]
        payday    = ctx["payday"]
        weather   = ctx["weather_today"]
        holidays  = ctx["holidays"]
        events    = ctx["events"]
        last_week = ctx["last_week_sales"]

        lines = [
            f"LA FLOR BLANCA — MORNING BRIEFING",
            f"{ctx['day_name']}, {now.strftime('%B %d, %Y')}",
            "",
        ]

        # Yesterday recap
        if yest:
            lines += [f"── YESTERDAY — {yest['date'].upper()} ─────────────────────", ""]
            vs = ""
            if last_week and last_week > 0 and yest["sales_cents"] > 0:
                diff = (yest["sales_cents"] - last_week) / last_week * 100
                direction = "above" if diff >= 0 else "below"
                vs = f"  ({abs(diff):.0f}% {direction} same day last week: ${last_week/100:,.0f})"
            lines.append(f"Sales:  ${yest['sales_cents']/100:,.2f}{vs}")
            lines.append(f"Labor:  {yest['labor_pct']:.1f}%  (${yest['labor_cents']/100:,.2f})")
            if yest["top_items"]:
                lines.append(f"Top sellers: {', '.join(f'{n} ({int(q)})' for n,q in yest['top_items'])}")
            if yest["slow_items"]:
                lines.append(f"Slowest:     {', '.join(f'{n} ({int(q)})' for n,q in yest['slow_items'])}")
            lines.append("")

        # Today's signals
        lines += ["── TODAY'S SIGNALS ─────────────────────────", ""]

        if weather:
            rain_note = " (rain forecast)" if weather["rain_in"] > 0.05 else ""
            lines.append(f"Weather:  High {weather['high_f']:.0f}°F / Low {weather['low_f']:.0f}°F{rain_note}")

        lines.append(f"Pay cycle: {payday['label']}")

        if holidays:
            lines.append(f"Upcoming: {', '.join(h['name'] for h in holidays[:3])}")
        if events:
            lines.append(f"Latino events nearby: {', '.join(e['name'] for e in events[:2])}")

        lines.append("")

        # Claude narrative
        if claude_narrative:
            lines += [
                "── AI BRIEFING ─────────────────────────────",
                "",
                claude_narrative,
                "",
            ]

        return "\n".join(lines)

    # ── Claude narrative ──────────────────────────────────────────────────────

    def _get_claude_narrative(self, ctx: dict) -> str:
        if not self._client:
            return ""

        yest           = ctx["yesterday"]
        payday         = ctx["payday"]
        weather        = ctx["weather_today"]
        holidays       = ctx["holidays"]
        events         = ctx["events"]
        last_week      = ctx["last_week_sales"]
        day_name       = ctx["day_name"]
        yest_day_name  = ctx.get("yesterday_day_name", day_name)

        # Build weekly pattern summary from history — compare same weekday as YESTERDAY
        daily_sales, daily_labor = ctx["weekly_history"]
        pattern_lines = ""
        if daily_sales and yest:
            same_day_totals = []
            for date_str, sales in daily_sales.items():
                try:
                    from datetime import datetime as dt
                    if dt.strptime(date_str, "%Y-%m-%d").weekday() == yest["weekday"]:
                        same_day_totals.append(sales)
                except ValueError:
                    pass
            if same_day_totals:
                avg = sum(same_day_totals) / len(same_day_totals)
                pattern_lines = f"Your last {len(same_day_totals)} {yest_day_name}s averaged ${avg/100:,.0f} in sales."

        yest_block = ""
        if yest:
            vs = ""
            if last_week and last_week > 0 and yest["sales_cents"] > 0:
                diff = (yest["sales_cents"] - last_week) / last_week * 100
                vs = f" ({abs(diff):.0f}% {'above' if diff >= 0 else 'below'} last {yest_day_name})"
            yest_block = (
                f"Yesterday: ${yest['sales_cents']/100:,.0f} in sales{vs}, "
                f"{yest['labor_pct']:.1f}% labor. "
                f"Top sellers: {', '.join(n for n,_ in yest['top_items'][:2]) or 'N/A'}."
            )

        weather_block = ""
        if weather:
            rain_note = " Rain forecasted." if weather["rain_in"] > 0.05 else ""
            hot_note  = " Very hot day." if weather["high_f"] > 92 else ""
            weather_block = f"Today's weather: {weather['high_f']:.0f}°F high.{hot_note}{rain_note}"

        holiday_block = f"Upcoming Salvadoran holidays: {', '.join(h['name'] for h in holidays[:2])}." if holidays else ""
        event_block   = f"Latino events nearby this week: {', '.join(e['name'] for e in events[:2])}." if events else ""

        # ── Rolling labor trend (last 6 days with valid data) ─────────────────
        recent_labor_pcts = [
            labor / sales * 100
            for date_str, sales in daily_sales.items()
            for labor in [daily_labor.get(date_str, 0)]
            if sales > 0 and labor > 0
        ][-6:]
        avg_labor = sum(recent_labor_pcts) / len(recent_labor_pcts) if recent_labor_pcts else 0
        labor_trend_block = (
            f"Recent labor avg (last {len(recent_labor_pcts)} days): {avg_labor:.1f}%"
            f" vs {self._config.labor_threshold * 100:.0f}% target."
            if recent_labor_pcts else ""
        )

        # ── Breach flag for yesterday ─────────────────────────────────────────
        threshold_pct = self._config.labor_threshold * 100
        breach_note = (
            f"NOTE: Yesterday's labor was OVER threshold at {yest['labor_pct']:.1f}%."
            if yest and yest["labor_pct"] > threshold_pct else ""
        )

        # ── Labor breach streak for today's weekday ───────────────────────────
        streak_block = ""
        if daily_sales and daily_labor:
            today_wd = ctx["now"].weekday()
            from datetime import datetime as _dt
            breach_hits, total_hits = 0, 0
            for date_str, sales in sorted(daily_sales.items(), reverse=True):
                if sales <= 0:
                    continue
                labor = daily_labor.get(date_str, 0)
                if labor <= 0:
                    continue
                try:
                    if _dt.strptime(date_str, "%Y-%m-%d").weekday() == today_wd:
                        total_hits += 1
                        if labor / sales * 100 > threshold_pct:
                            breach_hits += 1
                        if total_hits >= 4:
                            break
                except ValueError:
                    pass
            if total_hits >= 3 and breach_hits >= 3:
                streak_block = (
                    f"PATTERN: Labor has breached threshold {breach_hits} of the last "
                    f"{total_hits} {day_name}s — this is a schedule problem, not a today problem."
                )

        prompt = f"""You are a veteran restaurant operations advisor for La Flor Blanca, a lean Salvadoran restaurant in Los Angeles run by Alex.

It is {day_name} morning. Give Alex the one thing he needs to act on before he walks in the door — one clear decision and the specific reason why TODAY's context makes it the right call. Max 250 words.

DATA:
{yest_block}
{pattern_lines}
{labor_trend_block}
{breach_note}
{streak_block}
{weather_block}
Pay cycle: {payday['label']}
{holiday_block}
{event_block}

PRIORITY:
- If yesterday breached on labor, lead with what to do differently today — a specific staffing or operational call, not a general reminder to watch labor.
- If yesterday was fine on labor, focus on the sales opportunity: what today's signals (weather, pay cycle, events) mean for volume, and the one action Alex should take before noon.
- If there is a multi-week labor trend creeping up on the same weekday, call it plainly and say what specifically to change on the schedule.

Write like you're texting Alex before he gets to the restaurant — direct, no corporate language, no bullet points, no markdown, no asterisks, plain text only."""

        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=450,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            log.error("Claude daily briefing error: %s", exc)
            return ""

    # ── Entry point ───────────────────────────────────────────────────────────

    def send(self) -> None:
        log.info("Building daily morning briefing...")
        ctx             = self._build_context()
        claude_narrative = self._get_claude_narrative(ctx)
        body            = self._format_email(ctx, claude_narrative)
        now             = ctx["now"]
        subject         = f"La Flor Blanca — Morning Briefing {now.strftime('%a %b %d')}"
        self._notifier.send_email(subject, body)
        log.info("Morning briefing sent.")
