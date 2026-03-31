"""
LaborMonitor — orchestrates scheduled labor checks and weekly insights.
"""

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from alert_builder import AlertBuilder
from claude_advisor import ClaudeAdvisor
from config import Config
from daily_briefing import DailyBriefing
from notifier import Notifier
from square_client import SquareDataClient

log = logging.getLogger(__name__)


class LaborMonitor:
    """
    Runs two scheduled jobs:
      - check_labor: every 30 min during business hours
      - weekly_insight: every Monday at 8:30 AM
    """

    def __init__(
        self,
        config: Config,
        square: SquareDataClient,
        notifier: Notifier,
        builder: AlertBuilder,
        advisor: ClaudeAdvisor | None = None,
        briefing: DailyBriefing | None = None,
    ) -> None:
        self._config   = config
        self._square   = square
        self._notifier = notifier
        self._builder  = builder
        self._advisor  = advisor
        self._briefing = briefing

        # Breach state
        self._in_breach:    bool          = False
        self._breach_start: datetime|None = None
        self._alert_stage:  int           = 0

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _is_business_hours(self) -> bool:
        now = datetime.now(self._config.tz)
        day = now.weekday()
        if day not in self._config.BUSINESS_HOURS:
            return False
        oh, om = map(int, self._config.BUSINESS_HOURS[day][0].split(":"))
        ch, cm = map(int, self._config.BUSINESS_HOURS[day][1].split(":"))
        open_dt  = now.replace(hour=oh, minute=om, second=0, microsecond=0)
        close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
        return open_dt <= now <= close_dt

    def _reset_breach(self) -> None:
        self._in_breach    = False
        self._breach_start = None
        self._alert_stage  = 0

    # ── Scheduled jobs ────────────────────────────────────────────────────────

    def check_labor(self) -> None:
        now = datetime.now(self._config.tz)

        if not self._is_business_hours():
            log.info("Outside business hours (%s) — skipping.", now.strftime("%a %H:%M %Z"))
            if self._in_breach:
                log.info("Business day ended — resetting breach state.")
                self._reset_breach()
            return

        log.info("--- Check at %s ---", now.strftime("%Y-%m-%d %H:%M %Z"))

        active_count, labor_cents, shift_details = self._square.get_labor_data()

        if active_count == 0:
            log.info("No employees clocked in — skipping alert logic.")
            if self._in_breach:
                self._reset_breach()
            return

        sales_cents = self._square.get_sales_cents()
        if sales_cents is None:
            log.warning("Could not retrieve sales — skipping this check.")
            return
        if sales_cents == 0:
            log.info("No sales yet today — skipping.")
            return

        labor_pct = labor_cents / sales_cents
        log.info(
            "Labor: $%.2f | Sales: $%.2f | Labor%%: %.1f%% | Staff clocked in: %d",
            labor_cents / 100, sales_cents / 100, labor_pct * 100, active_count,
        )

        if labor_pct >= self._config.labor_threshold:
            if not self._in_breach:
                self._in_breach    = True
                self._breach_start = now
                log.info("Breach started at %s.", now.strftime("%H:%M"))

            breach_hours = (now - self._breach_start).total_seconds() / 3600
            target_stage = self._next_stage(breach_hours)

            if target_stage:
                item_sales = self._square.get_item_sales()
                hist_pace  = self._square.get_historical_pace()

                claude_section = ""
                if self._advisor:
                    log.info("Requesting Claude AI advice...")
                    try:
                        hourly_history = self._square.get_hourly_sales_history()
                    except Exception:
                        log.warning("Hourly history timed out — proceeding without trajectory data.")
                        hourly_history = {}
                    claude_section = self._advisor.get_labor_advice(
                        labor_pct, labor_cents, sales_cents,
                        shift_details, item_sales, hist_pace, target_stage,
                        hourly_history=hourly_history,
                    )

                body = self._builder.build_labor_alert(
                    labor_pct, labor_cents, sales_cents,
                    shift_details, item_sales, hist_pace, target_stage,
                    claude_section=claude_section,
                )
                subject = f"La Flor Blanca — Labor Alert (Stage {target_stage})"
                self._notifier.send_alert(subject, body)
                self._alert_stage = target_stage
                log.info("Stage %d alert sent (%.1fh into breach).", target_stage, breach_hours)
            else:
                log.info(
                    "In breach — stage %d, breach %.1fh, next escalation at %.0fh.",
                    self._alert_stage, breach_hours,
                    self._config.escalation_hours * self._alert_stage,
                )
        else:
            if self._in_breach:
                log.info("Labor back under threshold — resetting breach state.")
                self._reset_breach()
            else:
                log.info("Labor within target — all good.")

    def _next_stage(self, breach_hours: float) -> int | None:
        esc = self._config.escalation_hours
        if self._alert_stage == 0:
            return 1
        if self._alert_stage == 1 and breach_hours >= esc:
            return 2
        if self._alert_stage == 2 and breach_hours >= esc * 2:
            return 3
        return None

    def morning_briefing(self) -> None:
        if self._briefing:
            self._briefing.send()
        else:
            log.info("Morning briefing skipped — ANTHROPIC_API_KEY not set.")

    def weekly_insight(self) -> None:
        log.info("Running weekly insight...")
        daily_sales, daily_labor = self._square.get_weekly_history(weeks=8)

        if not daily_sales:
            log.info("Weekly insight: not enough historical data yet.")
            return

        message = self._builder.build_weekly_insight(daily_sales, daily_labor)
        if not message:
            log.info("Weekly insight: not enough data to generate insight.")
            return

        self._notifier.send_alert(
            subject="La Flor Blanca — Weekly Labor Insight",
            body=message,
        )
        log.info("Weekly insight sent.")

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        cfg = self._config
        log.info("=" * 60)
        log.info("La Flor Blanca Labor Monitor starting up")
        log.info("Timezone     : %s", cfg.tz_name)
        log.info("Threshold    : %.0f%%", cfg.labor_threshold * 100)
        log.info("Escalation   : every %.0f hours while in breach (max 3 bursts)", cfg.escalation_hours)
        log.info("Alert emails : %s", ", ".join(cfg.alert_emails))
        log.info("Claude AI    : %s", "enabled" if cfg.claude_enabled else "disabled (set ANTHROPIC_API_KEY to enable)")
        log.info("=" * 60)

        scheduler = BlockingScheduler(timezone=cfg.tz)
        scheduler.add_job(
            self.check_labor,
            trigger="interval",
            minutes=30,
            next_run_time=datetime.now(cfg.tz),
            id="labor_check",
        )
        scheduler.add_job(
            self.morning_briefing,
            trigger="cron",
            day_of_week="tue,wed,thu,fri,sat,sun",
            hour=9,
            minute=0,
            id="morning_briefing",
        )
        scheduler.add_job(
            self.weekly_insight,
            trigger="cron",
            day_of_week="mon",
            hour=8,
            minute=30,
            id="weekly_insight",
        )
        log.info("Scheduler started. Labor check every 30 min. Morning briefing 9 AM. Weekly insight Monday 8:30 AM.")

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Monitor stopped.")
