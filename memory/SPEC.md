# StoryForge Imported App

## Scope

StoryForge is an AI-powered mythology and folk-story video factory imported from the public repository `MaheswarMaji/StoryForge` without feature changes.

## Key flows

- Production dashboard at `/dashboard` with stories, videos, spend, queue, and pipeline status.
- PDF upload and OCR workflow for creating story projects.
- Script creation, story library, story detail, video production, news desk, social engagement, channels, integrations, settings, and admin routes.
- Each script segment can be edited directly and has independent AI regeneration controls for script, narration voice, and image/video clip. Script regeneration updates the segment text and prompt without automatically spending on media; voice and visual regeneration rebuild the affected segment and re-stitch the master video.
- Human Review → Request Edits requires notes and queues an AI revision job. The job applies targeted changes to affected segments, invalidates their cached media, records the request, and leaves the story ready for re-rendering.
- The Character & Style Consistency Sheet is editable and becomes a locked canonical continuity bible. Saving it preserves narration audio, marks all character references/frames/clips stale, and forces visual regeneration on the next render.
- Every visual prompt receives the full, untruncated continuity bible before the scene prompt. Storyboard mode uses one cohesive contact sheet with the same reference image; slide frames require a reference-aware provider; clip mode animates the approved frame locally rather than allowing text-to-video identity drift.
- Quality policy: the pipeline fails with a retryable error when reference-aware image generation is unavailable instead of silently substituting Pexels/procedural imagery that breaks character or illustration-style continuity.
- Create from Script detects numbered Hindi or English scene blocks. Supplied Visual and Narration/Voiceover/Dialogue content is parsed deterministically into matching segments and preserved verbatim; its Consistency Bible becomes the locked character sheet. No script-generation job is created for structured input. Unstructured prompts retain the existing AI script fallback.
- Emergent-managed Google OAuth session flow under `/api/auth/*`.

## Verification state

- Public `GET /api/` responds with the StoryForge health message.
- Public dashboard loads at `/dashboard`.
- Unauthenticated dashboard calls to `/api/auth/me` return `401`, which is the expected signed-out state.

## Credentials

No seeded test accounts or static credentials are present in the imported repository.

## Selectable media engines and Studio connector (current)
- Existing CRA/Craco app retained. New frontend code is strict TypeScript; `yarn typecheck` checks it, `yarn build` uses Craco. Legacy JS pages remain. Same-origin `/api` typed helpers and a single TanStack Query provider are now shared by new controls.
- Integrations has independent image/video defaults (Gemini direct key / local Ken Burns initially). Stories and individual zero-based segments can override either or inherit. Explicit choices do not silently fall back; Auto image = Gemini then Emergent; Auto video = Veo then local motion. Engines without reference-editing support fail clearly for locked frames.
- Settings vault wins over stale environment values on restart, including explicit cleared keys. Saving/clearing resets provider cooldowns. Keys are never returned, only configured flags. Existing optional Google auth is unchanged; story/settings paths remain usable without login as in the imported app.
- New Mongo collections: story_engines (image/video, per-segment overrides, Studio versioned character/reference mappings); generation_events (provider/model, stage/kind, segment, HTTP code, safe error text, remediation and timestamp); studio_jobs (durable idempotency key, payload hash, full request, remote ID, status and QC); studio_assets and studio_characters for remote versioned registrations.
- New API module routers/engines.py is included under api_router `/api` in server.py. Pydantic contracts in engine_models.py mirror frontend/src/lib/engine-types.ts.
- Gemini requests use direct saved Gemini keys and supported image-preview model with full prompts/references, actionable HTTP/safety errors, no wasteful quota retries or shared text circuit blocking image attempts. Key check is non-generative; Image test is explicit and potentially billable.
- Diagnostics retains attempts including fallback errors and pipeline failures. Historical generic errors cannot be reconstructed. Generation engines retain complete character bibles (including storyboard/SDK prompt paths).
- Google Veo now receives the approved frame; local Ken Burns remains an explicitly labelled non-generative choice.
- Studio: configurable service URL, backend-only STUDIO_API_TOKEN, capabilities check, reference upload/character registration, async submit/poll/cancel, authenticated artifact download, retained IDs for recovery, full bible/scene/LoRA/reference payload. Only `succeeded` + strict `production_pass: true` + a non-preview production artifact may enter the final video. QC review does not auto-approve. See STUDIO_API.md for provisional contract extensions.
- External blockers: direct live probe of newly saved Gemini key returned HTTP 429 RESOURCE_EXHAUSTED with `generate_content_free_tier_requests, limit: 0`. Google project billing/image quota must be enabled by owner. Studio HTTP API is NOT LIVE, and no actual base URL/token has been provided. Adapter implementation is not a remote server installation. Live Studio generation remains unverified; automated contract tests use MOCKED Studio HTTP responses.
- Public preview: https://script-to-star.preview.emergentagent.com

## Local-first narration policy
- System narration default is Kokoro → XTTS → gTTS. Legacy TTS_PROVIDER_ORDER cannot insert Gemini/OpenAI into this chain. `local:auto`, empty voice and Kokoro selections all use the local-first chain. Explicit XTTS starts at XTTS; explicit gTTS uses gTTS only.
- Gemini TTS is called only for an explicitly selected `gemini:<voice>` (then local fallback if it fails). Expressive narration is local post-processing for local voices and never implicitly enables Gemini. Explicit OpenAI voices remain opt-in, not fallback defaults. Image/video engine routing is unaffected.
- Channel model and voice-preview default = `local:auto`; base channel seeds and video-type seeds are local-first. Startup migration `local_tts_defaults_v1` changes existing cloud/legacy channel voices to local:auto once, preserves local voices, and never resets later explicit Gemini selections on restart.
- Channels UI now lists local automatic/Kokoro/XTTS/gTTS voices before opt-in cloud voices; saves invalidate channel data. Preview cache is versioned to avoid playing older Gemini-generated samples under a local voice name. Existing story narration is preserved until user regenerates it.
- This adapter supports Kokoro/XTTS for Hindi and English; unsupported languages skip them. Bengali falls back to gTTS. gTTS is online (not fully local/offline), but requires no Gemini API. No new TTS provider integration or model installation added.