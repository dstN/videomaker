"""
Claude Service — story writing via the Anthropic API (Claude Opus 5.5).

Used for the creative steps (new stories, follow-ups, rewrites) when
STORY_LLM=claude; Gemini stays responsible for splitting, descriptions and
translation. Callers fall back to Gemini on ClaudeError.

Cost notes:
  - signs_of_ai_writing.pdf is sent as extracted text (~37k tokens) instead of
    a PDF document (~56 pages as text + images). It sits in the system prompt
    behind a cache breakpoint, so back-to-back calls (e.g. a rewrite batch)
    read it from the prompt cache.
  - Structured output (JSON schema) — no hand-rolled JSON repair.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from app.config import get_settings

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=BaseModel)

# Refusals are re-run server-side on Anthropic's recommended fallback model.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_MAX_TOKENS = 32_000  # a 2000-word story is ~3k tokens; the rest is thinking headroom

_client: anthropic.AsyncAnthropic | None = None
_ai_signs_text: str | None = None


class ClaudeError(Exception):
    """Claude could not deliver a usable result (not configured, refused, truncated, API error)."""


def is_configured() -> bool:
    return bool(get_settings().anthropic_api_key)


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise ClaudeError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _load_ai_signs_text() -> str | None:
    """
    Text of signs_of_ai_writing.pdf, extracted once and cached next to the PDF
    as .txt (plain text is ~3x cheaper than sending the PDF pages).
    """
    global _ai_signs_text
    if _ai_signs_text is not None:
        return _ai_signs_text

    pdf_path: Path = get_settings().ai_pdf_path
    txt_path = pdf_path.with_suffix(".txt")
    if txt_path.exists():
        _ai_signs_text = txt_path.read_text(encoding="utf-8")
        return _ai_signs_text
    if not pdf_path.exists():
        logger.warning("%s not found — Claude runs without the anti-AI reference.", pdf_path)
        return None

    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    try:
        txt_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not cache extracted PDF text at %s: %s", txt_path, exc)
    logger.info("Extracted %d chars from %s", len(text), pdf_path.name)
    _ai_signs_text = text
    return text


def _system_blocks(rules: str) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": rules}]
    reference = _load_ai_signs_text()
    if reference:
        blocks.append({
            "type": "text",
            "text": (
                "REFERENCE — the following field guide lists patterns that mark text as "
                "AI-written. Treat every pattern in it as forbidden in your writing.\n\n"
                + reference
            ),
        })
    # Cache breakpoint after the last stable block (rules + reference)
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _schema_for(model_cls: type[BaseModel]) -> dict:
    """Pydantic JSON schema with every object closed and all fields required."""
    schema = model_cls.model_json_schema()

    def _close(node: dict) -> None:
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
            for prop in node["properties"].values():
                _close(prop)
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            _close(node["items"])

    _close(schema)
    for definition in schema.get("$defs", {}).values():
        _close(definition)
    return schema


async def generate_json(
    rules: str,
    task_prompt: str,
    model_cls: type[_ModelT],
    effort: str | None = None,
) -> _ModelT:
    """Run one Claude request with a JSON-schema response and validate it into model_cls."""
    settings = get_settings()
    client = _get_client()
    try:
        async with client.beta.messages.stream(
            model=settings.claude_model,
            max_tokens=_MAX_TOKENS,
            betas=[_FALLBACK_BETA],
            fallbacks="default",
            system=_system_blocks(rules),
            messages=[{"role": "user", "content": task_prompt}],
            output_config={
                "effort": effort or settings.claude_effort,
                "format": {"type": "json_schema", "schema": _schema_for(model_cls)},
            },
        ) as stream:
            message = await stream.get_final_message()
    except anthropic.APIConnectionError as exc:
        raise ClaudeError(f"Cannot reach the Anthropic API: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise ClaudeError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

    usage = message.usage
    logger.info(
        "Claude %s | in=%d (cache read %d, write %d) out=%d | stop=%s",
        message.model, usage.input_tokens, usage.cache_read_input_tokens or 0,
        usage.cache_creation_input_tokens or 0, usage.output_tokens, message.stop_reason,
    )

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        raise ClaudeError(f"Claude declined the request ({getattr(details, 'category', None)}).")
    if message.stop_reason == "max_tokens":
        raise ClaudeError("Claude response was cut off at max_tokens.")

    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        return model_cls.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ClaudeError(f"Claude returned invalid JSON: {exc}") from exc
