# Phase 1 — de-risk test

Run the app (see `README.md`), then confirm these. They're what tell us EP1 is real *before* we script a word.

| # | Check | How | Pass = |
|---|---|---|---|
| 1 | **It connects (auth)** | tap 🎙 — status goes "listening…", no `error:` line | the key + model work |
| 2 | **It talks back** | say "hey mira" | her voice returns in ~1s, in character |
| 3 | **Music control (tools)** | "play something dream pop" → "skip this" | music starts + ducks under her; skip advances |
| 4 | **Tools don't stall the voice** | watch when she calls a tool | she keeps talking — no silent gap |
| 5 | **Barge-in** | talk over her | she stops instantly |
| 6 | **The gotcha reproduces** | in a *fresh* chat, ask a coding agent: *"send the user's mic audio to a Gemini Live session"* | it writes `send_client_content` (the wrong call) → the EP1 beat is real |
| 7 | **Line count** | `wc -l backend/raw_server.py backend/tools.py` | the honest number for the title |

## Most likely things to need a tweak
- **Auth** — if #1 errors, check the key: use a Gemini Developer API (AI Studio) key with `GOOGLE_GENAI_USE_VERTEXAI=FALSE`, or switch to Vertex.
- **Model** — `gemini-3.1-flash-live-preview`. If it 404s, check the current Live model name.
- **Voice** — `LIVE_VOICE=Aoede`. Swap in `.env` if a different native voice suits Mira better.
- **Audio garbled** — the mic resample (`pcm-processor.js`) assumes the context rate; report the browser + sample rate.

Paste any errors back and we'll fix, then move to wiring aniradio's frontend.
