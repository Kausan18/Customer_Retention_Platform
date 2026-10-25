# app/guardrails/output_validator.py
"""
Guardrail 3: Structured Output Validator.

Validates LLM node outputs against their expected Pydantic schema.
Called in a wrapper around every LLM node in the LangGraph graphs (Week 2).
For Checkpoint 3, tested directly in unit tests.

Returns (parsed_model, None) on success.
Returns (None, error_message) on failure — the LangGraph node uses the error
message to build a re-prompt.
"""

import json
import re
from typing import TypeVar, Type

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Regex to strip ```json ... ``` or ``` ... ``` fences from LLM output.
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")


def _strip_fences(text: str) -> str:
    """Remove markdown code fences. If no fence found, return text as-is."""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def validate_output(
    raw_text: str,
    schema: Type[T],
) -> tuple[T | None, str | None]:
    """
    Parse raw LLM text into a Pydantic model instance.

    Args:
        raw_text: Raw text returned by the LLM node.
        schema: The Pydantic model class to validate against.

    Returns:
        (model_instance, None) on success.
        (None, error_message) on failure.
        error_message is suitable for inclusion in a re-prompt.
    """
    stripped = _strip_fences(raw_text)

    # Step 1: JSON parse
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        return None, (
            f"Output was not valid JSON. Parse error: {e}. "
            f"You must respond ONLY with a JSON object matching the required schema. "
            f"No preamble, no markdown fences, no trailing text."
        )

    # Step 2: Schema validation
    try:
        instance = schema.model_validate(data)
        return instance, None
    except ValidationError as e:
        # Summarize which fields failed — keeps the re-prompt focused
        field_errors = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in e.errors()
        )
        return None, (
            f"JSON parsed successfully but failed schema validation. "
            f"Errors: {field_errors}. "
            f"Respond ONLY with a JSON object matching the required schema."
        )