"""模拟投资闭环（第五阶段重写，任务书第十一章）。

独立 1000 元模拟账户（data/simulated.db），与真实账本完全隔离。

流程：
1. GPT-6 提出模拟建议（sim_advice，可来自交接包的 decision 记录）
2. 用户确认模拟委托（sim_order）
3. 系统依据实际市场数据与明确成交规则判断是否成交
   - 委托价在当日日线价格区间内 → "assumed_daily"（基于日线的假设成交，保守）
   - 委托价 < 当日最低价（买入）或 > 当日最高价（卖出） → "not_met"（当日不成交）
   - 缺少足够行情数据 → "unknown"（不假设成交）
   - "confirmed"（已确认真实成交）：仅用于真实券商回传，本模拟系统不产生
4. 记录模拟成交价格/数量/费用（含 T+1 检查）
5. 更新模拟账户
6. 生成持仓与盈亏报告
7. 观察期后自动复盘

硬规则：
- 不得把委托价直接当真实成交价（必须经过成交规则检验）
- 日内最低价低于买入限价，不能直接证明实际委托成交（买入方向）
- 数据不足时标记 "unknown"，不强行假设成交
- 模拟记录不得标记为真实成交（source 字段固定 "simulated"）
- T+1：当日买入不可当日卖出
- 滑点：成交价可按滑点调整（买入向上、卖出向下，默认0）
"""

import datetime as dt
import json
import math
import operator
import os
import re
import sqlite3

from stockpilot import DATA, audit, iso, save_json, setup_logger
from stockpilot import fee_model
from stockpilot import timeutil as tu
from stockpilot.collectors import quote as q

log = setup_logger("simulation", "simulation.log")

SIM_DB = os.path.join(DATA, "simulated.db")
INITIAL_CASH = 1000.0
LOT = 100
SLIPPAGE_RATE = 0.0005  # 默认滑点0.05%，买入向上、卖出向下

# 费率唯一来源：fee_model（股票/ETF 分档、买卖方向差异）。
# 模拟成交默认采用"悲观（最低佣金5元，压力测试假设）"配置，
# 与交接包 fee_model.json 中的假设保持一致，避免费率双份实现漂移。
SIM_FEE_CONFIG_ID_STOCK = "stock_pessimistic_5yuan"
SIM_FEE_CONFIG_ID_ETF = "etf_pessimistic_5yuan"

# 成交状态分级（任务书第六章 6.1）
STATUS_CONFIRMED = "confirmed"      # 已确认真实成交（模拟环境不产生）
STATUS_SIMULATED_EVIDENCE = "simulated_evidence"  # 基于足够行情证据的模拟成交（如分钟线）
STATUS_ASSUMED_DAILY = "assumed_daily"   # 基于日线价格范围的假设成交（当前主情况）
STATUS_NOT_MET = "not_met"              # 成交条件不满足
STATUS_UNKNOWN = "unknown"              # 数据不足，成交状态未知

SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_account (
    id INTEGER PRIMARY KEY CHECK (id=1), cash REAL NOT NULL, initial_capital REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_advice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL,
    code TEXT, name TEXT, price REAL, shares INTEGER, reason TEXT,
    handoff_id TEXT, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS sim_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, advice_id INTEGER, submitted_by TEXT NOT NULL,
    action TEXT NOT NULL, code TEXT NOT NULL, name TEXT, price REAL NOT NULL,
    shares INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'submitted',
    fill_price REAL, filled_at TEXT, fill_rule TEXT, fill_status TEXT, fee REAL
);
CREATE TABLE IF NOT EXISTS sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filled_at TEXT NOT NULL, trade_date TEXT, order_id INTEGER, code TEXT NOT NULL, name TEXT,
    action TEXT NOT NULL, price REAL NOT NULL, shares INTEGER NOT NULL,
    amount REAL NOT NULL, commission REAL, stamp_duty REAL, transfer_fee REAL,
    total_fee REAL, source TEXT NOT NULL DEFAULT 'simulated',
    fill_status TEXT NOT NULL DEFAULT 'assumed_daily'
);
CREATE TABLE IF NOT EXISTS sim_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT
);
"""



def connect(path=SIM_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db(path=SIM_DB):
    con = connect(path)
    con.executescript(SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(sim_trades)")}
    if "trade_date" not in columns:
        con.execute("ALTER TABLE sim_trades ADD COLUMN trade_date TEXT")
    con.execute("INSERT OR IGNORE INTO sim_account(id,cash,initial_capital) VALUES(1,?,?)",
                (INITIAL_CASH, INITIAL_CASH))
    con.commit()
    _audit(con, "sim_db_initialized", {"path": path, "initial_cash": INITIAL_CASH})
    con.commit()
    con.close()
    return path


def _audit(con, event, detail):
    con.execute("INSERT INTO sim_audit(at,event,detail) VALUES(?,?,?)",
                (iso(), event, json.dumps(detail, ensure_ascii=False, default=str)))


def sim_advice(source, action, code, name=None, price=None, shares=None, reason=None,
               handoff_id=None, path=SIM_DB):
    """步骤1：模拟投资建议"""
    con = connect(path)
    cur = con.execute(
        "INSERT INTO sim_advice(created_at,source,action,code,name,price,shares,reason,handoff_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (iso(), source, action, code, name, price, shares, reason, handoff_id))
    con.commit()
    aid = cur.lastrowid
    _audit(con, "sim_advice_created", {"id": aid, "code": code, "action": action})
    con.commit()
    con.close()
    audit("sim_advice_created", id=aid, code=code, action=action)
    return aid


def sim_order(advice_id, action, code, name=None, price=0, shares=0,
              submitted_by="user", path=SIM_DB):
    """步骤2：用户确认模拟委托"""
    price, shares = _validate_order(action, code, price, shares)
    con = connect(path)
    cur = con.execute(
        "INSERT INTO sim_orders(created_at,advice_id,submitted_by,action,code,name,price,shares)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (iso(), advice_id, submitted_by, action, code, name, price, shares))
    con.commit()
    oid = cur.lastrowid
    _audit(con, "sim_order_submitted", {"id": oid, "code": code, "price": price})
    con.commit()
    con.close()
    audit("sim_order_submitted", id=oid, code=code, price=price, shares=shares)
    return oid


def fetch_bar_for_rule(code, trade_date):
    """取委托判断所需的价格数据：当日/最近的 OHLC"""
    full = code if code.startswith(("sh", "sz")) else (
        f"sh{code}" if code.startswith(("6", "5", "9")) else f"sz{code}")
    bars, meta = q.sina_daily(full, datalen=5)
    if not bars:
        return None, meta
    # 取委托日当日 bar；若当日无数据（非交易日/停牌），取最近一根
    bar = None
    for b in bars:
        if b["date"] == trade_date:
            bar = b
            break
    if bar is None:
        meta = dict(meta or {})
        meta["note"] = f"委托日 {trade_date} 无数据，成交状态保持 unknown；不使用其他日期替代"
    return bar, meta


def detect_price_limit(code, name=None):
    """返回该标的的涨跌停幅度（小数），无法判定时返回 None。

    规则（A 股现行）：
      - ST / *ST：±5%
      - 科创板 688 / 创业板 300：±20%
      - 北交所 8xx / 4xx / 920：±30%
      - 其余主板：±10%
    """
    n = (name or "").upper()
    if n.startswith(("ST", "*ST")) or n.startswith("S*ST"):
        return 0.05
    m = _CODE_RE.match(str(code or "").strip())
    if not m:
        return None
    c = m.group(1)
    if c.startswith("688") or c.startswith("300") or c.startswith("301"):
        return 0.20
    if c.startswith(("8", "4")) or c.startswith("920"):
        return 0.30
    if c.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return 0.10
    return None


def is_limit_locked(bar, prev_close, limit_pct):
    """判断当日是否一字涨跌停（无法成交）。

    一字板特征：最高价 == 最低价，且价格贴在涨跌停价上。
    此时无论委托价如何，当日都不可能成交。
    """
    if not bar or prev_close in (None, 0) or not limit_pct:
        return False, None
    hi, lo = bar.get("high"), bar.get("low")
    if hi is None or lo is None:
        return False, None
    if round(hi, 4) != round(lo, 4):
        return False, None
    up = round(prev_close * (1 + limit_pct), 2)
    down = round(prev_close * (1 - limit_pct), 2)
    if abs(hi - up) < 0.005:
        return True, "涨停"
    if abs(lo - down) < 0.005:
        return True, "跌停"
    return False, None


def apply_slippage(price, action, slippage_rate=None):
    """按方向施加滑点：买入向上、卖出向下。

    原实现中 SLIPPAGE_RATE 仅定义未被调用（滑点实际未生效）。
    这里显式应用：买入成交价上浮、卖出成交价下调，均按比例。
    """
    rate = SLIPPAGE_RATE if slippage_rate is None else slippage_rate
    if not rate:
        return price
    if action == "buy":
        return round(price * (1 + rate), 4)
    if action == "sell":
        return round(price * (1 - rate), 4)
    return price


def fetch_prev_close(code, trade_date):
    """取委托日之前最近一根日线的收盘价，作为涨跌停判定基准（前收）。

    取不到返回 None；调用方不得因此假设涨跌停。
    """
    full = code if code.startswith(("sh", "sz")) else (
        f"sh{code}" if code.startswith(("6", "5", "9")) else f"sz{code}")
    bars, _meta = q.sina_daily(full, datalen=10)
    if not bars:
        return None
    before = [b for b in bars if b["date"] < trade_date]
    if before:
        return before[-1]["close"]
    return None


def _order_field(order, key, default=None):
    """兼容 dict 与 sqlite3.Row 的字段读取（sqlite3.Row 无 .get）。"""
    try:
        v = order[key]
        return default if v is None else v
    except (KeyError, IndexError, TypeError):
        return default


def evaluate_fill(order, bar, buy_dates, prev_close=None, slippage_rate=None):
    """步骤3：依据明确成交规则判断是否成交。

    成交状态分级（任务书第六章 6.1）：
      - "confirmed"：已确认真实成交（模拟环境不产生，仅保留接口）
      - "simulated_evidence"：基于足够行情证据的模拟成交（如有分钟线/盘口）
      - "assumed_daily"：基于日线价格范围的假设成交（当前主情况）
      - "not_met"：成交条件不满足
      - "unknown"：数据不足，成交状态未知

    规则（保守，禁止仅凭日线高低点断言必然成交）：
      - 一字涨跌停：直接 not_met（当日无法成交）
      - 买入：委托价 ≥ 当日最低价 → assumed_daily，否则 not_met
      - 卖出：委托价 ≤ 当日最高价 → assumed_daily，否则 not_met
      - T+1：当日买入的股份当日不可卖出
      - 数据缺失：unknown，不假设成交
      - 成交价施加滑点（买入上浮、卖出下调），费用按滑点后价格计算
    返回 (status, fill_price, rule_text)
    """
    if bar is None:
        return STATUS_UNKNOWN, None, "缺少当日价格数据，无法判断成交，不假设成交"
    try:
        price, _shares = _validate_order(order["action"], order["code"], order["price"], order["shares"])
        high, low = float(bar["high"]), float(bar["low"])
        if not all(math.isfinite(v) and v > 0 for v in (high, low)) or high < low:
            raise ValueError("invalid OHLC")
        dt.date.fromisoformat(bar["date"])
        bar = dict(bar, high=high, low=low)
        if prev_close is not None:
            prev_close = float(prev_close)
            if not math.isfinite(prev_close) or prev_close <= 0:
                raise ValueError("invalid previous close")
        rate = SLIPPAGE_RATE if slippage_rate is None else float(slippage_rate)
        if not math.isfinite(rate) or not 0 <= rate < 1:
            raise ValueError("invalid slippage")
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return STATUS_UNKNOWN, None, "委托或日线字段无效，无法判断，不假设成交"

    # 涨跌停一字板：任何方向都无法成交
    limit_pct = detect_price_limit(_order_field(order, "code"),
                                   _order_field(order, "name"))
    locked, which = is_limit_locked(bar, prev_close, limit_pct)
    if locked:
        return STATUS_NOT_MET, None, (
            f"{which}一字板（{bar['date']}，最高=最低={bar['high']}），"
            f"当日无法成交")

    if order["action"] == "buy":
        if price >= low:
            fill = apply_slippage(price, "buy", rate)
            return STATUS_ASSUMED_DAILY, fill, (
                f"买入委托价 {price} ≥ 当日最低 {bar['low']}（{bar['date']}），"
                f"按委托价+滑点 {fill} 成交（假设成交，基于日线范围）")
        return STATUS_NOT_MET, None, (
            f"买入委托价 {price} < 当日最低 {bar['low']}（{bar['date']}），"
            f"当日不成交")
    if order["action"] == "sell":
        # T+1 检查
        if order["code"] in buy_dates and buy_dates[order["code"]] == bar["date"]:
            return STATUS_NOT_MET, None, (
                f"T+1 限制：{order['code']} 当日买入不可当日卖出"
                f"（{bar['date']}）")
        if price <= high:
            fill = apply_slippage(price, "sell", rate)
            return STATUS_ASSUMED_DAILY, fill, (
                f"卖出委托价 {price} ≤ 当日最高 {bar['high']}（{bar['date']}），"
                f"按委托价-滑点 {fill} 成交（假设成交，基于日线范围）")
        return STATUS_NOT_MET, None, (
            f"卖出委托价 {price} > 当日最高 {bar['high']}（{bar['date']}），"
            f"当日不成交")
    return STATUS_UNKNOWN, None, f"未知委托类型 {order['action']}"


def resolve_fee_config(code, name=None, trade_date=None, config_id=None):
    """为模拟成交选择费率配置（统一走 fee_model）。

    返回 (is_etf, FeeConfig)。默认按资产类别显式选取"悲观（最低佣金5元）"
    压力测试配置，不依赖 get_applicable() 的排序稳定性：
      - 股票 → SIM_FEE_CONFIG_ID_STOCK
      - ETF  → SIM_FEE_CONFIG_ID_ETF
    若显式 config_id 不存在或与资产类别不符，则回退到 get_applicable()，
    绝不使用内联常量。
    """
    is_etf = is_etf_code(code) or (name or "").upper().endswith("ETF")
    trade_date = trade_date or tu.today().isoformat()
    want = config_id or (SIM_FEE_CONFIG_ID_ETF if is_etf else SIM_FEE_CONFIG_ID_STOCK)

    cfg = None
    for c in fee_model.fee_manager.list_configs():
        if c.config_id == want and c.is_etf == is_etf and c.is_applicable(trade_date):
            cfg = c
            break
    if cfg is None:
        cfg = fee_model.fee_manager.get_applicable(is_etf=is_etf, trade_date=trade_date)
    if cfg is None:
        raise ValueError(f"无适用费率配置：is_etf={is_etf} date={trade_date}")
    return is_etf, cfg


_CODE_RE = re.compile(r"^(?:(?:sh|sz|bj))?(\d{6})$", re.I)


def _validate_order(action, code, price, shares):
    if action not in ("buy", "sell") or not _CODE_RE.fullmatch(str(code or "").strip()):
        raise ValueError("模拟委托必须有有效代码和 buy/sell 方向")
    if isinstance(price, bool) or isinstance(shares, bool):
        raise ValueError("价格和数量不能是布尔值")
    try:
        price = float(price)
        shares = operator.index(shares)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("价格或整数数量无效") from exc
    if not math.isfinite(price) or price <= 0 or shares <= 0:
        raise ValueError("价格和整数数量必须为有限正数")
    return price, shares


def is_etf_code(code):
    """按 A 股代码规则判定 ETF/基金。

    沪市 5xxxxx（510300/588000 等）、深市 15x/16x/18x（159915 等）为基金；
    沪市 600/601/603/605/688、深市 000/001/002/300 等为股票。
    """
    m = _CODE_RE.match(str(code or "").strip())
    if not m:
        return False
    c = m.group(1)
    return c.startswith(("5", "15", "16", "18"))


def process_order(order_id, trade_date=None, path=SIM_DB):
    """步骤3+4：评估并执行成交（含费用与 T+1）"""
    trade_date = trade_date or tu.today().isoformat()
    dt.date.fromisoformat(trade_date)
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        o = con.execute("SELECT * FROM sim_orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise ValueError(f"模拟委托 {order_id} 不存在")
        if o["status"] in ("filled", "cancelled", "rejected"):
            return {"status": o["status"], "note": "已处理过的委托"}
    
        # T+1 支持数据：按"行情交易日"记录买入（filled_at 的日期部分）
        buy_dates = {}
        for r in con.execute("SELECT code, filled_at, trade_date FROM sim_trades WHERE action='buy' ORDER BY id").fetchall():
            buy_dates[r["code"]] = r["trade_date"] or r["filled_at"][:10]
    
        bar, meta = fetch_bar_for_rule(o["code"], trade_date)
        meta = dict(meta or {})
        if bar is not None and bar.get("date") != trade_date:
            bar = None  # Defense in depth: never fill against a different market date.
        # 取前收用于涨跌停判定（无法取得时传 None，不阻断流程）
        prev_close = None
        try:
            prev_close = fetch_prev_close(o["code"], trade_date)
        except Exception as e:
            meta["prev_close_note"] = f"前收取值失败：{e}"
        status, fill_price, rule = evaluate_fill(
            o, bar, buy_dates, prev_close=prev_close)
    
        if status == STATUS_ASSUMED_DAILY:
            amount = round(fill_price * o["shares"], 2)
            # 费率与费用计算统一走 fee_model（唯一来源，避免与账本费率漂移）
            is_etf, cfg = resolve_fee_config(o["code"], o["name"], trade_date)
            fb = fee_model.calc_fee_breakdown(fill_price, o["shares"], is_etf, cfg, o["action"])
            commission, stamp, transfer, fee = (
                fb["commission"], fb["stamp_duty"], fb["transfer_fee"], fb["total_fee"])
            cash = con.execute("SELECT cash FROM sim_account WHERE id=1").fetchone()["cash"]
            if o["action"] == "buy":
                if amount + fee > cash:
                    con.execute("UPDATE sim_orders SET status='rejected' WHERE id=?", (order_id,))
                    _audit(con, "sim_order_rejected",
                           {"id": order_id, "reason": "模拟资金不足"})
                    con.commit()
                    return {"status": "rejected", "note": "模拟资金不足"}
                con.execute("UPDATE sim_account SET cash=cash-? WHERE id=1",
                            (amount + fee,))
            else:
                held = con.execute(
                    "SELECT COALESCE(SUM(CASE WHEN action='buy' THEN shares ELSE -shares END),0) AS s"
                    " FROM sim_trades WHERE code=?", (o["code"],)).fetchone()["s"]
                if held < o["shares"]:
                    con.execute("UPDATE sim_orders SET status='rejected' WHERE id=?", (order_id,))
                    _audit(con, "sim_order_rejected", {"id": order_id, "reason": "持仓不足"})
                    con.commit()
                    return {"status": "rejected", "note": "持仓不足"}
                con.execute("UPDATE sim_account SET cash=cash+? WHERE id=1",
                            (amount - fee,))
            con.execute(
                "INSERT INTO sim_trades(filled_at,order_id,code,name,action,price,shares,amount,"
                "commission,stamp_duty,transfer_fee,total_fee,source,fill_status,trade_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iso(), order_id, o["code"], o["name"], o["action"], fill_price, o["shares"],
                 amount, commission, stamp, transfer, fee, "simulated", STATUS_ASSUMED_DAILY, trade_date))
            con.execute("UPDATE sim_orders SET status='filled', fill_price=?, filled_at=?,"
                        " fill_rule=?, fee=?, fill_status=? WHERE id=?",
                        (fill_price, iso(), rule, fee, STATUS_ASSUMED_DAILY, order_id))
            _audit(con, "sim_order_filled",
                   {"id": order_id, "price": fill_price, "shares": o["shares"], "fee": fee,
                    "rule": rule, "fill_status": STATUS_ASSUMED_DAILY})
            con.commit()
            result = {"status": STATUS_ASSUMED_DAILY, "fill_price": fill_price, "fee": fee, "rule": rule}
        else:
            con.execute("UPDATE sim_orders SET status=?, fill_rule=? WHERE id=?",
                        (status, rule, order_id))
            _audit(con, "sim_order_unfilled", {"id": order_id, "status": status, "rule": rule})
            con.commit()
            result = {"status": status, "rule": rule}
        audit("sim_order_processed", id=order_id, status=result["status"])
        return result
    finally:
        con.close()



def sim_portfolio(path=SIM_DB, prices=None):
    """步骤6：模拟账户持仓与盈亏报告"""
    con = connect(path)
    row = con.execute("SELECT cash, initial_capital FROM sim_account WHERE id=1").fetchone()
    cash, initial = row["cash"], row["initial_capital"]
    pos = []
    for r in con.execute(
            "SELECT code, name, SUM(CASE WHEN action='buy' THEN shares ELSE -shares END) AS shares,"
            " SUM(CASE WHEN action='buy' THEN amount ELSE -amount END) AS net,"
            " SUM(total_fee) AS fees FROM sim_trades GROUP BY code, name"
            " HAVING SUM(CASE WHEN action='buy' THEN shares ELSE -shares END) > 0").fetchall():
        p = dict(r)
        price = (prices or {}).get(p["code"])
        p["current_price"] = price
        cost = p["net"] + p["fees"]
        p["market_value"] = round(price * p["shares"], 2) if price else None
        p["unrealized_pnl"] = round(p["market_value"] - cost, 2) if price else None
        pos.append(p)
    fees_paid = con.execute("SELECT COALESCE(SUM(total_fee),0) AS v FROM sim_trades").fetchone()["v"]
    trades = con.execute("SELECT COUNT(*) AS c FROM sim_trades").fetchone()["c"]
    con.close()
    pos_val = round(sum(p["market_value"] for p in pos if p.get("market_value")), 2)
    unrealized = round(sum(p["unrealized_pnl"] for p in pos
                           if p.get("unrealized_pnl") is not None), 2)
    total = round(cash + pos_val, 2)
    return {
        "as_of": iso(), "kind": "simulated", "initial_capital": initial,
        "cash": round(cash, 2), "positions": pos, "positions_value": pos_val,
        "fees_paid": round(fees_paid, 2), "unrealized_pnl": unrealized,
        "total_assets": total, "net_return_pct": round((total - initial) / initial * 100, 3),
        "trade_count": trades,
        "note": "模拟账户，独立于真实账本；所有成交均标注 source=simulated",
    }


def cancel_sim_order(order_id, path=SIM_DB):
    con = connect(path)
    o = con.execute("SELECT status FROM sim_orders WHERE id=?", (order_id,)).fetchone()
    if not o or o["status"] not in ("submitted", STATUS_NOT_MET, STATUS_UNKNOWN):
        con.close()
        raise ValueError("委托不存在或状态不可撤销")
    con.execute("UPDATE sim_orders SET status='cancelled' WHERE id=?", (order_id,))
    _audit(con, "sim_order_cancelled", {"id": order_id})
    con.commit()
    con.close()
    return True


if __name__ == "__main__":
    init_db()
    print("模拟账户初始化完成：", sim_portfolio())
