"""Mac as an ADK agent: mortgage tools + policy door + audit trail.

Same shape as the front desk agent — an `Agent` with plain-function tools and
a `before_tool_callback`. `adk web .` picks this up as `mortgage`.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from mortgage.persona import instruction
from mortgage.policy import audit, mortgage_gate
from mortgage.tools import ALL_TOOLS

LIVE_MODEL = os.getenv("LIVE_MODEL", "gemini-3.1-flash-live-preview")
VOICE = os.getenv("MORTGAGE_VOICE") or os.getenv("LIVE_VOICE", "Aoede")

root_agent = Agent(
    model=Gemini(
        model=LIVE_MODEL,
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
    ),
    name="mortgage",
    description="AI Mortgage Advisor: answers mortgage questions, checks eligibility, explains products, transfers to loan officers.",
    instruction=instruction,
    tools=ALL_TOOLS,
    before_tool_callback=mortgage_gate,
    after_tool_callback=audit,
)
