from datetime import date, datetime, timedelta

import pytest

from front_desk.clinic import ClinicError, parse_date, spoken_time
from tests.conftest import NOW


def test_seed_loads(store):
    assert {"patel", "okafor", "ruiz", "lee"} <= set(store.providers)
    assert len(store.patients) == 3
    # seeded appointments land on days the provider works, in the future
    for a in store.appointments.values():
        assert a.start > NOW
        assert ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][a.start.weekday()] in store.providers[a.provider_id].days


@pytest.mark.parametrize("s", ["1985-03-14", "03/14/1985", "March 14, 1985", "March 14th 1985", "14 March 1985"])
def test_parse_date_formats(s):
    assert parse_date(s) == date(1985, 3, 14)


def test_parse_date_rejects_garbage():
    with pytest.raises(ClinicError) as e:
        parse_date("sometime in spring")
    assert e.value.code == "bad_date"


def test_spoken_time():
    assert spoken_time(datetime(2026, 1, 1, 9, 20).time()) == "9:20 AM"
    assert spoken_time(datetime(2026, 1, 1, 14, 0).time()) == "2:00 PM"


def test_find_patient_is_forgiving_on_case_and_middle_names(store):
    assert store.find_patient("jordan rivera", date(1985, 3, 14)).id == "P1001"
    assert store.find_patient("Jordan A. Rivera", date(1985, 3, 14)).id == "P1001"
    assert store.find_patient("Jordan Rivera", date(1985, 3, 15)) is None
    assert store.find_patient("Jordan", date(1985, 3, 14)) is None


def test_find_slots_respects_hours_lead_time_and_limit(store):
    slots = store.find_slots("sick_visit")
    assert 1 <= len(slots) <= 3
    for s in slots:
        assert s.start >= NOW + timedelta(hours=1)
        assert not (12 <= s.start.hour < 13)               # lunch
        store._check_slot(s.provider_id, s.start, s.visit_type)  # really free


def test_find_slots_spreads_options_across_day_parts(store):
    slots = store.find_slots("follow_up", on_or_after=date(2026, 10, 7))
    buckets = {(s.start.date(), s.start.hour < 12) for s in slots}
    assert len(buckets) == len(slots)


def test_find_slots_filters_provider_and_part_of_day(store):
    slots = store.find_slots("follow_up", provider_id="patel", part_of_day="afternoon")
    assert slots and all(s.provider_id == "patel" and s.start.hour >= 12 for s in slots)


def test_find_slots_matches_specialty(store):
    assert all(s.provider_id == "ruiz" for s in store.find_slots("skin_check"))
    with pytest.raises(ClinicError):
        store.find_slots("skin_check", provider_id="patel")


def test_closed_day_has_no_slots(store):
    sunday = date(2026, 10, 11)
    assert store.find_slots("sick_visit", on_or_after=sunday, only_date=True) == []


def test_book_then_slot_is_gone(store):
    s = store.find_slots("follow_up")[0]
    a = store.book("P1002", s.id, "check-in")
    assert a.id in store.appointments
    with pytest.raises(ClinicError) as e:
        store.book("P1003", s.id)
    assert e.value.code == "slot_unavailable"
    assert s.id not in {x.id for x in store.find_slots("follow_up")}


def test_book_rejects_invalid_and_past_slots(store):
    for bad in ["nonsense", "patel-20261005-0800-follow_up", "patel-20261011-1000-follow_up",
                "ruiz-20261006-1000-annual_physical"]:
        with pytest.raises(ClinicError):
            store.book("P1002", bad)


def test_patient_cannot_double_book_same_time(store):
    # Patel and Lee both work weekdays: find a start time both have free, book one, then the other.
    for s in store.find_slots("follow_up", provider_id="patel", on_or_after=date(2026, 10, 6)):
        if store._is_free("lee", s.start, "sick_visit"):
            store.book("P1002", s.id)
            with pytest.raises(ClinicError) as e:
                store.book("P1002", f"lee-{s.start:%Y%m%d-%H%M}-sick_visit")
            assert e.value.code == "double_booked"
            return
    pytest.skip("no shared free time in the seeded schedule")


def test_cancel_only_own_appointments(store):
    jordans = store.upcoming("P1001")[0]
    with pytest.raises(ClinicError) as e:
        store.cancel("P1002", jordans.id)
    assert e.value.code == "appointment_not_found"
    a, late = store.cancel("P1001", jordans.id)
    assert a.status == "cancelled" and late is False
    assert store.upcoming("P1001") == []


def test_late_cancellation_flagged(store):
    s = store.find_slots("sick_visit")[0]           # today/tomorrow
    a = store.book("P1002", s.id)
    _, late = store.cancel("P1002", a.id)
    assert late is True


def test_reschedule_is_atomic(store):
    old = store.upcoming("P1001")[0]
    with pytest.raises(ClinicError):
        store.reschedule("P1001", old.id, "patel-20261011-1000-follow_up")   # Sunday
    assert store.appointments[old.id].status == "booked"                    # not lost
    new_slot = store.find_slots("follow_up", provider_id="patel")[0]
    moved, new = store.reschedule("P1001", old.id, new_slot.id)
    assert moved.status == "cancelled" and new.start == new_slot.start and new.reason == old.reason


def test_register_validates_and_dedupes(store):
    p = store.register("alex kim", date(1990, 1, 2), "(555) 123-4567", "Aetna")
    assert p.first_name == "Alex" and p.phone == "5551234567"
    for args, code in [(("Alex", date(1990, 1, 2), "5551234567"), "need_full_name"),
                       (("Bo Li", date(2030, 1, 1), "5551234567"), "bad_date"),
                       (("Bo Li", date(1990, 1, 1), "12"), "bad_phone"),
                       (("Jordan Rivera", date(1985, 3, 14), "5550142"), "already_registered")]:
        with pytest.raises(ClinicError) as e:
            store.register(*args)
        assert e.value.code == code


def test_upcoming_limit(store):
    booked = 0
    for s in store.find_slots("follow_up", on_or_after=date(2026, 10, 7)) + \
            store.find_slots("follow_up", on_or_after=date(2026, 10, 14)):
        try:
            store.book("P1002", s.id)
            booked += 1
        except ClinicError as e:
            assert e.code == "too_many_upcoming"
            assert booked == 4
            return
    pytest.skip("not enough distinct slots to hit the limit")
