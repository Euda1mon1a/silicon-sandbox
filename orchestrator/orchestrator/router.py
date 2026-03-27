"""Model router — routes LLM calls to the right backend."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Timeout for LLM calls
LLM_TIMEOUT = 120.0


def _get_api_key(key_name: str) -> str:
    """Retrieve an API key from environment variable or macOS Keychain."""
    # Prefer environment variable (standard for CI/containers)
    env_key = key_name.upper().replace("-", "_")
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val

    # Fall back to macOS Keychain
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", key_name, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    raise RuntimeError(
        f"API key not found. Set {env_key} env var or add '{key_name}' to macOS Keychain."
    )


class ModelRouter:
    """Routes LLM calls to configured backends (Anthropic API, local MLX)."""

    def __init__(self, model_config: dict[str, dict]):
        self._config = model_config
        self._anthropic_key: str | None = None

    def _get_anthropic_key(self) -> str:
        if self._anthropic_key is None:
            self._anthropic_key = _get_api_key("anthropic-api-key")
        return self._anthropic_key

    def get_model_name(self, role: str) -> str:
        """Get the model name for a role (planner, coder, researcher, fast)."""
        cfg = self._config.get(role, {})
        return cfg.get("model", "unknown")

    async def call(
        self,
        role: str,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        """Call an LLM via the configured backend for the given role.

        Returns the model's text response.
        """
        cfg = self._config.get(role)
        if not cfg:
            raise ValueError(f"Unknown model role: {role}")

        provider = cfg.get("provider", "openai_compatible")

        if provider == "anthropic":
            return await self._call_anthropic(cfg, prompt, system, max_tokens, temperature)
        elif provider == "openai_compatible":
            return await self._call_openai_compatible(cfg, prompt, system, max_tokens, temperature)
        else:
            raise ValueError(f"Unknown provider: {provider}")

    async def _call_anthropic(
        self, cfg: dict, prompt: str, system: str,
        max_tokens: int, temperature: float,
    ) -> str:
        """Call Anthropic API (Claude)."""
        api_key = self._get_anthropic_key()
        model = cfg.get("model", "claude-sonnet-4-20250514")

        messages = [{"role": "user", "content": prompt}]

        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system or "You are a helpful assistant.",
                    "messages": messages,
                },
            )

        if resp.status_code != 200:
            error_body = resp.text[:500]
            raise RuntimeError(f"Anthropic API error {resp.status_code}: {error_body}")

        data = resp.json()
        # Extract text from content blocks
        content = data.get("content", [])
        text_parts = [block["text"] for block in content if block.get("type") == "text"]
        return "\n".join(text_parts)

    async def _call_openai_compatible(
        self, cfg: dict, prompt: str, system: str,
        max_tokens: int, temperature: float,
    ) -> str:
        """Call OpenAI-compatible endpoint (MLX server, Ollama, etc.)."""
        endpoint = cfg.get("endpoint", "http://127.0.0.1:8080/v1")
        model = cfg.get("model", "default")
        api_key = cfg.get("api_key", "not-needed")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            resp = await client.post(
                f"{endpoint}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )

        if resp.status_code != 200:
            error_body = resp.text[:500]
            raise RuntimeError(f"OpenAI-compatible API error {resp.status_code}: {error_body}")

        data = resp.json()
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return ""
