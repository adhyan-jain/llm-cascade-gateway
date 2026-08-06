from typing import Dict
from app.providers.base import BaseProvider

class OllamaProvider(BaseProvider):
    def get_base_url(self) -> str:
        base = self.url_override or "http://localhost:11434"
        if not base.endswith("/v1"):
            base = f"{base.rstrip('/')}/v1"
        return base

    def get_headers(self) -> Dict[str, str]:
        # Local Ollama typically requires no authentication headers
        return {}
