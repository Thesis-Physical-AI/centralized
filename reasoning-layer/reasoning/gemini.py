"""Google AI Studio / Gemini Developer API adapter using the official SDK."""

import json
from typing import Any

from .planner import GenerationError

DEFAULT_MODEL = "gemini-3.5-flash-lite"


class GeminiProvider:
    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, timeout_ms: int = 45000,
                 max_output_tokens: int = 2048, max_prompt_chars: int = 30000):
        if not api_key.strip():
            raise ValueError("Set GEMINI_API_KEY in .env or the environment.")
        if not model.strip() or not 1000 <= timeout_ms <= 120000:
            raise ValueError("Model must be non-blank; timeout must be 1000–120000 ms.")
        if not 128 <= max_output_tokens <= 8192 or not 1000 <= max_prompt_chars <= 100000:
            raise ValueError("Output budget must be 128–8192 tokens; prompt budget 1000–100000 characters.")
        self._api_key = api_key
        self.model = model
        self.timeout_ms = timeout_ms
        self.max_output_tokens = max_output_tokens
        self.max_prompt_chars = max_prompt_chars
        self.usage: list[dict[str, int | None]] = []

    def generate(self, *, system: str, prompt: str, schema: dict[str, Any]) -> str:
        from google import genai
        from google.genai import errors, types

        instructions = (system + "\n\nReturn only a JSON object matching this schema. "
                        "No Markdown or code fences.\n" + json.dumps(schema))
        if len(instructions) + len(prompt) > self.max_prompt_chars:
            raise GenerationError("Prompt exceeds the configured character budget; no API call made.")
        try:
            # Force the Developer API even if Vertex-related environment variables exist.
            # One HTTP attempt per generation; the planner separately bounds format repair.
            with genai.Client(
                api_key=self._api_key,
                vertexai=False,
                http_options=types.HttpOptions(
                    base_url="https://generativelanguage.googleapis.com",
                    api_version="v1beta",
                    timeout=self.timeout_ms,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ) as client:
                response = client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=instructions,
                        max_output_tokens=self.max_output_tokens,
                    ),
                )
        except errors.APIError as error:
            # Never surface raw SDK exceptions or include the configured key in diagnostics.
            code = error.code if isinstance(error.code, int) else "unknown"
            detail = str(error.message or "").replace(self._api_key, "[redacted]")[:500]
            raise GenerationError(
                f"Gemini request failed (HTTP {code}). {detail}"
            ) from None
        except Exception:
            raise GenerationError("Gemini request failed. Check connectivity and SDK configuration.") from None

        if response.usage_metadata is not None:
            usage = response.usage_metadata
            self.usage.append({
                "input": usage.prompt_token_count,
                "output": usage.candidates_token_count,
                "thinking": usage.thoughts_token_count,
                "total": usage.total_token_count,
            })
        candidates = response.candidates or []
        if not candidates or candidates[0].finish_reason != types.FinishReason.STOP:
            raise GenerationError("Gemini did not return a complete plan (blocked or truncated response).")
        text = response.text
        if not text or not text.strip():
            raise GenerationError("Gemini returned no text plan.")
        return text
