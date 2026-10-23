from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    requests: int = 48
    tokens: int = 2_000_000
    seconds: int = 1800
    output_tokens: int = 16384

    def __post_init__(self):
        for name, low, high in (("requests", 1, 1000), ("tokens", 1000, 50_000_000),
                                ("seconds", 10, 14400), ("output_tokens", 256, 65536)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"预算字段 {name} 超出允许范围")


class Budget:
    def __init__(self, limits: Limits, persist: Callable | None = None, prior: dict | None = None):
        self.limits = limits
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.requests = 0
        self.used = 0
        self.reserved = 0
        self.records = []
        self.persist = persist or (lambda _: None)
        if prior:
            self.requests = prior["requests"]
            self.used = prior["charged_tokens"] + prior.get("reserved_tokens", 0)
            self.records = list(prior.get("usage", []))
            self.started -= prior.get("seconds", 0)
            if prior.get("reserved_tokens", 0):
                self.records.append({"request": "recovery", "tokens": prior["reserved_tokens"],
                                     "measured": False, "status": "interrupted-reservation"})

    def check(self):
        if self.cancelled.is_set():
            raise BudgetExceeded("实验已取消")
        if time.monotonic() - self.started >= self.limits.seconds:
            raise BudgetExceeded("实验达到时间上限")

    def begin(self, body_bytes: int) -> tuple[int, int]:
        with self.lock:
            self.check()
            reserve = body_bytes + self.limits.output_tokens + 32768
            if self.requests >= self.limits.requests:
                raise BudgetExceeded("实验达到请求次数上限")
            if self.used + self.reserved + reserve > self.limits.tokens:
                raise BudgetExceeded("剩余 token 额度不足以预留下一请求")
            self.requests += 1
            self.reserved += reserve
            self.persist(self.public())
            return self.requests, reserve

    def finish(self, ticket: tuple[int, int], usage: dict | None, status: str):
        with self.lock:
            ident, reserve = ticket
            incoming = usage.get("input_tokens") if isinstance(usage, dict) else None
            outgoing = usage.get("output_tokens") if isinstance(usage, dict) else None
            measured = (type(incoming) is int and incoming >= 0
                        and type(outgoing) is int and outgoing >= 0)
            charged = incoming + outgoing if measured else reserve
            self.reserved -= reserve
            self.used += charged
            self.records.append({"request": ident, "tokens": charged,
                                 "input_tokens": incoming if measured else None,
                                 "output_tokens": outgoing if measured else None,
                                 "measured": measured, "status": status})
            if self.used >= self.limits.tokens:
                self.cancelled.set()
            self.persist(self.public())

    def public(self):
        with self.lock:
            return {"limits": asdict(self.limits), "requests": self.requests,
                    "charged_tokens": self.used, "reserved_tokens": self.reserved,
                    "seconds": round(time.monotonic() - self.started, 2),
                    "usage": list(self.records), "cost_usd": None,
                    "token_policy": "usage 后停止；请求前使用字节数加余量预留，"
                                    "未知分词器和在途请求可能超出 token 阈值；非费用硬上限"}
