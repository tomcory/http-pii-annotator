"""
Registry of the API connectors.
"""
from dataclasses import dataclass

from src.errors import ConfigError
from src.llm_connectors.api_anthropic import ApiAnthropic
from src.llm_connectors.api_base import LLMClient
from src.llm_connectors.api_deepseek import ApiDeepSeek
from src.llm_connectors.api_ollama import ApiOllama
from src.llm_connectors.api_openai import ApiOpenAI

CONNECTORS: dict[str, type[LLMClient]] = {
    'openai': ApiOpenAI,
    'anthropic': ApiAnthropic,
    'deepseek': ApiDeepSeek,
    'ollama': ApiOllama
}

APIS = tuple(CONNECTORS)
DEFAULT_API = 'openai'


@dataclass(frozen=True)
class ModelSpec:
    """
    A model and the API that serves it.

    Attributes:
        api: Name of the API
        name: Name of the model as expected by the API
    """
    api: str
    name: str


def create_client(api: str, hostname: str = None, timeout: float = None) -> LLMClient:
    if api not in CONNECTORS:
        raise ConfigError(f"Unknown API '{api}', expected one of {APIS}")
    return CONNECTORS[api](hostname, timeout)
