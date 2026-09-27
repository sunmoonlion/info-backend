"""财务数据的目录：字段字典、关键科目、口径表、勾稽规则。

这是我们自建的数据（0008-info），不是采来的。它决定数据集里有哪些字段、每个指标
怎么算、什么情况下不适用、哪些等式必须成立。改动这里等于改数据集的含义，要走评审。
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

# 行上的元数据字段
ROW_FIELDS: tuple[tuple[str, str], ...] = (
    ("security_code", "证券代码"),
    ("report_date", "报告期末日"),
    ("report_type", "报告类型：年报、中报、一季报、三季报"),
    ("fiscal_year", "会计年度"),
    ("basis", "口径：原始披露、追溯调整后、未核实"),
    ("verified", "是否已与法定披露原文核对（1 是 0 否）"),
    ("verified_against", "核对所依据的年度报告"),
    (
        "aggregator_notice_date",
        "第三方数据里的公告日，不是法定披露日，不得当作披露时点",
    ),
    ("currency", "币种"),
)

REPORT_TYPES = ("年报", "中报", "一季报", "三季报")


@dataclass(frozen=True)
class KeyItem:
    """年度报告「主要会计数据」里的科目，用来判口径和核对。"""

    item: str
    table: str
    field: str
    label: str  # 年报里的叫法（去掉空白与换行之后）


KEY_ITEMS: tuple[KeyItem, ...] = (
    KeyItem("operate_income", "income_statement", "operate_income", "营业收入"),
    KeyItem("total_profit", "income_statement", "total_profit", "利润总额"),
    KeyItem(
        "parent_netprofit",
        "income_statement",
        "parent_netprofit",
        "归属于上市公司股东的净利润",
    ),
    KeyItem(
        "deduct_parent_netprofit",
        "income_statement",
        "deduct_parent_netprofit",
        "归属于上市公司股东的扣除非经常性损益的净利润",
    ),
    KeyItem(
        "netcash_operate", "cash_flow", "netcash_operate", "经营活动产生的现金流量净额"
    ),
    KeyItem(
        "total_parent_equity",
        "balance_sheet",
        "total_parent_equity",
        "归属于上市公司股东的净资产",
    ),
    KeyItem("total_assets", "balance_sheet", "total_assets", "总资产"),
)


RATIO = "比率"
TIMES = "倍"
NEAR_ZERO = "为负或在一元以内时不适用"


@dataclass(frozen=True)
class Metric:
    """一个口径。前七项是给人看的说明；后面几项是可执行的定义（数据集自述第二版）。

    可执行的定义是逐行的：基础表的一行（一家公司的一个报告期）算出一个值，不做汇总。
    表达式只引用基础表的字段；引用有关系的表写成「表名.字段」，关系见 TABLE_LINKS。
    queryable 为假的口径只有说明，一行表达不了（例如要用上一期的数），由使用方按说明自己算。
    """

    metric_name: str
    display_name: str
    source_table: str
    expression_hint: str
    unit: str
    time_basis: str
    description: str
    base_table: str | None = None
    value_expression: str | None = None
    applicable_when: str | None = None
    reason_if_not: str | None = None

    @property
    def queryable(self) -> bool:
        return self.base_table is not None and self.value_expression is not None


METRICS: tuple[Metric, ...] = (
    Metric(
        "gross_margin",
        "毛利率",
        "income_statement",
        "(operate_income - operate_cost) / operate_income",
        RATIO,
        "报告期",
        "营业收入减营业成本，除以营业收入；0.25 即 25%",
        "income_statement",
        "(operate_income - operate_cost) / operate_income",
        "operate_income > 1",
        "营业收入" + NEAR_ZERO,
    ),
    Metric(
        "net_margin",
        "净利率",
        "income_statement",
        "netprofit / operate_income",
        RATIO,
        "报告期",
        "净利润除以营业收入；0.25 即 25%",
        "income_statement",
        "netprofit / operate_income",
        "operate_income > 1",
        "营业收入" + NEAR_ZERO,
    ),
    Metric(
        "deduct_ratio",
        "扣非净利润占比",
        "income_statement",
        "deduct_parent_netprofit / parent_netprofit",
        RATIO,
        "报告期",
        "扣非归母净利润除以归母净利润；归母净利润为负或接近零时不适用",
        "income_statement",
        "deduct_parent_netprofit / parent_netprofit",
        "parent_netprofit > 1",
        "归母净利润" + NEAR_ZERO,
    ),
    Metric(
        "invest_income_share",
        "投资收益占营业利润比",
        "income_statement",
        "invest_income / operate_profit",
        RATIO,
        "报告期",
        "投资收益除以营业利润；营业利润为负或接近零时不适用",
        "income_statement",
        "invest_income / operate_profit",
        "operate_profit > 1",
        "营业利润" + NEAR_ZERO,
    ),
    Metric(
        "roe_avg",
        "净资产收益率（平均）",
        "income_statement+balance_sheet",
        "parent_netprofit / ((期初归母权益 + 期末归母权益) / 2)",
        RATIO,
        "年度",
        "简单平均口径，与年报披露的加权平均口径不同；期初期末口径不一致时不适用",
    ),
    Metric(
        "debt_ratio",
        "资产负债率",
        "balance_sheet",
        "total_liabilities / total_assets",
        RATIO,
        "期末",
        "负债合计除以资产总计；0.25 即 25%",
        "balance_sheet",
        "total_liabilities / total_assets",
        "total_assets > 1",
        "资产总计" + NEAR_ZERO,
    ),
    Metric(
        "interest_bearing_debt",
        "有息负债（含租赁）",
        "balance_sheet",
        "COALESCE(short_loan,0) + COALESCE(noncurrent_liab_1year,0) + "
        "COALESCE(long_loan,0) + COALESCE(bond_payable,0) + COALESCE(lease_liab,0)",
        YUAN,
        "期末",
        "空值按零计；一年内到期的非流动负债可能含无息部分，是上限口径",
        "balance_sheet",
        "COALESCE(short_loan,0) + COALESCE(noncurrent_liab_1year,0) + "
        "COALESCE(long_loan,0) + COALESCE(bond_payable,0) + COALESCE(lease_liab,0)",
    ),
    Metric(
        "current_ratio",
        "流动比率",
        "balance_sheet",
        "total_current_assets / total_current_liab",
        TIMES,
        "期末",
        "流动资产除以流动负债",
        "balance_sheet",
        "total_current_assets / total_current_liab",
        "total_current_liab > 1",
        "流动负债" + NEAR_ZERO,
    ),
    Metric(
        "ocf_to_netprofit",
        "经营现金流与净利润之比",
        "cash_flow+income_statement",
        "netcash_operate / netprofit",
        TIMES,
        "年度",
        "净利润为负时不适用；2021 年起执行新租赁准则的公司，租金支付计入筹资活动，"
        "前后期间不可直接比较",
        "cash_flow",
        "netcash_operate / income_statement.netprofit",
        "income_statement.netprofit > 1",
        "净利润" + NEAR_ZERO,
    ),
    Metric(
        "free_cash_flow",
        "自由现金流（简化）",
        "cash_flow",
        "netcash_operate - construct_long_asset",
        YUAN,
        "年度",
        "经营现金流净额减购建长期资产支付的现金；未扣租赁本金偿付，对租赁负债大的公司会高估",
        "cash_flow",
        "netcash_operate - construct_long_asset",
    ),
)


@dataclass(frozen=True)
class TableLink:
    """两张表的行怎么对上。on_columns 是两边同名的字段。"""

    link_name: str
    from_table: str
    to_table: str
    cardinality: str
    on_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableKey:
    """一张表的一行由哪些字段确定（key），再带哪些字段才看得懂这一行（label）。"""

    table_name: str
    key_columns: tuple[str, ...]
    label_columns: tuple[str, ...]


_PERIOD = ("security_code", "report_date")
_PERIOD_LABELS = ("report_type", "fiscal_year", "basis", "verified")

TABLE_KEYS: tuple[TableKey, ...] = tuple(
    TableKey(statement, _PERIOD, _PERIOD_LABELS) for statement in STATEMENT_FIELDS
)

# 同一家公司同一个报告期的三张表互相对得上
TABLE_LINKS: tuple[TableLink, ...] = tuple(
    TableLink(f"{a}_to_{b}", a, b, "one_to_one", _PERIOD)
    for a in STATEMENT_FIELDS
    for b in STATEMENT_FIELDS
    if a != b
)


@dataclass(frozen=True)
class ReconciliationRule:
    rule_id: str
    rule: str
    source_table: str
    residual_expression: str  # 数据集所用 SQL 方言下的残差表达式，应为零
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

# 容差：一元。年报以元为单位、保留到分；累加的舍入误差远小于一元。
TOLERANCE_YUAN = 1.0

BASIS_ORIGINAL = "原始披露"
BASIS_RESTATED = "追溯调整后"
BASIS_UNVERIFIED = "未核实"
