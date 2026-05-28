"""邮件 Agent 核心类型定义。

定义了整个邮件处理流水线中使用的所有数据结构，包括：
- 策略模式类型（StrategyMode）
- 候选类型（CandidateKind）和读取深度（ReadDepth）
- 输入/计划/策略/邮件适配器相关类型
- 候选/上下文/评估/行动计划相关类型

所有类型使用 Python dataclass 实现，便于序列化和传递。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── 策略模式 ────────────────────────────────────────────────────────
# 三种预设的邮件处理策略，每种策略影响扫描范围、候选生成、评估标准和允许动作

StrategyMode = Literal["default_secretary", "creator_opportunity", "security_billing"]

# ── 候选类型 + 读取深度 ─────────────────────────────────────────────
# 候选类型对应邮件可能所属的分类，读取深度决定了 Anna 需要查看多少上下文

CandidateKind = Literal[
    "reply_required_possible",          # 可能需要回复
    "confirmation_required_possible",   # 可能需要确认
    "security_risk_possible",          # 可能存在安全风险
    "billing_issue_possible",          # 可能存在账单问题
    "account_notice_possible",         # 可能是账号通知
    "business_thread_possible",        # 可能是业务讨论
    "creator_thread_possible",         # 可能是创作者/合作讨论
    "safe_account_record",             # 安全的账号记录（如普通收据）
    "safe_cleanup_bundle",             # 可以安全清理的（如新闻通讯）
    "unsure",                          # 不确定类型
]

ReadDepth = Literal["header_only", "message_detail", "thread_context", "batch_summary"]

# 建议动作类型：Anna 可以对邮件执行的操作
ProposedActionType = Literal[
    "create_draft",       # 创建回复草稿
    "apply_label",        # 添加标签
    "create_reminder",    # 创建提醒
    "save_note",          # 保存备注
    "mark_read",          # 标记已读
    "archive",            # 归档
    "send_email",         # 发送邮件（被安全策略禁止）
    "delete_email",       # 删除邮件（被安全策略禁止）
    "unsubscribe",        # 退订（被安全策略禁止）
    "do_nothing",         # 不执行操作
]

# ── 输入 / 计划类型 ──────────────────────────────────────────────────

@dataclass
class MailTaskInput:
    """邮件任务的原始输入，来自用户请求。

    字段：
        user_request: 用户的自然语言请求，如"帮我看看有什么重要邮件"
        mailbox_id: 邮箱地址，如 kate@anna.partners
        user_email: 用户邮箱（通常同 mailbox_id）
        mode: 策略模式，auto 表示自动选择
        max_messages: 最多扫描的邮件数量
        dry_run: 是否为干运行模式（不执行真实写操作，MVP 下始终为 True）
    """
    user_request: str
    mailbox_id: str
    user_email: str
    mode: StrategyMode | Literal["auto"] = "auto"
    max_messages: int = 250
    dry_run: bool = True


@dataclass
class MailTaskPlan:
    """意图解析后的任务计划。

    包含从用户请求中提取的目标、约束和搜索范围，
    以及选定的策略模式。
    """
    raw_user_request: str                        # 原始用户请求文本
    mailbox_id: str                              # 目标邮箱
    user_email: str                              # 用户邮箱
    strategy_mode: StrategyMode                  # 选定的策略模式
    goals: list[str] = field(default_factory=list)        # 任务目标列表
    constraints: list[str] = field(default_factory=list)  # 约束列表（如 dry_run_only）
    scope: dict[str, Any] = field(default_factory=dict)   # 搜索范围（关键词、时间范围等）


# ── 邮箱画像（保留接口，MVP 阶段使用 mock）──────────────────────────

@dataclass
class MailboxProfile:
    """用户邮箱画像，用于个性化邮件处理。

    MVP 阶段使用 mock 数据，后续版本可以从历史邮件中自动生成。
    """
    mailbox_id: str = "default"
    owner: str = ""
    primary_role: str = "general_work_inbox"
    secondary_roles: list[str] = field(default_factory=list)
    high_priority_signals: list[str] = field(default_factory=lambda: [
        "human replies",
        "partner or creator collaboration",
        "customer requests",
        "security alerts",
        "billing or payment issues",
    ])
    low_priority_signals: list[str] = field(default_factory=lambda: [
        "generic newsletters",
        "platform digests",
        "promotions",
        "automated product updates",
    ])
    important_contacts: list[str] = field(default_factory=list)
    important_labels: list[str] = field(default_factory=lambda: ["INBOX", "IMPORTANT", "STARRED"])
    assumptions: list[str] = field(default_factory=list)


def default_mock_mailbox_profile(mailbox_id: str = "default") -> MailboxProfile:
    """创建默认的 mock 邮箱画像。后续可替换为自动生成的真实画像。"""
    return MailboxProfile(mailbox_id=mailbox_id)


# ── 策略配置类型 ────────────────────────────────────────────────────
# 每种 MailStrategy 由五个子策略组成，覆盖整个处理链路

@dataclass
class ScanQuery:
    """Gmail 搜索查询定义。

    query 使用 Gmail 搜索语法（如 newer_than:3d, label:INBOX 等）。
    """
    query: str                                  # Gmail 搜索查询字符串
    purpose: str                                # 此查询的目的说明
    max_results: int                            # 最大返回结果数
    priority: Literal["high", "medium", "low"]  # 查询优先级


@dataclass
class ScanBudget:
    """扫描阶段的资源预算限制。

    控制最多扫描多少封邮件、多少条线程等，防止过度消耗 API 配额。
    """
    max_messages: int      # 最多处理的消息数
    max_threads: int       # 最多处理的线程数
    max_detail_reads: int  # 最多读取全文的消息数
    max_thread_reads: int  # 最多读取完整线程数


@dataclass
class ScanPolicy:
    """扫描策略：定义如何搜索和筛选邮件。

    default_queries 是策略预设的 Gmail 查询列表，
    label_weights 定义不同标签的权重，
    budget 控制资源消耗。
    """
    default_time_range: str
    default_queries: list[dict[str, Any]]
    label_weights: dict[str, str]
    budget: dict[str, int]


@dataclass
class CandidatePolicy:
    """候选生成策略：定义哪些邮件应该成为候选项。

    include_signals: 正向信号列表（此类信号出现表示需要关注）
    exclude_signals: 排除信号列表（此类信号出现表示不重要）
    promote_signals: 提升信号列表（出现时提高优先级）
    candidate_kinds: 允许的候选类型（只生成这些类型的候选）
    llm_candidate_hints: 给 LLM 的候选判断提示
    """
    include_signals: list[str]
    exclude_signals: list[str]
    promote_signals: list[str]
    candidate_kinds: list[CandidateKind]
    llm_candidate_hints: str


@dataclass
class ContextPolicy:
    """上下文读取策略：定义不同类型的候选需要读取多深。

    read_depth_by_candidate_kind 将每种 CandidateKind 映射到对应的 ReadDepth。
    例如安全风险只需看 message_detail，而业务线程可能需要 thread_context。
    """
    read_depth_by_candidate_kind: dict[str, ReadDepth]


@dataclass
class JudgmentPolicy:
    """事项评估策略：定义 LLM 如何判断候选的重要性。

    rubric: 判断维度和评估标准的详细说明（Markdown 格式）
    output_buckets: 输出的分类桶列表
    mode_specific_schema_name: 模式特定的 schema 名称
    few_shot_examples: few-shot 示例（帮助 LLM 理解输出格式）
    """
    rubric: str
    output_buckets: list[str]
    mode_specific_schema_name: str
    few_shot_examples: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ActionPolicy:
    """动作策略：定义 LLM 可以建议哪些操作，哪些需要审批，哪些被禁止。

    这是安全边界的关键组件——即使 LLM 输出了被禁止的动作，
    guards.py 中的 apply_rule_guards 也会将其拦截。
    """
    allowed_suggested_actions: list[ProposedActionType]
    require_approval_actions: list[ProposedActionType]
    forbidden_actions: list[ProposedActionType]


@dataclass
class MailStrategy:
    """邮件处理策略的完整定义。

    一个策略 = 扫描策略 + 候选策略 + 上下文策略 + 评估策略 + 动作策略。
    三种预设策略在 strategies.py 中定义并注册。
    """
    id: StrategyMode                           # 策略唯一标识
    name: str                                  # 策略显示名称
    description: str                           # 策略描述
    scan_policy: ScanPolicy                    # 扫描策略
    candidate_policy: CandidatePolicy          # 候选策略
    context_policy: ContextPolicy              # 上下文策略
    judgment_policy: JudgmentPolicy            # 评估策略
    action_policy: ActionPolicy                # 动作策略


# ── 邮件适配器类型 ──────────────────────────────────────────────────
# 定义邮件数据的三种粒度：Lite（摘要）、Detail（全文）、Thread（线程上下文）

@dataclass
class MessageLite:
    """邮件摘要信息。从 Gmail API 的消息列表接口获取，不包含正文。"""
    message_id: str
    thread_id: str
    from_addr: str          # 发件人
    to_addr: str            # 收件人（可能是多个，逗号分隔）
    cc: str = ""            # 抄送
    subject: str = ""       # 主题
    snippet: str = ""       # 片段（Gmail 生成的前 ~100 字符文本预览）
    internal_date: str = "" # Gmail internalDate（epoch 毫秒字符串）
    label_ids: list[str] = field(default_factory=list)
    unread: bool = False    # 是否未读
    starred: bool = False   # 是否星标
    important: bool = False # 是否重要
    has_attachment: bool = False  # 是否有附件
    headers: dict[str, str] = field(default_factory=dict)  # 附加头信息（如 List-Unsubscribe）


@dataclass
class MessageDetail:
    """邮件详细信息。在 MessageLite 基础上增加正文内容。

    通常在候选生成后、需要深入读取某封邮件时获取。
    """
    message_id: str
    thread_id: str
    from_addr: str
    to_addr: str
    cc: str = ""
    subject: str = ""
    snippet: str = ""
    internal_date: str = ""
    label_ids: list[str] = field(default_factory=list)
    unread: bool = False
    starred: bool = False
    important: bool = False
    has_attachment: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    body_text: str = ""     # 邮件正文纯文本（解码后的明文）


@dataclass
class ThreadContext:
    """线程上下文。包含同一线程下所有消息的详细信息。

    用于需要完整对话历史才能做判断的场景（如合作讨论、客户沟通）。
    """
    thread_id: str
    messages: list[MessageDetail] = field(default_factory=list)


# ── 候选类型 ────────────────────────────────────────────────────────

@dataclass
class CandidateItem:
    """候选项：从邮件列表中筛选出的可能需要关注的条目。

    候选生成阶段（candidate.py 的 generate_candidates 或 phase1.py 的 LLM 分类）
    会产生此类型的实例，供后续的上下文读取和 LLM 评估阶段使用。

    evidence 字段包含判断依据（发件人、主题、匹配的信号等），
    帮助后续的 LLM 评估阶段理解为什么这条邮件被选为候选。
    """
    candidate_id: str                                                  # 候选唯一标识（如 cand_xxxxxxxxxxxx）
    kind: CandidateKind                                                # 候选类型
    message_ids: list[str] = field(default_factory=list)              # 关联的消息 ID 列表
    thread_id: str = ""                                                # 所属线程 ID
    evidence: dict[str, Any] = field(default_factory=dict)            # 判断依据（信号、发件人等）
    priority_hint: Literal["high", "medium", "low", "unknown"] = "unknown"  # 优先级提示
    read_depth_required: ReadDepth = "header_only"                    # 需要的读取深度
    source: Literal["rule", "llm", "rule_llm_merged"] = "rule"        # 候选来源
    confidence: float = 0.5                                           # 置信度 0.0~1.0


# ── 上下文类型 ──────────────────────────────────────────────────────

@dataclass
class CandidateContext:
    """候选的上下文数据。根据 read_depth_required 包含不同粒度的信息。

    type='header_only': 只有候选本身（无需额外读取）
    type='message_detail': 包含单封邮件的全文
    type='thread_context': 包含整个线程的消息历史
    """
    type: ReadDepth
    candidate: CandidateItem
    message: MessageDetail | None = None      # type=message_detail 时有值
    thread: ThreadContext | None = None        # type=thread_context 时有值
    messages: list[MessageLite] | None = None  # type=batch_summary 时有值


# ── 评估类型 ────────────────────────────────────────────────────────

@dataclass
class BaseJudgment:
    """基础评估结果。所有策略模式共享的通用字段。

    item_type: 自动从 mode_judgment 中推导，不要求 LLM 显式输出
    should_surface: 是否应该展示给用户（从 final_decision 推导 + 高风险自动升级）
    """
    item_type: str = "unknown"
    requires_user_action: bool = False              # 是否需要用户行动
    can_agent_prepare: bool = False                 # 是否可以由 Agent 准备
    can_agent_handle_after_approval: bool = False   # 是否可以在审批后由 Agent 处理
    risk_level: Literal["none", "low", "medium", "high", "critical"] = "none"
    other_party_waiting: bool = False               # 对方是否在等待
    user_is_blocking: bool = False                  # 用户是否在阻塞进度
    should_surface: bool = False                    # 是否应该展示给用户
    reason: str = ""                                # 判断理由（简短中文）


@dataclass
class ProposedAction:
    """建议的操作。LLM 可以为零个或多个候选建议操作。"""
    action_type: ProposedActionType = "do_nothing"
    risk_level: Literal["low", "medium", "high"] = "low"
    requires_approval: bool = False       # 是否需要用户审批
    payload: dict[str, Any] = field(default_factory=dict)  # 操作附带数据
    reason: str = ""                      # 建议理由


@dataclass
class SecretaryModeJudgment:
    """秘书模式专属评估字段。

    bucket:  分类桶（must_review/needs_reply/needs_confirmation/...）
    urgency: 紧急程度
    who_should_act: 应该由谁处理
    """
    bucket: Literal["must_review", "needs_reply", "needs_confirmation", "agent_can_prepare", "safe_cleanup", "lower_priority", "ignore"] = "lower_priority"
    urgency: Literal["today", "this_week", "later", "none"] = "none"
    who_should_act: Literal["user", "agent_after_approval", "no_action"] = "no_action"


@dataclass
class CreatorOpportunityJudgment:
    """合作机会模式专属评估字段。

    relationship_status: 合作关系状态
    opportunity_quality: 机会质量
    current_blocker: 当前阻塞因素
    suggested_next_step: 建议的下一步
    should_save_to_pipeline: 是否应保存到合作 pipeline 以便长期跟踪
    """
    relationship_status: Literal["continue", "needs_follow_up", "waiting_for_them", "waiting_for_us", "paused", "rejected", "not_worth_pursuing", "unknown"] = "unknown"
    opportunity_quality: Literal["high", "medium", "low", "unknown"] = "unknown"
    current_blocker: str = ""
    suggested_next_step: Literal["send_short_update", "share_build_or_demo", "ask_for_requirements", "send_pricing_or_terms", "wait", "close_or_archive", "manual_review"] = "manual_review"
    should_save_to_pipeline: bool = False


@dataclass
class SecurityBillingJudgment:
    """安全账单模式专属评估字段。

    risk_category:      风险类别（登录告警/付款失败/发票/订阅变更...）
    severity:           严重程度
    affected_service:    受影响的服服务（如 Stripe, AWS）
    amount:             涉及金额（如 $29.99）
    user_confirmation_needed: 是否需要用户确认
    recommended_handling: 建议的处理方式
    """
    risk_category: Literal["login_alert", "verification_code", "password_or_recovery", "payment_failed", "invoice_or_receipt", "subscription_change", "quota_or_storage", "account_restriction", "permission_or_access", "normal_account_notice", "unknown"] = "unknown"
    severity: Literal["critical", "warning", "info", "no_issue"] = "no_issue"
    affected_service: str = ""
    affected_account: str = ""
    amount: str = ""
    deadline: str = ""
    user_confirmation_needed: bool = False
    recommended_handling: Literal["confirm_login", "check_payment", "review_invoice", "increase_quota_or_clean_storage", "review_account_access", "record_only", "ignore"] = "record_only"


# 模式评估的联合类型（实际代码中使用 dict 以支持 JSON 序列化）
ModeJudgment = "SecretaryModeJudgment | CreatorOpportunityJudgment | SecurityBillingJudgment"


@dataclass
class FinalDecision:
    """最终决策。聚合了 LLM 评估的所有面向用户的输出。

    核心字段：
        user_facing_summary:      卡片第 1 行 — Attention Title
        user_facing_reason:       卡片第 2 行 — Compressed Context
        user_facing_recommendation: 卡片第 3 行 — Suggested Next Step
        display_bucket:           分类标签（中文短标签）
        priority:                 优先级
        should_show_in_main_result: 是否在"重点关注"区域展示
        should_show_in_lower_priority: 是否在"低优先"区域展示
    """
    display_bucket: str = ""
    priority: Literal["critical", "high", "medium", "low", "ignore"] = "low"
    should_show_in_main_result: bool = False
    should_show_in_lower_priority: bool = False
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)
    user_facing_summary: str = ""
    user_facing_reason: str = ""
    user_facing_recommendation: str = ""


@dataclass
class JudgmentResult:
    """单条候选的完整评估结果。

    包含基础评估（base_judgment）、模式专属评估（mode_judgment）和最终决策（final_decision）。
    """
    candidate_id: str
    strategy_mode: StrategyMode = "default_secretary"
    base_judgment: BaseJudgment = field(default_factory=BaseJudgment)
    mode_judgment: dict[str, Any] = field(default_factory=dict)   # 存储模式专属字段
    final_decision: FinalDecision = field(default_factory=FinalDecision)
    confidence: float = 0.5  # 整体置信度


# ── 行动计划类型 ────────────────────────────────────────────────────

@dataclass
class DisplayItem:
    """面向用户展示的单个条目的摘要信息。

    这些信息来自 JudgmentResult，但经过简化和格式化，
    适合直接在前端渲染为卡片列表。
    """
    candidate_id: str
    title: str           # 卡片第 1 行
    bucket: str          # 分类标签
    priority: str        # 优先级
    summary: str = ""    # 卡片第 2 行
    reason: str = ""     # 判断理由
    recommendation: str = ""  # 卡片第 3 行
    action_ids: list[str] = field(default_factory=list)


@dataclass
class ActionPlan:
    """邮件处理任务的完整输出。

    这是 run_mail_task 的最终返回值，包含：
    - main_items: 需要用户重点关注的事项
    - lower_priority_items: 低优先级事项
    - proposed_actions: Agent 建议执行的操作
    - approval_memo: 审批备忘（安全/需审批/被策略阻止的分类）
    """
    run_id: str
    strategy_mode: StrategyMode = "default_secretary"
    title: str = ""                                    # 简报标题
    summary: str = ""                                  # 简报摘要
    main_items: list[DisplayItem] = field(default_factory=list)           # 主关注事项
    lower_priority_items: list[DisplayItem] = field(default_factory=list) # 低优先事项
    proposed_actions: list[dict[str, Any]] = field(default_factory=list)  # 建议动作
    approval_memo: dict[str, list[str]] = field(default_factory=dict)     # 审批备忘


# ── 定制扫描类型 ────────────────────────────────────────────────────

@dataclass
class CustomScanPlan:
    """自包含的定制扫描执行计划。与 Brief 的策略体系完全解耦。

    Ask 走 agent 范式（非 workflow）：搜邮件 → 按需读 → 一次 LLM → 答案。
    由 planner.py 从自然语言请求生成，传递给 run_custom_scan() 执行。
    持久化后支持复用（跳过 LLM 规划阶段直接重新执行）。
    """
    plan_id: str = ""                      # "cplan_" + 12 hex
    user_request: str = ""                 # 原始 NL 输入

    # Scan
    gmail_queries: list[dict[str, Any]] = field(default_factory=list)
    scan_budget: dict[str, int] = field(default_factory=dict)

    # Read depth — Planner 根据用户意图选择
    read_depth: str = "message_detail"     # "header_only" | "message_detail" | "thread_context"

    # Task prompt — 告诉执行 LLM 做什么 + 输出什么格式
    task_prompt: str = ""

    # Metadata
    title: str = ""
    description: str = ""
    created_at: str = ""
    last_used_at: str = ""
    use_count: int = 0
    last_result_summary: str = ""
