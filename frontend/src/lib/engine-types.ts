export type ImageEngine = 'auto' | 'gemini' | 'openai' | 'stability' | 'fal_flux' | 'replicate_flux' | 'hf_flux' | 'studio';
export type VideoEngine = 'auto' | 'kenburns' | 'gemini_veo' | 'studio';
export interface EngineChoice { image: ImageEngine | null; video: VideoEngine | null }
export interface EngineSettings { image: ImageEngine; video: VideoEngine; studio_base_url: string; studio_poll_seconds: number; studio_timeout_seconds: number }
export interface EngineOption { id: string; label: string; configured: boolean; reference_aware: boolean; note: string }
export interface EngineSettingsView extends EngineSettings { studio_token_set: boolean; image_options: EngineOption[]; video_options: EngineOption[] }
export interface StudioBinding { character: Record<string, unknown>; references: Record<string, string>; direction: Record<string, unknown>; lora_ids: string[] }
export interface StoryEngines extends EngineChoice { segments: Record<string, EngineChoice>; studio: StudioBinding; studio_segments: Record<string, StudioBinding> }
export interface StoryEnginesView extends StoryEngines { effective: EngineChoice }
export interface GenerationEvent { id: string; story_id: string; job_id: string; segment: number | null; kind: string; provider: string; model: string; status: string; http_status: number | null; code: string; message: string; action: string; request_id: string; created_at: string }
export interface DiagnosticRequest { generate_sample: boolean }
export interface ConnectionResult { status: string; message: string; capabilities: Record<string, unknown> }
export interface StudioJobView { id: string; job_id: string; story_id: string; segment: number | null; operation: string; status: string; production_pass: boolean; qc: Record<string, unknown>; error: string }
export interface StudioAssetView { asset_id: string; media_type: string; status: string }
export interface StudioCharacterRequest { definition: Record<string, unknown> }
export interface StudioCharacterView { definition: Record<string, unknown> }