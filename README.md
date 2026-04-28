# WhatsApp-to-Teamwork Task Pipeline

Automated system that converts WhatsApp messages (text + voice notes) into structured tasks in Teamwork Projects.

```
WhatsApp → Node.js Bridge (Baileys) → FastAPI Backend → Whisper + LLaMA → Teamwork API
```

## Prerequisites

- **Node.js** >= 18
- **Python** >= 3.10
- **Ollama** — install from https://ollama.ai
- **ffmpeg** — required by Whisper for audio processing
  - Windows: `winget install ffmpeg` or download from https://ffmpeg.org
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`

## Setup

### 1. Pull LLaMA Model

```bash
ollama pull llama3.1:8b
```

Ensure Ollama is running (`ollama serve`).

### 2. Backend (FastAPI)

```bash
cd backend

# Create virtual environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate
# Activate (macOS/Linux)
# source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your Teamwork credentials
```

**Start the backend:**

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 3. Bridge (Node.js)

```bash
cd bridge

# Install dependencies
npm install

# Configure environment
cp .env.example .env
# Edit .env if needed
```

**Start the bridge:**

```bash
cd bridge
npx ts-node src/index.ts
```

On first run, a QR code will appear in the terminal. Scan it with WhatsApp (Linked Devices).

## Environment Variables

### Backend (`backend/.env`)

| Variable | Description | Default |
|---|---|---|
| `TEAMWORK_API_KEY` | Teamwork API key | (required) |
| `TEAMWORK_PROJECT_ID` | Target project ID | (required) |
| `TEAMWORK_DOMAIN` | e.g. `yourcompany.teamwork.com` | (required) |
| `LLAMA_ENDPOINT` | Ollama server URL | `http://localhost:11434` |
| `LLAMA_MODEL` | Model name | `llama3.1:8b` |
| `WHISPER_MODEL_SIZE` | Whisper model size | `base` |

### Bridge (`bridge/.env`)

| Variable | Description | Default |
|---|---|---|
| `WHATSAPP_SESSION_PATH` | Auth state folder | `./auth_state` |
| `API_URL` | FastAPI backend URL | `http://localhost:8000` |

## API Reference

### `POST /process-message`

Accepts `multipart/form-data`:

| Field | Type | Required | Description |
|---|---|---|---|
| `sender_id` | string | yes | WhatsApp sender JID |
| `message` | string | no | Text message content |
| `audio_file` | file | no | Audio file (`.ogg`) |

At least one of `message` or `audio_file` must be provided.

### Sample Request (text only)

```bash
curl -X POST http://localhost:8000/process-message \
  -F "sender_id=212600000000@s.whatsapp.net" \
  -F "message=We need a new landing page for the summer campaign, deadline next Friday, it's urgent"
```

### Sample Response

```json
{
  "status": "ok",
  "sender_id": "212600000000@s.whatsapp.net",
  "task": {
    "title": "Summer Campaign Landing Page",
    "description": "Create a new landing page for the upcoming summer marketing campaign. The page needs to be completed urgently with a tight deadline.",
    "client_request": "New landing page for summer campaign, urgent, deadline next Friday",
    "deadline": "2026-04-17",
    "priority": "high",
    "project_type": "web"
  },
  "teamwork_response": {
    "STATUS": "OK",
    "id": "12345678"
  }
}
```

### Sample Request (voice note)

```bash
curl -X POST http://localhost:8000/process-message \
  -F "sender_id=212600000000@s.whatsapp.net" \
  -F "audio_file=@voice_note.ogg;type=audio/ogg"
```

### `GET /health`

Health check endpoint.

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

## Architecture

```
bridge/                     Node.js WhatsApp listener
  src/
    index.ts                Entry point
    whatsapp.ts             Baileys connection + message handler
    api.ts                  HTTP client to FastAPI (with retry)
    queue.ts                In-memory FIFO queue + deduplication

backend/                    Python FastAPI backend
  app/
    main.py                 FastAPI app entrypoint
    api/routes.py           POST /process-message endpoint
    services/
      whisper_service.py    Local Whisper transcription
      ai_service.py         LLaMA task extraction via Ollama
      teamwork_service.py   Teamwork REST API client
    models/task.py          Pydantic TaskData model
    utils/
      logger.py             JSON structured logging
      retry.py              Async retry decorator
```

## How It Works

1. **Bridge** connects to WhatsApp via Baileys and listens for incoming messages
2. Text messages and voice notes are queued with deduplication
3. Messages are sent to the FastAPI backend via `POST /process-message`
4. If a voice note is attached, **Whisper** transcribes it to text
5. The text is sent to **LLaMA 3.1** (via Ollama) with a structured extraction prompt
6. The LLM output is validated against a Pydantic schema (with one retry on failure)
7. The validated task is created in **Teamwork** via their REST API
