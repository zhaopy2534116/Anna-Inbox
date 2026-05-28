"""High-level local-persistence operations for the mail agent.

All functions are async and use the shared storage singleton.
Callers must be running inside the asyncio event loop.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .storage_client import get_storage, get_files, scope as default_scope
from .storage_types import (
    ActiveCards,
    CardAction,
    CardDetails,
    CardStatus,
    LearningRecord,
    OriginalEmail,
    PersistentCard,
    ProcessedMessage,
    RunHistoryEntry,
    RunRecord,
    ScanPlan,
    ScanState,
    SnoozePrefs,
    UserPreferences,
    _now,
)


# ── Key builders ────────────────────────────────────────────────────

def _sanitize(email: str) -> str:
    """Sanitize email address for use in storage keys."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in email.strip()).strip("._") or "default"


def _mailbox_prefix(mailbox: str) -> str:
    return f"mailbox/{_sanitize(mailbox)}"


# ── Scan state ──────────────────────────────────────────────────────

async def get_scan_state(mailbox: str) -> ScanState:
    key = f"{_mailbox_prefix(mailbox)}/scan_state"
    result = await get_storage().get(key, scope=default_scope())
    if result.get("exists") and result.get("value"):
        raw = dict(result["value"])
        # 兼容旧持久化数据：历史记录里可能没有精确到邮件的最新时间。
        raw.setdefault("last_message_internal_date", "")
        return ScanState(**raw)
    return ScanState.empty(mailbox)


async def set_scan_state(mailbox: str, state: ScanState) -> dict:
    key = f"{_mailbox_prefix(mailbox)}/scan_state"
    return await get_storage().set(key, _dataclass_to_dict(state), scope=default_scope())


# ── Scan plan ────────────────────────────────────────────────────────


async def get_scan_plan(mailbox: str) -> ScanPlan:
    key = f"{_mailbox_prefix(mailbox)}/scan_plan"
    result = await get_storage().get(key, scope=default_scope())
    if result.get("exists") and result.get("value"):
        raw = dict(result["value"])
        raw.setdefault("mailbox", mailbox)
        return ScanPlan(**{k: v for k, v in raw.items() if k in ScanPlan.__dataclass_fields__})
    return ScanPlan.empty(mailbox)


async def set_scan_plan(mailbox: str, plan: ScanPlan) -> dict:
    key = f"{_mailbox_prefix(mailbox)}/scan_plan"
    plan.updated_at = _now()
    return await get_storage().set(key, _dataclass_to_dict(plan), scope=default_scope())


# ── Processed message index ─────────────────────────────────────────

def _msg_key(mailbox: str, message_id: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/msg/{message_id}"


async def get_processed_message_ids(mailbox: str) -> set[str]:
    """Return all already-processed Gmail message IDs for a mailbox.

    Uses storage.list with prefix so we avoid N individual GET calls.
    """
    prefix = f"{_mailbox_prefix(mailbox)}/msg/"
    processed: set[str] = set()
    cursor: str | None = None
    while True:
        result = await get_storage().list(prefix=prefix, cursor=cursor, limit=200, scope=default_scope())
        items = result.get("items") or []
        for item in items:
            key = item.get("key") if isinstance(item, dict) else item
            if key and key.startswith(prefix):
                msg_id = key[len(prefix):]
                if msg_id:
                    processed.add(msg_id)
        cursor = result.get("next_cursor")
        if not cursor:
            break
    return processed


async def filter_unprocessed(mailbox: str, message_ids: list[str]) -> list[str]:
    """Given a list of message IDs, return only the unprocessed ones."""
    processed = await get_processed_message_ids(mailbox)
    return [mid for mid in message_ids if mid not in processed]


async def mark_message_processed(mailbox: str, msg: ProcessedMessage) -> dict:
    """Mark a single message as processed."""
    key = _msg_key(mailbox, msg.message_id)
    return await get_storage().set(key, _dataclass_to_dict(msg), scope=default_scope())


async def mark_messages_processed_batch(mailbox: str, msgs: list[ProcessedMessage]) -> None:
    """Write processed-message markers for a batch (no concurrency control needed)."""
    for msg in msgs:
        await mark_message_processed(mailbox, msg)


async def is_message_processed(mailbox: str, message_id: str) -> bool:
    result = await get_storage().get(_msg_key(mailbox, message_id), scope=default_scope())
    return bool(result.get("exists"))


# ── Active cards ────────────────────────────────────────────────────

def _cards_key(mailbox: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/cards/active"


async def \
        get_active_cards(mailbox: str) -> ActiveCards:
    result = await get_storage().get(_cards_key(mailbox), scope=default_scope())
    if result.get("exists") and result.get("value"):
        raw = result["value"]
        cards = [_dict_to_persistent_card(c) for c in raw.get("cards", [])]
        return ActiveCards(cards=cards, updated_at=raw.get("updated_at", ""))
    return ActiveCards()


async def set_active_cards(mailbox: str, cards: ActiveCards) -> dict:
    cards.updated_at = _now()
    return await get_storage().set(_cards_key(mailbox), _dataclass_to_dict(cards), scope=default_scope())


async def update_card_status(
    mailbox: str, card_id: str, status: CardStatus, resolution: str = ""
) -> PersistentCard | None:
    """Update a single card's status in the active cards list."""
    active = await get_active_cards(mailbox)
    for card in active.cards:
        if card.card_id == card_id:
            card.status = status
            card.updated_at = _now()
            if status in ("resolved", "dismissed"):
                card.resolved_at = _now()
                card.resolution = resolution
            await set_active_cards(mailbox, active)
            return card
    return None


# ── Run records ─────────────────────────────────────────────────────

def _run_key(mailbox: str, run_id: str) -> str:
    return f"{_mailbox_prefix(mailbox)}/run/{run_id}"


async def save_run_record(mailbox: str, run: RunRecord) -> dict:
    return await get_storage().set(_run_key(mailbox, run.run_id), _dataclass_to_dict(run), scope=default_scope())


async def get_run_record(mailbox: str, run_id: str) -> RunRecord | None:
    result = await get_storage().get(_run_key(mailbox, run_id), scope=default_scope())
    if result.get("exists") and result.get("value"):
        return RunRecord(**result["value"])
    return None


# ── Run history (cross-mailbox) ─────────────────────────────────────

RUN_HISTORY_KEY = "runs/history"


async def get_run_history(limit: int = 20) -> list[RunHistoryEntry]:
    result = await get_storage().get(RUN_HISTORY_KEY, scope=default_scope())
    if result.get("exists") and result.get("value"):
        raw = result["value"]
        entries = [_dict_to_run_history_entry(e) for e in raw.get("entries", [])]
        return entries[:limit]
    return []


async def append_run_history(entry: RunHistoryEntry) -> dict:
    result = await get_storage().get(RUN_HISTORY_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else {"entries": []}
    if not isinstance(raw, dict):
        raw = {"entries": []}
    entries: list[dict] = raw.get("entries", [])
    entries.insert(0, _dataclass_to_dict(entry))
    # Keep last 50
    if len(entries) > 50:
        entries = entries[:50]
    raw["entries"] = entries
    return await get_storage().set(RUN_HISTORY_KEY, raw, scope=default_scope())


# ── User preferences ────────────────────────────────────────────────

SNOOZE_KEY = "prefs/snooze"
LEARNING_KEY = "prefs/learning"


async def get_user_prefs() -> UserPreferences:
    prefs = UserPreferences()
    snooze_result = await get_storage().get(SNOOZE_KEY, scope=default_scope())
    if snooze_result.get("exists") and snooze_result.get("value"):
        prefs.snooze = SnoozePrefs(**snooze_result["value"])
    learning_result = await get_storage().get(LEARNING_KEY, scope=default_scope())
    if learning_result.get("exists") and learning_result.get("value"):
        raw = learning_result["value"]
        prefs.learning = [LearningRecord(**r) for r in raw.get("records", [])]
    return prefs


async def set_snooze_prefs(prefs: SnoozePrefs) -> dict:
    prefs.updated_at = _now()
    return await get_storage().set(SNOOZE_KEY, _dataclass_to_dict(prefs), scope=default_scope())


async def add_snooze_sender(sender: str) -> dict:
    """Add a sender to the snooze preference list."""
    prefs = await get_user_prefs()
    if sender not in prefs.snooze.senders:
        prefs.snooze.senders.append(sender)
    return await set_snooze_prefs(prefs.snooze)


async def add_snooze_thread(thread: str) -> dict:
    prefs = await get_user_prefs()
    if thread not in prefs.snooze.threads:
        prefs.snooze.threads.append(thread)
    return await set_snooze_prefs(prefs.snooze)


async def append_learning(pattern: str, action: str) -> dict:
    result = await get_storage().get(LEARNING_KEY, scope=default_scope())
    raw = result.get("value") if result.get("exists") else {"records": []}
    if not isinstance(raw, dict):
        raw = {"records": []}
    records: list[dict] = raw.get("records", [])
    records.append(_dataclass_to_dict(LearningRecord(pattern=pattern, action=action)))
    if len(records) > 100:
        records = records[-100:]
    raw["records"] = records
    return await get_storage().set(LEARNING_KEY, raw, scope=default_scope())


# ── Serialization helpers ───────────────────────────────────────────

def _dataclass_to_dict(obj: Any) -> dict:
    """Convert a dataclass instance to a JSON-safe dict."""
    from dataclasses import asdict
    return asdict(obj)


def _dict_to_persistent_card(d: dict) -> PersistentCard:
    details = CardDetails(**d.get("details", {})) if isinstance(d.get("details"), dict) else CardDetails()
    original = OriginalEmail(**d.get("original", {})) if isinstance(d.get("original"), dict) else OriginalEmail()
    actions = [_dict_to_card_action(a) for a in d.get("actions", [])] if isinstance(d.get("actions"), list) else []
    return PersistentCard(
        card_id=d.get("card_id", ""),
        message_id=d.get("message_id", ""),
        thread_id=d.get("thread_id", ""),
        title=d.get("title", ""),
        summary=d.get("summary", ""),
        recommendation=d.get("recommendation", ""),
        label=d.get("label", ""),
        priority=d.get("priority", "medium"),
        item_type=d.get("item_type", ""),
        draft_reply=d.get("draft_reply", ""),
        thread_summary=d.get("thread_summary", ""),
        display_section=d.get("display_section", "main"),
        details=details,
        original=original,
        actions=actions,
        status=d.get("status", "pending"),
        snooze_until=d.get("snooze_until", ""),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        resolved_at=d.get("resolved_at", ""),
        resolution=d.get("resolution", ""),
    )


def _dict_to_card_action(d: dict) -> CardAction:
    return CardAction(
        id=d.get("id", ""),
        label=d.get("label", ""),
        button_label=d.get("button_label", ""),
        primary=d.get("primary", False),
        status_title=d.get("status_title", ""),
        status=d.get("status", ""),
    )


def _dict_to_run_history_entry(d: dict) -> RunHistoryEntry:
    return RunHistoryEntry(
        run_id=d.get("run_id", ""),
        mailbox=d.get("mailbox", ""),
        ts=d.get("ts", ""),
        request=d.get("request", ""),
        mode=d.get("mode", ""),
        strategy=d.get("strategy", ""),
        plan_id=d.get("plan_id", ""),
        result=d.get("result", ""),
        summary=d.get("summary", ""),
    )


# ── Custom scan plans ──────────────────────────────────────────────

_CUSTOM_PLANS_KEY = "custom/scan_plans"
_MAX_CUSTOM_PLANS = 20


async def save_custom_plan(plan: Any) -> None:
    """保存或更新一个 CustomScanPlan。plan_id 已存在则覆盖，最多保留 20 条。"""
    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    if not isinstance(data, dict):
        data = {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", [])
    if not isinstance(plans, list):
        plans = []

    plan_dict = {
        "plan_id": plan.plan_id,
        "user_request": plan.user_request,
        "title": plan.title,
        "description": plan.description,
        "gmail_queries": plan.gmail_queries,
        "scan_budget": plan.scan_budget,
        "read_depth": plan.read_depth,
        "task_prompt": plan.task_prompt,
        "created_at": plan.created_at,
        "last_used_at": plan.last_used_at,
        "use_count": plan.use_count,
        "last_result_summary": plan.last_result_summary,
    }

    # Replace existing or prepend
    existing_idx = next((i for i, p in enumerate(plans) if p.get("plan_id") == plan.plan_id), None)
    if existing_idx is not None:
        plans[existing_idx] = plan_dict
    else:
        plans.insert(0, plan_dict)
        if len(plans) > _MAX_CUSTOM_PLANS:
            plans = plans[:_MAX_CUSTOM_PLANS]

    data["plans"] = plans
    await storage.set(_CUSTOM_PLANS_KEY, data, scope=default_scope())


async def get_custom_plan(plan_id: str) -> dict[str, Any] | None:
    """按 ID 加载单个 plan 的完整字典。"""
    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", []) if isinstance(data, dict) else []
    for p in plans:
        if p.get("plan_id") == plan_id:
            return p
    return None


async def list_custom_plans() -> list[dict[str, Any]]:
    """返回所有 plan 的元数据列表（供前端展示）。"""
    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", []) if isinstance(data, dict) else []
    # Return lightweight metadata
    return [
        {
            "plan_id": p.get("plan_id", ""),
            "title": p.get("title", ""),
            "user_request": p.get("user_request", ""),
            "created_at": p.get("created_at", ""),
            "last_used_at": p.get("last_used_at", ""),
            "use_count": p.get("use_count", 0),
            "last_result_summary": p.get("last_result_summary", ""),
        }
        for p in plans
    ]


async def update_plan_result(plan_id: str, summary: str) -> None:
    """执行完成后更新 last_used_at、use_count 和 last_result_summary。"""
    from datetime import datetime, timedelta, timezone
    BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
    now = datetime.now(BEIJING_TZ).isoformat()

    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", []) if isinstance(data, dict) else []
    for p in plans:
        if p.get("plan_id") == plan_id:
            p["last_used_at"] = now
            p["use_count"] = p.get("use_count", 0) + 1
            p["last_result_summary"] = summary
            break
    data["plans"] = plans
    await storage.set(_CUSTOM_PLANS_KEY, data, scope=default_scope())


async def delete_custom_plan(plan_id: str) -> None:
    storage = get_storage()
    raw = await storage.get(_CUSTOM_PLANS_KEY, scope=default_scope())
    data = raw.get("value") if raw.get("exists") else {"plans": []}
    plans: list[dict[str, Any]] = data.get("plans", []) if isinstance(data, dict) else []
    data["plans"] = [p for p in plans if p.get("plan_id") != plan_id]
    await storage.set(_CUSTOM_PLANS_KEY, data, scope=default_scope())
