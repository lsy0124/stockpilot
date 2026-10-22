"""数据源健康与故障管理模块（第三阶段新增）。

任务书 5.2 要求：
- 合理超时、有界重试、退避机制、数据源切换、异常告警、数据质量标记
- 遇到 WAF/403/429/501 不得伪造指纹或轮换代理绕过
- 连续失败时允许降级为仅使用历史数据，但必须显著标记

设计：
- SourceHealth：每个数据源一个记录（状态/连续失败数/最近错误/熔断截止时间）
- CircuitBreaker：连续失败 N 次后熔断一段时间，期间直接返回"不可用"而不发起请求
- with_retry：有界重试 + 指数退避，仅对可重试错误（超时/连接错误/5xx）重试
- degrade_flag：降级标记，写入报告供下游披露

故障记录落盘 data/source_health.json（持久化，重启后保留）。
"""
import datetime as dt
import json
import os
import time

from stockpilot import DATA, audit, iso, load_json, save_json, setup_logger

log = setup_logger("source_health", "source_health.log")

HEALTH_FILE = os.path.join(DATA, "source_health.json")

# 可重试的错误类型（网络抖动/服务端临时错误）；WAF/403/429 属于硬失败，不重试
RETRYABLE = ("timeout", "timed out", "connection", "remote end closed",
             "502", "503", "504", "bad gateway", "service unavailable")
HARD_FAIL = ("waf", "501", "403", "429", "too many requests", "forbidden")


def _classify(err):
    e = str(err).lower()
    if any(k in e for k in HARD_FAIL):
        return "hard"
    if any(k in e for k in RETRYABLE):
        return "retryable"
    return "unknown"


def with_retry(fn, *args, retries=2, backoff=1.5, timeout=None, label="", **kwargs):
    """有界重试 + 指数退避。返回 (result, error)；error 为 None 表示成功。

    规则：
    - 最多 retries 次重试（总共最多 retries+1 次调用）
    - 只对 retryable 错误重试；hard 失败立即返回
    - 退避 = backoff * 2^attempt 秒
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs), None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            kind = _classify(last_err)
            if kind == "hard" or attempt == retries:
                break
            sleep_s = backoff * (2 ** attempt)
            log.warning("%s 第%d次失败(%s, %s)，%.1fs后重试",
                        label or "call", attempt + 1, kind, last_err[:80], sleep_s)
            time.sleep(sleep_s)
    return None, last_err


class SourceHealth:
    """数据源健康状态：内存 + 持久化到 JSON"""

    def __init__(self, path=HEALTH_FILE):
        self.path = path
        self.state = {}
        if os.path.exists(path):
            try:
                self.state = load_json(path).get("sources", {})
            except Exception:
                self.state = {}

    def _get(self, name):
        return self.state.setdefault(name, {
            "name": name, "status": "unknown", "consecutive_failures": 0,
            "total_calls": 0, "total_failures": 0, "last_ok_at": None,
            "last_error": None, "last_error_at": None, "circuit_open_until": None,
            "notes": [],
        })

    def is_available(self, name):
        s = self._get(name)
        until = s.get("circuit_open_until")
        if until:
            try:
                if dt.datetime.fromisoformat(until) > dt.datetime.now(dt.timezone.utc):
                    return False
            except Exception:
                pass
            s["circuit_open_until"] = None
        return True

    def record_ok(self, name, note=None):
        s = self._get(name)
        s["status"] = "ok"
        s["consecutive_failures"] = 0
        s["total_calls"] += 1
        s["last_ok_at"] = iso()
        s["circuit_open_until"] = None
        if note:
            s["notes"] = (s["notes"] + [f"{iso()} {note}"])[-10:]
        self._save()
        return s

    def record_fail(self, name, error, note=None):
        s = self._get(name)
        s["total_calls"] += 1
        s["total_failures"] += 1
        s["consecutive_failures"] += 1
        s["last_error"] = str(error)[:300]
        s["last_error_at"] = iso()
        kind = _classify(str(error))
        s["last_error_kind"] = kind
        if kind == "hard":
            s["status"] = "blocked"      # WAF/403/429：被限制，不重试不绕过
        elif s["consecutive_failures"] >= 3:
            s["status"] = "failing"
            # 熔断 5 分钟（UTC 记录）
            s["circuit_open_until"] = (dt.datetime.now(dt.timezone.utc)
                                       + dt.timedelta(minutes=5)).isoformat()
        else:
            s["status"] = "degraded"
        if note:
            s["notes"] = (s["notes"] + [f"{iso()} {note}"])[-10:]
        self._save()
        audit("source_failure", source=name, error=s["last_error"], kind=kind,
              consecutive=s["consecutive_failures"])
        return s

    def summary(self):
        return {
            "checked_at": iso(),
            "sources": {k: {kk: vv for kk, vv in v.items() if kk != "notes"}
                        for k, v in self.state.items()},
        }

    def _save(self):
        save_json({"updated_at": iso(), "sources": self.state}, self.path)


def degraded_marker(reason):
    """降级标记：下游报告必须原样展示"""
    return {
        "degraded": True,
        "reason": reason,
        "marked_at": iso(),
        "instruction": "数据不完整；相关结论仅供参考，不得视为实时行情结论",
    }
