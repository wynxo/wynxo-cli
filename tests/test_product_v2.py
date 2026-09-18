from __future__ import annotations

from types import SimpleNamespace

from wynxo.product.commands import PRIMARY, ProductRepl, autonomy_label
from wynxo.product import health
from wynxo.scope import Mode


def test_public_manual_name_is_ask():
    assert autonomy_label(Mode.MANUAL) == "ASK"
    assert autonomy_label(Mode.REVIEW) == "REVIEW"


def _compat_repl(mode=Mode.MANUAL, voice="warm"):
    repl = ProductRepl.__new__(ProductRepl)
    repl.agent = SimpleNamespace(permissions=SimpleNamespace(mode=mode))
    repl.config = SimpleNamespace(voice=voice)
    return repl


def test_legacy_commands_are_rewritten_to_canonical_surface():
    repl = _compat_repl()
    assert repl._compat("/code") == "/work"
    assert "/work" in PRIMARY
    assert "/code" not in PRIMARY
    assert repl._compat("/ctx 32768") == "/context window 32768"
    assert repl._compat("/repo wynxo/wynxo-cli") == "/github clone wynxo/wynxo-cli"
    assert repl._compat("/gh status") == "/github api status"
    assert repl._compat("/sessions") == "/resume"
    assert repl._compat("/todo") == "/plan"


def test_yolo_and_mommy_compatibility_are_stateful():
    assert _compat_repl()._compat("/yolo") == "/mode yolo"
    assert _compat_repl(Mode.YOLO)._compat("/yolo") == "/mode ask"
    assert _compat_repl(voice="warm")._compat("/mommy") == "/voice mommy"
    assert _compat_repl(voice="mommy")._compat("/mommy") == "/voice plain"


def test_model_health_store_is_bounded_and_descriptive(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "data_dir", lambda: tmp_path)
    for i in range(20):
        health.record("model", tps=10 + i, ttft_ms=100 + i, tool_failures=i % 2)
    summary = health.summary("model")
    assert summary["samples"] == health.MAX_SAMPLES
    assert summary["tps"] > 0
    assert "tok/s" in health.observation_text("model")
