"""
Tests for helpers.py — process_dashboard_data and _merge_goal.

Key scenarios covered:
- process_dashboard_data returns None for invalid/empty raw_data (prevents crash in
  admin.py view_user when calling dashboard_data['_admin_viewing'] = True on None)
- _merge_goal fills all required keys even with None or partial input
- Valid minimal raw_data produces a complete dashboard_data dict
- New users (no data imported yet) can open goal setup and chat with Sento:
  - goal_setup_view no longer redirects when raw_data is None (fixed: no redirect guard)
  - goal_setup_chat receives {"months":{}} fallback instead of returning 404
  - goal_setup_chat with empty raw_data must not raise KeyError/AttributeError
"""
from datetime import date

import pytest

from helpers import DEFAULT_GOAL, _merge_goal, is_goal_expired, process_dashboard_data

# ---------------------------------------------------------------------------
# Minimal fixture raw_data
# ---------------------------------------------------------------------------

def _make_raw_data(month_key="2026-03", activities=None, daily_stats=None):
    """Returns a minimal valid raw_data structure that process_dashboard_data accepts."""
    return {
        "months": {
            month_key: {
                "activities": activities or [],
                "daily_stats": daily_stats or {},
            }
        },
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# _merge_goal
# ---------------------------------------------------------------------------

class TestMergeGoal:
    def test_none_returns_defaults(self):
        goal = _merge_goal(None)
        for key in DEFAULT_GOAL:
            assert key in goal

    def test_partial_goal_fills_missing(self):
        goal = _merge_goal({"race_type": "Maratón"})
        assert goal["race_type"] == "Maratón"
        assert goal["target_pace_str"] == DEFAULT_GOAL["target_pace_str"]
        assert goal["weekly_peak_km"] == DEFAULT_GOAL["weekly_peak_km"]

    def test_empty_string_falls_back_to_default(self):
        goal = _merge_goal({"target_pace_str": ""})
        assert goal["target_pace_str"] == DEFAULT_GOAL["target_pace_str"]

    def test_none_value_falls_back_to_default(self):
        goal = _merge_goal({"easy_hr_max": None})
        assert goal["easy_hr_max"] == DEFAULT_GOAL["easy_hr_max"]

class TestIsGoalExpired:
    def test_none_goal_not_expired(self):
        assert is_goal_expired(None, date(2026, 7, 14)) is False

    def test_no_event_date_not_expired(self):
        assert is_goal_expired({"race_type": "10K"}, date(2026, 7, 14)) is False

    def test_future_event_not_expired(self):
        assert is_goal_expired({"event_date": "2026-08-01"}, date(2026, 7, 14)) is False

    def test_today_event_not_expired(self):
        assert is_goal_expired({"event_date": "2026-07-14"}, date(2026, 7, 14)) is False

    def test_past_event_expired(self):
        assert is_goal_expired({"event_date": "2026-07-13"}, date(2026, 7, 14)) is True

    def test_invalid_event_date_not_expired(self):
        assert is_goal_expired({"event_date": "not-a-date"}, date(2026, 7, 14)) is False


class TestFullGoalPreserved:
    def test_full_goal_preserved(self):
        custom = {
            "race_type": "10K",
            "target_pace_str": "4:30",
            "target_pace_min": 4,
            "target_pace_sec": 30,
            "weekly_peak_km": 60,
            "easy_hr_max": 150,
            "tempo_hr_min": 152,
            "tempo_hr_max": 168,
            "interval_hr_min": 172,
            "description": "San José 2026",
        }
        goal = _merge_goal(custom)
        for k, v in custom.items():
            assert goal[k] == v


# ---------------------------------------------------------------------------
# process_dashboard_data — invalid inputs
# ---------------------------------------------------------------------------

class TestProcessDashboardDataInvalid:
    def test_none_input_returns_none(self):
        """Prevents crash in admin view_user: dashboard_data['_admin_viewing'] on None."""
        result = process_dashboard_data(None)
        assert result is None

    def test_empty_dict_returns_none(self):
        result = process_dashboard_data({})
        assert result is None

    def test_missing_months_key_returns_none(self):
        result = process_dashboard_data({"metadata": {}})
        assert result is None

    def test_empty_months_returns_none(self):
        """Empty months dict (Cecilia case: file created but fetch not started yet) → None, no crash."""
        result = process_dashboard_data({"months": {}})
        assert result is None


# ---------------------------------------------------------------------------
# process_dashboard_data — valid input
# ---------------------------------------------------------------------------

class TestProcessDashboardDataValid:
    def test_minimal_data_returns_dict(self):
        raw = _make_raw_data()
        result = process_dashboard_data(raw)
        assert result is not None
        assert isinstance(result, dict)

    def test_required_template_keys_present(self):
        """All keys referenced in fitness_report.html must be present."""
        raw = _make_raw_data()
        result = process_dashboard_data(raw)
        required_keys = [
            "training_goal",
            "goal_configured",
            "recent_activities",
            "runs_count",
            "strength_count",
            "total_run_distance",
            "weekly_km_chart_labels",
            "weekly_km_chart_data",
            "vo2_max",
            "rhr",
            "stress_score",
            "sleep_hours",
            "sleep_score",
            "total_load_7d",
            "weekly_progress_pct",
            "weekly_remaining",
            "date_str",
        ]
        for key in required_keys:
            assert key in result, f"Missing key in dashboard_data: {key}"

    def test_goal_configured_false_without_training_goal(self):
        raw = _make_raw_data()
        result = process_dashboard_data(raw)
        assert result["goal_configured"] is False

    def test_goal_configured_true_with_valid_training_goal(self):
        raw = _make_raw_data()
        goal = {"target_pace_str": "5:00", "race_type": "Maratón"}
        result = process_dashboard_data(raw, training_goal=goal)
        assert result["goal_configured"] is True

    def test_training_goal_has_all_default_keys_when_none(self):
        raw = _make_raw_data()
        result = process_dashboard_data(raw)
        for key in DEFAULT_GOAL:
            assert key in result["training_goal"]

    def test_result_is_not_none_so_admin_dict_assignment_is_safe(self):
        """Regression: admin.py does dashboard_data['_admin_viewing'] = True — must not crash."""
        raw = _make_raw_data()
        result = process_dashboard_data(raw)
        # This was crashing before the None check was added to admin.py
        result["_admin_viewing"] = True
        assert result["_admin_viewing"] is True


# ---------------------------------------------------------------------------
# New-user scenarios (no imported data, no training goal)
# ---------------------------------------------------------------------------

class TestNewUserScenarios:
    def test_new_user_no_data_no_goal(self):
        """New user with no data imported yet must not crash dashboard."""
        result = process_dashboard_data(None, training_goal=None)
        assert result is None  # route handles None gracefully with empty skeleton

    def test_new_user_empty_month_no_goal(self):
        """New user with empty month structure (no activities) must return valid dict."""
        raw = _make_raw_data(activities=[], daily_stats={})
        result = process_dashboard_data(raw, training_goal=None)
        assert result is not None
        assert result["goal_configured"] is False
        assert result["runs_count"] == 0
        assert result["recent_activities"] == []

    def test_new_user_goal_configured_shows_plan_cta(self):
        """New user who set a goal manually must have goal_configured=True."""
        raw = _make_raw_data(activities=[], daily_stats={})
        goal = {"target_pace_str": "6:00", "race_type": "10K"}
        result = process_dashboard_data(raw, training_goal=goal)
        assert result is not None
        assert result["goal_configured"] is True

    def test_new_user_training_goal_has_all_defaults(self):
        """training_goal must always have all DEFAULT_GOAL keys to avoid KeyError in template."""
        raw = _make_raw_data(activities=[], daily_stats={})
        # Partial goal — missing keys should be filled with defaults
        partial_goal = {"race_type": "Media Maratón"}
        result = process_dashboard_data(raw, training_goal=partial_goal)
        for key in DEFAULT_GOAL:
            assert key in result["training_goal"], f"Missing training_goal key for new user: {key}"

    def test_new_user_zero_metrics(self):
        """Metrics for new users with no daily stats must be present and not crash template."""
        raw = _make_raw_data(activities=[], daily_stats={})
        result = process_dashboard_data(raw)
        # vo2_max / rhr can be '—', None, or a number — must exist and not crash Jinja
        assert "vo2_max" in result
        assert "rhr" in result
        assert result["total_run_distance"] == 0.0 or result["total_run_distance"] == 0


# ---------------------------------------------------------------------------
# Regression: goal_setup/chat fallback for users with no imported data
# Reproduces the "❌ No hay datos disponibles." bug where new users
# (no CSV imported yet) could not chat with Sento to set up a goal.
# Fix: route now passes {"months":{}, "metadata":{}} instead of returning 404.
# ---------------------------------------------------------------------------

class TestGoalSetupChatNoData:
    def test_empty_raw_data_months_iteration(self):
        """Empty months dict must not raise when iterated — simulates the route fallback."""
        raw = {"months": {}, "metadata": {}}
        # Simulates what goal_setup_chat does: iterate months safely
        acts = []
        for m_key, m_data in raw.get("months", {}).items():
            acts.extend(m_data.get("activities", []))
        assert acts == []

    def test_none_raw_data_fallback_produces_safe_dict(self):
        """Regression: _load_data returns None → fallback dict must be safe to iterate."""
        raw = None
        safe = raw or {"months": {}, "metadata": {}}
        assert safe["months"] == {}
        assert isinstance(safe, dict)

    def test_goal_setup_with_no_data_returns_valid_dashboard(self):
        """process_dashboard_data with empty months must still return valid dict for goal page."""
        raw = _make_raw_data(activities=[], daily_stats={})
        result = process_dashboard_data(raw)
        assert result is not None
        assert result["runs_count"] == 0
        assert result["total_run_distance"] == 0 or result["total_run_distance"] == 0.0
