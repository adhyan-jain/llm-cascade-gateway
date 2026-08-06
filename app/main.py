import asyncio
import json
import logging
import time
import uuid
from typing import AsyncGenerator
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config.settings import settings
from app.models.openai import ChatCompletionRequest, ChatCompletionResponse
from app.providers.manager import provider_manager
from app.router.fallback import router
from app.utils.cache import response_cache
from app.utils.logger import audit_logger, app_logger
from app.utils.stats import stats_tracker

# Setup FastAPI App
app = FastAPI(
    title="Local AI Gateway",
    description="Production-ready local AI Gateway exposing a fully OpenAI-compatible API.",
    version="1.0.0"
)

# Enable CORS for cross-origin client configurations (e.g., browser extensions or web interfaces)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background Health Monitor Task
health_task = None

async def health_monitor_loop():
    """
    Background loop that regularly checks the health of unhealthy or cooling down providers
    and restores them to healthy status if they pass their health ping check.
    """
    interval = settings.gateway.health_check_interval_seconds
    app_logger.info(f"Starting background health monitor with interval of {interval} seconds.")
    
    while True:
        try:
            await asyncio.sleep(interval)
            for provider in provider_manager.list_providers():
                # Perform health checks on unhealthy providers
                if provider.health_status == "unhealthy":
                    app_logger.info(f"Performing periodic health check for unhealthy provider: {provider.id}")
                    is_healthy = await provider.ping_health()
                    if is_healthy:
                        provider.health_status = "healthy"
                        provider.consecutive_failures = 0
                        app_logger.info(f"Provider {provider.id} recovered and is marked healthy.")
                        audit_logger.info({
                            "provider": provider.id,
                            "event": "recovery",
                            "message": f"Provider {provider.id} recovered from unhealthy status."
                        })
                
                # Check and clean up expired cooldowns
                elif provider.health_status == "cooldown":
                    if not provider.check_cooldown():
                        app_logger.info(f"Provider {provider.id} cooldown expired. Marked healthy.")
                        audit_logger.info({
                            "provider": provider.id,
                            "event": "cooldown_expired",
                            "message": f"Provider {provider.id} cooldown expired."
                        })

        except asyncio.CancelledError:
            break
        except Exception as e:
            app_logger.error(f"Error in health monitor background loop: {str(e)}", exc_info=True)

@app.on_event("startup")
async def startup_event():
    global health_task
    health_task = asyncio.create_task(health_monitor_loop())

@app.on_event("shutdown")
async def shutdown_event():
    if health_task:
        health_task.cancel()
        await health_task
    await provider_manager.close_all()
    app_logger.info("Gateway shutdown complete.")

# OpenAI compatibility Chat Completion Endpoint
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
    request_id = str(uuid.uuid4())
    raw_body = await raw_request.json()
    start_time = time.time()

    # 1. Non-streaming cache check
    if not request.stream:
        cached_response = response_cache.get(raw_body)
        if cached_response:
            latency_ms = (time.time() - start_time) * 1000.0
            app_logger.info(f"Cache hit for request {request_id}")
            
            # Record cached request statistics
            usage = cached_response.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            stats_tracker.record_request(True, latency_ms, prompt_tokens, completion_tokens)
            
            # Log cache hit audit
            audit_logger.info({
                "request_id": request_id,
                "provider": "cache",
                "status_code": 200,
                "latency_ms": latency_ms,
                "tokens": prompt_tokens + completion_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "model": request.model,
                "requested_model": request.model
            })
            return JSONResponse(content=cached_response)

    # 2. Route the completion request with fallback routing
    try:
        response, provider, mapped_model, latency_ms = await router.route_chat_completion(
            request, raw_body, request_id
        )
    except HTTPException as he:
        # Propagate router validation or exhaustion errors in OpenAI format
        return JSONResponse(
            status_code=he.status_code,
            content={
                "error": {
                    "message": he.detail,
                    "type": "gateway_error",
                    "param": None,
                    "code": None
                }
            }
        )
    except Exception as e:
        app_logger.error(f"Unexpected router error: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"An unexpected gateway error occurred: {str(e)}",
                    "type": "internal_error",
                    "param": None,
                    "code": None
                }
            }
        )

    # 3. Handle Streaming Response
    if request.stream:
        async def stream_generator() -> AsyncGenerator[bytes, None]:
            accumulated_content = []
            prompt_tokens_est = 0
            completion_tokens_est = 0
            buffer = []
            
            try:
                # Stream raw chunks directly from provider response to client
                async for chunk in response.aiter_bytes():
                    yield chunk
                    buffer.append(chunk)
            except Exception as e:
                # Log any streaming failures mid-stream
                app_logger.error(f"Error streaming response from {provider.id}: {str(e)}")
                yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'stream_error'}})}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
            finally:
                await response.aclose()
                total_latency = (time.time() - start_time) * 1000.0
                
                # Write raw stream to debug log
                try:
                    raw_response_text = b"".join(buffer).decode("utf-8", errors="ignore")
                    with open("logs/debug_stream.log", "a") as f:
                        f.write(f"\n--- REQUEST {request_id} ---\n")
                        f.write(f"Model: {request.model} | Provider: {provider.id}\n")
                        f.write(f"Response:\n{raw_response_text}\n")
                except Exception as log_ex:
                    app_logger.error(f"Failed to write debug stream: {str(log_ex)}")

                # Process buffer content for log analytics
                try:
                    full_text = b"".join(buffer).decode("utf-8", errors="ignore")
                    for line in full_text.splitlines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                continue
                            try:
                                data_json = json.loads(data_str)
                                usage = data_json.get("usage")
                                if usage:
                                    prompt_tokens_est = usage.get("prompt_tokens", 0)
                                    completion_tokens_est = usage.get("completion_tokens", 0)
                                
                                choices = data_json.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content")
                                    if content:
                                        accumulated_content.append(content)
                            except Exception:
                                pass
                except Exception as ex:
                    app_logger.error(f"Failed to parse streamed chunks for logging: {str(ex)}")

                # Estimate token usage if not provided natively by the stream
                if not prompt_tokens_est and not completion_tokens_est:
                    prompt_char_count = sum(len(m.content or "") for m in request.messages if isinstance(m.content, str))
                    prompt_tokens_est = max(1, prompt_char_count // 4)
                    
                    completion_text = "".join(accumulated_content)
                    completion_tokens_est = max(1, len(completion_text) // 4)
                
                # Update global stats
                stats_tracker.record_request(True, total_latency, prompt_tokens_est, completion_tokens_est)
                
                # Write to rotating audit logs
                audit_logger.info({
                    "request_id": request_id,
                    "provider": provider.id,
                    "status_code": 200,
                    "latency_ms": total_latency,
                    "tokens": prompt_tokens_est + completion_tokens_est,
                    "prompt_tokens": prompt_tokens_est,
                    "completion_tokens": completion_tokens_est,
                    "model": mapped_model,
                    "requested_model": request.model
                })

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream"
        )

    # 4. Handle Standard JSON Response
    else:
        try:
            response_data = response.json()
            
            # Save valid response to cache
            response_cache.set(raw_body, response_data)
            
            # Extract actual token counts
            usage = response_data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            
            stats_tracker.record_request(True, latency_ms, prompt_tokens, completion_tokens)
            
            audit_logger.info({
                "request_id": request_id,
                "provider": provider.id,
                "status_code": 200,
                "latency_ms": latency_ms,
                "tokens": prompt_tokens + completion_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "model": mapped_model,
                "requested_model": request.model
            })
            
            # Override model name in response to keep client SDKs consistent
            if "model" in response_data:
                response_data["model"] = request.model

            return JSONResponse(content=response_data)
        except Exception as e:
            app_logger.error(f"Failed parsing json response from {provider.id}: {str(e)}", exc_info=True)
            stats_tracker.record_request(False, latency_ms)
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": f"Provider responded with invalid JSON format: {str(e)}",
                        "type": "provider_error",
                        "param": None,
                        "code": None
                    }
                }
            )

# GET /v1/models
@app.get("/v1/models")
@app.get("/models")
async def get_models():
    """
    Returns lists of models including configured aliases and all provider default models.
    """
    model_list = []
    
    # Add our aliases
    aliases = settings.routing.aliases
    for alias_name in aliases.keys():
        model_list.append({
            "id": alias_name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "ai-gateway"
        })

    # Add all defaults from each active provider
    seen_models = set()
    for provider in provider_manager.list_providers():
        model_id = provider.default_model
        if model_id not in seen_models:
            seen_models.add(model_id)
            model_list.append({
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": provider.id
            })

    return {
        "object": "list",
        "data": model_list
    }

# GET /health
@app.get("/health")
async def get_health():
    return {"status": "healthy"}

# GET /providers
@app.get("/providers")
async def get_providers():
    """
    Returns health, status, and metrics for all loaded providers.
    """
    result = []
    for provider in provider_manager.list_providers():
        cooldown_remaining = max(0.0, provider.cooldown_until - time.time())
        result.append({
            "id": provider.id,
            "type": provider.type,
            "health_status": provider.health_status,
            "cooldown_remaining_seconds": round(cooldown_remaining, 1),
            "consecutive_failures": provider.consecutive_failures,
            "last_used_at": provider.last_used_at,
            "requests": {
                "total": provider.total_requests,
                "successful": provider.successful_requests,
                "failed": provider.failed_requests,
                "average_latency_ms": round(provider.total_latency_ms / provider.successful_requests, 2) if provider.successful_requests > 0 else 0.0
            }
        })
    return result

# GET /stats
@app.get("/stats")
async def get_stats():
    return stats_tracker.get_summary()
