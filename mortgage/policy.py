"""Mac's policy door — every tool call walks through here.

Mortgage lending compliance rules enforced in CODE, not in the prompt:

  1. LOCKOUT     — 3 failed identity checks lock the call; only a human can help.
  2. DISCLAIMER  — Rate estimates must include disclaimer language.
  3. TRANSFER    — Actual loan applications must go to a licensed loan officer.
  4. COMPLIANCE  — State-specific rules are enforced (e.g., Texas HELOC restrictions).

Same three verbs as the front desk: return None = allow (observe), return a dict = BLOCK
(ADK skips the tool and hands the dict to the model), or edit `args` in place = rewrite.

`audit` (the after_tool_callback) streams each result to the caller's browser.
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
SUGGEST_TRANSFER = {"check_heloc_eligibility", "get_rate_estimate", "calculate_equity"}

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

    # Log allowed action
    events.publish(call_id, {"type": "action", "tool": name, "args": dict(args), "status": "allowed"})
    log.info("ALLOW %s %s", name, _redact(args))
    return None  # allow the tool to run


async def audit(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext,
                tool_response: Any) -> Optional[dict]:
    """Post-tool callback — runs after every tool completes."""
    resp = tool_response if isinstance(tool_response, dict) else {"result": str(tool_response)}

    # Publish result to browser
    events.publish(tool_context.session.id, {"type": "tool_result", "tool": tool.name, "response": resp})

    # Log result (redacted)
    result_summary = resp.get("result", "unknown")
    code = resp.get("code", "")
    log.info("RESULT %s -> %s%s", tool.name, result_summary, f" ({code})" if code else "")

    # Add compliance reminders based on tool results
    if tool.name == "get_rate_estimate":
        # Ensure disclaimer is included
        if "disclaimer" not in resp:
            resp["disclaimer"] = tools.store.info["disclaimers"]["rates"]

    if tool.name == "check_heloc_eligibility" and resp.get("eligible") is False:
        # Add transfer suggestion for ineligible cases
        if "say" not in resp:
            resp["say"] = "Explain the restrictions and offer to connect with a loan officer to discuss alternatives."

    return None  # don't modify what the model sees
