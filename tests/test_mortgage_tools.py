"""Tests for mortgage tool functions and store methods."""
import asyncio
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from mortgage import tools
from mortgage.mortgage import MortgageStore


# Test times with timezone awareness
def utc_time(hour, minute=0):
    """Create a UTC datetime for testing."""
    return datetime(2026, 10, 5, hour, minute, tzinfo=ZoneInfo("UTC"))


def chicago_time(hour, minute=0):
    """Create a Chicago timezone datetime for testing."""
    return datetime(2026, 10, 5, hour, minute, tzinfo=ZoneInfo("America/Chicago"))


@pytest.fixture
def store():
    # Monday 2026-10-05, 09:00 Central Time (within working hours)
    s = MortgageStore.load(clock=lambda: chicago_time(9, 0))
    tools.use_store(s)
    return s


@pytest.fixture
def ctx():
    return SimpleNamespace(session=SimpleNamespace(id="call-1"))


def run(coro):
    return asyncio.run(coro)


# --- get_company_info tests ---

class TestGetCompanyInfo:
    """Tests for get_company_info tool."""

    def test_hours_uses_spoken_time_format(self, store, ctx):
        """get_company_info returns hours with proper spoken time format."""
        result = run(tools.get_company_info("hours"))
        assert result["result"] == "ok"
        assert "hours" in result
        hours = result["hours"]

        # Should be a list, not a hardcoded string
        assert isinstance(hours, list)
        assert len(hours) == 7  # All days of the week

        # Check Monday format - should have spoken time like "8:00 AM to 8:00 PM"
        mon_entry = next(h for h in hours if h.startswith("Mon"))
        assert "AM" in mon_entry or "PM" in mon_entry  # spoken_time uses AM/PM
        assert "to" in mon_entry  # Range format

        # Check Sunday is closed
        sun_entry = next(h for h in hours if h.startswith("Sun"))
        assert "closed" in sun_entry.lower()

    def test_hours_not_hardcoded_string(self, store, ctx):
        """Hours should be a list built from store.hours, not a hardcoded string."""
        result = run(tools.get_company_info("hours"))
        hours = result["hours"]

        # Verify it's using actual store data by checking format
        assert isinstance(hours, list)
        # Each entry should be like "Mon: 8:00 AM to 8:00 PM"
        for entry in hours:
            assert ": " in entry  # Day name followed by colon


# --- get_available_officers tests ---

class TestGetAvailableOfficers:
    """Tests for get_available_officers method."""

    def test_officers_available_during_work_hours(self):
        """Officers are available during their working hours on workdays."""
        # Monday 9:00 AM Central - within working hours (9am-5pm)
        store = MortgageStore.load(clock=lambda: chicago_time(9, 0))
        officers = store.get_available_officers()

        # All officers who work Monday should be available
        assert len(officers) > 0
        for officer in officers:
            assert "mon" in officer.available

    def test_officers_unavailable_before_work_hours(self):
        """Officers are not available before their working hours."""
        # Monday 7:00 AM Central - before working hours (9am-5pm)
        store = MortgageStore.load(clock=lambda: chicago_time(7, 0))
        officers = store.get_available_officers()

        # No officers should be available at 7 AM
        assert len(officers) == 0

    def test_officers_unavailable_after_work_hours(self):
        """Officers are not available after their working hours."""
        # Monday 6:00 PM Central - after working hours (9am-5pm)
        store = MortgageStore.load(clock=lambda: chicago_time(18, 0))
        officers = store.get_available_officers()

        # No officers should be available at 6 PM
        assert len(officers) == 0

    def test_officers_unavailable_on_off_day(self):
        """Officers are not available on days they don't work."""
        # Sunday 10:00 AM Central - Sunday, no one works
        sunday = datetime(2026, 10, 4, 10, 0, tzinfo=ZoneInfo("America/Chicago"))
        store = MortgageStore.load(clock=lambda: sunday)
        officers = store.get_available_officers()

        # No officers work Sunday
        assert len(officers) == 0

    def test_saturday_officer_availability(self):
        """Only officers who work Saturday are available on Saturday."""
        # Saturday 10:00 AM Central
        saturday = datetime(2026, 10, 10, 10, 0, tzinfo=ZoneInfo("America/Chicago"))
        store = MortgageStore.load(clock=lambda: saturday)
        officers = store.get_available_officers()

        # Only Williams works Saturday
        assert len(officers) == 1
        assert officers[0].name == "Michael Williams"

    def test_officer_at_boundary_start_time(self):
        """Officers are available exactly at their start time."""
        # Monday 9:00 AM exactly - start of working hours
        store = MortgageStore.load(clock=lambda: chicago_time(9, 0))
        officers = store.get_available_officers()

        # Should have officers available
        assert len(officers) > 0

    def test_officer_at_boundary_end_time(self):
        """Officers are available exactly at their end time."""
        # Monday 5:00 PM exactly - end of working hours
        store = MortgageStore.load(clock=lambda: chicago_time(17, 0))
        officers = store.get_available_officers()

        # Should still have officers available
        assert len(officers) > 0

    def test_specialty_filter_with_hours(self):
        """Specialty filter works correctly with hour checking."""
        # Monday 10:00 AM - during working hours
        store = MortgageStore.load(clock=lambda: chicago_time(10, 0))

        # Lisa Chen is the only HELOC specialist
        officers = store.get_available_officers(specialty="heloc")
        assert len(officers) == 1
        assert officers[0].name == "Lisa Chen"

    def test_specialty_filter_outside_hours(self):
        """Specialty filter returns no one outside working hours."""
        # Monday 7:00 AM - before working hours
        store = MortgageStore.load(clock=lambda: chicago_time(7, 0))

        # Even HELOC specialist shouldn't be available
        officers = store.get_available_officers(specialty="heloc")
        assert len(officers) == 0


# --- check_heloc_eligibility note format tests ---

class TestHelocEligibilityNote:
    """Tests for check_heloc_eligibility note format."""

    def test_texas_note_has_closing_and_rescission(self, store):
        """Texas note mentions both closing notice and rescission periods."""
        result = store.check_heloc_eligibility("TX", "primary")
        assert "note" in result
        note = result["note"]

        # Should mention 12-day notice before closing
        assert "12-day" in note
        assert "notice" in note.lower()
        assert "closing" in note.lower()

        # Should mention 3-day right to cancel
        assert "3-day" in note
        assert "cancel" in note.lower()

    def test_texas_note_phrasing(self, store):
        """Texas note is phrased correctly."""
        result = store.check_heloc_eligibility("TX", "primary")
        note = result["note"]

        # Should match expected phrasing
        expected = "TX requires a 12-day notice period before closing, plus a 3-day right to cancel after closing."
        assert note == expected

    def test_california_no_closing_notice(self, store):
        """California doesn't have closing notice requirements."""
        result = store.check_heloc_eligibility("CA", "primary")

        # California has no closing_notice_days in the data
        assert "note" not in result or result.get("note") is None or "notice" not in result.get("note", "").lower()


# --- LoanOfficer dataclass tests ---

class TestLoanOfficerDataclass:
    """Tests for LoanOfficer dataclass with hours and timezone."""

    def test_loan_officers_have_hours(self, store):
        """All loan officers have hours field populated."""
        for officer in store.loan_officers.values():
            assert officer.hours is not None
            assert isinstance(officer.hours, tuple)
            assert len(officer.hours) == 2
            assert isinstance(officer.hours[0], time)
            assert isinstance(officer.hours[1], time)

    def test_loan_officers_have_timezone(self, store):
        """All loan officers have timezone field populated."""
        for officer in store.loan_officers.values():
            assert officer.timezone is not None
            assert officer.timezone == "America/Chicago"

    def test_loan_officer_hours_values(self, store):
        """Loan officer hours are parsed correctly."""
        for officer in store.loan_officers.values():
            start_time, end_time = officer.hours
            assert start_time == time(9, 0)  # 09:00
            assert end_time == time(17, 0)  # 17:00


# --- State regulations source and last_reviewed tests ---

class TestStateRegulationsMetadata:
    """Tests for state regulations metadata fields."""

    def test_texas_heloc_rules_have_source(self, store):
        """Texas HELOC rules have a source citation."""
        state_code, rules = store.get_state_rules("TX", "heloc")
        assert rules is not None
        assert "source" in rules
        assert "Texas Constitution" in rules["source"]
        assert "Art. XVI" in rules["source"]
        assert "Sec. 50" in rules["source"]

    def test_texas_heloc_rules_have_last_reviewed(self, store):
        """Texas HELOC rules have a last_reviewed date."""
        state_code, rules = store.get_state_rules("TX", "heloc")
        assert rules is not None
        assert "last_reviewed" in rules
        # Should be a date string
        assert "2026" in rules["last_reviewed"]

    def test_texas_cashout_rules_have_source(self, store):
        """Texas cashout rules have a source citation."""
        state_code, rules = store.get_state_rules("TX", "cashout")
        assert rules is not None
        assert "source" in rules
        assert "Texas Constitution" in rules["source"]

    def test_california_rules_have_metadata(self, store):
        """California rules have source and last_reviewed."""
        state_code, rules = store.get_state_rules("CA", "heloc")
        assert rules is not None
        assert "source" in rules
        assert "last_reviewed" in rules
