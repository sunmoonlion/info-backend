"""knowfeed 用到的那部分词表：knowbase 词表第 2 版（SDD 0049）的副本。

细类 → 大类（按大类要的，展开成它的细类）、定期报告、事项、版本形态、主体编号的几种
写法与哪类主体能采哪些细类（`collectable`）。锁
`contracts/knowbase-vocabulary-lock.json` 钉 knowbase 的 `vocabulary.json`，测试核这里
与它一致；knowbase 加了细类、事项，这里跟着加。
"""

from __future__ import annotations

import re

VOCAB_VERSION = 2

# 细类 → 大类
DOC_TYPE_CLASS: dict[str, str] = {
    "administrative_regulation": "policy",
    "annual_report": "disclosure",
    "brief": "briefing",
    "business_rule": "policy",
    "census": "statistics",
    "departmental_rule": "policy",
    "financial_news": "news",
    "interim_announcement": "disclosure",
    "ir_activity": "disclosure",
    "ir_qa": "disclosure",
    "judicial_interpretation": "policy",
    "law": "policy",
    "listing_announcement": "disclosure",
    "local_regulation": "policy",
    "negative_news": "news",
    "normative_document": "policy",
    "other_table": "other",
    "other_text": "other",
    "policy_interpretation": "policy",
    "prospectus": "disclosure",
    "q1_report": "disclosure",
    "q3_report": "disclosure",
    "recommended_news": "news",
    "regulatory_letter": "policy",
    "research_commodity": "research",
    "research_company": "research",
    "research_derivatives": "research",
    "research_esg": "research",
    "research_fixed_income": "research",
    "research_fund": "research",
    "research_fx": "research",
    "research_industry": "research",
    "research_macro": "research",
    "research_market": "research",
    "research_morning": "research",
    "research_other": "research",
    "research_quant": "research",
    "research_reits": "research",
    "research_strategy": "research",
    "semiannual_report": "disclosure",
    "statistical_bulletin": "statistics",
    "statistical_data": "statistics",
    "webpage": "other",
}
PERIODIC: tuple[str, ...] = (
    "annual_report",
    "q1_report",
    "q3_report",
    "semiannual_report",
)
TOPICS: tuple[str, ...] = (
    "dividend",
    "equity",
    "governance",
    "intermediary",
    "ipo",
    "meetings",
    "operations",
    "refinancing",
    "restructuring",
    "results",
    "supervision",
    "trading",
)
EDITIONS: tuple[str, ...] = ("english", "full", "summary")

# 主体编号的几种写法（knowbase `domain/vocabulary.py` 的 `_SUBJECT_PATTERNS`，词表的
# `subject_kinds`）：上市证券用「代码.市场」；发文机关、统计口径这类不是上市公司的用
# 「org:统一社会信用代码」，没有信用代码的由 knowbase 管理端登记分配「org:kb-<编号>」
SUBJECT_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cn_security", re.compile(r"^\d{6}\.(SH|SZ|BJ)$")),
    ("hk_security", re.compile(r"^\d{5}\.HK$")),
    ("us_security", re.compile(r"^[A-Z][A-Z0-9.-]{0,9}\.US$")),
    ("org_credit_code", re.compile(r"^org:[0-9A-HJ-NPQRTUWXY]{18}$")),
    ("org_registered", re.compile(r"^org:kb-[a-z0-9-]{3,48}$")),
)
# 哪类主体能按描述采哪些细类（词表的 `collectable`）：机构编号对政策法规、统计数据
COLLECTABLE_BY_ORG: tuple[str, ...] = (
    "administrative_regulation",
    "census",
    "departmental_rule",
    "judicial_interpretation",
    "law",
    "local_regulation",
    "normative_document",
    "policy_interpretation",
    "statistical_bulletin",
    "statistical_data",
)


def types_of(doc_type: str) -> tuple[str, ...]:
    """细类就是它自己；大类是它下面的全部细类；不认得的是空。"""
    if doc_type in DOC_TYPE_CLASS:
        return (doc_type,)
    return tuple(t for t, cls in DOC_TYPE_CLASS.items() if cls == doc_type)


def is_periodic(doc_type: str) -> bool:
    return doc_type in PERIODIC


def subject_kind(subject: str) -> str | None:
    """主体编号是哪一种；不认得的是 None。"""
    for kind, pattern in SUBJECT_KINDS:
        if pattern.fullmatch(subject or ""):
            return kind
    return None


def is_org(subject: str) -> bool:
    return (subject_kind(subject) or "").startswith("org_")
