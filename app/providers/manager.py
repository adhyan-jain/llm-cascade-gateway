from typing import Dict, List, Optional
from app.config.settings import settings
from app.providers.base import BaseProvider
from app.providers.gemini import GeminiProvider
from app.providers.openrouter import OpenRouterProvider
from app.providers.groq import GroqProvider
from app.providers.nvidia import NvidiaProvider
from app.providers.mistral import MistralProvider
from app.providers.ollama import OllamaProvider

class ProviderManager:
    def __init__(self):
        self.providers: Dict[str, BaseProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self):
        provider_classes = {
            "gemini": GeminiProvider,
            "openrouter": OpenRouterProvider,
            "groq": GroqProvider,
            "nvidia": NvidiaProvider,
            "mistral": MistralProvider,
            "ollama": OllamaProvider
        }

        for p_setting in settings.providers:
            p_type = p_setting.type.lower()
            if p_type in provider_classes:
                cls = provider_classes[p_type]
                self.providers[p_setting.id] = cls(p_setting)
            else:
                # Fallback to base or log warning, for future custom providers
                # We can treat them as Ollama/generic OpenAI-compatible if URL is provided
                pass

    def get_provider(self, provider_id: str) -> Optional[BaseProvider]:
        return self.providers.get(provider_id)

    def list_providers(self) -> List[BaseProvider]:
        return list(self.providers.values())

    async def close_all(self):
        for provider in self.providers.values():
            await provider.close()

# Global provider manager instance
provider_manager = ProviderManager()
