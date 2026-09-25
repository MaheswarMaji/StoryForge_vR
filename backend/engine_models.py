"""Media-engine API contracts (mirrored in frontend/src/lib/engine-types.ts)."""
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator
from urllib.parse import urlparse

ImageEngine = Literal['auto', 'gemini', 'openai', 'stability', 'fal_flux', 'replicate_flux', 'hf_flux', 'studio']
VideoEngine = Literal['auto', 'kenburns', 'gemini_veo', 'studio']


class EngineChoice(BaseModel):
    image: ImageEngine | None = None
    video: VideoEngine | None = None


class EngineSettings(BaseModel):
    image: ImageEngine = 'gemini'
    video: VideoEngine = 'kenburns'
    studio_base_url: str = ''
    studio_poll_seconds: int = Field(default=5, ge=1, le=60)
    studio_timeout_seconds: int = Field(default=900, ge=30, le=7200)

    @field_validator('studio_base_url')
    @classmethod
    def valid_url(cls, v):
        v = v.strip().rstrip('/')
        if v:
            u = urlparse(v)
            if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError('Use an HTTP(S) service URL without credentials, query or fragment')
            if u.hostname in ('169.254.169.254', 'metadata.google.internal'):
                raise ValueError('Cloud metadata addresses are not studio services')
        return v


class EngineOption(BaseModel):
    id: str
    label: str
    configured: bool
    reference_aware: bool
    note: str = ''


class EngineSettingsView(EngineSettings):
    studio_token_set: bool
    image_options: list[EngineOption]
    video_options: list[EngineOption]


class StudioBinding(BaseModel):
    character: dict[str, Any] = Field(default_factory=dict)
    references: dict[str, str] = Field(default_factory=dict)
    direction: dict[str, Any] = Field(default_factory=dict)
    lora_ids: list[str] = Field(default_factory=list)


class StoryEngines(BaseModel):
    image: ImageEngine | None = None
    video: VideoEngine | None = None
    segments: dict[str, EngineChoice] = Field(default_factory=dict)
    studio: StudioBinding = Field(default_factory=StudioBinding)
    studio_segments: dict[str, StudioBinding] = Field(default_factory=dict)


class StoryEnginesView(StoryEngines):
    effective: EngineChoice


class GenerationEvent(BaseModel):
    id: str
    story_id: str = ''
    job_id: str = ''
    segment: int | None = None
    kind: str
    provider: str
    model: str = ''
    status: str
    http_status: int | None = None
    code: str = ''
    message: str
    action: str = ''
    request_id: str = ''
    created_at: str


class DiagnosticRequest(BaseModel):
    generate_sample: bool = False


class ConnectionResult(BaseModel):
    status: str
    message: str
    capabilities: dict[str, Any] = Field(default_factory=dict)


class StudioJobView(BaseModel):
    id: str
    job_id: str = ''
    story_id: str
    segment: int | None = None
    operation: str
    status: str
    production_pass: bool = False
    qc: dict[str, Any] = Field(default_factory=dict)
    error: str = ''


class StudioAssetView(BaseModel):
    asset_id: str
    media_type: str
    status: str


class StudioCharacterRequest(BaseModel):
    definition: dict[str, Any]


class StudioCharacterView(BaseModel):
    definition: dict[str, Any]