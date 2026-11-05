"""The mortgage advisor backend: browser <-> ADK run_live <-> Gemini Live.

Same loop as the front desk: LiveRequestQueue up, runner.run_live() down,
and the UI fed from the policy door.

WS /ws contract:
  up:   binary 16 kHz PCM · {"type":"text","text":...}
  down: binary 24 kHz PCM · transcript · interrupted · turn_complete · error
        · action (every policy verdict) · tool_result (what each tool returned)
        · usage (running token totals for this call)
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from google.adk.agents import LiveRequestQueue
from google.adk.agents.run_config import StreamingMode
from google.adk.runners import RunConfig, Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from mortgage import events, tools
from mortgage.agent import LIVE_MODEL, VOICE, root_agent
from mortgage.persona import AGENT_NAME
from mortgage.usage import COMPRESSION_TRIGGER, Usage, server_totals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mortgage")

APP_NAME = "mortgage-advisor"
GREET = os.getenv("MORTGAGE_GREETING", "1") != "0"
GREETING_CUE = "(The caller's line just connected. Greet them now.)"

session_service = InMemorySessionService()
runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

RUN_CONFIG = RunConfig(
    streaming_mode=StreamingMode.BIDI,
    response_modalities=["AUDIO"],
    speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    context_window_compression=types.ContextWindowCompressionConfig(
        trigger_tokens=COMPRESSION_TRIGGER, sliding_window=types.SlidingWindow(target_tokens=60_000)),
)

app = FastAPI(title="HomeFirst AI Lending · Mac Mortgage Advisor")
FRONTEND = Path(__file__).resolve().parent / "frontend"


@app.get("/healthz")
async def healthz():
    return {"ok": True, "model": LIVE_MODEL, "voice": VOICE}


@app.get("/api/company")
async def company():
    return {
        "company": tools.store.info["name"],
        "agent": AGENT_NAME,
        "phone": tools.store.info["phone"],
        "nmls": tools.store.info.get("nmls_id", ""),
        "tagline": tools.store.info.get("tagline", "")
    }


@app.get("/api/products")
async def products():
    return {"products": list(tools.store.products.keys())}


@app.get("/api/usage")
async def usage_totals():
    """Tokens used by every finished call since this server started."""
    return server_totals.snapshot()


def _friendly(exc: BaseException) -> str:
    s = f"{type(exc).__name__}: {exc}"
    if "API key" in s or "API_KEY" in s or "401" in s or "403" in s:
        return "The Gemini API key was rejected — check GOOGLE_API_KEY in .env (AI Studio key, not Vertex)."
    if "404" in s and "model" in s.lower():
        return f"The live model '{LIVE_MODEL}' wasn't found — check LIVE_MODEL in .env."
    return s


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    session = await session_service.create_session(app_name=APP_NAME, user_id="caller")
    call_id = session.id
    feed_q = events.subscribe(call_id)
    queue = LiveRequestQueue()
    usage = Usage()
    log.info("call %s connected (model=%s, voice=%s)", call_id[:8], LIVE_MODEL, VOICE)

    async def send(obj: dict):
        await websocket.send_text(json.dumps(obj, default=str))

    async def upstream():
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("bytes"):
                queue.send_realtime(types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000"))
            elif msg.get("text"):
                try:
                    m = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if m.get("type") == "text" and str(m.get("text", "")).strip():
                    text = str(m["text"]).strip()[:1000]
                    await send({"type": "transcript", "role": "user", "text": text, "final": True, "typed": True})
                    queue.send_content(types.Content(role="user", parts=[types.Part.from_text(text=text)]))

    async def downstream():
        async for event in runner.run_live(user_id=session.user_id, session_id=call_id,
                                           live_request_queue=queue, run_config=RUN_CONFIG):
            for role, attr in (("user", "input_transcription"), ("agent", "output_transcription")):
                t = getattr(event, attr, None)
                if t and getattr(t, "text", None):
                    await send({"type": "transcript", "role": role, "text": t.text,
                                "final": bool(getattr(t, "finished", False))})
            content = getattr(event, "content", None)
            for part in (getattr(content, "parts", None) or []):
                idata = getattr(part, "inline_data", None)
                if idata and getattr(idata, "data", None) and (idata.mime_type or "").startswith("audio"):
                    await websocket.send_bytes(idata.data)
            if getattr(event, "interrupted", None):
                await send({"type": "interrupted"})
            if getattr(event, "turn_complete", None):
                await send({"type": "turn_complete"})
            um = getattr(event, "usage_metadata", None)
            if um:
                usage.add(um)
                await send({"type": "usage", **usage.snapshot()})

    async def feed():
        while True:
            await send(await feed_q.get())

    if GREET:
        queue.send_content(types.Content(role="user", parts=[types.Part.from_text(text=GREETING_CUE)]))

    up = asyncio.create_task(upstream(), name="upstream")
    down = asyncio.create_task(downstream(), name="downstream")
    fd = asyncio.create_task(feed(), name="feed")
    try:
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        queue.close()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                log.error("call %s: %s failed: %r", call_id[:8], t.get_name(), exc)
                try:
                    await send({"type": "error", "message": _friendly(exc)})
                except Exception:
                    pass
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        fd.cancel()
        await asyncio.gather(fd, return_exceptions=True)
        queue.close()
        events.unsubscribe(call_id)
        tools.store.end_call(call_id)
        server_totals.add_call(usage)
        cost = usage.cost_usd()
        log.info("call %s ended — tokens: %d total (%d in / %d out) over %d turns%s; by modality %s",
                 call_id[:8], usage.total, usage.input, usage.output, usage.reports,
                 f", est. ${cost:.4f}" if cost is not None else "", usage.by)


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
