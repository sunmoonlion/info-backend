"""三张报表的字段字典与勾稽规则：从定期报告抽表时认科目（`labels`）、核等式（`checks`）。

这是我们自建的规则（0008-info 起），不是采来的；改动这里等于改抽表的含义，要走评审。
原来给旧 SQLite 数据集用的行字段、关键科目、口径表、表间关系、口径常量随数据集退役删了
（decisions ⑨、⑰；10-29 清小遗留）。
"""

from __future__ import annotations

from dataclasses import dataclass

YUAN = "元"

# 报表 → [(数据集字段, 来源字段, 中文名, 单位)]
STATEMENT_FIELDS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "balance_sheet": (
        ("total_assets", "TOTAL_ASSETS", "资产总计", YUAN),
        ("total_liabilities", "TOTAL_LIABILITIES", "负债合计", YUAN),
        ("total_equity", "TOTAL_EQUITY", "所有者权益合计", YUAN),
        (
            "total_parent_equity",
            "TOTAL_PARENT_EQUITY",
            "归属于母公司所有者权益合计",
            YUAN,
        ),
        ("minority_equity", "MINORITY_EQUITY", "少数股东权益", YUAN),
        ("total_liab_equity", "TOTAL_LIAB_EQUITY", "负债和所有者权益总计", YUAN),
        ("monetaryfunds", "MONETARYFUNDS", "货币资金", YUAN),
        ("accounts_rece", "ACCOUNTS_RECE", "应收账款", YUAN),
        ("inventory", "INVENTORY", "存货", YUAN),
        ("total_current_assets", "TOTAL_CURRENT_ASSETS", "流动资产合计", YUAN),
        ("long_equity_invest", "LONG_EQUITY_INVEST", "长期股权投资", YUAN),
        ("fixed_asset", "FIXED_ASSET", "固定资产", YUAN),
        ("cip", "CIP", "在建工程", YUAN),
        ("useright_asset", "USERIGHT_ASSET", "使用权资产", YUAN),
        ("intangible_asset", "INTANGIBLE_ASSET", "无形资产", YUAN),
        ("total_noncurrent_assets", "TOTAL_NONCURRENT_ASSETS", "非流动资产合计", YUAN),
        ("short_loan", "SHORT_LOAN", "短期借款", YUAN),
        ("accounts_payable", "ACCOUNTS_PAYABLE", "应付账款", YUAN),
        (
            "noncurrent_liab_1year",
            "NONCURRENT_LIAB_1YEAR",
            "一年内到期的非流动负债",
            YUAN,
        ),
        ("total_current_liab", "TOTAL_CURRENT_LIAB", "流动负债合计", YUAN),
        ("long_loan", "LONG_LOAN", "长期借款", YUAN),
        ("bond_payable", "BOND_PAYABLE", "应付债券", YUAN),
        ("lease_liab", "LEASE_LIAB", "租赁负债", YUAN),
        ("total_noncurrent_liab", "TOTAL_NONCURRENT_LIAB", "非流动负债合计", YUAN),
        ("share_capital", "SHARE_CAPITAL", "实收资本（股本）", YUAN),
        ("unassign_rpofit", "UNASSIGN_RPOFIT", "未分配利润", YUAN),
    ),
    "income_statement": (
        ("total_operate_income", "TOTAL_OPERATE_INCOME", "营业总收入", YUAN),
        ("operate_income", "OPERATE_INCOME", "营业收入", YUAN),
        ("total_operate_cost", "TOTAL_OPERATE_COST", "营业总成本", YUAN),
        ("operate_cost", "OPERATE_COST", "营业成本", YUAN),
        ("sale_expense", "SALE_EXPENSE", "销售费用", YUAN),
        ("manage_expense", "MANAGE_EXPENSE", "管理费用", YUAN),
        ("research_expense", "RESEARCH_EXPENSE", "研发费用", YUAN),
        ("finance_expense", "FINANCE_EXPENSE", "财务费用", YUAN),
        ("fe_interest_expense", "FE_INTEREST_EXPENSE", "利息费用", YUAN),
        ("invest_income", "INVEST_INCOME", "投资收益", YUAN),
        (
            "invest_joint_income",
            "INVEST_JOINT_INCOME",
            "对联营企业和合营企业的投资收益",
            YUAN,
        ),
        ("operate_profit", "OPERATE_PROFIT", "营业利润", YUAN),
        ("total_profit", "TOTAL_PROFIT", "利润总额", YUAN),
        ("income_tax", "INCOME_TAX", "所得税费用", YUAN),
        ("netprofit", "NETPROFIT", "净利润", YUAN),
        ("parent_netprofit", "PARENT_NETPROFIT", "归属于母公司股东的净利润", YUAN),
        ("minority_interest", "MINORITY_INTEREST", "少数股东损益", YUAN),
        (
            "deduct_parent_netprofit",
            "DEDUCT_PARENT_NETPROFIT",
            "扣除非经常性损益后归属于母公司股东的净利润",
            YUAN,
        ),
        ("basic_eps", "BASIC_EPS", "基本每股收益", "元/股"),
    ),
    "cash_flow": (
        ("sales_services", "SALES_SERVICES", "销售商品、提供劳务收到的现金", YUAN),
        ("total_operate_inflow", "TOTAL_OPERATE_INFLOW", "经营活动现金流入小计", YUAN),
        (
            "total_operate_outflow",
            "TOTAL_OPERATE_OUTFLOW",
            "经营活动现金流出小计",
            YUAN,
        ),
        ("netcash_operate", "NETCASH_OPERATE", "经营活动产生的现金流量净额", YUAN),
        (
            "construct_long_asset",
            "CONSTRUCT_LONG_ASSET",
            "购建固定资产、无形资产和其他长期资产支付的现金",
            YUAN,
        ),
        ("netcash_invest", "NETCASH_INVEST", "投资活动产生的现金流量净额", YUAN),
        (
            "assign_dividend_porfit",
            "ASSIGN_DIVIDEND_PORFIT",
            "分配股利、利润或偿付利息支付的现金",
            YUAN,
        ),
        ("netcash_finance", "NETCASH_FINANCE", "筹资活动产生的现金流量净额", YUAN),
        (
            "rate_change_effect",
            "RATE_CHANGE_EFFECT",
            "汇率变动对现金及现金等价物的影响",
            YUAN,
        ),
        ("cce_add", "CCE_ADD", "现金及现金等价物净增加额", YUAN),
        ("begin_cce", "BEGIN_CCE", "期初现金及现金等价物余额", YUAN),
        ("end_cce", "END_CCE", "期末现金及现金等价物余额", YUAN),
    ),
}


@dataclass(frozen=True)
class ReconciliationRule:
    rule_id: str
    rule: str
    source_table: str
    residual_expression: str  # 残差的写法（给人看），应为零
    terms: tuple[tuple[str, int, bool], ...]  # (字段, 符号, 是否可缺省按零计)


def _rule(rule_id, rule, table, expression, *terms) -> ReconciliationRule:
    return ReconciliationRule(rule_id, rule, table, expression, tuple(terms))


RECONCILIATION_RULES: tuple[ReconciliationRule, ...] = (
    _rule(
        "R01",
        "资产 = 负债 + 所有者权益",
        "balance_sheet",
        "total_assets - (total_liabilities + total_equity)",
        ("total_assets", 1, False),
        ("total_liabilities", -1, False),
        ("total_equity", -1, False),
    ),
    _rule(
        "R02",
        "资产总计 = 负债和所有者权益总计",
        "balance_sheet",
        "total_assets - total_liab_equity",
        ("total_assets", 1, False),
        ("total_liab_equity", -1, False),
    ),
    _rule(
        "R03",
        "所有者权益 = 归母权益 + 少数股东权益",
        "balance_sheet",
        "total_equity - (total_parent_equity + COALESCE(minority_equity,0))",
        ("total_equity", 1, False),
        ("total_parent_equity", -1, False),
        ("minority_equity", -1, True),
    ),
    _rule(
        "R04",
        "流动资产 + 非流动资产 = 资产总计",
        "balance_sheet",
        "total_assets - (total_current_assets + total_noncurrent_assets)",
        ("total_assets", 1, False),
        ("total_current_assets", -1, False),
        ("total_noncurrent_assets", -1, False),
    ),
    _rule(
        "R05",
        "流动负债 + 非流动负债 = 负债合计",
        "balance_sheet",
        "total_liabilities - (total_current_liab + total_noncurrent_liab)",
        ("total_liabilities", 1, False),
        ("total_current_liab", -1, False),
        ("total_noncurrent_liab", -1, False),
    ),
    _rule(
        "R06",
        "净利润 = 归母净利润 + 少数股东损益",
        "income_statement",
        "netprofit - (parent_netprofit + COALESCE(minority_interest,0))",
        ("netprofit", 1, False),
        ("parent_netprofit", -1, False),
        ("minority_interest", -1, True),
    ),
    _rule(
        "R07",
        "净利润 = 利润总额 - 所得税",
        "income_statement",
        "netprofit - (total_profit - COALESCE(income_tax,0))",
        ("netprofit", 1, False),
        ("total_profit", -1, False),
        ("income_tax", 1, True),
    ),
    _rule(
        "R08",
        "现金净增加 = 经营 + 投资 + 筹资 + 汇率影响",
        "cash_flow",
        "cce_add - (netcash_operate + netcash_invest + netcash_finance + "
        "COALESCE(rate_change_effect,0))",
        ("cce_add", 1, False),
        ("netcash_operate", -1, False),
        ("netcash_invest", -1, False),
        ("netcash_finance", -1, False),
        ("rate_change_effect", -1, True),
    ),
    _rule(
        "R09",
        "期末现金 = 期初现金 + 净增加",
        "cash_flow",
        "end_cce - (begin_cce + cce_add)",
        ("end_cce", 1, False),
        ("begin_cce", -1, False),
        ("cce_add", -1, False),
    ),
)
