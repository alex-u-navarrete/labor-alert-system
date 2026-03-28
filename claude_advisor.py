"""
ClaudeAdvisor — calls the Anthropic API to generate AI-powered business advice.
Returns an advisory string appended to email alerts.
"""

import logging
from datetime import datetime

import anthropic

from config import Config

log = logging.getLogger(__name__)


class ClaudeAdvisor:
    """Generates AI business advice using Claude API based on real-time Square data."""

    MODEL = "claude-sonnet-4-6"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def get_labor_advice(
        self,
        labor_pct: float,
        labor_cents: float,
        sales_cents: float,
        shift_details: list,
        item_sales: dict,
        hist_pace: float | None,
        stage: int,
        hourly_history: dict | None = None,
    ) -> str:
        """Returns a Claude-generated advisory string for a labor breach email."""
        now      = datetime.now(self._config.tz)
        day_name = self._config.DAY_NAMES.get(now.weekday(), now.strftime("%A"))
        time_str = now.strftime("%I:%M %p")

        # Build staff-on-clock summary
        open_shifts = [s for s in shift_details if s["status"] == "OPEN"]
        staff_lines = "\n".join(
            f"  - {s['name']}: {s['hours']:.1f}h clocked in, ~${s['cost_cents']/100:.2f} so far"
            for s in open_shifts
        ) or "  - No open shifts found"

        # Build item sales summary
        if item_sales:
            sorted_items = sorted(item_sales.items(), key=lambda x: x[1], reverse=True)
            top    = ", ".join(f"{n} ({int(q)} sold)" for n, q in sorted_items[:3])
            bottom = ", ".join(f"{n} ({int(q)} sold)" for n, q in sorted_items[-2:]) if len(sorted_items) > 4 else "N/A"
        else:
            top = bottom = "No data"

        # Build pace context
        if hist_pace and hist_pace > 0:
            diff_pct  = (sales_cents - hist_pace) / hist_pace * 100
            direction = "above" if diff_pct >= 0 else "below"
            pace_line = f"Running {abs(diff_pct):.0f}% {direction} the typical {day_name} pace by this hour"
        else:
            pace_line = "No historical pace data available for comparison"

        urgency = {1: "just crossed the threshold", 2: "been over threshold for hours", 3: "CRITICAL — hours over threshold and escalating"}[stage]

        # Build next-2-hours trajectory from historical hourly data
        trajectory_lines = ""
        if hourly_history:
            upcoming = [(h, hourly_history[h]) for h in range(now.hour + 1, now.hour + 3) if h in hourly_history]
            if upcoming:
                trajectory_lines = "\nHISTORICAL TRAJECTORY FOR THE NEXT 2 HOURS (same weekday, last 4 weeks avg):\n"
                trajectory_lines += "\n".join(
                    f"  {datetime(2000,1,1,h).strftime('%I %p')}: avg ${amt/100:,.0f} in sales"
                    for h, amt in upcoming
                )

        prompt = f"""You are a veteran restaurant operations consultant who has managed high-volume independent restaurants for 20+ years. You think in terms of covers, labor dollars per hour, ticket averages, and contribution margin — not vague suggestions.

You are advising Alex, the owner-operator of La Flor Blanca, an authentic Salvadoran restaurant in Los Angeles. Alex runs a lean operation — pupusas, tamales, traditional plates — with a small crew. He knows his restaurant. Give him operator-level advice, not textbook advice.

RIGHT NOW — {day_name} at {time_str}:
- Labor is at {labor_pct*100:.1f}% of sales. Target is under {self._config.labor_threshold*100:.0f}%. That's {(labor_pct - self._config.labor_threshold)*100:.1f} points over.
- Labor dollars on the clock: ${labor_cents/100:,.2f}
- Sales so far today: ${sales_cents/100:,.2f}
- Situation: {urgency}

WHO IS ON THE CLOCK:
{staff_lines}

SALES CONTEXT:
- {pace_line}
- Moving well: {top}
- Barely moving: {bottom}
{trajectory_lines}
CRITICAL INSTRUCTION: Use the trajectory data above to make a judgment call. If the next hour historically brings a significant sales jump, factor that into whether Alex should cut staff now or hold. If the next hour is historically flat or slow, cutting is the right move. Do the math — show what labor % looks like if he cuts the most expensive person AND if sales hit the historical average for the next hour.

Your job: Give Alex 3–4 blunt, specific moves he can make in the next 30 minutes. Think like an operator standing on the line next to him — not someone writing a report. Use the actual staff names and menu items above. No bullet-point fluff, no "consider" language. Tell him exactly what to do and why it moves the number. If sales are behind pace, tell him which item to push and exactly how."""

        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            log.error("Claude API error: %s", exc)
            return ""
