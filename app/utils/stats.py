import time
from typing import Dict, Any
from app.providers.manager import provider_manager

class StatsTracker:
    def __init__(self):
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_latency_ms = 0.0
        self.tokens_prompt = 0
        self.tokens_completion = 0

    def record_request(self, success: bool, latency_ms: float, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.total_requests += 1
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1
        self.total_latency_ms += latency_ms
        self.tokens_prompt += prompt_tokens
        self.tokens_completion += completion_tokens

    def get_summary(self) -> Dict[str, Any]:
        avg_latency = 0.0
        if self.successful_requests > 0:
            avg_latency = self.total_latency_ms / self.successful_requests

        provider_stats = {}
        for pid, provider in provider_manager.providers.items():
            p_avg_latency = 0.0
            if provider.successful_requests > 0:
                p_avg_latency = provider.total_latency_ms / provider.successful_requests
            
            provider_stats[pid] = {
                "requests": provider.total_requests,
                "successes": provider.successful_requests,
                "failures": provider.failed_requests,
                "average_latency_ms": round(p_avg_latency, 2),
                "health_status": provider.health_status,
                "consecutive_failures": provider.consecutive_failures,
                "last_used_at": provider.last_used_at
            }

        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "average_latency_ms": round(avg_latency, 2),
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "provider_stats": provider_stats
        }

# Global stats tracker instance
stats_tracker = StatsTracker()
