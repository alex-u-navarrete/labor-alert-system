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

        prompt = f"""You are an AI business advisor for La Flor Blanca, a Salvadoran restaurant in Los Angeles.
The owner is Alex. Right now the restaurant has a labor cost problem that needs immediate action.

CURRENT SITUATION ({day_name}, {time_str}):
- Labor cost: {labor_pct*100:.1f}% of sales (target is under {self._config.labor_threshold*100:.0f}%)
- Labor dollars: ${labor_cents/100:,.2f}
- Sales today: ${sales_cents/100:,.2f}
- Alert stage: {stage} (1=initial, 2=warning, 3=urgent)

STAFF CURRENTLY ON THE CLOCK:
{staff_lines}

SALES PERFORMANCE:
- {pace_line}
- Top sellers today: {top}
- Slowest sellers today: {bottom}

Give Alex 3–5 specific, actionable recommendations he can act on RIGHT NOW to bring labor back under target. Be direct and practical — no generic advice. Reference the actual staff names and menu items above where relevant. Keep it under 200 words."""

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
