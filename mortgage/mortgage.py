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

SEED_PATH = Path(__file__).resolve().parents[1] / "assets" / "mortgage" / "mortgage.json"

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
    def __init__(self, seed: dict, clock: Callable[[], datetime] = datetime.now):
        self.clock = clock
        c = seed["company"]
        self.info = c
        self.hours = {k: (tuple(map(_hhmm, v)) if v else None) for k, v in c["hours"].items()}

        # Products
        self.products: dict[str, dict] = seed["products"]

        # State regulations
        self.state_regulations: dict[str, dict] = seed.get("state_regulations", {})

        # Loan officers
        self.loan_officers = {lo["id"]: LoanOfficer(**lo) for lo in seed["loan_officers"]}

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
    def load(cls, path: Path = SEED_PATH, clock: Callable[[], datetime] = datetime.now) -> "MortgageStore":
        return cls(json.loads(path.read_text(encoding="utf-8")), clock)

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

    def get_state_rules(self, state: str, product_type: str = "heloc") -> Optional[dict]:
        """Get state-specific lending rules."""
        state = (state or "").strip().upper()
        if len(state) != 2:
            # Try to extract state abbreviation from address
            state_map = {"texas": "TX", "california": "CA", "florida": "FL"}
            state = state_map.get(state.lower(), state)

        state_info = self.state_regulations.get(state)
        if not state_info:
            return None

        if product_type == "heloc":
            return state_info.get("heloc_rules")
        elif product_type == "cashout":
            return state_info.get("cashout_rules")
        return state_info

    def check_heloc_eligibility(self, state: str, property_type: str,
                                 has_existing_cashout: bool = False,
                                 property_value: float = 0,
                                 current_balance: float = 0) -> dict:
        """Check if HELOC is allowed given the parameters."""
        rules = self.get_state_rules(state, "heloc")

        result = {
            "state": state.upper(),
            "property_type": property_type,
            "eligible": True,
            "issues": [],
            "alternatives": []
        }

        if rules:
            # Check investment property restriction
            if property_type == "investment" and not rules.get("allowed_on_investment", True):
                result["eligible"] = False
                result["issues"].append(f"{state.upper()} does not allow HELOCs on investment properties")
                result["alternatives"].append("Consider a cash-out refinance on the investment property")
                result["alternatives"].append("Use a HELOC on your primary residence instead")

            # Check existing cash-out restriction
            if has_existing_cashout and not rules.get("allowed_with_existing_cashout", True):
                result["eligible"] = False
                result["issues"].append(f"{state.upper()} does not allow HELOCs when there's an existing cash-out mortgage")
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

            if rules.get("cooling_off_period_days"):
                result["note"] = f"{state.upper()} requires a {rules['cooling_off_period_days']}-day cooling off period"

        return result

    def get_product_info(self, product_key: str) -> Optional[dict]:
        """Get info about a mortgage product."""
        key = product_key.lower().replace(" ", "_").replace("-", "_")
        return self.products.get(key)

    def get_rate_estimate(self, product_key: str, credit_score: int = 720,
                          ltv: float = 80) -> dict:
        """Get estimated rate for a product based on credit and LTV."""
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
        """Get loan officers available now, optionally filtered by specialty."""
        day = DAY_KEYS[self.clock().weekday()]
        available = []
        for lo in self.loan_officers.values():
            if day in lo.available:
                if specialty:
                    if specialty.lower() in [s.lower() for s in lo.specialties]:
                        available.append(lo)
                else:
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
