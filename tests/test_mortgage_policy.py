"""Tests for mortgage policy enforcement — mortgage_gate and audit callbacks."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from mortgage import policy, tools
from mortgage.mortgage import MortgageStore

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


def tool(name):
    """Create a mock tool object with just a name."""
    return SimpleNamespace(name=name)


# --- mortgage_gate tests ---

class TestMortgageGateInputValidation:
    """Tests for input validation in mortgage_gate."""

    def test_credit_score_too_low_blocked(self, store, ctx):
        """get_rate_estimate blocked if credit_score < 300."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 200, "loan_to_value": 80},
            ctx
        ))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "missing_inputs"
        assert "credit score" in result["say"].lower()

    def test_credit_score_too_high_blocked(self, store, ctx):
        """get_rate_estimate blocked if credit_score > 850."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 900, "loan_to_value": 80},
            ctx
        ))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "missing_inputs"
        assert "credit score" in result["say"].lower()

    def test_credit_score_at_minimum_allowed(self, store, ctx):
        """get_rate_estimate allowed if credit_score = 300."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 300, "loan_to_value": 80},
            ctx
        ))
        assert result is None  # allowed

    def test_credit_score_at_maximum_allowed(self, store, ctx):
        """get_rate_estimate allowed if credit_score = 850."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 850, "loan_to_value": 80},
            ctx
        ))
        assert result is None  # allowed

    def test_loan_to_value_too_low_blocked(self, store, ctx):
        """get_rate_estimate blocked if loan_to_value < 1."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 720, "loan_to_value": 0},
            ctx
        ))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "missing_inputs"
        assert "percentage" in result["say"].lower() or "borrow" in result["say"].lower()

    def test_loan_to_value_too_high_blocked(self, store, ctx):
        """get_rate_estimate blocked if loan_to_value > 100."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 720, "loan_to_value": 150},
            ctx
        ))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "missing_inputs"

    def test_loan_to_value_at_minimum_allowed(self, store, ctx):
        """get_rate_estimate allowed if loan_to_value = 1."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 720, "loan_to_value": 1},
            ctx
        ))
        assert result is None  # allowed

    def test_loan_to_value_at_maximum_allowed(self, store, ctx):
        """get_rate_estimate allowed if loan_to_value = 100."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 720, "loan_to_value": 100},
            ctx
        ))
        assert result is None  # allowed

    def test_valid_inputs_allowed(self, store, ctx):
        """get_rate_estimate allowed with valid inputs."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30", "credit_score": 720, "loan_to_value": 80},
            ctx
        ))
        assert result is None  # allowed

    def test_default_values_allowed(self, store, ctx):
        """get_rate_estimate allowed when using default credit_score and loan_to_value."""
        result = run(policy.mortgage_gate(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30"},
            ctx
        ))
        assert result is None  # allowed (defaults are 720, 80)


class TestMortgageGateLockout:
    """Tests for lockout behavior in mortgage_gate."""

    def test_identity_tool_blocked_when_locked(self, store, ctx):
        """Identity tools blocked after 3 failed verifications."""
        call = store.call(ctx.session.id)
        call.failed_verifications = 3
        call.verified_customer_id = None

        result = run(policy.mortgage_gate(
            tool("verify_customer"),
            {"full_name": "Test User", "phone_last_4": "1234"},
            ctx
        ))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "locked"

    def test_non_identity_tool_allowed_when_locked(self, store, ctx):
        """Non-identity tools still allowed when locked."""
        call = store.call(ctx.session.id)
        call.failed_verifications = 3
        call.verified_customer_id = None

        result = run(policy.mortgage_gate(
            tool("get_company_info"),
            {"topic": "hours"},
            ctx
        ))
        assert result is None  # allowed


# --- audit tests ---

class TestAuditDisclaimer:
    """Tests for disclaimer enforcement in audit."""

    def test_disclaimer_added_to_rate_estimate_without_one(self, store, ctx):
        """audit adds disclaimer to get_rate_estimate if missing."""
        tool_response = {
            "result": "ok",
            "product": "Conventional 30-Year",
            "estimated_rate": "6.50%",
            "range": "6.25% to 7.00%"
            # Note: no disclaimer
        }
        result = run(policy.audit(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30"},
            ctx,
            tool_response
        ))
        assert result is not None
        assert "disclaimer" in result
        assert result["disclaimer"] == store.info["disclaimers"]["rates"]
        # Original should not be mutated
        assert "disclaimer" not in tool_response

    def test_existing_disclaimer_preserved(self, store, ctx):
        """audit doesn't override existing disclaimer."""
        tool_response = {
            "result": "ok",
            "product": "Conventional 30-Year",
            "estimated_rate": "6.50%",
            "range": "6.25% to 7.00%",
            "disclaimer": "Custom disclaimer"
        }
        result = run(policy.audit(
            tool("get_rate_estimate"),
            {"product_type": "conventional_30"},
            ctx,
            tool_response
        ))
        # No modification needed since disclaimer exists
        assert result is None


class TestAuditTransferHint:
    """Tests for transfer hint enforcement in audit."""

    def test_say_added_for_ineligible_heloc(self, store, ctx):
        """audit adds 'say' for check_heloc_eligibility with eligible=False."""
        tool_response = {
            "result": "ok",
            "eligible": False,
            "state": "TX",
            "issues": ["TX does not allow HELOCs on investment properties"],
            "alternatives": ["Consider a cash-out refinance"]
            # Note: no "say"
        }
        result = run(policy.audit(
            tool("check_heloc_eligibility"),
            {"state": "TX", "property_type": "investment"},
            ctx,
            tool_response
        ))
        assert result is not None
        assert "say" in result
        assert "loan officer" in result["say"].lower()
        # Original should not be mutated
        assert "say" not in tool_response

    def test_say_added_for_needs_review_heloc(self, store, ctx):
        """audit adds 'say' for check_heloc_eligibility with status=needs_review."""
        tool_response = {
            "result": "ok",
            "eligible": None,
            "state": "NY",
            "status": "needs_review"
            # Note: no "say"
        }
        result = run(policy.audit(
            tool("check_heloc_eligibility"),
            {"state": "NY", "property_type": "primary"},
            ctx,
            tool_response
        ))
        assert result is not None
        assert "say" in result
        assert "loan officer" in result["say"].lower()

    def test_existing_say_preserved(self, store, ctx):
        """audit doesn't override existing 'say'."""
        tool_response = {
            "result": "ok",
            "eligible": False,
            "state": "TX",
            "issues": ["TX does not allow HELOCs on investment properties"],
            "alternatives": ["Consider a cash-out refinance"],
            "say": "Custom guidance here"
        }
        result = run(policy.audit(
            tool("check_heloc_eligibility"),
            {"state": "TX", "property_type": "investment"},
            ctx,
            tool_response
        ))
        # No modification needed since say exists
        assert result is None

    def test_say_added_for_calculate_equity_ineligible(self, store, ctx):
        """audit adds 'say' for calculate_equity in SUGGEST_TRANSFER with ineligible."""
        tool_response = {
            "result": "ok",
            "eligible": False,
            "property_value": "$500 thousand",
            "equity": "$100 thousand"
            # Note: no "say"
        }
        result = run(policy.audit(
            tool("calculate_equity"),
            {"property_value": 500000, "mortgage_balance": 400000},
            ctx,
            tool_response
        ))
        assert result is not None
        assert "say" in result
        assert "loan officer" in result["say"].lower()

    def test_no_say_for_eligible_result(self, store, ctx):
        """audit doesn't add 'say' for eligible results."""
        tool_response = {
            "result": "ok",
            "eligible": True,
            "state": "CA",
            "max_heloc_amount": "$200 thousand"
        }
        result = run(policy.audit(
            tool("check_heloc_eligibility"),
            {"state": "CA", "property_type": "primary"},
            ctx,
            tool_response
        ))
        # No modification needed for eligible result
        assert result is None

    def test_no_say_for_non_suggest_transfer_tool(self, store, ctx):
        """audit doesn't add 'say' for tools not in SUGGEST_TRANSFER."""
        tool_response = {
            "result": "ok",
            "eligible": False,  # Even if ineligible
            "some_data": "value"
        }
        result = run(policy.audit(
            tool("get_company_info"),  # Not in SUGGEST_TRANSFER
            {"topic": "hours"},
            ctx,
            tool_response
        ))
        # No modification for non-SUGGEST_TRANSFER tools
        assert result is None


class TestAuditNoMutation:
    """Tests that audit doesn't mutate original responses."""

    def test_original_response_not_mutated_on_disclaimer_add(self, store, ctx):
        """Original tool_response dict is not mutated when adding disclaimer."""
        original = {
            "result": "ok",
            "product": "HELOC",
            "estimated_rate": "7.00%"
        }
        original_copy = dict(original)

        run(policy.audit(
            tool("get_rate_estimate"),
            {"product_type": "heloc"},
            ctx,
            original
        ))

        assert original == original_copy  # No mutation

    def test_original_response_not_mutated_on_say_add(self, store, ctx):
        """Original tool_response dict is not mutated when adding say."""
        original = {
            "result": "ok",
            "eligible": False,
            "state": "TX",
            "issues": ["Investment property restriction"]
        }
        original_copy = dict(original)

        run(policy.audit(
            tool("check_heloc_eligibility"),
            {"state": "TX", "property_type": "investment"},
            ctx,
            original
        ))

        assert original == original_copy  # No mutation


class TestAuditCombined:
    """Tests for combined audit behaviors."""

    def test_disclaimer_and_say_both_added(self, store, ctx):
        """audit can add both disclaimer (for rate estimate context) and process correctly."""
        # This tests that when get_rate_estimate returns ineligible (unusual but possible)
        # and is in SUGGEST_TRANSFER, both disclaimer and say are added
        tool_response = {
            "result": "ok",
            "eligible": False,  # ineligible
            "product": "HELOC",
            "estimated_rate": "7.00%"
            # No disclaimer, no say
        }
        result = run(policy.audit(
            tool("get_rate_estimate"),
            {"product_type": "heloc"},
            ctx,
            tool_response
        ))
        assert result is not None
        assert "disclaimer" in result
        assert "say" in result
