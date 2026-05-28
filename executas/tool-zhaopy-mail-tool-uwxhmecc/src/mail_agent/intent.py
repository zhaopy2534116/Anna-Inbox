"""意图解析器和策略选择器。

采用规则优先（rule-first）方法：
- 显式指定 mode → 直接使用
- mode="auto" → 通过中英文关键词规则匹配，默认 fallback 到 default_secretary
- 同时从自然语言请求中提取搜索关键词，用于精细化 Gmail 扫描查询
- LLM 精细化解析保留给未来迭代

设计文档 §9。
"""

from __future__ import annotations

import re
from typing import Any

from .strategies import get as get_strategy
from .types import MailTaskInput, MailTaskPlan, StrategyMode


# ── 规则式关键词匹配 ─────────────────────────────────────────────
# _RULES 是一组 (关键词列表, 策略模式) 的映射，
# 按列表顺序匹配（先匹配到合作相关 → creator_opportunity，
# 再匹配安全/账单 → security_billing，最后默认 default_secretary）

_RULES: list[tuple[list[str], StrategyMode]] = [
    (
        # 合作机会模式触发词：中英文合作/创作者/BD/赞助相关
        [
            "合作", "creator", "youtube", "bd",
            "partner", "collaboration", "sponsor", "提案", "sponsorship",
        ],
        "creator_opportunity",
    ),
    (
        # 安全账单模式触发词：中英文安全/账单/登录/付款/订阅相关
        [
            "安全", "账单", "登录", "付款", "订阅",
            "billing", "security", "login", "payment",
            "invoice", "receipt", "subscription", "账户",
            "账号", "风险", "密码", "password",
        ],
        "security_billing",
    ),
]


def rule_select_strategy(user_request: str) -> StrategyMode:
    """基于规则选择策略模式。未匹配到任何关键词时默认返回 default_secretary。

    匹配逻辑：遍历 _RULES，任意一个关键词出现在用户请求中即选中对应模式。
    """
    text = user_request.lower()
    for keywords, mode in _RULES:
        if any(kw.lower() in text for kw in keywords):
            return mode
    return "default_secretary"


# ── 从用户请求中提取搜索关键词 ───────────────────────────────────
# 目标：从自然语言请求中提取可用于 Gmail 搜索的关键词。
# 例如："帮我看看最近有没有YouTube合作的邮件" → 提取 ["YouTube", "合作"]

# 中文停用短语：这些多字短语在提取关键词前从文本中移除
# 顺序重要：较长的短语必须先匹配，避免部分移除
_CHINESE_STOP_NGRAMS: list[str] = [
    # 问句/请求包装词
    "有没有", "是否能", "能不能", "可不可以", "是否",
    "帮我看看", "帮我查", "帮我找", "帮我搜",
    "替我看看", "替我查", "请帮我", "麻烦帮我",
    "看看有没有", "查一下有没有", "找一下有没有",
    "看一下", "查一下", "找一下", "搜一下", "检查一下",
    "看看", "帮我", "请问", "麻烦",
    "有什么", "是什么", "哪个", "哪些",
    "收到", "收到过", "有没有收到",
    "发给我", "给我发", "发送给",
    "告诉我", "通知我", "提醒我",
    "联系", "联系我", "联系过", "联系过你",
    # 相对时间词
    "最近", "今天", "昨天", "本周", "这周", "本月", "这个月",
    "最近几天", "最近一周", "最近一个月", "最近一周内",
    # 修饰词
    "相关的", "有关的", "关于",
    "重要的", "紧急的", "关键的",
    "邮件",  # 泛词，不参与搜索
    # 数字时间词
    "一周", "一天", "两天", "三天",
    "一个月", "两个月", "三个月",
]

# 中文字符级停用词：单字虚词，被移除并用空格替换
_CHINESE_STOP_CHARS: set[str] = {
    "的", "了", "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "嘛",
    "着", "过", "得", "地",
    "很", "都", "也", "就", "才", "还", "又", "再",
    "不", "没", "别", "勿",
    "和", "与", "或", "及", "把", "被", "让", "给",
    "从", "向", "到", "对", "上", "下", "里", "外", "中",
    "我", "你", "他", "她", "它", "们", "谁", "什么", "怎么",
    "会", "能", "是", "有", "在", "去", "来", "做", "看", "说",
}

# 英文停用词：常见的功能词，不参与 Gmail 搜索
_ENGLISH_STOP_WORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they",
    "it", "its", "this", "that", "these", "those",
    "do", "does", "did", "can", "could", "will", "would", "shall", "should",
    "may", "might", "must", "have", "has", "had",
    "to", "for", "of", "in", "on", "at", "by", "with", "from",
    "about", "into", "through", "during", "before", "after",
    "not", "no", "nor", "or", "and", "but",
    "please", "check", "find", "search", "look", "see", "tell",
    "any", "some", "all", "every", "each", "both",
    "recent", "today", "yesterday", "important", "urgent", "critical",
    "review", "help", "email", "mail", "message",
}


def _extract_keywords(user_request: str) -> list[str]:
    """从用户的自然语言请求中提取 Gmail 可搜索的关键词。

    处理策略：
    1. 提取 ASCII 单词（天生适合作为 Gmail 搜索词）
    2. 从文本中移除已知的中文停用短语
    3. 按标点/空白分割剩余文本
    4. 从片段边界剥离单个停用字符
    5. 保留 2 字以上的片段，去重，限制最多 5 个

    返回：关键词列表（最多 5 个）
    """
    text = str(user_request or "").strip()
    if not text:
        return []

    # 步骤 1：提取 ASCII 单词（天生是好的搜索词）
    ascii_words: list[str] = re.findall(r"[a-zA-Z0-9]{2,}", text)

    # 步骤 2：移除中文停用短语（长短语优先，防止部分匹配）
    for stop in _CHINESE_STOP_NGRAMS:
        text = text.replace(stop, " ")

    # 步骤 3：用空格替换停用字符
    cleaned: list[str] = []
    for ch in text:
        if ch in _CHINESE_STOP_CHARS or ch in {' ', '\t', '\n', '\r'}:
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    text = "".join(cleaned)

    # 步骤 4：按标点/空白分割
    segments: list[str] = re.split(
        r"[\s，。！？、（）：；「」『』【】《》／\\|,\.!\?:;\(\)\[\]\{\}\"\'`　]+",
        text,
    )

    # 步骤 5：收集有意义的关键词片段
    keywords: list[str] = [
        w for w in ascii_words if w.lower() not in _ENGLISH_STOP_WORDS
    ]
    # 同时从剩余文本中收集中文内容词
    for seg in segments:
        seg = seg.strip()
        # 从边缘剥离单个停用字符
        seg = seg.strip("".join(_CHINESE_STOP_CHARS))
        # 剥离已知的 ASCII 词汇边缘（如 "YouTube合作" → "合作"）
        for aw in ascii_words:
            if seg.startswith(aw):
                seg = seg[len(aw):].strip("".join(_CHINESE_STOP_CHARS))
            if seg.endswith(aw):
                seg = seg[:-len(aw)].strip("".join(_CHINESE_STOP_CHARS))
        if not seg:
            continue
        # 过滤掉仍然残留的英文停用词
        if seg.lower() in _ENGLISH_STOP_WORDS:
            continue
        if len(seg) >= 2:
            keywords.append(seg)

    # 去重保持顺序，限制最多 5 个关键词
    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        lower = kw.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(kw)
        if len(result) >= 5:
            break

    return result


# ── 主入口 ────────────────────────────────────────────────────────

async def parse_intent(
    input_: MailTaskInput,
    sampling_create_message: Any | None = None,
) -> MailTaskPlan:
    """解析用户意图并选择策略模式。

    工作流程：
    1. 如果用户显式指定了 mode → 直接使用该模式创建计划
    2. 如果 mode="auto" → 通过 rule_select_strategy 自动匹配
    3. 从用户请求中提取关键词，注入到 scope 中供扫描阶段使用

    参数：
        input_: 邮件任务输入，包含用户请求和邮箱信息
        sampling_create_message: 保留参数，用于未来的 LLM 意图精细化

    返回：
        MailTaskPlan，包含选定的策略模式、目标、约束和搜索范围
    """
    # 显式模式路径：用户指定了具体策略
    if input_.mode and input_.mode != "auto":
        strategy = get_strategy(input_.mode)
        if strategy is None:
            raise ValueError(f"Unknown strategy mode: {input_.mode}")
        return MailTaskPlan(
            raw_user_request=input_.user_request,
            mailbox_id=input_.mailbox_id,
            user_email=input_.user_email,
            strategy_mode=input_.mode,
            goals=[strategy.description],
            constraints=["dry_run_only", "no_write_operations"],
            scope={"keywords": _extract_keywords(input_.user_request)},
        )

    # 自动模式路径：规则匹配 + 关键词提取
    mode = rule_select_strategy(input_.user_request)
    strategy = get_strategy(mode)
    return MailTaskPlan(
        raw_user_request=input_.user_request,
        mailbox_id=input_.mailbox_id,
        user_email=input_.user_email,
        strategy_mode=mode,
        goals=[strategy.description] if strategy else [],
        constraints=["dry_run_only", "no_write_operations"],
        scope={"keywords": _extract_keywords(input_.user_request)},
    )
