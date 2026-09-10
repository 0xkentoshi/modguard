import asyncio
import logging
import time

import aiohttp


logger = logging.getLogger(__name__)


class OllamaEmbeddingProvider:
    """Small fail-open client for Ollama /api/embed."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int = 45,
        keep_alive: str = "24h",
        name: str = "semantic-embedding",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.name = name
        self.timeout = aiohttp.ClientTimeout(
            total=timeout_seconds
        )
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout
            )
        return self._session

    async def embed(self, text: str) -> list[float]:
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base_url}/api/embed",
                json={
                    "model": self.model,
                    "input": text,
                    "truncate": True,
                    "keep_alive": self.keep_alive,
                },
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise RuntimeError(
                        f"{self.name} returned HTTP {response.status}: "
                        f"{body[:400]}"
                    )
                data = await response.json()
        except (
            aiohttp.ClientError,
            TimeoutError,
            asyncio.TimeoutError,
        ) as exc:
            raise RuntimeError(
                f"Could not use {self.name}"
            ) from exc

        embeddings = data.get("embeddings")
        if (
            not isinstance(embeddings, list)
            or not embeddings
            or not isinstance(embeddings[0], list)
            or not embeddings[0]
        ):
            raise RuntimeError(
                "Unexpected Ollama embedding response"
            )

        return [float(value) for value in embeddings[0]]

    async def warmup(self) -> bool:
        started = time.perf_counter()
        try:
            vector = await self.embed(
                "ModGuard semantic campaign detection warmup"
            )
        except Exception as exc:
            logger.warning(
                "SEMANTIC EMBEDDING DISABLED | model=%s | %s",
                self.model,
                exc,
            )
            return False

        logger.info(
            "SEMANTIC EMBEDDING WARMUP | model=%s | dimensions=%s | ms=%s",
            self.model,
            len(vector),
            int((time.perf_counter() - started) * 1000),
        )
        return True

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
