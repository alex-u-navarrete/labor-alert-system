"""
La Flor Blanca — Operations Dashboard
FastAPI web app that exposes live business data to the portal.
Runs alongside the scheduler in the same process via BackgroundScheduler.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from config import Config
from scheduler import LaborMonitor
from square_client import SquareDataClient

log = logging.getLogger(__name__)

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


class DashboardApp:
    """
    Wraps the FastAPI application. Shares SquareDataClient and LaborMonitor
    with the scheduler so we never make duplicate API calls.
    """

    def __init__(
        self,
        config: Config,
        square: SquareDataClient,
        monitor: LaborMonitor,
    ) -> None:
        self._config  = config
        self._square  = square
        self._monitor = monitor
        self._tpl     = Jinja2Templates(directory=_TEMPLATES_DIR)
        self.app      = FastAPI(title="La Flor Blanca — Command Center", docs_url=None, redoc_url=None)
        self._register_routes()

    # ── Auth dependency ───────────────────────────────────────────────────────

    def _require_token(self, token: str = Query(default="")) -> None:
        """FastAPI dependency — rejects requests if DASHBOARD_TOKEN is set and doesn't match."""
        configured = self._config.dashboard_token
        if configured and token != configured:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")

    # ── Routes ────────────────────────────────────────────────────────────────

    def _register_routes(self) -> None:
        app = self.app

        @app.get("/health", include_in_schema=False)
        def health() -> JSONResponse:
            """Railway health check — no auth required."""
            return JSONResponse({"status": "ok"})

        @app.get("/", response_class=HTMLResponse)
        def index(request: Request, _: None = Depends(self._require_token)) -> HTMLResponse:
            token = request.query_params.get("token", "")
            return self._tpl.TemplateResponse(
                "index.html",
                {"request": request, "token": token, "threshold_pct": int(self._config.labor_threshold * 100)},
            )

        @app.get("/api/live")
        def api_live(_: None = Depends(self._require_token)) -> JSONResponse:
            """Current labor %, sales, staff on clock, breach status."""
            try:
                active_count, labor_cents, shift_details = self._square.get_labor_data()
                sales_cents  = self._square.get_sales_cents() or 0.0
                hist_cents   = self._square.get_historical_pace()
                breach       = self._monitor.breach_state

                labor_pct = (labor_cents / sales_cents) if sales_cents > 0 else 0.0

                breach_minutes = None
                if breach["in_breach"] and breach["breach_start"]:
                    delta = datetime.now(self._config.tz) - breach["breach_start"]
                    breach_minutes = int(delta.total_seconds() / 60)

                staff = [
                    {
                        "name":  s.get("name", "Staff"),
                        "hours": round(s.get("hours", 0), 2),
                        "cost":  round(s.get("cost_cents", 0) / 100, 2),
                    }
                    for s in shift_details
                ]

                return JSONResponse({
                    "labor_pct":        round(labor_pct * 100, 1),
                    "labor_dollars":    round(labor_cents / 100, 2),
                    "sales_dollars":    round(sales_cents / 100, 2),
                    "threshold_pct":    int(self._config.labor_threshold * 100),
                    "active_staff":     active_count,
                    "staff":            staff,
                    "in_breach":        breach["in_breach"],
                    "breach_minutes":   breach_minutes,
                    "alert_stage":      breach["alert_stage"],
                    "hist_pace_dollars": round(hist_cents / 100, 2) if hist_cents else None,
                    "timestamp":        datetime.now(self._config.tz).isoformat(),
                })
            except Exception as exc:
                log.exception("Error building /api/live response")
                raise HTTPException(status_code=500, detail=str(exc))

        @app.get("/api/weekly")
        def api_weekly(_: None = Depends(self._require_token)) -> JSONResponse:
            """8-week daily sales + labor % for the trend chart."""
            try:
                daily_sales, daily_labor = self._square.get_weekly_history(weeks=8)

                labels, labor_pcts, sales_vals = [], [], []
                for date_str in sorted(daily_sales.keys()):
                    s = daily_sales.get(date_str, 0)
                    l = daily_labor.get(date_str, 0)
                    pct = round((l / s) * 100, 1) if s > 0 else 0.0
                    dt  = datetime.strptime(date_str, "%Y-%m-%d")
                    labels.append(dt.strftime("%a %b %-d"))
                    labor_pcts.append(pct)
                    sales_vals.append(round(s / 100, 2))

                return JSONResponse({
                    "labels":     labels,
                    "labor_pct":  labor_pcts,
                    "sales":      sales_vals,
                    "threshold":  int(self._config.labor_threshold * 100),
                })
            except Exception as exc:
                log.exception("Error building /api/weekly response")
                raise HTTPException(status_code=500, detail=str(exc))

        @app.get("/api/items")
        def api_items(_: None = Depends(self._require_token)) -> JSONResponse:
            """Today's item sales sorted by quantity descending."""
            try:
                items = self._square.get_item_sales()
                sorted_items = sorted(items.items(), key=lambda x: x[1], reverse=True)
                return JSONResponse({
                    "items": [{"name": k, "qty": v} for k, v in sorted_items]
                })
            except Exception as exc:
                log.exception("Error building /api/items response")
                raise HTTPException(status_code=500, detail=str(exc))
