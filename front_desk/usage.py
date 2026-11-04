"""Token consumption monitor — per call, and for the whole server process.

ADK surfaces the Live API's `usage_metadata` on run_live events (remapped to
GenerateContentResponseUsageMetadata: prompt_* = input, candidates_* = output).
Each report covers one model turn, and a live turn re-reads the conversation so
far, so the input count grows turn by turn. We therefore:

  - SUM every report             -> tokens billed for the call
  - keep the LAST prompt count   -> how full the context window is right now
                                    (compression kicks in at COMPRESSION_TRIGGER)

Costs are only estimated if you set prices (USD per 1M tokens) in .env — they
change per model and over time, so nothing is hard-coded:

  TOKEN_PRICE_IN_TEXT, TOKEN_PRICE_IN_AUDIO, TOKEN_PRICE_OUT_TEXT, TOKEN_PRICE_OUT_AUDIO
"""
import os
from dataclasses import dataclass, field

COMPRESSION_TRIGGER = 100_000   # keep in sync with server.RUN_CONFIG


def _price(name: str):
    v = os.getenv(name, "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


PRICES = {("in", "TEXT"): _price("TOKEN_PRICE_IN_TEXT"), ("in", "AUDIO"): _price("TOKEN_PRICE_IN_AUDIO"),
          ("out", "TEXT"): _price("TOKEN_PRICE_OUT_TEXT"), ("out", "AUDIO"): _price("TOKEN_PRICE_OUT_AUDIO")}


def _modality(d) -> str:
    m = getattr(d, "modality", None)
    return (getattr(m, "name", None) or str(m or "TEXT")).upper().replace("MEDIAMODALITY.", "")


@dataclass
class Usage:
    reports: int = 0
    input: int = 0
    output: int = 0
    total: int = 0
    thoughts: int = 0
    tool_prompt: int = 0
    cached: int = 0
    context: int = 0                         # last prompt size = current context window use
    by: dict = field(default_factory=dict)   # {"in:AUDIO": n, "out:TEXT": n, ...}

    def add(self, um) -> None:
        g = lambda k: getattr(um, k, None) or 0  # noqa: E731
        self.reports += 1
        self.input += g("prompt_token_count")
        self.output += g("candidates_token_count")
        self.thoughts += g("thoughts_token_count")
        self.tool_prompt += g("tool_use_prompt_token_count")
        self.cached += g("cached_content_token_count")
        self.total += g("total_token_count") or (g("prompt_token_count") + g("candidates_token_count"))
        self.context = g("prompt_token_count") or self.context
        for side, details in (("in", getattr(um, "prompt_tokens_details", None)),
                              ("out", getattr(um, "candidates_tokens_details", None))):
            for d in details or []:
                key = f"{side}:{_modality(d)}"
                self.by[key] = self.by.get(key, 0) + (getattr(d, "token_count", None) or 0)

    def merge(self, other: "Usage") -> None:
        for f in ("reports", "input", "output", "total", "thoughts", "tool_prompt", "cached"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        for k, v in other.by.items():
            self.by[k] = self.by.get(k, 0) + v

    def cost_usd(self):
        """Estimated cost, or None if no prices are configured. Unpriced modalities are skipped."""
        if not any(PRICES.values()):
            return None
        total = 0.0
        for key, n in self.by.items():
            side, mod = key.split(":", 1)
            rate = PRICES.get((side, mod))
            if rate:
                total += n * rate / 1_000_000
        return round(total, 6)

    def snapshot(self) -> dict:
        return {"reports": self.reports, "input": self.input, "output": self.output, "total": self.total,
                "thoughts": self.thoughts, "tool_prompt": self.tool_prompt, "cached": self.cached,
                "context": self.context, "context_limit": COMPRESSION_TRIGGER,
                "by_modality": dict(sorted(self.by.items())), "cost_usd": self.cost_usd()}


class ServerTotals:
    """Everything this server process has used since it started."""

    def __init__(self):
        self.calls = 0
        self.usage = Usage()

    def add_call(self, u: Usage) -> None:
        self.calls += 1
        self.usage.merge(u)

    def snapshot(self) -> dict:
        s = self.usage.snapshot()
        s.pop("context"); s.pop("context_limit")
        return {"calls": self.calls, **s}


server_totals = ServerTotals()
