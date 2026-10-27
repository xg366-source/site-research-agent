"""Site-research agent: LLM plans, a deterministic executor enforces contracts,
LLM synthesises from numbered evidence, and guards check the synthesis.

  plan (LLM, cheap model) -> repair plan (code) -> execute tools w/ fallbacks
  -> evidence bundle [E1..En] -> synthesise (LLM, stronger model)
  -> guards: citation existence, numeric grounding, forbidden claims, confidence cap
  -> on guard failure: one regeneration with feedback -> else templated answer
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from typing import Any

from .core import Trace, Budget, BudgetExceeded, ToolHTTPError
from . import tools as T

PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "claude_haiku_4_5")
SYNTH_MODEL = os.environ.get("SYNTH_MODEL", "claude_sonnet_5_5")

TOOL_CATALOG = {
    "geocode": "地址 -> 经纬度（Census 主 / Nominatim 备）",
    "jurisdiction": "判断点位是否在 City of Ithaca 辖区内",
    "snap_parcel": "把点位吸附到宗地（0/15/35 米 + 门牌匹配）",
    "parcel_by_address": "空间吸附失败时按地址查宗地属性表",
    "zoning": "分区图层 + 宗地属性交叉核对，区分基础分区与叠加区",
    "historic": "历史街区",
    "flood": "洪水分区（FEMA → Esri 镜像 → 覆盖度探测 → 宗地旧字段）",
    "transit": "TCAT GTFS：400/800 米内站点、线路、班次",
    "zoning_rules": "从 2026-04 分区图表解析用途许可与体量指标（含列对齐校验）",
    "metrics": "计算可建体量、公交评分（纯计算）",
}
USE_KEYS = list(T.USE_ROWS.keys())
DEPS = {  # tool -> prerequisites (executor enforces; planner may forget)
    "jurisdiction": ["geocode"], "snap_parcel": ["geocode"], "parcel_by_address": ["geocode"],
    "zoning": ["snap_parcel"], "historic": ["snap_parcel"], "flood": ["snap_parcel"], "transit": ["geocode"],
    "zoning_rules": ["zoning"], "metrics": ["zoning_rules"],
}
CANON_ORDER = ["geocode", "jurisdiction", "snap_parcel", "parcel_by_address", "zoning", "historic", "flood",
               "transit", "zoning_rules", "metrics"]


# ---------------------------------------------------------------- LLM wrapper
def _llm(trace: Trace, span, model: str, system: str, user: str, max_tokens=1200) -> str:
    from anthropic import Anthropic
    span.args["reserved_usd"] = round(trace.reserve_llm(model, len(system) + len(user), max_tokens), 5)
    client = Anthropic(default_headers={"anthropic-version": "2023-06-01"})
    t0 = time.time()
    msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                 messages=[{"role": "user", "content": user}])
    span.args["model"] = model
    span.args["llm_ms"] = int((time.time() - t0) * 1000)
    trace.charge_llm(span, model, msg.usage.input_tokens, msg.usage.output_tokens)
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _json_from(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group())


# ---------------------------------------------------------------- planning
PLANNER_SYS = f"""你是城市选址调研 Agent 的规划器。只输出一个 JSON 对象，不要输出其他文字。
可用工具: {json.dumps(TOOL_CATALOG, ensure_ascii=False)}
用途关键字只能从 {USE_KEYS} 中选。
JSON schema:
{{"address": "完整街道地址，含城市；问题中没有具体地址则为 null",
  "uses": ["restaurant", ...],
  "proposed": {{"stories": 整数或 null, "height_ft": 整数或 null}},
  "questions": ["用户关心的子问题，如 用途许可/高度/公交/洪水/历史保护"],
  "steps": ["工具名", ...],
  "clarify": "如果缺少必要信息（如具体地址），写出要问用户的问题；否则 null"}}"""


def template_plan(q: str) -> dict:
    m = re.search(r"(\d{1,5}\s+[A-Za-z][A-Za-z .]+?(?:St|Street|Rd|Road|Ave|Avenue|Pl|Dr|Ln|Way)\b[^，。?？]*)", q, re.I)
    addr = m.group(1).strip() if m else None
    if addr and "ithaca" not in addr.lower():
        addr += ", Ithaca, NY"
    uses = [k for k, pats in {"restaurant": r"咖啡|餐|cafe|coffee|restaurant|酒吧|bar",
                               "retail": r"零售|商店|书店|retail|shop|store",
                               "office": r"办公|office", "multifamily": r"公寓|住宅|apartment|housing|residential",
                               "hotel": r"酒店|hotel"}.items() if re.search(pats, q, re.I)]
    st = re.search(r"(\d+)\s*(?:层|stor)", q)
    return {"address": addr, "uses": uses, "proposed": {"stories": int(st.group(1)) if st else None, "height_ft": None},
            "questions": [], "steps": CANON_ORDER, "clarify": None if addr else "请提供具体街道地址（例如 120 E State St）。"}


def plan(trace: Trace, q: str, use_llm: bool) -> dict:
    sp = trace.span("plan", "planner", {"question": q})
    raw = None
    if use_llm:
        try:
            raw = _llm(trace, sp, PLANNER_MODEL, PLANNER_SYS, q, 600)
            if trace.faults.get("planner") == "invalid_json":
                raw = "好的，我会先查询地理编码，然后……（模型没有按要求输出 JSON）"
            p = _json_from(raw)
            trace.close(sp, "ok", "LLM 生成计划", {"plan": p})
        except BudgetExceeded:
            raise
        except Exception as e:
            sp.failure_modes.append("FM-16")
            sp.warnings.append(f"planner output rejected: {type(e).__name__}: {str(e)[:100]}")
            p = template_plan(q)
            trace.close(sp, "degraded", "LLM 计划不可解析 → 改用规则模板计划", {"raw": (raw or "")[:300], "plan": p})
    else:
        p = template_plan(q)
        trace.close(sp, "ok", "规则模板计划（未启用 LLM）", {"plan": p})
    # ---- plan repair (code, not LLM)
    rp = trace.span("guard", "plan_repair")
    fixes = []
    steps = [s for s in p.get("steps", []) if s in TOOL_CATALOG]
    unknown = [s for s in p.get("steps", []) if s not in TOOL_CATALOG]
    if unknown:
        fixes.append(f"删除未知工具 {unknown}"); rp.failure_modes.append("FM-16")
    need = set(steps)
    changed = True
    while changed:
        changed = False
        for s in list(need):
            for d in DEPS.get(s, []):
                if d not in need:
                    need.add(d); changed = True; fixes.append(f"补上依赖 {d}（{s} 需要）")
    for must in ["jurisdiction", "zoning", "flood", "transit"]:   # policy: siting checklist is not up to the LLM
        if must not in need:
            need.add(must); fixes.append(f"按策略强制加入 {must}")
    p["uses"] = [u for u in p.get("uses", []) if u in USE_KEYS]
    p["steps"] = [s for s in CANON_ORDER if s in need]
    trace.close(rp, "ok" if not fixes else "degraded", "；".join(fixes) or "计划合法，无需修复", {"steps": p["steps"]})
    return p


# ---------------------------------------------------------------- execution
def _run_tool(trace, name, fn, *a, **kw) -> T.ToolResult:
    sp = trace.span("tool", name, {k: v for k, v in kw.items() if k != "parcel_attrs"})
    try:
        r = fn(trace, sp, *a, **kw)
    except BudgetExceeded:
        trace.close(sp, "error", "预算熔断"); raise
    except ToolHTTPError as e:
        r = T.ToolResult("error", {}, [], 0.0, f"{e.kind}: {e}", ["FM-02"] if e.retryable else ["FM-05"])
    except Exception as e:  # tool bug must not crash the run
        # never swallow silently: mark FM-24, surface in evidence, and fail the eval check
        r = T.ToolResult("error", {"exception": f"{type(e).__name__}: {str(e)[:120]}"}, [], 0.0,
                         f"工具内部异常 {type(e).__name__}: {str(e)[:120]}", ["FM-24"])
    sp.failure_modes += r.failure_modes
    sp.detail = {"data": r.data, "sources": r.sources, "confidence": r.confidence}
    trace.close(sp, r.status, r.note or r.status)
    return r


def execute(trace: Trace, p: dict) -> dict:
    R: dict[str, T.ToolResult] = {}
    addr = p.get("address")
    g = R["geocode"] = _run_tool(trace, "geocode", T.geocode, addr)
    if g.status not in ("ok", "degraded") or "lon" not in g.data:
        return R                      # nothing downstream is meaningful: stop, don't guess
    lon, lat = g.data["lon"], g.data["lat"]
    j = R["jurisdiction"] = _run_tool(trace, "jurisdiction", T.jurisdiction, lon, lat)
    if j.status == "ok" and not j.data.get("in_city"):
        s = trace.span("guard", "scope_gate"); s.failure_modes.append("FM-10")
        trace.close(s, "degraded", "超出辖区：停止调用 City 专属工具，避免用错规则集"); return R
    sp = R["snap_parcel"] = _run_tool(trace, "snap_parcel", T.snap_parcel, lon, lat, addr)
    if sp.status != "ok":
        fb = trace.span("fallback", "snap_parcel → parcel_by_address")
        trace.close(fb, "ok", "空间吸附失败，切换到属性反查")
        alt = R["parcel_by_address"] = _run_tool(trace, "parcel_by_address", T.parcel_by_address, addr, lon, lat)
        if alt.status == "ok":
            R["snap_parcel"] = alt
        else:
            s = trace.span("guard", "location_gate"); s.failure_modes.append("FM-21")
            trace.close(s, "degraded", "无法可靠确定宗地 → 不做任何分区结论（防止级联错误）")
            R["_location_unresolved"] = alt
            if "transit" in p["steps"]:
                R["transit"] = _run_tool(trace, "transit", T.transit, lon, lat)
            return R
    parcel = R["snap_parcel"].data.get("parcel")
    c = R["snap_parcel"].data.get("centroid")
    if c:
        lon, lat = c
    R["zoning"] = _run_tool(trace, "zoning", T.zoning, lon, lat, parcel_attrs=parcel)
    if "historic" in p["steps"]:
        R["historic"] = _run_tool(trace, "historic", T.historic, lon, lat)
    R["flood"] = _run_tool(trace, "flood", T.flood, lon, lat, parcel_attrs=parcel)
    if "transit" in p["steps"]:
        R["transit"] = _run_tool(trace, "transit", T.transit, lon, lat)
    zones = R["zoning"].data.get("base_zones") or []
    rules = []
    for z in zones[:2]:              # split-zoned parcels: evaluate each part
        rules.append(_run_tool(trace, f"zoning_rules[{z}]", T.zoning_rules, z, p.get("uses") or ["retail"]))
    R["zoning_rules"] = rules
    cs = trace.span("compute", "metrics")
    m = [T.metrics(R["snap_parcel"].data, r.data, R.get("transit").data if R.get("transit") else None) for r in rules] or \
        [T.metrics(R["snap_parcel"].data, None, R.get("transit").data if R.get("transit") else None)]
    R["metrics"] = m
    trace.close(cs, "ok", "确定性计算（不经过 LLM）", {"metrics": m})
    return R


# ---------------------------------------------------------------- evidence + synthesis
def evidence(R: dict, p: dict) -> list[dict]:
    ev = []

    def add(kind, text, res: T.ToolResult | None, conf=None, src=None):
        srcs = src or (res.sources if res else [])
        ev.append({"id": f"E{len(ev)+1}", "kind": kind, "text": text,
                   "status": res.status if res else "ok", "confidence": conf if conf is not None else (res.confidence if res else 0.9),
                   "source": srcs[-1] if srcs else None, "failure_modes": res.failure_modes if res else []})
    g = R.get("geocode")
    if g:
        add("location", f"地理编码：{g.data.get('matched') or '无结果'}（{g.note}）", g)
    j = R.get("jurisdiction")
    if j:
        add("jurisdiction", "位于 City of Ithaca 辖区内" if j.data.get("in_city") else j.note, j)
    loc = R.get("_location_unresolved")
    if loc:
        cands = (R.get("snap_parcel").data.get("candidates") if R.get("snap_parcel") else None) or []
        add("location", f"无法确定宗地：{loc.note}；附近宗地候选：{cands}", loc, 0.0)
    sp = R.get("snap_parcel")
    if sp and sp.status == "ok":
        a = sp.data["parcel"]
        add("parcel", f"宗地 {a.get('LOCADDR')}（PRINTKEY {a.get('PRINTKEY')}），面积约 {sp.data.get('lot_area_sqft')} sqft，"
                      f"现状 {a.get('PROPCLASS')}/{a.get('DESCRIPTIO')}；{sp.note}", sp)
    z = R.get("zoning")
    if z:
        add("zoning", f"基础分区 {z.data.get('base_zones')}，叠加区 {z.data.get('overlays')} {z.data.get('overlay_desc')}；{z.note}", z)
    h = R.get("historic")
    if h:
        add("historic", h.note + (f"：{h.data['districts']}" if h.data.get("districts") else ""), h)
    if sp and sp.status == "ok" and sp.data["parcel"].get("HISTPROP") == "YES":
        add("historic", "宗地属性 HISTPROP=YES：该宗地本身登记为历史建筑", sp)
    f = R.get("flood")
    if f:
        add("flood", (f.note if f.data.get("zone") else f.note) + f"（查询链：{' → '.join(f.data.get('chain', []))}）", f)
    for r in R.get("zoning_rules", []):
        zname = r.data.get("zone")
        for u, rec in r.data.get("uses", {}).items():
            sym = {"•": "允许", "-": "不允许", "SP": "需特别许可(SP)"}.get(rec["value"] or "", f"无法确定（{rec['value']}）")
            add("rule", f"{zname} 用途「{rec['row']}」：{sym}（图表第 {rec['page']} 页{'' if rec['aligned'] else '，列对齐失败'}）",
                r, 0.9 if rec["aligned"] else 0.3, [{**r.sources[0], "url": f"{T.CHART_URL}#page={rec['page']}"}])
        for k, rec in r.data.get("bulk", {}).items():
            label = {"max_stories": "最大层数", "max_height_ft": "最大高度", "max_lot_coverage": "最大建筑覆盖率"}[k]
            val = rec["value"] if rec["value"] else "无可靠值"
            add("rule", f"{zname} {label}：{val}（图表第 {rec['page']} 页，{rec['row']}{'' if rec['aligned'] else '，列对齐失败，未采用'}）",
                r, 0.9 if rec["aligned"] and rec["value"] else 0.3, [{**r.sources[0], "url": f"{T.CHART_URL}#page={rec['page']}"}])
        if r.note:
            add("rule_note", f"{zname} 解析备注：{r.note}", r)
        cur = r.data.get("currency")
        if cur:
            add("currency", cur["warning"], r, 0.9, [r.sources[-1]])
            break_currency = True
    for i, m in enumerate(R.get("metrics", [])):
        bits = []
        if m.get("envelope_gfa_sqft"):
            bits.append(f"理论可建建筑面积上限约 {m['envelope_gfa_sqft']} sqft（{m['envelope_formula']}）")
        if m.get("stories_estimated_from_height"):
            bits.append(f"按 12 ft 层高估算约 {m['stories_estimated_from_height']} 层（估算）")
        if bits:
            add("metric", "；".join(bits), None, 0.6, [{"id": "compute", "title": "Agent 确定性计算", "url": None}])
    t = R.get("transit")
    if t and t.status in ("ok", "degraded"):
        n = t.data.get("nearest", [])[:3]
        mm = (R.get("metrics") or [{}])[0]
        add("transit", f"400 米步行内 {t.data.get('stops_400m')} 个站点、{len(t.data.get('routes_400m', []))} 条线路、工作日停靠 {t.data.get('weekday_stop_events_400m')} 班次；"
                       f"最近站点 {[(x['name'], x['walk_m']) for x in n]}；公交评分 {mm.get('transit_score')}（{mm.get('transit_score_formula')}）；{t.note}", t)
    for name, res in R.items():                       # surface every failed tool instead of omitting it
        if isinstance(res, T.ToolResult) and res.status == "error":
            add("tool_error", f"{name} 查询失败，结果缺失：{res.note}", res, 0.0)
    # dedupe identical texts (e.g. currency repeated for split zones)
    seen, out = set(), []
    for e in ev:
        if e["text"] in seen:
            continue
        seen.add(e["text"]); e["id"] = f"E{len(out)+1}"; out.append(e)
    return out


SYNTH_SYS = """你是城市选址调研 Agent 的综合器。只依据给定证据回答，用中文。
硬性规则：
1. 每个事实句末尾用 [E编号] 引用证据；不得引用不存在的编号。
2. 不得出现证据中没有的数字。
3. 证据 status 为 unverified/empty/error 的内容，必须明确说“未能核实”，不得推断为否定结论（例如：查不到洪水分区 ≠ 不在洪水区）。
4. 置信度低于 0.5 的证据只能作为“待核实”。
5. 若用户拟建层数/高度与证据冲突，直接指出。
只输出 JSON：{"verdict": "可行|有条件可行|不可行|无法判断", "answer_md": "分点的 Markdown 回答", "open_questions": ["需要人工核实的事项"]}"""


def synthesize(trace, q, p, ev, use_llm, feedback=None) -> dict:
    sp = trace.span("synth", "synthesizer" if not feedback else "synthesizer(retry)")
    payload = {"question": q, "plan": {k: p.get(k) for k in ("address", "uses", "proposed")},
               "evidence": [{k: e[k] for k in ("id", "kind", "text", "status", "confidence")} for e in ev]}
    if feedback:
        payload["上一次输出未通过校验，请修正"] = feedback
    if use_llm:
        try:
            raw = _llm(trace, sp, SYNTH_MODEL, SYNTH_SYS, json.dumps(payload, ensure_ascii=False), 4000)
            out = _json_from(raw)
            if trace.faults.get("synth") == "hallucinate" and not feedback:
                out["answer_md"] += "\n- 该地块步行 3 分钟可达 Wegmans，日均客流 12000 人 [E99]。"
            trace.close(sp, "ok", f"LLM 综合 · verdict={out.get('verdict')}", {"output": out})
            return out
        except BudgetExceeded:
            raise
        except Exception as e:
            sp.failure_modes.append("FM-16")
            trace.close(sp, "error", f"综合输出不可解析：{type(e).__name__}")
            return {}
    out = template_answer(ev)
    trace.close(sp, "ok", "模板综合（未启用 LLM）", {"output": out})
    return out


def template_answer(ev):
    lines = [f"- {e['text']} [{e['id']}]" + ("（待核实）" if e["confidence"] < 0.5 or e["status"] in ("unverified", "empty", "error") else "")
             for e in ev]
    low = [e for e in ev if e["confidence"] < 0.5 or e["status"] in ("unverified", "empty", "error")]
    return {"verdict": "无法判断" if any(e["kind"] in ("location", "zoning") and e["confidence"] < 0.5 for e in ev) else "有条件可行",
            "answer_md": "以下为逐条证据汇总（模板降级输出，未经 LLM 改写）：\n" + "\n".join(lines),
            "open_questions": [e["text"][:80] for e in low]}


NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(ft|英尺|层|%|米|m\b|sqft|平方英尺|班次|个站点|条线路)")
FORBIDDEN = {"flood": r"(不在|不属于|没有|无)[^。；\n]{0,8}(洪水|洪泛|泛洪)[^。；\n]{0,6}(区|风险)|洪水风险(低|较低|很小)",
             }


NEGATED_DISCLAIMER = re.compile(r"(不能|不应|不可|不宜|并非|不代表|不等于|不意味|并不意味|不说明|≠)[^。；，,\n]{0,6}$")
QUOTES = "“”\"'「」『』（(【 "


def flood_negative_claims(text: str) -> list[str]:
    """Find '不在洪水区 / 无洪水风险' style claims, ignoring ones inside a disclaimer such as
    '这不代表不在洪水区' (a v1 false positive observed in a live run)."""
    hits = []
    for m in re.finditer(FORBIDDEN["flood"], text):
        prefix = text[max(0, m.start() - 14):m.start()].rstrip(QUOTES)   # v2 FP: 这不能解读为“不在洪水区”
        if NEGATED_DISCLAIMER.search(prefix):
            continue
        hits.append(m.group())
    return hits


def guards(trace, ans: dict, ev: list[dict], question: str = "") -> tuple[list[str], dict]:
    sp = trace.span("guard", "answer_guards")
    issues, fm = [], []
    text = ans.get("answer_md", "")
    ids = {e["id"] for e in ev}
    cited = set(re.findall(r"\[(E\d+)\]", text))
    bad = sorted(cited - ids)
    if bad:
        issues.append(f"引用了不存在的证据 {bad}"); fm.append("FM-17")
    if not cited:
        issues.append("回答没有任何引用"); fm.append("FM-17")
    evtext = " ".join(e["text"] for e in ev) + " " + json.dumps([e["text"] for e in ev], ensure_ascii=False)
    ev_nums = {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", evtext + " " + question)}  # user's own numbers are grounded
    ungrounded = sorted({m.group(1) for m in NUM_RE.finditer(text) if m.group(1).replace(",", "") not in ev_nums})
    if ungrounded:
        issues.append(f"出现证据中没有的数字 {ungrounded}"); fm.append("FM-17")
    f_ev = next((e for e in ev if e["kind"] == "flood"), None)
    if f_ev and f_ev["status"] != "ok" and flood_negative_claims(text):
        issues.append("洪水证据未核实，但回答给出了“无洪水风险/不在洪水区”类否定结论"); fm.append("FM-01")
    # confidence cap: verdict can't be stronger than the weakest critical evidence
    crit = [e["confidence"] for e in ev if e["kind"] in ("location", "parcel", "zoning", "jurisdiction")]
    cap = round(min(crit), 2) if crit else 0.0
    rule_conf = [e["confidence"] for e in ev if e["kind"] == "rule"]
    cap = round(min(cap, (sum(rule_conf) / len(rule_conf)) if rule_conf else 0.5), 2)
    meta = {"confidence_cap": cap, "cited": sorted(cited, key=lambda x: int(x[1:])), "ungrounded_numbers": ungrounded}
    if cap < 0.5 and ans.get("verdict") in ("可行", "不可行"):
        issues.append(f"关键证据置信度仅 {cap}，不应给出确定性结论「{ans.get('verdict')}」"); fm.append("FM-18")
    sp.failure_modes += sorted(set(fm))
    trace.close(sp, "ok" if not issues else "degraded", "全部通过" if not issues else "；".join(issues), meta)
    return issues, meta


# ---------------------------------------------------------------- entry
def run(question: str, faults: dict | None = None, budget: Budget | None = None, use_llm: bool = True) -> dict:
    trace = Trace(question, budget, faults)
    result: dict[str, Any] = {}
    ev: list = []
    try:
        p = plan(trace, question, use_llm)
        if p.get("clarify") and not p.get("address"):
            s = trace.span("guard", "clarify_gate"); s.failure_modes.append("FM-19")
            trace.close(s, "degraded", "缺少必要输入，先问用户而不是猜一个地址")
            result = {"verdict": "需要澄清", "answer_md": p["clarify"], "open_questions": [p["clarify"]], "evidence": []}
        else:
            R = execute(trace, p)
            ev = evidence(R, p)
            ans = synthesize(trace, question, p, ev, use_llm)
            issues, meta = guards(trace, ans, ev, question) if ans else (["综合失败"], {"confidence_cap": 0})
            if issues:
                fb = trace.span("fallback", "regenerate_with_feedback")
                trace.close(fb, "ok", "护栏未通过 → 带反馈重试一次")
                ans2 = synthesize(trace, question, p, ev, use_llm, feedback=issues)
                issues2, meta = guards(trace, ans2, ev, question) if ans2 else (["综合失败"], meta)
                if issues2:
                    fb2 = trace.span("fallback", "template_answer")
                    trace.close(fb2, "degraded", "重试仍未通过 → 输出逐条证据模板（宁可丑，不可错）")
                    ans = template_answer(ev); meta["template"] = True
                else:
                    ans = ans2
            result = {**ans, "evidence": ev, "confidence": meta.get("confidence_cap"), "plan": p}
    except BudgetExceeded as e:
        s = trace.span("guard", "budget_breaker"); s.failure_modes.append("FM-20")
        trace.close(s, "error", f"预算熔断：{e}；用已收集的证据输出模板答案（不再调用 LLM）")
        if ev:
            t = template_answer(ev)
            result = {**t, "verdict": "无法判断", "answer_md": f"本次调研触发预算上限，已停止调用模型。\n\n" + t["answer_md"],
                      "evidence": ev, "confidence": 0.0}
        else:
            result = {"verdict": "无法判断", "answer_md": f"本次调研触发预算上限（{e}），已停止。", "evidence": []}
    out = trace.to_json()
    out["result"] = result
    out["failure_modes_triggered"] = sorted({fm for s in trace.spans for fm in s.failure_modes})
    return out
