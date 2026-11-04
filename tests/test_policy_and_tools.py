"""The policy door + the tools, called exactly the way ADK calls them (minus the model)."""
import asyncio
import inspect

from front_desk import events, policy, tools
from tests.conftest import run, tool


async def call(name, ctx, **args):
    """gate -> tool -> audit, the ADK order. Returns what the MODEL would see."""
    blocked = await policy.front_desk_gate(tool(name), args, ctx)
    if blocked is not None:
        return blocked
    fn = getattr(tools, name)
    if "tool_context" in inspect.signature(fn).parameters:
        args = {**args, "tool_context": ctx}
    resp = await fn(**args)
    await policy.audit(tool(name), args, ctx, resp)
    return resp


def test_every_tool_is_async_and_documented():
    for fn in tools.ALL_TOOLS:
        assert inspect.iscoroutinefunction(fn), fn.__name__
        params = [p for p in inspect.signature(fn).parameters if p != "tool_context"]
        assert fn.__doc__, fn.__name__
        assert "Args:" in fn.__doc__ or not params, fn.__name__


def test_no_tool_accepts_a_patient_id():
    for fn in tools.ALL_TOOLS:
        assert "patient_id" not in inspect.signature(fn).parameters


def test_record_tools_blocked_until_verified(store, ctx):
    for name, args in [("list_my_appointments", {}),
                       ("book_appointment", {"slot_id": "x", "reason": "", "caller_confirmed": True}),
                       ("cancel_appointment", {"appointment_id": "x", "caller_confirmed": True}),
                       ("request_prescription_refill", {"medication": "lisinopril", "pharmacy": "CVS"})]:
        r = run(call(name, ctx, **args))
        assert r["result"] == "blocked" and r["code"] == "not_verified", name


def test_verify_then_list(store, ctx):
    r = run(call("verify_patient", ctx, full_name="Jordan Rivera", date_of_birth="1985-03-14"))
    assert r["verified"] and r["first_name"] == "Jordan" and "dob" not in r
    r = run(call("list_my_appointments", ctx))
    assert r["result"] == "ok" and len(r["appointments"]) == 1


def test_verification_is_per_call(store, ctx):
    from types import SimpleNamespace
    run(call("verify_patient", ctx, full_name="Jordan Rivera", date_of_birth="1985-03-14"))
    other = SimpleNamespace(session=SimpleNamespace(id="call-2"))
    assert run(call("list_my_appointments", other))["code"] == "not_verified"


def test_lockout_after_three_failures(store, ctx):
    for i in range(3):
        r = run(call("verify_patient", ctx, full_name="Jordan Rivera", date_of_birth="1999-01-01"))
        assert r["code"] == "no_match" and r["attempts_left"] == 2 - i
    r = run(call("verify_patient", ctx, full_name="Jordan Rivera", date_of_birth="1985-03-14"))
    assert r["result"] == "blocked" and r["code"] == "locked"
    assert run(call("register_new_patient", ctx, full_name="New Person", date_of_birth="1990-01-01",
                    phone="5551234567"))["code"] == "locked"


def test_booking_needs_confirmation_then_succeeds(store, ctx):
    run(call("verify_patient", ctx, full_name="Priya Shah", date_of_birth="November 2, 1992"))
    opts = run(call("find_available_slots", ctx, visit_type="annual_physical", provider="Dr. Okafor"))["options"]
    assert opts and all(o["provider"] == "Dr. James Okafor" for o in opts)
    slot = opts[0]["slot_id"]
    r = run(call("book_appointment", ctx, slot_id=slot, reason="yearly checkup", caller_confirmed=False))
    assert r["code"] == "needs_confirmation"
    r = run(call("book_appointment", ctx, slot_id=slot, reason="yearly checkup", caller_confirmed=True))
    assert r["result"] == "ok" and r["booked"]["provider"] == "Dr. James Okafor"


def test_new_patient_flow(store, ctx):
    r = run(call("register_new_patient", ctx, full_name="Sam Taylor", date_of_birth="2001-06-30",
                 phone="555 222 3333", insurance="Oscar"))
    assert r["registered"]
    slot = run(call("find_available_slots", ctx, visit_type="new_patient"))["options"][0]["slot_id"]
    r = run(call("book_appointment", ctx, slot_id=slot, reason="establish care", caller_confirmed=True))
    assert r["result"] == "ok" and r["booked"]["visit"] == "new patient visit"


def test_reschedule_and_cancel_through_tools(store, ctx):
    run(call("verify_patient", ctx, full_name="Marcus Chen", date_of_birth="1978-07-22"))
    appt = run(call("list_my_appointments", ctx))["appointments"][0]
    new = run(call("find_available_slots", ctx, visit_type="skin_check",
                   preferred_date="2026-10-15"))["options"][0]["slot_id"]
    r = run(call("reschedule_appointment", ctx, appointment_id=appt["appointment_id"], new_slot_id=new,
                 caller_confirmed=True))
    assert r["result"] == "ok"
    r = run(call("cancel_appointment", ctx, appointment_id=r["booked"]["appointment_id"], caller_confirmed=True))
    assert r["result"] == "ok"
    assert run(call("list_my_appointments", ctx))["appointments"] == []


def test_emergency_blocks_scheduling(store, ctx):
    run(call("verify_patient", ctx, full_name="Jordan Rivera", date_of_birth="1985-03-14"))
    r = run(call("flag_emergency", ctx, summary="crushing chest pain"))
    assert "112" in r["say"] and "108" in r["say"] and "14416" in r["say"] and "911" not in r["say"]
    slot = run(call("find_available_slots", ctx, visit_type="sick_visit"))["options"][0]["slot_id"]
    r = run(call("book_appointment", ctx, slot_id=slot, reason="chest pain", caller_confirmed=True))
    assert r["code"] == "emergency" and "112" in r["say"]
    assert store.tickets[-1].kind == "emergency"


def test_controlled_refill_blocked_normal_refill_routed(store, ctx):
    run(call("verify_patient", ctx, full_name="Jordan Rivera", date_of_birth="1985-03-14"))
    assert run(call("request_prescription_refill", ctx, medication="Xanax 1mg",
                    pharmacy="CVS Main St"))["code"] == "controlled_substance"
    r = run(call("request_prescription_refill", ctx, medication="lisinopril 10mg", pharmacy="CVS Main St"))
    assert r["result"] == "ok" and r["routed_to"] == "Dr. Maya Patel"


def test_open_tools(store, ctx):
    assert run(call("check_insurance", ctx, plan_name="blue cross"))["accepted"] is True
    assert run(call("check_insurance", ctx, plan_name="Acme Health"))["accepted"] is False
    info = run(call("get_clinic_info", ctx, topic="hours"))
    assert any("Sun: closed" in h for h in info["hours"])
    assert "providers" in run(call("get_clinic_info", ctx, topic="something odd"))
    r = run(call("find_available_slots", ctx, visit_type="skin_check", preferred_date="2026-10-11"))
    assert r["options"] and "note" in r                       # Sunday -> next openings
    assert run(call("find_available_slots", ctx, visit_type="massage"))["code"] == "unknown_visit_type"
    assert run(call("find_available_slots", ctx, visit_type="sick_visit",
                    provider="Dr. Nobody"))["code"] == "unknown_provider"


def test_events_reach_only_the_subscribed_call(store, ctx):
    async def go():
        q = events.subscribe("call-1")
        try:
            await call("check_insurance", ctx, plan_name="aetna")
            from types import SimpleNamespace
            await call("check_insurance", SimpleNamespace(session=SimpleNamespace(id="call-9")), plan_name="x")
            got = [q.get_nowait() for _ in range(q.qsize())]
        finally:
            events.unsubscribe("call-1")
        return got
    got = asyncio.run(go())
    assert [e["type"] for e in got] == ["action", "tool_result"]
    assert got[0]["status"] == "allowed" and got[1]["response"]["accepted"] is True


def test_logs_redact_phi(store, ctx, caplog):
    caplog.set_level("INFO", logger="front-desk.policy")
    run(call("verify_patient", ctx, full_name="Jordan Rivera", date_of_birth="1985-03-14"))
    text = caplog.text
    assert "Jordan" not in text and "1985" not in text and "***" in text


def test_persona_knows_today(store):
    from front_desk.persona import instruction
    s = instruction()
    assert "2026-10-05" in s and "Riverside Family Health" in s
    assert "call 112 now" in s and "Tele-MANAS on 14416" in s


def test_same_turn_verify_and_request_race(store, ctx):
    """ADK runs one turn's calls concurrently; the request may hit the gate before verify lands."""
    async def go():
        return await asyncio.gather(
            call("list_my_appointments", ctx),
            call("verify_patient", ctx, full_name="Jordan Rivera", date_of_birth="1985-03-14"))
    listed, verified = asyncio.run(go())
    assert verified["verified"] and listed["result"] == "ok"
