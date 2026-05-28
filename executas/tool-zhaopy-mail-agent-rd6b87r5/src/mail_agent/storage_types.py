"""Persistent data types for the mail agent's local JSON store.

Key naming convention (user scope):
    mailbox/{sanitized_email}/scan_state
    mailbox/{sanitized_email}/msg/{gmail_message_id}
    mailbox/{sanitized_email}/run/{run_id}
    mailbox/{sanitized_email}/cards/active
    prefs/snooze
    prefs/learning
    runs/history
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

BEIJING_TZ = timezone(__import__("datetime").timedelta(hours=8), name="Asia/Shanghai")


def _now() -> str:
    return datetime.now(BEIJING_TZ).isoformat()


# ── Scan state ──────────────────────────────────────────────────────


@dataclass
class ScanState:
    mailbox: str
    last_scan_ts: str = ""             # ISO timestamp of last scan
    last_message_internal_date: str = ""  # 最近一次扫描到的最新 Gmail internalDate
    last_history_id: str = ""           # Gmail historyId at last scan
    total_scans: int = 0
    total_processed: int = 0

    @classmethod
    def empty(cls, mailbox: str) -> ScanState:
        return cls(mailbox=mailbox)


# ── Scan plan (user-configurable scan preferences) ──────────────────


@dataclass
class ScanPlan:
    mailbox: str
    schedule: str = "manual"         # manual | every_morning | every_afternoon | twice_daily | workdays
    time_range: str = "auto"         # auto | since_last | last_24h | last_7d | unread_backlog
    max_messages: int = 100
    priorities: list[str] = field(default_factory=lambda: [
        "inbox_first", "active_threads", "important_contacts", "security_billing",
    ])
    include_newsletters: bool = False
    include_promotions: bool = False
    include_archived: bool = False
    batch_behavior: str = "ask"      # ask | auto_300 | never_older
    active: bool = True
    updated_at: str = field(default_factory=_now)

    @classmethod
    def empty(cls, mailbox: str) -> ScanPlan:
        return cls(mailbox=mailbox)


# ── Processed message index ─────────────────────────────────────────


@dataclass
class ProcessedMessage:
    message_id: str
    thread_id: str = ""
    from_addr: str = ""
    subject: str = ""
    snippet: str = ""
    internal_date: str = ""
    processed_at: str = field(default_factory=_now)
    run_id: str = ""
    is_candidate: bool = False
    candidate_kind: str = ""
    priority: str = "low"
    read_depth: str = "header_only"
    confidence: float = 0.0


# ── V2 Attention Card (persisted) ───────────────────────────────────


@dataclass
class CardDetails:
    needs: str = ""              # what user action is needed
    latest_activity: str = ""    # who did what + when
    reviewed: str = ""           # Anna read depth description
    mailbox: str = ""


@dataclass
class OriginalEmail:
    source: str = "Gmail"
    thread: str = ""
    from_addr: str = ""
    to_addr: str = ""
    time: str = ""
    status: str = "Connected Gmail source"
    body: str = ""


@dataclass
class CardAction:
    id: str = ""
    label: str = ""           # 内部/抽屉详情中使用
    button_label: str = ""    # 卡片主按钮文案（更短、动作感更强）
    primary: bool = False
    status_title: str = ""
    status: str = ""


CardStatus = Literal["pending", "snoozed", "resolved", "dismissed"]


@dataclass
class PersistentCard:
    card_id: str
    message_id: str = ""
    thread_id: str = ""
    # 3-line display
    title: str = ""              # Line 1: Attention Title (≤1 visual line)
    summary: str = ""            # Line 2: Compressed Context (1–2 factual bullets)
    recommendation: str = ""     # Line 3: Suggested Next Step
    label: str = ""              # category tag
    priority: str = "medium"
    item_type: str = ""          # from judgment: reply_required, security_risk, low_value_cleanup, etc.
    draft_reply: str = ""        # persisted LLM-generated draft reply body
    thread_summary: str = ""     # persisted LLM thread summary (JSON string)
    display_section: str = "main"
    # expanded details
    details: CardDetails = field(default_factory=CardDetails)
    original: OriginalEmail = field(default_factory=OriginalEmail)
    actions: list[CardAction] = field(default_factory=list)
    # lifecycle
    status: CardStatus = "pending"
    snooze_until: str = ""       # ISO timestamp, set when snoozed
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    resolved_at: str = ""
    resolution: str = ""         # "no_action_needed" | "handled_manually" | "dismissed"


@dataclass
class ActiveCards:
    """Wraps the active cards list stored under mailbox/{id}/cards/active."""
    cards: list[PersistentCard] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)


# ── Run record ──────────────────────────────────────────────────────


@dataclass
class RunRecord:
    run_id: str
    mailbox: str = ""
    ts: str = field(default_factory=_now)
    strategy_mode: str = ""
    user_request: str = ""
    mode: str = "auto"
    plan_id: str = ""
    scanned_count: int = 0
    candidate_count: int = 0
    main_count: int = 0
    lower_count: int = 0
    summary: list[str] = field(default_factory=list)
    strategy: list[str] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)


# ── User preferences ────────────────────────────────────────────────


@dataclass
class SnoozePrefs:
    threads: list[str] = field(default_factory=list)      # thread subjects/IDs to deprioritize
    senders: list[str] = field(default_factory=list)      # sender addresses to deprioritize
    categories: list[str] = field(default_factory=list)   # categories to deprioritize
    updated_at: str = field(default_factory=_now)


@dataclass
class LearningRecord:
    pattern: str = ""
    action: str = ""
    learnt_at: str = field(default_factory=_now)


@dataclass
class UserPreferences:
    snooze: SnoozePrefs = field(default_factory=SnoozePrefs)
    learning: list[LearningRecord] = field(default_factory=list)


# ── Run history entry (lightweight, stored in runs/history list) ────


@dataclass
class RunHistoryEntry:
    run_id: str
    mailbox: str = ""
    ts: str = ""
    request: str = ""
    mode: str = ""
    strategy: str = ""
    plan_id: str = ""
    result: str = ""      # 1-line summary
    summary: str = ""     # multi-line detail
