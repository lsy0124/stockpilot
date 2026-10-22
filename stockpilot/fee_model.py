"""1000元账户交易费用模型（第五阶段新增，任务书4.1节）。

核心原则：
- 费率可配置，股票与ETF分别配置，不得套用同一费率
- 所有费率均为假设值，除非用户明确确认真实券商费率
- 最低5元佣金仅为压力测试假设，不得当作真实费率
- 支持买卖方向差异（印花税仅卖出，过户费双向等）
- 支持费用规则生效日期，按交易日期选取适用配置
"""
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Optional, Dict, List

from stockpilot import timeutil as tu


@dataclass
class FeeConfig:
    """单一券商费率配置。

    Attributes:
        config_id: 配置唯一标识
        name: 配置名称
        source: 数据来源（"assumption"/"official"/"broker_confirmed"）
        effective_date: 生效日期（YYYY-MM-DD），空表示长期有效
        is_etf: 是否为ETF标的
        market: 交易所（"SH"/"SZ"/"BJ"/None）
        commission_rate_buy: 买入佣金比例
        commission_rate_sell: 卖出佣金比例
        commission_min_buy: 买入最低佣金（元）
        commission_min_sell: 卖出最低佣金（元）
        stamp_duty_rate_sell: 卖出印花税比例（ETF免征）
        transfer_fee_rate_buy: 买入过户费比例
        transfer_fee_rate_sell: 卖出过户费比例
        other_fee_rate: 其他费用比例（如经手费等）
        notes: 备注说明
    """
    config_id: str
    name: str
    source: str = "assumption"
    effective_date: Optional[str] = None
    is_etf: bool = False
    market: Optional[str] = None
    commission_rate_buy: float = 0.00025
    commission_rate_sell: float = 0.00025
    commission_min_buy: float = 5.0
    commission_min_sell: float = 5.0
    stamp_duty_rate_sell: float = 0.001
    transfer_fee_rate_buy: float = 0.00001
    transfer_fee_rate_sell: float = 0.00001
    other_fee_rate: float = 0.0
    notes: str = ""

    def is_applicable(self, trade_date: str) -> bool:
        """判断该配置在给定交易日期是否生效。"""
        if not self.effective_date:
            return True
        return trade_date >= self.effective_date


class FeeModelManager:
    """费率配置管理器。

    支持按交易日期查询适用的费率配置，股票与ETF分别管理。
    所有内置配置均为假设值，非真实券商费率。
    """

    def __init__(self):
        self._configs: List[FeeConfig] = []
        self._init_default_configs()

    def _init_default_configs(self):
        """初始化默认费率配置。

        【费率来源分级 —— 必须如实区分】
        - "official"：交易所/税务机关公开标准，可在官网复核
            · 经手费：上交所《收费一览表》A股 0.00341%（双向）
            · 证管费：证监会规定 0.002%（双向）
            · 印花税：2023-08-28 起减半，卖出单边 0.05%（千分之0.5）
            · 过户费：中国结算 0.001%（双向）
        - "assumption"：券商佣金率与最低佣金，属**券商自定**，
          因券商与客户而异，本项目无法从公开渠道确认，必须由用户
          向自己的券商核实后覆盖。

        历史修正记录：早期版本把印花税写成 0.001（千分之一），
        那是 2023-08-28 之前的税率；现行是 0.0005。
        """
        # 交易所/税务官方部分（可复核）
        OFFICIAL_HANDLING = 0.0000341   # 经手费 0.00341%
        OFFICIAL_SUPERVISION = 0.00002  # 证管费 0.002%
        OFFICIAL_STAMP_SELL = 0.0005    # 印花税（2023-08-28 减半后，卖出单边）
        OFFICIAL_TRANSFER = 0.00001     # 过户费 0.001%（双向）
        # 经手费+证管费合并进 other_fee_rate（券商代收，必收）
        OFFICIAL_OTHER = OFFICIAL_HANDLING + OFFICIAL_SUPERVISION

        # 股票：悲观配置（最低佣金5元，压力测试）
        self.add_config(FeeConfig(
            config_id="stock_pessimistic_5yuan",
            name="股票-悲观（最低佣金5元，压力测试）",
            source="assumption",
            effective_date="2023-08-28",
            is_etf=False,
            market=None,
            commission_rate_buy=0.00025,
            commission_rate_sell=0.00025,
            commission_min_buy=5.0,
            commission_min_sell=5.0,
            stamp_duty_rate_sell=OFFICIAL_STAMP_SELL,
            transfer_fee_rate_buy=OFFICIAL_TRANSFER,
            transfer_fee_rate_sell=OFFICIAL_TRANSFER,
            other_fee_rate=OFFICIAL_OTHER,
            notes="印花税/过户费/经手费/证管费为官方标准；"
                  "佣金率与最低佣金为压力测试假设，须向券商核实",
        ))

        # 股票：中等配置（万2，最低佣金5元）
        self.add_config(FeeConfig(
            config_id="stock_moderate_2wan_5yuan",
            name="股票-中等（万2，最低佣金5元）",
            source="assumption",
            effective_date="2023-08-28",
            is_etf=False,
            market=None,
            commission_rate_buy=0.0002,
            commission_rate_sell=0.0002,
            commission_min_buy=5.0,
            commission_min_sell=5.0,
            stamp_duty_rate_sell=OFFICIAL_STAMP_SELL,
            transfer_fee_rate_buy=OFFICIAL_TRANSFER,
            transfer_fee_rate_sell=OFFICIAL_TRANSFER,
            other_fee_rate=OFFICIAL_OTHER,
            notes="印花税/过户费/经手费/证管费为官方标准；"
                  "佣金万2+最低5元为假设，须向券商核实",
        ))

        # 股票：乐观配置（万2.5，无最低佣金）
        self.add_config(FeeConfig(
            config_id="stock_optimistic_2wan5_nomin",
            name="股票-乐观（万2.5，无最低佣金）",
            source="assumption",
            effective_date="2023-08-28",
            is_etf=False,
            market=None,
            commission_rate_buy=0.00025,
            commission_rate_sell=0.00025,
            commission_min_buy=0.0,
            commission_min_sell=0.0,
            stamp_duty_rate_sell=OFFICIAL_STAMP_SELL,
            transfer_fee_rate_buy=OFFICIAL_TRANSFER,
            transfer_fee_rate_sell=OFFICIAL_TRANSFER,
            other_fee_rate=OFFICIAL_OTHER,
            notes="部分券商可协商免五，但未经确认；仅供对比上限",
        ))

        # ETF：悲观配置（最低佣金5元，免印花税）
        self.add_config(FeeConfig(
            config_id="etf_pessimistic_5yuan",
            name="ETF-悲观（最低佣金5元，免印花税）",
            source="assumption",
            effective_date="2023-08-28",
            is_etf=True,
            market=None,
            commission_rate_buy=0.00025,
            commission_rate_sell=0.00025,
            commission_min_buy=5.0,
            commission_min_sell=5.0,
            stamp_duty_rate_sell=0.0,  # ETF 免印花税（官方）
            transfer_fee_rate_buy=OFFICIAL_TRANSFER,
            transfer_fee_rate_sell=OFFICIAL_TRANSFER,
            other_fee_rate=OFFICIAL_OTHER,
            notes="ETF 免印花税为官方规则；佣金假设同股票悲观配置",
        ))

        # ETF：中等配置（万1.5，最低佣金1元）
        self.add_config(FeeConfig(
            config_id="etf_moderate_1wan5_1yuan",
            name="ETF-中等（万1.5，最低佣金1元）",
            source="assumption",
            effective_date="2023-08-28",
            is_etf=True,
            market=None,
            commission_rate_buy=0.00015,
            commission_rate_sell=0.00015,
            commission_min_buy=1.0,
            commission_min_sell=1.0,
            stamp_duty_rate_sell=0.0,
            transfer_fee_rate_buy=OFFICIAL_TRANSFER,
            transfer_fee_rate_sell=OFFICIAL_TRANSFER,
            other_fee_rate=OFFICIAL_OTHER,
            notes="部分券商 ETF 佣金较低且最低1元，未经确认",
        ))

    def add_config(self, config: FeeConfig):
        """添加一条费率配置。"""
        self._configs.append(config)

    def get_applicable(self, is_etf: bool, trade_date: str,
                       market: Optional[str] = None) -> Optional[FeeConfig]:
        """查找给定标的和交易日期的适用费率配置。

        优先匹配：is_etf + market + effective_date <= trade_date 的最新配置。
        若无精确market匹配，则尝试market=None的配置。
        """
        candidates = [c for c in self._configs
                      if c.is_etf == is_etf and c.is_applicable(trade_date)]
        if not candidates:
            return None
        # 按生效日期降序，取最新
        candidates.sort(key=lambda c: c.effective_date or "1970-01-01", reverse=True)
        # 优先匹配market，其次返回任意
        for c in candidates:
            if c.market == market or c.market is None:
                return c
        return candidates[0]

    def list_configs(self) -> List[FeeConfig]:
        return list(self._configs)


# 全局费率管理器实例（默认使用假设配置）
fee_manager = FeeModelManager()


def calc_fee_breakdown(price: float, shares: int, is_etf: bool,
                       config: FeeConfig,
                       direction: str = "buy") -> dict:
    """计算单笔交易的详细费用。

    Args:
        price: 成交价格
        shares: 交易股数
        is_etf: 是否为ETF
        config: 费率配置
        direction: "buy" 或 "sell"

    Returns:
        字典，包含金额、佣金、印花税、过户费、其他费用、总费用等
    """
    amount = round(price * shares, 2)

    if direction == "buy":
        commission = max(amount * config.commission_rate_buy,
                         config.commission_min_buy)
        stamp_duty = 0.0  # 买入不收印花税
        transfer_fee = amount * config.transfer_fee_rate_buy
        other_fee = amount * config.other_fee_rate
    elif direction == "sell":
        commission = max(amount * config.commission_rate_sell,
                         config.commission_min_sell)
        stamp_duty = amount * config.stamp_duty_rate_sell  # ETF为0
        transfer_fee = amount * config.transfer_fee_rate_sell
        other_fee = amount * config.other_fee_rate
    else:
        raise ValueError(f"未知交易方向：{direction}")

    total_fee = round(commission + stamp_duty + transfer_fee + other_fee, 2)

    # 为兼容历史调用方（ledger.calc_fees 曾单独返回这两项），
    # 把 other_fee 拆出可读的经手费与证管费。
    handling_fee = amount * 0.0000341 * (1 if config.other_fee_rate else 0)
    supervision_fee = amount * 0.00002 * (1 if config.other_fee_rate else 0)

    return {
        "direction": direction,
        "amount": round(amount, 2),
        "commission": round(commission, 2),
        "stamp_duty": round(stamp_duty, 2),
        "transfer_fee": round(transfer_fee, 2),
        "handling_fee": round(handling_fee, 2),
        "supervision_fee": round(supervision_fee, 2),
        "other_fee": round(other_fee, 2),
        "total_fee": total_fee,
        "config_id": config.config_id,
        "config_name": config.name,
        "source": config.source,
        "fee_model_source": config.source,
    }


DEFAULT_INITIAL_CAPITAL = 1000.0


def calc_round_trip(price: float, shares: int, is_etf: bool,
                    config: FeeConfig,
                    initial_capital: float = DEFAULT_INITIAL_CAPITAL) -> dict:
    """计算完整往返成本（买入+卖出）。

    Args:
        price: 成交价格
        shares: 交易股数
        is_etf: 是否为ETF
        config: 费率配置
        initial_capital: 账户初始资金，仅用于计算"占本金比例"。
            默认 1000.0（本账户规模）；不再隐式硬编码。

    Returns:
        字典，包含买入成本、卖出成本、往返总成本、占初始资金比例、
        占成交金额比例、盈亏平衡所需价格变化等
    """
    buy = calc_fee_breakdown(price, shares, is_etf, config, "buy")
    sell = calc_fee_breakdown(price, shares, is_etf, config, "sell")

    total_fee = buy["total_fee"] + sell["total_fee"]
    trade_amount = buy["amount"] + sell["amount"]  # 双边成交金额

    return {
        "price": price,
        "shares": shares,
        "is_etf": is_etf,
        "config_id": config.config_id,
        "config_name": config.name,
        "source": config.source,
        "initial_capital": initial_capital,
        "buy": buy,
        "sell": sell,
        "round_trip_total": round(total_fee, 2),
        "trade_amount": round(trade_amount, 2),
        "pct_of_initial_capital": round(total_fee / initial_capital * 100, 3)
        if initial_capital > 0 else 0.0,
        "pct_of_trade_amount": round(total_fee / trade_amount * 100, 3) if trade_amount > 0 else 0.0,
        "breakeven_pct": round(total_fee / buy["amount"] * 100, 3) if buy["amount"] > 0 else 0.0,
        "note": "最低佣金5元仅为压力测试假设，非真实券商费率",
    }


def batch_scenarios(price: float, shares: int, is_etf: bool,
                    manager: FeeModelManager,
                    trade_date: str,
                    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
                    market: Optional[str] = None) -> List[dict]:
    """对给定标的，计算所有适用费率配置下的场景结果。

    用于多场景敏感性测试。使用公开的 list_configs()，不再访问私有属性。
    market 参与市场匹配（与 get_applicable 规则一致）。
    """
    results = []
    for config in manager.list_configs():
        if config.is_etf != is_etf or not config.is_applicable(trade_date):
            continue
        if market is not None and config.market not in (market, None):
            continue
        results.append(calc_round_trip(price, shares, is_etf, config,
                                       initial_capital=initial_capital))
    return results


if __name__ == "__main__":
    # 自测
    mgr = FeeModelManager()
    cfg = mgr.get_applicable(is_etf=False, trade_date="2026-10-18")
    print("适用配置:", cfg.config_id, cfg.name)
    rt = calc_round_trip(7.51, 100, False, cfg)
    print(json.dumps(rt, indent=2, ensure_ascii=False))