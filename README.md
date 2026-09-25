# StoryForge

React (CRA + craco) frontend and FastAPI + MongoDB backend. No third-party hosting platform is required.

## Run locally

Prerequisites: Python 3.11+, Node 18/20 + yarn 1.x, MongoDB, ffmpeg and poppler-utils (`pdfinfo`).

```bash
# 1. backend
cd backend
cp .env.example .env            # edit MONGO_URL / API keys
pip install -r requirements.txt
uvicorn server:app --reload --port 8001

# 2. frontend (new terminal)
cd frontend
cp .env.example .env
yarn install
yarn start                      # http://localhost:3000, /api is proxied to :8001
```

Uploaded PDFs are stored on local disk in `storage/` (override with `STORAGE_DIR`); generated media goes to `media/`.
Google sign-in is optional and only needed for the Admin Console (`GOOGLE_CLIENT_ID` in `backend/.env`).
