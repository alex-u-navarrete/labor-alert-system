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
                trajectory_lines = "\nHISTORICAL NEXT 2 HOURS (same weekday, last 4 weeks avg):\n"
                trajectory_lines += "\n".join(
                    f"  {datetime(2000,1,1,h).strftime('%I %p')}: avg ${amt/100:,.0f} in sales"
                    for h, amt in upcoming
                )

        # Flag staff who may be in or near a legally required break window (California law)
        break_notes = []
        for s in open_shifts:
            h = s["hours"]
            if 3.5 <= h < 4.0:
                break_notes.append(f"  {s['name']} ({h:.1f}h) — likely due for or mid 10-min paid rest break")
            elif 4.5 <= h < 5.5:
                break_notes.append(f"  {s['name']} ({h:.1f}h) — approaching 30-min unpaid meal break (CA law, 5h mark)")
            elif h >= 5.5:
                break_notes.append(f"  {s['name']} ({h:.1f}h) — past meal break, cuttable now if volume warrants")
        break_context = ("\nCALIFORNIA BREAK STATUS:\n" + "\n".join(break_notes)) if break_notes else ""

        prompt = f"""You are a veteran restaurant operations consultant who has run independent restaurants in Los Angeles for 20+ years. You think in labor dollars per hour, ticket averages, item contribution margin, and kitchen throughput — not generic management advice.

You are advising Alex, the owner-operator of La Flor Blanca, an authentic Salvadoran restaurant in Los Angeles. Small crew, lean operation.

CRITICAL MENU KNOWLEDGE — factor this into every recommendation:
- Pupusas are LABOR-HEAVY: each one is 8–10 minutes of skilled hand work (patting masa, stuffing, cooking). High volume on pupusas during a labor breach makes it worse, not better. Do NOT tell Alex to push pupusas when he's already over on labor.
- HIGH-MARGIN, LOW-LABOR items to push instead: drinks (horchata, agua fresca, sodas — near-zero prep, ~80% margin), tamales (already made, just heat), plantains (simple fry), packaged items. These move the ticket average without adding kitchen labor.
- A "special" only makes sense if it doesn't create more kitchen work. A verbal upsell at the counter ("want a drink with that?") costs nothing. A new combo that requires more pupusa production makes the labor problem worse.
- Do NOT suggest dynamic pricing or POS changes — too slow to execute and margins aren't calculated yet.

RIGHT NOW — {day_name} at {time_str}:
- Labor: {labor_pct*100:.1f}% of sales (target: under {self._config.labor_threshold*100:.0f}%, currently {(labor_pct - self._config.labor_threshold)*100:.1f} points over)
- Labor on the clock: ${labor_cents/100:,.2f} | Sales today: ${sales_cents/100:,.2f}
- Alert stage: {urgency}

WHO IS ON THE CLOCK (sorted by cost, highest first):
{staff_lines}
{break_context}
SALES CONTEXT:
- {pace_line}
- Moving: {top}
- Stagnant: {bottom}
{trajectory_lines}

YOUR JOB — give Alex 3–4 blunt, specific calls:

1. CUT OR HOLD: Use the trajectory data. If the next hour historically spikes, do the math — show labor % if sales hit that average AND if he cuts the top earner. If it's flat, cut immediately. But check break status first — never cut someone mid-break or before they've hit their legal break window. Name the person, state the math.

2. WHAT TO PUSH RIGHT NOW: Only low-labor items. Drinks, tamales, sides. Tell him exactly how to move them — a counter ask, a table walkthrough, a handwritten sign. Not pupusas if labor is the problem.

3. FORWARD LOOK: What does the next 90 minutes look like based on history? What's the decision point — the specific time or sales number where he should reassess cutting vs. holding?

Be direct. Do the math inline. No hedging, no "consider," no textbook language."""

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
