import time
import pytest
import asyncio
from typing import Generator
from fastapi.testclient import TestClient
import httpx

from app.main import app, health_monitor_loop
from app.config.settings import settings
from app.providers.manager import provider_manager
from app.router.fallback import router
from app.utils.cache import response_cache
from app.utils.stats import stats_tracker

# Create a FastAPI test client
client = TestClient(app)

@pytest.fixture(autouse=True)
def run_before_and_after_tests():
    # Setup: Reset provider status and cache before each test
    response_cache.clear()
    for provider in provider_manager.list_providers():
        provider.health_status = "healthy"
        provider.consecutive_failures = 0
        provider.cooldown_until = 0.0
        provider.last_used_at = 0.0
        provider.total_requests = 0
        provider.successful_requests = 0
        provider.failed_requests = 0
        provider.total_latency_ms = 0.0
    
    yield
    
    # Teardown: Clean up
    response_cache.clear()

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

def test_models_endpoint():
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    # Verify our custom aliases exist
    model_ids = [m["id"] for m in data["data"]]
    assert "coding-fast" in model_ids
    assert "coding-best" in model_ids
    assert "reasoning" in model_ids
    assert "local" in model_ids

def test_providers_endpoint():
    response = client.get("/providers")
    assert response.status_code == 200
    providers = response.json()
    assert len(providers) > 0
    assert providers[0]["health_status"] == "healthy"

@pytest.mark.asyncio
async def test_cache_hits(monkeypatch):
    """
    Verifies that non-streaming requests are cached and consecutive requests
    hit the cache without calling the provider.
    """
    call_count = 0

    async def mock_execute_request(self, provider, mapped_model, request_data, is_stream):
        nonlocal call_count
        call_count += 1
        # Return a mock successful httpx.Response
        mock_response = httpx.Response(
            status_code=200,
            json={
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": f"Response number {call_count}"}
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
            }
        )
        return mock_response, 10.0

    monkeypatch.setattr(router.__class__, "execute_request", mock_execute_request)

    request_payload = {
        "model": "local",
        "messages": [{"role": "user", "content": "Hello cache test"}],
        "stream": False
    }

    # First request - should call execute_request
    response1 = client.post("/v1/chat/completions", json=request_payload)
    assert response1.status_code == 200
    assert response1.json()["choices"][0]["message"]["content"] == "Response number 1"
    assert call_count == 1

    # Second request with same payload - should hit cache
    response2 = client.post("/v1/chat/completions", json=request_payload)
    assert response2.status_code == 200
    assert response2.json()["choices"][0]["message"]["content"] == "Response number 1"
    assert call_count == 1  # Still 1, verify cache was used!

@pytest.mark.asyncio
async def test_fallback_on_429(monkeypatch):
    """
    Verifies that if a provider returns 429, the router puts it in cooldown
    and transparently falls back to the next provider.
    """
    monkeypatch.setattr(settings.gateway, "load_balancing", "priority")
    executed_providers = []

    async def mock_execute_request(self, provider, mapped_model, request_data, is_stream):
        executed_providers.append(provider.id)
        if provider.id == "gemini_1":
            # Return 429 for the first provider
            return httpx.Response(status_code=429, content="Rate limit exceeded"), 5.0
        
        # Return 200 for subsequent providers
        return httpx.Response(
            status_code=200,
            json={
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5}
            }
        ), 10.0

    monkeypatch.setattr(router.__class__, "execute_request", mock_execute_request)

    # Force a chain starting with gemini_1 followed by gemini_2
    request_payload = {
        "model": "coding-fast",
        "messages": [{"role": "user", "content": "testing 429 fallback"}],
        "stream": False
    }

    response = client.post("/v1/chat/completions", json=request_payload)
    assert response.status_code == 200
    # Should have tried gemini_1 first, failed, then successfully completed with gemini_2
    assert "gemini_1" in executed_providers
    assert "gemini_2" in executed_providers
    
    # gemini_1 should now be in cooldown status
    gemini_1 = provider_manager.get_provider("gemini_1")
    assert gemini_1.health_status == "cooldown"
    assert gemini_1.cooldown_until > time.time()

@pytest.mark.asyncio
async def test_prefix_routing():
    """
    Verifies that model names beginning with certain prefixes are correctly resolved.
    """
    # 1. GPT prefix should place openrouter providers first
    chain, _ = router.resolve_chain("gpt-4o")
    assert chain[0] in ["openrouter_1", "openrouter_2", "openrouter_3"]
    assert chain[-1] == "ollama_1"  # Ollama always last fallback

    # 2. Claude prefix should place openrouter providers first
    chain, _ = router.resolve_chain("claude-3-5-sonnet")
    assert chain[0] in ["openrouter_1", "openrouter_2", "openrouter_3"]
    assert chain[-1] == "ollama_1"

    # 3. Gemini prefix should place gemini providers first
    chain, _ = router.resolve_chain("gemini-1.5-pro")
    assert chain[0] in ["gemini_1", "gemini_2"]
    assert chain[-1] == "ollama_1"

    # 4. Deepseek prefix should place groq first
    chain, _ = router.resolve_chain("deepseek-v3")
    assert chain[0] in ["groq_1", "groq_2", "openrouter_1"]
    assert chain[-1] == "ollama_1"

@pytest.mark.asyncio
async def test_load_balancing_lru(monkeypatch):
    """
    Verifies that LRU load balancing orders active providers by last usage time.
    """
    # Change settings to LRU load balancing
    monkeypatch.setattr(settings.gateway, "load_balancing", "lru")

    # Set custom last used timestamps
    p1 = provider_manager.get_provider("gemini_1")
    p2 = provider_manager.get_provider("gemini_2")
    p1.last_used_at = 200.0
    p2.last_used_at = 100.0  # gemini_2 was used longer ago (Least Recently Used)

    # For the default chain, gemini_2 should be prioritized over gemini_1
    chain = ["gemini_1", "gemini_2"]
    ordered = await router.get_ordered_providers(chain)
    
    assert ordered[0].id == "gemini_2"
    assert ordered[1].id == "gemini_1"

@pytest.mark.asyncio
async def test_cooldown_recovery(monkeypatch):
    """
    Verifies that cooldown status resets automatically when checked after its duration.
    """
    p = provider_manager.get_provider("gemini_1")
    p.trigger_cooldown(1.0)  # 1 second cooldown
    assert p.health_status == "cooldown"
    assert not p.is_available()

    await asyncio.sleep(1.1)
    
    # Accessing is_available triggers the cooldown check and restores the provider
    assert p.is_available()
    assert p.health_status == "healthy"
