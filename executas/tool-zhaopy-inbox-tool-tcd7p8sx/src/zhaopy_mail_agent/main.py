from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Windows pipe encoding: -X utf8 covers console but not pipes;
# explicit reconfigure ensures stdin/stdout are UTF-8 regardless.
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        log(f"reconfigure {_stream} failed")

# Ensure src/ is on sys.path so mail_agent and executa_sdk are importable
# when running via `py -3 src/zhaopy_mail_agent/main.py`
_SRC_DIR = str(Path(__file__).resolve().parents[1])
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from executa_sdk import PROTOCOL_VERSION_V2, SamplingClient, SamplingError
from executa_sdk.storage import StorageClient, FilesClient, StorageError, make_response_router

JSONRPC_VERSION = "2.0"
TOOL_ID = "tool-zhaopy-inbox-tool-tcd7p8sx"
VERSION = "0.1.0"
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
STDOUT_LOCK = threading.Lock()
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_STDIO_MESSAGE_BYTES = 512 * 1024
SUPPORTED_MAILBOXES = {
    "zhaopy2121@gamil.com": "zhaopy2121@gmail.com",
    "zhaopy2121@gmail.com": "zhaopy2121@gmail.com",
    "kate@anna.partners": "kate@anna.partners",
    "hr@anna.partners": "hr@anna.partners",
}

MANIFEST = {
    "name": TOOL_ID,
    "display_name": "Zhaopy Mail Agent RD6B87R5",
    "version": VERSION,
    "description": "Minimal Anna Executa skeleton for reading Gmail through local token files and DashScope or Anna sampling LLM.",
    "author": "Zhaopy",
    "host_capabilities": ["llm.sample", "llm.complete", "aps.kv"],
    "credentials": [
        {
            "name": "GMAIL_ACCESS_TOKEN",
            "display_name": "Gmail Access Token",
            "description": "Optional Google OAuth access token. Local token files are used when platform injection is unavailable.",
            "required": False,
            "sensitive": True,
        },
        {
            "name": "GOOGLE_ACCESS_TOKEN",
            "display_name": "Google Access Token",
            "description": "Alternative Google OAuth access token name supported by Anna platform credential mapping.",
            "required": False,
            "sensitive": True,
        },
        {
            "name": "DASHSCOPE_API_KEY",
            "display_name": "DashScope API Key",
            "description": "DashScope API key used by the mail-agent LLM evaluator.",
            "required": False,
            "sensitive": True,
        },
        {
            "name": "DASHSCOPE_MODEL",
            "display_name": "DashScope Model",
            "description": "Optional DashScope model override. Defaults to qwen3-max.",
            "required": False,
            "sensitive": False,
        },
    ],
    "tools": [
        {
            "name": "check_google_oauth",
            "description": "Check whether Anna injected a Google/Gmail OAuth credential for this invocation.",
            "parameters": [],
        },
        {
            "name": "ask_anna_llm",
            "description": "Deprecated compatibility alias for ask_dashscope_llm. This still uses DashScope, not Anna sampling.",
            "parameters": [
                {
                    "name": "message",
                    "type": "string",
                    "description": "User message to send to DashScope.",
                    "required": True,
                }
            ],
        },
        {
            "name": "ask_dashscope_llm",
            "description": "Ask the configured DashScope model. This is a direct DashScope wiring test.",
            "parameters": [
                {
                    "name": "message",
                    "type": "string",
                    "description": "User message to send to DashScope.",
                    "required": True,
                }
            ],
        },
        {
            "name": "ask_anna_sampling",
            "description": "Ask Anna LLM through sampling/createMessage and return the model response.",
            "parameters": [
                {
                    "name": "message",
                    "type": "string",
                    "description": "User message to send through Anna sampling.",
                    "required": True,
                },
                {
                    "name": "system_prompt",
                    "type": "string",
                    "description": "Optional system prompt.",
                    "required": False,
                },
                {
                    "name": "max_tokens",
                    "type": "integer",
                    "description": "Maximum output tokens. Defaults to 512.",
                    "required": False,
                },
                {
                    "name": "temperature",
                    "type": "number",
                    "description": "Sampling temperature. Defaults to 0.2.",
                    "required": False,
                },
                {
                    "name": "model_preference",
                    "type": "string",
                    "description": "Optional model hint such as gpt or claude.",
                    "required": False,
                },
            ],
        },
        {
            "name": "test_anna_sampling",
            "description": "Smoke-test Anna sampling/createMessage without touching Gmail or the mail-agent pipeline.",
            "parameters": [
                {
                    "name": "prompt",
                    "type": "string",
                    "description": "User message to send through Anna sampling.",
                    "required": True,
                },
                {
                    "name": "system_prompt",
                    "type": "string",
                    "description": "Optional system prompt.",
                    "required": False,
                },
                {
                    "name": "max_tokens",
                    "type": "integer",
                    "description": "Maximum output tokens. Defaults to 64.",
                    "required": False,
                },
                {
                    "name": "temperature",
                    "type": "number",
                    "description": "Sampling temperature. Defaults to 0.2.",
                    "required": False,
                },
                {
                    "name": "model_preference",
                    "type": "string",
                    "description": "Optional model hint such as gpt or claude.",
                    "required": False,
                },
            ],
        },
        {
            "name": "test_aps_storage",
            "description": "Smoke-test Anna Persistent Storage KV without touching Gmail or the mail-agent pipeline.",
            "parameters": [
                {
                    "name": "key_suffix",
                    "type": "string",
                    "description": "Optional suffix for the temporary debug key.",
                    "required": False,
                },
                {
                    "name": "value",
                    "type": "string",
                    "description": "Optional value to write and read back.",
                    "required": False,
                },
            ],
        },
        {
            "name": "read_primary_emails",
            "description": "Read recent Gmail Primary emails through local token files, save them as JSON, and deduplicate by message id.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "limit", "type": "integer", "description": "How many recent Primary messages to read.", "required": False},
            ],
        },
        {
            "name": "list_cached_emails",
            "description": "List compact local Gmail message summaries for one mailbox.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
            ],
        },
        {
            "name": "get_cached_email",
            "description": "Read one full locally cached Gmail message JSON by message id.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email.", "required": True},
                {"name": "message_id", "type": "string", "description": "Gmail message id.", "required": True},
            ],
        },
        {
            "name": "run_mail_agent",
            "description": "Run the full Anna mail agent pipeline: scan emails, generate candidates, evaluate with LLM, and produce an action plan.",
            "parameters": [
                {"name": "user_request", "type": "string", "description": "Natural language request from user, e.g. '帮我看看有什么重要邮件'", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "mode", "type": "string", "description": "Strategy mode: auto, default_secretary, creator_opportunity, security_billing", "required": False},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
            ],
        },
        {
            "name": "start_mail_agent_run",
            "description": "Start the full mail-agent pipeline in the background and poll status separately.",
            "parameters": [
                {"name": "user_request", "type": "string", "description": "Natural language request from user.", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "mode", "type": "string", "description": "Strategy mode: auto, default_secretary, creator_opportunity, security_billing", "required": False},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
            ],
        },
        {
            "name": "get_mail_agent_run",
            "description": "Poll a background mail-agent run by run id.",
            "parameters": [
                {"name": "run_id", "type": "string", "description": "Run id returned by start_mail_agent_run.", "required": True},
            ],
        },
        {
            "name": "get_active_cards",
            "description": "Get all active attention cards for a mailbox from persistent storage.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
            ],
        },
        {
            "name": "get_scan_plan",
            "description": "Get the current scan plan configuration for a mailbox (schedule, time range, priorities, etc.).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
            ],
        },
        {
            "name": "set_scan_plan",
            "description": "Save or update the scan plan for a mailbox.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "schedule", "type": "string", "description": "manual | every_morning | every_afternoon | twice_daily | workdays", "required": False},
                {"name": "time_range", "type": "string", "description": "auto | since_last | last_24h | last_7d | unread_backlog", "required": False},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages per scan (50-300).", "required": False},
                {"name": "include_newsletters", "type": "boolean", "description": "Whether to include newsletter emails.", "required": False},
                {"name": "include_promotions", "type": "boolean", "description": "Whether to include promotion emails.", "required": False},
                {"name": "batch_behavior", "type": "string", "description": "ask | auto_300 | never_older", "required": False},
                {"name": "active", "type": "boolean", "description": "Whether the plan is active.", "required": False},
            ],
        },
        {
            "name": "get_card_detail",
            "description": "Get a single card's full detail including thread context.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID from get_active_cards.", "required": True},
            ],
        },
        {
            "name": "summarize_thread",
            "description": "Ask Anna to summarize a thread behind a card.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
            ],
        },
        {
            "name": "generate_draft_reply",
            "description": "Ask Anna to draft a reply for a card's thread.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "reply_mode", "type": "string", "description": "reply_to_sender or reply_all.", "required": False},
            ],
        },
        {
            "name": "revise_draft",
            "description": "Ask Anna to revise a draft reply based on user feedback.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "current_draft", "type": "string", "description": "The current draft text to revise.", "required": True},
                {"name": "revision_input", "type": "string", "description": "User's revision request.", "required": True},
            ],
        },
        {
            "name": "start_summarize_thread",
            "description": "Start a background thread summarization. Returns run_id immediately. Poll with get_mail_agent_run.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "ai_provider", "type": "string", "description": "LLM provider.", "required": False},
            ],
        },
        {
            "name": "start_generate_draft",
            "description": "Start background draft generation or revision. If current_draft is provided, revises it. Returns run_id immediately. Poll with get_mail_agent_run.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "reply_mode", "type": "string", "description": "reply_to_sender or reply_all.", "required": False},
                {"name": "current_draft", "type": "string", "description": "Existing draft to revise (optional).", "required": False},
                {"name": "revision_input", "type": "string", "description": "User instructions for generation or revision (optional).", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider.", "required": False},
            ],
        },
        {
            "name": "record_card_decision",
            "description": "Record a user decision on a card (no_action_needed, handled_manually, dismissed).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "decision", "type": "string", "description": "no_action_needed | handled_manually | dismissed.", "required": True},
            ],
        },
        {
            "name": "record_snooze",
            "description": "Snooze a card (tomorrow, next_week) or record a permanent preference (dont_prioritize).",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "snooze_option", "type": "string", "description": "tomorrow | next_week | dont_prioritize.", "required": True},
            ],
        },
        {
            "name": "restore_card",
            "description": "Restore a snoozed/dismissed/resolved card back to pending.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
            ],
        },
        {
            "name": "record_learning",
            "description": "Record a learning feedback from the user about email patterns.",
            "parameters": [
                {"name": "pattern", "type": "string", "description": "The pattern the user noticed.", "required": True},
                {"name": "action", "type": "string", "description": "What action to remember.", "required": True},
            ],
        },
        {
            "name": "reply_now",
            "description": "Send the draft reply via Gmail. Default dry_run=True mocks the send without actually emailing.",
            "parameters": [
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "card_id", "type": "string", "description": "Card ID.", "required": True},
                {"name": "draft_body", "type": "string", "description": "The draft body to send.", "required": True},
                {"name": "reply_mode", "type": "string", "description": "reply_to_sender | reply_all.", "required": False},
                {"name": "dry_run", "type": "boolean", "description": "If true (default), mock the send without actually emailing.", "required": False},
            ],
        },
        {
            "name": "get_run_history",
            "description": "Get the history of past mail agent runs.",
            "parameters": [],
        },
        {
            "name": "start_custom_scan",
            "description": "Start a custom (Ask) scan: generate a plan from natural language and execute it in the background.",
            "parameters": [
                {"name": "user_request", "type": "string", "description": "Natural language request from user.", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
            ],
        },
        {
            "name": "re_run_custom_scan",
            "description": "Re-run a previously saved custom scan plan against the current inbox (skips LLM planning).",
            "parameters": [
                {"name": "plan_id", "type": "string", "description": "Plan ID from get_custom_plans.", "required": True},
                {"name": "mailbox", "type": "string", "description": "Mailbox email address.", "required": True},
                {"name": "max_messages", "type": "integer", "description": "Maximum messages to scan.", "required": False},
                {"name": "primary_count", "type": "integer", "description": "How many recent Primary emails to fetch first.", "required": False},
                {"name": "ai_provider", "type": "string", "description": "LLM provider: dashscope or anna-llm.", "required": False},
            ],
        },
        {
            "name": "get_authorized_email",
            "description": "Discover the authorized Gmail account from the platform-injected token. Returns empty string in local dev mode.",
            "parameters": [],
        },
        {
            "name": "get_custom_plans",
            "description": "List all saved custom scan plans with lightweight metadata.",
            "parameters": [],
        },
        {
            "name": "get_custom_plan_detail",
            "description": "Get a single custom plan's full details.",
            "parameters": [
                {"name": "plan_id", "type": "string", "description": "Plan ID from get_custom_plans.", "required": True},
            ],
        },
    ],
    "runtime": {"type": "python", "min_version": "3.10"},
}


def log(message: str) -> None:
    sys.stderr.write(f"[{TOOL_ID}] {message}\n")
    sys.stderr.flush()


def beijing_now() -> str:
    return datetime.now(BEIJING_TZ).isoformat()


def make_response(request_id: Any, result: Any | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id}
    if error is None:
        response["result"] = result
    else:
        response["error"] = error
    return response


def make_error(code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return error


def _sampling_result_shape(result: Any) -> str:
    # 中文注释：只记录响应形态，不记录模型正文，便于定位 Anna sampling 空响应。
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


def write_frame(message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))

    # Guard against lone surrogates (U+D800–U+DFFF) from LLM output or
    # cached email data that would break UTF-8 encoding on stdout.
    payload_bytes: bytes
    try:
        payload_bytes = payload.encode("utf-8")
    except UnicodeEncodeError:
        # Slow path: replace surrogates character by character
        payload = "".join(c if ord(c) < 0xD800 or ord(c) >= 0xE000 else "?" for c in payload)
        payload_bytes = payload.encode("utf-8")

    with STDOUT_LOCK:
        if len(payload_bytes) > MAX_STDIO_MESSAGE_BYTES:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="executa-resp-", delete=False, encoding="utf-8") as handle:
                handle.write(payload)
                path = handle.name
            sys.stdout.write(json.dumps({"jsonrpc": JSONRPC_VERSION, "id": message.get("id"), "__file_transport": path}, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(payload + "\n")
        sys.stdout.flush()


sampling = SamplingClient(write_frame=write_frame)


def _normalize_storage_provider(value: Any = "") -> str:
    raw = str(value or "").strip().lower()
    if raw in {"aps", "anna", "anna-aps"}:
        return "aps"
    if raw in {"local", "local-json", "json", "file"}:
        return "local"
    return "aps"


_aps_storage = StorageClient(write_frame=write_frame)
_aps_files = FilesClient(write_frame=write_frame)
_aps_route_storage_response = make_response_router(_aps_storage, _aps_files)

from mail_agent.local_storage import make_local_clients
_local_data_dir = Path(os.environ.get("ZHAOPY_MAIL_AGENT_STORAGE_DIR") or (Path(__file__).resolve().parents[1] / ".local_storage")).expanduser().resolve()
_local_storage, _local_files = make_local_clients(_local_data_dir)

from mail_agent.storage_client import init as init_storage_singleton
_active_storage_provider = ""
_route_storage_response = lambda msg: False


def _set_storage_backend(provider: Any = "") -> str:
    global _active_storage_provider, _route_storage_response
    selected = _normalize_storage_provider(provider)
    if selected == "aps":
        init_storage_singleton(_aps_storage, _aps_files, scope="user")
        _route_storage_response = _aps_route_storage_response
    else:
        init_storage_singleton(_local_storage, _local_files, scope="user")
        _route_storage_response = lambda msg: False
    if selected != _active_storage_provider:
        # 中文注释：调试开关允许前端按工具调用切换 APS 或本地 JSON 存储。
        log("storage backend: aps" if selected == "aps" else f"storage backend: local-json dir={_local_data_dir}")
    _active_storage_provider = selected
    return selected


def _apply_storage_provider(arguments: dict[str, Any]) -> str:
    if "storage_provider" in arguments:
        return _set_storage_backend(arguments.get("storage_provider"))
    return _active_storage_provider or _set_storage_backend(os.environ.get("ANNA_STORAGE_BACKEND", "local"))


def _should_use_aps_storage() -> bool:
    return _active_storage_provider == "aps"


_set_storage_backend(os.environ.get("ANNA_STORAGE_BACKEND", "local"))

loop = asyncio.new_event_loop()
loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
loop_thread.start()
MAIL_AGENT_RUNS: dict[str, dict[str, Any]] = {}
RUN_STATE_LOCK = threading.RLock()
RUN_CHECKPOINT_DIR = Path(__file__).resolve().parents[1] / ".local_storage" / "runs" / "background"


def _run_checkpoint_path(run_id: str) -> Path:
    safe_id = "".join(ch for ch in str(run_id or "") if ch.isalnum() or ch in {"_", "-"})
    return RUN_CHECKPOINT_DIR / f"{safe_id}.json"


def _save_run_checkpoint(run_id: str) -> None:
    # 中文注释：后台任务是进程内存态；落盘用于本地 runtime 重启后的轮询诊断。
    with RUN_STATE_LOCK:
        state = MAIL_AGENT_RUNS.get(run_id)
        if not state:
            return
        RUN_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        path = _run_checkpoint_path(run_id)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
        tmp_path.replace(path)


def _load_run_checkpoint(run_id: str) -> dict[str, Any] | None:
    path = _run_checkpoint_path(run_id)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _get_run_state(run_id: str) -> dict[str, Any] | None:
    with RUN_STATE_LOCK:
        state = MAIL_AGENT_RUNS.get(run_id)
        if state:
            return state
        checkpoint = _load_run_checkpoint(run_id)
        if not checkpoint:
            return None
        if checkpoint.get("status") in {"queued", "running"}:
            checkpoint = dict(checkpoint)
            last_stage = checkpoint.get("stage") or "unknown"
            checkpoint["status"] = "failed"
            checkpoint["stage"] = "failed"
            checkpoint["error"] = (
                "run interrupted because the Executa process restarted; "
                f"last stage was {last_stage}"
            )
            checkpoint["updated_at"] = beijing_now()
            MAIL_AGENT_RUNS[run_id] = checkpoint
            _save_run_checkpoint(run_id)
        return checkpoint


def _compact_run_payload(value: Any, *, text_limit: int = 1200) -> Any:
    # 中文注释：轮询状态只需要展示摘要，完整邮件正文保留在持久化卡片和详情接口里。
    if isinstance(value, list):
        return [_compact_run_payload(item, text_limit=text_limit) for item in value]
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"body", "body_text", "body_html", "raw", "raw_message"} and isinstance(item, str):
                compact[key] = item[:text_limit]
                if len(item) > text_limit:
                    compact[f"{key}_truncated"] = True
                continue
            compact[key] = _compact_run_payload(item, text_limit=text_limit)
        return compact
    return value


def _compact_run_result(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, dict) and result.get("tool") == "run_mail_agent":
        action_plan = result.get("action_plan") if isinstance(result.get("action_plan"), dict) else {}
        # 中文注释：Brief 轮询完成后前端会再读持久化卡片，这里避免重复返回完整 cards/proposed_actions。
        return {
            "success": result.get("success", True),
            "tool": result.get("tool"),
            "action_plan": {
                "run_id": action_plan.get("run_id", ""),
                "strategy_mode": action_plan.get("strategy_mode", ""),
                "title": action_plan.get("title", ""),
                "summary": action_plan.get("summary", ""),
                "main_items": _compact_run_payload(action_plan.get("main_items", []), text_limit=800),
                "lower_priority_items": _compact_run_payload(action_plan.get("lower_priority_items", []), text_limit=800),
            },
            "cards_count": len(result.get("cards") or []),
            "meta": _compact_run_payload(result.get("meta") or {}, text_limit=800),
        }
    return _compact_run_payload(result, text_limit=1200)


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    protocol_version = str((params or {}).get("protocolVersion") or "1.1")
    v2 = protocol_version == PROTOCOL_VERSION_V2
    if not v2:
        sampling.disable(
            f"host did not negotiate v2 (got {protocol_version!r}); sampling/createMessage requires Executa protocol 2.0"
        )
    return {
        "protocolVersion": PROTOCOL_VERSION_V2 if v2 else "1.1",
        "serverInfo": {"name": TOOL_ID, "version": VERSION},
        "client_capabilities": {"sampling": {}} if v2 else {},
        "capabilities": {"storage": {"kv": _should_use_aps_storage(), "files": _should_use_aps_storage()}} if v2 else {},
    }


def read_credentials(context: dict[str, Any]) -> dict[str, str]:
    raw_credentials = context.get("credentials") if isinstance(context, dict) else {}
    credentials = raw_credentials if isinstance(raw_credentials, dict) else {}
    gmail_token = credentials.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GMAIL_ACCESS_TOKEN")
    google_token = credentials.get("GOOGLE_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN")
    return {
        "GMAIL_ACCESS_TOKEN": gmail_token or "",
        "GOOGLE_ACCESS_TOKEN": google_token or "",
    }


def apply_runtime_credentials(context: dict[str, Any]) -> None:
    raw_credentials = context.get("credentials") if isinstance(context, dict) else {}
    credentials = raw_credentials if isinstance(raw_credentials, dict) else {}
    for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_MODEL", "GMAIL_ACCESS_TOKEN", "GOOGLE_ACCESS_TOKEN"):
        value = credentials.get(name) or os.environ.get(name)
        if value:
            os.environ[name] = str(value)


def check_google_oauth(context: dict[str, Any]) -> dict[str, Any]:
    credentials = read_credentials(context)
    has_gmail = bool(credentials["GMAIL_ACCESS_TOKEN"])
    has_google = bool(credentials["GOOGLE_ACCESS_TOKEN"])
    return {
        "authorized": has_gmail or has_google,
        "credential_names": {
            "gmail": "present" if has_gmail else "missing",
            "google": "present" if has_google else "missing",
        },
        "next_step": "Google OAuth credential is available." if has_gmail or has_google else "Authorize Google/Gmail in Anna platform authorizations, then retry.",
        "checked_at": beijing_now(),
    }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def tool_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sanitize_mailbox_id(mailbox: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in mailbox.strip())
    return safe.strip("._") or "default"


def normalize_mailbox(mailbox: str) -> str:
    normalized = SUPPORTED_MAILBOXES.get(str(mailbox or "").strip().lower())
    if not normalized:
        raise ValueError(f"Unsupported mailbox: {mailbox}")
    return normalized


def token_dir() -> Path:
    override = os.environ.get("ANNA_INBOX_TOKEN_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return repo_root() / "executas" / "anna-inbox-tool" / ".secrets" / "gmail_tokens"


def cache_dir() -> Path:
    override = os.environ.get("ZHAOPY_MAIL_AGENT_DATA_DIR")
    base = Path(override).expanduser().resolve() if override else tool_root() / ".data"
    path = base / "gmail_cache" / "mailboxes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mailbox_cache_dir(mailbox: str) -> Path:
    path = cache_dir() / sanitize_mailbox_id(mailbox)
    path.mkdir(parents=True, exist_ok=True)
    return path


def index_path(mailbox: str) -> Path:
    return mailbox_cache_dir(mailbox) / "index.json"


def message_path(mailbox: str, message_id: str) -> Path:
    return mailbox_cache_dir(mailbox) / f"{sanitize_mailbox_id(message_id)}.json"


def read_cache(mailbox: str) -> dict[str, Any]:
    path = index_path(mailbox)
    if not path.exists():
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    if not isinstance(payload, dict):
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    return {"mailbox": mailbox, "messages": messages, "updated_at": payload.get("updated_at")}


def write_index(mailbox: str, messages: list[dict[str, Any]]) -> None:
    payload = {
        "mailbox": mailbox,
        "updated_at": beijing_now(),
        "message_count": len(messages),
        "messages": messages,
    }
    index_path(mailbox).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_message(mailbox: str, message: dict[str, Any]) -> None:
    message_id = str(message.get("id") or "")
    if not message_id:
        raise ValueError("Cannot cache Gmail message without id")
    message_path(mailbox, message_id).write_text(json.dumps(message, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_message(mailbox: str, message_id: str) -> dict[str, Any]:
    path = message_path(mailbox, message_id)
    if not path.exists():
        raise ValueError(f"Cached message not found: {message_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Cached message is invalid: {message_id}")
    return payload


def message_summary(message: dict[str, Any]) -> dict[str, Any]:
    body_text = str(message.get("body_text") or "")
    headers = message.get("raw_headers") if isinstance(message.get("raw_headers"), dict) else {}
    summary_keys = [
        "id",
        "thread_id",
        "mailbox",
        "history_id",
        "internal_date",
        "date",
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "message_id",
        "in_reply_to",
        "references",
        "label_ids",
        "snippet",
        "size_estimate",
        "mime_type",
        "attachments",
        "fetched_at",
    ]
    summary = {key: message.get(key) for key in summary_keys}
    summary["body_preview"] = body_text[:500]
    summary["body_length"] = len(body_text)
    summary["raw_header_count"] = len(headers)
    summary["json_file"] = str(message_path(str(message.get("mailbox") or ""), str(message.get("id") or "")))
    return summary


def load_local_token_record(mailbox: str) -> dict[str, Any]:
    candidates = [
        token_dir() / f"{sanitize_mailbox_id(mailbox)}.json",
        token_dir() / "default.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            record["_token_file"] = str(path)
            return record
    raise ValueError(f"Local Gmail token file not found for {mailbox}")


def get_access_token(mailbox: str) -> str:
    record = load_local_token_record(mailbox)
    if should_refresh_token(record):
        refresh_access_token(record)
    token = record.get("access_token")
    if not token:
        raise ValueError(f"Local Gmail access token is missing for {mailbox}")
    return str(token)


def should_refresh_token(record: dict[str, Any]) -> bool:
    if not record.get("refresh_token"):
        return False
    try:
        return float(record.get("expires_at") or 0) <= time.time() + 60
    except (TypeError, ValueError):
        return False


def refresh_access_token(record: dict[str, Any]) -> None:
    client_id = record.get("client_id")
    client_secret = record.get("client_secret")
    refresh_token = record.get("refresh_token")
    if not client_id or not client_secret or not refresh_token:
        raise ValueError("Gmail refresh token is missing client metadata")
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    request = urllib.request.Request(TOKEN_URI, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    record["access_token"] = payload["access_token"]
    if "expires_in" in payload:
        record["expires_at"] = int(time.time()) + int(payload["expires_in"])
    record["updated_at"] = beijing_now()
    token_file = record.get("_token_file")
    if token_file:
        clean_record = {key: value for key, value in record.items() if not key.startswith("_")}
        Path(str(token_file)).write_text(json.dumps(clean_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def gmail_request(mailbox: str, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    token = get_access_token(mailbox)
    url = GMAIL_API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = read_http_error(exc)
        raise ValueError(f"Gmail API request failed: {exc.code} {details}") from exc
    return json.loads(raw) if raw else {}


def read_http_error(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8")
        return json.loads(raw) if raw else {"status_code": exc.code}
    except Exception:
        return {"status_code": exc.code}


def header_map(message: dict[str, Any]) -> dict[str, str]:
    headers = ((message.get("payload") or {}).get("headers") or [])
    result: dict[str, str] = {}
    for header in headers:
        if isinstance(header, dict) and header.get("name"):
            result[str(header["name"]).lower()] = str(header.get("value") or "")
    return result


def decode_gmail_body(message: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = body.get("data")
        if data and mime_type in {"text/plain", "text/html"}:
            try:
                decoded = base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4)).decode("utf-8", errors="replace")
                parts.append(decoded)
            except Exception:
                return
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    walk(payload)
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    return text[:30000]


def extract_attachments(part: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        filename = str(node.get("filename") or "")
        body = node.get("body") if isinstance(node.get("body"), dict) else {}
        if filename:
            attachments.append({
                "filename": filename,
                "mimeType": node.get("mimeType"),
                "size": body.get("size"),
                "attachmentId": body.get("attachmentId"),
            })
        for child in node.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(part)
    return attachments


def normalize_message(mailbox: str, message: dict[str, Any]) -> dict[str, Any]:
    headers = header_map(message)
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "mailbox": mailbox,
        "history_id": message.get("historyId"),
        "internal_date": message.get("internalDate"),
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
        "label_ids": message.get("labelIds") or [],
        "snippet": message.get("snippet") or "",
        "size_estimate": message.get("sizeEstimate"),
        "mime_type": payload.get("mimeType"),
        "attachments": extract_attachments(payload),
        "body_text": decode_gmail_body(message),
        "raw_headers": headers,
        "fetched_at": beijing_now(),
    }


def read_primary_emails(mailbox_arg: str, limit_arg: Any) -> dict[str, Any]:
    mailbox = normalize_mailbox(mailbox_arg)
    limit = max(1, min(int(limit_arg or 5), 20))
    refs_payload = gmail_request(mailbox, "/users/me/messages", {"q": "category:primary", "maxResults": limit})
    refs = refs_payload.get("messages") or []
    existing = read_cache(mailbox)
    by_id = {str(item.get("id")): item for item in existing["messages"] if item.get("id")}
    fetched_summaries: list[dict[str, Any]] = []
    added_count = 0
    updated_count = 0
    for ref in refs:
        message_id = ref.get("id") if isinstance(ref, dict) else None
        if not message_id:
            continue
        full = gmail_request(mailbox, f"/users/me/messages/{urllib.parse.quote(str(message_id), safe='')}", {"format": "full"})
        normalized = normalize_message(mailbox, full)
        write_message(mailbox, normalized)
        summary = message_summary(normalized)
        if str(message_id) in by_id:
            updated_count += 1
        else:
            added_count += 1
        by_id[str(message_id)] = summary
        fetched_summaries.append(summary)

    merged = sorted(by_id.values(), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    write_index(mailbox, merged)
    return {
        "mailbox": mailbox,
        "requested": limit,
        "fetched_count": len(fetched_summaries),
        "added_count": added_count,
        "updated_count": updated_count,
        "cached_count": len(merged),
        "cache_dir": str(mailbox_cache_dir(mailbox)),
        "index_file": str(index_path(mailbox)),
        "messages": fetched_summaries,
        "updated_at": beijing_now(),
    }


def list_cached_emails(mailbox_arg: str) -> dict[str, Any]:
    mailbox = normalize_mailbox(mailbox_arg)
    cached = read_cache(mailbox)
    messages = sorted(cached["messages"], key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    return {
        "mailbox": mailbox,
        "cache_dir": str(mailbox_cache_dir(mailbox)),
        "index_file": str(index_path(mailbox)),
        "updated_at": cached.get("updated_at"),
        "count": len(messages),
        "messages": messages,
    }


def get_cached_email(mailbox_arg: str, message_id: str) -> dict[str, Any]:
    mailbox = normalize_mailbox(mailbox_arg)
    message = read_message(mailbox, str(message_id or ""))
    return {
        "mailbox": mailbox,
        "json_file": str(message_path(mailbox, str(message_id or ""))),
        "message": message,
    }


async def run_dashscope_llm(message: str, invoke_id: str) -> dict[str, Any]:
    """调用 DashScope 文本模型，用于保留旧调试入口。"""
    clean_message = str(message or "").strip()
    if not clean_message:
        return {"text": "", "note": "empty message"}

    from mail_agent.llm import call_dashscope_text

    result = await asyncio.to_thread(
        call_dashscope_text,
        system_prompt="You are a concise assistant inside a minimal mail-agent wiring test.",
        user_message=clean_message,
        max_tokens=512,
        timeout=90.0,
    )
    return {
        "text": result.get("text", ""),
        "model": result.get("model"),
        "usage": result.get("usage"),
        "provider": result.get("provider"),
        "answered_at": beijing_now(),
    }


async def run_anna_sampling_text(arguments: dict[str, Any], invoke_id: str, *, tool_name: str, default_max_tokens: int) -> dict[str, Any]:
    """调用 Anna sampling/createMessage，并返回文本结果。"""
    from mail_agent.llm import extract_sampling_text

    prompt = str(arguments.get("message") or arguments.get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "message is required"}

    max_tokens = int(arguments.get("max_tokens") or default_max_tokens)
    temperature = float(arguments.get("temperature") if arguments.get("temperature") is not None else 0.2)
    metadata = {
        "executa_invoke_id": invoke_id or f"local_{uuid.uuid4().hex}",
        "tool": tool_name,
    }
    model_preference = str(arguments.get("model_preference") or "").strip()
    model_preferences = {"hints": [{"name": model_preference}]} if model_preference else None
    started = time.time()
    started_at = beijing_now()

    try:
        result = await sampling.create_message(
            messages=[
                {
                    "role": "user",
                    "content": {"type": "text", "text": prompt},
                }
            ],
            max_tokens=max_tokens,
            system_prompt=str(arguments.get("system_prompt") or "") or None,
            temperature=temperature,
            include_context="none",
            model_preferences=model_preferences,
            metadata=metadata,
            timeout=90.0,
        )
    except SamplingError as exc:
        return {
            "success": False,
            "provider": "anna-sampling",
            "error": exc.message,
            "error_code": exc.code,
            "error_data": exc.data,
            "started_at": started_at,
            "finished_at": beijing_now(),
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    text = extract_sampling_text(result)
    return {
        "success": True,
        "provider": "anna-sampling",
        "text": text,
        "model": result.get("model") if isinstance(result, dict) else None,
        "usage": result.get("usage") if isinstance(result, dict) else None,
        "stop_reason": result.get("stopReason") if isinstance(result, dict) else None,
        "started_at": started_at,
        "finished_at": beijing_now(),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


async def run_anna_sampling_smoke(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """只验证 Anna sampling/createMessage 的最小链路。"""
    return await run_anna_sampling_text(arguments, invoke_id, tool_name="test_anna_sampling", default_max_tokens=64)


async def run_aps_storage_smoke(arguments: dict[str, Any]) -> dict[str, Any]:
    """只验证 APS KV 的最小读写链路，不触碰业务邮箱数据。"""
    from mail_agent.storage_client import get_storage, scope as default_scope

    storage = get_storage()
    suffix = str(arguments.get("key_suffix") or uuid.uuid4().hex[:8]).strip()
    key = f"debug/aps_smoke/{suffix}"
    value = {
        "value": str(arguments.get("value") or "hello aps"),
        "ts": beijing_now(),
    }
    storage_scope = default_scope()

    set_result = await storage.set(key, value, scope=storage_scope)
    get_result = await storage.get(key, scope=storage_scope)
    list_result = await storage.list(prefix="debug/aps_smoke/", limit=20, scope=storage_scope)
    list_all_result = await storage.list(limit=20, scope=storage_scope)
    delete_result = await storage.delete(key, scope=storage_scope)
    after_delete = await storage.get(key, scope=storage_scope)

    return {
        "success": bool(get_result.get("exists")) and get_result.get("value") == value,
        "backend": "aps" if _should_use_aps_storage() else "local-json",
        "scope": storage_scope,
        "key": key,
        "set": set_result,
        "get": get_result,
        "list": list_result,
        "list_all": list_all_result,
        "list_count": len(list_result.get("items") or []),
        "delete": delete_result,
        "exists_after_delete": bool(after_delete.get("exists")),
    }


def serialize_value(obj: Any) -> Any:
    from dataclasses import asdict

    if hasattr(obj, "__dataclass_fields__"):
        return {key: serialize_value(value) for key, value in asdict(obj).items()}
    if isinstance(obj, list):
        return [serialize_value(item) for item in obj]
    if isinstance(obj, dict):
        return {key: serialize_value(value) for key, value in obj.items()}
    return obj


async def run_mail_agent_pipeline(
    *,
    user_request: str,
    mailbox: str,
    mode: str,
    max_messages: int,
    primary_count: int = 20,
    ai_provider: str = "anna-llm",
    invoke_id: str,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Run the full mail agent pipeline."""
    from dataclasses import asdict
    from mail_agent.llm import dashscope_available
    from mail_agent.pipeline import run_mail_task
    from mail_agent.types import MailTaskInput

    provider = str(ai_provider or "anna-llm").strip()
    invoke_id = invoke_id or f"local_{uuid.uuid4().hex}"
    log(f"mail-agent pipeline start: mailbox={mailbox} mode={mode} provider={provider} request={user_request[:80]} primary_count={primary_count}")
    if provider == "dashscope" and not dashscope_available():
        raise RuntimeError("DASHSCOPE_API_KEY is not set; DashScope provider cannot run.")
    if progress_callback is None:
        progress_callback = lambda stage, progress: log(f"mail-agent progress: {stage} {json.dumps(progress, ensure_ascii=False)}")

    input_ = MailTaskInput(
        user_request=str(user_request or "").strip(),
        mailbox_id=str(mailbox or "").strip(),
        user_email=str(mailbox or "").strip(),
        mode=mode if mode else "auto",
        max_messages=max_messages,
        dry_run=True,
    )

    if not input_.user_request or not input_.mailbox_id:
        return {"success": False, "error": "user_request and mailbox are required"}

    sampling_create_message = None
    if provider == "anna-llm":
        async def sampling_create_message_with_invoke(**kwargs: Any) -> dict[str, Any]:
            metadata = {str(key): str(value) for key, value in (kwargs.get("metadata") or {}).items()}
            metadata["executa_invoke_id"] = invoke_id
            kwargs["metadata"] = metadata
            log(f"anna sampling start: max_tokens={kwargs.get('max_tokens')} metadata={metadata}")
            started = time.time()
            result = await sampling.create_message(**kwargs)
            log(f"anna sampling done: elapsed_ms={int((time.time() - started) * 1000)} model={result.get('model')} shape={_sampling_result_shape(result)}")
            return result

        sampling_create_message = sampling_create_message_with_invoke

    action_plan = await run_mail_task(
        input_,
        sampling_create_message=sampling_create_message,
        primary_count=primary_count,
        progress_callback=progress_callback,
    )

    # Convert to serializable dict
    def _serialize(obj: Any) -> Any:
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _serialize(v) for k, v in asdict(obj).items()}
        if isinstance(obj, list):
            return [_serialize(item) for item in obj]
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        return obj

    log(f"mail-agent pipeline done: strategy={action_plan.strategy_mode} main={len(action_plan.main_items)} lower={len(action_plan.lower_priority_items)}")

    # Read active cards from storage for V2 frontend
    active_cards: list[dict[str, Any]] = []
    try:
        from mail_agent.storage_client import is_ready
        if is_ready():
            from mail_agent.storage_ops import get_active_cards
            from mail_agent.card_service import cards_to_frontend
            stored = await get_active_cards(input_.mailbox_id)
            active_cards = cards_to_frontend(stored)
    except Exception:
        pass

    return {
        "success": True,
        "tool": "run_mail_agent",
        "action_plan": _serialize(action_plan),
        "cards": active_cards,
        "meta": {
            "run_id": action_plan.run_id,
            "strategy_mode": action_plan.strategy_mode,
            "main_items": len(action_plan.main_items),
            "lower_priority_items": len(action_plan.lower_priority_items),
            "proposed_actions": len(action_plan.proposed_actions),
            "approval_memo": action_plan.approval_memo,
            "llm_provider": provider,
        },
    }


async def run_mail_agent_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
    _save_run_checkpoint(run_id)

    def _update_progress(stage: str, progress: dict[str, Any]) -> None:
        partial_update = progress.pop("partial", None)
        if isinstance(partial_update, dict):
            MAIL_AGENT_RUNS[run_id].setdefault("partial", {}).update(partial_update)
        MAIL_AGENT_RUNS[run_id]["stage"] = stage
        MAIL_AGENT_RUNS[run_id]["progress"] = progress
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

    try:
        primary_count = arguments.get("primary_count", 20)
        max_messages = arguments.get("max_messages", primary_count)
        result = await run_mail_agent_pipeline(
            user_request=arguments.get("user_request", ""),
            mailbox=arguments.get("mailbox", ""),
            mode=arguments.get("mode", "auto"),
            max_messages=max_messages,
            primary_count=primary_count,
            ai_provider=arguments.get("ai_provider", "anna-llm"),
            invoke_id=invoke_id,
            progress_callback=_update_progress,
        )
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
            "result": result,
        })
        _save_run_checkpoint(run_id)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)


async def run_custom_scan_background(run_id: str, plan: Any, arguments: dict[str, Any], invoke_id: str) -> None:
    """Background execution of a custom scan (plan already generated or loaded)."""
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
    _save_run_checkpoint(run_id)

    def _update_progress(stage: str, progress: dict[str, Any]) -> None:
        partial_update = progress.pop("partial", None)
        if isinstance(partial_update, dict):
            MAIL_AGENT_RUNS[run_id].setdefault("partial", {}).update(partial_update)
        MAIL_AGENT_RUNS[run_id]["stage"] = stage
        MAIL_AGENT_RUNS[run_id]["progress"] = progress
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

    try:
        from mail_agent.pipeline import run_custom_scan
        result = await run_custom_scan(
            plan=plan,
            mailbox=arguments.get("mailbox", ""),
            sampling_create_message=_build_sampling_for_run(arguments, invoke_id),
            progress_callback=_update_progress,
        )
        # Update plan result metadata
        from mail_agent.storage_ops import update_plan_result
        section_count = len(result.get("sections", []))
        item_count = sum(len(s.get("items", [])) for s in result.get("sections", []))
        await update_plan_result(plan.plan_id, f"{section_count} sections, {item_count} items")

        # Store result for frontend
        planner_llm = getattr(plan, "llm_meta", {})
        executor_llm = result.get("llm_meta", {}) if isinstance(result, dict) else {}
        result_data: dict[str, Any] = {
            "success": True,
            "run_id": run_id,
            "plan_id": plan.plan_id,
            "plan_title": plan.title,
            "plan_description": plan.description,
            "plan_gmail_queries": plan.gmail_queries,
            "plan_read_depth": plan.read_depth or "message_detail",
            "title": result.get("title", ""),
            "summary": result.get("summary", ""),
            "sections": result.get("sections", []),
            "ai_provider": str(arguments.get("ai_provider", "anna-llm") or "anna-llm"),
            "planner_llm": planner_llm,
            "executor_llm": executor_llm,
            "trace": {
                "plan": {
                    "plan_id": plan.plan_id,
                    "title": plan.title,
                    "description": plan.description,
                    "gmail_queries": plan.gmail_queries,
                    "read_depth": plan.read_depth or "message_detail",
                },
                "sources": MAIL_AGENT_RUNS[run_id].get("partial", {}).get("sources", []),
                "progress": MAIL_AGENT_RUNS[run_id].get("progress", {}),
                "started_at": MAIL_AGENT_RUNS[run_id].get("started_at"),
                "updated_at": beijing_now(),
            },
        }
        MAIL_AGENT_RUNS[run_id].update({
            "status": "done",
            "stage": "done",
            "updated_at": beijing_now(),
            "result": result_data,
        })
        _save_run_checkpoint(run_id)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)


def _build_sampling_for_run(arguments: dict[str, Any], invoke_id: str) -> Any:
    """Build sampling_create_message for a run based on ai_provider arg."""
    provider = str(arguments.get("ai_provider", "anna-llm")).strip()
    if provider == "anna-llm":
        async def _sampling(**kwargs: Any) -> dict[str, Any]:
            metadata = {str(key): str(value) for key, value in (kwargs.get("metadata") or {}).items()}
            metadata["executa_invoke_id"] = invoke_id
            kwargs["metadata"] = metadata
            tool_name = metadata.get("tool", "unknown")
            started = time.time()
            log(f"anna ask sampling start: tool={tool_name} max_tokens={kwargs.get('max_tokens')} metadata={metadata}")
            result = await sampling.create_message(**kwargs)
            log(f"anna ask sampling done: tool={tool_name} elapsed_ms={int((time.time() - started) * 1000)} model={result.get('model')} shape={_sampling_result_shape(result)}")
            return result
        return _sampling
    return None


def start_mail_agent_run(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    run_id = f"bg_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "queued",
        "progress": {},
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {},
    }
    _save_run_checkpoint(run_id)
    future = asyncio.run_coroutine_threadsafe(run_mail_agent_background(run_id, arguments, invoke_id), loop)
    if arguments.get("wait") is True:
        wait_timeout = int(arguments.get("wait_timeout_seconds", 300))
        future.result(timeout=wait_timeout)
        return {
            "success": MAIL_AGENT_RUNS[run_id].get("status") == "done",
            "run_id": run_id,
            "status": MAIL_AGENT_RUNS[run_id]["status"],
            "stage": MAIL_AGENT_RUNS[run_id]["stage"],
            "progress": MAIL_AGENT_RUNS[run_id]["progress"],
            "partial": MAIL_AGENT_RUNS[run_id].get("partial", {}),
            "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
            "updated_at": MAIL_AGENT_RUNS[run_id]["updated_at"],
            "result": _compact_run_result(MAIL_AGENT_RUNS[run_id].get("result")),
            "error": MAIL_AGENT_RUNS[run_id].get("error", ""),
        }
    return {
        "success": True,
        "run_id": run_id,
        "status": "queued",
        "stage": MAIL_AGENT_RUNS[run_id]["stage"],
        "progress": MAIL_AGENT_RUNS[run_id]["progress"],
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
    }


def start_custom_scan(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Start a custom scan: generate plan (LLM) then execute in background."""
    run_id = f"bg_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "planning",
        "progress": {},
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {},
    }
    _save_run_checkpoint(run_id)
    future = asyncio.run_coroutine_threadsafe(
        _start_custom_scan_async(run_id, arguments, invoke_id),
        loop,
    )
    if arguments.get("wait") is True:
        wait_timeout = int(arguments.get("wait_timeout_seconds", 180))
        future.result(timeout=wait_timeout)
        return {
            "success": MAIL_AGENT_RUNS[run_id].get("status") == "done",
            "run_id": run_id,
            "status": MAIL_AGENT_RUNS[run_id]["status"],
            "stage": MAIL_AGENT_RUNS[run_id]["stage"],
            "progress": MAIL_AGENT_RUNS[run_id]["progress"],
            "partial": MAIL_AGENT_RUNS[run_id].get("partial", {}),
            "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
            "updated_at": MAIL_AGENT_RUNS[run_id]["updated_at"],
            "result": _compact_run_result(MAIL_AGENT_RUNS[run_id].get("result")),
            "error": MAIL_AGENT_RUNS[run_id].get("error", ""),
        }
    return {
        "success": True,
        "run_id": run_id,
        "status": "queued",
        "stage": "planning",
        "progress": {},
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
    }


async def _start_custom_scan_async(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """Async portion: generate plan (LLM), save it, then execute."""
    from mail_agent.planner import generate_custom_plan
    from mail_agent.storage_ops import save_custom_plan

    try:
        MAIL_AGENT_RUNS[run_id]["status"] = "running"
        MAIL_AGENT_RUNS[run_id]["stage"] = "planning"
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        sampling = _build_sampling_for_run(arguments, invoke_id)
        mailbox = str(arguments.get("mailbox", "")).strip()
        user_request = str(arguments.get("user_request", "")).strip()

        # Step 1: Generate plan
        plan = await generate_custom_plan(user_request, mailbox, sampling)
        MAIL_AGENT_RUNS[run_id]["stage"] = "planning_done"
        MAIL_AGENT_RUNS[run_id].setdefault("partial", {})["plan"] = {
            "plan_id": plan.plan_id,
            "title": plan.title,
            "description": plan.description,
            "gmail_queries": plan.gmail_queries,
            "read_depth": plan.read_depth or "message_detail",
        }
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        # Step 2: Save plan
        await save_custom_plan(plan)

        # Step 3: Execute
        await run_custom_scan_background(run_id, plan, arguments, invoke_id)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)


def re_run_custom_scan(arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Re-run a previously saved custom scan plan (skip LLM planning)."""
    plan_id = str(arguments.get("plan_id", "")).strip()
    if not plan_id:
        return {"success": False, "error": "plan_id is required"}

    run_id = f"bg_{uuid.uuid4().hex[:12]}"
    MAIL_AGENT_RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "stage": "planning_done",
        "progress": {},
        "started_at": beijing_now(),
        "updated_at": beijing_now(),
        "result": None,
        "error": "",
        "partial": {},
    }
    _save_run_checkpoint(run_id)
    asyncio.run_coroutine_threadsafe(
        _re_run_custom_scan_async(run_id, plan_id, arguments, invoke_id),
        loop,
    )
    return {
        "success": True,
        "run_id": run_id,
        "status": "queued",
        "stage": "planning_done",
        "progress": {},
        "started_at": MAIL_AGENT_RUNS[run_id]["started_at"],
    }


async def _re_run_custom_scan_async(run_id: str, plan_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    """Async portion: load plan and execute."""
    from mail_agent.storage_ops import get_custom_plan
    from mail_agent.types import CustomScanPlan

    try:
        MAIL_AGENT_RUNS[run_id]["status"] = "running"
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        plan_dict = await get_custom_plan(plan_id)
        if not plan_dict:
            raise ValueError(f"Custom plan not found: {plan_id}")

        plan = CustomScanPlan(
            plan_id=plan_dict.get("plan_id", plan_id),
            user_request=plan_dict.get("user_request", ""),
            title=plan_dict.get("title", ""),
            description=plan_dict.get("description", ""),
            gmail_queries=plan_dict.get("gmail_queries", []),
            scan_budget=plan_dict.get("scan_budget", {}),
            read_depth=plan_dict.get("read_depth", "message_detail"),
            task_prompt=plan_dict.get("task_prompt", ""),
            created_at=plan_dict.get("created_at", ""),
            last_used_at=plan_dict.get("last_used_at", ""),
            use_count=plan_dict.get("use_count", 0),
            last_result_summary=plan_dict.get("last_result_summary", ""),
        )
        MAIL_AGENT_RUNS[run_id]["stage"] = "planning_done"
        MAIL_AGENT_RUNS[run_id].setdefault("partial", {})["plan"] = {
            "plan_id": plan.plan_id,
            "title": plan.title,
            "description": plan.description,
            "gmail_queries": plan.gmail_queries,
            "read_depth": plan.read_depth or "message_detail",
        }
        MAIL_AGENT_RUNS[run_id]["updated_at"] = beijing_now()
        _save_run_checkpoint(run_id)

        await run_custom_scan_background(run_id, plan, arguments, invoke_id)
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update({
            "status": "failed",
            "stage": "failed",
            "updated_at": beijing_now(),
            "error": str(exc),
        })
        _save_run_checkpoint(run_id)


def _run_storage_query(coro: Any, timeout: float = 60.0) -> Any:
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _sync_get_custom_plans() -> dict[str, Any]:
    """同步入口通过统一 storage_ops 读取，兼容本地 JSON 和 APS。"""
    from mail_agent.storage_ops import list_custom_plans

    return {"plans": _run_storage_query(list_custom_plans())}


def _sync_get_custom_plan_detail(arguments: dict[str, Any]) -> dict[str, Any]:
    """同步入口通过统一 storage_ops 读取单个 custom plan。"""
    plan_id = str(arguments.get("plan_id", "")).strip()
    if not plan_id:
        return {"error": "plan_id is required"}
    from mail_agent.storage_ops import get_custom_plan

    plan = _run_storage_query(get_custom_plan(plan_id))
    if plan:
        return {"plan": plan}
    return {"error": f"Custom plan not found: {plan_id}"}


def _sync_get_active_cards(arguments: dict[str, Any]) -> dict[str, Any]:
    """同步入口通过统一 storage_ops 读取 active cards 和 scan state。"""
    mailbox = str(arguments.get("mailbox", "")).strip()
    if not mailbox:
        return {"error": "mailbox is required"}

    from mail_agent.card_service import cards_to_frontend
    from mail_agent.storage_ops import get_active_cards, get_scan_state

    active = _run_storage_query(get_active_cards(mailbox))
    state = _run_storage_query(get_scan_state(mailbox))
    scan_state = {
        "last_scan_ts": getattr(state, "last_scan_ts", ""),
        "last_message_internal_date": getattr(state, "last_message_internal_date", ""),
        "total_scans": getattr(state, "total_scans", 0),
        "total_processed": getattr(state, "total_processed", 0),
    }

    return {
        "cards": cards_to_frontend(active),
        "count": len(active.cards),
        "scan_state": scan_state,
    }

def _sync_get_run_history() -> dict[str, Any]:
    from mail_agent.storage_ops import get_run_history

    history = _run_storage_query(get_run_history(limit=20))
    return {"history": [serialize_value(entry) for entry in history]}


def get_mail_agent_run(run_id_arg: str) -> dict[str, Any]:
    run_id = str(run_id_arg or "")
    state = _get_run_state(run_id)
    if not state:
        return {"success": False, "error": "run not found", "run_id": run_id}
    return {
        "success": True,
        "run_id": run_id,
        "status": state.get("status"),
        "stage": state.get("stage") or "",
        "progress": state.get("progress") or {},
        "partial": state.get("partial") or {},
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "error": state.get("error") or "",
        "result": _compact_run_result(state.get("result")),
    }


async def _handle_summarize_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()
    try:
        from mail_agent.storage_ops import get_active_cards as storage_get_cards, set_active_cards
        from mail_agent.handle_service import summarize_thread
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            raise ValueError(f"Card {card_id} not found")
        _sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await summarize_thread(card, mailbox, sampling_create_message=_sampling)
        summary = result.get("summary") if isinstance(result, dict) else {}
        if isinstance(summary, dict):
            card.thread_summary = json.dumps(summary, ensure_ascii=False)
            await set_active_cards(mailbox, cards)
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    _save_run_checkpoint(run_id)


async def _handle_generate_draft_background(run_id: str, arguments: dict[str, Any], invoke_id: str) -> None:
    MAIL_AGENT_RUNS[run_id]["status"] = "running"
    _save_run_checkpoint(run_id)

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()
    reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
    current_draft = str(arguments.get("current_draft", "")).strip()
    revision_input = str(arguments.get("revision_input", "")).strip()
    try:
        from mail_agent.storage_ops import get_active_cards as storage_get_cards, set_active_cards
        from mail_agent.handle_service import generate_draft_reply
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            raise ValueError(f"Card {card_id} not found")
        _sampling = _build_sampling_for_run(arguments, invoke_id)
        result = await generate_draft_reply(
            card, mailbox, reply_mode, sampling_create_message=_sampling,
            current_draft=current_draft, revision_input=revision_input,
        )
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            await set_active_cards(mailbox, cards)
        MAIL_AGENT_RUNS[run_id].update(status="done", result=result, updated_at=beijing_now())
    except Exception as exc:
        MAIL_AGENT_RUNS[run_id].update(status="failed", error=str(exc), updated_at=beijing_now())
    _save_run_checkpoint(run_id)


async def _handle_v2_tool(tool: str, arguments: dict[str, Any], invoke_id: str) -> dict[str, Any]:
    """Handle V2 interaction tools (async, runs on the event loop)."""
    from mail_agent.storage_ops import (
        get_active_cards as storage_get_cards,
        get_scan_plan,
        get_scan_state,
        set_scan_plan,
        update_card_status,
        add_snooze_sender,
        add_snooze_thread,
        append_learning,
        get_run_history,
    )
    from mail_agent.card_service import cards_to_frontend
    from mail_agent.handle_service import (
        _fetch_thread_context_sync,
        summarize_thread,
        generate_draft_reply,
        reply_now,
    )
    from mail_agent.storage_types import PersistentCard

    mailbox = str(arguments.get("mailbox", "")).strip()
    card_id = str(arguments.get("card_id", "")).strip()

    if tool == "get_active_cards":
        if not mailbox:
            return {"error": "mailbox is required"}
        cards = await storage_get_cards(mailbox)
        scan_state = await get_scan_state(mailbox)
        return {
            "cards": cards_to_frontend(cards),
            "count": len(cards.cards),
            "scan_state": {
                "last_scan_ts": scan_state.last_scan_ts,
                "last_message_internal_date": scan_state.last_message_internal_date,
                "total_scans": scan_state.total_scans,
                "total_processed": scan_state.total_processed,
            },
        }

    if tool == "get_scan_plan":
        if not mailbox:
            return {"error": "mailbox is required"}
        plan = await get_scan_plan(mailbox)
        return {
            "mailbox": plan.mailbox,
            "schedule": plan.schedule,
            "time_range": plan.time_range,
            "max_messages": plan.max_messages,
            "priorities": plan.priorities,
            "include_newsletters": plan.include_newsletters,
            "include_promotions": plan.include_promotions,
            "include_archived": plan.include_archived,
            "batch_behavior": plan.batch_behavior,
            "active": plan.active,
            "updated_at": plan.updated_at,
        }

    if tool == "set_scan_plan":
        if not mailbox:
            return {"error": "mailbox is required"}
        plan = await get_scan_plan(mailbox)
        for field in ("schedule", "time_range", "include_newsletters", "include_promotions",
                       "batch_behavior", "active"):
            val = arguments.get(field)
            if val is not None:
                setattr(plan, field, str(val) if not isinstance(val, bool) else bool(val))
        if arguments.get("max_messages") is not None:
            plan.max_messages = max(30, min(int(arguments["max_messages"]), 300))
        if arguments.get("priorities") is not None and isinstance(arguments["priorities"], list):
            plan.priorities = [str(p) for p in arguments["priorities"]]
        await set_scan_plan(mailbox, plan)
        return {"ok": True, "mailbox": mailbox, "updated_at": plan.updated_at}

    if tool == "get_card_detail":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        thread_ctx = _fetch_thread_context_sync(mailbox, card)
        return {
            "card": _serialize_card_for_frontend(card),
            "thread_context": thread_ctx,
        }

    # Build sampling for Anna LLM path (same logic as _build_sampling_for_run)
    _sampling = _build_sampling_for_run(arguments, invoke_id)

    # ── Background (async) tools — return run_id immediately ──
    if tool == "start_summarize_thread":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "summarize_thread", "progress": {}, "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_summarize_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "start_generate_draft":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        run_id = f"bg_{uuid.uuid4().hex[:12]}"
        MAIL_AGENT_RUNS[run_id] = {"run_id": run_id, "status": "queued", "stage": "generate_draft_reply", "progress": {}, "started_at": beijing_now(), "updated_at": beijing_now(), "result": None, "error": "", "partial": {}}
        _save_run_checkpoint(run_id)
        asyncio.ensure_future(_handle_generate_draft_background(run_id, arguments, invoke_id))
        return {"success": True, "run_id": run_id, "status": "queued"}

    if tool == "summarize_thread":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        result = await summarize_thread(card, mailbox, sampling_create_message=_sampling)
        summary = result.get("summary") if isinstance(result, dict) else {}
        if isinstance(summary, dict):
            import json as _json
            card.thread_summary = _json.dumps(summary, ensure_ascii=False)
            from mail_agent.storage_ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "generate_draft_reply":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        result = await generate_draft_reply(card, mailbox, reply_mode, sampling_create_message=_sampling)
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            from mail_agent.storage_ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "revise_draft":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        current_draft = str(arguments.get("current_draft", "")).strip()
        revision_input = str(arguments.get("revision_input", "")).strip()
        if not current_draft and not revision_input:
            return {"error": "current_draft or revision_input is required"}
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        result = await generate_draft_reply(card, mailbox, "reply_to_sender", sampling_create_message=_sampling, current_draft=current_draft, revision_input=revision_input)
        draft_body = (result.get("draft") or {}).get("body", "") if isinstance(result, dict) else ""
        if draft_body:
            card.draft_reply = draft_body
            from mail_agent.storage_ops import set_active_cards
            await set_active_cards(mailbox, cards)
        return result

    if tool == "record_card_decision":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        decision = str(arguments.get("decision", "")).strip()
        if decision not in ("no_action_needed", "handled_manually", "dismissed"):
            return {"error": f"Invalid decision: {decision}"}
        updated = await update_card_status(mailbox, card_id, "resolved" if decision != "dismissed" else "dismissed", decision)
        # Record weak signal: no_action_needed → LearningRecord
        if decision == "no_action_needed":
            cards = await storage_get_cards(mailbox)
            card = next((c for c in cards.cards if c.card_id == card_id), None)
            if card:
                await append_learning(card.original.from_addr, "no_action_needed")
        return {"ok": updated is not None, "card_id": card_id, "decision": decision}

    if tool == "record_snooze":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        snooze_option = str(arguments.get("snooze_option", "")).strip()
        if snooze_option == "dont_prioritize":
            cards = await storage_get_cards(mailbox)
            card = next((c for c in cards.cards if c.card_id == card_id), None)
            if card:
                await add_snooze_sender(card.original.from_addr)
                await add_snooze_thread(card.original.thread)
                await update_card_status(mailbox, card_id, "resolved", "dont_prioritize")
            return {"ok": True, "card_id": card_id, "option": snooze_option}
        else:
            from datetime import datetime, timedelta
            from mail_agent.storage_types import BEIJING_TZ
            now = datetime.now(BEIJING_TZ)
            if snooze_option == "tomorrow":
                until = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            elif snooze_option == "next_week":
                days_until_monday = (7 - now.weekday()) % 7 or 7
                until = (now + timedelta(days=days_until_monday)).replace(hour=9, minute=0, second=0, microsecond=0)
            else:
                return {"error": f"Unknown snooze option: {snooze_option}"}
            cards = await storage_get_cards(mailbox)
            for c in cards.cards:
                if c.card_id == card_id:
                    c.status = "snoozed"
                    c.snooze_until = until.isoformat()
                    from mail_agent.storage_ops import set_active_cards
                    await set_active_cards(mailbox, cards)
                    break
            return {"ok": True, "card_id": card_id, "option": snooze_option, "snooze_until": until.isoformat()}

    if tool == "restore_card":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        await update_card_status(mailbox, card_id, "pending")
        return {"ok": True, "card_id": card_id}

    if tool == "delete_custom_plan":
        plan_id = str(arguments.get("plan_id", "")).strip()
        if not plan_id:
            return {"error": "plan_id is required"}
        from mail_agent.storage_ops import delete_custom_plan
        await delete_custom_plan(plan_id)
        return {"ok": True, "plan_id": plan_id}

    if tool == "reply_now":
        if not mailbox or not card_id:
            return {"error": "mailbox and card_id are required"}
        draft_body = str(arguments.get("draft_body", "")).strip()
        if not draft_body:
            return {"error": "draft_body is required"}
        reply_mode = str(arguments.get("reply_mode", "reply_to_sender")).strip() or "reply_to_sender"
        dry_run = arguments.get("dry_run", True)
        if not isinstance(dry_run, bool):
            dry_run = True
        cards = await storage_get_cards(mailbox)
        card = next((c for c in cards.cards if c.card_id == card_id), None)
        if not card:
            return {"error": f"Card {card_id} not found"}
        result = await reply_now(card, mailbox, draft_body, reply_mode, dry_run=dry_run)
        if result.get("ok") and not dry_run:
            await update_card_status(mailbox, card_id, "resolved", "replied")
        return result

    if tool == "record_learning":
        pattern = str(arguments.get("pattern", "")).strip()
        action = str(arguments.get("action", "")).strip()
        if not pattern or not action:
            return {"error": "pattern and action are required"}
        await append_learning(pattern, action)
        return {"ok": True, "pattern": pattern, "action": action}

    if tool == "get_run_history":
        history = await get_run_history(limit=20)
        return {"history": [{
            "run_id": h.run_id,
            "mailbox": h.mailbox,
            "ts": h.ts,
            "request": h.request,
            "mode": h.mode,
            "strategy": h.strategy,
            "plan_id": getattr(h, 'plan_id', ''),
            "result": h.result,
            "summary": h.summary,
        } for h in history]}

    return {"error": f"Unknown V2 tool: {tool}"}


def _serialize_card_for_frontend(card: Any) -> dict[str, Any]:
    return {
        "id": card.card_id,
        "title": card.title,
        "summary": card.summary,
        "recommendation": card.recommendation,
        "label": card.label,
        "details": {
            "needs": card.details.needs,
            "latestActivity": card.details.latest_activity,
            "reviewed": card.details.reviewed,
            "mailbox": card.details.mailbox,
        },
        "original": {
            "source": card.original.source,
            "thread": card.original.thread,
            "from": card.original.from_addr,
            "to": card.original.to_addr,
            "time": card.original.time,
            "status": card.original.status,
            "body": card.original.body,
        },
        "actions": [
            {"id": a.id, "label": a.label, "primary": a.primary, "statusTitle": a.status_title, "status": a.status}
            for a in (card.actions or [])
        ],
        "status": card.status,
    }


def handle_invoke(params: dict[str, Any]) -> dict[str, Any]:
    tool = params.get("tool")
    arguments = params.get("arguments") or {}
    context = params.get("context") or {}
    invoke_id = str(params.get("invoke_id") or "")
    apply_runtime_credentials(context)
    _apply_storage_provider(arguments)

    if tool == "check_google_oauth":
        return {"success": True, "tool": tool, "data": check_google_oauth(context)}
    if tool in ("ask_anna_llm", "ask_dashscope_llm"):
        future = asyncio.run_coroutine_threadsafe(run_dashscope_llm(arguments.get("message", ""), invoke_id), loop)
        try:
            return {"success": True, "tool": tool, "data": future.result(timeout=180.0)}
        except SamplingError as exc:
            raise RuntimeError(json.dumps({"code": exc.code, "message": exc.message, "data": exc.data}, ensure_ascii=False)) from exc
    if tool == "ask_anna_sampling":
        future = asyncio.run_coroutine_threadsafe(
            run_anna_sampling_text(arguments, invoke_id, tool_name="ask_anna_sampling", default_max_tokens=512),
            loop,
        )
        try:
            return {"success": True, "tool": tool, "data": future.result(timeout=180.0)}
        except SamplingError as exc:
            raise RuntimeError(json.dumps({"code": exc.code, "message": exc.message, "data": exc.data}, ensure_ascii=False)) from exc
    if tool == "test_anna_sampling":
        future = asyncio.run_coroutine_threadsafe(run_anna_sampling_smoke(arguments, invoke_id), loop)
        try:
            return {"success": True, "tool": tool, "data": future.result(timeout=180.0)}
        except SamplingError as exc:
            raise RuntimeError(json.dumps({"code": exc.code, "message": exc.message, "data": exc.data}, ensure_ascii=False)) from exc
    if tool == "test_aps_storage":
        future = asyncio.run_coroutine_threadsafe(run_aps_storage_smoke(arguments), loop)
        return {"success": True, "tool": tool, "data": future.result(timeout=60.0)}
    if tool == "read_primary_emails":
        return {"success": True, "tool": tool, "data": read_primary_emails(arguments.get("mailbox", ""), arguments.get("limit", 5))}
    if tool == "list_cached_emails":
        return {"success": True, "tool": tool, "data": list_cached_emails(arguments.get("mailbox", ""))}
    if tool == "get_cached_email":
        return {"success": True, "tool": tool, "data": get_cached_email(arguments.get("mailbox", ""), arguments.get("message_id", ""))}
    if tool == "start_mail_agent_run":
        return {"success": True, "tool": tool, "data": start_mail_agent_run(arguments, invoke_id)}
    if tool == "get_mail_agent_run":
        return {"success": True, "tool": tool, "data": get_mail_agent_run(arguments.get("run_id", ""))}
    if tool == "start_custom_scan":
        return {"success": True, "tool": tool, "data": start_custom_scan(arguments, invoke_id)}
    if tool == "re_run_custom_scan":
        return {"success": True, "tool": tool, "data": re_run_custom_scan(arguments, invoke_id)}
    if tool == "get_authorized_email":
        from mail_agent.mail_adapter import get_authorized_email
        email = get_authorized_email()
        return {"success": True, "tool": tool, "data": {"email": email}}
    if tool == "get_custom_plans":
        return {"success": True, "tool": tool, "data": _sync_get_custom_plans()}
    if tool == "get_custom_plan_detail":
        return {"success": True, "tool": tool, "data": _sync_get_custom_plan_detail(arguments)}

    if tool == "get_active_cards":
        return {"success": True, "tool": tool, "data": _sync_get_active_cards(arguments)}
    if tool == "get_run_history":
        return {"success": True, "tool": tool, "data": _sync_get_run_history()}

    # ── V2 interaction tools (async → dispatch to event loop) ──
    if tool in (
        "get_card_detail", "summarize_thread",
        "generate_draft_reply", "revise_draft", "record_card_decision",
        "record_snooze", "restore_card", "record_learning",
        "start_summarize_thread", "start_generate_draft",
        "delete_custom_plan",
        "get_scan_plan", "set_scan_plan",
    ):
        future = asyncio.run_coroutine_threadsafe(
            _handle_v2_tool(tool, arguments, invoke_id),
            loop,
        )
        try:
            return {"success": True, "tool": tool, "data": future.result(timeout=180.0)}
        except SamplingError as exc:
            raise RuntimeError(json.dumps({"code": exc.code, "message": exc.message, "data": exc.data}, ensure_ascii=False)) from exc

    if tool == "run_mail_agent":
        future = asyncio.run_coroutine_threadsafe(
            run_mail_agent_pipeline(
                user_request=arguments.get("user_request", ""),
                mailbox=arguments.get("mailbox", ""),
                mode=arguments.get("mode", "auto"),
                max_messages=arguments.get("max_messages", arguments.get("primary_count", 20)),
                primary_count=arguments.get("primary_count", 20),
                ai_provider=arguments.get("ai_provider", "anna-llm"),
                invoke_id=invoke_id,
            ),
            loop,
        )
        try:
            return {"success": True, "tool": tool, "data": future.result(timeout=600.0)}
        except SamplingError as exc:
            raise RuntimeError(json.dumps({"code": exc.code, "message": exc.message, "data": exc.data}, ensure_ascii=False)) from exc

    raise ValueError(f"Unknown tool: {tool}")


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    try:
        if method == "initialize":
            return make_response(request_id, result=handle_initialize(params))
        if method == "describe":
            return make_response(request_id, result=MANIFEST)
        if method == "health":
            return make_response(
                request_id,
                result={
                    "status": "healthy",
                    "timestamp": beijing_now(),
                    "version": VERSION,
                    "tools_count": len(MANIFEST["tools"]),
                },
            )
        if method == "invoke":
            return make_response(request_id, result=handle_invoke(params))
        if method == "shutdown":
            return make_response(request_id, result={"ok": True})
        return make_response(request_id, error=make_error(-32601, f"Method not found: {method}"))
    except ValueError as exc:
        return make_response(request_id, error=make_error(-32601, str(exc)))
    except RuntimeError as exc:
        try:
            error_data = json.loads(str(exc))
        except json.JSONDecodeError:
            error_data = {"code": -32603, "message": str(exc)}
        return make_response(request_id, error=make_error(int(error_data.get("code", -32603)), str(error_data.get("message", exc)), error_data.get("data")))
    except Exception as exc:
        log(f"internal error: {type(exc).__name__}: {exc}")
        return make_response(request_id, error=make_error(-32603, "Internal error"))


def handle_line(line: str) -> None:
    # Diagnostic: check if stdin encoding is working for CJK text
    try:
        _diag_bytes = line.encode("utf-8")
    except Exception:
        _diag_bytes = b"<encode failed>"
    non_ascii = any(b > 127 for b in _diag_bytes)
    if non_ascii and len(line) > 40:
        log(f"stdin-diag first 80 chars: {repr(line[:80])}")

    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        write_frame(make_response(None, error=make_error(-32700, "Parse error")))
        return

    if not isinstance(message, dict):
        write_frame(make_response(None, error=make_error(-32600, "Invalid request")))
        return

    if "method" not in message:
        if not sampling.dispatch_response(message) and not _route_storage_response(message):
            log(f"unmatched response id={message.get('id')!r}")
        return

    response = handle_request(message)
    if response is not None and message.get("id") is not None:
        write_frame(response)


def main() -> None:
    # Write startup log so we can diagnose harness crashes even without stderr.
    _diag_dir = (Path(__file__).resolve().parents[1] / ".local_storage")
    _diag_dir.mkdir(parents=True, exist_ok=True)
    _diag_path = _diag_dir / "agent_startup.log"
    with open(_diag_path, "a", encoding="utf-8") as _df:
        _df.write(f"{beijing_now()} startup stdin={sys.stdin.encoding} stdout={sys.stdout.encoding}\n")

    log("ready")
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="mail-agent-rpc") as pool:
        try:
            for raw_line in sys.stdin:
                line = raw_line.strip()
                if line:
                    pool.submit(handle_line, line)
        except Exception as exc:
            with open(_diag_path, "a", encoding="utf-8") as _df:
                import traceback
                _df.write(f"{beijing_now()} CRASH {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n")
            log(f"stdin loop crashed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
