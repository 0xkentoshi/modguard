import asyncio
import json
import logging
import time

import aiohttp

from app.llm.base import (
    LLMInvalidResponseError,
    LLMProvider,
    LLMUnavailableError,
    ModelT,
)


logger = logging.getLogger(__name__)


def _normalize_confidence_value(value):
    """
    Normalize a common structured-output formatting mistake.

    LLM schemas require confidence in [0, 1], but local models sometimes emit
    percentages such as 95 or 100. This function repairs only confidence-like
    scalar formatting. It never changes category/action/semantic meaning.
    """

    if isinstance(value, str):
        stripped = value.strip()

        if stripped.endswith("%"):
            try:
                number = float(
                    stripped[:-1].strip()
                )
            except ValueError:
                return value

            if 0.0 <= number <= 100.0:
                return number / 100.0

        try:
            number = float(stripped)
        except ValueError:
            return value

        if 1.0 < number <= 100.0:
            return number / 100.0

        return value

    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 1.0 < float(value) <= 100.0
    ):
        return float(value) / 100.0

    return value


def _repair_structured_payload(payload):
    """
    Recursively repair only confidence/report_confidence percentage formatting.
    """

    repaired = False

    if isinstance(payload, list):
        values = []

        for item in payload:
            fixed, changed = _repair_structured_payload(
                item
            )
            values.append(fixed)
            repaired = repaired or changed

        return values, repaired

    if not isinstance(payload, dict):
        return payload, False

    result = {}

    for key, value in payload.items():
        if key in {
            "confidence",
            "report_confidence",
        }:
            fixed = _normalize_confidence_value(
                value
            )

            if fixed != value:
                repaired = True

            result[key] = fixed
            continue

        fixed, changed = _repair_structured_payload(
            value
        )

        result[key] = fixed
        repaired = repaired or changed

    return result, repaired


class OllamaProvider(LLMProvider):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int = 120,
        think: bool = False,
        keep_alive: str = "24h",
        num_ctx: int = 4096,
        num_predict: int = 320,
        name: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.think = think
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.name = name or model
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def warmup(self) -> None:
        session = await self._get_session()
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "Reply OK."}],
            "stream": False,
            "think": self.think,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0,
                "num_ctx": 256,
                "num_predict": 2,
            },
        }

        started = time.perf_counter()

        try:
            async with session.post(url, json=payload) as response:
                await response.read()
                if response.status != 200:
                    logger.warning("Warmup failed for %s: HTTP %s", self.name, response.status)
                    return
        except Exception:
            logger.exception("Warmup failed for %s", self.name)
            return

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info("LLM WARMUP | %s | ms=%s", self.name, elapsed_ms)

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
    ) -> ModelT:
        session = await self._get_session()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": response_model.model_json_schema(),
            "think": self.think,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        url = f"{self.base_url}/api/chat"

        try:
            async with session.post(url, json=payload) as response:
                body = await response.text()
                if response.status != 200:
                    raise LLMUnavailableError(
                        f"{self.name} returned HTTP {response.status}: {body[:500]}"
                    )
                try:
                    data = await response.json()
                    content = data["message"]["content"]
                except Exception as exc:
                    raise LLMInvalidResponseError(
                        f"Unexpected {self.name} response structure"
                    ) from exc
        except aiohttp.ClientError as exc:
            raise LLMUnavailableError(f"Could not connect to {self.name} at {url}") from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise LLMUnavailableError(f"{self.name} request timed out") from exc

        try:
            return response_model.model_validate_json(content)
        except Exception as first_exc:
            # Local models occasionally return a semantically valid JSON object
            # but express confidence as 95/100 instead of 0.95/1.0.
            # Repair that formatting deterministically before declaring the
            # whole moderation decision invalid.
            json_decode_failed = False
            try:
                raw_payload = json.loads(content)
                repaired_payload, changed = (
                    _repair_structured_payload(
                        raw_payload
                    )
                )

                if changed:
                    result = response_model.model_validate(
                        repaired_payload
                    )

                    logger.warning(
                        "STRUCTURED REPAIR | %s | normalized confidence percentage",
                        self.name,
                    )

                    return result

            except json.JSONDecodeError:
                json_decode_failed = True
            except Exception:
                pass

            # A common local-model failure is valid structured output being cut
            # off by num_predict before the closing JSON tokens. Retry exactly
            # once with a larger output budget and an explicit compact-output
            # instruction. This changes no semantic decision and is used only
            # when the first response is not even parseable JSON.
            if json_decode_failed:
                retry_payload = dict(payload)
                retry_payload["messages"] = [
                    {
                        "role": "system",
                        "content": (
                            system_prompt
                            + "\n\nRETRY REQUIREMENT: Return one compact, complete "
                            "JSON object only. Keep reason/evidence concise. "
                            "Do not repeat conversation history."
                        ),
                    },
                    {"role": "user", "content": user_prompt},
                ]
                retry_payload["options"] = dict(payload["options"])
                retry_payload["options"]["num_predict"] = max(
                    int(self.num_predict) * 2,
                    640,
                )

                try:
                    async with session.post(url, json=retry_payload) as response:
                        retry_body = await response.text()
                        if response.status != 200:
                            raise LLMUnavailableError(
                                f"{self.name} retry returned HTTP {response.status}: "
                                f"{retry_body[:500]}"
                            )
                        retry_data = await response.json()
                        retry_content = retry_data["message"]["content"]

                    try:
                        result = response_model.model_validate_json(retry_content)
                    except Exception:
                        raw_retry = json.loads(retry_content)
                        repaired_retry, _ = _repair_structured_payload(raw_retry)
                        result = response_model.model_validate(repaired_retry)

                    logger.warning(
                        "STRUCTURED RETRY RECOVERED | %s | num_predict=%s",
                        self.name,
                        retry_payload["options"]["num_predict"],
                    )
                    return result

                except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "Structured retry failed for %s: %s",
                        self.name,
                        exc,
                    )
                except Exception:
                    logger.exception(
                        "Structured retry did not recover %s",
                        self.name,
                    )

            logger.error(
                "Invalid structured response from %s: %s",
                self.name,
                content,
            )

            raise LLMInvalidResponseError(
                f"{self.name} returned JSON that did not match the required schema"
            ) from first_exc

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
