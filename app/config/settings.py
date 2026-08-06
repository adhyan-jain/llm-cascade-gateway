import os
import yaml
from pathlib import Path
from typing import List, Dict, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load environment variables
BASE_DIR = Path(__file__).resolve().parent.parent.parent
env_path = BASE_DIR / '.env'
load_dotenv(dotenv_path=env_path)

class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_seconds: int = 300
    max_size: int = 1000

class GatewaySettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 11435
    load_balancing: str = "round_robin"  # round_robin, lru, priority
    cooldown_duration_seconds: int = 300
    health_check_interval_seconds: int = 30
    cache: CacheConfig = Field(default_factory=CacheConfig)

class ProviderSettings(BaseModel):
    id: str
    type: str
    api_key_env: Optional[str] = None
    url: Optional[str] = None
    default_model: str

    @property
    def api_key(self) -> Optional[str]:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)

class AliasSettings(BaseModel):
    chain: List[str]
    models: Dict[str, str]

class PrefixSettings(BaseModel):
    primary_providers: List[str]

class RoutingSettings(BaseModel):
    default_chain: List[str]
    aliases: Dict[str, AliasSettings] = Field(default_factory=dict)
    prefixes: Dict[str, PrefixSettings] = Field(default_factory=dict)

class Config(BaseModel):
    gateway: GatewaySettings
    providers: List[ProviderSettings]
    routing: RoutingSettings

def load_config(config_path: Optional[Path] = None) -> Config:
    if config_path is None:
        config_path = BASE_DIR / 'config.yaml'
    
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found at {config_path}")
    
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f)
        
    return Config(**data)

# Global settings instance
settings = load_config()
