"""规则守护模块。

在 LLM 评估之后、生成行动计划之前，对 JudgmentResult 进行安全策略审查。
确保即使 LLM 输出了被禁止的动作，也会被拦截和替换。

设计文档 §15。

三条守护规则：
  1. 禁止动作替换：将 forbidden_actions 中的动作替换为 do_nothing
  2. 审批标记：对 require_approval_actions 中的动作强制标记 requires_approval=True
  3. 高风险升级：将 risk_level 为 critical/high 的项强制提升为最高优先级展示
"""

from __future__ import annotations

from .types import JudgmentResult, MailStrategy


def apply_rule_guards(result: JudgmentResult, strategy: MailStrategy) -> JudgmentResult:
    """对 LLM 评估结果应用安全策略守护。

    此函数确保：
    - 永远不会执行被禁止的动作（send_email / delete_email / unsubscribe）
    - 需要审批的动作被正确标记
    - 高风险项不会被降级

    参数：
        result: LLM 返回的评估结果
        strategy: 当前策略（提供 action_policy）

    返回：
        经过安全审查后的评估结果（原地修改）
    """
    ap = strategy.action_policy

    # 1. 遍历推荐动作列表，应用动作策略
    cleaned_actions: list[dict] = []
    for action in result.final_decision.recommended_actions:
        action_type = str(action.get("action_type") or "do_nothing")

        # 1a. 被禁止的动作 → 替换为 do_nothing
        if action_type in ap.forbidden_actions:
            cleaned_actions.append({
                "action_type": "do_nothing",
                "risk_level": "low",
                "requires_approval": False,
                "payload": {},
                "reason": f"Action '{action_type}' removed by policy guard. 原建议：{action.get('reason', '')}",
            })
            continue

        # 1b. 不在允许列表中的动作 → 静默跳过
        if action_type not in ap.allowed_suggested_actions:
            continue

        # 1c. 需要审批的动作 → 强制标记 requires_approval=True
        if action_type in ap.require_approval_actions:
            action["requires_approval"] = True

        cleaned_actions.append(action)

    result.final_decision.recommended_actions = cleaned_actions

    # 2. 高风险升级：critical/high 风险项强制设为最高优先级展示
    risk = result.base_judgment.risk_level
    if risk in ("critical", "high"):
        result.final_decision.priority = "critical"
        result.final_decision.should_show_in_main_result = True
        result.final_decision.should_show_in_lower_priority = False

    return result
