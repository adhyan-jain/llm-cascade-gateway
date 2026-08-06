# File-by-File Detail Reference

This document provides a detailed walkthrough of every code module, configuration file, and utility inside the LLM Cascade Gateway repository.

---

## 1. Core Entrypoint & API Layer

### `app/main.py`
* **Purpose**: Initializes the FastAPI application, registers middleware (CORS), hooks up background loops, and exposes API routes.
* **Key Components**:
  - `health_monitor_loop()`: A background task running every 30 seconds that checks unhealthy or cooling down providers, pings them, and recovers them if they respond successfully.
  - `/v1/chat/completions`: The core endpoint. Handles caching checks, fallback routing, and formats responses.
    - **Streaming Mode**: Streams raw chunk bytes directly to the client via `response.aiter_bytes()`, and buffers the text to log token statistics in the `finally` block.
    - **JSON Mode**: Captures the complete response payload, updates the cache, updates stats, and overrides the model name in the JSON to match the client's original requested model name.
  - `/v1/models`: Exposes the list of custom aliases and active provider defaults.
  - `/health`, `/providers`, `/stats`: Operations endpoints for debugging, monitoring, and statistics tracking.

---

## 2. Configuration & Initialization

### `config.yaml`
* **Purpose**: Centralized declarative configuration for gateway settings, caching parameters, model routing chains, aliases, and provider metadata.
* **Key Sections**:
  - `gateway`: Settings for cooldown durations, health check intervals, and active load-balancing strategies.
  - `cache`: TTL (Time To Live) and maximum size configurations for the response cache.
  - `routing`: Defines the default fallback chains, prefixes (e.g., `gemini-`, `gpt-`), and model aliases (e.g., `coding-fast`, `coding-best`).
  - `providers`: List of providers with their base URLs, default models, and credential environment variables.

### `app/config/settings.py`
* **Purpose**: Loads and parses `config.yaml` and `.env` variables using **Pydantic v2 Settings**.
* **Key Components**:
  - Uses Pydantic's validation models (`GatewaySettings`, `CacheSettings`, `RoutingSettings`, `ProviderSettings`) to ensure type safety and schema consistency upon startup.
  - Loads credentials from environment variables dynamically using placeholders.

---

## 3. OpenAI Models Compatibility

### `app/models/openai.py`
* **Purpose**: Contains the Pydantic request and response schemas matching the official OpenAI API specifications.
* **Key Components**:
  - `ChatMessage`: Schema representing a single chat dialogue step (`role`, `content`, `name`, `tool_calls`).
  - `ChatCompletionRequest`: Schema for the incoming POST completion request payload.
  - `ChatCompletionResponse`: Schema for standard JSON chat completion responses.

---

## 4. Provider Layer

### `app/providers/base.py`
* **Purpose**: Defines the abstract base class and common behavior/properties for all downstream API providers.
* **Key Features**:
  - Cooldown Tracking: Properties `cooldown_until` and `health_status` indicating whether the provider is active, unhealthy, or in a cooldown.
  - Metrics Collection: Tracks request count, failures, successes, and average latency.
  - `ping_health()`: Executes a minimal, cheap model completion test (e.g., max 1 token) to verify API connectivity.

### `app/providers/` (Submodules)
* **`gemini.py`**: Points to Google's official OpenAI-compatible endpoint (`/v1beta/openai`). Passes the key via `Bearer` Authorization.
* **`groq.py`**: Maps to Groq's endpoint. Uses the `Authorization` header with the configured API key.
* **`openrouter.py`**: Connects to OpenRouter. Includes required headers like `HTTP-Referer` and `X-Title` to prevent request rejection.
* **`nvidia.py` / `mistral.py`**: Standard Bearer token authentication endpoints.
* **`ollama.py`**: Connects to the local Ollama instance (default `http://localhost:11434/v1`). Requires no credentials.
* **`manager.py`**: Initializes all active providers based on configuration values and handles client connection pools (`httpx.AsyncClient`).

---

## 5. Router & Fallback Engine

### `app/router/fallback.py`
* **Purpose**: Coordinates chain resolution, provider sorting, and execution of retry logic.
* **Key Components**:
  - `resolve_chain()`: Determines the order of providers to try based on whether the model is an alias or matches a prefix.
  - `get_ordered_providers()`: Sorts healthy providers in the active chain based on the chosen load-balancing strategy (e.g., Least Recently Used, Round Robin, or Priority).
  - `route_chat_completion()`: The core execution loop. Iterates through sorted providers:
    - Runs a nested retry loop up to 3 times per provider.
    - Captures rate limits, parses delay parameters from response text, sleeps, and retries.
    - If a provider is completely exhausted, triggers a cooldown and falls back to the next provider in the chain.

---

## 6. Utilities

### `app/utils/cache.py`
* **Purpose**: Thread-safe in-memory cache implemented using an LRU eviction dictionary.
* **Key Components**:
  - Automatically serializes request parameters (messages, temperature, model) to serve as a lookup key.
  - Expiration based on TTL timestamps to guarantee fresh responses.

### `app/utils/logger.py`
* **Purpose**: Configures standard logging for application info and rotatable JSON logs for auditing.
* **Key Components**:
  - `gateway.log`: Rotated hourly to save operational audit footprints (latency, token count, cost/tokens estimation, and request IDs).

### `app/utils/stats.py`
* **Purpose**: Aggregates runtime gateway metrics.
* **Key Components**:
  - Exposes active uptime, request count, success rate, cache hit rate, and average latency.

---

## 7. System Persistence

### `ai-gateway.service`
* **Purpose**: Systemd user service unit configuration template.
* **Key Configuration**:
  - Runs in the background under `uvicorn` using the virtual environment's interpreter.
  - Restarts automatically on failure (`Restart=always`).
  - Sets dependencies on user-level target managers.
