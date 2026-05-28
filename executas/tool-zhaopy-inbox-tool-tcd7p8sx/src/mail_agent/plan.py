"""行动计划生成器。

将 JudgmentResult 列表聚合成面向用户的 ActionPlan。
MVP 版本使用模板聚合方式（不用额外 LLM 调用），第二版可以改用 LLM 优化摘要文案。

设计文档 §16。
"""

from __future__ import annotations

import uuid
from typing import Any

from .types import (
    ActionPlan,
    DisplayItem,
    JudgmentResult,
    MailTaskPlan,
    StrategyMode,
)


def create_run_id() -> str:
    """生成唯一的运行标识。前缀 run_ + 12 位随机 hex。"""
    return f"run_{uuid.uuid4().hex[:12]}"


def _build_plan_title(mode: StrategyMode) -> str:
    """根据策略模式生成简报标题（中文）。"""
    titles = {
        "default_secretary": "收件箱简报 · 秘书模式",
        "creator_opportunity": "合作机会分析 · BD模式",
        "security_billing": "安全与账单检查 · 风控模式",
    }
    return titles.get(mode, "邮件处理建议")


def _build_plan_summary(mode: StrategyMode, judgments: list[JudgmentResult]) -> str:
    """生成简报摘要文本。

    统计维度：总数、主关注项数、紧急项数、高优项数、低优项数。
    """
    total = len(judgments)
    main = sum(1 for j in judgments if j.final_decision.should_show_in_main_result)
    lower = sum(1 for j in judgments if j.final_decision.should_show_in_lower_priority)
    critical = sum(1 for j in judgments if j.final_decision.priority == "critical")
    high = sum(1 for j in judgments if j.final_decision.priority == "high")
    return (
        f"共评估 {total} 个候选事项。"
        f"需要关注 {main} 项，其中紧急 {critical} 项、高优先级 {high} 项。"
        f"低优先级 {lower} 项。"
    )


def _to_display_item(j: JudgmentResult, idx: int) -> DisplayItem:
    """将单个 JudgmentResult 转换为前端展示用的 DisplayItem。

    DisplayItem 包含卡片三行信息（title/summary/recommendation），
    是 ActionPlan 中 main_items 和 lower_priority_items 的基本单元。
    """
    fd = j.final_decision
    return DisplayItem(
        candidate_id=j.candidate_id,
        title=fd.user_facing_summary or j.base_judgment.item_type,
        bucket=fd.display_bucket,
        priority=fd.priority,
        summary=fd.user_facing_summary,
        reason=fd.user_facing_reason,
        recommendation=fd.user_facing_recommendation,
        # 为每个建议动作生成一个 action_id（用于前端跟踪）
        action_ids=[f"act_{idx}_{i}" for i in range(len(fd.recommended_actions))],
    )


def _build_approval_memo(actions: list[dict[str, Any]]) -> dict[str, list[str]]:
    """构建审批备忘。

    将建议动作分为三类：
      safe_to_prepare: 不需要审批的，Agent 可以安全准备
      needs_approval: 需要用户审批的
      blocked_by_policy: 高风险但被策略阻止的
    """
    safe: list[str] = []
    needs_approval: list[str] = []
    blocked: list[str] = []

    for action in actions:
        at = action.get("action_type", "do_nothing")
        reason = str(action.get("reason", ""))[:80]
        if action.get("requires_approval"):
            needs_approval.append(f"{at}: {reason}")
        elif action.get("risk_level") == "high":
            blocked.append(f"{at}: {reason}")
        else:
            safe.append(f"{at}: {reason}")

    return {
        "safe_to_prepare": safe,
        "needs_approval": needs_approval,
        "blocked_by_policy": blocked,
    }


def generate_action_plan(
    run_id: str,
    task_plan: MailTaskPlan,
    judgments: list[JudgmentResult],
) -> ActionPlan:
    """从评估结果生成行动计划。

    处理流程：
    1. 按 should_show_in_main_result 将评估结果分为"主关注"和"低优先"两组
    2. 收集所有建议动作并标记候选归属
    3. 按优先级排序（critical > high > medium > low > ignore）
    4. 构建审批备忘

    参数：
        run_id: 本次运行的唯一标识
        task_plan: 任务计划
        judgments: 所有候选项的评估结果列表

    返回：
        ActionPlan，包含主关注事项、低优先事项、建议动作和审批备忘
    """
    # 分组：主关注 vs 低优先
    main_judgments = [j for j in judgments if j.final_decision.should_show_in_main_result]
    lower_judgments = [j for j in judgments if j.final_decision.should_show_in_lower_priority]

    # 收集所有建议动作并标注候选归属
    all_actions: list[dict[str, Any]] = []
    for j in judgments:
        for action in j.final_decision.recommended_actions:
            action_with_candidate = dict(action)
            action_with_candidate["candidate_id"] = j.candidate_id
            all_actions.append(action_with_candidate)

    # 按优先级排序：critical > high > medium > low > ignore
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "ignore": 4}
    main_judgments.sort(key=lambda j: priority_order.get(j.final_decision.priority, 99))
    lower_judgments.sort(key=lambda j: priority_order.get(j.final_decision.priority, 99))

    return ActionPlan(
        run_id=run_id,
        strategy_mode=task_plan.strategy_mode,
        title=_build_plan_title(task_plan.strategy_mode),
        summary=_build_plan_summary(task_plan.strategy_mode, judgments),
        main_items=[_to_display_item(j, i) for i, j in enumerate(main_judgments)],
        lower_priority_items=[_to_display_item(j, i) for i, j in enumerate(lower_judgments)],
        proposed_actions=all_actions,
        approval_memo=_build_approval_memo(all_actions),
    )
