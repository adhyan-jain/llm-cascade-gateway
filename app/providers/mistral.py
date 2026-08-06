from typing import Dict
from app.providers.base import BaseProvider

class MistralProvider(BaseProvider):
    def get_base_url(self) -> str:
        if self.url_override:
            return self.url_override
        return "https://api.mistral.ai/v1"

    def get_headers(self) -> Dict[str, str]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
