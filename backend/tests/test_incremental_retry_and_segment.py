"""Unit tests for:
  1. Incremental retry — _editor_pass invalidates ONLY the segments whose text
     actually changed (diff-checked), so a produce retry reuses cached assets.
  2. Create-from-script LLM segmentation — run_script_segment_job normalizes the
     model output into chunks + characters + character_sheet.
  3. CPU model auto-selection by free RAM (qwen2.5:72b vs deepseek-r1:32b).
"""
import asyncio

import pytest


# ── 1. Editor pass: surgical, diff-checked cache invalidation ──────────────────

def test_editor_pass_only_invalidates_changed_segments(monkeypatch):
    import pipeline

    story_id = "storyX"
    chunks = [
        {"voiceover": "line0", "visual": "v0", "video_prompt": "p0"},
        {"voiceover": "line1", "visual": "v1", "video_prompt": "p1"},
        {"voiceover": "line2", "visual": "v2", "video_prompt": "p2"},
    ]
    story = {"_id": story_id, "script": {"chunks": [dict(c) for c in chunks]}}

    # Editor changes ONLY segment 1's voiceover; segment 0 correction is IDENTICAL
    # (must be treated as a no-op → not invalidated).
    async def fake_editor_pass(_story, _channel, _src):
        return ({
            "corrections": [
                {"index": 0, "voiceover": "line0"},          # unchanged → no invalidation
                {"index": 1, "voiceover": "line1-NEW"},      # changed  → invalidate seg 1
            ],
            "viral_score": {"total": 80},
        }, 0.0)

    invalidated = {}

    def fake_invalidate(sid, changed):
        invalidated[sid] = set(changed)

    async def fake_save_cost(*a, **k):
        return None

    async def fake_source_text(_story):
        return "source"

    monkeypatch.setattr(pipeline.agents, "editor_pass", fake_editor_pass)
    monkeypatch.setattr(pipeline, "_invalidate_caches", fake_invalidate)
    monkeypatch.setattr(pipeline, "_save_cost", fake_save_cost)
    monkeypatch.setattr(pipeline, "_source_text", fake_source_text)

    saved = {}

    async def set_story(**kw):
        saved.update(kw)

    async def run():
        return await pipeline._editor_pass(story_id, story, {}, set_story)

    new_chunks, editor = asyncio.get_event_loop().run_until_complete(run())

    # Only segment 1 should have been invalidated (segment 0 correction was identical).
    assert invalidated.get(story_id) == {1}, invalidated
    assert new_chunks[0]["voiceover"] == "line0"
    assert new_chunks[1]["voiceover"] == "line1-NEW"
    assert new_chunks[2]["voiceover"] == "line2"


def test_editor_pass_no_changes_invalidates_nothing(monkeypatch):
    """A retry where the editor returns the same corrections must reuse ALL assets."""
    import pipeline

    story_id = "storyY"
    story = {"_id": story_id, "script": {"chunks": [
        {"voiceover": "a", "visual": "b", "video_prompt": "c"},
    ]}}

    async def fake_editor_pass(_s, _c, _src):
        return ({"corrections": [{"index": 0, "voiceover": "a"}]}, 0.0)  # identical

    called = {"n": 0}

    def fake_invalidate(sid, changed):
        called["n"] += 1

    async def noop(*a, **k):
        return None

    async def fake_source_text(_s):
        return ""

    monkeypatch.setattr(pipeline.agents, "editor_pass", fake_editor_pass)
    monkeypatch.setattr(pipeline, "_invalidate_caches", fake_invalidate)
    monkeypatch.setattr(pipeline, "_save_cost", noop)
    monkeypatch.setattr(pipeline, "_source_text", fake_source_text)

    async def set_story(**kw):
        return None

    asyncio.get_event_loop().run_until_complete(
        pipeline._editor_pass(story_id, story, {}, set_story))
    assert called["n"] == 0, "no diff → _invalidate_caches must NOT be called"


# ── 3. CPU model selection by free RAM ─────────────────────────────────────────

def test_pick_creative_model_by_ram(monkeypatch):
    from services import llm

    monkeypatch.setattr(llm, "_free_ram_gb", lambda: 60.0)
    assert llm.pick_creative_model() == llm.ollama_main_model()      # qwen2.5:72b

    monkeypatch.setattr(llm, "_free_ram_gb", lambda: 20.0)
    assert llm.pick_creative_model() == llm.ollama_reasoning_model()  # deepseek-r1:32b


def test_ollama_payload_is_cpu_only(monkeypatch):
    """Every Ollama text call must pin num_gpu=0 so the GPU stays free for Studio."""
    from services import llm

    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": '{"ok": true}'}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["payload"] = json
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    out = asyncio.get_event_loop().run_until_complete(
        llm._ollama_json("sys", "prompt", model="deepseek-r1:32b"))
    assert out == {"ok": True}
    assert captured["payload"]["options"]["num_gpu"] == 0
    assert captured["payload"]["model"] == "deepseek-r1:32b"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
