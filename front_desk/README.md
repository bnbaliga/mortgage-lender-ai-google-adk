# front_desk — Ava, the clinic's voice front desk

A patient calls (from the browser), and **Ava** answers for *Riverside Family Health* (a fictional clinic): she books, moves and cancels visits, registers new patients, answers clinic questions, checks insurance, sends refill requests, hands off to a human — and tells anyone describing an emergency to call 112 (or 108 for an ambulance; Tele-MANAS 14416 for a mental-health crisis — India numbers, set in `clinic.json` → `clinic.emergency`). You can talk over her mid-sentence and she stops.

Built on the EP3 pattern from [`../adk_tools/`](../adk_tools/): ADK `run_live` + plain-function tools + **one policy door** every tool call walks through. The DJ's toy rule (quiet hours) is replaced by the rules a front desk actually needs.

## Run it

```bash
uv sync
cp .env.example .env                       # GOOGLE_API_KEY = an AI Studio key (not Vertex)
uv run uvicorn front_desk.server:app --port 8000
```

Open <http://localhost:8000>, press **Call the front desk** (or space), and talk. No mic? It connects anyway and you type.

Demo patients (all fictional): **Jordan Rivera** 1985-03-14 · **Priya Shah** 1992-11-02 · **Marcus Chen** 1978-07-22.

Try: *"I'd like to book a physical with Dr. Okafor"* · *"I'm Jordan Rivera, born March 14 1985 — can I move my appointment?"* · *"do you take Cigna?"* · *"I need a refill of my lisinopril"* · *"I have crushing chest pain"*.

## Files

| File | What it is |
|---|---|
| [`clinic.py`](clinic.py) | The back office: providers, hours, a deterministic schedule, patients, appointments, tickets. Pure Python — **the file you swap for a real EHR / practice-management API**. |
| [`tools.py`](tools.py) | 12 ADK tools. Instant returns (a slow tool is dead air). **No tool takes a patient id** — who the caller is lives server-side in the call state. |
| [`policy.py`](policy.py) | The policy door (`before_tool_callback`) + an audit trail (`after_tool_callback`) streamed to the browser. Server logs are PHI-redacted. |
| [`persona.py`](persona.py) | Ava's instruction — a function, so "today" and "next Tuesday" are always right. |
| [`agent.py`](agent.py) | The ADK `Agent` (also loads in `adk web .` as `front_desk`). |
| [`server.py`](server.py) | FastAPI WebSocket bridge: per-call isolation, Ava greets first, typed input, long-call context compression. |
| [`usage.py`](usage.py) | Token monitor: per-call and per-server totals, optional cost estimate. |
| [`events.py`](events.py) | Per-call event bus (EP3's single global queue leaked between callers). |
| [`frontend/`](frontend/) | The call page: voice orb, captions, typed fallback, the **visit card** (what got booked/cancelled/sent), and the "what Ava did" log. |
| [`smoke_live.py`](smoke_live.py) | End-to-end scenarios against the real live model. |
| [`../assets/clinic/clinic.json`](../assets/clinic/clinic.json) | The clinic: hours, providers, visit types, insurance, policies, demo patients. |

## The rules (policy.py)

| Rule | Blocks | Why it's in code, not the prompt |
|---|---|---|
| **Identity** | reading/changing appointments, refills — until `verify_patient` (name + DOB) succeeds on *this* call | a prompt can be talked out of it |
| **Lockout** | identity tools after 3 failed checks | stops guessing DOBs |
| **Confirm** | book / cancel / reschedule without `caller_confirmed=true` | forces a read-back before anything changes |
| **Emergency** | booking after `flag_emergency` | the only job left is "call 112 / 108" |
| **Controlled substances** | refill requests for opioids, benzos, stimulants… | needs a visit |

Blocked calls return a dict the model paraphrases to the caller; the browser log marks them as stopped.

The model often fires `verify_patient` and the request in the **same turn**, and ADK runs a turn's calls concurrently — so the gate gives a sibling identity check up to 250 ms to land before refusing. (Found by the live smoke test; covered by `test_same_turn_verify_and_request_race`.)

## Wire protocol (`WS /ws`)

- **up:** binary 16 kHz PCM · `{"type":"text","text":…}`
- **down:** binary 24 kHz PCM · `transcript {role, text, final}` · `interrupted` · `turn_complete` · `error` · `action {tool, args, status, reason}` · `tool_result {tool, response}`

Transcripts stream as chunks, then ADK repeats the whole line once with `final: true` — the UI replaces the line rather than appending twice.

## Tests

```bash
uv run pytest                                   # 38 unit tests — clinic, tools, policy, no network
uv run python -m front_desk.smoke_live          # live: greeting, booking, unverified cancel, emergency,
                                                #       controlled refill, no medical advice, clinic info
uv run python -m front_desk.smoke_live --audio  # + a real spoken turn (Windows SAPI voice -> 16 kHz PCM)
```

All 8 live scenarios pass against `gemini-3.1-flash-live-preview`.

## Config (`.env`)

| Var | Default | |
|---|---|---|
| `GOOGLE_API_KEY` | — | AI Studio key |
| `LIVE_MODEL` | `gemini-3.1-flash-live-preview` | |
| `FRONT_DESK_VOICE` | `LIVE_VOICE` → `Aoede` | Live native voice for Ava |
| `FRONT_DESK_GREETING` | `1` | `0` = Ava waits for the caller to speak first |
| `FRONT_DESK_ACCENT` | — | e.g. `Indian English`. Live voices have no fixed accent, so it's set in the instruction; she also follows the caller into Hindi / Hinglish |
| `FRONT_DESK_AGENT_NAME` | `Ava` | e.g. `Asha` |
| `TOKEN_PRICE_IN_AUDIO` · `TOKEN_PRICE_IN_TEXT` · `TOKEN_PRICE_OUT_AUDIO` · `TOKEN_PRICE_OUT_TEXT` | — | USD per 1M tokens, from Google's current price list for your `LIVE_MODEL`. Unset = tokens only, no cost estimate |

## Token monitor

Every model turn, the Live API reports token usage; [`usage.py`](usage.py) sums it per call (billed tokens) and keeps the latest prompt size (how full the conversation memory is — older turns get compressed at 100,000).

- **On the page:** the "Tokens used" panel — total, in/out split by voice vs text, model turns, estimated cost, and a memory bar that turns red past 80%.
- **In the server log:** one line per finished call: `call ab12cd34 ended — tokens: 18,240 total (17,100 in / 1,140 out) over 6 turns …`
- **`GET /api/usage`:** totals for every finished call since the server started.
- **`smoke_live`** prints tokens per scenario and for the whole run.

## Known limits — read before pointing this at real patients

- **Demo data, in memory.** Restarting resets the schedule. Swap `ClinicStore` for your EHR/PM system.
- **Not HIPAA-ready as-is.** Audio goes to Google's Gemini API — you need a BAA-covered setup (e.g. Vertex AI under your org's BAA), TLS, authentication, retention policies, and an audit store. The server-log redaction here is a start, not compliance.
- **Name + DOB is weak identity.** Real deployments add a second factor (SMS code to the number on file, or caller-ID match).
- **The preview model adds its own disclaimer** ("this is not medical advice…") on health topics, despite the persona asking it not to. Harmless; noted.
- **Browser, not phone.** For real phone calls, put a telephony bridge (Twilio Media Streams, etc.) in front of the same `/ws` loop — it needs 8 kHz μ-law ↔ 16/24 kHz PCM conversion.
- Headphones still help: browser echo cancellation is good, not perfect.
