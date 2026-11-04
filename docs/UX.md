# live-dj — UX Design Doc

**Designer:** cuppibla
**Status:** Draft v0.1
**Last updated:** 2026-06-06

---

## 1. The design bet

We're betting that live-dj should **look exactly like aniradio** — same warm-black room, same Mira aurora orb, same tiny-caps calm — and that the orb (which already pulses when Mira *speaks*) can carry the *whole* voice interaction. The hard problem is layering an **active** state — *"Mira is listening to me"* — onto a design built for **passive** listening, while music ducks underneath. We solve it in the orb and one italic status line, **not** by bolting a chat UI onto a beautiful ambient screen.

## 2. The defining interaction

> You tap **Talk** and say "hey Mira, play something for studying." The ring pulses with your voice; the orb stills a beat; then a lofi track fades in, the now-playing strip updates, and she says — warm — "here's something easy." The music **ducks** under her voice. You talk over her — "actually, skip this" — she stops *instantly* and the next track slides in. It feels like calling into a radio station and having the DJ actually do what you asked.

If the barge-in lags or the duck is late, the whole thing feels like a toy.

## 3. Screen inventory

- **The Talk page** — the only screen: connect, talk to Mira, see the transcript.

(One screen. There is nothing else to build.)

## 4. Screen-by-screen specs

### The Talk page

**Purpose:** Let someone have a live voice conversation with Mira, and always know what state that conversation is in.

**Layout (matches aniradio's room, top to bottom):**
1. **Top bar** — `← LOBBY` · `WITH MIRA` · `WEEK OF …`, tiny wide caps (unchanged from aniradio).
2. **The Mira aurora orb** — dead center, the hero; peach→lavender glow. It *is* the state indicator (see States). This is aniradio's existing `MiraOrb`, extended with two states.
3. **Italic status line** — under the orb, aniradio's "opening the room" slot, now showing the voice/now-playing state: *"listening…"* / *"playing porcelain mornings"*.
4. **Minimal controls + mic** — the existing circular pause/skip buttons, plus one **mic toggle in the same style** to start/stop talking. No big "Talk" button.
5. **NEXT / THEN ticker** — what's queued, aniradio's existing ticker.
6. **Credits footer** — unchanged.

No chat panel. If we show captions at all, it's a single fading italic line — never a scrolling log.

**Key interactions:**
- Tap **Talk** (idle) → request mic → connect → enter *listening*.
- You speak → orb pulses with input level.
- You stop → *thinking* (brief) → Mira replies → *speaking*.
- You speak while she's speaking → **barge-in**: playback stops, back to *listening*.
- Tap **Talk** (active) → end session → *idle*.

**States:**
- **Default / idle:** the orb in its slow **breathe** (aniradio's existing idle); status line "tap the mic to talk to Mira."
- **Empty / first-time:** same, plus the **headphones** hint (the one bit of onboarding that matters — §7).
- **Connecting:** orb breathes faster; "waking Mira up…".
- **Listening:** the orb **pulses with your mic level** (new state) — the proof she's hearing you.
- **Thinking:** the orb holds **still** for the ~half-second before her first audio.
- **Speaking:** the orb's **bright, faster pulse** — aniradio's existing `speaking` state, reused verbatim.
- **Music playing:** the now-playing strip is lit; music at full level between her turns.
- **Ducking (speaking + music):** while Mira talks, music drops to a murmur, then restores — the audible proof she's talking *over* the track. Both cues (warm orb + duck) fire together.
- **Error — no mic permission:** orb greyed; "I need mic access to hear you" + retry.
- **Error — no/invalid key:** "No Gemini key found — add it to `.env`."
- **Error — disconnected:** "Lost the line. Tap to reconnect."
- **Edge — long session:** Live sessions cap out (~10–15 min); at the warning, surface "the line's about to drop — tap to reconnect," don't just die.

## 5. The user journey

> First open: you're in Mira's room — the warm-dark stage, her aurora orb breathing, a track already playing (lean-back, exactly aniradio). A quiet line: *"headphones on, then tap the mic."* You plug in, tap the mic. The orb shifts to a soft pulse as it picks up your voice; you say, a little self-consciously, "hey — play something chill." The orb stills a beat, a lofi track fades in, and her line rises as an italic caption — *"here's something easy."* You talk over her, "skip this," and she stops dead, the next track sliding in. *That's* the moment it lands: it's not a chatbot with a voice, it's a station that's actually listening. Second session: enter the room, tap the mic, straight in — nothing new to learn.

## 6. Component & visual notes

**Follow the `listening-room.md` aesthetic (aniradio's canonical design system) — inherit it, don't invent.** It must pass the skill's 5-point test: dark warm canvas · one hero artifact · host presence orb · one ambient motion · broadcast chrome.
- **Canvas:** warm near-black (`#0f0b0a`) with two low-opacity corner radial gradients for depth. Never pure black, never light mode.
- **Hero artifact + host orb (the skill separates them):** a bespoke **hero artifact** on the stage — Mira's bedroom-pop room = the spinning vinyl + aurora glow — and the **Mira orb** as host presence. The *orb* is what we make reactive: **breathe** (idle), **pulse-with-mic** (listening — new), **still** (thinking — new), **bright fast pulse** (speaking — already in `MiraOrb`).
- **One accent:** Mira's peach `#FFB89E` (the skill's "soft mornings/dawn") — orb core, the pulse dot, the caption quote marks. One per screen, nothing else.
- **Broadcast chrome:** tiny UPPERCASE wide-tracked text (10–11px, `letter-spacing 0.25em`) in a **top bar** (`← LOBBY · WITH MIRA · WEEK OF…`) and a **bottom info bar** (`● LIVE · CHANNEL MODE · NEXT: …` + lowercase track title). A **● pulse dot** (accent) starts the bottom status strip.
- **Caption overlay (Mira speaking) — replaces any chat panel:** the skill's transient italic-serif caption fades in above the bottom bar, in curly quotes whose marks render in peach at 50%.
- **Typography:** lowercase serif for track titles · UPPERCASE wide-tracked sans for chrome · italic serif captions. **No Title Case anywhere.**
- **One ambient motion:** bedroom/soft = the single breathing radial orb behind the artifact (already aniradio's). Never a second.
- **Loading is the breathing orb, never a spinner** ("waking Mira up…").
- **Mira's voice:** a Gemini Live *native* voice — not aniradio's Chirp voice (Live can't reproduce it). Persona carries the character; timbre changes.

## 7. Accessibility & inclusion

- **Deaf / hard-of-hearing:** the live transcript *is* a caption track — present from the first frame, not an afterthought. (Audio-only would exclude them; the transcript is non-negotiable.)
- **Motor:** one large tap target; start/stop also on the spacebar. No precise gestures, no press-and-hold.
- **The headphones requirement:** an accessibility *tax* we're deliberately imposing in Phase 1 — without echo cancellation, an open mic over speakers makes Mira hear herself. We state it plainly up front rather than letting the experience silently break. (Phase 2: AEC so headphones are optional.)
- **Low bandwidth:** out of scope for a local-dev Phase 1; flagged for later.

## 8. What we are NOT designing

- **No playlist *generation* UI** — Mira plays from a fixed set of tracks; making new songs isn't a v1 control. No queue editor, no library browser — you *ask*, she plays.
- **No settings screen** — there's nothing to set; the key lives in `.env`.
- **No persona-switcher UI** — swapping DJs is a code edit, not a control.
- **No history / past-conversations view** — every session is fresh (no memory yet).
- **No onboarding flow** — the one headphones hint on the idle screen is the entire onboarding.

## 9. Open design questions

- [ ] **Push-to-talk or always-listening?** Always-listening feels more "live," but pairs badly with speakers (echo). Default to always-listening + headphones, or offer push-to-talk as the safe path?
- [ ] How loud/expressive should the "thinking" beat be — is stillness enough to read as "she heard me," or does it read as "it broke"?

## 10. Handoff to engineering

> The single most important number is **barge-in latency: from your voice to playback stopping must be well under 200ms** — that's the difference between "a real conversation" and "a laggy toy." It needs client-side detection (don't wait for the server's VAD to round-trip).

Open items left for eng: the orb's state machine maps 1:1 to the live session's events (input transcription started / output audio / `interrupted`) — wire the visual states directly to those, not to a guessed timer.
