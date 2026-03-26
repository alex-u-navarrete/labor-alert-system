"""
La Flor Blanca — Square Labor Cost Alert System (Enhanced)

Every 30 min during business hours:
  - Checks labor cost % against threshold
  - If over threshold, sends 2 enriched texts:
      Text 1: who's on the clock, cost per person, who to cut
      Text 2: sales pace vs historical, top/slow items, marketing suggestion
  - Escalates every 2 hours while still in breach (max 3 alert bursts)

Every Monday at 8:30 AM:
  - Sends weekly labor pattern insight based on last 8 weeks of data
"""

import os
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from square import Square as SquareClient
from twilio.rest import Client as TwilioClient

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────
def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Required environment variable '{name}' is not set.")
    return val


SQUARE_TOKEN      = require_env("SQUARE_ACCESS_TOKEN")
SQUARE_LOCATION   = require_env("SQUARE_LOCATION_ID")
TWILIO_SID        = require_env("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN      = require_env("TWILIO_AUTH_TOKEN")
TWILIO_FROM       = require_env("TWILIO_FROM_NUMBER")
ALERT_PHONES      = [p.strip() for p in require_env("ALERT_PHONE_NUMBERS").split(",")]
TZ_NAME           = os.environ.get("TIMEZONE", "America/Los_Angeles")
LABOR_THRESHOLD   = float(os.environ.get("LABOR_THRESHOLD_PCT", "33")) / 100
ESCALATION_HOURS  = float(os.environ.get("ESCALATION_HOURS", "2"))

TZ = pytz.timezone(TZ_NAME)

# Python weekday(): 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
BUSINESS_HOURS: dict = {
    0: ("09:30", "19:00"),  # Monday
    2: ("09:30", "19:00"),  # Wednesday
    3: ("09:30", "19:00"),  # Thursday
    4: ("09:30", "19:30"),  # Friday
    5: ("09:30", "19:30"),  # Saturday
    6: ("09:30", "19:30"),  # Sunday
}

DAY_NAMES = {
    0: "Monday", 2: "Wednesday", 3: "Thursday",
    4: "Friday", 5: "Saturday",  6: "Sunday",
}

# ── API clients ───────────────────────────────────────────────────────────────
sq     = SquareClient(token=SQUARE_TOKEN)
twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)

# ── Team member name cache (id -> "First Last") ───────────────────────────────
_team_cache: dict = {}

# ── Breach state ──────────────────────────────────────────────────────────────
# alert_stage: 0=none sent  1=initial sent  2=2hr escalation sent  3=4hr final sent
state: dict = {
    "in_breach":    False,
    "breach_start": None,
    "alert_stage":  0,
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def is_business_hours() -> bool:
    now = datetime.now(TZ)
    day = now.weekday()
    if day not in BUSINESS_HOURS:
        return False
    oh, om = map(int, BUSINESS_HOURS[day][0].split(":"))
    ch, cm = map(int, BUSINESS_HOURS[day][1].split(":"))
    open_dt  = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return open_dt <= now <= close_dt


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_range_utc() -> tuple:
    now   = datetime.now(TZ)
    start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return fmt_utc(start), fmt_utc(end)


def reset_breach_state() -> None:
    state.update(in_breach=False, breach_start=None, alert_stage=0)


def get_member_name(team_member_id: str) -> str:
    """Fetch employee name from Square, with local cache."""
    if team_member_id in _team_cache:
        return _team_cache[team_member_id]
    try:
        result = sq.team_members.retrieve_team_member(team_member_id)
        if result.is_success():
            m    = result.body.get("team_member", {})
            name = f"{m.get('given_name', '')} {m.get('family_name', '')}".strip()
            _team_cache[team_member_id] = name or "Staff"
        else:
            _team_cache[team_member_id] = "Staff"
    except Exception as exc:
        log.warning("Could not fetch team member %s: %s", team_member_id, exc)
        _team_cache[team_member_id] = "Staff"
    return _team_cache[team_member_id]


# ── Square data fetchers ──────────────────────────────────────────────────────
def get_labor_data() -> tuple:
    """
    Returns (active_count, total_labor_cents, shift_details).
    shift_details is a list of dicts sorted by cost descending.
    """
    begin, end = today_range_utc()
    now_str    = fmt_utc(datetime.now(timezone.utc))

    body = {
        "query": {
            "filter": {
                "location_ids": [SQUARE_LOCATION],
                "start": {"start_at": begin, "end_at": end},
            }
        }
    }

    active_count      = 0
    total_labor_cents = 0.0
    shift_details     = []
    cursor            = None

    while True:
        if cursor:
            body["cursor"] = cursor

        result = sq.labor.search_shifts(body)
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

            hourly_cents = (
                shift.get("wage", {})
                     .get("hourly_rate", {})
                     .get("amount", 0)
            )
            if hourly_cents == 0:
                log.warning(
                    "Shift %s has no wage set in Square — labor cost understated.",
                    shift.get("id", "?"),
                )

            start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
            hours    = max(0.0, (end_dt - start_dt).total_seconds() / 3600)
            cost     = hourly_cents * hours
            total_labor_cents += cost

            member_id = shift.get("team_member_id", "")
            name      = get_member_name(member_id) if member_id else "Staff"

            shift_details.append({
                "name":       name,
                "hours":      hours,
                "cost_cents": cost,
                "status":     status,
            })

        cursor = result.body.get("cursor")
        if not cursor:
            break

    # Most expensive first — that's who to cut
    shift_details.sort(key=lambda x: x["cost_cents"], reverse=True)
    return active_count, total_labor_cents, shift_details


def get_sales_cents() -> float | None:
    """Total sales today (excluding tips)."""
    begin, end = today_range_utc()
    total      = 0.0
    cursor     = None

    while True:
        kwargs = dict(begin_time=begin, end_time=end, location_id=SQUARE_LOCATION, sort_order="ASC")
        if cursor:
            kwargs["cursor"] = cursor

        result = sq.payments.list_payments(**kwargs)
        if not result.is_success():
            log.error("Square Payments API error: %s", result.errors)
            return None

        for p in result.body.get("payments", []):
            total_m = p.get("total_money", {}).get("amount", 0)
            tip_m   = p.get("tip_money",   {}).get("amount", 0)
            total  += total_m - tip_m

        cursor = result.body.get("cursor")
        if not cursor:
            break

    return total


def get_item_sales() -> dict:
    """Returns {item_name: quantity} for all completed orders today."""
    begin, end = today_range_utc()
    items      = defaultdict(float)
    cursor     = None

    while True:
        body = {
            "location_ids": [SQUARE_LOCATION],
            "query": {
                "filter": {
                    "date_time_filter": {
                        "created_at": {"start_at": begin, "end_at": end}
                    },
                    "state_filter": {"states": ["COMPLETED"]},
                }
            },
        }
        if cursor:
            body["cursor"] = cursor

        result = sq.orders.search_orders(body)
        if not result.is_success():
            log.error("Square Orders API error: %s", result.errors)
            return {}

        for order in result.body.get("orders", []):
            for line in order.get("line_items", []):
                name = line.get("name", "Unknown item")
                qty  = float(line.get("quantity", "1"))
                items[name] += qty

        cursor = result.body.get("cursor")
        if not cursor:
            break

    return dict(items)


def get_historical_pace() -> float | None:
    """
    Average sales for the same weekday over the last 4 weeks,
    measured only up to the current time of day.
    Returns cents, or None if not enough history.
    """
    now    = datetime.now(TZ)
    totals = []

    for weeks_back in range(1, 5):
        past_day = now - timedelta(weeks=weeks_back)
        weekday  = past_day.weekday()
        if weekday not in BUSINESS_HOURS:
            continue

        oh, om      = map(int, BUSINESS_HOURS[weekday][0].split(":"))
        day_start   = past_day.replace(hour=oh,       minute=om,       second=0, microsecond=0)
        day_cutoff  = past_day.replace(hour=now.hour, minute=now.minute, second=0, microsecond=0)

        if day_cutoff <= day_start:
            continue

        day_total = 0.0
        cursor    = None
        while True:
            kwargs = dict(
                begin_time=fmt_utc(day_start),
                end_time=fmt_utc(day_cutoff),
                location_id=SQUARE_LOCATION,
                sort_order="ASC",
            )
            if cursor:
                kwargs["cursor"] = cursor
            result = sq.payments.list_payments(**kwargs)
            if not result.is_success():
                break
            for p in result.body.get("payments", []):
                day_total += (
                    p.get("total_money", {}).get("amount", 0)
                    - p.get("tip_money", {}).get("amount", 0)
                )
            cursor = result.body.get("cursor")
            if not cursor:
                break

        if day_total > 0:
            totals.append(day_total)

    return sum(totals) / len(totals) if totals else None


# ── Marketing suggestion ──────────────────────────────────────────────────────
def marketing_suggestion(item_sales: dict, hist_pace: float | None, sales_cents: float, now: datetime) -> str:
    total_items = sum(item_sales.values()) if item_sales else 0

    # Drinks underperforming?
    drink_keywords = ["coffee", "drink", "juice", "soda", "water", "tea",
                      "latte", "agua", "jugo", "cafe", "bebida", "horchata"]
    drink_qty = sum(qty for name, qty in item_sales.items()
                    if any(k in name.lower() for k in drink_keywords))

    if total_items >= 5 and (drink_qty / total_items) < 0.15:
        return "Drinks are slow today. Offer a $1 upgrade at the counter to lift ticket size."

    # Sales pace slow?
    if hist_pace and sales_cents < hist_pace * 0.80:
        hour = now.hour
        if hour < 13:
            return "Morning is running behind. A breakfast combo or daily special could drive traffic now."
        elif hour < 16:
            return "Slow afternoon. A limited-time lunch deal could bring in walk-ins."
        else:
            return "Evening is slow. Push a high-margin item or notify regulars of a special."

    # Top seller upsell
    if item_sales:
        top_item = max(item_sales, key=item_sales.get)
        return f"Your {top_item} is your top seller today — suggest it as an add-on to every order."

    return "Consider a combo deal or daily special to increase your average ticket size."


# ── Alert builder ─────────────────────────────────────────────────────────────
def build_alerts(
    labor_pct: float,
    labor_cents: float,
    sales_cents: float,
    shift_details: list,
    stage: int,
) -> list:
    """Returns [text1, text2] to send as two separate SMS."""
    now           = datetime.now(TZ)
    time_str      = now.strftime("%I:%M %p")
    labor_dollars = labor_cents / 100
    sales_dollars = sales_cents / 100

    labels = {1: "LABOR ALERT", 2: "LABOR WARNING", 3: "URGENT LABOR WARNING"}
    header = labels.get(stage, "LABOR ALERT")

    # ── Text 1: Labor breakdown + who to cut ──────────────────────────────
    lines1 = [
        f"LA FLOR BLANCA - {header}",
        f"{time_str} | Labor: {labor_pct*100:.1f}% (target: {LABOR_THRESHOLD*100:.0f}%)",
        f"Labor cost: ${labor_dollars:,.2f} | Sales: ${sales_dollars:,.2f}",
        "",
        "ON THE CLOCK RIGHT NOW:",
    ]

    open_shifts = [s for s in shift_details if s["status"] == "OPEN"]
    for s in open_shifts:
        lines1.append(f"  {s['name']}: {s['hours']:.1f}h = ${s['cost_cents']/100:.2f}")

    if open_shifts:
        cut     = open_shifts[0]  # highest cost (sorted descending)
        new_pct = (labor_cents - cut["cost_cents"]) / sales_cents * 100
        lines1 += [
            "",
            f"ACTION: Send {cut['name']} home now.",
            f"Saves ~${cut['cost_cents']/100:.2f}/hr, drops labor to ~{new_pct:.1f}%",
        ]

    # ── Text 2: Sales pace + items + marketing ────────────────────────────
    item_sales = get_item_sales()
    hist_pace  = get_historical_pace()

    lines2 = [f"SALES PACE - {time_str}"]

    if hist_pace and hist_pace > 0:
        diff_pct  = (sales_cents - hist_pace) / hist_pace * 100
        direction = "above" if diff_pct >= 0 else "below"
        day_name  = DAY_NAMES.get(now.weekday(), "today")
        lines2 += [
            f"Today: ${sales_dollars:,.2f}",
            f"Typical {day_name} by now: ${hist_pace/100:,.2f}",
            f"Running {abs(diff_pct):.0f}% {direction} your usual pace",
        ]
    else:
        lines2.append(f"Sales today: ${sales_dollars:,.2f}")

    if item_sales:
        sorted_items = sorted(item_sales.items(), key=lambda x: x[1], reverse=True)

        lines2 += ["", "TOP SELLERS TODAY:"]
        for name, qty in sorted_items[:3]:
            lines2.append(f"  {name}: {int(qty)} sold")

        if len(sorted_items) > 4:
            lines2 += ["", "SLOWEST TODAY:"]
            for name, qty in sorted_items[-2:]:
                lines2.append(f"  {name}: {int(qty)} sold")

    suggestion = marketing_suggestion(item_sales, hist_pace, sales_cents, now)
    if suggestion:
        lines2 += ["", f"SUGGESTION: {suggestion}"]

    return ["\n".join(lines1), "\n".join(lines2)]


# ── SMS sender ────────────────────────────────────────────────────────────────
def send_sms(message: str) -> None:
    for phone in ALERT_PHONES:
        try:
            twilio.messages.create(body=message, from_=TWILIO_FROM, to=phone)
            log.info("SMS sent to %s", phone)
        except Exception as exc:
            log.error("Failed SMS to %s: %s", phone, exc)


# ── Weekly insight ────────────────────────────────────────────────────────────
def weekly_insight() -> None:
    """Runs every Monday at 8:30 AM. Analyzes 8 weeks of history by weekday."""
    log.info("Running weekly insight...")
    now      = datetime.now(TZ)
    end_dt   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start_dt = end_dt - timedelta(weeks=8)

    begin = fmt_utc(start_dt)
    end   = fmt_utc(end_dt)

    # Collect daily sales
    daily_sales: dict = defaultdict(float)
    cursor = None
    while True:
        kwargs = dict(begin_time=begin, end_time=end, location_id=SQUARE_LOCATION, sort_order="ASC")
        if cursor:
            kwargs["cursor"] = cursor
        result = sq.payments.list_payments(**kwargs)
        if not result.is_success():
            log.error("Weekly insight: payments API error %s", result.errors)
            return
        for p in result.body.get("payments", []):
            date_str = p.get("created_at", "")[:10]
            amt = (
                p.get("total_money", {}).get("amount", 0)
                - p.get("tip_money", {}).get("amount", 0)
            )
            daily_sales[date_str] += amt
        cursor = result.body.get("cursor")
        if not cursor:
            break

    # Collect daily labor
    daily_labor: dict = defaultdict(float)
    now_str = fmt_utc(datetime.now(timezone.utc))
    body    = {
        "query": {
            "filter": {
                "location_ids": [SQUARE_LOCATION],
                "start": {"start_at": begin, "end_at": end},
            }
        }
    }
    cursor = None
    while True:
        if cursor:
            body["cursor"] = cursor
        result = sq.labor.search_shifts(body)
        if not result.is_success():
            log.error("Weekly insight: labor API error %s", result.errors)
            return
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

    # Calculate labor % per weekday
    weekday_pcts: dict = defaultdict(list)
    for date_str, sales in daily_sales.items():
        if sales <= 0:
            continue
        labor = daily_labor.get(date_str, 0)
        if labor <= 0:
            continue
        try:
            dt      = datetime.strptime(date_str, "%Y-%m-%d")
            weekday = dt.weekday()
            if weekday in BUSINESS_HOURS:
                weekday_pcts[weekday].append(labor / sales * 100)
        except ValueError:
            pass

    if not weekday_pcts:
        log.info("Weekly insight: not enough historical data yet.")
        return

    day_avgs    = {d: sum(p) / len(p) for d, p in weekday_pcts.items() if p}
    sorted_days = sorted(day_avgs.items(), key=lambda x: x[1], reverse=True)
    worst_day, worst_pct = sorted_days[0]
    best_day,  best_pct  = sorted_days[-1]

    lines = [
        "LA FLOR BLANCA - WEEKLY LABOR INSIGHT",
        "Based on last 8 weeks of data:",
        "",
        "AVG LABOR % BY DAY:",
    ]
    for day, pct in sorted(day_avgs.items()):
        flag = " << HIGH" if day == worst_day else ""
        lines.append(f"  {DAY_NAMES.get(day, '?')}: {pct:.1f}%{flag}")

    lines += [
        "",
        f"WATCH: {DAY_NAMES.get(worst_day, '?')} consistently runs highest "
        f"at {worst_pct:.1f}% avg labor. Consider trimming your schedule that day.",
        "",
        f"STRONG: {DAY_NAMES.get(best_day, '?')} is your most efficient day "
        f"at {best_pct:.1f}% avg.",
    ]

    # 4-week trend
    cutoff       = (now - timedelta(weeks=2)).strftime("%Y-%m-%d")
    recent_pcts  = []
    earlier_pcts = []
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

    send_sms("\n".join(lines))
    log.info("Weekly insight sent.")


# ── Main labor check ──────────────────────────────────────────────────────────
def check_labor() -> None:
    now = datetime.now(TZ)

    if not is_business_hours():
        log.info("Outside business hours (%s) — skipping.", now.strftime("%a %H:%M %Z"))
        if state["in_breach"]:
            log.info("Business day ended — resetting breach state.")
            reset_breach_state()
        return

    log.info("--- Check at %s ---", now.strftime("%Y-%m-%d %H:%M %Z"))

    active_count, labor_cents, shift_details = get_labor_data()

    if active_count == 0:
        log.info("No employees clocked in — skipping alert logic.")
        if state["in_breach"]:
            reset_breach_state()
        return

    sales_cents = get_sales_cents()
    if sales_cents is None:
        log.warning("Could not retrieve sales — skipping this check.")
        return
    if sales_cents == 0:
        log.info("No sales yet today — skipping.")
        return

    labor_pct     = labor_cents / sales_cents
    labor_dollars = labor_cents / 100
    sales_dollars = sales_cents / 100

    log.info(
        "Labor: $%.2f | Sales: $%.2f | Labor%%: %.1f%% | Staff clocked in: %d",
        labor_dollars, sales_dollars, labor_pct * 100, active_count,
    )

    if labor_pct >= LABOR_THRESHOLD:
        if not state["in_breach"]:
            state["in_breach"]    = True
            state["breach_start"] = now
            log.info("Breach started at %s.", now.strftime("%H:%M"))

        breach_hours = (now - state["breach_start"]).total_seconds() / 3600

        # Determine next alert stage
        target_stage = None
        if state["alert_stage"] == 0:
            target_stage = 1
        elif state["alert_stage"] == 1 and breach_hours >= ESCALATION_HOURS:
            target_stage = 2
        elif state["alert_stage"] == 2 and breach_hours >= ESCALATION_HOURS * 2:
            target_stage = 3

        if target_stage:
            messages = build_alerts(labor_pct, labor_cents, sales_cents, shift_details, target_stage)
            for msg in messages:
                send_sms(msg)
            state["alert_stage"] = target_stage
            log.info("Stage %d alerts sent (%.1fh into breach).", target_stage, breach_hours)
        else:
            log.info(
                "In breach — stage %d, breach %.1fh, next escalation at %.0fh.",
                state["alert_stage"],
                breach_hours,
                ESCALATION_HOURS * state["alert_stage"],
            )

    else:
        if state["in_breach"]:
            log.info("Labor back under threshold — resetting breach state.")
            reset_breach_state()
        else:
            log.info("Labor within target — all good.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 60)
    log.info("La Flor Blanca Labor Monitor starting up (Enhanced)")
    log.info("Timezone     : %s", TZ_NAME)
    log.info("Threshold    : %.0f%%", LABOR_THRESHOLD * 100)
    log.info("Escalation   : every %.0f hours while in breach (max 3 bursts)", ESCALATION_HOURS)
    log.info("Alert phones : %s", ", ".join(ALERT_PHONES))
    log.info("=" * 60)

    scheduler = BlockingScheduler(timezone=TZ)

    # Labor check every 30 minutes, runs immediately on startup
    scheduler.add_job(
        check_labor,
        trigger="interval",
        minutes=30,
        next_run_time=datetime.now(TZ),
        id="labor_check",
    )

    # Weekly insight every Monday at 8:30 AM
    scheduler.add_job(
        weekly_insight,
        trigger="cron",
        day_of_week="mon",
        hour=8,
        minute=30,
        id="weekly_insight",
    )

    log.info("Scheduler started. Labor check every 30 min. Weekly insight every Monday 8:30 AM.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
