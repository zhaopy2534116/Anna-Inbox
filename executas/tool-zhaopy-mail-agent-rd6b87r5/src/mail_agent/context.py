"""上下文读取模块。

根据候选所需的读取深度（read_depth_required），从邮件适配器中获取对应粒度的数据：
  - header_only: 不需要额外读取（只使用候选本身的信息）
  - message_detail: 获取单封邮件的全文（body_text）
  - thread_context: 获取整个线程的消息历史（最多10封）
  - batch_summary: 获取批量消息的摘要（预留）

所有数据已在扫描阶段缓存到本地，此模块不会产生额外的 Gmail API 调用。
设计文档 §12。
"""

from __future__ import annotations

from .mail_adapter import get_message_detail, get_thread_context
from .types import CandidateContext, CandidateItem


async def read_candidate_context(
    mailbox: str,
    candidate: CandidateItem,
) -> CandidateContext:
    """根据候选的 read_depth_required 获取对应的上下文数据。

    参数：
        mailbox: 邮箱地址
        candidate: 需要获取上下文的候选项

    返回：
        CandidateContext，type 字段标记了上下文的粒度。
        如果需要的消息不存在或读取失败，会降级返回 header_only。
    """
    depth = candidate.read_depth_required

    # header_only: 不需要额外数据，直接返回候选本身
    if depth == "header_only":
        return CandidateContext(type="header_only", candidate=candidate)

    # message_detail: 从本地缓存读取单封邮件的全文
    if depth == "message_detail":
        msg_id = candidate.message_ids[0] if candidate.message_ids else ""
        if not msg_id:
            return CandidateContext(type="header_only", candidate=candidate)
        detail = get_message_detail(mailbox, msg_id)
        return CandidateContext(type="message_detail", candidate=candidate, message=detail)

    # thread_context: 从本地缓存读取完整线程消息历史
    if depth == "thread_context" and candidate.thread_id:
        thread = get_thread_context(mailbox, candidate.thread_id, max_messages=10)
        return CandidateContext(type="thread_context", candidate=candidate, thread=thread)

    # Fallback: batch_summary 或 缺少 thread_id 时降级为 header_only
    return CandidateContext(type="header_only", candidate=candidate)
