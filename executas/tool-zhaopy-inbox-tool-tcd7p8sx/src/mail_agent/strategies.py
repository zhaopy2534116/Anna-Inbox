"""三种策略模式配置及策略注册表。

完全遵循设计文档 §5–§6 的定义：
- default_secretary: 默认秘书模式 — 找出需要用户注意/回复/确认的邮件
- creator_opportunity: 合作机会模式 — 找出创作者/BD/合作伙伴相关的合作机会
- security_billing: 安全账单模式 — 检查账号安全、账单异常和订阅状态

每种策略包含五个子策略：
  ScanPolicy → CandidatePolicy → ContextPolicy → JudgmentPolicy → ActionPolicy
  分别控制扫描范围、候选筛选、读取深度、评估标准和允许动作。

使用方式：
  from .strategies import get as get_strategy
  strategy = get_strategy("default_secretary")
"""

from __future__ import annotations

from .types import (
    ActionPolicy,
    CandidatePolicy,
    ContextPolicy,
    JudgmentPolicy,
    MailStrategy,
    ScanPolicy,
    StrategyMode,
)

# ── 策略注册表 ──────────────────────────────────────────────────────
# 简单的内存字典，所有策略通过 register() 函数注册后，
# 通过 get(mode) 按 StrategyMode 查询。

_registry: dict[StrategyMode, MailStrategy] = {}


def register(strategy: MailStrategy) -> MailStrategy:
    """将策略注册到全局注册表。返回策略本身，便于链式赋值。"""
    _registry[strategy.id] = strategy
    return strategy


def get(mode: StrategyMode) -> MailStrategy | None:
    """按模式标识查询已注册的策略。未找到返回 None。"""
    return _registry.get(mode)


def all_strategies() -> dict[StrategyMode, MailStrategy]:
    """返回所有已注册策略的副本。"""
    return dict(_registry)


# ═══════════════════════════════════════════════════════════════════════
#  6.1  Default 秘书模式
# ═══════════════════════════════════════════════════════════════════════
# 目标：找出当前邮箱中真正需要用户注意、回复、确认或处理的事项，
#       同时把普通通知、newsletter、促销、重复 digest 降级。

default_secretary = register(MailStrategy(
    id="default_secretary",
    name="Default 秘书模式",
    description="找出真正需要用户注意的邮件事项。",

    # ── 扫描策略 ──────────────────────────────────────────────────
    # 最近3天的高优先查询 + 最近14天的线程 + 已加星标/重要的邮件 + 风险信号
    scan_policy=ScanPolicy(
        default_time_range="last_72_hours_plus_recent_open_threads",
        default_queries=[
            # 收件箱全部 + 星标/重要合并为一条查询（减少 Gmail API 调用）
            {"query": "in:inbox (newer_than:3d OR is:important OR is:starred)", "purpose": "all_inbox_and_flagged", "max_results": 100, "priority": "high"},
            # 草稿（提醒发送）
            {"query": "in:draft newer_than:7d", "purpose": "unsent_drafts", "max_results": 20, "priority": "medium"},
        ],
        # Gmail 标签权重：INBOX/IMPORTANT/STARRED 权重高，更新类中，社交/促销低
        label_weights={
            "INBOX": "high", "IMPORTANT": "high", "STARRED": "high",
            "CATEGORY_UPDATES": "medium", "CATEGORY_SOCIAL": "low", "CATEGORY_PROMOTIONS": "low",
        },
        budget={"max_messages": 250, "max_threads": 120, "max_detail_reads": 40, "max_thread_reads": 25},
    ),

    # ── 候选策略 ──────────────────────────────────────────────────
    # 关注人类回复、已知联系人、问题请求、安全/账单风险
    # 排除纯新闻通讯、促销、自动通知
    candidate_policy=CandidatePolicy(
        include_signals=[
            "human reply", "known contact", "question or request",
            "security alert", "billing issue", "customer or partner",
            "attachment from human sender",
        ],
        exclude_signals=[
            "generic newsletter", "promotion", "platform digest",
            "automated notification without user action",
        ],
        promote_signals=[
            "important label", "starred", "other party waiting",
            "deadline", "risk-related keyword",
        ],
        candidate_kinds=[
            "reply_required_possible", "confirmation_required_possible",
            "security_risk_possible", "billing_issue_possible",
            "business_thread_possible", "safe_cleanup_bundle", "unsure",
        ],
        llm_candidate_hints="判断是否是需要用户注意的邮件事项，而不是普通邮件列表。",
    ),

    # ── 上下文策略 ──────────────────────────────────────────────────
    # 可能需回复/业务线程 → 需要看完整线程上下文
    # 安全/账单 → 只需要看邮件正文详情
    # 可清理 → 只看摘要即可
    context_policy=ContextPolicy(
        read_depth_by_candidate_kind={
            "reply_required_possible": "thread_context",
            "confirmation_required_possible": "message_detail",
            "security_risk_possible": "message_detail",
            "billing_issue_possible": "message_detail",
            "business_thread_possible": "thread_context",
            "safe_cleanup_bundle": "header_only",
            "unsure": "message_detail",
        },
    ),

    # ── 评估策略 ──────────────────────────────────────────────────
    # 按优先级排序的判断维度：安全/风险 > 回复等待 > 用户行动 > 业务影响 > 可代理 > 低价值
    judgment_policy=JudgmentPolicy(
        rubric=(
            "## 判断维度（按优先级，冲突时以高优先级为准）\n"
            "1. **安全/风险** (权重:最高): 登录异常、安全提醒、付款失败、账号风险 → priority ≥ high\n"
            "2. **回复等待** (权重:高): 有人等待回复、含问句或请求、对方最近发来 → priority ≥ medium, should_show_in_main_result=true\n"
            "3. **用户行动** (权重:高): 用户需确认、付款、回复、审批 → should_show_in_main_result=true\n"
            "4. **业务影响** (权重:中): 客户、合作方、业务机会相关 → priority ≥ medium\n"
            "5. **可代理** (权重:中): agent 可准备草稿/标签/提醒 → 标记 can_agent_prepare=true\n"
            "6. **低价值** (权重:低): newsletter、促销、自动通知、已处理完成 → priority=low, should_show_in_main_result=false"
        ),
        output_buckets=[
            "must_review",        # 必须查看
            "needs_reply",        # 需要回复
            "needs_confirmation", # 需要确认
            "agent_can_prepare",  # Agent 可准备
            "safe_cleanup",       # 可安全清理
            "lower_priority",     # 低优先级
            "ignore",             # 可忽略
        ],
        mode_specific_schema_name="SecretaryModeJudgment",
        # few-shot 示例帮助 LLM 理解预期输出格式和质量标准
        few_shot_examples=[
            {
                "input_summary": "Google security alert: new device login",
                "input_detail": "From: Google <no-reply@accounts.google.com>, Subject: New sign-in notification, Snippet: Your Google Account was signed into on an unknown Windows device in Shanghai...",
                "output": '{"base_judgment":{"requires_user_action":true,"can_agent_prepare":false,"can_agent_handle_after_approval":false,"risk_level":"high","other_party_waiting":false,"user_is_blocking":false,"reason":"Google account sign-in from unknown device — user must confirm immediately"},"mode_judgment":{"bucket":"must_review","urgency":"today","who_should_act":"user"},"final_decision":{"display_bucket":"安全告警","priority":"high","should_show_in_main_result":true,"should_show_in_lower_priority":false,"recommended_actions":[{"action_type":"create_reminder","risk_level":"high","requires_approval":false,"payload":{},"reason":"Remind user to verify this login"}],"user_facing_summary":"Google login alert from unknown device","user_facing_reason":"An unknown Windows device signed into your Google account today in Shanghai. You have not reviewed this yet.","user_facing_recommendation":"Check your recent login activity and change your password if this wasn\'t you."},"confidence":0.9}',
            },
            {
                "input_summary": "Colleague asks for proposal review",
                "input_detail": "From: Colleague <colleague@company.com>, Subject: Please review the updated Q2 proposal, Snippet: Hi, I updated the proposal based on your feedback. Could you take a look? Need confirmation by next Monday...",
                "output": '{"base_judgment":{"requires_user_action":true,"can_agent_prepare":true,"can_agent_handle_after_approval":false,"risk_level":"medium","other_party_waiting":true,"user_is_blocking":true,"reason":"Colleague is waiting for review with a deadline next Monday"},"mode_judgment":{"bucket":"needs_reply","urgency":"this_week","who_should_act":"user"},"final_decision":{"display_bucket":"需回复","priority":"medium","should_show_in_main_result":true,"should_show_in_lower_priority":false,"recommended_actions":[{"action_type":"create_reminder","risk_level":"low","requires_approval":false,"payload":{},"reason":"Remind user to review the proposal"}],"user_facing_summary":"Colleague needs your review on Q2 proposal","user_facing_reason":"Your colleague sent the updated Q2 proposal for your review. They asked for confirmation by next Monday.","user_facing_recommendation":"Review the proposal this week and send your feedback before Monday."},"confidence":0.85}',
            },
        ],
    ),

    # ── 动作策略 ──────────────────────────────────────────────────
    # 允许：创建草稿、打标签、创建提醒、标记已读、归档
    # 需审批：标记已读、归档（需用户确认）
    # 禁止：发送邮件、删除邮件、退订
    action_policy=ActionPolicy(
        allowed_suggested_actions=[
            "create_draft", "apply_label", "create_reminder",
            "mark_read", "archive", "do_nothing",
        ],
        require_approval_actions=["mark_read", "archive"],
        forbidden_actions=["send_email", "delete_email", "unsubscribe"],
    ),
))


# ═══════════════════════════════════════════════════════════════════════
#  6.2  合作机会模式
# ═══════════════════════════════════════════════════════════════════════
# 目标：找出合作、BD、creator、YouTube、partner 相关 thread，
#       判断合作关系状态、推进价值、当前 blocker、下一步动作。

creator_opportunity = register(MailStrategy(
    id="creator_opportunity",
    name="合作机会模式",
    description="整理 creator / BD / partner 合作机会。",

    # ── 扫描策略 ──────────────────────────────────────────────────
    # 90天窗口，重点搜索合作关键词、跟进关键词、创作者标签
    scan_policy=ScanPolicy(
        default_time_range="last_90_days",
        default_queries=[
            # 合作/创作者关键词（90天内，高优先级，排除已发送和草稿）
            {"query": "newer_than:90d (collaboration OR partnership OR sponsor OR sponsorship OR creator OR YouTube OR video OR proposal OR demo) -in:sent -in:draft", "purpose": "creator_and_partnership_keywords", "max_results": 150, "priority": "high"},
            # 跟进相关关键词（90天内，中优先级，排除已发送和草稿）
            {"query": "newer_than:90d (interested OR follow up OR following up OR intro OR partnership) -in:sent -in:draft", "purpose": "relationship_followup", "max_results": 100, "priority": "medium"},
            # 带 Creator/YouTube/Partnership 标签的邮件（高优先级，排除已发送和草稿）
            {"query": "(label:Creator OR label:YouTube OR label:Partnership) -in:sent -in:draft", "purpose": "creator_labels", "max_results": 100, "priority": "high"},
        ],
        # 社交类标签在此模式下提升为中等权重
        label_weights={
            "INBOX": "high", "IMPORTANT": "high", "STARRED": "high",
            "CATEGORY_SOCIAL": "medium", "CATEGORY_PROMOTIONS": "low", "CATEGORY_UPDATES": "low",
        },
        budget={"max_messages": 300, "max_threads": 180, "max_detail_reads": 40, "max_thread_reads": 60},
    ),

    # ── 候选策略 ──────────────────────────────────────────────────
    # 关注合作关键词、创作者/YouTube相关、人类发件人
    # 排除平台自动通知、社交媒体摘要、纯促销
    candidate_policy=CandidatePolicy(
        include_signals=[
            "collaboration keyword", "partnership keyword", "creator or YouTube mention",
            "human sender", "follow-up wording", "proposal or demo",
            "other party waiting", "thread with previous replies",
        ],
        exclude_signals=[
            "generic platform notification", "automated social digest",
            "newsletter without direct relationship", "promotion",
        ],
        promote_signals=[
            "known creator", "known partner", "last sender is not user",
            "reply contains interest", "proposal attachment",
        ],
        candidate_kinds=[
            "creator_thread_possible", "business_thread_possible",
            "reply_required_possible", "unsure",
        ],
        llm_candidate_hints="判断是否是真实合作机会或合作关系 thread，不要把普通平台通知当成合作机会。",
    ),

    # ── 上下文策略 ──────────────────────────────────────────────────
    # 合作线程/业务线程 → 需要看完整线程上下文
    # 不确定的 → 至少看邮件正文
    context_policy=ContextPolicy(
        read_depth_by_candidate_kind={
            "creator_thread_possible": "thread_context",
            "business_thread_possible": "thread_context",
            "reply_required_possible": "thread_context",
            "unsure": "message_detail",
        },
    ),

    # ── 评估策略 ──────────────────────────────────────────────────
    # 按合作关系视角排序：真实兴趣 > 等待方 > 推进价值 > 当前阻塞 > 下一步行动 > 关系状态
    judgment_policy=JudgmentPolicy(
        rubric=(
            "## 判断维度（合作关系视角，按优先级）\n"
            "1. **真实兴趣** (权重:最高): 对方是否明确表达合作/赞助/推广意向？→ 如真实则 priority≥high\n"
            "2. **等待方** (权重:高): 现在是谁在等谁？对方等我们 → user_is_blocking=true; 我们等对方 → other_party_waiting=true\n"
            "3. **推进价值** (权重:高): 品牌/预算/受众是否匹配？是否值得继续投入？→ opportunity_quality\n"
            "4. **当前阻塞** (权重:中): 缺什么信息/材料/确认才能推进？→ current_blocker\n"
            "5. **下一步行动** (权重:中): 发更新/发demo/问需求/发报价/等待/关闭 → suggested_next_step\n"
            "6. **关系状态** (权重:中): 是否应该保存到 pipeline 以便跟踪长期关系？"
        ),
        output_buckets=[
            "continue",            # 继续推进
            "needs_follow_up",     # 需要跟进
            "waiting_for_them",    # 等待对方
            "waiting_for_us",      # 等待我方
            "paused",              # 暂停
            "rejected",            # 已拒绝
            "not_worth_pursuing",  # 不值得推进
            "unknown",             # 未知
        ],
        mode_specific_schema_name="CreatorOpportunityJudgment",
        few_shot_examples=[
            {
                "input_summary": "Brand collaboration inquiry",
                "input_detail": "From: Brand Manager <collab@beautybrand.com>, Subject: Collaboration inquiry - makeup tutorial, Snippet: Hi, we love your content! We'd like to discuss a sponsorship for our new product line launching next month...",
                "output": '{"base_judgment":{"requires_user_action":true,"can_agent_prepare":true,"can_agent_handle_after_approval":false,"risk_level":"medium","other_party_waiting":true,"user_is_blocking":true,"reason":"Brand proactively reached out for collaboration — they are waiting for our response"},"mode_judgment":{"relationship_status":"needs_follow_up","opportunity_quality":"medium","current_blocker":"Need to confirm user interest before proceeding","suggested_next_step":"ask_for_requirements","should_save_to_pipeline":true},"final_decision":{"display_bucket":"合作机会","priority":"high","should_show_in_main_result":true,"should_show_in_lower_priority":false,"recommended_actions":[{"action_type":"create_draft","risk_level":"low","requires_approval":true,"payload":{},"reason":"Prepare draft reply asking about collaboration details"}],"user_facing_summary":"Beauty Brand reached out for a makeup tutorial sponsorship","user_facing_reason":"The Brand Manager contacted you about sponsoring their new product line launching next month. They expressed clear interest and are waiting for your response.","user_facing_recommendation":"Reply to express interest and ask about campaign details and budget."},"confidence":0.88}',
            },
            {
                "input_summary": "YouTube automated milestone (not a partnership)",
                "input_detail": "From: YouTube <noreply@youtube.com>, Subject: Your video reached 100K views, Snippet: Congratulations! Your video has reached a new milestone...",
                "output": '{"base_judgment":{"requires_user_action":false,"can_agent_prepare":false,"can_agent_handle_after_approval":false,"risk_level":"none","other_party_waiting":false,"user_is_blocking":false,"reason":"Automated platform notification — not a real collaboration opportunity"},"mode_judgment":{"relationship_status":"not_worth_pursuing","opportunity_quality":"unknown","current_blocker":"","suggested_next_step":"close_or_archive","should_save_to_pipeline":false},"final_decision":{"display_bucket":"不重要","priority":"ignore","should_show_in_main_result":false,"should_show_in_lower_priority":true,"recommended_actions":[{"action_type":"do_nothing","risk_level":"low","requires_approval":false,"payload":{},"reason":"Platform milestone notification — no action needed"}],"user_facing_summary":"YouTube milestone notification — no action needed","user_facing_reason":"YouTube sent an automated notification that your video hit 100K views. No one is waiting for a response.","user_facing_recommendation":"Archive this — it is an automated notification with no follow-up needed."},"confidence":0.95}',
            },
        ],
    ),

    # ── 动作策略 ──────────────────────────────────────────────────
    # 允许：创建草稿、创建提醒、打标签、保存备注、归档
    # 注意：此模式下不禁止 mark_read（但在 guards.py 中仍会检查 require_approval）
    action_policy=ActionPolicy(
        allowed_suggested_actions=[
            "create_draft", "create_reminder", "apply_label",
            "save_note", "archive", "do_nothing",
        ],
        require_approval_actions=["archive"],
        forbidden_actions=["send_email", "delete_email", "unsubscribe"],
    ),
))


# ═══════════════════════════════════════════════════════════════════════
#  6.3  安全账单模式
# ═══════════════════════════════════════════════════════════════════════
# 目标：检查安全、登录、账号、账单、付款、订阅、容量、quota 风险。

security_billing = register(MailStrategy(
    id="security_billing",
    name="安全账单模式",
    description="检查账号风险、账单异常和订阅状态。",

    # ── 扫描策略 ──────────────────────────────────────────────────
    # 90天窗口，安全信号、账单信号、重要服务发送者三类查询
    # 注意：UPDATES 类别在此模式下权重为 high（因为账单/安全通知常在此类别下）
    scan_policy=ScanPolicy(
        default_time_range="last_90_days",
        default_queries=[
            # 安全相关关键词（登录/验证/密码/恢复/账号，排除已发送和草稿）
            {"query": "newer_than:90d (security OR login OR verification OR password OR recovery OR account) -in:sent -in:draft", "purpose": "security_signals", "max_results": 100, "priority": "high"},
            # 账单相关关键词（付款/发票/订阅/续费/容量，排除已发送和草稿）
            {"query": "newer_than:90d (billing OR invoice OR payment OR receipt OR subscription OR renewal OR quota OR storage) -in:sent -in:draft", "purpose": "billing_signals", "max_results": 120, "priority": "high"},
            # 重要服务发送者的邮件（Google/Apple/GitHub/OpenAI/AWS/Stripe 等，排除已发送和草稿）
            {"query": "newer_than:90d (Google OR Apple OR GitHub OR OpenAI OR Microsoft OR AWS OR Azure OR Stripe OR PayPal) -in:sent -in:draft", "purpose": "important_service_senders", "max_results": 120, "priority": "medium"},
        ],
        label_weights={
            "INBOX": "high", "IMPORTANT": "high", "CATEGORY_UPDATES": "high",
            "CATEGORY_PROMOTIONS": "low", "CATEGORY_SOCIAL": "ignore",
        },
        budget={"max_messages": 300, "max_threads": 150, "max_detail_reads": 80, "max_thread_reads": 10},
    ),

    # ── 候选策略 ──────────────────────────────────────────────────
    # 关注安全/登录/验证/付款失败/账单问题
    # 排除产品更新/营销促销/新闻通讯/社交通知
    candidate_policy=CandidatePolicy(
        include_signals=[
            "security keyword", "login keyword", "verification keyword",
            "payment failed", "billing issue", "invoice",
            "subscription change", "quota or storage limit", "account restriction",
        ],
        exclude_signals=[
            "generic product update", "marketing promotion",
            "newsletter", "social notification",
        ],
        promote_signals=[
            "payment failed", "suspicious login", "password changed",
            "account restricted", "quota exceeded", "subscription canceled",
        ],
        candidate_kinds=[
            "security_risk_possible", "billing_issue_possible",
            "account_notice_possible", "safe_account_record", "unsure",
        ],
        llm_candidate_hints="判断是否存在账号、安全、账单、订阅或容量风险。普通收据可以记录但不必高亮。",
    ),

    # ── 上下文策略 ──────────────────────────────────────────────────
    # 安全/账单模式下，所有候选项都需要看完整的邮件正文
    # （因为安全判断需要详细内容，且 max_thread_reads 也设得较低）
    context_policy=ContextPolicy(
        read_depth_by_candidate_kind={
            "security_risk_possible": "message_detail",
            "billing_issue_possible": "message_detail",
            "account_notice_possible": "message_detail",
            "safe_account_record": "message_detail",
            "unsure": "message_detail",
        },
    ),

    # ── 评估策略 ──────────────────────────────────────────────────
    # 按严重程度排序：安全事件 > 付款问题 > 账单/订阅 > 容量/Quota > 普通通知 > 无关
    judgment_policy=JudgmentPolicy(
        rubric=(
            "## 判断维度（按严重程度排序）\n"
            "1. **安全事件** (权重:最高): 异常登录、可疑活动、密码修改、权限变更、账号恢复 → severity≥warning, priority≥high\n"
            "2. **付款问题** (权重:最高): 付款失败、扣款异常、订阅过期 → severity≥warning, priority≥high\n"
            "3. **账单/订阅** (权重:高): 即将续费、订阅变更、发票、收据异常 → priority≥medium\n"
            "4. **容量/Quota** (权重:中): 存储配额不足、API额度用尽 → priority≥medium\n"
            "5. **普通通知** (权重:低): 正常收据、订阅确认、常规账号通知 → severity=info, priority=low\n"
            "6. **无关内容** (权重:无): 非安全/账单相关的邮件 → priority=ignore"
        ),
        output_buckets=[
            "critical_security",   # 严重安全事件
            "billing_attention",   # 需要关注的账单
            "account_warning",     # 账号警告
            "normal_record",       # 正常记录（如收据）
            "no_issue",            # 无问题
            "unknown",             # 未知
        ],
        mode_specific_schema_name="SecurityBillingJudgment",
        few_shot_examples=[
            {
                "input_summary": "Stripe payment failed",
                "input_detail": "From: Stripe <noreply@stripe.com>, Subject: Payment failed - subscription renewal, Snippet: Your recent payment of $29.99 has failed. The card was declined. Please update your payment method to avoid service interruption...",
                "output": '{"base_judgment":{"requires_user_action":true,"can_agent_prepare":false,"can_agent_handle_after_approval":false,"risk_level":"critical","other_party_waiting":false,"user_is_blocking":false,"reason":"Payment failed — subscription service may be interrupted"},"mode_judgment":{"risk_category":"payment_failed","severity":"critical","affected_service":"Stripe subscription","affected_account":"","amount":"$29.99","deadline":"","user_confirmation_needed":true,"recommended_handling":"check_payment"},"final_decision":{"display_bucket":"付款失败","priority":"critical","should_show_in_main_result":true,"should_show_in_lower_priority":false,"recommended_actions":[{"action_type":"create_reminder","risk_level":"high","requires_approval":false,"payload":{},"reason":"Remind user to update payment method"}],"user_facing_summary":"Stripe subscription payment failed — $29.99","user_facing_reason":"A Stripe payment of $29.99 for your subscription renewal was declined today. Your service may be interrupted if not resolved.","user_facing_recommendation":"Update your payment method in Stripe to avoid service interruption."},"confidence":0.92}',
            },
            {
                "input_summary": "Apple normal receipt",
                "input_detail": "From: Apple <no_reply@email.apple.com>, Subject: Your receipt from Apple, Snippet: This is your receipt for a purchase from Apple. Amount: $0.99...",
                "output": '{"base_judgment":{"requires_user_action":false,"can_agent_prepare":false,"can_agent_handle_after_approval":false,"risk_level":"none","other_party_waiting":false,"user_is_blocking":false,"reason":"Routine purchase receipt — nothing unusual"},"mode_judgment":{"risk_category":"invoice_or_receipt","severity":"no_issue","affected_service":"Apple","affected_account":"","amount":"$0.99","deadline":"","user_confirmation_needed":false,"recommended_handling":"record_only"},"final_decision":{"display_bucket":"正常记录","priority":"low","should_show_in_main_result":false,"should_show_in_lower_priority":true,"recommended_actions":[{"action_type":"do_nothing","risk_level":"low","requires_approval":false,"payload":{},"reason":"Routine receipt — record only"}],"user_facing_summary":"Apple purchase receipt — $0.99","user_facing_reason":"Apple sent a standard receipt for a $0.99 purchase today. This is a routine record with no suspicious activity.","user_facing_recommendation":"File away — this is a routine receipt with no action needed."},"confidence":0.95}',
            },
        ],
    ),

    # ── 动作策略 ──────────────────────────────────────────────────
    # 安全账单模式下的动作限制更严格：
    # 不允许 mark_read 和 archive（安全事件必须由用户亲自确认）
    # 只允许创建提醒、打标签、保存备注
    action_policy=ActionPolicy(
        allowed_suggested_actions=[
            "create_reminder", "apply_label", "save_note", "do_nothing",
        ],
        require_approval_actions=[],
        forbidden_actions=[
            "send_email", "delete_email", "unsubscribe",
            "mark_read", "archive",
        ],
    ),
))
