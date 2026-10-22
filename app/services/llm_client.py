"""LLM client service with Gemini and Groq support."""
import asyncio
import json
import logging
import re
from typing import Any, Callable, Dict, Optional, Type
from pydantic import BaseModel, ValidationError
import google.generativeai as genai
from groq import AsyncGroq
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

# Concurrency semaphore
_llm_semaphore = asyncio.Semaphore(max(1, settings.llm_max_concurrent_requests))


class LLMError(Exception):
    """LLM call error."""
    pass


class LLMClient:
    """Unified LLM client supporting multiple providers with lazy initialization."""

    def __init__(self):
        """Initialize LLM client metadata without requiring credentials at import time."""
        self._gemini_client = None
        self._groq_client = None
        self._configured_provider = None
        self._configured_model = None

    @property
    def provider(self) -> str:
        return (settings.llm_provider or "gemini").lower()

    @property
    def model(self) -> str:
        return settings.llm_model

    # Verified free-tier Gemini models when per-model daily quota (429) is exhausted
    GEMINI_FALLBACK_MODELS = [
        "models/gemini-3.5-flash-lite",
        "models/gemini-flash-lite-latest",
        "models/gemini-3.5-flash",
        "models/gemini-2.5-flash",
    ]

    def _get_gemini_client(self, model_override: Optional[str] = None):
        if not settings.is_llm_configured or not settings.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is not configured. Please set a valid key in .env.")
        target_model = model_override or self.model
        if self._gemini_client is None or self._configured_model != target_model:
            genai.configure(api_key=settings.gemini_api_key)
            self._gemini_client = genai.GenerativeModel(target_model)
            self._configured_model = target_model
        return self._gemini_client

    def _get_groq_client(self) -> AsyncGroq:
        if not settings.is_llm_configured or not settings.groq_api_key:
            raise LLMError("GROQ_API_KEY is not configured. Please set a valid key in .env.")
        if self._groq_client is None:
            self._groq_client = AsyncGroq(api_key=settings.groq_api_key)
        return self._groq_client

    def _clean_json_text(self, text: str) -> str:
        """Strip markdown code fences if present around JSON."""
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[BaseModel],
        max_retries: int = 2,
        timeout: int = 120,
        custom_validator: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """
        Generate structured output with strict schema validation and controlled repair retry.

        Args:
            system_prompt: System instructions
            user_prompt: User prompt with content
            response_schema: Pydantic model for validation
            max_retries: Maximum retry attempts
            timeout: Timeout in seconds
            custom_validator: Optional additional validator (e.g., evidence / fact_id check)
                              that raises ValueError on invalid content.

        Returns:
            Validated response dict

        Raises:
            LLMError: On failure after retries
        """
        if not settings.is_llm_configured:
            raise LLMError(f"LLM provider '{self.provider}' is not configured with a valid API key.")

        repair_feedback: Optional[str] = None

        async with _llm_semaphore:
            for attempt in range(max_retries + 1):
                effective_user_prompt = user_prompt
                if repair_feedback:
                    effective_user_prompt = (
                        f"{user_prompt}\n\n"
                        f"PREVIOUS RESPONSE FAILED VALIDATION:\n{repair_feedback}\n"
                        "Fix the JSON structure and citations to strictly match the schema and source documents "
                        "without inventing or changing clinical meaning."
                    )

                try:
                    if self.provider in ("claude", "anthropic"):
                        result = await self._call_claude(
                            system_prompt,
                            effective_user_prompt,
                            response_schema,
                            timeout
                        )
                    elif self.provider == "gemini":
                        result = await self._call_gemini(
                            system_prompt,
                            effective_user_prompt,
                            response_schema,
                            timeout
                        )
                    elif self.provider == "groq":
                        result = await self._call_groq(
                            system_prompt,
                            effective_user_prompt,
                            response_schema,
                            timeout
                        )
                    else:
                        raise LLMError(f"Unsupported LLM provider: {self.provider}")

                    validated = response_schema.model_validate(result)
                    dumped = validated.model_dump()

                    if custom_validator is not None:
                        custom_validator(dumped)

                    return dumped

                except (ValidationError, ValueError) as e:
                    repair_feedback = str(e)
                    logger.warning(f"Validation failed (attempt {attempt + 1}/{max_retries + 1}): {e}")
                    if attempt >= max_retries:
                        raise LLMError(f"Schema/evidence validation failed after {max_retries + 1} attempts: {e}")

                except LLMError as e:
                    repair_feedback = str(e)
                    logger.error(f"LLM call failed (attempt {attempt + 1}/{max_retries + 1}): {e}")
                    if attempt >= max_retries:
                        raise LLMError(f"LLM call failed after {max_retries + 1} attempts: {e}")

                except Exception as e:
                    logger.error(f"Unexpected LLM error (attempt {attempt + 1}/{max_retries + 1}): {e}")
                    if attempt >= max_retries:
                        raise LLMError(f"LLM call failed after {max_retries + 1} attempts: {e}")

                if attempt < max_retries:
                    await asyncio.sleep(min(2 ** attempt, 4))

            raise LLMError("Maximum retries exceeded")

    async def _call_claude(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[BaseModel],
        timeout: int
    ) -> Dict[str, Any]:
        """Call Anthropic Claude (Haiku 4.5) with structured JSON output."""
        api_key = settings.effective_claude_key
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not configured in .env.")

        schema = response_schema.model_json_schema()
        full_user_prompt = (
            f"{user_prompt}\n\n"
            f"Return ONLY valid JSON matching this exact JSON schema (no markdown fences, no commentary):\n"
            f"{json.dumps(schema)}"
        )

        # Try configured model name first, then standard Haiku 4.5 / 3.5 model IDs
        candidate_models = []
        for m in [self.model, "claude-haiku-4-5-20251001", "claude-haiku-4-5", "claude-3-5-haiku-latest"]:
            if m and m not in candidate_models:
                candidate_models.append(m)

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        last_err: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=min(timeout, 60)) as client:
            for model_name in candidate_models:
                try:
                    payload = {
                        "model": model_name,
                        "max_tokens": 8192,
                        "temperature": 0.1,
                        "system": system_prompt,
                        "messages": [
                            {"role": "user", "content": full_user_prompt}
                        ],
                    }
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers=headers,
                        json=payload,
                    )
                    if resp.status_code in (404, 400) and "model" in resp.text.lower():
                        last_err = LLMError(f"Claude model '{model_name}' unavailable: {resp.text[:200]}")
                        continue
                    if resp.status_code != 200:
                        raise LLMError(f"Claude API HTTP {resp.status_code}: {resp.text[:300]}")

                    data = resp.json()
                    if data.get("stop_reason") == "max_tokens":
                        raise LLMError("Claude output was truncated (stop_reason=max_tokens)")

                    content_blocks = data.get("content") or []
                    text_parts = [
                        b.get("text", "")
                        for b in content_blocks
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    text = self._clean_json_text("".join(text_parts))
                    if not text:
                        raise LLMError("Empty response from Claude")
                    return json.loads(text)

                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse Claude JSON response ({model_name}): {e}")
                    raise LLMError(f"Invalid JSON response from Claude: {e}")
                except Exception as e:
                    last_err = e
                    if settings.gemini_api_key and ("401" in str(e) or "403" in str(e) or "429" in str(e)):
                        logger.warning(f"Claude API call failed ({e}); falling back to configured Gemini key...")
                        return await self._call_gemini(system_prompt, user_prompt, response_schema, timeout)
                    raise LLMError(f"Claude error: {e}")

        raise LLMError(f"Claude error: {last_err}")

    async def _call_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[BaseModel],
        timeout: int
    ) -> Dict[str, Any]:
        """Call Gemini with structured output, truncation check, and automatic 429 model fallback."""
        schema = response_schema.model_json_schema()
        full_prompt = (
            f"{system_prompt}\n\n{user_prompt}\n\n"
            f"Return valid JSON matching this schema: {json.dumps(schema)}"
        )

        # Prefer active/last-working model first, then fallback models if 429 quota is hit
        start_model = self._configured_model or self.model
        raw_candidates = [start_model, self.model] + self.GEMINI_FALLBACK_MODELS
        candidate_models = []
        seen_normalized = set()
        for m in raw_candidates:
            norm = m if m.startswith("models/") else f"models/{m}"
            if norm not in seen_normalized:
                seen_normalized.add(norm)
                candidate_models.append(norm)

        last_err: Optional[Exception] = None
        for model_name in candidate_models:
            try:
                client = self._get_gemini_client(model_override=model_name)
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.generate_content,
                        full_prompt,
                        generation_config=genai.GenerationConfig(
                            temperature=0.1,
                            response_mime_type="application/json"
                        ),
                        request_options={"timeout": min(timeout, 45)}
                    ),
                    timeout=min(timeout, 50)
                )

                # Check for truncated output if candidates metadata is available
                candidates = getattr(response, "candidates", None)
                if candidates:
                    finish_reason = getattr(candidates[0], "finish_reason", None)
                    finish_name = getattr(finish_reason, "name", str(finish_reason or "")).upper()
                    if "MAX_TOKENS" in finish_name or finish_reason == 2:
                        raise LLMError("Gemini output was truncated (MAX_TOKENS)")

                text = self._clean_json_text(response.text)
                if not text:
                    raise LLMError("Empty response from Gemini")
                return json.loads(text)

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Gemini JSON response ({model_name}): {e}")
                raise LLMError(f"Invalid JSON response: {e}")
            except asyncio.TimeoutError:
                raise LLMError(f"Gemini call timed out after {timeout}s")
            except LLMError:
                raise
            except Exception as e:
                err_str = str(e)
                last_err = e
                if "429" in err_str or "Quota exceeded" in err_str or "ResourceExhausted" in err_str or "404" in err_str:
                    logger.warning(f"Gemini model '{model_name}' hit quota/availability limit ({err_str[:120]}); trying next fallback model...")
                    continue
                logger.error(f"Gemini call failed ({model_name}): {e}")
                raise LLMError(f"Gemini error: {e}")

        logger.error(f"All Gemini models exhausted or failed: {last_err}")
        raise LLMError(f"Gemini error: {last_err}")

    async def _call_groq(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[BaseModel],
        timeout: int
    ) -> Dict[str, Any]:
        """Call Groq with structured output and truncation check."""
        try:
            client = self._get_groq_client()
            schema = response_schema.model_json_schema()

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{user_prompt}\n\nReturn valid JSON matching this schema: {json.dumps(schema)}"}
            ]

            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    response_format={"type": "json_object"}
                ),
                timeout=timeout
            )

            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason == "length":
                raise LLMError("Groq output was truncated (finish_reason=length)")

            text = self._clean_json_text(choice.message.content or "")
            if not text:
                raise LLMError("Empty response from Groq")
            return json.loads(text)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Groq JSON response: {e}")
            raise LLMError(f"Invalid JSON response: {e}")
        except asyncio.TimeoutError:
            raise LLMError(f"Groq call timed out after {timeout}s")
        except LLMError:
            raise
        except Exception as e:
            logger.error(f"Groq call failed: {e}")
            raise LLMError(f"Groq error: {e}")

    async def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text (1 token ≈ 4 chars)."""
        return max(1, len(text or "") // 4)

    def get_context_limit(self) -> int:
        """Get model context window size."""
        limits = {
            "gemini-1.5-flash": 1_000_000,
            "gemini-1.5-pro": 2_000_000,
            "gemini-2.0-flash-exp": 1_000_000,
            "llama-3.1-70b-versatile": 128_000,
            "llama-3.1-8b-instant": 128_000,
            "llama-3.3-70b-versatile": 128_000,
        }
        return limits.get(self.model, 100_000)


# Singleton instance (lazy — safe to import without API keys)
llm_client = LLMClient()
