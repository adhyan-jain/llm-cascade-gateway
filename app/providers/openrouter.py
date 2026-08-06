from typing import Dict
from app.providers.base import BaseProvider

class OpenRouterProvider(BaseProvider):
    def get_base_url(self) -> str:
        if self.url_override:
            return self.url_override
        return "https://openrouter.ai/api/v1"

    def get_headers(self) -> Dict[str, str]:
        headers = {
            "HTTP-Referer": "http://localhost:11435",
            "X-Title": "Local AI Gateway"
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
