"""Ava as an ADK agent: the clinic's tools + ONE policy door + an audit trail.

Same shape as EP3's DJ (adk_tools/agent.py) — an `Agent` with plain-function
tools and a `before_tool_callback` — with the toy quiet-hours rule replaced by
the real front-desk rules in policy.py. `adk web .` picks this up as `front_desk`.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from front_desk.persona import instruction
from front_desk.policy import audit, front_desk_gate
from front_desk.tools import ALL_TOOLS

LIVE_MODEL = os.getenv("LIVE_MODEL", "gemini-3.1-flash-live-preview")
VOICE = os.getenv("FRONT_DESK_VOICE") or os.getenv("LIVE_VOICE", "Aoede")

root_agent = Agent(
    # Gemini(...) carries the voice with the agent, so `adk web` uses it too.
    model=Gemini(
        model=LIVE_MODEL,
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
    ),
    name="front_desk",
    description="Voice front desk for a family clinic: scheduling, identity checks, clinic info, refills.",
    instruction=instruction,
    tools=ALL_TOOLS,
    before_tool_callback=front_desk_gate,
    after_tool_callback=audit,
)
