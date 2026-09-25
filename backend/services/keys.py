import os


def candidate_keys():
    """OpenAI API keys to try, in priority order (currently just OPENAI_API_KEY)."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    return [key] if key else []
