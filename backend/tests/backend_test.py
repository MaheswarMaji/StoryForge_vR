"""StoryForge backend test suite — auth is OPTIONAL now (wall removed).

Focus of THIS iteration:
- New /api/stories/create + /api/video-types
- PATCH /api/stories/{id}/script + PUT /api/stories/{id}/config
- API Keys Vault (GET/PUT/DELETE)
- Admin still gated (/api/admin/* -> 401 anon, 403 non-admin, 200 admin)
- Regression on unauthenticated open endpoints
"""
import os
import time
import copy
import requests
import pytest
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL must be set"

STORY_ID = "6aa57c1eaad23f394fffaa41"  # approved w/ script chunks
MEDIA_ROOT = "/app/backend/media"


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient("mongodb://localhost:27017")
    yield c["test_database"]
    c.close()


@pytest.fixture(scope="module")
def anon():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def non_admin_token(mongo):
    tok = f"test_session_{int(time.time()*1000)}"
    uid = f"test-user-{int(time.time()*1000)}"
    mongo.users.insert_one({"user_id": uid, "email": f"{uid}@example.com",
                            "name": "PyTest QA", "picture": "",
                            "role": "user",
                            "created_at": datetime.now(timezone.utc)})
    mongo.user_sessions.insert_one({"user_id": uid, "session_token": tok,
                                    "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
                                    "created_at": datetime.now(timezone.utc)})
    yield tok
    mongo.user_sessions.delete_many({"session_token": tok})
    mongo.users.delete_many({"user_id": uid})


@pytest.fixture(scope="module")
def admin_token(mongo):
    tok = f"test_admin_session_{int(time.time()*1000)}"
    admin = mongo.users.find_one({"user_id": "user_fba19a29ab52"})
    assert admin is not None, "admin seed user missing"
    mongo.user_sessions.insert_one({"user_id": admin["user_id"], "session_token": tok,
                                    "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
                                    "created_at": datetime.now(timezone.utc)})
    yield tok
    mongo.user_sessions.delete_many({"session_token": tok})


@pytest.fixture(scope="module")
def non_admin_client(non_admin_token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {non_admin_token}"})
    return s


@pytest.fixture(scope="module")
def admin_client(admin_token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json",
                      "Authorization": f"Bearer {admin_token}"})
    return s


@pytest.fixture(scope="module")
def original_story_chunk0(mongo):
    """Snapshot voiceover chunk 0 so we can restore after PATCH test."""
    s = mongo.stories.find_one({"_id": STORY_ID})
    chunks = ((s or {}).get("script") or {}).get("chunks") or []
    orig = copy.deepcopy(chunks[0]) if chunks else None
    orig_mode = (s or {}).get("mode", "slide")
    yield orig, orig_mode
    # restore
    s2 = mongo.stories.find_one({"_id": STORY_ID})
    chunks2 = ((s2 or {}).get("script") or {}).get("chunks") or []
    if orig is not None and chunks2:
        chunks2[0] = orig
        mongo.stories.update_one({"_id": STORY_ID},
                                 {"$set": {"script.chunks": chunks2,
                                           "mode": orig_mode,
                                           "status": "approved",
                                           "stage": "Ready", "error": ""}})


# ---------- 1. Video types registry ----------
class TestVideoTypes:
    def test_video_types_list(self, anon):
        r = anon.get(f"{BASE_URL}/api/video-types", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 8, f"expected 8 types, got {len(data)}"
        keys = {d["key"] for d in data}
        expected = {"mythology_moral", "folk_horror", "motivational", "kids_fables",
                    "educational", "business", "farming", "tech"}
        assert keys == expected, f"missing types: {expected - keys}"
        for d in data:
            for k in ("name", "audience", "language", "voice", "music_mood", "style_prefix"):
                assert k in d, f"video-type {d.get('key')} missing {k}"


# ---------- 2. Create story flow ----------
class TestCreateStory:
    def test_create_tech_clip(self, anon, mongo):
        payload = {"title": "TEST_tech_clip", "source_text": "AI is eating software. Test text.",
                   "video_type": "tech", "length_seconds": 45, "mode": "clip"}
        r = anon.post(f"{BASE_URL}/api/stories/create", json=payload, timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "story_id" in d and "job_id" in d
        sid = d["story_id"]
        # verify GET story
        gr = anon.get(f"{BASE_URL}/api/stories/{sid}", timeout=30)
        assert gr.status_code == 200
        story = gr.json()
        assert story.get("mode") == "clip"
        assert story.get("target_seconds") == 45
        # channel key
        ch = mongo.channels.find_one({"_id": story["channel_id"]})
        assert ch and ch.get("key") == "vt-tech"
        # job appears
        jr = anon.get(f"{BASE_URL}/api/jobs", timeout=30)
        assert jr.status_code == 200
        assert any(j.get("_id") == d["job_id"] or j.get("id") == d["job_id"] for j in jr.json())
        # cleanup
        mongo.stories.delete_one({"_id": sid})
        mongo.jobs.delete_one({"_id": d["job_id"]})

    def test_create_empty_source_400(self, anon):
        r = anon.post(f"{BASE_URL}/api/stories/create",
                      json={"source_text": "", "video_type": "tech"}, timeout=30)
        assert r.status_code == 400

    def test_create_unknown_type_400(self, anon):
        r = anon.post(f"{BASE_URL}/api/stories/create",
                      json={"source_text": "abc", "video_type": "bogus_type"}, timeout=30)
        assert r.status_code == 400

    def test_create_length_clamped(self, anon, mongo):
        r = anon.post(f"{BASE_URL}/api/stories/create",
                      json={"title": "TEST_clamp", "source_text": "clamp test",
                            "video_type": "educational", "length_seconds": 9999,
                            "mode": "slide"}, timeout=30)
        assert r.status_code == 200
        sid = r.json()["story_id"]
        jid = r.json()["job_id"]
        gr = anon.get(f"{BASE_URL}/api/stories/{sid}", timeout=30)
        assert gr.status_code == 200
        assert gr.json().get("target_seconds") == 240
        mongo.stories.delete_one({"_id": sid})
        mongo.jobs.delete_one({"_id": jid})


# ---------- 3. Script PATCH ----------
class TestPatchScript:
    def test_patch_edits_and_invalidates_cache(self, anon, mongo, original_story_chunk0):
        orig, _mode = original_story_chunk0
        assert orig is not None
        new_vo = f"TEST_edited_voiceover_{int(time.time())}"
        r = anon.patch(f"{BASE_URL}/api/stories/{STORY_ID}/script",
                       json={"chunks": [{"index": 0, "voiceover": new_vo}]}, timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("ok") is True
        assert 0 in d.get("edited", [])
        # verify stored
        gr = anon.get(f"{BASE_URL}/api/stories/{STORY_ID}", timeout=30)
        assert gr.status_code == 200
        chunks = (gr.json().get("script") or {}).get("chunks") or []
        assert chunks[0]["voiceover"] == new_vo
        # audio cache unlinked (file for index 0 should NOT exist now)
        audio0 = os.path.join(MEDIA_ROOT, "audio", STORY_ID, "00.mp3")
        assert not os.path.exists(audio0), f"cache not invalidated: {audio0} still exists"

    def test_patch_bad_story_404(self, anon):
        r = anon.patch(f"{BASE_URL}/api/stories/DOES_NOT_EXIST/script",
                       json={"chunks": [{"index": 0, "voiceover": "x"}]}, timeout=30)
        assert r.status_code == 404


# ---------- 4. Config PUT (mode + length) ----------
class TestStoryConfig:
    def test_switch_to_clip_then_slide(self, anon, mongo):
        # mode -> clip
        r = anon.put(f"{BASE_URL}/api/stories/{STORY_ID}/config",
                     json={"mode": "clip"}, timeout=30)
        assert r.status_code == 200, r.text
        assert r.json().get("mode") == "clip"
        clips_dir = os.path.join(MEDIA_ROOT, "clips", STORY_ID)
        assert not os.path.isdir(clips_dir), "clips dir should be wiped after mode switch"
        # switch back to slide
        r2 = anon.put(f"{BASE_URL}/api/stories/{STORY_ID}/config",
                      json={"mode": "slide"}, timeout=30)
        assert r2.status_code == 200
        gr = anon.get(f"{BASE_URL}/api/stories/{STORY_ID}", timeout=30)
        assert gr.json().get("mode") == "slide"

    def test_target_seconds_clamped(self, anon, mongo):
        r = anon.put(f"{BASE_URL}/api/stories/{STORY_ID}/config",
                     json={"target_seconds": 9999}, timeout=30)
        assert r.status_code == 200
        assert r.json().get("target_seconds") == 240
        # low clamp
        r2 = anon.put(f"{BASE_URL}/api/stories/{STORY_ID}/config",
                      json={"target_seconds": 5}, timeout=30)
        assert r2.json().get("target_seconds") == 30
        # restore something reasonable
        anon.put(f"{BASE_URL}/api/stories/{STORY_ID}/config",
                 json={"target_seconds": 90}, timeout=30)


# ---------- 5. API Keys Vault ----------
class TestApiKeysVault:
    ALL_KEYS = {"OPENAI_API_KEY", "GEMINI_API_KEY", "FAL_KEY", "HF_TOKEN",
                "REPLICATE_API_TOKEN", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                "YOUTUBE_REFRESH_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"}

    def test_get_keys_masked(self, anon):
        r = anon.get(f"{BASE_URL}/api/settings/api-keys", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert set(d.keys()) == self.ALL_KEYS, f"missing keys: {self.ALL_KEYS - set(d.keys())}"
        for k, v in d.items():
            assert set(v.keys()) == {"set", "hint"}, f"{k} extra fields: {v.keys()}"
            # hint must be short (never full key)
            assert len(v["hint"]) <= 20

    def test_put_and_delete_hf_token(self, anon):
        fake = "hf_fake_TESTABCDEF123456"
        r = anon.put(f"{BASE_URL}/api/settings/api-keys",
                     json={"values": {"HF_TOKEN": fake}}, timeout=30)
        assert r.status_code == 200
        assert "HF_TOKEN" in r.json().get("saved", [])
        # GET shows it set, and no raw value leaked
        gr = anon.get(f"{BASE_URL}/api/settings/api-keys", timeout=30)
        row = gr.json()["HF_TOKEN"]
        assert row["set"] is True
        assert fake not in str(gr.json()), "raw HF_TOKEN leaked in GET response"
        # DELETE
        dr = anon.delete(f"{BASE_URL}/api/settings/api-keys/HF_TOKEN", timeout=30)
        assert dr.status_code == 200
        assert dr.json().get("cleared") == "HF_TOKEN"
        gr2 = anon.get(f"{BASE_URL}/api/settings/api-keys", timeout=30)
        assert gr2.json()["HF_TOKEN"]["set"] is False

    def test_put_rejects_unknown_key(self, anon):
        r = anon.put(f"{BASE_URL}/api/settings/api-keys",
                     json={"values": {"BOGUS_KEY": "x"}}, timeout=30)
        assert r.status_code == 200
        assert r.json().get("saved", []) == []

    def test_delete_unknown_key_400(self, anon):
        r = anon.delete(f"{BASE_URL}/api/settings/api-keys/BOGUS_KEY", timeout=30)
        assert r.status_code == 400


# ---------- 6. Router status ----------
class TestRouterStatus:
    def test_status_new_providers(self, anon):
        r = anon.get(f"{BASE_URL}/api/router-status", timeout=30)
        assert r.status_code == 200
        blob = str(r.json()).lower()
        for prov in ("gemini_veo", "hf_flux", "kokoro", "kenburns"):
            assert prov in blob, f"provider {prov} missing from router-status"


# ---------- 7. Admin gating regression ----------
class TestAdminGating:
    def test_admin_overview_401_anon(self, anon):
        r = anon.get(f"{BASE_URL}/api/admin/overview", timeout=30)
        assert r.status_code == 401

    def test_admin_overview_403_non_admin(self, non_admin_client):
        r = non_admin_client.get(f"{BASE_URL}/api/admin/overview", timeout=30)
        assert r.status_code == 403

    def test_admin_overview_200_admin(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/admin/overview", timeout=60)
        assert r.status_code == 200
        for k in ("totals", "accounts", "channels", "videos"):
            assert k in r.json()


# ---------- 8. Regression: open endpoints ----------
class TestOpenEndpoints:
    def test_stories(self, anon):
        r = anon.get(f"{BASE_URL}/api/stories", timeout=30)
        assert r.status_code == 200 and isinstance(r.json(), list)

    def test_dashboard(self, anon):
        r = anon.get(f"{BASE_URL}/api/dashboard", timeout=30)
        assert r.status_code == 200
        for k in ("status_counts", "cost", "queue", "jobs", "recent_stories"):
            assert k in r.json()

    def test_channels(self, anon):
        r = anon.get(f"{BASE_URL}/api/channels", timeout=30)
        assert r.status_code == 200
        # 8 seeded video-type channels expected (may include legacy ones too)
        assert len(r.json()) >= 8

    def test_media_health(self, anon):
        r = anon.get(f"{BASE_URL}/api/media-health", timeout=30)
        assert r.status_code == 200
