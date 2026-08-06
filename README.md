# LLM Cascade Gateway

A production-ready, high-performance local AI Gateway for Linux (Arch/Omarchy) that exposes a fully OpenAI-compatible API and acts as a router/fallback server with automatic retries and cooldown tracking for multiple LLM providers.

Built directly using **FastAPI + httpx + asyncio** (independent of LiteLLM).

---

## Features

- **OpenAI Compatible Interface**: Exposes `/v1/chat/completions` and `/v1/models`. Works seamlessly with Roo Code, Cline, Continue, Aider, LangChain, and OpenAI SDKs.
- **Internal Retry Mechanism**: Automatically retries transient errors on the *same* provider up to 3 times (429s using parsed retryDelay backoff, 503/504 server overloads, and network timeouts) before switching providers.
- **Failover & Fallbacks**: Automatically cycles through the configured fallback chain if a provider's retry attempts are fully exhausted.
- **Rate Limit Cooldown**: Automatically puts a provider in cooldown when all retry attempts fail (configured to 30s to recover rapidly from rate limits).
- **Load Balancing**: Supports **Priority** (strict chain order), **Round Robin**, and **Least Recently Used (LRU)** strategies to order healthy providers, ensuring Ollama is always the absolute last fallback.
- **SSE Stream Compliance**: Streams raw chunk bytes directly (`aiter_bytes()`) to ensure perfect compatibility with Server-Sent Events (SSE) specifications expected by strict VSCode extensions like Cline/Roo Code.
- **Model Aliases**: Exposes user-friendly model aliases like `coding-fast` and `coding-best` (mapped to `gemini-3.6-flash` for high-quota, large-context free-tier use), `reasoning`, and `local` which map to custom provider chains.
- **Optional Response Cache**: Thread-safe in-memory cache with size-eviction and TTL for non-streaming completions.
- **Structured Auditing**: Logs timestamps, latencies, provider IDs, status codes, token usage (natively parsed or estimated), and errors into a rotating log file.
- **Systemd User Service**: Automatically launches on login as a background user daemon.

---

## Directory Structure

```text
ai-gateway/
├── app/
│   ├── config/
│   │   └── settings.py       # Config loader (Pydantic v2 + yaml)
│   ├── models/
│   │   └── openai.py         # OpenAI-compatible Pydantic schemas
│   ├── providers/
│   │   ├── base.py           # Abstract BaseProvider with stats & health
│   │   ├── gemini.py         # Gemini OpenAI-compatibility backend
│   │   ├── openrouter.py     # OpenRouter API client
│   │   ├── groq.py           # Groq API client
│   │   ├── nvidia.py         # NVIDIA NIM API client
│   │   ├── mistral.py        # Mistral API client
│   │   ├── ollama.py         # Local Ollama client
│   │   └── manager.py        # Provider lifecycle manager
│   ├── router/
│   │   └── fallback.py       # Fallback retry and load-balancing router
│   ├── utils/
│   │   ├── cache.py          # Size-limited TTL response cache
│   │   ├── logger.py         # Rotating audit & app logger
│   │   └── stats.py          # Global usage metrics aggregator
│   ├── tests/
│   │   └── test_gateway.py   # Gateway integration & fallback tests
│   └── main.py               # FastAPI router routes & health monitor
├── docs/
│   ├── architecture.md       # High-level architecture documentation
│   └── details.md            # Detailed file-by-file logic reference
├── config.yaml               # Routing, balancing, and provider configs
├── .env.example              # Env template for API keys
├── requirements.txt          # Python dependency list
├── ai-gateway.service        # Systemd user service template
└── README.md
```

---

## Setup & Installation

### 1. Clone & Initialize Environment
Set up your python virtual environment and install dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure API Keys
Copy the template env file:
```bash
cp .env.example .env
```
Edit `.env` and insert your API keys:
```env
GEMINI_API_KEY_1=AIzaSy...
GEMINI_API_KEY_2=AIzaSy...
OPENROUTER_API_KEY_1=sk-or-...
...
```

### 3. Check Configuration (`config.yaml`)
You can adjust the load-balancing strategy, health checks, cache rules, aliases, and chains inside `config.yaml`.
Default load-balancing options are:
- `round_robin`: Rotates non-Ollama providers.
- `lru`: Prioritizes providers used longest ago.
- `priority`: Follows the strict order specified in the chain.

---

## Running the Gateway

### Run Locally (Development)
Launch the server in the foreground:
```bash
PYTHONPATH=. .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 11435 --reload
```

### Run as a Systemd User Service (Production)
To run the gateway in the background and start it automatically on login:
1. Create the systemd user config directory if it doesn't exist:
   ```bash
   mkdir -p ~/.config/systemd/user/
   ```
2. Copy the service definition:
   ```bash
   cp ai-gateway.service ~/.config/systemd/user/
   ```
3. Reload systemd user daemon and start/enable the service:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable ai-gateway.service
   systemctl --user start ai-gateway.service
   ```
4. Check status and log outputs:
   ```bash
   systemctl --user status ai-gateway.service
   journalctl --user -u ai-gateway.service -f
   ```

---

## API Documentation

The gateway runs at `http://127.0.0.1:11435`.

### 1. Chat Completions
Exposes standard OpenAI endpoint:
- **URL**: `POST /v1/chat/completions`
- **Body**: Standard chat completions JSON (supports `stream: true/false`, `tools`, etc.).
- **Headers**: `Content-Type: application/json`

**Example request utilizing an alias**:
```bash
curl http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "coding-fast",
    "messages": [{"role": "user", "content": "Write a python quicksort function."}],
    "stream": false
  }'
```

### 2. Available Models
Lists custom aliases and all provider default models:
- **URL**: `GET /v1/models`

### 3. Health Check
- **URL**: `GET /health`

### 4. Providers Status
Lists current status, failure count, and remaining cooldown of all providers:
- **URL**: `GET /providers`

### 5. Stats Aggregator
Exposes requests count, average latency, total tokens, and per-provider metrics:
- **URL**: `GET /stats`

---

## Running Tests

Verify all fallback routing, cooldowns, prefix matches, and caching features using `pytest`:
```bash
PYTHONPATH=. .venv/bin/pytest -v app/tests/test_gateway.py
```
