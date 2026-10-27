"""Core runtime: trace spans, cost ledger, budgeted HTTP with retry + fault injection.

Design rule: every tool call returns a ToolResult whose `status` is one of
  ok | empty | error | degraded | unverified
so that "nothing came back" is never silently treated as "the answer is no".
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

import requests

# Nominatim usage policy asks for a contact in the User-Agent; set SITE_AGENT_CONTACT to your email.
UA = f"site-research-agent/0.3 (portfolio demo; contact {os.environ.get('SITE_AGENT_CONTACT', 'unset')})"

# ---------------------------------------------------------------- pricing
# USD per 1M tokens. ASSUMPTION: list-price style numbers for Sonnet/Haiku-class
# models; edit here if your contract differs. Public GIS APIs cost $0 but we
# still meter calls and wall time, because latency and rate limits are the
# real budget for them.
PRICE_PER_MTOK = {
    "claude_haiku_4_5": {"in": 1.00, "out": 5.00},
    "claude_sonnet_5_5": {"in": 3.00, "out": 15.00},
    "template": {"in": 0.0, "out": 0.0},
}
# notional per-call cost for external APIs (all free here; kept for the model)
API_COST_USD = {"default": 0.0}


@dataclass
class Budget:
    max_usd: float = 0.15
    max_tool_calls: int = 30
    max_wall_s: float = 120.0


class BudgetExceeded(Exception):
    pass


@dataclass
class Span:
    id: str
    kind: str                 # plan | tool | validate | fallback | compute | synth | guard
    name: str
    args: dict = field(default_factory=dict)
    status: str = "ok"
    started: float = 0.0
    latency_ms: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    http_calls: int = 0
    summary: str = ""
    detail: Any = None
    warnings: list = field(default_factory=list)
    failure_modes: list = field(default_factory=list)   # FM ids triggered
    parent: str | None = None


class Trace:
    def __init__(self, question: str, budget: Budget | None = None, faults: dict | None = None):
        self.run_id = uuid.uuid4().hex[:10]
        self.question = question
        self.budget = budget or Budget()
        self.faults = faults or {}
        self.spans: list[Span] = []
        self.t0 = time.time()
        self.http_calls = 0
        self.cache: dict[str, Any] = {}

    # ---- spans
    def span(self, kind: str, name: str, args: dict | None = None, parent: str | None = None) -> Span:
        s = Span(id=f"s{len(self.spans)+1:02d}", kind=kind, name=name, args=args or {}, started=time.time(), parent=parent)
        self.spans.append(s)
        return s

    def close(self, s: Span, status: str = "ok", summary: str = "", detail: Any = None):
        s.latency_ms = int((time.time() - s.started) * 1000)
        s.status, s.summary = status, summary
        if detail is not None:
            s.detail = detail
        return s

    # ---- cost
    def charge_llm(self, s: Span, model: str, tin: int, tout: int):
        p = PRICE_PER_MTOK.get(model, PRICE_PER_MTOK["claude_sonnet_5_5"])
        s.tokens_in += tin
        s.tokens_out += tout
        s.cost_usd += tin / 1e6 * p["in"] + tout / 1e6 * p["out"]
        self.check_budget()

    def reserve_llm(self, model: str, prompt_chars: int, max_out: int):
        """Pre-flight worst-case reservation. A post-hoc check cannot stop a single expensive
        call (observed: $0.002 cap, $0.044 spent), so we refuse calls whose worst case would breach."""
        p = PRICE_PER_MTOK.get(model, PRICE_PER_MTOK["claude_sonnet_5_5"])
        est_in = prompt_chars / 1.6          # rough: mixed zh/en JSON ≈ 1.6 chars/token
        worst = est_in / 1e6 * p["in"] + max_out / 1e6 * p["out"]
        if self.total_cost + worst > self.budget.max_usd:
            raise BudgetExceeded(f"pre-flight: spent {self.total_cost:.4f} + worst-case {worst:.4f} ({model}) > cap {self.budget.max_usd}")
        return worst

    @property
    def total_cost(self) -> float:
        return round(sum(s.cost_usd for s in self.spans), 6)

    def check_budget(self):
        wall = time.time() - self.t0
        tool_calls = sum(1 for s in self.spans if s.kind in ("tool", "fallback"))
        if self.total_cost > self.budget.max_usd:
            raise BudgetExceeded(f"cost {self.total_cost:.4f} > {self.budget.max_usd}")
        if tool_calls > self.budget.max_tool_calls:
            raise BudgetExceeded(f"tool calls {tool_calls} > {self.budget.max_tool_calls}")
        if wall > self.budget.max_wall_s:
            raise BudgetExceeded(f"wall {wall:.0f}s > {self.budget.max_wall_s}s")

    def ledger(self) -> dict:
        by_kind: dict[str, dict] = {}
        for s in self.spans:
            k = by_kind.setdefault(s.kind, {"spans": 0, "usd": 0.0, "ms": 0, "tokens_in": 0, "tokens_out": 0, "http": 0})
            k["spans"] += 1; k["usd"] += s.cost_usd; k["ms"] += s.latency_ms
            k["tokens_in"] += s.tokens_in; k["tokens_out"] += s.tokens_out; k["http"] += s.http_calls
        for k in by_kind.values():
            k["usd"] = round(k["usd"], 6)
        wasted = sum(s.cost_usd for s in self.spans if s.status in ("error",))
        wasted_ms = sum(s.latency_ms for s in self.spans if s.status in ("error", "empty", "unverified") and s.kind in ("tool", "fallback"))
        return {
            "total_usd": self.total_cost,
            "wall_ms": int((time.time() - self.t0) * 1000),
            "http_calls": self.http_calls,
            "llm_tokens_in": sum(s.tokens_in for s in self.spans),
            "llm_tokens_out": sum(s.tokens_out for s in self.spans),
            "by_kind": by_kind,
            "wasted_usd_on_failed_spans": round(wasted, 6),
            "ms_spent_on_failed_or_empty_tools": wasted_ms,
            "budget": asdict(self.budget),
        }

    def to_json(self) -> dict:
        return {"run_id": self.run_id, "question": self.question, "faults": self.faults,
                "spans": [asdict(s) for s in self.spans], "ledger": self.ledger()}


# ---------------------------------------------------------------- HTTP
class ToolHTTPError(Exception):
    def __init__(self, kind: str, msg: str, retryable: bool):
        super().__init__(msg)
        self.kind, self.retryable = kind, retryable


def http_get(trace: Trace, span: Span, url: str, params: dict | None = None, timeout: float = 12,
             retries: int = 1, expect_json: bool = True, fault_key: str | None = None):
    """GET with timeout, bounded retry on retryable errors only, response-type check,
    in-run cache, and deterministic fault injection (trace.faults[fault_key])."""
    key = url + json.dumps(params or {}, sort_keys=True)
    if key in trace.cache:
        span.warnings.append("cache hit")
        return trace.cache[key]
    fault = trace.faults.get(fault_key) if fault_key else None
    last: ToolHTTPError | None = None
    for attempt in range(retries + 1):
        trace.check_budget()
        trace.http_calls += 1
        span.http_calls += 1
        try:
            if fault == "timeout":
                time.sleep(0.4)
                raise requests.Timeout("injected timeout")
            if fault == "http_429":
                raise ToolHTTPError("rate_limited", "injected 429", True)
            r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": UA})
            if r.status_code == 429:
                raise ToolHTTPError("rate_limited", "HTTP 429", True)
            if r.status_code >= 500:
                raise ToolHTTPError("upstream_5xx", f"HTTP {r.status_code}", True)
            if r.status_code >= 400:
                raise ToolHTTPError("client_4xx", f"HTTP {r.status_code}", False)
            if expect_json:
                ctype = r.headers.get("content-type", "")
                try:
                    data = r.json()
                except ValueError:
                    # e.g. Census API returns an HTML 'Missing Key' page with 200
                    raise ToolHTTPError("schema_mismatch", f"expected JSON, got {ctype[:40]}: {r.text[:80]!r}", False)
                if isinstance(data, dict) and "error" in data and data.get("error"):
                    raise ToolHTTPError("api_error", f"ArcGIS error: {str(data['error'])[:120]}", False)
            else:
                data = r.content
            if fault == "empty" and isinstance(data, dict):
                data = {**data, "features": [], "result": {"addressMatches": []}}
            trace.cache[key] = data
            return data
        except requests.Timeout:
            last = ToolHTTPError("timeout", f"timeout after {timeout}s", True)
        except requests.ConnectionError as e:
            last = ToolHTTPError("network", f"connection error: {str(e)[:80]}", True)
        except ToolHTTPError as e:
            last = e
        span.warnings.append(f"attempt {attempt+1}: {last.kind} ({last})")
        if not last.retryable:
            break
        time.sleep(min(2 ** attempt * 0.5, 2))
    raise last  # type: ignore[misc]
