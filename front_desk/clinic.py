"""The clinic's back office — providers, the schedule, patients, appointments.

Plain Python, no ADK and no network: the voice agent's tools are thin wrappers
over `ClinicStore`, so this is the one file you swap for a real practice-management
/ EHR integration (athenahealth, Epic, eClinicalWorks, ...) — keep the method
names, change the insides.

Everything is in memory and seeded from `assets/clinic/clinic.json` (a fictional
clinic and fictional patients). Restarting the server resets the schedule.

Two rules the whole file follows:
  - every method takes/derives `now` from ONE injectable clock, so tests are
    deterministic and "tomorrow" means the same thing everywhere;
  - failures raise `ClinicError(code, message)` — a stable `code` the tools hand
    to the model, and a `message` written to be paraphrased out loud.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional

SEED_PATH = Path(__file__).resolve().parents[1] / "assets" / "clinic" / "clinic.json"

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SLOT_STEP = 20                 # minutes — the schedule grid every visit starts on
LEAD_TIME = timedelta(hours=1) # earliest bookable start is an hour from now
SEARCH_DAYS = 21               # how far ahead find_slots looks
MAX_OPTIONS = 3                # a voice agent should never read out more than three
BUSY_PERCENT = 35              # share of grid cells already taken by "other patients"
MAX_UPCOMING = 4               # per patient
MAX_VERIFY_ATTEMPTS = 3
LATE_CANCEL = timedelta(hours=24)
PARTS_OF_DAY = {"morning": (time(0), time(12)), "afternoon": (time(12), time(16)),
                "evening": (time(16), time(23, 59)), "any": (time(0), time(23, 59))}


class ClinicError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------- records ---

@dataclass
class Provider:
    id: str
    name: str
    title: str
    specialty: str
    days: list[str]
    also: str = ""


@dataclass
class Patient:
    id: str
    first_name: str
    last_name: str
    dob: date
    phone: str = ""
    insurance: str = ""
    primary_provider: str = ""


@dataclass
class Appointment:
    id: str
    patient_id: str
    provider_id: str
    visit_type: str
    start: datetime
    minutes: int
    reason: str = ""
    status: str = "booked"            # booked | cancelled

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)


@dataclass
class Slot:
    provider_id: str
    start: datetime
    visit_type: str

    @property
    def id(self) -> str:
        return f"{self.provider_id}-{self.start:%Y%m%d-%H%M}-{self.visit_type}"


@dataclass
class Ticket:
    """Anything a human on staff has to pick up: a transfer, a refill, an emergency."""
    id: str
    kind: str                          # transfer | refill | emergency
    summary: str
    patient_id: Optional[str]
    created: datetime
    details: dict = field(default_factory=dict)


@dataclass
class CallState:
    """What the clinic knows about ONE live call. Lives server-side, keyed by
    the ADK session id — never in the prompt, so the model can't talk its way
    into a verified state."""
    verified_patient_id: Optional[str] = None
    failed_verifications: int = 0
    emergency: bool = False

    @property
    def locked(self) -> bool:
        return self.failed_verifications >= MAX_VERIFY_ATTEMPTS and not self.verified_patient_id


# ---------------------------------------------------------------- helpers ---

def _hhmm(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


def normalize_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z\s]", "", (s or "").lower().replace("-", " "))).strip()


_DOB_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y", "%d %B %Y", "%b %d, %Y", "%b %d %Y")


def parse_date(s: str) -> date:
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", (s or "").strip())
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ClinicError("bad_date", f"I couldn't understand the date '{s}'. Ask for it again (month, day, year).")


def spoken_date(d: date, today: date) -> str:
    if d == today:
        return f"today, {d:%A} {d:%B} {d.day}"
    if d == today + timedelta(days=1):
        return f"tomorrow, {d:%A} {d:%B} {d.day}"
    return f"{d:%A}, {d:%B} {d.day}"


def spoken_time(t: time) -> str:
    return datetime.combine(date.min, t).strftime("%I:%M %p").lstrip("0")


# ------------------------------------------------------------------ store ---

class ClinicStore:
    def __init__(self, seed: dict, clock: Callable[[], datetime] = datetime.now):
        self.clock = clock
        c = seed["clinic"]
        self.info = c
        self.hours = {k: (tuple(map(_hhmm, v)) if v else None) for k, v in c["hours"].items()}
        self.lunch = tuple(map(_hhmm, c["lunch"])) if c.get("lunch") else None
        self.visit_types: dict[str, dict] = seed["visit_types"]
        self.providers = {p["id"]: Provider(**p) for p in seed["providers"]}
        self.patients: dict[str, Patient] = {}
        self.appointments: dict[str, Appointment] = {}
        self.tickets: list[Ticket] = []
        self.calls: dict[str, CallState] = {}
        for raw in seed.get("patients", []):
            p = Patient(raw["id"], raw["first_name"], raw["last_name"], parse_date(raw["dob"]),
                        raw.get("phone", ""), raw.get("insurance", ""), raw.get("primary_provider", ""))
            self.patients[p.id] = p
            for a in raw.get("appointments", []):
                self._seed_appointment(p.id, a)

    @classmethod
    def load(cls, path: Path = SEED_PATH, clock: Callable[[], datetime] = datetime.now) -> "ClinicStore":
        return cls(json.loads(path.read_text(encoding="utf-8")), clock)

    def _seed_appointment(self, patient_id: str, a: dict) -> None:
        # land on the next day the provider actually works, at the seeded time
        prov = self.providers[a["provider"]]
        d = self.clock().date() + timedelta(days=a["days_from_today"])
        while DAY_KEYS[d.weekday()] not in prov.days or not self.hours.get(DAY_KEYS[d.weekday()]):
            d += timedelta(days=1)
        appt = Appointment(self._new_id("A"), patient_id, prov.id, a["visit_type"],
                           datetime.combine(d, _hhmm(a["time"])), self.visit_types[a["visit_type"]]["minutes"],
                           a.get("reason", ""))
        self.appointments[appt.id] = appt

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

    def emergency_advice(self) -> str:
        """The one sentence every emergency path says — numbers come from clinic.json."""
        e = self.info["emergency"]
        return (f"hang up and call {e['number']} now, or {e['ambulance']} for an ambulance. "
                f"For a mental-health crisis, call {e['crisis_line']} on {e['crisis_number']} "
                f"(free, 24 hours).")

    def provider_by_name(self, name: str) -> Provider:
        n = normalize_name(name).replace("dr ", "").replace("doctor ", "")
        for p in self.providers.values():
            pn = normalize_name(p.name).replace("dr ", "")
            if n and (n == pn or n in pn.split() or n == p.id):
                return p
        names = ", ".join(p.name for p in self.providers.values())
        raise ClinicError("unknown_provider", f"There's no provider called '{name}'. Our providers are {names}.")

    def visit_type(self, key: str) -> dict:
        k = re.sub(r"[\s-]+", "_", (key or "").strip().lower())
        if k in self.visit_types:
            return self.visit_types[k]
        for vk, v in self.visit_types.items():
            if normalize_name(v["label"]) == normalize_name(key):
                return v
        raise ClinicError("unknown_visit_type",
                          f"'{key}' isn't a visit type. Use one of: {', '.join(self.visit_types)}.")

    def visit_key(self, key: str) -> str:
        v = self.visit_type(key)
        return next(k for k, x in self.visit_types.items() if x is v)

    def find_patient(self, full_name: str, dob: date) -> Optional[Patient]:
        parts = normalize_name(full_name).split()
        if len(parts) < 2:
            return None
        first, last = parts[0], parts[-1]
        for p in self.patients.values():
            if (p.dob == dob and normalize_name(p.first_name).split()[0] == first
                    and normalize_name(p.last_name).split()[-1] == last):
                return p
        return None

    # ---- the schedule ---------------------------------------------------
    def _other_patient(self, provider_id: str, cell: datetime) -> bool:
        """Deterministic 'already booked by someone else' — makes the calendar realistic
        without a database: the same cell is busy on every run and in every test."""
        h = hashlib.sha256(f"{provider_id}|{cell:%Y-%m-%dT%H:%M}".encode()).digest()
        return h[0] * 100 // 256 < BUSY_PERCENT

    def _check_slot(self, provider_id: str, start: datetime, visit_type: str,
                    ignore_appt: Optional[str] = None) -> None:
        prov = self.providers.get(provider_id)
        vt = self.visit_types.get(visit_type)
        if not prov or not vt:
            raise ClinicError("slot_unavailable", "That time isn't valid anymore. Search for openings again.")
        if prov.specialty not in vt["specialties"]:
            raise ClinicError("wrong_provider", f"{prov.name} doesn't do {vt['label']}s.")
        day = DAY_KEYS[start.weekday()]
        hours = self.hours.get(day)
        end = start + timedelta(minutes=vt["minutes"])
        if day not in prov.days or not hours:
            raise ClinicError("slot_unavailable", f"{prov.name} isn't in the office that day.")
        opens, closes = (datetime.combine(start.date(), t) for t in hours)
        if start < opens or end > closes or (start - opens).seconds // 60 % SLOT_STEP:
            raise ClinicError("slot_unavailable", "That time is outside clinic hours.")
        if self.lunch:
            l0, l1 = (datetime.combine(start.date(), t) for t in self.lunch)
            if start < l1 and end > l0:
                raise ClinicError("slot_unavailable", "That time overlaps the lunch break.")
        if start < self.clock() + LEAD_TIME:
            raise ClinicError("slot_unavailable", "That time has already passed or is too soon to book.")
        cell = start
        while cell < end:
            if self._other_patient(provider_id, cell):
                raise ClinicError("slot_unavailable", "That time was just taken.")
            cell += timedelta(minutes=SLOT_STEP)
        for a in self.appointments.values():
            if (a.status == "booked" and a.provider_id == provider_id and a.id != ignore_appt
                    and a.start < end and start < a.end):
                raise ClinicError("slot_unavailable", "That time was just taken.")

    def _is_free(self, provider_id: str, start: datetime, visit_type: str) -> bool:
        try:
            self._check_slot(provider_id, start, visit_type)
            return True
        except ClinicError:
            return False

    def find_slots(self, visit_type: str, on_or_after: Optional[date] = None,
                   provider_id: Optional[str] = None, part_of_day: str = "any",
                   only_date: bool = False) -> list[Slot]:
        """Up to MAX_OPTIONS openings, spread across days / parts of day — the way a
        receptionist offers 'Tuesday morning, Tuesday afternoon, or Wednesday'."""
        vkey = self.visit_key(visit_type)
        vt = self.visit_types[vkey]
        lo, hi = PARTS_OF_DAY.get((part_of_day or "any").lower(), PARTS_OF_DAY["any"])
        provs = [p for p in self.providers.values() if p.specialty in vt["specialties"]
                 and (provider_id is None or p.id == provider_id)]
        if provider_id and not provs:
            p = self.providers[provider_id]
            raise ClinicError("wrong_provider", f"{p.name} doesn't do {vt['label']}s.")
        first = max(on_or_after or self.today(), self.today())
        found: list[Slot] = []
        seen: set[tuple] = set()
        for offset in range(1 if only_date else SEARCH_DAYS):
            d = first + timedelta(days=offset)
            hours = self.hours.get(DAY_KEYS[d.weekday()])
            if not hours:
                continue
            day_slots = []
            for p in provs:
                t = datetime.combine(d, hours[0])
                while t < datetime.combine(d, hours[1]):
                    if lo <= t.time() < hi and self._is_free(p.id, t, vkey):
                        day_slots.append(Slot(p.id, t, vkey))
                    t += timedelta(minutes=SLOT_STEP)
            for s in sorted(day_slots, key=lambda s: s.start):
                bucket = (d, "am" if s.start.hour < 12 else "pm")
                if bucket in seen:
                    continue
                seen.add(bucket)
                found.append(s)
                if len(found) == MAX_OPTIONS:
                    return found
        return found

    def parse_slot(self, slot_id: str) -> Slot:
        m = re.fullmatch(r"\s*([a-z]+)-(\d{8})-(\d{4})-([a-z_]+)\s*", slot_id or "")
        if not m:
            raise ClinicError("slot_unavailable", "That slot id isn't valid. Search for openings again.")
        return Slot(m[1], datetime.strptime(m[2] + m[3], "%Y%m%d%H%M"), m[4])

    # ---- patients -------------------------------------------------------
    def register(self, full_name: str, dob: date, phone: str, insurance: str = "") -> Patient:
        parts = (full_name or "").strip().split()
        if len(parts) < 2:
            raise ClinicError("need_full_name", "I need both a first and a last name.")
        if dob >= self.today() or dob.year < 1900:
            raise ClinicError("bad_date", "That date of birth doesn't look right. Ask for it again.")
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) < 7:
            raise ClinicError("bad_phone", "I need a phone number we can reach them at.")
        if self.find_patient(full_name, dob):
            raise ClinicError("already_registered",
                              "They're already a patient here — verify them instead of registering again.")
        p = Patient(self._new_id("P"), parts[0].title(), " ".join(parts[1:]).title(), dob, digits,
                    insurance.strip(), "")
        self.patients[p.id] = p
        return p

    # ---- appointments ---------------------------------------------------
    def upcoming(self, patient_id: str) -> list[Appointment]:
        now = self.clock()
        return sorted((a for a in self.appointments.values()
                       if a.patient_id == patient_id and a.status == "booked" and a.start >= now),
                      key=lambda a: a.start)

    def _owned(self, patient_id: str, appointment_id: str) -> Appointment:
        a = self.appointments.get((appointment_id or "").strip().upper())
        # same error for "not yours" and "doesn't exist" — never confirm someone else's record
        if not a or a.patient_id != patient_id or a.status != "booked" or a.start < self.clock():
            raise ClinicError("appointment_not_found",
                              "I can't find that upcoming appointment. List their appointments again.")
        return a

    def book(self, patient_id: str, slot_id: str, reason: str = "") -> Appointment:
        slot = self.parse_slot(slot_id)
        self._check_slot(slot.provider_id, slot.start, slot.visit_type)
        mine = self.upcoming(patient_id)
        if len(mine) >= MAX_UPCOMING:
            raise ClinicError("too_many_upcoming",
                              f"They already have {len(mine)} upcoming visits — that's our limit. Offer a transfer to staff.")
        minutes = self.visit_types[slot.visit_type]["minutes"]
        end = slot.start + timedelta(minutes=minutes)
        for a in mine:
            if a.start < end and slot.start < a.end:
                raise ClinicError("double_booked", "They already have an appointment at that time.")
        appt = Appointment(self._new_id("A"), patient_id, slot.provider_id, slot.visit_type,
                           slot.start, minutes, (reason or "").strip()[:200])
        self.appointments[appt.id] = appt
        return appt

    def cancel(self, patient_id: str, appointment_id: str) -> tuple[Appointment, bool]:
        """-> (the cancelled appointment, whether it was a late cancellation)."""
        a = self._owned(patient_id, appointment_id)
        a.status = "cancelled"
        return a, a.start - self.clock() < LATE_CANCEL

    def reschedule(self, patient_id: str, appointment_id: str, new_slot_id: str) -> tuple[Appointment, Appointment]:
        """Book-then-cancel, so a failed booking never loses the original appointment."""
        old = self._owned(patient_id, appointment_id)
        old.status = "cancelled"              # free the patient's own time for the overlap check
        try:
            new = self.book(patient_id, new_slot_id, old.reason)
        except ClinicError:
            old.status = "booked"
            raise
        return old, new

    # ---- staff hand-offs -----------------------------------------------
    def open_ticket(self, kind: str, summary: str, patient_id: Optional[str] = None, **details) -> Ticket:
        t = Ticket(self._new_id("T"), kind, (summary or "").strip()[:300], patient_id, self.clock(), details)
        self.tickets.append(t)
        return t

    # ---- spoken descriptions -------------------------------------------
    def describe_slot(self, slot: Slot) -> dict:
        p = self.providers[slot.provider_id]
        return {"slot_id": slot.id, "provider": p.name,
                "when": f"{spoken_date(slot.start.date(), self.today())} at {spoken_time(slot.start.time())}",
                "visit": self.visit_types[slot.visit_type]["label"],
                "minutes": self.visit_types[slot.visit_type]["minutes"]}

    def describe_appt(self, a: Appointment) -> dict:
        p = self.providers[a.provider_id]
        return {"appointment_id": a.id, "provider": p.name,
                "when": f"{spoken_date(a.start.date(), self.today())} at {spoken_time(a.start.time())}",
                "date": a.start.date().isoformat(), "time": a.start.strftime("%H:%M"),
                "visit": self.visit_types[a.visit_type]["label"], "minutes": a.minutes,
                "reason": a.reason}
