"""
La Flor Blanca — Square Labor Cost Alert System
Checks every 30 minutes during business hours and sends Twilio SMS alerts
when labor cost exceeds the configured threshold.
"""

import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


# ── Config (all from environment variables) ───────────────────────────────────
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

# ── Business hours ─────────────────────────────────────────────────────────────
# Python weekday(): 0=Mon  1=Tue  2=Wed  3=Thu  4=Fri  5=Sat  6=Sun
# Format: "HH:MM" (24-hour)
BUSINESS_HOURS: dict[int, tuple[str, str]] = {
    0: ("09:30", "19:00"),   # Monday
    # 1 Tuesday → closed (not in dict)
    2: ("09:30", "19:00"),   # Wednesday
    3: ("09:30", "19:00"),   # Thursday
    4: ("09:30", "19:30"),   # Friday
    5: ("09:30", "19:30"),   # Saturday
    6: ("09:30", "19:30"),   # Sunday
}

# ── API clients ───────────────────────────────────────────────────────────────
square = SquareClient(token=SQUARE_TOKEN)
twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)

# ── Breach state (in-memory; resets on restart, which is fine) ────────────────
state: dict = {
    "in_breach":             False,
    "breach_start":          None,   # datetime (tz-aware)
    "initial_alert_sent":    False,
    "escalation_alert_sent": False,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_business_hours() -> bool:
    now = datetime.now(TZ)
    day = now.weekday()
    if day not in BUSINESS_HOURS:
        return False
    open_str, close_str = BUSINESS_HOURS[day]
    oh, om = map(int, open_str.split(":"))
    ch, cm = map(int, close_str.split(":"))
    open_dt  = now.replace(hour=oh, minute=om,  second=0, microsecond=0)
    close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return open_dt <= now <= close_dt


def today_range_utc() -> tuple[str, str]:
    """Start and end of today (local) expressed as UTC ISO-8601 strings."""
    now = datetime.now(TZ)
    start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        start.astimezone(timezone.utc).strftime(fmt),
        end.astimezone(timezone.utc).strftime(fmt),
    )


def reset_breach_state() -> None:
    state.update(
        in_breach=False,
        breach_start=None,
        initial_alert_sent=False,
        escalation_alert_sent=False,
    )


def send_sms(message: str) -> None:
    for phone in ALERT_PHONES:
        try:
            twilio.messages.create(body=message, from_=TWILIO_FROM, to=phone)
            log.info("SMS sent to %s", phone)
        except Exception as exc:
            log.error("Failed to send SMS to %s: %s", phone, exc)


# ── Square data fetchers ──────────────────────────────────────────────────────

def get_labor_data() -> tuple[int, float]:
    """
    Returns (active_employee_count, total_labor_cost_cents).
    Counts all shifts that STARTED today; uses current time for open shifts.
    Returns (0, 0.0) if the API call fails so the caller can decide what to do.
    """
    begin, end = today_range_utc()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    body = {
        "query": {
            "filter": {
                "location_ids": [SQUARE_LOCATION],
                "start": {
                    "start_at": begin,
                    "end_at":   end,
                },
            }
        }
    }

    active_count      = 0
    total_labor_cents = 0.0
    cursor            = None

    while True:
        if cursor:
            body["cursor"] = cursor

        result = square.labor.search_shifts(body)

        if not result.is_success():
            log.error("Square Labor API error: %s", result.errors)
            return 0, 0.0

        for shift in result.body.get("shifts", []):
            status     = shift.get("status", "")
            start_at   = shift.get("start_at", now_str)
            end_at     = shift.get("end_at") or now_str   # open shifts have no end_at

            if status == "OPEN":
                active_count += 1
                end_at = now_str   # bill up to right now

            # Hourly rate stored in cents-per-hour in Square's API
            hourly_cents = (
                shift.get("wage", {})
                     .get("hourly_rate", {})
                     .get("amount", 0)
            )
            if hourly_cents == 0:
                log.warning(
                    "Shift %s has no hourly wage configured in Square — "
                    "labor cost will be understated. Set hourly wages in "
                    "Square Dashboard → Team → [Employee] → Compensation.",
                    shift.get("id", "?"),
                )

            start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
            hours    = max(0.0, (end_dt - start_dt).total_seconds() / 3600)
            total_labor_cents += hourly_cents * hours

        cursor = result.body.get("cursor")
        if not cursor:
            break

    return active_count, total_labor_cents


def get_sales_cents() -> float | None:
    """
    Returns total sales (excluding tips) in cents for today.
    Returns None on API error.
    """
    begin, end = today_range_utc()
    total  = 0.0
    cursor = None

    while True:
        kwargs: dict = dict(
            begin_time=begin,
            end_time=end,
            location_id=SQUARE_LOCATION,
            sort_order="ASC",
        )
        if cursor:
            kwargs["cursor"] = cursor

        result = square.payments.list_payments(**kwargs)

        if not result.is_success():
            log.error("Square Payments API error: %s", result.errors)
            return None

        for payment in result.body.get("payments", []):
            # total_money − tip_money = sales revenue (food + bev + tax, excl. tip)
            total_m = payment.get("total_money", {}).get("amount", 0)
            tip_m   = payment.get("tip_money",   {}).get("amount", 0)
            total += total_m - tip_m

        cursor = result.body.get("cursor")
        if not cursor:
            break

    return total


# ── Main check ────────────────────────────────────────────────────────────────

def check_labor() -> None:
    now = datetime.now(TZ)

    if not is_business_hours():
        log.info("Outside business hours (%s) — skipping.", now.strftime("%a %H:%M %Z"))
        if state["in_breach"]:
            log.info("Business day ended — resetting breach state.")
            reset_breach_state()
        return

    log.info("--- Check at %s ---", now.strftime("%Y-%m-%d %H:%M %Z"))

    active_count, labor_cents = get_labor_data()

    if active_count == 0:
        log.info("No employees clocked in — no alert needed.")
        if state["in_breach"]:
            log.info("No staff clocked in — resetting breach state.")
            reset_breach_state()
        return

    sales_cents = get_sales_cents()
    if sales_cents is None:
        log.warning("Could not retrieve sales — skipping this check.")
        return

    if sales_cents == 0:
        log.info("No sales recorded yet today — skipping alert calculation.")
        return

    labor_pct     = labor_cents / sales_cents
    labor_dollars = labor_cents / 100
    sales_dollars = sales_cents / 100

    log.info(
        "Labor: $%.2f | Sales: $%.2f | Labor%%: %.1f%% | Staff clocked in: %d",
        labor_dollars, sales_dollars, labor_pct * 100, active_count,
    )

    # ── Over threshold ────────────────────────────────────────────────────────
    if labor_pct >= LABOR_THRESHOLD:

        if not state["in_breach"]:
            state["in_breach"]    = True
            state["breach_start"] = now
            log.info("Breach started at %s.", now.strftime("%H:%M"))

        # Send first alert once per breach
        if not state["initial_alert_sent"]:
            msg = (
                f"LA FLOR BLANCA - LABOR ALERT\n"
                f"Labor is at {labor_pct*100:.1f}% — over the {LABOR_THRESHOLD*100:.0f}% target.\n"
                f"Labor cost: ${labor_dollars:,.2f}\n"
                f"Sales today: ${sales_dollars:,.2f}\n"
                f"Staff clocked in: {active_count}\n"
                f"Time: {now.strftime('%I:%M %p')}"
            )
            send_sms(msg)
            state["initial_alert_sent"] = True
            log.info("Initial breach alert sent.")

        # Send escalation alert after 2+ hours over threshold
        if state["breach_start"] is not None and not state["escalation_alert_sent"]:
            breach_hours = (now - state["breach_start"]).total_seconds() / 3600
            if breach_hours >= ESCALATION_HOURS:
                msg = (
                    f"LA FLOR BLANCA - URGENT LABOR WARNING\n"
                    f"Labor has been over {LABOR_THRESHOLD*100:.0f}% for "
                    f"{breach_hours:.1f} hours!\n"
                    f"Currently at {labor_pct*100:.1f}% (${labor_dollars:,.2f})\n"
                    f"Sales today: ${sales_dollars:,.2f}\n"
                    f"Consider sending staff home early to reduce cost."
                )
                send_sms(msg)
                state["escalation_alert_sent"] = True
                log.info("Escalation alert sent (breach lasted %.1fh).", breach_hours)

    # ── Back under threshold ──────────────────────────────────────────────────
    else:
        if state["in_breach"]:
            log.info("Labor back under threshold — resetting breach state.")
            reset_breach_state()
        else:
            log.info("Labor within target — all good.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("La Flor Blanca Labor Monitor starting up")
    log.info("Timezone     : %s", TZ_NAME)
    log.info("Threshold    : %.0f%%", LABOR_THRESHOLD * 100)
    log.info("Escalation   : after %.0f hours over threshold", ESCALATION_HOURS)
    log.info("Alert phones : %s", ", ".join(ALERT_PHONES))
    log.info("=" * 60)

    scheduler = BlockingScheduler(timezone=TZ)

    # Run immediately on startup, then every 30 minutes
    scheduler.add_job(
        check_labor,
        trigger="interval",
        minutes=30,
        next_run_time=datetime.now(TZ),
        id="labor_check",
    )

    log.info("Scheduler started. Checking every 30 minutes during business hours.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
