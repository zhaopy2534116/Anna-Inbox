"""定制扫描计划生成器。

从用户的自然语言请求生成 CustomScanPlan，使用 LLM 做语义理解和规划。
与 strategies.py 完全解耦——plan 是自包含的执行计划。

Ask 走 agent 范式：搜邮件 → 按需读 → 一次 LLM → 答案。
Planner 输出 task_prompt，由执行 LLM 直接完成分析和输出。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .types import CustomScanPlan

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_PLANNER_SYSTEM_PROMPT = """You are Anna's scan planner. Given a natural-language email assistant request, produce a complete execution plan. Output a single valid JSON object. The very first character you write MUST be `{`. Do NOT write any text before or after the JSON — no markdown fences, no explanation, no commentary. If you output invalid JSON the result will be discarded.

CRITICAL: Use single quotes for ALL text inside JSON string values (e.g. "don't" not "don't"). If you use double quotes inside a JSON string, the JSON will be unparseable.

## Output format
{
  "title": "Short English title summarizing the scan goal (<=12 words)",
  "description": "One sentence describing what this scan will do",
  "gmail_queries": [
    {
      "query": "Gmail search syntax",
      "purpose": "Why this query",
      "max_results": 50,
      "priority": "high"
    }
  ],
  "read_depth": "header_only",
  "task_prompt": "Analysis instructions for the execution LLM. What to look for, how to organize findings. Do NOT include JSON output format — the execution system has a fixed output schema."
}

## read_depth — choose based on what the user needs

- "header_only": The LLM only needs sender / subject / date / snippet / labels.
  Use when: finding unsubscribe candidates, checking email frequency, counting senders.
- "message_detail": The LLM needs the full body of each relevant email.
  Use when: judging whether a reply is needed, drafting a reply, checking if an email is actionable.
- "thread_context": The LLM needs the ENTIRE thread history (all messages in the conversation).
  Use when: summarizing a conversation, understanding relationship history, catching up on a discussion.

## task_prompt — write this as if you are instructing a smart assistant

The execution LLM will receive:
  - The user's original request
  - A list of emails (with the read depth you chose)
  - This task_prompt
  - A fixed output JSON schema (title / summary / sections with items)

Your task_prompt MUST tell the LLM:
1. What to look for in the emails
2. How to group and organize findings into sections
3. What kind of items to surface (people, threads, action items, etc.)

Do NOT include JSON output format in task_prompt — the execution system already has a fixed schema. Focus on analysis instructions only.

## Gmail search syntax reference
- is:unread / is:starred
- category:primary / category:promotions / category:social / category:updates / category:forums
- from:someone@example.com / to:someone@example.com
- subject:keyword
- newer_than:1d / newer_than:3d / newer_than:7d / newer_than:30d / newer_than:90d
- has:attachment
- label:labelname
- in:inbox / in:sent / in:spam
- -in:sent (exclude sent)
- OR / AND to combine conditions

## Direction guidance — choose the right scope for each query

The mailbox owner is provided in the user request. Match by EMAIL ADDRESS.

- User asks "check my inbox" / "what needs attention" / "pending" → use **in:inbox** or **-in:sent -in:draft** (exclude outgoing)
- User asks "what did I send" / "my outreach" / "proposals I sent" → use **in:sent**
- User asks to see full conversations regardless of direction → **no direction filter**
- User asks about collaboration / partnership threads → use **-in:sent -in:draft** (keep everything except own outgoing)
- Default (unclear intent): use **-in:sent -in:draft** — show everything except the user's own sent mail and drafts, since those were already handled by the user.

## Notes
- gmail_queries: 1-3 entries. Prefer precise Gmail operators.
- read_depth: "header_only" unless the task requires reading body text or thread history.
- max_results per query: 30-100 depending on how many emails you expect.
- CRITICAL: When searching for a person, use PARTIAL name matching (e.g. "from:christopher OR to:christopher") — NEVER guess full email addresses. You don't know their email domain.
- For unsubscribe candidates: category:promotions unsubscribe.
- For unread: is:unread.
- For summarising a conversation: use thread_context so the LLM sees all messages.
"""

_PLANNER_USER_TEMPLATE = """Mailbox owner: {mailbox}
User request: {user_request}"""

_FALLBACK_PLAN_JSON = {
    "title": "Inbox check",
    "description": "Scan recent primary emails",
    "gmail_queries": [
        {"query": "category:primary -in:sent newer_than:2d", "purpose": "recent_primary", "max_results": 50, "priority": "high"}
    ],
    "read_depth": "message_detail",
    "task_prompt": "Review the emails below. Identify any that need user attention — replies, decisions, security alerts, or billing issues. Group them into sections by action type (e.g. 'Needs reply', 'Needs review', 'For information'). For each item note the sender, subject, what it is about, and what the user should do.",
}


def _fallback_plan_for_request(user_request: str) -> dict[str, Any]:
    # 中文注释：Anna planner 偶发空响应时，用规则计划兜底，保证 Ask 链路还能继续执行。
    request = str(user_request or "").strip()
    lowered = request.lower()
    is_count = any(term in lowered for term in ("how many", "count", "number of", "多少", "几个"))
    asks_unread = any(term in lowered for term in ("unread", "未读"))
    asks_job_seekers = any(term in lowered for term in ("job seeker", "candidate", "resume", "cv", "applicant", "求职", "简历", "候选人"))

    if asks_job_seekers:
        base = "is:unread " if asks_unread else ""
        queries = [
            {
                "query": f"{base}(resume OR CV OR application OR candidate OR applicant OR interview OR 求职 OR 简历) newer_than:180d",
                "purpose": "Find unread job-seeker or candidate-related mail by common recruiting keywords",
                "max_results": 80,
                "priority": "high",
            }
        ]
        task_prompt = (
            "Count emails from job seekers or candidates. Treat resumes, CVs, applications, interview requests, "
            "and candidate introductions as job-seeker mail. Report the total count and list the matching senders/subjects."
        )
        scan_budget = {"max_messages": 80, "max_threads": 80, "max_detail_reads": 0, "max_thread_reads": 0}
        read_depth = "header_only" if is_count else "message_detail"
    else:
        stopwords = {
            "help", "me", "check", "how", "many", "what", "which", "from", "are",
            "the", "a", "an", "my", "please", "email", "emails", "mail", "mails",
        }
        keywords = [
            token.strip("\"'()[]{}.,:;!?，。！？、").lower()
            for token in request.replace("/", " ").replace("\\", " ").split()
        ]
        keywords = [token for token in keywords if 2 <= len(token) <= 32 and token not in stopwords][:4]
        query = f"{' '.join(keywords)} newer_than:90d" if keywords else "category:primary -in:sent newer_than:30d"
        queries = [
            {
                "query": query,
                "purpose": "Rule-based fallback query when Anna planning returns no usable JSON",
                "max_results": 50,
                "priority": "high",
            }
        ]
        task_prompt = (
            "Answer the user's request using only the emails provided. "
            "Surface concrete threads, people, dates, and recommended next actions."
        )
        scan_budget = {"max_messages": 50, "max_threads": 50, "max_detail_reads": 25, "max_thread_reads": 10}
        read_depth = "message_detail"

    fallback = dict(_FALLBACK_PLAN_JSON)
    fallback["title"] = "Custom inbox scan"
    fallback["description"] = "Run a broad local scan based on the user's request."
    fallback["gmail_queries"] = queries
    fallback["scan_budget"] = scan_budget
    fallback["read_depth"] = read_depth
    fallback["task_prompt"] = task_prompt
    return fallback


async def generate_custom_plan(
    user_request: str,
    mailbox: str = "",
    sampling_create_message: Any = None,
) -> CustomScanPlan:
    """从自然语言请求生成定制扫描计划。

    Planner LLM 输出 gmail_queries + read_depth + task_prompt，
    执行 LLM 根据 task_prompt 直接完成分析和输出。
    """
    from .llm import call_llm_json_safe

    user_message = _PLANNER_USER_TEMPLATE.format(
        user_request=user_request,
        mailbox=mailbox or "unknown",
    )

    strict_anna_sampling = sampling_create_message is not None
    fallback_plan = _fallback_plan_for_request(user_request)
    result = await call_llm_json_safe(
        sampling_create_message,
        system_prompt=_PLANNER_SYSTEM_PROMPT,
        user_message=user_message,
        fallback=fallback_plan,
        temperature=0.1,
        max_tokens=2048,
        timeout=90.0,
        metadata={
            "tool": "planner_generate_custom_plan",
        },
        allow_fallback=True,
        allow_sampling_provider_fallback=not strict_anna_sampling,
        max_attempts=3 if strict_anna_sampling else None,
    )

    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload:
        payload = fallback_plan

    plan_id = f"cplan_{uuid.uuid4().hex[:12]}"
    now = datetime.now(BEIJING_TZ).isoformat()

    plan = CustomScanPlan(
        plan_id=plan_id,
        user_request=user_request,
        title=str(payload.get("title") or ""),
        description=str(payload.get("description") or ""),
        gmail_queries=_parse_queries(payload.get("gmail_queries")),
        scan_budget=_parse_budget(payload.get("scan_budget")),
        read_depth=str(payload.get("read_depth") or "message_detail"),
        task_prompt=str(payload.get("task_prompt") or ""),
        created_at=now,
        last_used_at=now,
        use_count=1,
    )
    plan.llm_meta = {
        "provider": result.get("provider"),
        "model": result.get("model"),
        "usage": result.get("usage"),
        "fallback_used": result.get("fallback_used", False),
        "fallback_reason": result.get("fallback_reason", ""),
    }
    return plan


def _parse_queries(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return _FALLBACK_PLAN_JSON["gmail_queries"]  # type: ignore[return-value]
    result: list[dict[str, Any]] = []
    for q in raw:
        if isinstance(q, dict):
            result.append({
                "query": str(q.get("query") or ""),
                "purpose": str(q.get("purpose") or ""),
                "max_results": max(1, min(int(q.get("max_results") or 50), 200)),
                "priority": str(q.get("priority") or "high"),
            })
    return result or _FALLBACK_PLAN_JSON["gmail_queries"]  # type: ignore[return-value]


def _parse_budget(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return dict(_FALLBACK_PLAN_JSON.get("scan_budget", {"max_messages": 200}))
    return {
        "max_messages": max(10, int(raw.get("max_messages") or 200)),
        "max_threads": max(10, int(raw.get("max_threads") or 100)),
        "max_detail_reads": max(5, int(raw.get("max_detail_reads") or 50)),
        "max_thread_reads": max(5, int(raw.get("max_thread_reads") or 20)),
    }
