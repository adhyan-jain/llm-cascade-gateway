import time
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
import httpx
from app.config.settings import ProviderSettings

class BaseProvider(ABC):
    def __init__(self, settings: ProviderSettings):
        self.id = settings.id
        self.type = settings.type
        self.default_model = settings.default_model
        self.api_key = settings.api_key
        self.url_override = settings.url
        
        self.health_status = "healthy"  # healthy, unhealthy, cooldown
        self.cooldown_until = 0.0
        self.consecutive_failures = 0
        self.last_used_at = 0.0
        
        # Statistics
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_latency_ms = 0.0
        
        self._client: Optional[httpx.AsyncClient] = None

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # Standard client config for high concurrency local gateway
            limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
            # Default timeout of 60 seconds (useful for LLMs)
            self._client = httpx.AsyncClient(limits=limits, timeout=60.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @abstractmethod
    def get_base_url(self) -> str:
        """Returns the base endpoint URL for the OpenAI compatible API."""
        pass

    @abstractmethod
    def get_headers(self) -> Dict[str, str]:
        """Returns headers required for authentication and request context."""
        pass

    def mark_success(self, latency_ms: float):
        self.last_used_at = time.time()
        self.total_requests += 1
        self.successful_requests += 1
        self.consecutive_failures = 0
        self.total_latency_ms += latency_ms
        self.health_status = "healthy"

    def mark_failure(self):
        self.last_used_at = time.time()
        self.total_requests += 1
        self.failed_requests += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= 3:
            self.health_status = "unhealthy"

    def trigger_cooldown(self, duration_seconds: int = 300):
        self.health_status = "cooldown"
        self.cooldown_until = time.time() + duration_seconds

    def check_cooldown(self) -> bool:
        """Checks if cooldown expired, and updates status to healthy if so."""
        if self.health_status == "cooldown":
            if time.time() >= self.cooldown_until:
                self.health_status = "healthy"
                self.consecutive_failures = 0
                return False
            return True
        return False

    def is_available(self) -> bool:
        """Determines if the provider is currently eligible to receive requests."""
        if self.check_cooldown():
            return False
        return self.health_status == "healthy"

    async def ping_health(self) -> bool:
        """
        Sends a minimal request to check if the provider is reachable.
        Can be overridden by subclasses for provider-specific health checks.
        """
        try:
            client = self.get_client()
            url = f"{self.get_base_url().rstrip('/')}/models"
            headers = self.get_headers()
            
            # Use a short timeout of 5s for health check pings
            response = await client.get(url, headers=headers, timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False
