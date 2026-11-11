"""Tests for option comparison, payment calculations, and rate sheet lookups."""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mortgage.mortgage import MortgageStore, MortgageError


# --- Fixtures ---

@pytest.fixture
def store_with_rates():
    """Create a store with a complete rate sheet for testing."""
    seed = {
        "company": {
            "name": "Test Mortgage Co",
            "phone": "555-1234",
            "address": "123 Test St",
            "hours": {"mon": ["09:00", "17:00"]},
            "disclaimers": {"rates": "Rates subject to change."}
        },
        "products": {
            "heloc": {
                "name": "HELOC",
                "description": "Home Equity Line of Credit",
                "rate_range": [7.0, 10.0]
            },
            "cash_out_refi": {
                "name": "Cash-Out Refinance",
                "description": "Cash-out refinance",
                "rate_range": [6.0, 8.0]
            },
            "rate_term_refi": {
                "name": "Rate-Term Refinance",
                "description": "Rate and term refinance",
                "rate_range": [5.5, 7.5]
            }
        },
        "state_regulations": {
            "TX": {
                "heloc_rules": {
                    "max_cltv": 80,
                    "allowed_on_investment": False,
                    "closing_notice_days": 12,
                    "rescission_days": 3
                }
            },
            "CA": {
                "heloc_rules": {
                    "max_cltv": 90,
                    "allowed_on_investment": True
                }
            }
        },
        "loan_officers": [],
        "customers": []
    }

    rate_sheet = {
        "as_of": "2026-10-05",
        "credit_tiers": {
            "excellent": {"min": 760, "max": 850},
            "good": {"min": 720, "max": 759},
            "fair": {"min": 680, "max": 719},
            "below_average": {"min": 640, "max": 679},
            "poor": {"min": 300, "max": 639}
        },
        "ltv_bands": {
            "low": {"min": 0, "max": 60},
            "moderate": {"min": 61, "max": 75},
            "standard": {"min": 76, "max": 80},
            "high": {"min": 81, "max": 90},
            "very_high": {"min": 91, "max": 100}
        },
        "products": {
            "heloc": {
                "name": "Home Equity Line of Credit",
                "rates": {
                    "excellent": {"low": 7.25, "moderate": 7.5, "standard": 7.75, "high": None, "very_high": None},
                    "good": {"low": 7.75, "moderate": 8.0, "standard": 8.25, "high": None, "very_high": None},
                    "fair": {"low": 8.25, "moderate": 8.5, "standard": 8.75, "high": None, "very_high": None}
                },
                "market_benchmark": {"rate": 8.75, "source": "Test Index"}
            },
            "cash_out_refi": {
                "name": "Cash-Out Refinance",
                "term_years": 30,
                "rates": {
                    "excellent": {"low": 6.125, "moderate": 6.25, "standard": 6.375, "high": None, "very_high": None},
                    "good": {"low": 6.375, "moderate": 6.5, "standard": 6.625, "high": None, "very_high": None}
                },
                "market_benchmark": {"rate": 7.0, "source": "Test Index"}
            },
            "rate_term_refi": {
                "name": "Rate-and-Term Refinance",
                "term_years": 30,
                "rates": {
                    "excellent": {"low": 5.75, "moderate": 5.875, "standard": 6.0, "high": 6.25, "very_high": 6.5},
                    "good": {"low": 6.0, "moderate": 6.125, "standard": 6.25, "high": 6.5, "very_high": 6.75}
                },
                "market_benchmark": {"rate": 6.5, "source": "Test Index"}
            }
        }
    }

    return MortgageStore(seed, rate_sheet=rate_sheet)


# --- Payment Calculation Tests ---

class TestMonthlyPaymentCalculation:
    """Tests for calculate_monthly_payment static method."""

    def test_standard_30_year_mortgage(self, store_with_rates):
        """Calculate payment for a standard 30-year mortgage."""
        # $300,000 at 6% for 30 years = ~$1,798.65
        payment = store_with_rates.calculate_monthly_payment(300000, 6.0, 30)
        assert 1798 <= payment <= 1800

    def test_15_year_mortgage(self, store_with_rates):
        """Calculate payment for a 15-year mortgage."""
        # $300,000 at 5.5% for 15 years = ~$2,451.55
        payment = store_with_rates.calculate_monthly_payment(300000, 5.5, 15)
        assert 2451 <= payment <= 2452

    def test_zero_principal_returns_zero(self, store_with_rates):
        """Zero principal should return zero payment."""
        payment = store_with_rates.calculate_monthly_payment(0, 6.0, 30)
        assert payment == 0.0

    def test_zero_rate_returns_zero(self, store_with_rates):
        """Zero rate should return zero."""
        payment = store_with_rates.calculate_monthly_payment(300000, 0, 30)
        assert payment == 0.0

    def test_negative_values_return_zero(self, store_with_rates):
        """Negative values should return zero."""
        assert store_with_rates.calculate_monthly_payment(-100000, 6.0, 30) == 0.0
        assert store_with_rates.calculate_monthly_payment(100000, -6.0, 30) == 0.0
        assert store_with_rates.calculate_monthly_payment(100000, 6.0, -30) == 0.0


# --- Credit Tier Tests ---

class TestCreditTierLookup:
    """Tests for get_credit_tier method."""

    def test_excellent_tier(self, store_with_rates):
        """Scores 760-850 are excellent."""
        assert store_with_rates.get_credit_tier(760) == "excellent"
        assert store_with_rates.get_credit_tier(800) == "excellent"
        assert store_with_rates.get_credit_tier(850) == "excellent"

    def test_good_tier(self, store_with_rates):
        """Scores 720-759 are good."""
        assert store_with_rates.get_credit_tier(720) == "good"
        assert store_with_rates.get_credit_tier(740) == "good"
        assert store_with_rates.get_credit_tier(759) == "good"

    def test_fair_tier(self, store_with_rates):
        """Scores 680-719 are fair."""
        assert store_with_rates.get_credit_tier(680) == "fair"
        assert store_with_rates.get_credit_tier(700) == "fair"
        assert store_with_rates.get_credit_tier(719) == "fair"

    def test_below_average_tier(self, store_with_rates):
        """Scores 640-679 are below_average."""
        assert store_with_rates.get_credit_tier(640) == "below_average"
        assert store_with_rates.get_credit_tier(660) == "below_average"
        assert store_with_rates.get_credit_tier(679) == "below_average"

    def test_poor_tier(self, store_with_rates):
        """Scores 300-639 are poor."""
        assert store_with_rates.get_credit_tier(300) == "poor"
        assert store_with_rates.get_credit_tier(500) == "poor"
        assert store_with_rates.get_credit_tier(639) == "poor"

    def test_no_rate_sheet_returns_none(self):
        """No rate sheet returns None."""
        seed = {
            "company": {"name": "Test", "phone": "", "address": "", "hours": {}, "disclaimers": {}},
            "products": {},
            "loan_officers": []
        }
        store = MortgageStore(seed)
        assert store.get_credit_tier(750) is None


# --- LTV Band Tests ---

class TestLtvBandLookup:
    """Tests for get_ltv_band method."""

    def test_low_band(self, store_with_rates):
        """LTV 0-60% is low."""
        assert store_with_rates.get_ltv_band(0) == "low"
        assert store_with_rates.get_ltv_band(30) == "low"
        assert store_with_rates.get_ltv_band(60) == "low"

    def test_moderate_band(self, store_with_rates):
        """LTV 61-75% is moderate."""
        assert store_with_rates.get_ltv_band(61) == "moderate"
        assert store_with_rates.get_ltv_band(70) == "moderate"
        assert store_with_rates.get_ltv_band(75) == "moderate"

    def test_standard_band(self, store_with_rates):
        """LTV 76-80% is standard."""
        assert store_with_rates.get_ltv_band(76) == "standard"
        assert store_with_rates.get_ltv_band(78) == "standard"
        assert store_with_rates.get_ltv_band(80) == "standard"

    def test_high_band(self, store_with_rates):
        """LTV 81-90% is high."""
        assert store_with_rates.get_ltv_band(81) == "high"
        assert store_with_rates.get_ltv_band(85) == "high"
        assert store_with_rates.get_ltv_band(90) == "high"

    def test_very_high_band(self, store_with_rates):
        """LTV 91-100% is very_high."""
        assert store_with_rates.get_ltv_band(91) == "very_high"
        assert store_with_rates.get_ltv_band(95) == "very_high"
        assert store_with_rates.get_ltv_band(100) == "very_high"


# --- Rate Sheet Lookup Tests ---

class TestRateFromSheet:
    """Tests for get_rate_from_sheet method."""

    def test_excellent_low_ltv_heloc(self, store_with_rates):
        """Excellent credit + low LTV gets best HELOC rate."""
        result = store_with_rates.get_rate_from_sheet("heloc", 800, 50)
        assert result is not None
        assert result["rate"] == 7.25
        assert result["credit_tier"] == "excellent"
        assert result["ltv_band"] == "low"

    def test_good_moderate_ltv(self, store_with_rates):
        """Good credit + moderate LTV."""
        result = store_with_rates.get_rate_from_sheet("heloc", 730, 70)
        assert result is not None
        assert result["rate"] == 8.0
        assert result["credit_tier"] == "good"
        assert result["ltv_band"] == "moderate"

    def test_unavailable_combination_returns_none(self, store_with_rates):
        """HELOC not available at high LTV."""
        result = store_with_rates.get_rate_from_sheet("heloc", 800, 85)
        # Rate is None in the sheet for high LTV HELOC
        assert result is None

    def test_unknown_product_returns_none(self, store_with_rates):
        """Unknown product returns None."""
        result = store_with_rates.get_rate_from_sheet("unknown_product", 750, 75)
        assert result is None

    def test_benchmark_included(self, store_with_rates):
        """Market benchmark is included in result."""
        result = store_with_rates.get_rate_from_sheet("heloc", 800, 50)
        assert result["benchmark"]["rate"] == 8.75


# --- Rate Estimate Tests ---

class TestRateEstimate:
    """Tests for get_rate_estimate using rate sheet."""

    def test_uses_rate_sheet_when_available(self, store_with_rates):
        """get_rate_estimate uses rate sheet rates."""
        result = store_with_rates.get_rate_estimate("heloc", 800, 50)
        assert result["estimated_rate"] == 7.25
        assert result["credit_tier"] == "excellent"
        assert result["market_benchmark"] == 8.75

    def test_includes_benchmark_comparison(self, store_with_rates):
        """Result includes market benchmark for comparison."""
        result = store_with_rates.get_rate_estimate("cash_out_refi", 800, 60)
        assert "market_benchmark" in result
        assert result["market_benchmark"] == 7.0

    def test_falls_back_to_product_range(self, store_with_rates):
        """Falls back to product rate_range when sheet unavailable."""
        # Poor credit at high LTV - not in rate sheet
        result = store_with_rates.get_rate_estimate("heloc", 600, 85)
        # Should fall back to product rate_range with adjustments
        assert "estimated_rate" in result
        assert "rate_range" in result


# --- Compare Options Tests ---

class TestCompareOptions:
    """Tests for compare_options method."""

    def test_california_eligible_for_heloc(self, store_with_rates):
        """California property eligible for HELOC."""
        result = store_with_rates.compare_options(
            property_value=500000,
            current_balance=200000,
            credit_score=750,
            state="CA"
        )
        assert result["state"] == "CA"
        assert len(result["options"]) > 0
        # Should have HELOC option since CA allows it
        heloc_options = [o for o in result["options"] if o["product"] == "heloc"]
        assert len(heloc_options) > 0

    def test_texas_heloc_at_80_ltv_blocked(self, store_with_rates):
        """Texas HELOC blocked when at 80% LTV."""
        result = store_with_rates.compare_options(
            property_value=500000,
            current_balance=400000,  # 80% LTV
            credit_score=750,
            state="TX"
        )
        assert result["state"] == "TX"
        # HELOC should not be available
        heloc_options = [o for o in result["options"] if o["product"] == "heloc"]
        assert len(heloc_options) == 0
        # Should have heloc_issues
        assert len(result["heloc_issues"]) > 0

    def test_includes_cash_out_option(self, store_with_rates):
        """Cash-out refinance option included when LTV allows."""
        result = store_with_rates.compare_options(
            property_value=500000,
            current_balance=200000,
            credit_score=750,
            state="CA",
            cash_needed=50000
        )
        cashout_options = [o for o in result["options"] if o["product"] == "cash_out_refi"]
        assert len(cashout_options) > 0
        opt = cashout_options[0]
        assert "monthly_payment" in opt
        assert opt["amount"] == 250000  # 200k + 50k

    def test_includes_rate_term_refi_when_no_cash(self, store_with_rates):
        """Rate-term refi included when cash_needed is 0."""
        result = store_with_rates.compare_options(
            property_value=500000,
            current_balance=300000,
            credit_score=750,
            state="CA",
            cash_needed=0
        )
        refi_options = [o for o in result["options"] if o["product"] == "rate_term_refi"]
        assert len(refi_options) > 0

    def test_no_rate_term_refi_when_cash_needed(self, store_with_rates):
        """Rate-term refi not included when cash_needed > 0."""
        result = store_with_rates.compare_options(
            property_value=500000,
            current_balance=200000,
            credit_score=750,
            state="CA",
            cash_needed=50000
        )
        refi_options = [o for o in result["options"] if o["product"] == "rate_term_refi"]
        assert len(refi_options) == 0

    def test_savings_vs_market_calculated(self, store_with_rates):
        """Savings vs market benchmark calculated when rate is lower."""
        result = store_with_rates.compare_options(
            property_value=500000,
            current_balance=200000,
            credit_score=800,  # Excellent credit
            state="CA"
        )
        # Some options should show savings vs market
        options_with_savings = [o for o in result["options"] if "savings_vs_market" in o]
        # With excellent credit, rates should be below benchmark
        assert len(options_with_savings) > 0

    def test_unknown_state_raises_error(self, store_with_rates):
        """Unknown state raises MortgageError."""
        with pytest.raises(MortgageError) as exc:
            store_with_rates.compare_options(
                property_value=500000,
                current_balance=200000,
                credit_score=750,
                state="ZZ"
            )
        assert exc.value.code == "unknown_state"

    def test_state_without_rules_shows_heloc_issues(self, store_with_rates):
        """State without rules on file shows heloc_issues."""
        # Add a state with no rules
        store_with_rates.state_regulations["FL"] = {}

        result = store_with_rates.compare_options(
            property_value=500000,
            current_balance=200000,
            credit_score=750,
            state="FL"
        )
        assert "State regulations not on file" in result["heloc_issues"]

    def test_equity_calculation(self, store_with_rates):
        """Equity is correctly calculated."""
        result = store_with_rates.compare_options(
            property_value=500000,
            current_balance=200000,
            credit_score=750,
            state="CA"
        )
        assert result["equity"] == 300000
        assert result["current_ltv"] == 40.0


# --- Policy Door Tests for compare_options ---

class TestCompareOptionsPolicy:
    """Tests for policy door validation of compare_options."""

    @pytest.fixture
    def mock_tool_context(self, store_with_rates):
        """Create mock tool context."""
        from mortgage import tools
        tools.use_store(store_with_rates)
        return SimpleNamespace(
            session=SimpleNamespace(id="test-session-123")
        )

    @pytest.fixture
    def mock_tool(self):
        """Create mock tool."""
        return SimpleNamespace(name="compare_options")

    def test_blocks_unknown_state(self, mock_tool, mock_tool_context):
        """Blocks when state is unrecognized."""
        from mortgage.policy import mortgage_gate

        args = {
            "state": "ZZ",
            "property_value": 500000,
            "current_mortgage_balance": 200000,
            "credit_score": 750
        }

        result = asyncio.run(mortgage_gate(mock_tool, args, mock_tool_context))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "unknown_state"

    def test_blocks_state_without_regulations(self, mock_tool, mock_tool_context, store_with_rates):
        """Blocks when state has no regulations on file."""
        from mortgage.policy import mortgage_gate
        from mortgage import tools

        # NY is a valid state but has no regulations in our test data
        args = {
            "state": "NY",
            "property_value": 500000,
            "current_mortgage_balance": 200000,
            "credit_score": 750
        }

        result = asyncio.run(mortgage_gate(mock_tool, args, mock_tool_context))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "unknown_state"
        assert "No regulations on file" in result["reason"]

    def test_blocks_invalid_credit_score(self, mock_tool, mock_tool_context):
        """Blocks when credit score is out of range."""
        from mortgage.policy import mortgage_gate

        args = {
            "state": "TX",
            "property_value": 500000,
            "current_mortgage_balance": 200000,
            "credit_score": 900  # Invalid
        }

        result = asyncio.run(mortgage_gate(mock_tool, args, mock_tool_context))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "missing_inputs"

    def test_blocks_zero_property_value(self, mock_tool, mock_tool_context):
        """Blocks when property value is zero or negative."""
        from mortgage.policy import mortgage_gate

        args = {
            "state": "TX",
            "property_value": 0,
            "current_mortgage_balance": 200000,
            "credit_score": 750
        }

        result = asyncio.run(mortgage_gate(mock_tool, args, mock_tool_context))
        assert result is not None
        assert result["result"] == "blocked"
        assert result["code"] == "missing_inputs"

    def test_allows_valid_request(self, mock_tool, mock_tool_context):
        """Allows valid compare_options request."""
        from mortgage.policy import mortgage_gate

        args = {
            "state": "TX",
            "property_value": 500000,
            "current_mortgage_balance": 200000,
            "credit_score": 750
        }

        result = asyncio.run(mortgage_gate(mock_tool, args, mock_tool_context))
        assert result is None  # None means allowed


# --- Tool Integration Tests ---

class TestCompareOptionsTool:
    """Tests for compare_options tool function."""

    @pytest.fixture
    def setup_store(self, store_with_rates):
        """Set up the store for tool tests."""
        from mortgage import tools
        tools.use_store(store_with_rates)
        yield

    def test_tool_returns_formatted_response(self, setup_store):
        """Tool returns properly formatted response."""
        from mortgage.tools import compare_options

        result = asyncio.run(compare_options(
            state="CA",
            property_value=500000,
            current_mortgage_balance=200000,
            credit_score=750
        ))

        assert result["result"] == "ok"
        assert "options" in result
        assert "disclaimer" in result
        assert "say" in result

    def test_tool_formats_currency(self, setup_store):
        """Tool formats currency values."""
        from mortgage.tools import compare_options

        result = asyncio.run(compare_options(
            state="CA",
            property_value=500000,
            current_mortgage_balance=200000,
            credit_score=750
        ))

        assert "$" in result["property_value"]
        assert "$" in result["equity"]

    def test_tool_formats_rates(self, setup_store):
        """Tool formats rate percentages."""
        from mortgage.tools import compare_options

        result = asyncio.run(compare_options(
            state="CA",
            property_value=500000,
            current_mortgage_balance=200000,
            credit_score=750
        ))

        for opt in result["options"]:
            assert "%" in opt["rate"]

    def test_tool_handles_error(self, setup_store):
        """Tool handles MortgageError gracefully."""
        from mortgage.tools import compare_options

        result = asyncio.run(compare_options(
            state="ZZ",  # Invalid
            property_value=500000,
            current_mortgage_balance=200000,
            credit_score=750
        ))

        assert result["result"] == "error"
        assert result["code"] == "unknown_state"
