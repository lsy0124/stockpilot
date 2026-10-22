"""StockPilot 公共工具：日志、路径、常量、时间。
所有模块统一从这里取路径与 logger，保证审计链一致。
"""
import datetime as dt
import json
import logging
import os
import sys
import tempfile

SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Set before importing StockPilot; defaults preserve existing local data paths.
BASE = os.path.abspath(os.environ.get("STOCKPILOT_HOME") or SOURCE_ROOT)
LOGS = os.path.join(BASE, "logs")
DATA = os.path.join(BASE, "data")
REPORTS = os.path.join(BASE, "reports")
VAULT = os.path.join(BASE, "vault")          # 长期 Markdown 知识库（Obsidian 兼容）
AUDIT = os.path.join(BASE, "audit")          # 运行审计记录
EVIDENCE = os.path.join(BASE, "evidence")
SQLITE = os.path.join(DATA, "stockpilot.db")

for d in (LOGS, DATA, REPORTS, VAULT, AUDIT, EVIDENCE):
    os.makedirs(d, exist_ok=True)

INITIAL_CAPITAL = 1000.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"

# 费用模型默认参数
# official：印花税/过户费/经手费/证管费（交易所与税务机关公开标准，已核实）
# assumption：佣金率与最低佣金（券商自定，须用户核实）
FEE_MODEL = {
    "commission_rate": 0.00025,      # 佣金万分之2.5（假设）
    "commission_min": 5.0,           # 每笔最低佣金（假设，是否免五须核实）
    "stamp_duty_rate": 0.0005,       # 印花税卖出万分之五（2023-08-28 减半后）
    "stamp_duty_etf_exempt": True,   # ETF 免印花税（官方）
    "transfer_fee_rate": 0.00001,    # 过户费万分之0.1（官方）
    "handling_fee_rate": 0.0000341,  # 经手费（官方，上交所收费表）
    "supervision_fee_rate": 0.00002,  # 证管费（官方）
    "lot_size": 100,                 # 最小交易单位
    "official_verified_at": "2026-10-21",
}


def now():
    from .timeutil import now as market_now
    return market_now()


def iso(ts=None):
    return (ts or now()).isoformat(timespec="seconds")


def setup_logger(name, filename=None):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if filename:
        fh = logging.FileHandler(os.path.join(LOGS, filename), encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def audit(event, **fields):
    """追加一条审计记录（不可变追加，禁止事后删改）"""
    rec = {"ts": iso(), "event": event}
    rec.update(fields)
    path = os.path.join(AUDIT, f"audit_{now().strftime('%Y%m')}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return rec


def save_json(obj, path):
    path = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=parent,
                                         prefix=".stockpilot-", suffix=".tmp", delete=False) as f:
            temporary = f.name
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return path


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def latest_trading_day(today=None, calendar=None):
    """返回今天之前最近的交易日（含今天，若今天是交易日）"""
    today = today or now().date()
    if calendar is None:
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            calendar = [str(x)[:10] for x in df["trade_date"]]
        except Exception:
            calendar = []
    if not calendar:
        return today
    cand = [d for d in calendar if d <= today.isoformat()]
    return dt.date.fromisoformat(cand[-1]) if cand else today
