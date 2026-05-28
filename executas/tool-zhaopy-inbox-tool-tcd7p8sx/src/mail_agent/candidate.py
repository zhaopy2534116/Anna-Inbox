"""候选生成模块。

从 MessageLite 列表中筛选出可能需要关注的候选项（CandidateItem）。
包含三个核心步骤：
  1. detect_signals() — 信号检测，从邮件元数据和内容中提取信号
  2. classify_candidate_kind_by_rule() — 基于规则的分类，将信号映射到候选类型
  3. generate_candidates() — 主入口，批量处理消息列表并去重

设计文档 §11。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from .types import (
    CandidateItem,
    CandidateKind,
    MailStrategy,
    MailboxProfile,
    MessageLite,
    ReadDepth,
)


def create_candidate_id(message_id: str) -> str:
    """生成候选唯一标识。前缀 cand_ + 12 位随机 hex。"""
    return f"cand_{uuid.uuid4().hex[:12]}"


# ── 信号检测 ──────────────────────────────────────────────────────
# detect_signals 是候选生成的第一步，从邮件中提取结构化信号，
# 供后续的规则分类和 LLM 评估使用。

def detect_signals(
    msg: MessageLite,
    strategy: MailStrategy,
    profile: MailboxProfile,
) -> list[str]:
    """从一封邮件中检测信号。

    信号来源包括：
    - Gmail 元数据信号（unread, starred, important, has_attachment）
    - 关键词匹配信号（安全/账单/合作/请求相关的中英文关键词）
    - 低价值信号（newsletter/promotion/digest 关键词，退订头）
    - 已知联系人信号（与邮箱画像中的重要联系人匹配）

    返回信号字符串列表，如 ["unread", "security_keyword", "important"]。
    """
    # 拼接发件人、主题和片段形成搜索文本
    text = f"{msg.from_addr} {msg.subject} {msg.snippet}".lower()

    signals: list[str] = []

    # --- Gmail 元数据信号 ---
    if msg.unread:
        signals.append("unread")
    if msg.starred:
        signals.append("starred")
    if msg.important:
        signals.append("important")
    if msg.has_attachment:
        signals.append("has_attachment")

    # --- 安全/登录关键词 ---
    if re.search(r"(security|login|verification|password|account|登录|安全|验证|密码|账号)", text, re.IGNORECASE):
        signals.append("security_keyword")

    # --- 账单/付款关键词 ---
    if re.search(r"(billing|invoice|payment|receipt|subscription|renewal|quota|storage|账单|付款|发票|订阅|续费)", text, re.IGNORECASE):
        signals.append("billing_keyword")

    # --- 合作/创作者关键词 ---
    if re.search(r"(collaboration|partnership|sponsor|creator|youtube|video|proposal|demo|interested|合作|提案)", text, re.IGNORECASE):
        signals.append("collaboration_keyword")

    # --- 请求/问题信号 ---
    if re.search(r"(can you|could you|please|question|thoughts|let me know|waiting|follow up|请|能否|麻烦|帮忙)", text, re.IGNORECASE):
        signals.append("possible_request")

    # --- 低价值/批量信号 ---
    if re.search(r"(newsletter|digest|promotion|sale|webinar|event|unsubscribe|退订)", text, re.IGNORECASE):
        signals.append("low_value_bulk_possible")

    # --- 退订头 ---
    if msg.headers.get("list_unsubscribe"):
        signals.append("list_unsubscribe")

    # --- 人类回复检测 ---
    if re.search(r"\bRe:\b|\bFwd:\b|\b回复：\b|\b转发：\b", msg.subject, re.IGNORECASE):
        signals.append("human_reply")

    # --- 已知联系人匹配 ---
    from_lower = msg.from_addr.lower()
    for contact in profile.important_contacts:
        if contact.lower() in from_lower:
            signals.append("known_contact")
            break

    return signals


# ── 候选分类 ──────────────────────────────────────────────────────

# 每个 CandidateKind 对应的关键词字典，用于规则式分类
# 关键词同时匹配信号列表和邮件文本
_KIND_KEYWORDS: dict[CandidateKind, list[str]] = {
    "security_risk_possible": [
        "security_keyword", "login", "verification", "password",
        "suspicious", "安全", "验证码", "密码",
    ],
    "billing_issue_possible": [
        "billing_keyword", "invoice", "payment", "receipt",
        "subscription", "账单", "付款", "发票", "订阅",
    ],
    "account_notice_possible": [
        "account", "quota", "storage", "renewal", "容量",
    ],
    "creator_thread_possible": [
        "collaboration_keyword", "creator", "youtube",
        "partnership", "sponsor", "提案",
    ],
    "business_thread_possible": [
        "collaboration", "partner", "demo", "proposal",
        "合作", "演示",
    ],
    "reply_required_possible": [
        "possible_request", "human_reply", "waiting", "follow up",
    ],
    "confirmation_required_possible": [
        "verification", "confirm", "确认", "验证",
    ],
    "safe_cleanup_bundle": [
        "low_value_bulk_possible", "newsletter", "promotion",
    ],
}


def classify_candidate_kind_by_rule(
    signals: list[str],
    strategy: MailStrategy,
    msg: MessageLite,
) -> CandidateKind | None:
    """基于规则将邮件分类到候选类型。

    分类逻辑：
    1. 排除检查：纯低价值/退订信号 → 不进入候选（返回 None）
    2. 增强信号检查：有安全/账单/合作/重要/星标/请求/回复等高关注信号
    3. 关键词匹配：对每个候选类型计算匹配分数
    4. 分数 >= 2 或有增强信号 → 返回该类型
    5. 有增强信号但不匹配任何类型 → 返回 "unsure"（不确定）

    返回 None 表示该邮件不应成为候选项。
    """
    text = f"{msg.from_addr} {msg.subject} {msg.snippet}".lower()

    # 排除：纯低价值或纯退订信号 → 跳过
    if signals == ["low_value_bulk_possible"] or signals == ["list_unsubscribe"]:
        return None
    if set(signals).issubset({"low_value_bulk_possible", "list_unsubscribe", "unread"}):
        return None

    # 检查是否有增强信号（高关注度信号）
    has_promoted = any(s in signals for s in [
        "security_keyword", "billing_keyword", "collaboration_keyword",
        "important", "starred", "possible_request", "human_reply",
        "known_contact",
    ])

    # 对策略允许的每个类型计算匹配分数
    best_kind: CandidateKind | None = None
    best_score = 0

    for kind, keywords in _KIND_KEYWORDS.items():
        # 只考虑当前策略允许的候选类型
        if kind not in strategy.candidate_policy.candidate_kinds:
            continue

        # 分数 = 文本中关键词出现次数 * 2 + 信号列表中匹配次数 * 1
        score = 0
        for kw in keywords:
            if kw in text:
                score += 2
        for signal in signals:
            if signal in keywords or signal.replace("_", " ") in " ".join(keywords):
                score += 1

        if score > best_score:
            best_score = score
            best_kind = kind

    # 分数 >= 2 或有增强信号 → 返回最佳匹配类型
    if best_kind and (best_score >= 2 or has_promoted):
        return best_kind
    # 有增强信号但没匹配到任何类型 → 不确定
    if has_promoted and best_score == 0:
        return "unsure"

    return None


def infer_priority_hint(signals: list[str]) -> str:
    """从信号推断优先级提示。

    高优先级信号：安全、重要、星标、账单
    中优先级信号：请求、人类回复、合作、已知联系人
    低优先级：其余信号
    无信号：unknown
    """
    high = {"security_keyword", "important", "starred", "billing_keyword"}
    medium = {"possible_request", "human_reply", "collaboration_keyword", "known_contact"}
    if signals and set(signals) & high:
        return "high"
    if signals and set(signals) & medium:
        return "medium"
    if signals:
        return "low"
    return "unknown"


def infer_rule_confidence(signals: list[str]) -> float:
    """从信号强度推断规则置信度。

    强信号：安全/账单关键词、重要、星标、人类回复
    基础置信度 0.4 + 强信号数 * 0.15，上限 0.9
    """
    if not signals:
        return 0.3
    strong = {"security_keyword", "billing_keyword", "important", "starred", "human_reply"}
    score = sum(1 for s in signals if s in strong)
    return min(0.9, 0.4 + score * 0.15)


# ── 主候选生成器 ─────────────────────────────────────────────────

def generate_candidates(
    messages: list[MessageLite],
    strategy: MailStrategy,
    profile: MailboxProfile,
) -> list[CandidateItem]:
    """从 MessageLite 列表生成 CandidateItem 列表。

    对每封邮件：
    1. detect_signals → 信号列表
    2. classify_candidate_kind_by_rule → 候选类型（返回 None 则跳过）
    3. 根据 context_policy 确定需要的读取深度
    4. 创建 CandidateItem 并组装证据

    最后按线程去重：同一线程只保留置信度最高的候选。
    """
    candidates: list[CandidateItem] = []

    for msg in messages:
        if not msg.message_id:
            continue

        signals = detect_signals(msg, strategy, profile)
        kind = classify_candidate_kind_by_rule(signals, strategy, msg)
        if not kind:
            continue

        # 根据策略的上下文策略确定需要的读取深度
        read_depth: ReadDepth = (
            strategy.context_policy.read_depth_by_candidate_kind.get(kind)
            or "message_detail"
        )

        candidates.append(CandidateItem(
            candidate_id=create_candidate_id(msg.message_id),
            kind=kind,
            message_ids=[msg.message_id],
            thread_id=msg.thread_id,
            evidence={
                "from": msg.from_addr,
                "subject": msg.subject,
                "snippet": msg.snippet,
                "date": msg.internal_date,
                "labels": msg.label_ids,
                "matched_signals": signals,
            },
            priority_hint=infer_priority_hint(signals),
            read_depth_required=read_depth,
            source="rule",            # 标记为规则生成（区别于 LLM 生成的候选）
            confidence=infer_rule_confidence(signals),
        ))

    # 按线程去重：同一线程只保留置信度最高的候选
    return _dedupe_by_thread(candidates)


def _dedupe_by_thread(candidates: list[CandidateItem]) -> list[CandidateItem]:
    """按线程去重。同一 thread_id 下只保留置信度最高的候选。

    如果没有 thread_id，使用第一个 message_id 作为临时 thread_id。
    """
    thread_map: dict[str, CandidateItem] = {}
    for c in candidates:
        tid = c.thread_id or c.message_ids[0]
        if tid not in thread_map or c.confidence > thread_map[tid].confidence:
            thread_map[tid] = c
    return list(thread_map.values())
