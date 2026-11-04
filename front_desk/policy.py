"""The front desk's policy door — every tool call walks through here.

EP3 taught the shape with one toy rule (quiet hours). A clinic needs real ones,
and they live in CODE, not in the prompt, because a prompt can be argued with:

  1. LOCKOUT     — 3 failed identity checks lock the call; only a human can help.
  2. IDENTITY    — anything that reads or changes a patient's record needs a
                   verified caller (set server-side by verify_patient).
  3. CONFIRM     — book / cancel / reschedule need `caller_confirmed=true`, i.e.
                   the agent read the details back and heard a yes.
  4. EMERGENCY   — once an emergency is flagged, no routine scheduling on this
                   call; the only job left is "call 112 / 108" (clinic.json).
  5. CONTROLLED  — controlled-substance refills can't be requested by phone.

Same three verbs as EP3: return None = allow (observe), return a dict = BLOCK
(ADK skips the tool and hands the dict to the model, which explains it to the
caller), or edit `args` in place = rewrite.

`audit` (the after_tool_callback) streams each result to the caller's browser.
Server logs get a REDACTED copy — names, dates of birth and phone numbers never
hit stdout.
"""
import asyncio
import logging
import re
from typing import Any, Optional

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from front_desk import events, tools

log = logging.getLogger("front-desk.policy")

NEEDS_VERIFIED = {"list_my_appointments", "book_appointment", "cancel_appointment",
                  "reschedule_appointment", "request_prescription_refill"}
NEEDS_CONFIRMATION = {"book_appointment", "cancel_appointment", "reschedule_appointment"}
BLOCKED_IN_EMERGENCY = {"book_appointment", "reschedule_appointment"}
IDENTITY_TOOLS = {"verify_patient", "register_new_patient"}

# Schedule II–IV examples. A real deployment pulls this from a drug database.
CONTROLLED = re.compile(
    r"oxycodone|oxycontin|percocet|hydrocodone|vicodin|norco|morphine|fentanyl|codeine|tramadol|"
    r"methadone|buprenorphine|suboxone|adderall|amphetamine|ritalin|methylphenidate|vyvanse|"
    r"xanax|alprazolam|valium|diazepam|klonopin|clonazepam|ativan|lorazepam|ambien|zolpidem|"
    r"phenobarbital|ketamine|testosterone|modafinil|provigil|lyrica|pregabalin",
    re.I)

_PHI_ARGS = {"full_name", "date_of_birth", "phone"}


def _redact(args: dict) -> dict:
    return {k: ("***" if k in _PHI_ARGS else v) for k, v in args.items()}


def _block(call_id: str, tool: str, args: dict, code: str, reason: str, say: str) -> dict:
    events.publish(call_id, {"type": "action", "tool": tool, "args": dict(args),
                             "status": "blocked", "reason": reason})
    log.info("BLOCK %s %s — %s", tool, _redact(args), code)
    # Returning a dict SKIPS the tool; this dict is what the model sees.
    return {"result": "blocked", "code": code, "reason": reason, "say": say}


async def front_desk_gate(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext) -> Optional[dict]:
    call_id = tool_context.session.id
    call = tools.store.call(call_id)
    name = tool.name

    if call.locked and (name in IDENTITY_TOOLS or name in NEEDS_VERIFIED):
        return _block(call_id, name, args, "locked", "identity check locked after 3 attempts",
                      "For their privacy you can't try again on this call. Offer to transfer them to staff.")

    if name in NEEDS_VERIFIED and not call.verified_patient_id:
        # The model often fires verify_patient AND the request in the same turn, and ADK
        # runs a turn's calls concurrently — give a sibling identity check a beat to land.
        for _ in range(25):
            await asyncio.sleep(0.01)
            if call.verified_patient_id:
                break
    if name in NEEDS_VERIFIED and not call.verified_patient_id:
        return _block(call_id, name, args, "not_verified", "caller not verified",
                      "Ask for the patient's full name and date of birth and call verify_patient first. "
                      "If they're new here, use register_new_patient.")

    if name in BLOCKED_IN_EMERGENCY and call.emergency:
        return _block(call_id, name, args, "emergency", "emergency flagged on this call",
                      f"Don't schedule anything. Tell them again to {tools.store.emergency_advice()}")

    if name in NEEDS_CONFIRMATION and args.get("caller_confirmed") is not True:
        return _block(call_id, name, args, "needs_confirmation", "caller hasn't confirmed",
                      "Read the details back (provider, day, time) and ask 'shall I go ahead?'. "
                      "Call again with caller_confirmed=true only after they say yes.")

    if name == "request_prescription_refill" and CONTROLLED.search(str(args.get("medication", ""))):
        return _block(call_id, name, args, "controlled_substance", "controlled medication",
                      "Refills for that medication need a visit with their provider. "
                      "Offer to book a follow_up visit instead.")

    events.publish(call_id, {"type": "action", "tool": name, "args": dict(args), "status": "allowed"})
    log.info("ALLOW %s %s", name, _redact(args))
    return None   # observe only — the tool runs


async def audit(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext,
                tool_response: Any) -> Optional[dict]:
    resp = tool_response if isinstance(tool_response, dict) else {"result": str(tool_response)}
    events.publish(tool_context.session.id, {"type": "tool_result", "tool": tool.name, "response": resp})
    log.info("RESULT %s -> %s%s", tool.name, resp.get("result"),
             f" ({resp['code']})" if resp.get("code") else "")
    return None   # don't change what the model sees
