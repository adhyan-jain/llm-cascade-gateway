import time
import asyncio
import logging
from typing import List, Dict, Any, Tuple, Optional, AsyncGenerator
import httpx
from fastapi import HTTPException

from app.config.settings import settings
from app.models.openai import ChatCompletionRequest
from app.providers.manager import provider_manager
from app.providers.base import BaseProvider
from app.utils.logger import audit_logger

logger = logging.getLogger("ai_gateway.router")

class Router:
    def __init__(self):
        self.request_counter = 0
        self.lock = asyncio.Lock()

    def resolve_chain(self, requested_model: str) -> Tuple[List[str], Optional[Dict[str, str]]]:
        """
        Resolves the routing chain (list of provider IDs) and model overrides.
        Returns (provider_ids, model_mappings).
        """
        # 1. Check if model is an alias
        aliases = settings.routing.aliases
        if requested_model in aliases:
            alias_cfg = aliases[requested_model]
            return list(alias_cfg.chain), alias_cfg.models

        # 2. Check if model matches intelligent routing prefixes
        prefixes = settings.routing.prefixes
        for prefix, prefix_cfg in prefixes.items():
            if requested_model.lower().startswith(prefix.lower()):
                primary = list(prefix_cfg.primary_providers)
                # Build fallback chain: primary providers first, then rest of default chain
                default_chain = settings.routing.default_chain
                full_chain = []
                # Add primary providers first
                for pid in primary:
                    if pid not in full_chain:
                        full_chain.append(pid)
                # Add the rest of default chain
                for pid in default_chain:
                    if pid not in full_chain:
                        full_chain.append(pid)
                
                # Ensure Ollama is at the absolute end of the chain
                ollama_ids = [pid for pid in full_chain if provider_manager.get_provider(pid) and provider_manager.get_provider(pid).type == "ollama"]
                other_ids = [pid for pid in full_chain if pid not in ollama_ids]
                
                return other_ids + ollama_ids, None

        # 3. Fallback to default chain
        return list(settings.routing.default_chain), None

    async def get_ordered_providers(self, chain: List[str]) -> List[BaseProvider]:
        """
        Orders and filters providers in the chain based on health, cooldown, and load balancing strategy.
        Ollama is always appended at the end.
        """
        async with self.lock:
            self.request_counter += 1
            current_counter = self.request_counter

        # Resolve provider instances
        providers = []
        for pid in chain:
            provider = provider_manager.get_provider(pid)
            if provider:
                providers.append(provider)

        # Check availability (filter out cooldowns/unhealthy)
        available_providers = [p for p in providers if p.is_available()]

        # If ALL providers are down/cooling, try them all anyway as a last-resort recovery
        if not available_providers:
            logger.warning("All providers are unhealthy or cooling down. Attempting recovery with all configured providers.")
            available_providers = providers

        # Separate Ollama from load balancing (must always be the last fallback)
        ollama_providers = [p for p in available_providers if p.type == "ollama"]
        non_ollama_providers = [p for p in available_providers if p not in ollama_providers]

        lb_mode = settings.gateway.load_balancing.lower()

        if lb_mode == "round_robin" and non_ollama_providers:
            # Shift list based on request counter
            n = len(non_ollama_providers)
            shift = current_counter % n
            ordered_non_ollama = non_ollama_providers[shift:] + non_ollama_providers[:shift]
        elif lb_mode == "lru" and non_ollama_providers:
            # Sort by last_used_at (oldest/zero first)
            ordered_non_ollama = sorted(non_ollama_providers, key=lambda p: p.last_used_at)
        else:
            # Default to "priority" (preserve chain configuration order)
            ordered_non_ollama = non_ollama_providers

        # Always append Ollama at the absolute end
        return ordered_non_ollama + ollama_providers

    def get_mapped_model(self, provider: BaseProvider, requested_model: str, model_mappings: Optional[Dict[str, str]]) -> str:
        """
        Resolves the actual model name to send to the provider.
        """
        # 1. Check if we have an explicit alias model mapping
        if model_mappings and provider.id in model_mappings:
            return model_mappings[provider.id]

        # 2. Check if requested model matches provider type (passthrough)
        # e.g., if requesting 'gemini-1.5-pro' and routing to a gemini provider, pass it through.
        p_type = provider.type.lower()
        if p_type == "gemini" and requested_model.lower().startswith("gemini"):
            return requested_model
        if p_type == "groq" and (requested_model.lower().startswith("llama") or requested_model.lower().startswith("mixtral") or requested_model.lower().startswith("gemma")):
            return requested_model
        if p_type == "mistral" and requested_model.lower().startswith("mistral"):
            return requested_model
        if p_type == "ollama" and requested_model.lower().startswith("qwen"):
            return requested_model

        # 3. Fall back to provider default model
        return provider.default_model

    async def execute_request(
        self,
        provider: BaseProvider,
        mapped_model: str,
        request_data: dict,
        is_stream: bool
    ) -> Tuple[httpx.Response, float]:
        """
        Executes the HTTP call to the provider. Returns (response, latency_ms).
        """
        client = provider.get_client()
        url = f"{provider.get_base_url().rstrip('/')}/chat/completions"
        headers = provider.get_headers()

        # Update the model field in payload
        payload = {**request_data, "model": mapped_model}

        start_time = time.time()
        
        if is_stream:
            # Build request but do not stream yet
            req = client.build_request("POST", url, json=payload, headers=headers)
            response = await client.send(req, stream=True)
        else:
            response = await client.post(url, json=payload, headers=headers)

        latency_ms = (time.time() - start_time) * 1000.0
        return response, latency_ms

    async def route_chat_completion(
        self,
        request: ChatCompletionRequest,
        raw_body: dict,
        request_id: str
    ) -> Tuple[httpx.Response, BaseProvider, str, float]:
        """
        Main fallback routing handler.
        """
        requested_model = request.model
        chain, model_mappings = self.resolve_chain(requested_model)
        ordered_providers = await self.get_ordered_providers(chain)

        if not ordered_providers:
            raise HTTPException(status_code=500, detail="No active or fallback providers available.")

        last_error = None
        
        for provider in ordered_providers:
            mapped_model = self.get_mapped_model(provider, requested_model, model_mappings)
            logger.info(f"Attempting routing: {requested_model} -> provider {provider.id} using model {mapped_model}")

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response, latency_ms = await self.execute_request(
                        provider, mapped_model, raw_body, request.stream
                    )

                    # Check for 429
                    if response.status_code == 429:
                        try:
                            await response.aread()
                            error_body = response.text
                        except Exception:
                            error_body = "Could not read body"
                        
                        if attempt < max_retries - 1:
                            import re
                            delay = 2.0
                            if "retryDelay" in error_body:
                                match = re.search(r'"retryDelay":\s*"([\d\.]+)s?"', error_body)
                                if match:
                                    delay = float(match.group(1))
                            elif "Please retry in" in error_body:
                                match = re.search(r'Please retry in\s*([\d\.]+)s', error_body)
                                if match:
                                    delay = float(match.group(1))
                            
                            delay = min(delay, 5.0)
                            logger.warning(f"Provider {provider.id} returned 429. Retrying in {delay}s (Attempt {attempt+1}/{max_retries})...")
                            if request.stream:
                                await response.aclose()
                            await asyncio.sleep(delay)
                            continue

                        provider.mark_failure()
                        cooldown = settings.gateway.cooldown_duration_seconds
                        provider.trigger_cooldown(cooldown)
                        err_msg = f"Provider {provider.id} returned 429. Cooldown triggered for {cooldown}s. Response: {error_body}"
                        logger.warning(err_msg)
                        audit_logger.warning({
                            "request_id": request_id,
                            "provider": provider.id,
                            "status_code": 429,
                            "latency_ms": latency_ms,
                            "error": err_msg
                        })
                        try:
                            req = response.request
                        except RuntimeError:
                            req = httpx.Request("POST", f"{provider.get_base_url().rstrip('/')}/chat/completions")
                        last_error = httpx.HTTPStatusError(err_msg, request=req, response=response)
                        if request.stream:
                            await response.aclose()
                        break  # move to next provider in the chain

                    # Check for 503 or 504
                    if response.status_code in (503, 504):
                        try:
                            await response.aread()
                            error_body = response.text
                        except Exception:
                            error_body = "Could not read body"

                        if attempt < max_retries - 1:
                            delay = 1.5 * (attempt + 1)
                            logger.warning(f"Provider {provider.id} returned transient status {response.status_code}. Retrying in {delay}s (Attempt {attempt+1}/{max_retries})...")
                            if request.stream:
                                await response.aclose()
                            await asyncio.sleep(delay)
                            continue

                        provider.mark_failure()
                        err_msg = f"Provider {provider.id} returned transient status {response.status_code} after {max_retries} attempts. Response: {error_body}"
                        logger.warning(err_msg)
                        audit_logger.error({
                            "request_id": request_id,
                            "provider": provider.id,
                            "status_code": response.status_code,
                            "latency_ms": latency_ms,
                            "error": err_msg
                        })
                        try:
                            req = response.request
                        except RuntimeError:
                            req = httpx.Request("POST", f"{provider.get_base_url().rstrip('/')}/chat/completions")
                        last_error = httpx.HTTPStatusError(err_msg, request=req, response=response)
                        if request.stream:
                            await response.aclose()
                        break  # move to next provider

                    # Treat other non-200 responses as failures
                    if response.status_code != 200:
                        provider.mark_failure()
                        try:
                            await response.aread()
                            error_body = response.text
                        except Exception:
                            error_body = "Could not read body"
                        err_msg = f"Provider {provider.id} failed with status {response.status_code}. Response: {error_body}"
                        logger.warning(err_msg)
                        audit_logger.error({
                            "request_id": request_id,
                            "provider": provider.id,
                            "status_code": response.status_code,
                            "latency_ms": latency_ms,
                            "error": err_msg
                        })
                        try:
                            req = response.request
                        except RuntimeError:
                            req = httpx.Request("POST", f"{provider.get_base_url().rstrip('/')}/chat/completions")
                        last_error = httpx.HTTPStatusError(err_msg, request=req, response=response)
                        if request.stream:
                            await response.aclose()
                        break  # move to next provider

                    # SUCCESS!
                    provider.mark_success(latency_ms)
                    return response, provider, mapped_model, latency_ms

                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    if attempt < max_retries - 1:
                        delay = 1.0
                        logger.warning(f"Provider {provider.id} encountered connection error/timeout: {str(e)}. Retrying in {delay}s (Attempt {attempt+1}/{max_retries})...")
                        await asyncio.sleep(delay)
                        continue

                    provider.mark_failure()
                    err_msg = f"Provider {provider.id} encountered connection error/timeout after {max_retries} attempts: {str(e)}"
                    logger.warning(err_msg)
                    audit_logger.error({
                        "request_id": request_id,
                        "provider": provider.id,
                        "status_code": "error",
                        "latency_ms": 0.0,
                        "error": err_msg
                    })
                    last_error = e
                    break  # move to next provider

        # If we got here, all providers in the chain failed
        raise HTTPException(
            status_code=502,
            detail=f"All configured providers in the chain failed. Last error: {str(last_error)}"
        )

# Global router instance
router = Router()
