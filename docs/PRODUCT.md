# live-dj — Product Design Doc

**Author:** cuppibla
**Status:** Draft v0.1
**Last updated:** 2026-06-06
**One-liner:** Turn a one-way AI radio into a DJ you can talk to — and tell what to play.

---

## 1. The user & the moment

- **Who:** A developer who just watched "TTS vs Live," already builds with LLM APIs, and wants to feel a *live* voice agent with their own hands — not read about one. (Second audience: the creator on camera, who needs a demo that earns a "whoa.")
- **When:** Right after the episode. They clone the repo, paste a Gemini key, and want to be *talking to something that does something* before they lose interest — call it five minutes.
- **Why now:** Every "voice agent" tutorial result is the bolt-on STT→LLM→TTS pipeline or a framework that hides the wire. Almost nobody hands you the raw Live primitive — voice *and* tools — small enough to read in one sitting.

## 2. The contract (I/O)

- **Input:** Your voice — a mic stream from one web page.
- **Output (talk):** Mira's voice — live, conversational, interruptible. **Her persona carries over from aniradio; her exact voice does not.** aniradio's Mira is a Chirp 3 HD *TTS* voice, and Gemini Live can't reproduce it — she speaks in one of the Live API's native voices, chosen for warmth. Same character, different timbre.
- **Output (do):** When you ask her to, Mira **controls the music** — play a vibe, queue a track, skip, pause — via function calling. The music plays in the page and ducks under her voice when she talks.
- **The loop:** Open → "hey Mira, play something for studying" → music starts, she says a word about it → you talk over her, she stops → "skip this" → next track. A conversation that *does* things.

## 3. The magical moment

> "wait — I just *told my radio* what to play, out loud, and it did it — and the whole thing is a few hundred lines?"

## 4. Scope: what we ARE building (Phase 1)

- A single web page: one "Talk" control, a now-playing strip, a live transcript
- The raw `google-genai` live loop — mic in, Mira's (native) voice out, barge-in
- **Music control via function calling** — `play_playlist` / `play_track` / `skip` / `pause`, returning *instantly* so the voice never stalls
- A music player that plays aniradio's tracks, **ducking** under Mira's voice
- Mira's persona (borrowed from aniradio) as the system instruction
- A deliberately-wrong `send_client_content` example, so the episode's gotcha is real

## 5. Scope: what we are NOT building (in Phase 1)

- **No ADK *yet*** — music control uses the **raw SDK's** function calling; the ADK rebuild is Phase 2 (EP3)
- **No playlist *generation*** — Mira curates from aniradio's existing tracks; generating new Lyria songs is too slow for a live turn
- **No memory** — every session starts fresh; persistence is a later episode
- **No accounts, no deploy, no mobile** — run it locally, one user, one key
- **No persona-switcher UI** — one DJ (Mira); swapping is a one-line code change

## 6. The signature detail

Mira's **persona** is the soul — a late-night radio DJ who talks *between songs*: warm, unhurried, a little intimate (borrowed verbatim from aniradio's `bedroom-pop` persona, "a quiet friend at midnight"). Her **timbre changes** going live — a Gemini Live native voice, not her Chirp TTS voice — but the *character* is hers. That's the honest trade of TTS→Live: you give up voice-library freedom to get real-time audio that hears your tone — and now answers you *and* works the decks.

## 7. Success: how we know it worked

- **Primary:** A developer who clones the repo is **talking to Mira and changing the music by voice within 5 minutes** of pasting their key.
- **Secondary:** the `send_client_content` gotcha reproduces on camera with a current model; and "play lofi" does **not** make Mira go silent (the tool returns instantly).
- **What we're NOT measuring:** stars, forks, "engagement." This is teaching material.

## 8. Open questions

- [ ] Which Live native voice is the closest "Mira"? (Her Chirp voice is gone — pick the warmest native match.)
- [ ] Tool granularity: `play_playlist(mood)` only, or also `play_track(title)` / `skip` / `pause`? (More tools = more to teach; fewer = a thinner demo.)
- [ ] Does a cold coding agent *still* write `send_client_content` for live audio with today's models?
- [ ] Honest line count of the loop + tools? (The title's "~N lines" has to be true.)

## 9. Handoff

- **For UX:** Two states now compete for the orb — *talking* and *music playing*. The hard problem is showing "Mira is listening" while music is playing underneath. And the now-playing strip has to feel like a radio, not a media player.
- **For Eng:** The music tools **must return instantly** (fire the play command, don't await playback) or the voice stalls — that's the whole "tools pause audio" lesson. Build Phase 1 so the backend swaps raw→ADK in Phase 2 **without touching the frontend**.
