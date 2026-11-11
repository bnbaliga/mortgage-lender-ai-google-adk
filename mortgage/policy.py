"""Mac's policy door — every tool call walks through here.

Mortgage lending compliance rules enforced in CODE, not in the prompt:

  BEFORE tool execution (mortgage_gate):
    1. LOCKOUT        — 3 failed identity checks lock the call; only a human can help.
    2. INPUT_VALIDATION — get_rate_estimate blocked if credit_score not in 300-850 or
                         loan_to_value not in 1-100; ask caller for valid inputs.

  AFTER tool execution (audit):
    3. DISCLAIMER     — get_rate_estimate results always include the rates disclaimer.
    4. TRANSFER_HINT  — Tools in SUGGEST_TRANSFER that return ineligible or needs_review
                        get a "say" suggesting loan officer transfer.

Same three verbs as the front desk: return None = allow (observe), return a dict = BLOCK
(ADK skips the tool and hands the dict to the model), or edit `args` in place = rewrite.

`audit` (the after_tool_callback) streams each result to the caller's browser.
When audit returns a dict, ADK uses that dict instead of the original tool response.
"""
import asyncio
import logging
from typing import Any, Optional

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from mortgage import events, tools

log = logging.getLogger("mortgage.policy")

# Tools that need verified customer
NEEDS_VERIFIED = set()  # Currently no tools strictly require verification

# Tools that should suggest loan officer transfer for complex cases
SUGGEST_TRANSFER = {"check_heloc_eligibility", "get_rate_estimate", "calculate_equity", "compare_options"}

# Identity tools
IDENTITY_TOOLS = {"verify_customer", "register_lead"}

# Sensitive info that should be redacted in logs
_SENSITIVE_ARGS = {"full_name", "phone", "phone_last_4", "email"}


def _redact(args: dict) -> dict:
    return {k: ("***" if k in _SENSITIVE_ARGS else v) for k, v in args.items()}


def _block(call_id: str, tool: str, args: dict, code: str, reason: str, say: str) -> dict:
    events.publish(call_id, {"type": "action", "tool": tool, "args": dict(args),
                             "status": "blocked", "reason": reason})
    log.info("BLOCK %s %s — %s", tool, _redact(args), code)
    return {"result": "blocked", "code": code, "reason": reason, "say": say}


async def mortgage_gate(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext) -> Optional[dict]:
    """Pre-tool policy check — runs before every tool call."""
    call_id = tool_context.session.id
    call = tools.store.call(call_id)
    name = tool.name

    # Check lockout status
    if call.locked and name in IDENTITY_TOOLS:
        return _block(call_id, name, args, "locked", "verification locked after 3 attempts",
                      "For their security, you can't verify on this call. Offer to transfer them to a loan officer.")

    # For tools that need verification, wait a beat for concurrent verify_customer
    if name in NEEDS_VERIFIED and not call.verified_customer_id:
        for _ in range(25):
            await asyncio.sleep(0.01)
            if call.verified_customer_id:
                break
        if not call.verified_customer_id:
            return _block(call_id, name, args, "not_verified", "customer not verified",
                          "Ask for their name and the last 4 digits of their phone number to verify their account.")

    # Validate inputs for get_rate_estimate
    if name == "get_rate_estimate":
        credit_score = args.get("credit_score", 720)
        loan_to_value = args.get("loan_to_value", 80)

        if not (300 <= credit_score <= 850):
            return _block(call_id, name, args, "missing_inputs",
                          f"credit_score {credit_score} is outside valid range 300-850",
                          "Ask the caller for their approximate credit score. It should be between 300 and 850.")

        if not (1 <= loan_to_value <= 100):
            return _block(call_id, name, args, "missing_inputs",
                          f"loan_to_value {loan_to_value} is outside valid range 1-100",
                          "Ask the caller what percentage of the home's value they want to borrow. It should be between 1% and 100%.")

    # Validate inputs for compare_options
    if name == "compare_options":
        from mortgage.mortgage import normalize_state, MortgageError, VALID_STATE_CODES

        state = args.get("state", "")
        property_value = args.get("property_value", 0)
        credit_score = args.get("credit_score", 720)

        # Validate state - must be recognizable
        try:
            state_code = normalize_state(state)
            # Check if we have regulations on file for this state
            state_rules = tools.store.state_regulations.get(state_code)
            if not state_rules:
                return _block(call_id, name, args, "unknown_state",
                              f"No regulations on file for state {state_code}",
                              "Let them know you can't compare options for that state yet and offer to connect them with a loan officer who can help.")
        except MortgageError:
            return _block(call_id, name, args, "unknown_state",
                          f"Unrecognized state: {state}",
                          "Ask the caller which state the property is located in.")

        # Validate property value
        if property_value <= 0:
            return _block(call_id, name, args, "missing_inputs",
                          "property_value must be positive",
                          "Ask the caller for the estimated value of their property.")

        # Validate credit score
        if not (300 <= credit_score <= 850):
            return _block(call_id, name, args, "missing_inputs",
                          f"credit_score {credit_score} is outside valid range 300-850",
                          "Ask the caller for their approximate credit score. It should be between 300 and 850.")

    # Log allowed action
    events.publish(call_id, {"type": "action", "tool": name, "args": dict(args), "status": "allowed"})
    log.info("ALLOW %s %s", name, _redact(args))
    return None  # allow the tool to run


async def audit(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext,
                tool_response: Any) -> Optional[dict]:
    """Post-tool callback — runs after every tool completes.

    Returns a NEW dict (not mutating the original) when compliance additions are needed.
    ADK will use the returned dict instead of the original tool_response.
    """
    resp = tool_response if isinstance(tool_response, dict) else {"result": str(tool_response)}

    # Publish result to browser
    events.publish(tool_context.session.id, {"type": "tool_result", "tool": tool.name, "response": resp})

    # Log result (redacted)
    result_summary = resp.get("result", "unknown")
    code = resp.get("code", "")
    log.info("RESULT %s -> %s%s", tool.name, result_summary, f" ({code})" if code else "")

    # Track if we need to return a modified response
    modified = None

    # Ensure get_rate_estimate always includes disclaimer
    if tool.name == "get_rate_estimate" and "disclaimer" not in resp:
        modified = {**resp, "disclaimer": tools.store.info["disclaimers"]["rates"]}

    # For tools in SUGGEST_TRANSFER that return ineligible or needs_review, add loan officer hint
    if tool.name in SUGGEST_TRANSFER:
        is_ineligible = resp.get("eligible") is False
        is_needs_review = resp.get("status") == "needs_review"

        if (is_ineligible or is_needs_review) and "say" not in resp:
            base = modified if modified else resp
            modified = {
                **base,
                "say": "Explain the situation and offer to connect with a licensed loan officer who can discuss alternatives and next steps."
            }

    # Return the modified dict if we made changes, otherwise None (use original)
    return modified
