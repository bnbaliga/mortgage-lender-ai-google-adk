# Mac: Claude Code prompts for the recommended changes

Run these in order from the root of `mortgage-lender-ai-google-adk`. Each prompt is self-contained. Review and run `uv run pytest` after each one before moving on. The numbers match the findings table (section 9) in the executive review.

---

## Prompt 1: Fix the Texas "rental" bypass and unknown-state default (findings 1, 2, 13)

```
In mortgage/mortgage.py and mortgage/tools.py, harden HELOC eligibility:

1. Add a normalize_property_use() helper that maps free-text property use to exactly one of
   "primary", "second_home", "investment". Map "rental", "rent out", "rented", "investment property",
   "income property", "landlord" -> "investment"; "primary residence", "homestead", "my home", "owner occupied" -> "primary";
   "vacation", "second home" -> "second_home". Unknown values raise MortgageError("unknown_property_use").
   Call it at the top of MortgageStore.check_heloc_eligibility.
2. Add a full 50-state + DC name -> 2-letter code map and a normalize_state() helper. Use it in get_state_rules,
   check_heloc_eligibility and check_state_regulations so the result always reports the 2-letter code (e.g. "TX", never "TEXAS").
3. If a state has no rules on file, check_heloc_eligibility must NOT return eligible. Return
   {"result": "ok", "eligible": None, "status": "needs_review", "say": "Explain you can't confirm eligibility for that state
   on this call and offer to connect them with a licensed loan officer."}.
   Change check_state_regulations so it no longer says "standard federal guidelines apply"; it should offer a loan officer instead.
4. In the check_heloc_eligibility tool docstring, tell the model property_type accepts the caller's own words.
5. Create tests/test_mortgage_rules.py covering: TX + "rental" -> ineligible; TX + "investment" -> ineligible;
   TX primary with existing cash-out -> ineligible with both alternatives; TX primary, value 550000, balance 100000,
   no cash-out -> eligible with max_heloc_amount for 80% CLTV; "Texas" and "tx" both normalise to "TX";
   NY (no rules) -> needs_review, never eligible; unknown property use -> error.
Use the existing tests/conftest.py patterns and tools.use_store() to inject a fresh store. Run uv run pytest.
```

## Prompt 2: Make the policy door enforce what it promises (findings 7, 8)

```
mortgage/policy.py's docstring claims it enforces disclaimers, transfers and compliance, but SUGGEST_TRANSFER is unused,
NEEDS_VERIFIED is empty, and audit() mutates the response dict while commenting "don't modify what the model sees".

1. In audit(): when get_rate_estimate returns without a disclaimer, return a NEW dict that includes the disclaimer
   (return a dict from after_tool_callback so ADK uses it), rather than relying on in-place mutation.
   Do the same for check_heloc_eligibility ineligible results (add "say" offering a loan officer) and for any tool in
   SUGGEST_TRANSFER whose result is ineligible or needs_review.
2. Add a block in mortgage_gate: get_rate_estimate is blocked with code "missing_inputs" if credit_score or loan_to_value
   is outside a sane range (300-850, 1-100), with a "say" that asks the caller for the missing info.
3. Remove the hard-coded Texas rules from mortgage/persona.py (the "STATE REGULATIONS — TEXAS IS SPECIAL" block).
   Replace with: "Whenever a caller mentions a state, a property, or home equity, call check_heloc_eligibility or
   check_state_regulations and speak only from the result." Rules must live only in assets/mortgage/mortgage.json.
4. Update the policy.py docstring to describe exactly what is enforced.
5. Add tests in tests/test_mortgage_policy.py that call mortgage_gate and audit directly with fake tool/context objects
   (see how tests/test_policy_and_tools.py does it for the clinic) to prove each rule.
```

## Prompt 3: Fix smaller tool bugs (findings 10, 12, 14)

```
1. get_company_info in mortgage/tools.py checks hasattr(store, 'spoken_time'), which is always False, so hours are a
   hard-coded string. Import spoken_time from mortgage.mortgage and build hours from store.hours. Add a test.
2. get_available_officers only checks the weekday. Add "hours" ["09:00","17:00"] and "timezone" "America/Chicago" to each
   loan officer in assets/mortgage/mortgage.json; update the LoanOfficer dataclass; check current time in the officer's
   time zone using zoneinfo. Add tests using the injectable clock.
3. In assets/mortgage/mortgage.json rename "cooling_off_period_days" to "closing_notice_days" (Texas 12-day notice before
   closing) and add "rescission_days": 3. Update code that reads it, and phrase the note as
   "Texas requires a 12-day notice period before closing, plus a 3-day right to cancel after closing."
   Add a "source" field with a citation (Texas Constitution Art. XVI, Sec. 50) and "last_reviewed" date to each state rule.
```

## Prompt 4: Mac test suite and scripted call evaluation (finding 5)

```
Create a test and evaluation suite for the mortgage agent:

1. tests/test_mortgage_tools.py: unit tests for every tool in mortgage/tools.py ALL_TOOLS (happy path + error path),
   using tools.use_store() with a fixed clock.
2. mortgage/smoke_live.py, modelled on front_desk/smoke_live.py: drives the real agent over runner.run_live with typed
   (or --audio synthesised) caller turns and asserts on the tool calls made. Include the reference call:
     Turn 1: "I have a couple of properties in Texas and I'm hoping to get a HELOC, probably around $40,000. One is my
              primary residence. The other is a property I rent out. Can I put the HELOC on the rental?"
       expect check_heloc_eligibility called with state TX and an investment property -> eligible False.
     Turn 2: "I do have a mortgage on my primary, about $100,000, worth about $550,000. It's a cash-out I did a few years ago."
       expect check_heloc_eligibility(TX, primary, has_existing_cashout=True, 550000, 100000) -> eligible False,
       and the spoken reply mentions refinancing then a HELOC, or another cash-out refinance, and offers a loan officer.
     Turn 3: "Yeah, that'd be great." expect transfer_to_loan_officer called.
   Add 6 more scenarios: California HELOC (eligible), unknown state, caller asks for legal advice, caller asks Mac to
   approve the loan, caller says "rental" vs "investment", caller fails identity verification 3 times.
3. Add a README section "Testing Mac" with the commands.
```

## Prompt 5: Regulation knowledge pipeline with citations (finding 3)

```
Add a regulation knowledge pipeline so Mac answers from approved documents and website content:

1. New package mortgage/knowledge/ with:
   - sources.yaml: approved sources (url, jurisdiction, topic). Start with Texas Constitution Art. XVI Sec. 50
     (statutes.capitol.texas.gov), Texas Finance Commission / OCCC home equity interpretations (7 TAC Chapter 153),
     CFPB Regulation Z pages for HELOCs, and a local folder docs/policies/ for internal PDFs.
   - ingest.py: fetches each URL with httpx (respect robots.txt, identify with a generic user agent, rate limit),
     extracts text (trafilatura or BeautifulSoup; pypdf for PDFs), splits into ~800-token passages with overlap, and
     stores passages with source url, title, section heading, retrieved_at and a content hash.
   - index.py: embeds passages with the Gemini embedding model (google-genai) and stores them in a local
     SQLite + numpy index behind an interface (search(query, k, jurisdiction)) so it can later be swapped for Vertex AI Search.
   - Change detection: if a source's hash changes, mark it "pending_review"; only "approved" passages are searchable.
     Add a CLI: uv run python -m mortgage.knowledge.ingest, and ... approve <source_id>.
2. New tool search_regulations(question: str, state: str = "") in mortgage/tools.py returning the top 3 approved
   passages with citations, plus "say": "Answer only from these passages, mention the source briefly, and offer a loan
   officer if they don't answer the question." Add it to ALL_TOOLS and the persona's tool guidance.
3. Eligibility decisions must still come from the structured rules in mortgage.json, never from search results.
4. Add dependencies with uv add, and tests that use a small fixture corpus (no network).
```

## Prompt 6: Option comparison and savings estimate (finding 4)

```
Add a compare_options tool:

1. assets/mortgage/rate_sheet.json: today's indicative rates per product, credit tier and LTV band, plus a
   "market_benchmark" rate per product with its source and as_of date. Load it in MortgageStore and use it in
   get_rate_estimate instead of the static product rate_range.
2. compare_options(goal: str, state: str, property_use: str, property_value: float, mortgage_balance: float,
   cash_needed: float, credit_score: int, has_existing_cashout: bool) returns up to 3 options that pass the state rules
   (e.g. HELOC, cash-out refinance, rate-and-term refinance then HELOC), each with estimated rate, estimated monthly
   payment, and estimated saving per month and over the loan versus the market benchmark. Always attach the rate
   disclaimer and "not a loan commitment" disclaimer.
3. The policy door must block compare_options if the state rules are unknown (needs loan officer review).
4. Unit tests for the payment maths and that ineligible products are never offered (e.g. no HELOC for a Texas rental).
```

## Prompt 7: Real phone calls, warm transfer and call summary (finding 6)

```
Add a phone channel and a real hand-off:

1. New module mortgage/telephony.py: a Twilio Media Streams WebSocket endpoint (/twilio/stream) and a TwiML webhook
   (/twilio/voice) that bridge 8 kHz mu-law phone audio to the existing run_live loop (resample to 16 kHz PCM up,
   24 kHz PCM -> 8 kHz mu-law down). Reuse the same session, events and policy code as the browser /ws path.
   Keep the browser path working.
2. The greeting must add: "This call may be recorded" before the existing greeting text.
3. New tool send_call_summary(): builds a structured summary (caller need, state, property use, values, eligibility
   results, options discussed, consent captured) from CallState.inquiry_context. Update tools to record their results
   into inquiry_context. transfer_to_loan_officer calls it and, when an officer is available, issues a Twilio <Dial>
   warm transfer; otherwise books a call-back.
4. register_lead must capture explicit consent to be contacted (new consent_to_contact: bool arg); the policy door
   blocks register_lead without it.
5. Config via env vars (TWILIO_ACCOUNT_SID etc.); never commit secrets. Tests with mocked Twilio.
```

## Prompt 8: Production readiness (findings 9, 11)

```
Make the mortgage service deployable:

1. Rename the project in pyproject.toml from "live-dj" to "mac-mortgage-advisor" with an accurate description.
2. Add Dockerfile.mortgage that builds mortgage/ and assets/mortgage/ (the current Dockerfile only builds front_desk),
   runs as a non-root user and starts uvicorn mortgage.server:app on $PORT.
3. Replace in-memory leads/transfers/call records with a repository interface and a Firestore (or SQLite for local)
   implementation, selected by env var. Keep MortgageStore method names unchanged.
4. Strengthen verify_customer: after name match, send a one-time passcode to the phone on file (pluggable sender,
   console sender locally) and only mark verified after verify_passcode(). Return only first name and number of
   properties on verification, not values or credit range.
5. Add a simple shared-secret or signed-token check on the /ws WebSocket, structured JSON logging with PII redaction,
   and a GitHub Actions workflow that runs uv run pytest on every push.
6. Pin LIVE_MODEL to a generally available model in .env.example and document how to upgrade safely.
```
