"""HomeFirst AI Lending's back office — products, customers, properties, loan officers.

Plain Python, no ADK and no network: the voice agent's tools are thin wrappers
over `MortgageStore`, so this is the file you swap for a real LOS integration
(Encompass, Blend, etc.) — keep the method names, change the insides.

Everything is in memory and seeded from `assets/mortgage/mortgage.json`.
Restarting the server resets the data.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

SEED_PATH = Path(__file__).resolve().parents[1] / "assets" / "mortgage" / "mortgage.json"
RATE_SHEET_PATH = Path(__file__).resolve().parents[1] / "assets" / "mortgage" / "rate_sheet.json"

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_VERIFY_ATTEMPTS = 3


class MortgageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------- records ---

@dataclass
class LoanOfficer:
    id: str
    name: str
    title: str
    nmls_id: str
    specialties: list[str]
    languages: list[str]
    available: list[str]
    hours: tuple[time, time] = None  # (start_time, end_time) in the officer's timezone
    timezone: str = "America/Chicago"  # Default to Central Time


@dataclass
class Property:
    id: str
    address: str
    property_type: str  # primary, secondary, investment
    estimated_value: float
    mortgage_balance: float
    mortgage_type: str  # conventional, cash_out_refi, fha, va, etc.
    equity: float


@dataclass
class Customer:
    id: str
    first_name: str
    last_name: str
    phone: str
    email: str
    properties: list[Property]
    credit_score_range: str
    notes: str = ""


@dataclass
class MortgageProduct:
    id: str
    name: str
    description: str
    rate_range: tuple[float, float]
    min_credit_score: int
    max_ltv: float
    property_types: list[str]
    term_years: int = 30
    notes: str = ""


@dataclass
class Lead:
    """A potential customer inquiry — tracked for follow-up."""
    id: str
    name: str
    phone: str
    inquiry_type: str  # heloc, purchase, refinance, etc.
    property_state: str
    summary: str
    created: datetime
    assigned_officer: Optional[str] = None


@dataclass
class TransferRequest:
    """Request to transfer caller to a licensed loan officer."""
    id: str
    reason: str
    customer_id: Optional[str]
    created: datetime
    details: dict = field(default_factory=dict)


@dataclass
class CallState:
    """What we know about ONE live call. Lives server-side, keyed by
    the ADK session id — never in the prompt."""
    verified_customer_id: Optional[str] = None
    failed_verifications: int = 0
    inquiry_context: dict = field(default_factory=dict)  # track conversation context

    @property
    def locked(self) -> bool:
        return self.failed_verifications >= MAX_VERIFY_ATTEMPTS and not self.verified_customer_id


# ---------------------------------------------------------------- helpers ---

def normalize_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z\s]", "", (s or "").lower().replace("-", " "))).strip()


# Full 50-state + DC name -> 2-letter code map
STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# Set of valid 2-letter state codes
VALID_STATE_CODES = set(STATE_NAME_TO_CODE.values())


def normalize_state(state: str) -> str:
    """Normalize state name or code to 2-letter uppercase code.

    Accepts state names ('Texas', 'texas', 'TEXAS') and codes ('TX', 'tx').
    Returns the 2-letter uppercase state code (e.g., 'TX').
    Raises MortgageError if state cannot be recognized.
    """
    if not state:
        raise MortgageError("unknown_state", "State is required")

    state_clean = state.strip()
    state_upper = state_clean.upper()

    # Check if it's already a valid 2-letter code
    if len(state_upper) == 2 and state_upper in VALID_STATE_CODES:
        return state_upper

    # Try to match by full name
    state_lower = state_clean.lower()
    if state_lower in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[state_lower]

    raise MortgageError("unknown_state", f"Unrecognized state: {state}")


# Property use mapping: free-text -> normalized value
PROPERTY_USE_MAPPINGS = {
    # Investment property terms
    "rental": "investment",
    "rent out": "investment",
    "rented": "investment",
    "investment property": "investment",
    "income property": "investment",
    "landlord": "investment",
    "investment": "investment",
    # Primary residence terms
    "primary residence": "primary",
    "homestead": "primary",
    "my home": "primary",
    "owner occupied": "primary",
    "primary": "primary",
    # Second home terms
    "vacation": "second_home",
    "second home": "second_home",
    "second_home": "second_home",
    "secondary": "second_home",
}


def normalize_property_use(property_use: str) -> str:
    """Normalize free-text property use to one of: primary, second_home, investment.

    Accepts various caller phrasings and maps them to the canonical values.
    Raises MortgageError if property use cannot be recognized.
    """
    if not property_use:
        raise MortgageError("unknown_property_use", "Property use is required")

    use_clean = property_use.strip().lower()

    # Try direct mapping
    if use_clean in PROPERTY_USE_MAPPINGS:
        return PROPERTY_USE_MAPPINGS[use_clean]

    # Try partial match (for phrases containing keywords)
    for key, value in PROPERTY_USE_MAPPINGS.items():
        if key in use_clean or use_clean in key:
            return value

    raise MortgageError("unknown_property_use", f"Unknown property use: {property_use}")


def spoken_time(t: time) -> str:
    return datetime.combine(date.min, t).strftime("%I:%M %p").lstrip("0")


def _hhmm(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


def format_currency(amount: float) -> str:
    """Format as spoken currency."""
    if amount >= 1_000_000:
        return f"${amount/1_000_000:.1f} million"
    if amount >= 1_000:
        return f"${amount/1_000:.0f} thousand"
    return f"${amount:.0f}"


def format_rate(rate: float) -> str:
    """Format rate for speaking."""
    return f"{rate:.2f}%"


# ------------------------------------------------------------------ store ---

class MortgageStore:
    def __init__(self, seed: dict, clock: Callable[[], datetime] = datetime.now,
                 rate_sheet: dict | None = None):
        self.clock = clock
        c = seed["company"]
        self.info = c
        self.hours = {k: (tuple(map(_hhmm, v)) if v else None) for k, v in c["hours"].items()}

        # Products
        self.products: dict[str, dict] = seed["products"]

        # Rate sheet for pricing
        self.rate_sheet = rate_sheet or {}

        # State regulations
        self.state_regulations: dict[str, dict] = seed.get("state_regulations", {})

        # Loan officers
        self.loan_officers = {}
        for lo in seed["loan_officers"]:
            hours_raw = lo.get("hours")
            hours = tuple(map(_hhmm, hours_raw)) if hours_raw else None
            self.loan_officers[lo["id"]] = LoanOfficer(
                id=lo["id"],
                name=lo["name"],
                title=lo["title"],
                nmls_id=lo["nmls_id"],
                specialties=lo["specialties"],
                languages=lo["languages"],
                available=lo["available"],
                hours=hours,
                timezone=lo.get("timezone", "America/Chicago")
            )

        # FAQ
        self.faq = seed.get("faq", {})

        # Customers and properties
        self.customers: dict[str, Customer] = {}
        for raw in seed.get("customers", []):
            props = [Property(
                p["id"], p["address"], p["type"], p["estimated_value"],
                p["mortgage_balance"], p.get("mortgage_type", ""), p["equity"]
            ) for p in raw.get("properties", [])]
            cust = Customer(
                raw["id"], raw["first_name"], raw["last_name"],
                raw.get("phone", ""), raw.get("email", ""),
                props, raw.get("credit_score_range", ""),
                raw.get("notes", "")
            )
            self.customers[cust.id] = cust

        # Runtime state
        self.leads: list[Lead] = []
        self.transfers: list[TransferRequest] = []
        self.calls: dict[str, CallState] = {}

    @classmethod
    def load(cls, path: Path = SEED_PATH, clock: Callable[[], datetime] = datetime.now,
             rate_sheet_path: Path = RATE_SHEET_PATH) -> "MortgageStore":
        seed = json.loads(path.read_text(encoding="utf-8"))
        rate_sheet = None
        if rate_sheet_path.exists():
            rate_sheet = json.loads(rate_sheet_path.read_text(encoding="utf-8"))
        return cls(seed, clock, rate_sheet)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex[:6].upper()}"

    # ---- calls (per live session) -------------------------------------
    def call(self, call_id: str) -> CallState:
        return self.calls.setdefault(call_id, CallState())

    def end_call(self, call_id: str) -> None:
        self.calls.pop(call_id, None)

    # ---- lookups --------------------------------------------------------
    def today(self) -> date:
        return self.clock().date()

    def get_credit_tier(self, credit_score: int) -> str | None:
        """Get the credit tier name for a given score."""
        if not self.rate_sheet or "credit_tiers" not in self.rate_sheet:
            return None
        for tier_name, bounds in self.rate_sheet["credit_tiers"].items():
            if bounds["min"] <= credit_score <= bounds["max"]:
                return tier_name
        return None

    def get_ltv_band(self, ltv: float) -> str | None:
        """Get the LTV band name for a given LTV percentage."""
        if not self.rate_sheet or "ltv_bands" not in self.rate_sheet:
            return None
        for band_name, bounds in self.rate_sheet["ltv_bands"].items():
            if bounds["min"] <= ltv <= bounds["max"]:
                return band_name
        return None

    def get_rate_from_sheet(self, product_key: str, credit_score: int, ltv: float) -> dict | None:
        """Look up rate from the rate sheet.

        Returns dict with rate, product_name, credit_tier, ltv_band, benchmark, or None if unavailable.
        """
        if not self.rate_sheet or "products" not in self.rate_sheet:
            return None

        key = product_key.lower().replace(" ", "_").replace("-", "_")
        product = self.rate_sheet["products"].get(key)
        if not product:
            return None

        tier = self.get_credit_tier(credit_score)
        band = self.get_ltv_band(ltv)
        if not tier or not band:
            return None

        rates = product.get("rates", {})
        tier_rates = rates.get(tier)
        if not tier_rates:
            return None

        rate = tier_rates.get(band)
        if rate is None:
            return None

        return {
            "rate": rate,
            "product_name": product["name"],
            "credit_tier": tier,
            "ltv_band": band,
            "term_years": product.get("term_years"),
            "benchmark": product.get("market_benchmark"),
            "as_of": self.rate_sheet.get("as_of")
        }

    def get_state_rules(self, state: str, product_type: str = "heloc") -> tuple[str, Optional[dict]]:
        """Get state-specific lending rules.

        Returns:
            Tuple of (normalized_state_code, rules_dict or None)
        """
        state_code = normalize_state(state)

        state_info = self.state_regulations.get(state_code)
        if not state_info:
            return state_code, None

        if product_type == "heloc":
            return state_code, state_info.get("heloc_rules")
        elif product_type == "cashout":
            return state_code, state_info.get("cashout_rules")
        return state_code, state_info

    def check_heloc_eligibility(self, state: str, property_type: str,
                                 has_existing_cashout: bool = False,
                                 property_value: float = 0,
                                 current_balance: float = 0) -> dict:
        """Check if HELOC is allowed given the parameters."""
        # Normalize property use at the top - raises MortgageError if unknown
        normalized_property_type = normalize_property_use(property_type)

        # Get state rules with normalized state code
        state_code, rules = self.get_state_rules(state, "heloc")

        # If no rules on file for this state, return needs_review
        if rules is None:
            return {
                "result": "ok",
                "state": state_code,
                "property_type": normalized_property_type,
                "eligible": None,
                "status": "needs_review",
                "say": "Explain you can't confirm eligibility for that state on this call and offer to connect them with a licensed loan officer."
            }

        result = {
            "state": state_code,
            "property_type": normalized_property_type,
            "eligible": True,
            "issues": [],
            "alternatives": []
        }

        # Check investment property restriction
        if normalized_property_type == "investment" and not rules.get("allowed_on_investment", True):
            result["eligible"] = False
            result["issues"].append(f"{state_code} does not allow HELOCs on investment properties")
            result["alternatives"].append("Consider a cash-out refinance on the investment property")
            result["alternatives"].append("Use a HELOC on your primary residence instead")

        # Check existing cash-out restriction
        if has_existing_cashout and not rules.get("allowed_with_existing_cashout", True):
            result["eligible"] = False
            result["issues"].append(f"{state_code} does not allow HELOCs when there's an existing cash-out mortgage")
            result["alternatives"].append("Refinance the existing mortgage first, then add a HELOC")
            result["alternatives"].append("Do another cash-out refinance instead")

        # Check LTV if values provided
        if property_value > 0 and current_balance > 0:
            max_cltv = rules.get("max_cltv", 80)
            current_ltv = (current_balance / property_value) * 100
            max_heloc = (property_value * max_cltv / 100) - current_balance
            result["current_ltv"] = round(current_ltv, 1)
            result["max_cltv"] = max_cltv
            result["max_heloc_amount"] = max(0, round(max_heloc, 0))

            if current_ltv >= max_cltv:
                result["eligible"] = False
                result["issues"].append(f"Current LTV ({current_ltv:.1f}%) already at or above maximum ({max_cltv}%)")

        # Add note about closing notice and rescission periods
        closing_notice = rules.get("closing_notice_days")
        rescission = rules.get("rescission_days")
        if closing_notice and rescission:
            result["note"] = f"{state_code} requires a {closing_notice}-day notice period before closing, plus a {rescission}-day right to cancel after closing."
        elif closing_notice:
            result["note"] = f"{state_code} requires a {closing_notice}-day notice period before closing."

        return result

    def get_product_info(self, product_key: str) -> Optional[dict]:
        """Get info about a mortgage product."""
        key = product_key.lower().replace(" ", "_").replace("-", "_")
        return self.products.get(key)

    def get_rate_estimate(self, product_key: str, credit_score: int = 720,
                          ltv: float = 80) -> dict:
        """Get estimated rate for a product based on credit and LTV.

        Uses rate sheet if available, otherwise falls back to product rate_range.
        """
        # Try rate sheet first
        sheet_rate = self.get_rate_from_sheet(product_key, credit_score, ltv)
        if sheet_rate and sheet_rate["rate"] is not None:
            result = {
                "product": sheet_rate["product_name"],
                "estimated_rate": sheet_rate["rate"],
                "credit_tier": sheet_rate["credit_tier"],
                "ltv_band": sheet_rate["ltv_band"],
                "disclaimer": self.info["disclaimers"]["rates"]
            }
            # Add benchmark comparison if available
            if sheet_rate["benchmark"]:
                benchmark = sheet_rate["benchmark"]
                result["market_benchmark"] = benchmark["rate"]
                result["benchmark_source"] = benchmark["source"]
                result["benchmark_as_of"] = benchmark.get("as_of")
            if sheet_rate["as_of"]:
                result["rates_as_of"] = sheet_rate["as_of"]
            return result

        # Fall back to product rate_range with adjustments
        product = self.get_product_info(product_key)
        if not product:
            raise MortgageError("unknown_product", f"Unknown product: {product_key}")

        base_low, base_high = product["rate_range"]

        # Adjust based on credit score (simplified)
        if credit_score >= 760:
            rate_adj = -0.25
        elif credit_score >= 720:
            rate_adj = 0
        elif credit_score >= 680:
            rate_adj = 0.25
        elif credit_score >= 640:
            rate_adj = 0.5
        else:
            rate_adj = 0.75

        # Adjust based on LTV
        if ltv > 90:
            rate_adj += 0.25
        elif ltv > 80:
            rate_adj += 0.125

        estimated_rate = base_low + rate_adj

        return {
            "product": product["name"],
            "estimated_rate": round(estimated_rate, 3),
            "rate_range": f"{format_rate(base_low)} to {format_rate(base_high)}",
            "disclaimer": self.info["disclaimers"]["rates"]
        }

    @staticmethod
    def calculate_monthly_payment(principal: float, annual_rate: float, term_years: int) -> float:
        """Calculate monthly payment for a fixed-rate loan.

        Uses standard amortization formula: M = P * [r(1+r)^n] / [(1+r)^n - 1]
        """
        if annual_rate <= 0 or principal <= 0 or term_years <= 0:
            return 0.0
        monthly_rate = annual_rate / 100 / 12
        num_payments = term_years * 12
        if monthly_rate == 0:
            return principal / num_payments
        payment = principal * (monthly_rate * (1 + monthly_rate) ** num_payments) / \
                  ((1 + monthly_rate) ** num_payments - 1)
        return round(payment, 2)

    def compare_options(self, property_value: float, current_balance: float,
                        credit_score: int, state: str,
                        cash_needed: float = 0) -> dict:
        """Compare eligible mortgage options for a property.

        Args:
            property_value: Current property value in dollars.
            current_balance: Current mortgage balance in dollars.
            credit_score: Borrower's credit score.
            state: State where property is located.
            cash_needed: Amount of cash the borrower wants to access (for HELOC/cash-out).

        Returns:
            Dict with eligible options, each including rate, monthly payment, and potential savings.
        """
        # Normalize state - raises if unknown
        state_code = normalize_state(state)

        # Check HELOC eligibility for this state
        state_rules = self.state_regulations.get(state_code, {})
        heloc_rules = state_rules.get("heloc_rules") if state_rules else None

        equity = property_value - current_balance
        current_ltv = (current_balance / property_value * 100) if property_value > 0 else 100

        options = []
        errors = []

        # --- HELOC Option ---
        heloc_eligible = True
        heloc_issues = []

        if heloc_rules:
            max_cltv = heloc_rules.get("max_cltv", 80)
            if current_ltv >= max_cltv:
                heloc_eligible = False
                heloc_issues.append(f"Current LTV {current_ltv:.1f}% exceeds max {max_cltv}%")
            else:
                max_heloc = (property_value * max_cltv / 100) - current_balance
                if cash_needed > max_heloc:
                    heloc_eligible = False
                    heloc_issues.append(f"Cash needed ${cash_needed:,.0f} exceeds max HELOC ${max_heloc:,.0f}")
        else:
            # No rules on file - cannot confirm eligibility
            heloc_eligible = False
            heloc_issues.append("State regulations not on file")

        if heloc_eligible:
            heloc_amount = cash_needed if cash_needed > 0 else min(50000, equity * 0.5)
            heloc_ltv = ((current_balance + heloc_amount) / property_value) * 100
            rate_info = self.get_rate_from_sheet("heloc", credit_score, heloc_ltv)
            if rate_info and rate_info["rate"]:
                # HELOC is interest-only during draw period
                monthly_interest = round(heloc_amount * rate_info["rate"] / 100 / 12, 2)
                options.append({
                    "product": "heloc",
                    "name": rate_info["product_name"],
                    "rate": rate_info["rate"],
                    "amount": round(heloc_amount, 0),
                    "monthly_payment": monthly_interest,
                    "payment_type": "interest_only_draw",
                    "note": "Payment shown is interest-only during 10-year draw period",
                    "credit_tier": rate_info["credit_tier"],
                    "benchmark": rate_info.get("benchmark", {}).get("rate")
                })

        # --- Cash-Out Refinance Option ---
        cashout_amount = current_balance + (cash_needed if cash_needed > 0 else 50000)
        cashout_ltv = (cashout_amount / property_value) * 100
        # Cash-out usually limited to 80% LTV
        if cashout_ltv <= 80:
            rate_info = self.get_rate_from_sheet("cash_out_refi", credit_score, cashout_ltv)
            if rate_info and rate_info["rate"]:
                monthly = self.calculate_monthly_payment(cashout_amount, rate_info["rate"], 30)
                options.append({
                    "product": "cash_out_refi",
                    "name": rate_info["product_name"],
                    "rate": rate_info["rate"],
                    "amount": round(cashout_amount, 0),
                    "monthly_payment": monthly,
                    "payment_type": "principal_and_interest",
                    "term_years": 30,
                    "cash_at_closing": round(cash_needed if cash_needed > 0 else 50000, 0),
                    "credit_tier": rate_info["credit_tier"],
                    "benchmark": rate_info.get("benchmark", {}).get("rate")
                })

        # --- Rate-and-Term Refinance Option (if they just want lower rate) ---
        if current_balance > 0 and cash_needed == 0:
            refi_ltv = current_ltv
            rate_info = self.get_rate_from_sheet("rate_term_refi", credit_score, refi_ltv)
            if rate_info and rate_info["rate"]:
                monthly = self.calculate_monthly_payment(current_balance, rate_info["rate"], 30)
                options.append({
                    "product": "rate_term_refi",
                    "name": rate_info["product_name"],
                    "rate": rate_info["rate"],
                    "amount": round(current_balance, 0),
                    "monthly_payment": monthly,
                    "payment_type": "principal_and_interest",
                    "term_years": 30,
                    "credit_tier": rate_info["credit_tier"],
                    "benchmark": rate_info.get("benchmark", {}).get("rate")
                })

        # Calculate potential savings vs market benchmarks
        for opt in options:
            if opt.get("benchmark") and opt["rate"] < opt["benchmark"]:
                savings_rate = opt["benchmark"] - opt["rate"]
                opt["savings_vs_market"] = f"{savings_rate:.3f}% below market"

        return {
            "state": state_code,
            "property_value": round(property_value, 0),
            "current_balance": round(current_balance, 0),
            "equity": round(equity, 0),
            "current_ltv": round(current_ltv, 1),
            "credit_score": credit_score,
            "options": options,
            "heloc_issues": heloc_issues if not heloc_eligible else [],
            "disclaimer": self.info["disclaimers"]["rates"]
        }

    def find_customer(self, full_name: str, phone_last_4: str = "") -> Optional[Customer]:
        """Find a customer by name and optional phone digits."""
        parts = normalize_name(full_name).split()
        if len(parts) < 2:
            return None
        first, last = parts[0], parts[-1]
        for c in self.customers.values():
            if (normalize_name(c.first_name).split()[0] == first
                    and normalize_name(c.last_name).split()[-1] == last):
                if phone_last_4 and not c.phone.endswith(phone_last_4):
                    continue
                return c
        return None

    def loan_officer_by_name(self, name: str) -> Optional[LoanOfficer]:
        """Find a loan officer by name."""
        n = normalize_name(name)
        for lo in self.loan_officers.values():
            ln = normalize_name(lo.name)
            if n and (n == ln or n in ln.split() or n == lo.id):
                return lo
        return None

    def get_available_officers(self, specialty: str = "") -> list[LoanOfficer]:
        """Get loan officers available now, optionally filtered by specialty.

        Checks both the day of week and the current time in the officer's timezone.
        """
        now = self.clock()
        available = []

        for lo in self.loan_officers.values():
            # Convert current time to the officer's timezone
            try:
                tz = ZoneInfo(lo.timezone)
                officer_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
            except Exception:
                # Fallback to system time if timezone conversion fails
                officer_now = now

            # Check day of week in officer's timezone
            day = DAY_KEYS[officer_now.weekday()]
            if day not in lo.available:
                continue

            # Check hours if specified
            if lo.hours:
                officer_time = officer_now.time()
                start_time, end_time = lo.hours
                if not (start_time <= officer_time <= end_time):
                    continue

            # Check specialty if required
            if specialty:
                if specialty.lower() not in [s.lower() for s in lo.specialties]:
                    continue

            available.append(lo)

        return available

    # ---- leads and transfers -------------------------------------------
    def create_lead(self, name: str, phone: str, inquiry_type: str,
                    state: str, summary: str) -> Lead:
        """Create a new lead for follow-up."""
        lead = Lead(
            self._new_id("L"), name, phone, inquiry_type,
            state.upper(), summary, self.clock()
        )
        self.leads.append(lead)
        return lead

    def create_transfer(self, reason: str, customer_id: Optional[str] = None,
                        **details) -> TransferRequest:
        """Create a transfer request to a loan officer."""
        transfer = TransferRequest(
            self._new_id("X"), reason, customer_id, self.clock(), details
        )
        self.transfers.append(transfer)
        return transfer
