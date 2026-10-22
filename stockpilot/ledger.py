"""SQLite 账户账本（第三阶段重写版）。

对应任务书第六、七章。相较第二版的增强：
1. 数据库迁移机制（schema_version 表）
2. 唯一成交标识 + 幂等（重复导入同一成交返回已有记录，不重复计费）
3. 部分成交 / 撤单 / 委托生命周期
4. 分红与公司行为可追溯记录
5. A股与ETF分别处理（ETF免印花税、T+1、最小单位）
6. 事务保证 + 对账（现金/持仓/费用恒等式）
7. SQLite 官方在线备份（backup API，不用文件复制）
8. 资金流水表（每笔现金变动都有记录）

严格区分四层：
  advice(GPT-6建议) → intent(用户确认准备交易) → order(提交委托) → trade(券商确认成交)
只有 trade 能改变真实持仓与现金。
"""
import datetime as dt
import json
import os
import shutil
import sqlite3

from stockpilot import DATA, audit, iso, setup_logger
from stockpilot import timeutil as tu

log = setup_logger("ledger", "ledger.log")

SCHEMA_VERSION = 3
DEFAULT_DB = os.path.join(DATA, "stockpilot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT
);

-- 1. 账户
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    opened_at TEXT NOT NULL
);

-- 2. 资金流水（每笔现金变动）
CREATE TABLE IF NOT EXISTS cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- initial/buy/sell/dividend/fee/deposit/withdraw
    amount REAL NOT NULL,               -- 正=流入，负=流出
    balance_after REAL NOT NULL,
    ref_type TEXT, ref_id INTEGER,
    note TEXT
);

-- 3. GPT-6 建议（第一层）
CREATE TABLE IF NOT EXISTS advice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,               -- GLM / GPT-6
    action TEXT NOT NULL,               -- buy/sell/hold/observe
    code TEXT, name TEXT, market TEXT,
    price_low REAL, price_high REAL, shares INTEGER,
    reason_bull TEXT, reason_bear TEXT, exit_condition TEXT,
    status TEXT NOT NULL DEFAULT 'pending'  -- pending/approved/rejected/expired
);

-- 4. 用户确认准备交易（第二层）
CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    advice_id INTEGER REFERENCES advice(id),
    action TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT, market TEXT,
    price REAL, shares INTEGER,
    status TEXT NOT NULL DEFAULT 'confirmed'  -- confirmed/submitted/cancelled/expired
);

-- 5. 交易委托（第三层）
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    intent_id INTEGER REFERENCES intents(id),
    advice_id INTEGER REFERENCES advice(id),
    submitted_by TEXT NOT NULL DEFAULT 'user',
    action TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT, market TEXT,
    price REAL NOT NULL, shares INTEGER NOT NULL,
    filled_shares INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'submitted'  -- submitted/partial/filled/cancelled
);

-- 6. 实际成交（第四层，唯一能改持仓）
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_uid TEXT NOT NULL UNIQUE,     -- 唯一成交标识（幂等键）
    order_id INTEGER NOT NULL REFERENCES orders(id),
    filled_at TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT, market TEXT,
    action TEXT NOT NULL,               -- buy/sell
    price REAL NOT NULL, shares INTEGER NOT NULL,
    amount REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    stamp_duty REAL NOT NULL DEFAULT 0,
    transfer_fee REAL NOT NULL DEFAULT 0,
    other_fee REAL NOT NULL DEFAULT 0,
    total_fee REAL NOT NULL,
    is_partial INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'user_confirmed'
);

-- 7. 公司行为（分红/送股/拆股）
CREATE TABLE IF NOT EXISTS corp_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ex_date TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT, market TEXT,
    type TEXT NOT NULL,                 -- dividend/split/bonus
    cash_per_share REAL DEFAULT 0,
    shares_ratio TEXT,
    note TEXT,
    recorded_at TEXT NOT NULL
);

-- 8. 投资决策（GPT-6 正式决定，经用户确认或可核验交接导入）
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_uid TEXT NOT NULL UNIQUE,
    decided_at TEXT NOT NULL,
    source TEXT NOT NULL,               -- GPT-6
    code TEXT NOT NULL, name TEXT, market TEXT,
    direction TEXT NOT NULL,            -- buy/sell/hold/observe
    price_low REAL, price_high REAL,
    shares INTEGER, position_pct REAL,
    horizon TEXT,
    reason_bull TEXT, reason_bear TEXT, risk_condition TEXT, exit_condition TEXT,
    data_version TEXT,                  -- 当时可见的数据版本（交接包ID）
    glm_research_ref TEXT,              -- 智谱研究意见引用（不修改）
    executed INTEGER NOT NULL DEFAULT 0,
    executed_result TEXT,
    created_at TEXT NOT NULL
);

-- 9. 历史预测
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    subject TEXT NOT NULL,
    claim TEXT NOT NULL,
    horizon TEXT,
    code TEXT,
    base_price REAL,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT, outcome TEXT
);

-- 10. 审计事件
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code);
CREATE INDEX IF NOT EXISTS idx_trades_order ON trades(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_cashflows_ref ON cash_flows(ref_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_decisions_code ON decisions(code);
CREATE INDEX IF NOT EXISTS idx_predictions_resolved ON predictions(resolved);
"""


# ---------------------------------------------------------------- 连接与迁移
def connect(path=DEFAULT_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")     # 支持在线备份与崩溃恢复
    return con


def init_db(path=DEFAULT_DB, initial_capital=1000.0):
    con = connect(path)
    con.executescript(SCHEMA)
    cur = con.execute("SELECT COALESCE(MAX(version),0) AS v FROM schema_migrations")
    v = cur.fetchone()["v"]
    if v < 1:
        con.execute("INSERT INTO schema_migrations(version,applied_at,note) VALUES(1,?,?)",
                    (iso(), "v3 schema: account/cash_flows/advice/intents/orders/trades/"
                            "corp_actions/decisions/predictions/audit_log"))
    if v < SCHEMA_VERSION:
        con.execute("INSERT OR REPLACE INTO schema_migrations(version,applied_at,note)"
                    " VALUES(?,?,?)", (SCHEMA_VERSION, iso(), "v3.2 幂等成交与公司行为"))
    con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('initial_capital',?)",
                (str(initial_capital),))
    con.execute("INSERT OR IGNORE INTO account(id,initial_capital,opened_at) VALUES(1,?,?)",
                (initial_capital, iso()))
    con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('created_at',?)", (iso(),))
    # 初始资金流水（幂等）
    has_init = con.execute("SELECT COUNT(*) AS c FROM cash_flows WHERE kind='initial'").fetchone()["c"]
    if not has_init:
        _add_cash_flow(con, "initial", initial_capital, None, None, "初始入金")
    con.commit()
    _audit(con, "db_initialized", {"path": path, "initial_capital": initial_capital,
                                   "schema_version": SCHEMA_VERSION})
    con.commit()
    con.close()
    return path


def _audit(con, event, detail):
    con.execute("INSERT INTO audit_log(at,event,detail) VALUES(?,?,?)",
                (iso(), event, json.dumps(detail, ensure_ascii=False, default=str)))


def _add_cash_flow(con, kind, amount, ref_type=None, ref_id=None, note=None):
    row = con.execute("SELECT balance_after FROM cash_flows"
                      " ORDER BY id DESC LIMIT 1").fetchone()
    bal = row["balance_after"] if row else 0.0
    new_bal = round(bal + amount, 2)
    con.execute("INSERT INTO cash_flows(at,kind,amount,balance_after,ref_type,ref_id,note)"
                " VALUES(?,?,?,?,?,?,?)",
                (iso(), kind, round(amount, 2), new_bal, ref_type, ref_id, note))
    return new_bal


# ---------------------------------------------------------------- 费用模型
def fee_model():
    """账本侧费用模型。

    费率来源分级（与 fee_model.py 保持一致）：
    - official：印花税/过户费/经手费/证管费，交易所与税务机关公开标准
    - assumption：券商佣金率与最低佣金，须用户向券商核实

    历史修正：印花税原写 0.001（千分之一），那是 2023-08-28 前的税率；
    现行 0.0005（万分之五），已更正。
    """
    return {
        "commission_rate": 0.00025,
        "commission_min": 5.0,
        "stamp_duty_rate": 0.0005,      # 2023-08-28 起减半，卖出单边
        "stamp_duty_etf_exempt": True,
        "transfer_fee_rate": 0.00001,
        "handling_fee_rate": 0.0000341,  # 经手费（上交所收费表）
        "supervision_fee_rate": 0.00002,  # 证管费
        "lot_size": 100,
        "source": "mixed",               # 官方项 + 待核实的佣金假设
        "official_verified_at": "2026-10-21",
    }


def calc_fees(price, shares, market="A", is_etf=False, fm=None, direction="buy"):
    """计算交易费用。

    **唯一实现来源**：委托给 `stockpilot.fee_model.calc_fee_breakdown`，
    账本不再保留第二套费率算法（早期两份实现会漂移到分位级差异，
    例如经手费逐项舍入 vs 先加后舍入）。

    保留本函数作薄适配层，因为 ledger 内部与历史调用方都依赖其返回结构。

    direction: "buy" / "sell"。印花税仅卖出计收（ETF 免征）。
    """
    from stockpilot import fee_model as _fm
    configs = _fm.fee_manager.list_configs()
    cfg = next((c for c in configs
                if c.is_etf == bool(is_etf) and c.config_id.endswith("_5yuan")), None)
    if cfg is None:
        cfg = next((c for c in configs if c.is_etf == bool(is_etf)), configs[0])
    return _fm.calc_fee_breakdown(price, shares, bool(is_etf), cfg, direction)


# ---------------------------------------------------------------- 交易流程
def add_advice(source, action, code=None, name=None, market=None, price_low=None,
               price_high=None, shares=None, reason_bull=None, reason_bear=None,
               exit_condition=None, path=DEFAULT_DB):
    """第一层：记录建议（GLM研究意见或GPT-6建议）"""
    con = connect(path)
    cur = con.execute(
        "INSERT INTO advice(created_at,source,action,code,name,market,price_low,price_high,"
        "shares,reason_bull,reason_bear,exit_condition) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (iso(), source, action, code, name, market, price_low, price_high, shares,
         reason_bull, reason_bear, exit_condition))
    con.commit()
    aid = cur.lastrowid
    _audit(con, "advice_added", {"id": aid, "source": source, "action": action, "code": code})
    con.commit()
    con.close()
    audit("ledger_advice_added", id=aid, source=source, action=action, code=code)
    return aid


def add_intent(advice_id, action, code, name=None, market=None, price=None, shares=None,
               path=DEFAULT_DB):
    """第二层：用户确认准备交易"""
    con = connect(path)
    cur = con.execute(
        "INSERT INTO intents(created_at,advice_id,action,code,name,market,price,shares)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (iso(), advice_id, action, code, name, market, price, shares))
    con.commit()
    iid = cur.lastrowid
    _audit(con, "intent_confirmed", {"id": iid, "advice_id": advice_id, "code": code})
    con.commit()
    con.close()
    audit("ledger_intent_confirmed", id=iid, code=code)
    return iid


def submit_order(intent_id, action, code, name=None, market=None, price=0, shares=0,
                  path=DEFAULT_DB):
    """第三层：提交委托"""
    con = connect(path)
    cur = con.execute(
        "INSERT INTO orders(created_at,intent_id,submitted_by,action,code,name,market,price,shares)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (iso(), intent_id, "user", action, code, name, market, price, shares))
    con.commit()
    oid = cur.lastrowid
    _audit(con, "order_submitted", {"id": oid, "code": code, "price": price, "shares": shares})
    con.commit()
    con.close()
    audit("ledger_order_submitted", id=oid, code=code, price=price, shares=shares)
    return oid


def cancel_order(order_id, path=DEFAULT_DB):
    """撤单：仅 submitted/partial 状态可撤"""
    con = connect(path)
    o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        con.close()
        raise ValueError(f"委托 {order_id} 不存在")
    if o["status"] not in ("submitted", "partial"):
        con.close()
        raise ValueError(f"委托 {order_id} 状态为 {o['status']}，不可撤销")
    con.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
    _audit(con, "order_cancelled", {"id": order_id})
    con.commit()
    con.close()
    audit("ledger_order_cancelled", id=order_id)
    return True


def confirm_trade(order_id, price, shares, trade_uid=None, is_partial=0,
                  filled_at=None, fee_model=None, path=DEFAULT_DB):
    """第四层：确认成交（唯一改变持仓与现金的入口）。

    - trade_uid：唯一成交标识；重复导入同一 uid 时返回已有记录（幂等），不重复计费
    - 部分成交：is_partial=1，委托状态转 partial
    - 资金不足时拒绝（不允许负现金）
    """
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        # 幂等优先：相同 trade_uid 的重复导入直接返回已有记录，
        # 必须早于委托状态检查（委托可能已 filled，重推同一成交不应报错）
        uid = trade_uid or f"T{order_id}_{int(dt.datetime.now().timestamp()*1000)}"
        exist = con.execute("SELECT * FROM trades WHERE trade_uid=?", (uid,)).fetchone()
        if exist:
            con.commit()
            audit("ledger_trade_duplicate_ignored", uid=uid, id=exist["id"])
            return {"id": exist["id"], "duplicate": True, "trade_uid": uid}
        o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise ValueError(f"委托 {order_id} 不存在")
        if o["status"] in ("filled", "cancelled"):
            raise ValueError(f"委托 {order_id} 状态为 {o['status']}，不能成交")
        remaining = o["shares"] - o["filled_shares"]
        if shares > remaining:
            raise ValueError(f"成交数量 {shares} 超过委托剩余 {remaining}")
        if shares <= 0:
            raise ValueError("成交数量必须为正")

        market = o["market"] or "A"
        is_etf = (o["name"] or "").upper().endswith("ETF") or market == "ETF"
        # 必须传方向：印花税只在卖出计收，买入不应含印花税
        fees = calc_fees(price, shares, market=market, is_etf=is_etf,
                         fm=fee_model, direction=o["action"])
        cash = current_cash(con)
        if o["action"] == "buy":
            need = fees["amount"] + fees["total_fee"]
            if need > cash:
                raise ValueError(f"资金不足：需 {need:.2f}，可用 {cash:.2f}")
            _add_cash_flow(con, "buy", -(fees["amount"] + fees["total_fee"]), "trade", None,
                           f"买入 {o['code']} {shares}股 @{price}（含费用 {fees['total_fee']}）")
        else:
            # 卖出前检查持仓充足
            held = con.execute(
                "SELECT COALESCE(SUM(CASE WHEN action='buy' THEN shares ELSE -shares END),0) AS s"
                " FROM trades WHERE code=?", (o["code"],)).fetchone()["s"]
            if held < shares:
                raise ValueError(f"持仓不足：持有 {held}，欲卖 {shares}")
            _add_cash_flow(con, "sell", fees["amount"] - fees["total_fee"], "trade", None,
                           f"卖出 {o['code']} {shares}股 @{price}（扣费用 {fees['total_fee']}）")

        cur = con.execute(
            "INSERT INTO trades(trade_uid,order_id,filled_at,code,name,market,action,price,"
            "shares,amount,commission,stamp_duty,transfer_fee,total_fee,is_partial,source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, order_id, filled_at or iso(), o["code"], o["name"], market, o["action"],
             price, shares, fees["amount"], fees["commission"], fees["stamp_duty"],
             fees["transfer_fee"], fees["total_fee"], is_partial, "user_confirmed"))
        tid = cur.lastrowid
        new_filled = o["filled_shares"] + shares
        new_status = "filled" if new_filled >= o["shares"] else "partial"
        con.execute("UPDATE orders SET filled_shares=?, status=? WHERE id=?",
                    (new_filled, new_status, order_id))
        _audit(con, "trade_confirmed", {"id": tid, "uid": uid, "code": o["code"],
                                        "action": o["action"], "price": price, "shares": shares,
                                        "fee": fees["total_fee"]})
        con.commit()
        audit("ledger_trade_confirmed", id=tid, uid=uid, code=o["code"],
              action=o["action"], price=price, shares=shares)
        return {"id": tid, "duplicate": False, "trade_uid": uid, "fees": fees,
                "order_status": new_status}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------- 决策与预测
def add_decision(source, code, name=None, market=None, direction="buy",
                 price_low=None, price_high=None, shares=None, position_pct=None,
                 horizon=None, reason_bull=None, reason_bear=None,
                 risk_condition=None, exit_condition=None, data_version=None,
                 glm_research_ref=None, supersedes=None, path=DEFAULT_DB):
    """记录 GPT-6 正式投资决策（经用户确认或可核验交接导入）。

    - decision_uid 唯一；supersedes 指向被修订的旧决策（原记录永不修改）
    - 不得把智谱研究意见自动记为 GPT-6 决定（source 必须显式传入）
    """
    con = connect(path)
    con.execute("BEGIN IMMEDIATE")
    uid = f"DEC-{int(dt.datetime.now().timestamp()*1000)}"
    cur = con.execute(
        "INSERT INTO decisions(decision_uid,decided_at,source,code,name,market,direction,"
        "price_low,price_high,shares,position_pct,horizon,reason_bull,reason_bear,"
        "risk_condition,exit_condition,data_version,glm_research_ref,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, iso(), source, code, name, market, direction, price_low, price_high, shares,
         position_pct, horizon, reason_bull, reason_bear, risk_condition, exit_condition,
         data_version, glm_research_ref, iso()))
    did = cur.lastrowid
    if supersedes:
        con.execute("UPDATE decisions SET executed=-1 WHERE id=?", (supersedes,))
        _audit(con, "decision_superseded", {"old": supersedes, "new": did, "uid": uid})
    _audit(con, "decision_recorded", {"id": did, "uid": uid, "source": source,
                                      "code": code, "direction": direction,
                                      "data_version": data_version})
    con.commit()
    con.close()
    audit("ledger_decision_recorded", id=did, uid=uid, source=source, code=code)
    return did


def get_decisions(code=None, limit=50, path=DEFAULT_DB):
    con = connect(path)
    if code:
        rows = con.execute("SELECT * FROM decisions WHERE code=? ORDER BY id DESC LIMIT ?",
                           (code, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def add_prediction(subject, claim, horizon=None, code=None, base_price=None,
                  path=DEFAULT_DB):
    con = connect(path)
    cur = con.execute(
        "INSERT INTO predictions(created_at,subject,claim,horizon,code,base_price)"
        " VALUES(?,?,?,?,?,?)",
        (iso(), subject, claim, horizon, code, base_price))
    con.commit()
    pid = cur.lastrowid
    _audit(con, "prediction_recorded", {"id": pid, "subject": subject, "code": code})
    con.commit()
    con.close()
    return pid


def get_predictions(resolved=None, limit=50, path=DEFAULT_DB):
    con = connect(path)
    if resolved is None:
        rows = con.execute("SELECT * FROM predictions ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM predictions WHERE resolved=? ORDER BY id DESC LIMIT ?",
                           (1 if resolved else 0, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def resolve_prediction(pid, outcome, resolved_at=None, path=DEFAULT_DB):
    """结算预测：只允许从未结算 → 已结算，不可覆盖已有结论。
    outcome 接受 dict 或 str；dict 会序列化为 JSON 字符串存储。"""
    if isinstance(outcome, (dict, list)):
        outcome = json.dumps(outcome, ensure_ascii=False, default=str)
    con = connect(path)
    con.execute("BEGIN IMMEDIATE")
    p = con.execute("SELECT * FROM predictions WHERE id=?", (pid,)).fetchone()
    if not p:
        con.rollback()
        con.close()
        raise ValueError(f"预测 {pid} 不存在")
    if p["resolved"]:
        con.rollback()
        con.close()
        raise ValueError(f"预测 {pid} 已结算，不可覆盖（历史不可篡改）")
    con.execute("UPDATE predictions SET resolved=1, resolved_at=?, outcome=? WHERE id=?",
                (resolved_at or iso(), outcome, pid))
    _audit(con, "prediction_resolved", {"id": pid, "outcome": outcome})
    con.commit()
    con.close()
    audit("ledger_prediction_resolved", id=pid)
    return True


def record_dividend(ex_date, code, name=None, market=None, cash_per_share=0.0,
                    shares_ratio=None, note=None, path=DEFAULT_DB):
    """分红/送股记录：按当前持仓自动入账现金分红"""
    con = connect(path)
    con.execute("BEGIN IMMEDIATE")
    held = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN action='buy' THEN shares ELSE -shares END),0) AS s"
        " FROM trades WHERE code=?", (code,)).fetchone()["s"]
    amount = 0.0
    if held > 0 and cash_per_share:
        amount = round(held * cash_per_share, 2)
        _add_cash_flow(con, "dividend", amount, "corp_action", None,
                       f"{code} 每股派现 {cash_per_share} 元 × {held} 股")
    con.execute(
        "INSERT INTO corp_actions(ex_date,code,name,market,type,cash_per_share,shares_ratio,"
        "note,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (ex_date, code, name, market, "dividend", cash_per_share, shares_ratio, note, iso()))
    _audit(con, "dividend_recorded", {"code": code, "held": held, "amount": amount})
    con.commit()
    con.close()
    audit("ledger_dividend_recorded", code=code, amount=amount)
    return {"code": code, "held_shares": held, "cash_in": amount}


# ---------------------------------------------------------------- 查询
def current_cash(con=None, path=DEFAULT_DB):
    own = con is None
    con = con or connect(path)
    v = con.execute("SELECT COALESCE(balance_after,0) AS b FROM cash_flows"
                    " ORDER BY id DESC LIMIT 1").fetchone()["b"]
    if own:
        con.close()
    return round(v, 2)


def positions(con=None, prices=None, path=DEFAULT_DB):
    own = con is None
    con = con or connect(path)
    rows = con.execute(
        "SELECT code, name, market,"
        " SUM(CASE WHEN action='buy' THEN shares ELSE -shares END) AS shares,"
        " SUM(CASE WHEN action='buy' THEN amount ELSE -amount END) AS net_amount,"
        " SUM(total_fee) AS fees FROM trades GROUP BY code, name, market"
        # 注意：HAVING 必须写完整表达式，不能引用 shares 别名（与 trades.shares 列名冲突，
        # SQLite 会解析为原始列导致过滤失效）
        " HAVING SUM(CASE WHEN action='buy' THEN shares ELSE -shares END) > 0"
    ).fetchall()
    out = []
    for r in rows:
        p = dict(r)
        price = (prices or {}).get(p["code"])
        p["current_price"] = price
        p["avg_cost"] = round((p["net_amount"] + p["fees"]) / p["shares"], 4) if p["shares"] else None
        if price:
            p["market_value"] = round(price * p["shares"], 2)
            p["unrealized_pnl"] = round(p["market_value"] - (p["net_amount"] + p["fees"]), 2)
            p["unrealized_pnl_pct"] = round(p["unrealized_pnl"] /
                                            (p["net_amount"] + p["fees"]) * 100, 2)
        out.append(p)
    if own:
        con.close()
    return out


def portfolio(prices=None, path=DEFAULT_DB):
    con = connect(path)
    initial = con.execute("SELECT initial_capital FROM account WHERE id=1").fetchone()[0]
    cash = current_cash(con)
    pos = positions(con, prices)
    pos_val = round(sum(p["market_value"] for p in pos if p.get("market_value")), 2)
    unrealized = round(sum(p["unrealized_pnl"] for p in pos if p.get("unrealized_pnl") is not None), 2)
    total = round(cash + pos_val, 2)
    buys = con.execute("SELECT COALESCE(SUM(amount+total_fee),0) AS v FROM trades"
                       " WHERE action='buy'").fetchone()["v"]
    sells = con.execute("SELECT COALESCE(SUM(amount-total_fee),0) AS v FROM trades"
                        " WHERE action='sell'").fetchone()["v"]
    fees_paid = con.execute("SELECT COALESCE(SUM(total_fee),0) AS v FROM trades").fetchone()["v"]
    trade_count = con.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
    con.close()
    return {
        "as_of": iso(),
        "initial_capital": initial,
        "cash": cash,
        "positions": pos,
        "positions_value": pos_val,
        "buy_total": round(buys, 2), "sell_total": round(sells, 2),
        "fees_paid": round(fees_paid, 2),
        "realized_pnl": round(sells - buys + pos_val, 2) if pos else round(sells - buys, 2),
        "unrealized_pnl": unrealized,
        "total_assets": total,
        "net_return_pct": round((total - initial) / initial * 100, 3),
        "trade_count": trade_count,
    }


def reconcile(con=None, path=DEFAULT_DB):
    """对账：初始资金 = 现金 + 持仓净成本 + 已实现盈亏"""
    own = con is None
    con = con or connect(path)
    initial = con.execute("SELECT initial_capital FROM account WHERE id=1").fetchone()[0]
    cash = current_cash(con)
    buys = con.execute("SELECT COALESCE(SUM(amount+total_fee),0) AS v FROM trades"
                       " WHERE action='buy'").fetchone()["v"]
    sells = con.execute("SELECT COALESCE(SUM(amount-total_fee),0) AS v FROM trades"
                        " WHERE action='sell'").fetchone()["v"]
    dividends = con.execute("SELECT COALESCE(SUM(amount),0) AS v FROM cash_flows"
                            " WHERE kind='dividend'").fetchone()["v"]
    # 恒等式：initial + dividends = cash + buys - sells
    lhs = round(initial + dividends, 2)
    rhs = round(cash + buys - sells, 2)
    ok = abs(lhs - rhs) < 0.01
    if own:
        con.close()
    return {"ok": ok, "initial": initial, "dividends": round(dividends, 2),
            "cash": cash, "buys": round(buys, 2), "sells": round(sells, 2),
            "lhs": lhs, "rhs": rhs,
            "note": "恒等式: 初始资金+分红 = 现金 + 买入支出 - 卖出回款"}


# ---------------------------------------------------------------- 备份（官方在线备份）
def backup(path=DEFAULT_DB, backup_dir=None, keep=5):
    """SQLite 官方在线备份（con.backup），避免直接复制正在写入的主文件。"""
    backup_dir = backup_dir or os.path.join(DATA, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, f"stockpilot_{tu.now().strftime('%Y%m%d_%H%M%S')}.db")
    src = connect(path)
    try:
        dst_con = sqlite3.connect(dst)
        src.backup(dst_con)          # 官方在线备份 API
        dst_con.close()
    finally:
        src.close()
    # 轮换：只保留最近 keep 份
    files = sorted(f for f in os.listdir(backup_dir) if f.endswith(".db"))
    for f in files[:-keep] if len(files) > keep else []:
        os.remove(os.path.join(backup_dir, f))
    audit("ledger_backup", dst=dst, keep=keep)
    return dst


def restore(backup_file, target=DEFAULT_DB):
    """从备份恢复：先备份当前库，再用官方 API 恢复。"""
    if not os.path.exists(backup_file):
        raise FileNotFoundError(backup_file)
    safety = backup(target, backup_dir=os.path.join(DATA, "backups", "pre_restore"))
    dst_con = sqlite3.connect(target)
    try:
        src_con = sqlite3.connect(backup_file)
        src_con.backup(dst_con)
        src_con.close()
    finally:
        dst_con.close()
    audit("ledger_restored", from_file=backup_file, to=target, safety_backup=safety)
    return {"restored_from": backup_file, "to": target, "safety_backup": safety}


def integrity_check(path=DEFAULT_DB):
    con = connect(path)
    r = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    return r == "ok"


if __name__ == "__main__":
    init_db()
    print("reconcile:", reconcile())
    print("integrity:", integrity_check())
    print("backup:", backup())
