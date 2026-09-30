"""Prompt-injection screening with Meta's Llama Prompt Guard 2 (served free on Groq).

The model returns the probability that the text is an injection or jailbreak
attempt. Only the latest user turn is screened, which keeps the check fast.
"""

from __future__ import annotations

import httpx

GUARD_MODEL = "meta-llama/llama-prompt-guard-2-86m"


class PromptGuard:
    def __init__(self, api_key: str, threshold: float = 0.9, transport: httpx.AsyncBaseTransport | None = None):
        self.threshold = threshold
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(5.0), transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def score(self, text: str) -> float | None:
        """Injection probability in [0, 1], or None if the check could not run (fail open)."""
        if not self._key or not text.strip():
            return None
        try:
            r = await self._client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._key}"},
                json={"model": GUARD_MODEL, "messages": [{"role": "user", "content": text[-4000:]}]},
            )
            r.raise_for_status()
            return float(r.json()["choices"][0]["message"]["content"].strip())
        except (httpx.HTTPError, KeyError, ValueError, IndexError):
            return None

    async def aclose(self) -> None:
        await self._client.aclose()
