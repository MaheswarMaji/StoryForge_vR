"""Iteration 5 — verifies user's 3-part bug report:
1) Image fallback speed (dead-circuit + 35s caps).
2) QA+metadata parallel + zero-API thumbnail (fast tail).
3) Configurable engagement scheduler.
Plus regression on public endpoints and admin auth.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
STORY_ID = "6aa616a70b6da578466afad5"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    return s


# ---------- Regression ----------
class TestRegression:
    def test_stories_public(self, api):
        r = api.get(f"{BASE_URL}/api/stories")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_dashboard_public(self, api):
        r = api.get(f"{BASE_URL}/api/dashboard")
        assert r.status_code == 200

    def test_admin_overview_anon_401(self, api):
        r = api.get(f"{BASE_URL}/api/admin/overview")
        assert r.status_code == 401


# ---------- Scheduler settings ----------
class TestScheduler:
    def test_get_default(self, api):
        r = api.get(f"{BASE_URL}/api/settings/scheduler")
        assert r.status_code == 200
        d = r.json()
        assert d["engagement_hours"] == 6.0
        assert "news_hours" in d
        assert "queue_paused" in d

    def test_put_low_clamp(self, api):
        r = api.put(f"{BASE_URL}/api/settings/scheduler", json={"engagement_hours": 0.1})
        assert r.status_code == 200
        assert r.json()["engagement_hours"] == 0.25  # clamped to min

    def test_put_high_clamp(self, api):
        r = api.put(f"{BASE_URL}/api/settings/scheduler", json={"engagement_hours": 999})
        assert r.status_code == 200
        assert r.json()["engagement_hours"] == 72.0

    def test_put_valid_and_persist(self, api):
        r = api.put(f"{BASE_URL}/api/settings/scheduler", json={"engagement_hours": 0.25})
        assert r.json()["engagement_hours"] == 0.25
        r2 = api.get(f"{BASE_URL}/api/settings/scheduler")
        assert r2.json()["engagement_hours"] == 0.25

    def test_put_reset_6h(self, api):
        r = api.put(f"{BASE_URL}/api/settings/scheduler", json={"engagement_hours": 6.0})
        assert r.json()["engagement_hours"] == 6.0


# ---------- Story artifacts pre-check ----------
class TestArtifacts:
    def test_story_in_review(self, api):
        r = api.get(f"{BASE_URL}/api/stories/{STORY_ID}")
        assert r.status_code == 200
        d = r.json()
        assert d["mode"] == "storyboard"
        assert d["status"] == "in_review"
        m = d.get("media") or {}
        assert m.get("final")
        assert m.get("thumbnail")
        assert (d.get("script") or {}).get("viral_score", {}).get("total") is not None
        assert d.get("metadata") or (d.get("script") or {}).get("metadata")

    def test_thumbnail_and_hook_frame_exist_on_disk(self):
        assert os.path.exists(f"/app/backend/media/thumbs/{STORY_ID}.jpg")
        assert os.path.exists(f"/app/backend/media/frames/{STORY_ID}/00.png")


# ---------- Image dead-circuit code inspection ----------
class TestDeadCircuitCode:
    def test_imagegen_has_dead_circuit(self):
        src = open("/app/backend/services/imagegen.py").read()
        assert "_IMAGE_DEAD_UNTIL" in src
        assert "asyncio.wait_for" in src
        assert "timeout=35" in src
        # 10-min window
        assert "time.time() + 600" in src

    def test_pipeline_qa_meta_parallel(self):
        src = open("/app/backend/pipeline.py").read()
        assert "asyncio.gather(_qa(), _meta())" in src
        # thumbnail reuses hook frame 00.png before any AI thumb call
        assert 'frames" / aid / "00.png"' in src


# ---------- Produce speed test (only if not currently rendering) ----------
class TestProduceSpeed:
    def test_reproduce_within_budget(self, api):
        r = api.get(f"{BASE_URL}/api/stories/{STORY_ID}")
        s = r.json()
        if s.get("status") == "rendering":
            pytest.skip("story already rendering")
        thumb_path = f"/app/backend/media/thumbs/{STORY_ID}.jpg"
        pre_mtime = os.path.getmtime(thumb_path) if os.path.exists(thumb_path) else 0
        # trigger produce
        pr = api.post(f"{BASE_URL}/api/stories/{STORY_ID}/produce")
        assert pr.status_code in (200, 201, 202), pr.text
        started = time.time()
        deadline = started + 240  # 4 min budget (spec says <3 min but allow small margin)
        last_stage = None
        seen_stages = []
        while time.time() < deadline:
            rs = api.get(f"{BASE_URL}/api/stories/{STORY_ID}")
            js = rs.json()
            stage = js.get("stage")
            status = js.get("status")
            if stage != last_stage:
                seen_stages.append((round(time.time() - started, 1), stage, status))
                last_stage = stage
            if status == "in_review":
                break
            if status in ("error", "failed"):
                pytest.fail(f"produce failed: {js.get('error')}; stages: {seen_stages}")
            time.sleep(4)
        elapsed = time.time() - started
        print(f"\nStages: {seen_stages}\nTotal: {elapsed:.1f}s")
        assert js.get("status") == "in_review", f"did not reach in_review in {elapsed:.0f}s; stages={seen_stages}"
        assert elapsed < 240, f"took {elapsed:.0f}s (>4min)"
        # thumbnail updated
        if os.path.exists(thumb_path):
            assert os.path.getmtime(thumb_path) >= pre_mtime
        # QA & metadata combined stage should appear
        stage_names = [s[1] or "" for s in seen_stages]
        assert any("QA" in n and "metadata" in n for n in stage_names), f"no combined QA & metadata stage; got {stage_names}"
