"""Iteration 9: Voice Preview + Beat-Synced Slides tests."""
import os
import time
import subprocess
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
ADMIN_BEARER = "qa_admin_test-admin-4447"
H = {"Authorization": f"Bearer {ADMIN_BEARER}", "Content-Type": "application/json"}


# --- Voice preview API ---
class TestVoicePreview:
    def test_preview_english_kokoro(self):
        body = {"voice": "kokoro:af_heart", "language": "en", "tone": "Friendly, playful"}
        r = requests.post(f"{BASE_URL}/api/tts/preview", json=body, headers=H, timeout=90)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"
        data = r.json()
        assert "url" in data and data["url"].startswith("/api/media/tmp/voice-preview-")
        # download and ffprobe
        media_r = requests.get(f"{BASE_URL}{data['url']}", timeout=30)
        assert media_r.status_code == 200
        assert len(media_r.content) > 1000
        path = "/tmp/preview_test.mp3"
        with open(path, "wb") as f:
            f.write(media_r.content)
        dur = float(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path]).strip())
        assert 1.5 <= dur <= 15.0, f"duration out of range: {dur}"
        # store url on class
        TestVoicePreview.first_url = data["url"]

    def test_preview_cached_same_body(self):
        body = {"voice": "kokoro:af_heart", "language": "en", "tone": "Friendly, playful"}
        r = requests.post(f"{BASE_URL}/api/tts/preview", json=body, headers=H, timeout=30)
        assert r.status_code == 200
        assert r.json()["url"] == getattr(TestVoicePreview, "first_url", None)

    def test_preview_different_tone_different_url(self):
        body = {"voice": "kokoro:af_heart", "language": "en", "tone": "Somber, slow"}
        r = requests.post(f"{BASE_URL}/api/tts/preview", json=body, headers=H, timeout=90)
        assert r.status_code == 200
        assert r.json()["url"] != getattr(TestVoicePreview, "first_url", None)

    def test_preview_bad_voice_falls_through_or_clean_502(self):
        body = {"voice": "does-not-exist:zzz", "language": "en", "tone": ""}
        r = requests.post(f"{BASE_URL}/api/tts/preview", json=body, headers=H, timeout=90)
        # must be 200 (chain fallback worked) or clean 502 JSON — no 500 / stack trace
        assert r.status_code in (200, 502), f"unexpected {r.status_code}: {r.text[:300]}"
        assert "Traceback" not in r.text
        assert r.headers.get("content-type", "").startswith("application/json")
        if r.status_code == 502:
            assert "detail" in r.json()

    def test_preview_no_auth_still_works(self):
        # Public endpoint (only /api/admin/* is protected). Cached from earlier call → fast.
        r = requests.post(f"{BASE_URL}/api/tts/preview",
                          json={"voice": "kokoro:af_heart", "language": "en",
                                "tone": "Friendly, playful"}, timeout=60)
        assert r.status_code == 200


# --- Beat-sync math ---
class TestBeatSyncMath:
    def test_action_bpm_snap(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.media import MOODS
        beat = 60.0 / MOODS["action"]["bpm"]
        assert abs(beat - 0.4545) < 0.001
        snapped = round(3.0 / beat) * beat
        assert abs(snapped - 3.1818) < 0.01  # 7 beats
        # simulate produce_stitch formula
        dur = max(beat, round(3.0 / beat) * beat)
        assert abs(dur - 3.1818) < 0.01
        # 0.2s should floor to 1 beat
        dur2 = max(beat, round(0.2 / beat) * beat)
        assert abs(dur2 - beat) < 0.001

    def test_devotional_bpm_snap(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from services.media import MOODS
        beat = 60.0 / MOODS["devotional"]["bpm"]  # 0.909
        # 3.0 → round(3/0.909)=3 → 2.727
        dur = max(beat, round(3.0 / beat) * beat)
        assert abs(dur - 2.727) < 0.01


# --- Stitch beat_sync default + off ---
class TestStitchBeatSync:
    def _upload_png(self):
        # generate small PNG via PIL
        from PIL import Image
        p = "/tmp/beatsync_test.png"
        Image.new("RGB", (720, 1280), (60, 90, 140)).save(p)
        with open(p, "rb") as f:
            r = requests.post(f"{BASE_URL}/api/stitch/upload",
                              files={"file": ("t.png", f, "image/png")},
                              headers={"Authorization": f"Bearer {ADMIN_BEARER}"}, timeout=30)
        assert r.status_code == 200, r.text[:300]
        return r.json()["media_id"]

    def test_stitch_default_beat_sync_true(self):
        mid = self._upload_png()
        body = {"title": "TEST_beatsync_default", "video_type": "mythology_moral",
                "items": [{"media_id": mid, "narration": "", "duration": 3.0}],
                "music": False, "endcard": False}  # beat_sync omitted → default true
        r = requests.post(f"{BASE_URL}/api/stitch", json=body, headers=H, timeout=30)
        assert r.status_code == 200, r.text[:300]
        story_id = r.json()["story_id"]
        # verify beat_sync default persisted true
        s = requests.get(f"{BASE_URL}/api/stories/{story_id}", headers=H, timeout=15).json()
        assert s.get("script", {}).get("beat_sync") is True

    def test_stitch_beat_sync_false_renders_unsnapped(self):
        # Uses previously-rendered story from initial run (disk is often 95% full).
        # Story 6aa6891b7099f489606c7a3b was rendered with beat_sync=false, 2 items @3s each.
        # Expected duration: exactly 6.0s (unsnapped, no music, no endcard).
        story_id = "6aa6891b7099f489606c7a3b"
        s = requests.get(f"{BASE_URL}/api/stories/{story_id}", headers=H, timeout=15).json()
        if s.get("status") != "in_review":
            pytest.skip(f"prior story not in_review: {s.get('status')} — retry after disk cleanup")
        assert s["script"]["beat_sync"] is False
        assert s["media"]["duration_sec"] == 6.0, f"unsnapped dur unexpected: {s['media']['duration_sec']}"
        h = requests.head(f"{BASE_URL}{s['media']['final']}", timeout=15)
        assert h.status_code == 200


# --- Regression ---
class TestRegression:
    def test_dashboard_ok(self):
        r = requests.get(f"{BASE_URL}/api/dashboard", headers=H, timeout=15)
        assert r.status_code == 200

    def test_yt_live_unauth_401(self):
        r = requests.get(f"{BASE_URL}/api/admin/youtube/live", timeout=15)
        assert r.status_code == 401

    @pytest.mark.parametrize("sid", [
        "6aa66ac00cdbbc6fd4060168",
        "6aa66b8558ffb9fdd5448232",
        "6aa66d2907c6c0f8bc93e391",
        "6aa6886a2b0899a8dbfe1228",
    ])
    def test_prior_stitch_stories_still_in_review(self, sid):
        r = requests.get(f"{BASE_URL}/api/stories/{sid}", headers=H, timeout=15)
        assert r.status_code == 200, f"{sid}: {r.status_code}"
        j = r.json()
        assert j.get("status") == "in_review", f"{sid}: {j.get('status')}"
        final = j.get("media", {}).get("final")
        assert final
        h = requests.head(f"{BASE_URL}{final}", timeout=15)
        assert h.status_code == 200
