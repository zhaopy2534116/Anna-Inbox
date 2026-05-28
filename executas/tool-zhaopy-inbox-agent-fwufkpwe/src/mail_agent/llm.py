

"""LLM adapter using DashScope or Anna Executa sampling."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Awaitable

# 兼容旧调用签名，pipeline 仍会传入 sampling_create_message 参数。
LlmFunc = Callable[..., Awaitable[dict[str, Any]]]
DASHSCOPE_API_KEY_ENV = "DASHSCOPE_API_KEY"
DASHSCOPE_MODEL_ENV = "DASHSCOPE_MODEL"
DASHSCOPE_DEFAULT_MODEL = "qwen3-max"
DASHSCOPE_CHAT_COMPLETIONS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MAX_RETRIES = 2
BASE_DELAY = 1.2

# Surrogate range: U+D800–U+DFFF
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xE000


def _sanitize_str(s: str) -> str:
    """Replace lone surrogate characters that break JSON/UTF-8 encoding."""
    if not isinstance(s, str):
        return str(s) if s is not None else ""
    # Fast path: most strings are clean
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        pass
    # Slow path: replace surrogates character by character
    return "".join(
        c if ord(c) < _SURROGATE_START or ord(c) >= _SURROGATE_END else "�"
        for c in s
    )


def _sanitize_value(obj: Any) -> Any:
    """Recursively sanitize surrogate characters from all strings in a nested structure."""
    if isinstance(obj, str):
        return _sanitize_str(obj)
    if isinstance(obj, dict):
        return {_sanitize_str(str(k)): _sanitize_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_value(item) for item in obj]
    return obj


def _repair_json(text: str) -> str:
    """Fix common LLM JSON mistakes so json.loads has a better chance.

    Only applies safe, regex-based fixes that won't corrupt valid JSON:
    1. Missing comma between consecutive string values on adjacent lines
    2. Trailing comma before } or ]
    """
    # Trailing comma before ] or }  (e.g. {"a": 1,} → {"a": 1})
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Missing comma: "value"\n  "next_key"  →  "value",\n  "next_key"
    text = re.sub(r'"\s*\n\s*"', '",\n"', text)
    # 中文注释：Anna sampling 偶尔会在同一行的下一个 key 前漏逗号。
    text = re.sub(r'(?<=")\s+(?="[^"\r\n]{1,80}"\s*:)', ', ', text)
    # Missing comma: }\n  "next_key"  →  },\n  "next_key"
    text = re.sub(r'}\s*\n\s*"', '},\n"', text)
    # 中文注释：数组里的相邻对象如果少逗号，json.loads 会报 Expecting delimiter。
    text = re.sub(r'}\s*{', '},{', text)
    # Missing comma: ]\n  "next_key"  →  ],\n  "next_key"
    text = re.sub(r']\s*\n\s*"', '],\n"', text)
    text = re.sub(r'(?<=])\s+(?="[^"\r\n]{1,80}"\s*:)', ', ', text)
    # Missing comma: number\n  "next_key"  →  number,\n  "next_key"
    text = re.sub(r'(\d)\s*\n\s*"', r'\1,\n"', text)
    text = re.sub(r'(?<=\d)\s+(?="[^"\r\n]{1,80}"\s*:)', ', ', text)
    # Missing comma: true\n  "next_key" | false\n  "next_key" | null\n  "next_key"
    text = re.sub(r'(true|false|null)\s*\n\s*"', r'\1,\n"', text)
    text = re.sub(r'\b(true|false|null)\s+(?="[^"\r\n]{1,80}"\s*:)', r'\1, ', text)
    return text


def parse_json_response(text: str) -> dict[str, Any]:
    """Extract JSON object from LLM response text (handles markdown fences and common LLM errors)."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*)\n?```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        repaired = _repair_json(text)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as repaired_exc:
            start_excerpt = max(0, repaired_exc.pos - 140)
            end_excerpt = min(len(repaired), repaired_exc.pos + 140)
            excerpt = repaired[start_excerpt:end_excerpt].replace("\n", "\\n")
            raise ValueError(
                f"{repaired_exc.msg}: line {repaired_exc.lineno} column {repaired_exc.colno} "
                f"(char {repaired_exc.pos}); excerpt={excerpt}"
            ) from exc


def _build_sampling_json_user_message(system_prompt: str, user_message: str, retry_note: str = "") -> str:
    prompt = (
        f"{user_message.strip()}\n\n"
        "Return ONLY one valid JSON object. Do not include markdown fences, prose, analysis, or code comments."
    )
    if retry_note:
        prompt = f"{prompt}\n\n{retry_note}"
    return prompt


def extract_sampling_text(result: Any) -> str:
    # 中文注释：不同 host 版本可能返回 content.text、content 字符串、content 数组或 OpenAI 风格 message.content。
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if isinstance(text, str):
            return text
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    message = result.get("message")
    if isinstance(message, dict):
        nested = extract_sampling_text({"content": message.get("content")})
        if nested:
            return nested
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        nested = extract_sampling_text(first)
        if nested:
            return nested
        nested = extract_sampling_text(first.get("message") if isinstance(first.get("message"), dict) else {})
        if nested:
            return nested
    return ""


def _sampling_result_shape(result: Any) -> str:
    # 中文注释：错误信息只带响应形态，不带正文，避免日志泄漏邮件内容。
    if not isinstance(result, dict):
        return type(result).__name__
    content = result.get("content")
    if isinstance(content, dict):
        content_shape = f"dict:{','.join(sorted(str(k) for k in content.keys()))}"
        text = content.get("text")
        if isinstance(text, str):
            content_shape = f"{content_shape}; text_len={len(text)}"
    elif isinstance(content, list):
        content_shape = f"list:{len(content)}"
        text_len = 0
        for item in content:
            if isinstance(item, str):
                text_len += len(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text_len += len(item["text"])
        content_shape = f"{content_shape}; text_len={text_len}"
    else:
        content_shape = type(content).__name__
        if isinstance(content, str):
            content_shape = f"{content_shape}; text_len={len(content)}"
    return f"keys={','.join(sorted(str(k) for k in result.keys()))}; content={content_shape}"


def dashscope_available() -> bool:
    return bool(os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip())


def _call_dashscope_json(
    *,
    system_prompt: str,
    user_message: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    api_key = os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{DASHSCOPE_API_KEY_ENV} is not set")

    model = os.environ.get(DASHSCOPE_MODEL_ENV, "").strip() or DASHSCOPE_DEFAULT_MODEL
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": _sanitize_str(system_prompt)},
                    {"role": "user", "content": _sanitize_str(user_message)},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "enable_thinking": False,
            }
            request = urllib.request.Request(
                DASHSCOPE_CHAT_COMPLETIONS_URL,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            result = json.loads(raw)
            choices = result.get("choices") if isinstance(result, dict) else None
            message = ((choices or [{}])[0].get("message") or {}) if isinstance(choices, list) else {}
            text = message.get("content") if isinstance(message, dict) else ""
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty DashScope response")
            text = _sanitize_str(text)
            return {
                "payload": _sanitize_value(parse_json_response(text)),
                "text": text,
                "model": result.get("model") or model,
                "usage": result.get("usage"),
                "provider": "dashscope",
            }
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
        if attempt >= MAX_RETRIES:
            break
        time.sleep(min(BASE_DELAY ** (attempt + 1), 6.0))

    raise RuntimeError(f"DashScope call failed after {MAX_RETRIES + 1} attempts: {last_error}")


def call_dashscope_text(
    *,
    system_prompt: str,
    user_message: str,
    temperature: float = 0.2,
    max_tokens: int = 512,
    timeout: float = 60.0,
) -> dict[str, Any]:
    api_key = os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{DASHSCOPE_API_KEY_ENV} is not set")

    model = os.environ.get(DASHSCOPE_MODEL_ENV, "").strip() or DASHSCOPE_DEFAULT_MODEL
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _sanitize_str(system_prompt)},
            {"role": "user", "content": _sanitize_str(user_message)},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enable_thinking": False,
    }
    request = urllib.request.Request(
        DASHSCOPE_CHAT_COMPLETIONS_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    result = json.loads(raw)
    choices = result.get("choices") if isinstance(result, dict) else None
    message = ((choices or [{}])[0].get("message") or {}) if isinstance(choices, list) else {}
    text = message.get("content") if isinstance(message, dict) else ""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty DashScope response")
    return {
        "text": text,
        "model": result.get("model") or model,
        "usage": result.get("usage"),
        "provider": "dashscope",
    }


async def call_llm_json(
    sampling_create_message: Any,
    *,
    system_prompt: str,
    user_message: str,
    temperature: float = 0.1,
    max_tokens: int = 512,
    timeout: float = 60.0,
    metadata: dict[str, str] | None = None,
    allow_sampling_provider_fallback: bool = True,
    stop_sequences: list[str] | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Call the selected LLM and return parsed JSON payload.

    Returns: {"payload": {...}, "text": "...", "model": "...", "usage": {...}}
    """
    if sampling_create_message is not None:
        last_error: Exception | None = None
        last_text = ""
        attempts = max(1, max_attempts or (MAX_RETRIES + 1))
        for attempt in range(attempts):
            try:
                retry_note = ""
                if last_text:
                    retry_note = (
                        "Your previous response was not parseable as a JSON object. "
                        f"Parser error: {last_error}. "
                        f"Previous response excerpt: {last_text[:800]}. "
                        "Return the corrected JSON object only."
                    )
                metadata_payload = {str(key): str(value) for key, value in (metadata or {}).items()}
                result = await sampling_create_message(
                    messages=[
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": _sanitize_str(
                                    _build_sampling_json_user_message(system_prompt, user_message, retry_note)
                                ),
                            },
                        }
                    ],
                    max_tokens=max_tokens,
                    system_prompt=_sanitize_str(system_prompt),
                    temperature=temperature,
                    include_context="none",
                    metadata=metadata_payload,
                    timeout=timeout,
                    stop_sequences=stop_sequences,
                )
                text = extract_sampling_text(result)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError(f"empty Anna sampling response ({_sampling_result_shape(result)})")
                text = _sanitize_str(text)
                last_text = text
                return {
                    "payload": _sanitize_value(parse_json_response(text)),
                    "text": text,
                    "model": result.get("model"),
                    "usage": result.get("usage"),
                    "provider": "anna-sampling",
                }
            except Exception as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break
                await asyncio.sleep(min(BASE_DELAY ** (attempt + 1), 6.0))

        if allow_sampling_provider_fallback and dashscope_available():
            return await asyncio.to_thread(
                _call_dashscope_json,
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )

        raise RuntimeError(
            f"Anna sampling failed after {attempts} attempts and DashScope is not available: {last_error}"
        ) from last_error

    # No Anna Sampling at all; go directly to DashScope
    return await asyncio.to_thread(
        _call_dashscope_json,
        system_prompt=system_prompt,
        user_message=user_message,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


async def call_llm_json_safe(
    sampling_create_message: Any,
    *,
    system_prompt: str,
    user_message: str,
    fallback: dict[str, Any],
    temperature: float = 0.1,
    max_tokens: int = 512,
    timeout: float = 60.0,
    metadata: dict[str, str] | None = None,
    allow_fallback: bool = True,
    allow_sampling_provider_fallback: bool = True,
    stop_sequences: list[str] | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Call LLM with fallback on failure."""
    try:
        result = await call_llm_json(
            sampling_create_message,
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            metadata=metadata,
            allow_sampling_provider_fallback=allow_sampling_provider_fallback,
            stop_sequences=stop_sequences,
            max_attempts=max_attempts,
        )
        result["fallback_used"] = False
        return result
    except Exception as exc:
        if not allow_fallback:
            raise
        return {
            "payload": fallback,
            "text": "",
            "model": None,
            "usage": None,
            "fallback_used": True,
            "fallback_reason": str(exc),
        }
