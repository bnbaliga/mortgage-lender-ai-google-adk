from google.genai import types

from front_desk import usage as usage_mod
from front_desk.usage import ServerTotals, Usage


def report(prompt, out, audio_in=0, text_in=0, audio_out=0, text_out=0):
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt, candidates_token_count=out, total_token_count=prompt + out,
        prompt_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=audio_in),
                               types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=text_in)],
        candidates_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=audio_out),
                                   types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=text_out)])


def test_sums_reports_and_tracks_context():
    u = Usage()
    u.add(report(1000, 200, audio_in=100, text_in=900, audio_out=180, text_out=20))
    u.add(report(1500, 300, audio_in=400, text_in=1100, audio_out=290, text_out=10))
    s = u.snapshot()
    assert (s["input"], s["output"], s["total"], s["reports"]) == (2500, 500, 3000, 2)
    assert s["context"] == 1500                       # the latest prompt, not the sum
    assert s["by_modality"] == {"in:AUDIO": 500, "in:TEXT": 2000, "out:AUDIO": 470, "out:TEXT": 30}


def test_missing_fields_are_zero():
    u = Usage()
    u.add(types.GenerateContentResponseUsageMetadata(prompt_token_count=50))
    assert u.snapshot()["total"] == 50 and u.snapshot()["by_modality"] == {}


def test_cost_only_with_prices(monkeypatch):
    u = Usage()
    u.add(report(1_000_000, 1_000_000, audio_in=1_000_000, audio_out=1_000_000))
    monkeypatch.setattr(usage_mod, "PRICES", {k: None for k in usage_mod.PRICES})
    assert u.cost_usd() is None
    monkeypatch.setattr(usage_mod, "PRICES", {("in", "AUDIO"): 3.0, ("out", "AUDIO"): 12.0,
                                              ("in", "TEXT"): None, ("out", "TEXT"): None})
    assert u.cost_usd() == 15.0


def test_server_totals_accumulate_calls():
    t = ServerTotals()
    for _ in range(2):
        u = Usage()
        u.add(report(100, 10, text_in=100, audio_out=10))
        t.add_call(u)
    s = t.snapshot()
    assert s["calls"] == 2 and s["total"] == 220 and "context" not in s
