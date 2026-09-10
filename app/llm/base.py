from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel


ModelT = TypeVar(
    "ModelT",
    bound=BaseModel,
)


class LLMError(Exception):
    pass


class LLMUnavailableError(
    LLMError
):
    pass


class LLMInvalidResponseError(
    LLMError
):
    pass


class LLMProvider(ABC):
    @abstractmethod
    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ModelT],
    ) -> ModelT:
        raise NotImplementedError

    async def close(
        self,
    ) -> None:
        pass