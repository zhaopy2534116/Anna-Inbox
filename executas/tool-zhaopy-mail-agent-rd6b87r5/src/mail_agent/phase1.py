"""Phase 1 LLM 批量分类模块。

用一次 LLM 调用对所有邮件头进行批量分类，替换/增强基于规则的候选生成。
设计文档 §11.1。

工作流程：
  1. 将所有邮件的紧凑头信息（发件人、主题、片段、标签等）打包成一个 JSON
  2. 调用 LLM 一次性批量分类，输出每条邮件的 item_type、is_candidate、candidate_kind 等
  3. 解析 LLM 响应，与规则信号融合，生成 CandidateItem 列表
  4. LLM 失败时 fallback 到纯规则生成（generate_candidates）

相比纯规则方式，LLM 可以识别更微妙的语境（如区分"真实的合作邮件"
和"伪装成合作的平台通知"）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .types import (
    CandidateItem,
    CandidateKind,
    MailStrategy,
    MailboxProfile,
    MessageLite,
    ReadDepth,
)

# ── LLM Item Type → CandidateKind 映射 ───────────────────────────
# LLM 输出的 item_type 是语义类别，需要映射到系统的 CandidateKind

_ITEM_TYPE_TO_KIND: dict[str, CandidateKind] = {
    "SecurityRisk": "security_risk_possible",
    "BillingPayment": "billing_issue_possible",
    "NeedsReply": "reply_required_possible",
    "NeedsConfirmation": "confirmation_required_possible",
    "CustomerPartnerCommunication": "business_thread_possible",
    "CreatorOpportunity": "creator_thread_possible",
    "AccountNotice": "account_notice_possible",
    "SafeAccountRecord": "safe_account_record",
    "LowValueArchivable": "safe_cleanup_bundle",
    "NewsletterSignal": "safe_cleanup_bundle",
    "OrdinaryNotification": "safe_cleanup_bundle",
    "OutreachBounce": "safe_cleanup_bundle",
    "MeetingSchedule": "reply_required_possible",
    "RecruitingCandidate": "business_thread_possible",
    "Unknown": "unsure",
}

# Anna sampling 目前没有 schema/streaming 约束，Phase 1 大批量 JSON 容易输出不完整。
_ANNA_PHASE1_BATCH_SIZE = 8

# ── Prompt 构建 ──────────────────────────────────────────────────

_PHASE1_SYSTEM = """You are Anna's batch email triage engine. Classify ALL email headers in one pass.

## Item Types
SecurityRisk | BillingPayment | NeedsReply | NeedsConfirmation | CustomerPartnerCommunication | CreatorOpportunity | AccountNotice | SafeAccountRecord | LowValueArchivable | NewsletterSignal | OrdinaryNotification | Unknown

## Priority
- high: security incident, payment failure, direct ask from known contact, deadline today
- medium: needs reply/confirmation, collaboration opportunity, account notice worth checking
- low: newsletters, promotions, automated notifications, receipts, already-handled threads

## Read Depth
- header_only: obvious low-value (promotions, newsletters, social notifications)
- message_detail: needs body text to confirm (security, billing, account notices)
- thread_context: needs full thread history (ongoing conversations, collaboration)

## Safety Rules (NEVER violate)
1. Login alerts, password changes, suspicious sign-ins → SecurityRisk + high
2. Payment failed, chargeback, subscription expired → BillingPayment + high
3. Permission changes, account recovery → SecurityRisk + high
4. If the sender's email address matches the mailbox owner, the message is OUTGOING.
   SENT → is_candidate=false (user already sent it). DRAFT → is_candidate=true, item_type=NeedsReply, reason="unsent draft".

## Candidate Rules
- is_candidate=true: anything needing user attention, decision, or awareness
- is_candidate=false: promotions, social notifications, automated digests, already-resolved

## Output Constraint
Your entire response must be a single JSON object. The very first character you output must be `{`. Do NOT wrap the JSON in markdown fences. Do NOT write any text before or after the JSON."""


def _compact_header(msg: MessageLite, index: int) -> dict[str, Any]:
    """将 MessageLite 压缩为 LLM prompt 可用的紧凑字典。

    使用单字母 key 以减少 token 消耗：
      i=序号, id=消息ID, f=发件人, s=主题, sn=片段,
      d=日期, l=标签, u=未读, st=星标, im=重要, at=有附件
    """
    return {
        "i": index,
        "id": msg.message_id[:40] if msg.message_id else "",
        "f": (msg.from_addr or "")[:80],
        "s": (msg.subject or "")[:120],
        "sn": (msg.snippet or "")[:100],
        "d": (msg.internal_date or "")[:20],
        "l": (msg.label_ids or [])[:5],
        "u": msg.unread,
        "st": msg.starred,
        "im": msg.important,
        "at": msg.has_attachment,
    }


def build_phase1_user_prompt(
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
) -> str:
    """构建 Phase 1 的 user prompt，包含所有邮件的紧凑头信息。

    返回的 prompt 指引 LLM 对每封邮件输出分类和判断理由。
    """
    headers_json = json.dumps(
        [_compact_header(m, i) for i, m in enumerate(messages)],
        ensure_ascii=False,
    )

    allowed_kinds = strategy.candidate_policy.candidate_kinds
    kinds_hint = ", ".join(allowed_kinds)

    return f"""## Strategy
{strategy.name}: {strategy.description}
Allowed candidate kinds: {kinds_hint}
{strategy.candidate_policy.llm_candidate_hints}

## Mailbox Owner
{profile.mailbox_id} — match by EMAIL ADDRESS (between < >), not by display name.
- Sender IS the mailbox owner → OUTGOING. SENT: is_candidate=false. DRAFT: is_candidate=true, item_type=NeedsReply, reason="unsent draft".

## Headers ({len(messages)} emails)
Each header: i=index, id=message_id, f=from, s=subject, sn=snippet, d=date, l=labels, u=unread, st=starred, im=important, at=has_attachment.
{headers_json}

## Required Output
Return a JSON object whose FIRST character is `{{`. Shape:

{{"classifications": [
  {{{{
    "message_id": "<copy the exact 'id' from the input header>",
    "item_type": "SecurityRisk",
    "is_candidate": true,
    "priority_hint": "high",
    "read_depth": "message_detail",
    "confidence": 0.92,
    "reason": "Login alert from unknown Windows device in Shanghai — user must verify"
  }}}}
]}}

Every input header MUST have exactly one entry in the classifications array. Do NOT skip or add entries.
- message_id: copy the "id" field from the input header exactly — no extra spaces, no truncation.
- item_type: one of the Item Types listed in the system instructions.
- is_candidate: true if the email needs user attention, false otherwise.
- priority_hint: "high" | "medium" | "low"
- read_depth: "header_only" | "message_detail" | "thread_context"
- confidence: float between 0.0 and 1.0
- reason: one short English sentence explaining your classification."""


# ── 响应解析 ──────────────────────────────────────────────────────

def parse_phase1_response(
    payload: dict[str, Any],
    messages: list[MessageLite],
    strategy: MailStrategy,
) -> list[dict[str, Any]]:
    """解析 Phase 1 LLM 的 JSON 响应。

    返回分类字典列表，每个字典包含：
      message_id, is_candidate, candidate_kind, priority_hint,
      read_depth, confidence, reason, item_type, source

    安全规则：
    - LLM 输出的候选类型必须在策略允许的列表中
    - 不在允许列表中的类型会被跳过（除非是 Unknown 且 unsure 被允许）
    - LLM 未分类的邮件会以低优先级 fallback 添加到结果中
    """
    raw_classifications = payload.get("classifications") if isinstance(payload.get("classifications"), list) else []

    # 构建消息 ID → MessageLite 的查找表
    msg_by_id: dict[str, MessageLite] = {}
    for m in messages:
        if m.message_id:
            msg_by_id[m.message_id] = m

    allowed_kinds = set(strategy.candidate_policy.candidate_kinds)

    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in raw_classifications:
        if not isinstance(item, dict):
            continue

        msg_id = str(item.get("message_id") or "")
        if not msg_id or msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)

        is_candidate = bool(item.get("is_candidate", True))

        # 将 LLM 的 item_type 映射到 CandidateKind
        item_type = str(item.get("item_type") or "Unknown")
        llm_kind = _ITEM_TYPE_TO_KIND.get(item_type, "unsure")

        # 检查候选类型是否在策略允许的列表中
        if llm_kind not in allowed_kinds:
            # 只在 LLM 确实不确定且 unsure 被允许时才保留
            if item_type == "Unknown" and "unsure" in allowed_kinds:
                llm_kind = "unsure"
            else:
                continue

        # 非候选且非清理类 → 完全跳过
        if not is_candidate and llm_kind == "safe_cleanup_bundle":
            pass  # 清理类即使非候选也保留为低优先级
        elif not is_candidate:
            continue

        # 解析优先级
        llm_priority = str(item.get("priority_hint") or item.get("priority") or "medium")
        priority_hint = "medium"
        if llm_priority in ("high", "critical", "action_needed"):
            priority_hint = "high"
        elif llm_priority in ("medium", "agent_can_handle"):
            priority_hint = "medium"
        elif llm_priority in ("low", "fyi", "ignore_or_archive"):
            priority_hint = "low"

        # 解析读取深度（支持多种命名格式）
        llm_depth = str(item.get("read_depth") or "message_detail")
        read_depth: ReadDepth = "message_detail"
        if llm_depth in ("header_only", "message_detail", "thread_context", "batch_summary"):
            read_depth = llm_depth  # type: ignore[assignment]
        elif llm_depth == "HeaderOnly":
            read_depth = "header_only"
        elif llm_depth == "MessageDetail":
            read_depth = "message_detail"
        elif llm_depth == "ThreadContext":
            read_depth = "thread_context"

        # 解析置信度（浮点数，默认 0.7）
        try:
            confidence = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7

        results.append({
            "message_id": msg_id,
            "is_candidate": True,
            "candidate_kind": llm_kind,
            "priority_hint": priority_hint,
            "read_depth": read_depth,
            "confidence": confidence,
            "reason": str(item.get("reason") or "")[:200],
            "source": "llm",
            "item_type": item_type,
        })

    # 补充 LLM 未分类的邮件（标记为非候选、低优先级）
    for m in messages:
        if m.message_id and m.message_id not in seen_ids:
            safe_kind = "safe_cleanup_bundle" if "safe_cleanup_bundle" in allowed_kinds else "unsure"
            results.append({
                "message_id": m.message_id,
                "is_candidate": False,
                "candidate_kind": safe_kind,
                "priority_hint": "low",
                "read_depth": "header_only",
                "confidence": 0.3,
                "reason": "Not classified by LLM — default low priority",
                "source": "fallback",
                "item_type": "Unknown",
            })

    return results


# ── 与规则信号融合 ──────────────────────────────────────────────

def create_candidate_id(message_id: str) -> str:
    """生成候选唯一标识。"""
    import uuid
    return f"cand_{uuid.uuid4().hex[:12]}"


def classifications_to_candidates(
    classifications: list[dict[str, Any]],
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
) -> list[CandidateItem]:
    """将 Phase 1 分类结果转换为 CandidateItem 对象。

    融合 LLM 分类和规则信号检测：
    - LLM 提供 item_type 和 reason（存储到 evidence 中供 Phase 2 使用）
    - 规则检测提供 matched_signals（信号列表）
    - 置信度 = LLM 基础置信度 + 规则信号增强（每个强信号 +0.05，上限 0.95）
    """
    from .candidate import detect_signals

    msg_map: dict[str, MessageLite] = {m.message_id: m for m in messages if m.message_id}
    candidates: list[CandidateItem] = []

    for cls in classifications:
        msg_id = cls["message_id"]
        msg = msg_map.get(msg_id)
        if not msg:
            continue

        # 获取规则信号以丰富 evidence
        signals = detect_signals(msg, strategy, profile)

        kind: CandidateKind = cls["candidate_kind"]  # type: ignore[assignment]

        # 优先使用策略预设的读取深度，其次使用 LLM 建议的
        read_depth: ReadDepth = (
            strategy.context_policy.read_depth_by_candidate_kind.get(kind)
            or cls["read_depth"]
        )

        # 融合置信度：LLM 基础 + 规则信号增强
        confidence = cls["confidence"]
        if signals:
            strong_signals = {"security_keyword", "billing_keyword", "important", "starred", "human_reply"}
            boost = sum(0.05 for s in signals if s in strong_signals)
            confidence = min(0.95, confidence + boost)

        candidates.append(CandidateItem(
            candidate_id=create_candidate_id(msg_id),
            kind=kind,
            message_ids=[msg_id],
            thread_id=msg.thread_id,
            evidence={
                "from": msg.from_addr,
                "subject": msg.subject,
                "snippet": msg.snippet,
                "date": msg.internal_date,
                "labels": msg.label_ids,
                "matched_signals": signals,
                "llm_item_type": cls.get("item_type", ""),    # LLM 判断的类型
                "llm_reason": cls.get("reason", ""),          # LLM 判断的理由
            },
            priority_hint=cls["priority_hint"],  # type: ignore[arg-type]
            read_depth_required=read_depth,
            source="rule_llm_merged",             # 标记为规则+LLM 融合
            confidence=confidence,
        ))

    # 按线程去重（与规则路径保持一致）
    return _dedupe_by_thread(candidates)


def _dedupe_by_thread(candidates: list[CandidateItem]) -> list[CandidateItem]:
    """按线程去重。同一线程只保留置信度最高的候选。"""
    thread_map: dict[str, CandidateItem] = {}
    for c in candidates:
        tid = c.thread_id or c.message_ids[0]
        if tid not in thread_map or c.confidence > thread_map[tid].confidence:
            thread_map[tid] = c
    return list(thread_map.values())


# ── 主入口 ────────────────────────────────────────────────────────

async def _run_phase1_single_batch(
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
    sampling_create_message: Any,
    *,
    batch_index: int | None = None,
    batch_total: int | None = None,
) -> list[CandidateItem]:
    from .llm import call_llm_json_safe

    system_prompt = _PHASE1_SYSTEM
    user_prompt = build_phase1_user_prompt(messages, strategy, profile)
    strict_anna_sampling = sampling_create_message is not None

    metadata = {
        "tool": "phase1_batch_classify",
        "strategy_mode": strategy.id,
        "message_count": str(len(messages)),
    }
    if batch_index is not None and batch_total is not None:
        metadata["batch_index"] = str(batch_index)
        metadata["batch_total"] = str(batch_total)

    # 调用 LLM（DashScope 或 Anna sampling）。Anna 路径保持严格失败，不静默回退 DashScope。
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=system_prompt,
        user_message=user_prompt,
        fallback={"classifications": []},
        temperature=0.1,
        max_tokens=2048,
        timeout=240.0,
        metadata=metadata,
        allow_fallback=not strict_anna_sampling,
        allow_sampling_provider_fallback=not strict_anna_sampling,
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    # LLM 失败时 fallback 到纯规则生成；Anna strict 路径不会走到这里，会直接抛错。
    if not payload or result.get("fallback_used"):
        from .candidate import generate_candidates
        return generate_candidates(messages, strategy, profile)

    classifications = parse_phase1_response(payload, messages, strategy)
    return classifications_to_candidates(classifications, messages, strategy, profile)


async def run_phase1_batch_classify(
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
    sampling_create_message: Any = None,
) -> list[CandidateItem]:
    """执行 Phase 1 的 LLM 批量分类。

    DashScope 路径保持单批调用；Anna sampling 路径按小批调用，
    解析响应并返回融合了规则信号的 CandidateItem 列表。

    参数：
        messages: 扫描得到的邮件摘要列表
        strategy: 当前策略（决定哪些候选类型被允许）
        profile: 邮箱画像
        sampling_create_message: Anna sampling 函数（为 None 时走 DashScope）

    返回：
        CandidateItem 列表
    """
    if not messages:
        return []

    if sampling_create_message is None:
        return await _run_phase1_single_batch(messages, strategy, profile, sampling_create_message)

    batches = [
        messages[index:index + _ANNA_PHASE1_BATCH_SIZE]
        for index in range(0, len(messages), _ANNA_PHASE1_BATCH_SIZE)
    ]
    candidates: list[CandidateItem] = []
    for batch_index, batch in enumerate(batches, start=1):
        candidates.extend(await _run_phase1_single_batch(
            batch,
            strategy,
            profile,
            sampling_create_message,
            batch_index=batch_index,
            batch_total=len(batches),
        ))

    # 分批后再做一次线程级去重，保持与原单批输出语义一致。
    return _dedupe_by_thread(candidates)
