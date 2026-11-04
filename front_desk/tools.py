"""The front desk's abilities — ADK function tools.

Same rules as the DJ's music tools (EP2/EP3), now with stakes:

  - Plain `async def`s: ADK builds the schema from the signature + docstring, and
    `tool_context` is injected (never shown to the model).
  - Every tool returns INSTANTLY — they only touch the in-memory `ClinicStore`.
    A live function call is synchronous: a slow tool is dead air on the phone.
  - Results are written for the MODEL to paraphrase out loud: short, spoken-form
    dates/times, never more than three options.
  - The tools never take a patient id. "Who is this caller" lives server-side in
    the call state (set only by verify_patient / register_new_patient), so the
    model cannot be talked into reading someone else's chart.

Who may call what is NOT decided here — that's `policy.py`, the one door every
call walks through. The checks below are defense in depth.
"""
from typing import Optional

from google.adk.tools.tool_context import ToolContext

from front_desk.clinic import ClinicError, ClinicStore, parse_date, spoken_time

store = ClinicStore.load()


def use_store(s: ClinicStore) -> None:
    """Swap the backing store (tests, or a real EHR adapter)."""
    global store
    store = s


def _err(e: ClinicError) -> dict:
    return {"result": "error", "code": e.code, "message": e.message}


def _caller(tool_context: ToolContext) -> Optional[str]:
    return store.call(tool_context.session.id).verified_patient_id


def _not_verified() -> dict:
    return {"result": "error", "code": "not_verified",
            "message": "Verify the caller's identity (full name + date of birth) first."}


# ------------------------------------------------------------- open tools ---

async def get_clinic_info(topic: str) -> dict:
    """Look up front-desk facts: hours, address, parking, phone, providers, self-pay prices, or a policy.

    Args:
        topic: one of hours, address, parking, phone, providers, self_pay, cancellation,
            what_to_bring, telehealth, refills, records, billing — or "all".
    """
    c = store.info
    hours = [f"{d.title()}: " + (f"{spoken_time(h[0])} to {spoken_time(h[1])}" if h else "closed")
             for d, h in store.hours.items()]
    providers = [f"{p.name}, {p.title} — {p.specialty}" + (f" ({p.also})" if p.also else "")
                 + f"; in the office {', '.join(d.title() for d in p.days)}"
                 for p in store.providers.values()]
    facts = {"hours": hours, "address": c["address"], "parking": c["parking"], "phone": c["phone"],
             "providers": providers, "self_pay": c["self_pay"], **c["policies"]}
    key = (topic or "all").strip().lower().replace(" ", "_")
    if key in facts:
        return {"result": "ok", key: facts[key]}
    return {"result": "ok", **facts}


async def check_insurance(plan_name: str) -> dict:
    """Check whether the clinic accepts an insurance plan.

    Args:
        plan_name: the insurance company or plan the caller names, e.g. "blue cross".
    """
    want = (plan_name or "").lower().replace("&", "and")
    for plan in store.info["insurance_accepted"]:
        p = plan.lower()
        if want and (want in p or p in want or want.split()[0] == p.split()[0]):
            return {"result": "ok", "accepted": True, "plan": plan,
                    "note": "Coverage depends on their specific plan — suggest they confirm with their insurer."}
    return {"result": "ok", "accepted": False, "self_pay": store.info["self_pay"],
            "accepted_plans": store.info["insurance_accepted"]}


async def find_available_slots(visit_type: str, preferred_date: str = "", provider: str = "",
                               part_of_day: str = "any") -> dict:
    """Find up to three open appointment times. Call this before offering any time.

    Args:
        visit_type: one of new_patient, annual_physical, sick_visit, follow_up,
            vaccination, skin_check, telehealth.
        preferred_date: optional, YYYY-MM-DD — the earliest day the caller wants ("next week" = that
            Monday). Results start there and spread over the following days. Empty = soonest.
        provider: optional provider name the caller asked for, e.g. "Dr. Patel". Empty = anyone.
        part_of_day: morning, afternoon, evening, or any.
    """
    try:
        prov_id = store.provider_by_name(provider).id if provider.strip() else None
        day = parse_date(preferred_date) if preferred_date.strip() else None
        # "next week" / "Tuesday" means "starting then": search on-or-after, spread across days
        slots = store.find_slots(visit_type, day, prov_id, part_of_day)
        note = ""
        if day and slots and slots[0].start.date() != day:
            note = f"Nothing open on {day:%A, %B} {day.day}; these are the next openings."
        if not slots:
            return {"result": "ok", "options": [],
                    "note": "No openings in the next three weeks for that request. Try another provider, "
                            "another part of the day, or offer a transfer to staff."}
        out = {"result": "ok", "options": [store.describe_slot(s) for s in slots],
               "say": "Offer these times out loud (provider + day + time). Never read the slot_id."}
        if note:
            out["note"] = note
        return out
    except ClinicError as e:
        return _err(e)


async def verify_patient(full_name: str, date_of_birth: str, tool_context: ToolContext) -> dict:
    """Verify an existing patient's identity. Required before reading or changing their appointments.

    Args:
        full_name: the patient's first and last name, as the caller said it.
        date_of_birth: the patient's date of birth, YYYY-MM-DD.
    """
    call = store.call(tool_context.session.id)
    try:
        dob = parse_date(date_of_birth)
    except ClinicError as e:
        return _err(e)
    p = store.find_patient(full_name, dob)
    if not p:
        call.failed_verifications += 1
        left = max(0, 3 - call.failed_verifications)
        return {"result": "error", "code": "no_match", "attempts_left": left,
                "message": ("I couldn't match that name and date of birth. Ask them to repeat it, "
                            "or ask whether they're a new patient." if left else
                            "Too many attempts — offer to transfer them to staff.")}
    call.verified_patient_id, call.failed_verifications = p.id, 0
    nxt = store.upcoming(p.id)
    return {"result": "ok", "verified": True, "first_name": p.first_name,
            "upcoming_appointments": len(nxt),
            "primary_provider": store.providers[p.primary_provider].name if p.primary_provider else "none on file"}


async def register_new_patient(full_name: str, date_of_birth: str, phone: str,
                               tool_context: ToolContext, insurance: str = "") -> dict:
    """Register a brand-new patient so they can book. Only after they say they've never been seen here.

    Args:
        full_name: first and last name.
        date_of_birth: YYYY-MM-DD.
        phone: a callback phone number.
        insurance: optional insurance plan name, or "self-pay".
    """
    try:
        p = store.register(full_name, parse_date(date_of_birth), phone, insurance)
    except ClinicError as e:
        return _err(e)
    store.call(tool_context.session.id).verified_patient_id = p.id
    return {"result": "ok", "registered": True, "first_name": p.first_name,
            "next_step": "Book their new_patient visit (40 minutes). Remind them to arrive 15 minutes early."}


async def transfer_to_staff(reason: str, tool_context: ToolContext) -> dict:
    """Hand the caller to a human at the front desk (they asked for a person, or it's outside what you can do).

    Args:
        reason: one sentence for the staff member — what the caller needs.
    """
    t = store.open_ticket("transfer", reason, _caller(tool_context))
    return {"result": "ok", "ticket": t.id,
            "say": "Tell them you're connecting them to a team member now, and that someone will "
                   "call back within one business hour if the line is busy."}


async def flag_emergency(summary: str, tool_context: ToolContext) -> dict:
    """Flag a possible medical emergency. Call this the moment a caller describes emergency symptoms.

    Args:
        summary: a few words on what the caller described.
    """
    store.call(tool_context.session.id).emergency = True
    t = store.open_ticket("emergency", summary, _caller(tool_context))
    return {"result": "ok", "ticket": t.id,
            "say": f"Tell them clearly to {store.emergency_advice()} "
                   "Do not book anything. Stay calm and brief."}


# --------------------------------------------- tools that need a verified caller ---

async def list_my_appointments(tool_context: ToolContext) -> dict:
    """List the verified caller's upcoming appointments."""
    pid = _caller(tool_context)
    if not pid:
        return _not_verified()
    appts = [store.describe_appt(a) for a in store.upcoming(pid)]
    return {"result": "ok", "appointments": appts,
            "say": "Describe them by provider, day and time. Never read the appointment_id."}


async def book_appointment(slot_id: str, caller_confirmed: bool, tool_context: ToolContext,
                           reason: str = "") -> dict:
    """Book an appointment in a slot returned by find_available_slots.

    Args:
        slot_id: the slot_id of the option the caller chose.
        caller_confirmed: true ONLY after you read back provider, day and time and the caller said yes.
        reason: optional — the reason for the visit in the caller's words, if they gave one.
    """
    pid = _caller(tool_context)
    if not pid:
        return _not_verified()
    try:
        a = store.book(pid, slot_id, reason)
    except ClinicError as e:
        return _err(e)
    return {"result": "ok", "booked": store.describe_appt(a),
            "say": "Confirm the day, time and provider, and mention what to bring."}


async def cancel_appointment(appointment_id: str, caller_confirmed: bool, tool_context: ToolContext) -> dict:
    """Cancel one of the verified caller's upcoming appointments.

    Args:
        appointment_id: the appointment_id from list_my_appointments.
        caller_confirmed: true ONLY after you named the appointment back and the caller said yes, cancel it.
    """
    pid = _caller(tool_context)
    if not pid:
        return _not_verified()
    try:
        a, late = store.cancel(pid, appointment_id)
    except ClinicError as e:
        return _err(e)
    out = {"result": "ok", "cancelled": store.describe_appt(a),
           "say": "Confirm it's cancelled and offer to find a new time."}
    if late:
        out["late_cancellation"] = store.info["policies"]["cancellation"]
    return out


async def reschedule_appointment(appointment_id: str, new_slot_id: str, caller_confirmed: bool,
                                 tool_context: ToolContext) -> dict:
    """Move one of the verified caller's appointments to a new slot from find_available_slots.

    Args:
        appointment_id: the appointment being moved (from list_my_appointments).
        new_slot_id: the slot_id of the new time the caller chose.
        caller_confirmed: true ONLY after you read back old and new times and the caller said yes.
    """
    pid = _caller(tool_context)
    if not pid:
        return _not_verified()
    try:
        old, new = store.reschedule(pid, appointment_id, new_slot_id)
    except ClinicError as e:
        return _err(e)
    return {"result": "ok", "moved_from": store.describe_appt(old), "booked": store.describe_appt(new)}


async def request_prescription_refill(medication: str, pharmacy: str, tool_context: ToolContext) -> dict:
    """Send a refill request to the verified caller's primary provider.

    Args:
        medication: the medication name (and strength if they know it).
        pharmacy: the pharmacy name and location they want it sent to.
    """
    pid = _caller(tool_context)
    if not pid:
        return _not_verified()
    p = store.patients[pid]
    prov = store.providers.get(p.primary_provider)
    t = store.open_ticket("refill", f"{medication} -> {pharmacy}", pid,
                          medication=medication, pharmacy=pharmacy, provider=p.primary_provider)
    return {"result": "ok", "ticket": t.id, "routed_to": prov.name if prov else "the on-call provider",
            "say": store.info["policies"]["refills"]}


ALL_TOOLS = [
    get_clinic_info, check_insurance, find_available_slots,
    verify_patient, register_new_patient,
    list_my_appointments, book_appointment, cancel_appointment, reschedule_appointment,
    request_prescription_refill, transfer_to_staff, flag_emergency,
]
