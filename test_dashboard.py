"""
Tests for dashboard.py — covers auth, all API endpoints, and edge cases.
Uses FastAPI's TestClient (backed by httpx) with fully mocked Square + scheduler state.
"""

import sys
import types
from unittest.mock import MagicMock, PropertyMock

# ── Stub out squareup before any project module imports it ─────────────────────
# The squareup version in this environment has a different internal structure.
# Since all tests use a mocked SquareDataClient, we never need real Square API calls.
_square_mod = types.ModuleType("square")
_square_client_mod = types.ModuleType("square.client")
_square_client_mod.Client = MagicMock
sys.modules.setdefault("square", _square_mod)
sys.modules.setdefault("square.client", _square_client_mod)
# ───────────────────────────────────────────────────────────────────────────────

import pytest
from datetime import datetime
from fastapi.testclient import TestClient

import pytz

# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_config(token="secret"):
    cfg = MagicMock()
    cfg.dashboard_token   = token
    cfg.labor_threshold   = 0.33
    cfg.tz                = pytz.timezone("America/Los_Angeles")
    cfg.tz_name           = "America/Los_Angeles"
    return cfg


def make_square(
    labor_data=None,
    sales_cents=50000.0,
    hist_pace=60000.0,
    item_sales=None,
    weekly_history=None,
):
    sq = MagicMock()
    sq.get_labor_data.return_value = labor_data or (
        2,
        16500.0,
        [
            {"name": "Maria", "hours": 3.0, "cost_cents": 9000.0, "status": "OPEN"},
            {"name": "Carlos", "hours": 2.5, "cost_cents": 7500.0, "status": "OPEN"},
        ],
    )
    sq.get_sales_cents.return_value = sales_cents
    sq.get_historical_pace.return_value = hist_pace
    sq.get_item_sales.return_value = item_sales if item_sales is not None else {"Pupusas": 24, "Coffee": 15, "Tamales": 8}
    sq.get_weekly_history.return_value = weekly_history or (
        {"2026-03-24": 80000.0, "2026-03-25": 65000.0, "2026-03-30": 90000.0},
        {"2026-03-24": 24000.0, "2026-03-25": 26000.0, "2026-03-30": 27000.0},
    )
    return sq


def make_monitor(in_breach=False, breach_start=None, alert_stage=0):
    mon = MagicMock()
    type(mon).breach_state = PropertyMock(return_value={
        "in_breach":    in_breach,
        "breach_start": breach_start,
        "alert_stage":  alert_stage,
    })
    return mon


def make_client(token="secret", square=None, monitor=None):
    """Build a TestClient with the full DashboardApp."""
    from dashboard import DashboardApp
    cfg = make_config(token)
    sq  = square or make_square()
    mon = monitor or make_monitor()
    app = DashboardApp(cfg, sq, mon)
    return TestClient(app.app, raise_server_exceptions=False)


# ── /health ────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_200_always(self):
        client = make_client()
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_no_token_required(self):
        client = make_client(token="secret")
        # No token at all — health must still pass
        r = client.get("/health")
        assert r.status_code == 200


# ── Auth ───────────────────────────────────────────────────────────────────────

class TestAuth:
    def test_root_no_token_returns_401(self):
        client = make_client(token="secret")
        r = client.get("/")
        assert r.status_code == 401

    def test_root_wrong_token_returns_401(self):
        client = make_client(token="secret")
        r = client.get("/?token=wrong")
        assert r.status_code == 401

    def test_root_correct_token_returns_200(self):
        client = make_client(token="secret")
        r = client.get("/?token=secret")
        assert r.status_code == 200

    def test_api_live_no_token_returns_401(self):
        client = make_client(token="secret")
        r = client.get("/api/live")
        assert r.status_code == 401

    def test_api_weekly_no_token_returns_401(self):
        client = make_client(token="secret")
        r = client.get("/api/weekly")
        assert r.status_code == 401

    def test_api_items_no_token_returns_401(self):
        client = make_client(token="secret")
        r = client.get("/api/items")
        assert r.status_code == 401

    def test_no_token_configured_allows_all(self):
        """If DASHBOARD_TOKEN is empty string, dashboard is open (dev mode)."""
        client = make_client(token="")
        r = client.get("/api/live")
        assert r.status_code == 200


# ── /api/live ─────────────────────────────────────────────────────────────────

class TestApiLive:
    def _get(self, **kwargs):
        client = make_client(**kwargs)
        return client.get("/api/live?token=secret")

    def test_normal_response_shape(self):
        r = self._get()
        assert r.status_code == 200
        d = r.json()
        assert "labor_pct" in d
        assert "sales_dollars" in d
        assert "threshold_pct" in d
        assert "active_staff" in d
        assert "staff" in d
        assert "in_breach" in d
        assert "breach_minutes" in d
        assert "alert_stage" in d
        assert "hist_pace_dollars" in d
        assert "timestamp" in d

    def test_labor_pct_calculation(self):
        # labor=16500 / sales=50000 = 33%
        sq = make_square(sales_cents=50000.0)
        sq.get_labor_data.return_value = (2, 16500.0, [])
        r = make_client(square=sq).get("/api/live?token=secret")
        assert r.json()["labor_pct"] == 33.0

    def test_zero_sales_returns_zero_labor_pct(self):
        sq = make_square(sales_cents=0.0)
        r = make_client(square=sq).get("/api/live?token=secret")
        d = r.json()
        assert d["labor_pct"] == 0.0
        assert d["sales_dollars"] == 0.0

    def test_none_sales_treated_as_zero(self):
        """get_sales_cents() returns None on API error — must not crash."""
        sq = make_square(sales_cents=None)
        sq.get_sales_cents.return_value = None
        r = make_client(square=sq).get("/api/live?token=secret")
        assert r.status_code == 200
        assert r.json()["sales_dollars"] == 0.0

    def test_no_hist_pace_returns_null(self):
        sq = make_square(hist_pace=None)
        sq.get_historical_pace.return_value = None
        r = make_client(square=sq).get("/api/live?token=secret")
        assert r.json()["hist_pace_dollars"] is None

    def test_breach_active_includes_minutes(self):
        tz = pytz.timezone("America/Los_Angeles")
        breach_start = datetime.now(tz).replace(microsecond=0)
        # Simulate 47-minute-old breach
        from datetime import timedelta
        breach_start = breach_start - timedelta(minutes=47)
        mon = make_monitor(in_breach=True, breach_start=breach_start, alert_stage=1)
        r = make_client(monitor=mon).get("/api/live?token=secret")
        d = r.json()
        assert d["in_breach"] is True
        assert d["alert_stage"] == 1
        assert d["breach_minutes"] is not None
        assert 45 <= d["breach_minutes"] <= 50  # allow a few seconds of test runtime

    def test_breach_false_minutes_is_null(self):
        mon = make_monitor(in_breach=False)
        r = make_client(monitor=mon).get("/api/live?token=secret")
        d = r.json()
        assert d["in_breach"] is False
        assert d["breach_minutes"] is None

    def test_breach_true_but_start_none_does_not_crash(self):
        """Race condition: in_breach=True but breach_start already reset to None."""
        mon = make_monitor(in_breach=True, breach_start=None, alert_stage=1)
        r = make_client(monitor=mon).get("/api/live?token=secret")
        assert r.status_code == 200
        assert r.json()["breach_minutes"] is None

    def test_staff_list_shape(self):
        r = self._get()
        staff = r.json()["staff"]
        assert len(staff) == 2
        assert staff[0]["name"] == "Maria"
        assert staff[0]["hours"] == 3.0
        assert staff[0]["cost"] == 90.0

    def test_empty_staff(self):
        sq = make_square(labor_data=(0, 0.0, []))
        r = make_client(square=sq).get("/api/live?token=secret")
        assert r.json()["staff"] == []
        assert r.json()["active_staff"] == 0

    def test_square_exception_returns_500(self):
        sq = make_square()
        sq.get_labor_data.side_effect = RuntimeError("Square timeout")
        r = make_client(square=sq).get("/api/live?token=secret")
        assert r.status_code == 500


# ── /api/weekly ───────────────────────────────────────────────────────────────

class TestApiWeekly:
    def _get(self, square=None):
        client = make_client(square=square)
        return client.get("/api/weekly?token=secret")

    def test_normal_response_shape(self):
        r = self._get()
        assert r.status_code == 200
        d = r.json()
        assert "labels" in d
        assert "labor_pct" in d
        assert "sales" in d
        assert "threshold" in d

    def test_labels_sorted_chronologically(self):
        sq = make_square(weekly_history=(
            {"2026-03-25": 65000.0, "2026-03-24": 80000.0},
            {"2026-03-25": 26000.0, "2026-03-24": 24000.0},
        ))
        r = self._get(square=sq)
        d = r.json()
        assert len(d["labels"]) == 2
        assert d["labels"][0].startswith("Tue")  # Mar 24, 2026 is Tuesday
        assert d["labels"][1].startswith("Wed")  # Mar 25, 2026 is Wednesday

    def test_labor_pct_correct(self):
        sq = make_square(weekly_history=(
            {"2026-03-24": 100000.0},  # $1000 sales
            {"2026-03-24": 33000.0},   # $330 labor = 33%
        ))
        r = self._get(square=sq)
        assert r.json()["labor_pct"] == [33.0]

    def test_zero_sales_day_shows_zero_pct(self):
        sq = make_square(weekly_history=(
            {"2026-03-24": 0.0},
            {"2026-03-24": 15000.0},
        ))
        r = self._get(square=sq)
        assert r.json()["labor_pct"] == [0.0]

    def test_empty_history_returns_empty_lists(self):
        sq = make_square(weekly_history=({}, {}))
        r = self._get(square=sq)
        d = r.json()
        assert d["labels"] == []
        assert d["labor_pct"] == []
        assert d["sales"] == []

    def test_malformed_date_key_is_skipped(self):
        """Empty string key from Square API must not crash the endpoint."""
        sq = make_square(weekly_history=(
            {"": 50000.0, "2026-03-24": 80000.0},
            {"": 15000.0, "2026-03-24": 24000.0},
        ))
        r = self._get(square=sq)
        assert r.status_code == 200
        d = r.json()
        # Only the valid date should appear
        assert len(d["labels"]) == 1

    def test_threshold_in_response(self):
        r = self._get()
        assert r.json()["threshold"] == 33


# ── /api/items ────────────────────────────────────────────────────────────────

class TestApiItems:
    def _get(self, square=None):
        client = make_client(square=square)
        return client.get("/api/items?token=secret")

    def test_sorted_by_quantity_descending(self):
        sq = make_square(item_sales={"Coffee": 5, "Pupusas": 24, "Tamales": 8})
        r = self._get(square=sq)
        items = r.json()["items"]
        assert items[0]["name"] == "Pupusas"
        assert items[0]["qty"] == 24
        assert items[1]["name"] == "Tamales"
        assert items[2]["name"] == "Coffee"

    def test_empty_sales_returns_empty_list(self):
        sq = make_square(item_sales={})
        r = self._get(square=sq)
        assert r.json()["items"] == []

    def test_response_shape(self):
        r = self._get()
        items = r.json()["items"]
        assert all("name" in i and "qty" in i for i in items)

    def test_square_exception_returns_500(self):
        sq = make_square()
        sq.get_item_sales.side_effect = RuntimeError("API error")
        r = self._get(square=sq)
        assert r.status_code == 500


# ── Scheduler breach_state ────────────────────────────────────────────────────

class TestBreachState:
    def test_initial_state(self):
        from scheduler import LaborMonitor
        mon = LaborMonitor.__new__(LaborMonitor)
        mon._in_breach    = False
        mon._breach_start = None
        mon._alert_stage  = 0
        state = mon.breach_state
        assert state == {"in_breach": False, "breach_start": None, "alert_stage": 0}

    def test_breach_active(self):
        from scheduler import LaborMonitor
        tz    = pytz.timezone("America/Los_Angeles")
        start = datetime.now(tz)
        mon   = LaborMonitor.__new__(LaborMonitor)
        mon._in_breach    = True
        mon._breach_start = start
        mon._alert_stage  = 2
        state = mon.breach_state
        assert state["in_breach"] is True
        assert state["breach_start"] == start
        assert state["alert_stage"] == 2
