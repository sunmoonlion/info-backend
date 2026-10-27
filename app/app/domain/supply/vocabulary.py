"""knowfeed 用到的那部分词表：knowbase 词表第 2 版（SDD 0049）的副本。

细类 → 大类（按大类要的，展开成它的细类）、定期报告、事项、版本形态。锁
`contracts/knowbase-vocabulary-lock.json` 钉 knowbase 的 `vocabulary.json`，测试核这里
与它一致；knowbase 加了细类、事项，这里跟着加。
"""

from __future__ import annotations

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


def types_of(doc_type: str) -> tuple[str, ...]:
    """细类就是它自己；大类是它下面的全部细类；不认得的是空。"""
    if doc_type in DOC_TYPE_CLASS:
        return (doc_type,)
    return tuple(t for t, cls in DOC_TYPE_CLASS.items() if cls == doc_type)


def is_periodic(doc_type: str) -> bool:
    return doc_type in PERIODIC
