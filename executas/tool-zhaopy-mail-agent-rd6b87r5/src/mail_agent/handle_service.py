"""Handle Panel service — thread summary, draft reply, and revise draft LLM calls.

§1.2 of PRD-V2: when the user clicks "Handle" on an attention card, the panel
shows thread metadata, latest email, thread summary (on-demand LLM), and
draft reply (LLM-generated, user-editable).
"""

from __future__ import annotations

import json
from typing import Any

from .types import CandidateItem, MailboxProfile, MailStrategy
from .storage_types import PersistentCard


# ── Thread context fetch ────────────────────────────────────────────

def _fetch_thread_context_sync(
    mailbox: str,
    card: PersistentCard,
) -> dict[str, Any]:
    """Fetch full thread messages for a card (sync, using cached Gmail data).

    Returns structured thread data for the Handle Panel header.
    """
    from .mail_adapter import get_thread_context

    try:
        thread = get_thread_context(mailbox, card.thread_id, max_messages=10)
        messages = thread.messages if thread else []
    except Exception:
        messages = []

    if not messages:
        return {
            "thread_id": card.thread_id,
            "message_count": 0,
            "from": card.original.from_addr,
            "to": card.original.to_addr,
            "cc": "",
            "subject": card.original.thread,
            "latest_time": card.original.time,
            "messages": [],
        }

    latest = messages[-1]
    return {
        "thread_id": card.thread_id,
        "message_count": len(messages),
        "from": latest.from_addr or "",
        "to": latest.to_addr or "",
        "cc": latest.cc or "",
        "subject": card.original.thread,
        "latest_time": latest.internal_date or "",
        "messages": [
            {
                "from": m.from_addr,
                "to": m.to_addr,
                "cc": m.cc or "",
                "date": m.internal_date,
                "subject": m.subject,
                "body": (m.body_text or "")[:2000],
            }
            for m in messages[-5:]
        ],
    }


# ── Thread summary ──────────────────────────────────────────────────

THREAD_SUMMARY_SYSTEM = """You are Anna's thread summarizer. Summarize an email thread for the user so they can quickly understand what happened and what's needed.

Output a JSON object with these fields:
- core_ask: what the other party is actually asking for (1 short sentence, English)
- current_progress: where the discussion stands right now (1-2 sentences, English)
- open_questions: any unresolved points the user should address (English)
- user_action_needed: what specifically the user needs to respond to (English)
- tone: neutral | warm | urgent | waiting

Keep it factual. Do not invent details not present in the thread."""


def build_thread_summary_prompt(thread_context: dict[str, Any], card: PersistentCard) -> str:
    """Build the user prompt for thread summarization."""
    messages_text = ""
    for i, m in enumerate(thread_context.get("messages", []), start=1):
        messages_text += f"\n--- Message {i} ---\n"
        messages_text += f"From: {m.get('from', '')}\n"
        messages_text += f"Date: {m.get('date', '')}\n"
        messages_text += f"Body: {m.get('body', '')[:800]}\n"

    return f"""Card context:
Title: {card.title}
Summary: {card.summary}

Thread has {thread_context.get('message_count', 0)} messages. Latest {len(thread_context.get('messages', []))} shown below:
{messages_text}

Summarize this thread. Return JSON only."""


# ── Draft reply & revise (unified) ───────────────────────────────────

_DRAFT_SYSTEM = """You are Anna, an executive assistant. Generate or revise a reply email for the user.

Output a JSON object with:
- subject: reply subject line (keep original subject, add "Re: " prefix only if not already present)
- body: the draft email body (plain text, professional but warm tone)
- tone: the tone of the reply (e.g. "warm and professional", "brief confirmation")
- note: a short internal note about what you did (English, <=50 chars)

Guidelines:
- Match the sender's tone and formality level
- Be concise — reply length should be proportional to the original message
- If the user gave specific instructions, apply them precisely while keeping unmentioned parts
- If no existing draft, generate from scratch based on the thread and user instructions
- Never make commitments or promises on the user's behalf
- Do not include email headers (To, From, CC) in the body"""


def _build_draft_prompt(
    card: PersistentCard,
    thread_context: dict[str, Any],
    reply_mode: str,
    current_draft: str = "",
    revision_input: str = "",
) -> str:
    """Build the unified user prompt for draft generation or revision."""
    latest_msg = None
    msgs = thread_context.get("messages", [])
    if msgs:
        latest_msg = msgs[-1]

    has_draft = bool(current_draft.strip())
    has_instruction = bool(revision_input.strip())

    parts = [
        f"Card: {card.title}",
        f"Context: {card.summary}",
        f"Action needed: {card.recommendation}",
        "",
        "Latest email:",
        f"From: {latest_msg.get('from', '') if latest_msg else card.original.from_addr}",
        f"Subject: {thread_context.get('subject', card.original.thread)}",
        f"Body: {latest_msg.get('body', '')[:1200] if latest_msg else 'Not available'}",
    ]

    if has_draft:
        parts.append(f"\nExisting draft:\n{current_draft}")

    if has_instruction:
        task = "Revise the existing draft" if has_draft else "Generate a new draft incorporating these instructions"
        parts.append(f"\nUser instruction:\n{revision_input}")
    else:
        task = "Revise the existing draft considering the thread context" if has_draft else "Draft a reply based on the thread above"

    parts.append(f"\nReply mode: {reply_mode}")
    parts.append(f"Thread has {thread_context.get('message_count', 0)} total messages.")
    parts.append(f"\n{task}. Return JSON only.")

    return "\n".join(parts)


# ── LLM call wrappers ───────────────────────────────────────────────

async def summarize_thread(
    card: PersistentCard,
    mailbox: str,
    sampling_create_message: Any = None,
) -> dict[str, Any]:
    """Call LLM to summarize a thread. Returns the summary JSON."""
    from .llm import call_llm_json_safe

    thread_ctx = _fetch_thread_context_sync(mailbox, card)
    prompt = build_thread_summary_prompt(thread_ctx, card)

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=THREAD_SUMMARY_SYSTEM,
        user_message=prompt,
        fallback={"core_ask": "Unable to summarize", "current_progress": "", "open_questions": [], "user_action_needed": "", "tone": "neutral"},
        temperature=0.2,
        max_tokens=800,
        timeout=90.0,
        metadata={"tool": "summarize_thread", "card_id": card.card_id},
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    return {
        "summary": payload,
        "thread_context": {
            "message_count": thread_ctx.get("message_count", 0),
            "latest_from": thread_ctx.get("from", ""),
            "latest_time": thread_ctx.get("latest_time", ""),
        },
        "fallback_used": result.get("fallback_used", False),
        "fallback_reason": result.get("fallback_reason", ""),
    }


async def generate_draft_reply(
    card: PersistentCard,
    mailbox: str,
    reply_mode: str = "reply_to_sender",
    sampling_create_message: Any = None,
    *,
    current_draft: str = "",
    revision_input: str = "",
) -> dict[str, Any]:
    """Generate or revise a draft reply. If current_draft is non-empty, revises it."""
    from .llm import call_llm_json_safe

    thread_ctx = _fetch_thread_context_sync(mailbox, card)
    prompt = _build_draft_prompt(card, thread_ctx, reply_mode, current_draft, revision_input)

    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=_DRAFT_SYSTEM,
        user_message=prompt,
        fallback={"subject": "", "body": current_draft or "", "tone": "", "note": "Draft generation failed"},
        temperature=0.3,
        max_tokens=1200,
        timeout=150.0,
        metadata={"tool": "generate_draft", "card_id": card.card_id, "reply_mode": reply_mode},
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    return {
        "draft": payload,
        "reply_mode": reply_mode,
        "fallback_used": result.get("fallback_used", False),
        "fallback_reason": result.get("fallback_reason", ""),
    }


# ── Reply now ────────────────────────────────────────────────────────

async def reply_now(
    card: PersistentCard,
    mailbox: str,
    draft_body: str,
    reply_mode: str = "reply_to_sender",
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Send the draft reply via Gmail API (or mock if dry_run=True).

    dry_run=True (default): 仅验证参数并返回模拟结果，不真实发送邮件。
    dry_run=False: 通过 Gmail API 真实发送。
    """
    from .mail_adapter import send_reply

    if not draft_body.strip():
        return {"ok": False, "error": "Draft body is empty", "dry_run": dry_run}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": "Mock: reply was NOT actually sent.",
            "detail": {
                "to": card.original.from_addr,
                "thread": card.original.thread,
                "reply_mode": reply_mode,
                "body_preview": draft_body[:200],
            },
        }

    try:
        result = send_reply(
            mailbox=mailbox,
            thread_id=card.thread_id,
            to_addr=card.original.from_addr,
            body=draft_body,
            reply_mode=reply_mode,
        )
        return {"ok": True, "dry_run": False, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "dry_run": False}
