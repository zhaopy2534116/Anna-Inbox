"""Card lifecycle service — build, merge, expire, and format V2 attention cards.

Translates Phase 2 JudgmentResults + MessageLite into PersistentCard objects,
merges new cards with existing active cards, and handles snooze expiry.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .types import CandidateItem, JudgmentResult, MessageLite, ReadDepth
from .storage_types import (
    BEIJING_TZ,
    ActiveCards,
    CardAction,
    CardDetails,
    OriginalEmail,
    PersistentCard,
    _now,
)


def create_card_id() -> str:
    return f"card_{uuid.uuid4().hex[:12]}"


# ── Read depth → user-facing description ───────────────────────────

_READ_DEPTH_LABELS: dict[str, str] = {
    "header_only": "Header and snippet only",
    "message_detail": "Latest message and attachment metadata",
    "thread_context": "Full thread",
    "batch_summary": "Batch summary",
}


def _describe_read_depth(depth: ReadDepth | str) -> str:
    return _READ_DEPTH_LABELS.get(str(depth), "Header and snippet only")


# ── Build single card ───────────────────────────────────────────────

def build_card(
    candidate: CandidateItem,
    judgment: JudgmentResult,
    message: MessageLite | None,
    mailbox: str,
) -> PersistentCard:
    """Build a V2 PersistentCard from one candidate + judgment result."""
    fd = judgment.final_decision

    # Build details
    details = CardDetails(
        needs=_build_needs(candidate, judgment),
        latest_activity=_build_latest_activity(message, candidate, judgment),
        reviewed=_describe_read_depth(candidate.read_depth_required),
        mailbox=mailbox,
    )

    # Build original
    original = OriginalEmail(
        source="Gmail",
        thread=message.subject or "",
        from_addr=message.from_addr or "",
        to_addr=message.to_addr or "",
        time=message.internal_date or "",
        status="Connected Gmail source",
        body=(message.snippet or "")[:500],
    )

    # Build actions
    actions = _build_card_actions(fd)

    item_type = judgment.base_judgment.item_type if judgment else ""

    return PersistentCard(
        card_id=create_card_id(),
        message_id=candidate.message_ids[0] if candidate.message_ids else "",
        thread_id=candidate.thread_id,
        title=fd.user_facing_summary or candidate.evidence.get("subject", "Unknown"),
        summary=fd.user_facing_reason or "",
        recommendation=_format_recommendation(fd.user_facing_recommendation),
        label=fd.display_bucket or "",
        priority=fd.priority,
        item_type=item_type or "",
        display_section="main" if fd.should_show_in_main_result else "lower",
        details=details,
        original=original,
        actions=actions,
        status="pending",
    )


def _build_needs(candidate: CandidateItem, judgment: JudgmentResult) -> str:
    """Derive the 'Needs' field per PRD-V2 §1.1.2.

    Priority: LLM-generated needs > mode-specific fallback > generic fallback.
    """
    mode = judgment.mode_judgment if isinstance(judgment.mode_judgment, dict) else {}

    # 1. LLM-generated needs (from batch or single-item prompt)
    llm_needs = str(mode.get("needs") or "").strip()
    if llm_needs:
        return llm_needs

    # 2. Mode-specific fallback from structured judgment fields
    strategy_mode = str(judgment.strategy_mode or "")

    if strategy_mode == "default_secretary":
        bucket = str(mode.get("bucket") or "")
        needs_map: dict[str, str] = {
            "must_review": "Manual review",
            "needs_reply": "Reply needed",
            "needs_confirmation": "Confirmation needed",
            "agent_can_prepare": "Draft preparation",
            "safe_cleanup": "No action needed",
            "lower_priority": "No action needed",
            "ignore": "No action needed",
        }
        return needs_map.get(bucket, "Review")

    if strategy_mode == "creator_opportunity":
        step = str(mode.get("suggested_next_step") or "")
        step_map: dict[str, str] = {
            "send_short_update": "Send update",
            "share_build_or_demo": "Share demo",
            "ask_for_requirements": "Ask for details",
            "send_pricing_or_terms": "Send pricing",
            "wait": "Wait for reply",
            "close_or_archive": "Close thread",
            "manual_review": "Partnership review",
        }
        return step_map.get(step, "Partnership review")

    if strategy_mode == "security_billing":
        handling = str(mode.get("recommended_handling") or "")
        handling_map: dict[str, str] = {
            "confirm_login": "Login check",
            "check_payment": "Payment check",
            "review_invoice": "Invoice review",
            "increase_quota_or_clean_storage": "Quota check",
            "review_account_access": "Access review",
            "record_only": "Record only",
            "ignore": "No action needed",
        }
        return handling_map.get(handling, "Security review")

    # 3. Generic fallback (should rarely be reached)
    bj = judgment.base_judgment
    if bj.requires_user_action:
        return "User action needed"
    if bj.risk_level in ("high", "critical"):
        return "Manual review"
    return "Review"


def _format_relative_time(internal_date: str) -> str:
    """Convert Gmail internalDate (epoch millis string) to human-readable relative time.

    Examples: "today · 8:12 PM", "yesterday · 10:14 AM", "May 20 · 3:30 PM"
    """
    if not internal_date:
        return "recently"
    try:
        ts = int(internal_date) / 1000.0
        dt = datetime.fromtimestamp(ts, tz=BEIJING_TZ)
        now = datetime.now(BEIJING_TZ)
        diff = now - dt
        hour = dt.hour
        minute = dt.minute
        ampm = "AM" if hour < 12 else "PM"
        display_hour = hour % 12
        if display_hour == 0:
            display_hour = 12
        time_str = f"{display_hour}:{minute:02d} {ampm}"

        if diff.days == 0 and diff.seconds < 86400 and dt.day == now.day:
            return f"today · {time_str}"
        if diff.days == 1 or (diff.days == 0 and diff.seconds < 172800 and dt.day != now.day):
            return f"yesterday · {time_str}"
        if diff.days < 7:
            return f"{dt.strftime('%A')} · {time_str}"
        return f"{dt.strftime('%b %d')} · {time_str}"
    except (ValueError, TypeError, OSError):
        return internal_date or "recently"


def _build_latest_activity(
    msg: MessageLite | None,
    candidate: CandidateItem | None = None,
    judgment: JudgmentResult | None = None,
) -> str:
    """Format 'Latest activity' per PRD-V2 §1.1.2: who did what + when.

    Priority: LLM-generated latest_action/latest_actor > item_type derivation > signals > fallback.
    """
    if not msg:
        return "Unknown"

    sender = (msg.from_addr or "Someone").split("<")[0].strip().strip('"')
    when = _format_relative_time(msg.internal_date)

    # 1. LLM-generated fields (from batch or single-item prompt)
    if judgment and isinstance(judgment.mode_judgment, dict):
        mode = judgment.mode_judgment
        llm_action = str(mode.get("latest_action") or "").strip()
        llm_actor = str(mode.get("latest_actor") or "").strip()
        if llm_action:
            actor = llm_actor or sender
            return f"{actor} {llm_action} · {when}"

    # 2. Derive action from judgment item_type or candidate kind
    item_type = ""
    kind = ""
    if judgment:
        item_type = judgment.base_judgment.item_type
    if candidate:
        kind = candidate.kind or ""

    action = _derive_action(item_type, kind)
    return f"{sender} {action} · {when}"


# ── Action derivation helpers ────────────────────────────────────────

_KIND_ACTION_MAP: dict[str, str] = {
    "reply_required_possible": "replied",
    "confirmation_required_possible": "asked for confirmation",
    "security_risk_possible": "sent a security alert",
    "billing_issue_possible": "sent a billing notice",
    "account_notice_possible": "sent an account notice",
    "business_thread_possible": "sent a message",
    "creator_thread_possible": "sent a collaboration message",
    "safe_account_record": "sent a receipt",
    "safe_cleanup_bundle": "sent a newsletter",
    "unsure": "sent an email",
}

_ITEM_TYPE_ACTION_MAP: dict[str, str] = {
    "reply_required": "replied",
    "confirmation_required": "asked for confirmation",
    "security_risk": "sent a security alert",
    "billing_or_subscription": "sent a billing notice",
    "business_or_creator_thread": "sent a message",
    "account_notice": "sent an account notice",
    "low_value_cleanup": "sent a notification",
    "unknown": "sent an email",
}


def _derive_action(item_type: str, kind: str) -> str:
    """Derive a concise action description from item_type or candidate kind."""
    if item_type and item_type in _ITEM_TYPE_ACTION_MAP:
        return _ITEM_TYPE_ACTION_MAP[item_type]
    if kind and kind in _KIND_ACTION_MAP:
        return _KIND_ACTION_MAP[kind]
    return "sent an email"


def _format_recommendation(text: str) -> str:
    """Ensure recommendation starts with 'Suggested:' per PRD §1.1.1."""
    t = (text or "").strip()
    if not t:
        return "Suggested: Review this item."
    if t.lower().startswith("suggested"):
        return t
    return f"Suggested: {t}"


def _build_card_actions(fd: Any) -> list[CardAction]:
    """Convert FinalDecision.recommended_actions into CardAction list."""
    actions: list[CardAction] = [
        CardAction(id="view", label="View original", button_label="", primary=False),
    ]

    for act in (fd.recommended_actions or []):
        act_type = act.get("action_type", "do_nothing")
        if act_type in ("do_nothing", "mark_read"):
            continue
        actions.append(CardAction(
            id=act_type,
            label=_action_label(act_type),
            button_label=_primary_button_label(act_type),
            primary=False,
        ))

    # Fallback: if no actionable item survived filtering, add a generic handle action
    non_view = [a for a in actions if a.id != "view"]
    if not non_view:
        actions.append(CardAction(
            id="handle",
            label="Handle",
            button_label="Handle",
            primary=True,
        ))
        return actions
    else:
        non_view[0].primary = True

    return actions


_ACTION_LABELS: dict[str, str] = {
    "create_draft": "Prepare reply",
    "apply_label": "Add label",
    "create_reminder": "Remind me later",
    "save_note": "Save note",
    "archive": "Prepare cleanup",
}

_PRIMARY_BUTTON_LABELS: dict[str, str] = {
    "create_draft": "Reply",
    "apply_label": "Label",
    "create_reminder": "Remind",
    "save_note": "Save note",
    "archive": "Clean up",
}


def _action_label(act_type: str) -> str:
    return _ACTION_LABELS.get(act_type, act_type.replace("_", " ").title())


def _primary_button_label(act_type: str) -> str:
    return _PRIMARY_BUTTON_LABELS.get(act_type, _action_label(act_type))


# ── Merge new and existing cards ────────────────────────────────────

def merge_cards(existing: ActiveCards, new_cards: list[PersistentCard]) -> ActiveCards:
    """Merge new scan results with existing active cards.

    Rules:
    - Keep existing cards that are still 'pending' (not yet resolved).
    - Snoozed cards whose snooze_until has passed → back to 'pending'.
    - Snoozed cards still within window → keep as snoozed.
    - Resolved/dismissed cards → drop.
    - Deduplicate: if a new card has the same thread_id as an existing one,
      prefer the new version (updated judgment).
    """
    now = _now()
    merged: dict[str, PersistentCard] = {}

    # Process existing cards
    for card in existing.cards:
        if card.status == "resolved" or card.status == "dismissed":
            continue
        if card.status == "snoozed":
            if card.snooze_until and card.snooze_until < now:
                card.status = "pending"
                card.snooze_until = ""
                card.updated_at = now
            else:
                merged[card.thread_id or card.card_id] = card
                continue
        merged[card.thread_id or card.card_id] = card

    # Merge new cards (overwrite existing by thread_id)
    for card in new_cards:
        if card.priority == "ignore":
            continue
        key = card.thread_id or card.card_id
        merged[key] = card

    return ActiveCards(cards=list(merged.values()), updated_at=now)


# ── Card list for frontend ──────────────────────────────────────────

def cards_to_frontend(cards: ActiveCards) -> list[dict[str, Any]]:
    """Serialize active cards to the V2 frontend format."""
    result: list[dict[str, Any]] = []
    for card in cards.cards:
        result.append({
            "id": card.card_id,
            "title": card.title,
            "summary": card.summary,
            "recommendation": card.recommendation,
            "label": card.label,
            "priority": card.priority,
            "item_type": card.item_type,
            "draft_reply": card.draft_reply,
            "thread_summary": card.thread_summary,
            "displaySection": card.display_section,
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
                {
                    "id": a.id,
                    "label": a.label,
                    "buttonLabel": a.button_label,
                    "primary": a.primary,
                    "statusTitle": a.status_title,
                    "status": a.status,
                }
                for a in card.actions
            ],
            "status": card.status,
        })
    return result


# ── Build action memo ───────────────────────────────────────────────

def build_action_memo(cards: ActiveCards, judgments: list[JudgmentResult]) -> dict[str, list[str]]:
    """Build approval memo (safe / review) from judgments and cards."""
    safe: list[str] = []
    review: list[str] = []

    for j in judgments:
        fd = j.final_decision
        for act in (fd.recommended_actions or []):
            act_type = act.get("action_type", "do_nothing")
            reason = str(act.get("reason", ""))[:80]
            requires_approval = bool(act.get("requires_approval", True))
            if act_type in ("do_nothing", "mark_read"):
                continue
            if requires_approval:
                review.append(f"{_action_label(act_type)}: {reason}")
            else:
                safe.append(f"{_action_label(act_type)}: {reason}")

    return {"safe": safe, "review": review}
