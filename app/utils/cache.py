import hashlib
import json
import time
from typing import Optional, Dict, Tuple, Any
from app.config.settings import settings

class ResponseCache:
    def __init__(self):
        self.cache: Dict[str, Tuple[Any, float]] = {}
        self.ttl = settings.gateway.cache.ttl_seconds
        self.max_size = settings.gateway.cache.max_size
        self.enabled = settings.gateway.cache.enabled

    def _generate_key(self, request_payload: dict) -> str:
        # Generate a canonical JSON string to ensure key stability
        canonical = json.dumps(request_payload, sort_keys=True)
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    def get(self, request_payload: dict) -> Optional[dict]:
        if not self.enabled:
            return None
        
        key = self._generate_key(request_payload)
        now = time.time()
        
        if key in self.cache:
            response, expires_at = self.cache[key]
            if now < expires_at:
                return response
            else:
                # Remove expired entry
                del self.cache[key]
        return None

    def set(self, request_payload: dict, response_payload: dict):
        if not self.enabled:
            return
        
        # Enforce max size limit
        if len(self.cache) >= self.max_size:
            # Clean expired items first
            now = time.time()
            expired_keys = [k for k, (_, exp) in self.cache.items() if now >= exp]
            for k in expired_keys:
                del self.cache[k]
            
            # If still full, evict the oldest item (FIFO order since python dict maintains insertion order)
            if len(self.cache) >= self.max_size:
                oldest_key = next(iter(self.cache))
                del self.cache[oldest_key]
                
        key = self._generate_key(request_payload)
        expires_at = time.time() + self.ttl
        self.cache[key] = (response_payload, expires_at)

    def clear(self):
        self.cache.clear()

# Global cache instance
response_cache = ResponseCache()
