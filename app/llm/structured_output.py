import logging
from typing import Type, TypeVar
from pydantic import BaseModel, ValidationError
from app.llm.client import LLMClient

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("structured_output")


class StructuredOutputFailure(Exception):
    """Raised when structured output generation fails after re-prompting."""
    pass


async def call_with_validation(
    client: LLMClient,
    prompt: str,
    response_model: Type[T],
    system_prompt: str = "",
    temperature: float = 0.2,
    max_retries: int = 1
) -> T:
    """Wrapper that calls generate_structured and re-prompts once with validation errors if parsing fails."""
    try:
        return await client.generate_structured(
            prompt=prompt,
            response_model=response_model,
            system_prompt=system_prompt,
            temperature=temperature
        )
    except (ValidationError, ValueError) as e:
        logger.warning(f"Structured output validation failed on initial attempt: {e}. Re-prompting LLM...")
        error_context = (
            f"{prompt}\n\n"
            f"[PREVIOUS ATTEMPT VALIDATION ERROR]\n"
            f"Your previous JSON output failed Pydantic schema validation with error:\n{str(e)}\n"
            f"Please strictly fix all validation errors and output valid JSON matching the schema."
        )
        try:
            return await client.generate_structured(
                prompt=error_context,
                response_model=response_model,
                system_prompt=system_prompt,
                temperature=temperature
            )
        except Exception as retry_err:
            raise StructuredOutputFailure(
                f"Structured output generation failed after re-prompting: {retry_err}"
            ) from retry_err
