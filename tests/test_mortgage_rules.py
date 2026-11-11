"""Tests for HELOC eligibility rules, state normalization, and property use normalization."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from mortgage import tools
from mortgage.mortgage import (
    MortgageError,
    MortgageStore,
    normalize_property_use,
    normalize_state,
)

# Monday 2026-10-05, 09:00 — every test runs on the same frozen morning.
NOW = datetime(2026, 10, 5, 9, 0)


@pytest.fixture
def store():
    s = MortgageStore.load(clock=lambda: NOW)
    tools.use_store(s)
    return s


@pytest.fixture
def ctx():
    return SimpleNamespace(session=SimpleNamespace(id="call-1"))


def run(coro):
    return asyncio.run(coro)


# --- State normalization tests ---

def test_normalize_state_tx_variations():
    """'Texas' and 'tx' both normalize to 'TX'."""
    assert normalize_state("Texas") == "TX"
    assert normalize_state("texas") == "TX"
    assert normalize_state("TEXAS") == "TX"
    assert normalize_state("tx") == "TX"
    assert normalize_state("TX") == "TX"
    assert normalize_state("  TX  ") == "TX"


def test_normalize_state_other_states():
    """Various state names and codes normalize correctly."""
    assert normalize_state("California") == "CA"
    assert normalize_state("new york") == "NY"
    assert normalize_state("FL") == "FL"
    assert normalize_state("district of columbia") == "DC"


def test_normalize_state_unknown_raises():
    """Unknown state raises MortgageError."""
    with pytest.raises(MortgageError) as exc_info:
        normalize_state("Narnia")
    assert exc_info.value.code == "unknown_state"


def test_normalize_state_empty_raises():
    """Empty state raises MortgageError."""
    with pytest.raises(MortgageError) as exc_info:
        normalize_state("")
    assert exc_info.value.code == "unknown_state"


# --- Property use normalization tests ---

def test_normalize_property_use_rental_to_investment():
    """'rental' maps to 'investment'."""
    assert normalize_property_use("rental") == "investment"
    assert normalize_property_use("rent out") == "investment"
    assert normalize_property_use("rented") == "investment"
    assert normalize_property_use("investment property") == "investment"
    assert normalize_property_use("income property") == "investment"
    assert normalize_property_use("landlord") == "investment"
    assert normalize_property_use("investment") == "investment"


def test_normalize_property_use_primary():
    """Primary residence terms map to 'primary'."""
    assert normalize_property_use("primary residence") == "primary"
    assert normalize_property_use("homestead") == "primary"
    assert normalize_property_use("my home") == "primary"
    assert normalize_property_use("owner occupied") == "primary"
    assert normalize_property_use("primary") == "primary"


def test_normalize_property_use_second_home():
    """Second home terms map to 'second_home'."""
    assert normalize_property_use("vacation") == "second_home"
    assert normalize_property_use("second home") == "second_home"
    assert normalize_property_use("secondary") == "second_home"


def test_normalize_property_use_unknown_raises():
    """Unknown property use raises MortgageError."""
    with pytest.raises(MortgageError) as exc_info:
        normalize_property_use("commercial warehouse")
    assert exc_info.value.code == "unknown_property_use"


def test_normalize_property_use_empty_raises():
    """Empty property use raises MortgageError."""
    with pytest.raises(MortgageError) as exc_info:
        normalize_property_use("")
    assert exc_info.value.code == "unknown_property_use"


# --- HELOC eligibility tests ---

def test_tx_rental_ineligible(store):
    """TX + 'rental' -> ineligible (investment properties not allowed in Texas)."""
    result = store.check_heloc_eligibility("TX", "rental")
    assert result["eligible"] is False
    assert result["state"] == "TX"
    assert result["property_type"] == "investment"  # normalized from "rental"
    assert any("investment properties" in issue for issue in result["issues"])


def test_tx_investment_ineligible(store):
    """TX + 'investment' -> ineligible."""
    result = store.check_heloc_eligibility("TX", "investment")
    assert result["eligible"] is False
    assert result["state"] == "TX"
    assert result["property_type"] == "investment"
    assert any("investment properties" in issue for issue in result["issues"])


def test_tx_primary_with_cashout_ineligible_with_alternatives(store):
    """TX primary with existing cash-out -> ineligible with both alternatives."""
    result = store.check_heloc_eligibility("TX", "primary", has_existing_cashout=True)
    assert result["eligible"] is False
    assert result["state"] == "TX"
    assert any("existing cash-out" in issue for issue in result["issues"])
    # Should have both alternatives
    assert len(result["alternatives"]) >= 2
    assert any("refinance" in alt.lower() for alt in result["alternatives"])
    assert any("cash-out" in alt.lower() for alt in result["alternatives"])


def test_tx_primary_eligible_with_max_heloc(store):
    """TX primary, value 550000, balance 100000, no cash-out -> eligible with max_heloc_amount for 80% CLTV."""
    property_value = 550_000
    current_balance = 100_000
    result = store.check_heloc_eligibility(
        "TX", "primary",
        has_existing_cashout=False,
        property_value=property_value,
        current_balance=current_balance
    )
    assert result["eligible"] is True
    assert result["state"] == "TX"
    # 80% CLTV max for Texas
    # max_heloc = (550000 * 0.80) - 100000 = 440000 - 100000 = 340000
    expected_max = (property_value * 0.80) - current_balance
    assert result["max_heloc_amount"] == expected_max
    assert result["max_cltv"] == 80


def test_texas_normalizes_to_tx(store):
    """'Texas' normalizes to 'TX' in result."""
    result = store.check_heloc_eligibility("Texas", "primary")
    assert result["state"] == "TX"


def test_tx_lowercase_normalizes(store):
    """'tx' normalizes to 'TX' in result."""
    result = store.check_heloc_eligibility("tx", "primary")
    assert result["state"] == "TX"


def test_ny_no_rules_needs_review(store):
    """NY (no rules) -> needs_review, never eligible."""
    result = store.check_heloc_eligibility("NY", "primary")
    assert result.get("status") == "needs_review"
    assert result["eligible"] is None  # Not eligible, not ineligible - needs review
    assert result["state"] == "NY"
    assert "loan officer" in result["say"].lower()


def test_new_york_normalizes_to_ny_needs_review(store):
    """'New York' normalizes to 'NY' and returns needs_review."""
    result = store.check_heloc_eligibility("New York", "primary")
    assert result["state"] == "NY"
    assert result.get("status") == "needs_review"
    assert result["eligible"] is None


def test_unknown_property_use_raises_error(store):
    """Unknown property use returns error through the MortgageStore."""
    with pytest.raises(MortgageError) as exc_info:
        store.check_heloc_eligibility("TX", "submarine base")
    assert exc_info.value.code == "unknown_property_use"


# --- Tools layer tests ---

def test_check_heloc_eligibility_tool_tx_rental(store, ctx):
    """Tool: TX + 'rental' -> ineligible."""
    result = run(tools.check_heloc_eligibility("TX", "rental"))
    assert result["result"] == "ok"
    assert result["eligible"] is False
    assert result["state"] == "TX"


def test_check_heloc_eligibility_tool_ny_needs_review(store, ctx):
    """Tool: NY -> needs_review status."""
    result = run(tools.check_heloc_eligibility("NY", "primary"))
    assert result["result"] == "ok"
    assert result["eligible"] is None
    assert result["status"] == "needs_review"
    assert result["state"] == "NY"


def test_check_heloc_eligibility_tool_unknown_property_use(store, ctx):
    """Tool: Unknown property use returns error."""
    result = run(tools.check_heloc_eligibility("TX", "submarine base"))
    assert result["result"] == "error"
    assert result["code"] == "unknown_property_use"


def test_check_state_regulations_tool_ny_needs_review(store, ctx):
    """Tool: check_state_regulations for NY returns needs_review."""
    result = run(tools.check_state_regulations("NY", "heloc"))
    assert result["result"] == "ok"
    assert result["state"] == "NY"
    assert result["status"] == "needs_review"
    assert "loan officer" in result["say"].lower()


def test_check_state_regulations_tool_tx_has_rules(store, ctx):
    """Tool: check_state_regulations for TX returns rules."""
    result = run(tools.check_state_regulations("TX", "heloc"))
    assert result["result"] == "ok"
    assert result["state"] == "TX"
    assert "rules" in result
    assert result["rules"]["allowed_on_investment"] is False


def test_check_state_regulations_tool_unknown_state(store, ctx):
    """Tool: check_state_regulations for unknown state returns error."""
    result = run(tools.check_state_regulations("Narnia", "heloc"))
    assert result["result"] == "error"
    assert result["code"] == "unknown_state"
