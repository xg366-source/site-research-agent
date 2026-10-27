"""Eval set: natural scenarios against live data + deterministic fault injection.
Each check is (name, fn(out)->bool). Checks encode *behaviour under failure*, not just 'got an answer'."""
import re, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from agent.agent import flood_negative_claims   # the checker reuses the validated guard (v1 checker had its own false positive)
BASE = "我想在 120 E State St, Ithaca 盖一栋 6 层的混合用途楼，底层开咖啡馆，可行吗？"

def ans(o): return o["result"].get("answer_md", "")
def fms(o): return set(o.get("failure_modes_triggered", []))
def span(o, name): return [s for s in o["spans"] if s["name"].startswith(name)]
def verdict(o): return o["result"].get("verdict")
NO_FLOOD_CLAIM = re.compile(r"(不在|不属于|没有|无)[^。；\n]{0,8}(洪水|洪泛|泛洪)[^。；\n]{0,6}(区|风险)|洪水风险(低|较低|很小)")
def flood_ok(o):
    f = span(o, "flood")
    if not f or f[0]["status"] == "ok": return True
    return not flood_negative_claims(ans(o))
def cites_valid(o):
    ids = {e["id"] for e in o["result"].get("evidence", [])}
    c = set(re.findall(r"\[(E\d+)\]", ans(o)))
    return bool(c) and c <= ids
def no_tool_crash(o): return not any(s["kind"] == "tool" and "工具内部异常" in (s["summary"] or "") for s in o["spans"])
COMMON = [("引用全部有效", cites_valid), ("洪水未核实时不做否定结论", flood_ok), ("无静默工具异常", no_tool_crash)]

S = [
 dict(tag="N1_commons_6story", q=BASE, checks=COMMON + [
   ("识别 60 ft 高度上限", lambda o: "60" in ans(o)),
   ("提及历史街区审查", lambda o: re.search(r"历史|ILPC", ans(o)) is not None),
   ("提及街道层活跃用途叠加区", lambda o: re.search(r"活跃|SLAUOZ|active", ans(o), re.I) is not None),
   ("不给出无条件“可行”", lambda o: verdict(o) != "可行")]),
 dict(tag="N2_collegetown_8story", q="113 Dryden Rd, Ithaca 能不能建一栋 8 层学生公寓？公交怎么样？", checks=COMMON + [
   ("空间吸附失败后走属性反查", lambda o: bool(span(o, "parcel_by_address")) and span(o, "parcel_by_address")[0]["status"] == "ok"),
   ("识别 MU-2", lambda o: "MU-2" in ans(o)),
   ("给出公交证据", lambda o: re.search(r"站点|线路|班次", ans(o)) is not None)]),
 dict(tag="N3_split_zoned", q="202 E Seneca St, Ithaca 适合做办公楼吗？最高能建多高？", checks=COMMON + [
   ("检测到跨分区宗地 (FM-12)", lambda o: "FM-12" in fms(o)),
   ("对两个分区分别解析规则", lambda o: len(span(o, "zoning_rules")) == 2),
   ("同时提及 CBD-85 与 CBD-100", lambda o: "CBD-85" in ans(o) and "CBD-100" in ans(o))]),
 dict(tag="N4_out_of_jurisdiction", q="1 Judd Falls Rd, Ithaca 可以开餐厅吗？", checks=[
   ("识别超出辖区 (FM-10)", lambda o: "FM-10" in fms(o)),
   ("不调用 City 分区规则", lambda o: not span(o, "zoning_rules")),
   ("结论为无法判断/需澄清", lambda o: verdict(o) in ("无法判断", "需要澄清"))]),
 dict(tag="N5_nonexistent_address", q="999 Fakename St, Ithaca 能开零售店吗？", checks=[
   ("地理编码为空时停止 (FM-09)", lambda o: "FM-09" in fms(o)),
   ("不编造分区", lambda o: not span(o, "zoning")),
   ("结论为无法判断/需澄清", lambda o: verdict(o) in ("无法判断", "需要澄清"))]),
 dict(tag="N6_unmatched_parcel", q="130 E Clinton St, Ithaca 开咖啡馆可以吗？", checks=[
   ("宗地无法确认时触发位置闸门", lambda o: bool(span(o, "location_gate"))),
   ("不输出分区结论", lambda o: not span(o, "zoning_rules")),
   ("结论为无法判断/需澄清", lambda o: verdict(o) in ("无法判断", "需要澄清"))]),
 dict(tag="N7_vague_question", q="Ithaca Commons 附近哪里适合开一家独立书店？", checks=[
   ("缺地址时先澄清 (FM-19)", lambda o: verdict(o) == "需要澄清" or "FM-19" in fms(o)),
   ("不调用 GIS 工具", lambda o: not span(o, "geocode"))]),
 dict(tag="F1_flood_timeout", q=BASE, faults={"flood": "timeout", "flood_mirror": "timeout"}, checks=COMMON + [
   ("洪水标为未核实", lambda o: span(o, "flood")[0]["status"] == "unverified"),
   ("回答中提示洪水待核实", lambda o: re.search(r"(未能|无法)[^。]{0,6}(核实|确认)", ans(o)) is not None)]),
 dict(tag="F2_geocoders_rate_limited", q=BASE, faults={"geocode": "http_429", "geocode_fallback": "http_429"}, checks=[
   ("两级地理编码失败后停止", lambda o: not span(o, "zoning")),
   ("结论为无法判断", lambda o: verdict(o) in ("无法判断", "需要澄清"))]),
 dict(tag="F3_pdf_misaligned", q=BASE, faults={"docs": "misaligned"}, checks=COMMON + [
   ("检测到列错位 (FM-13)", lambda o: "FM-13" in fms(o)),
   ("不采用错位数值算体量", lambda o: not any(m.get("envelope_gfa_sqft") for m in (span(o, "metrics")[0]["detail"] or {}).get("metrics", [])))]),
 dict(tag="F4_zoning_empty", q=BASE, faults={"zoning": "empty"}, checks=COMMON + [
   ("分区为空时置信度上限 < 0.5", lambda o: (o["result"].get("confidence") or 0) < 0.5),
   ("不给出可行/不可行定论", lambda o: verdict(o) not in ("可行", "不可行"))]),
 dict(tag="F5_planner_invalid_json", q=BASE, faults={"planner": "invalid_json"}, checks=COMMON + [
   ("计划解析失败 → 模板计划 (FM-16)", lambda o: "FM-16" in fms(o)),
   ("仍完成调研", lambda o: bool(span(o, "zoning_rules")))]),
 dict(tag="F6_synth_hallucination", q=BASE, faults={"synth": "hallucinate"}, checks=COMMON + [
   ("护栏抓到伪造引用/数字 (FM-17)", lambda o: "FM-17" in fms(o)),
   ("最终回答不含 E99", lambda o: "E99" not in ans(o))]),
 dict(tag="F7_budget_breaker", q=BASE, budget={"max_usd": 0.002, "max_tool_calls": 30, "max_wall_s": 120}, checks=[
   ("触发预算熔断 (FM-20)", lambda o: "FM-20" in fms(o)),
   ("实际花费不超过上限", lambda o: o["ledger"]["total_usd"] <= 0.002)]),
 dict(tag="F8_gtfs_stale", q=BASE, faults={"transit": "stale"}, checks=COMMON + [
   ("检测到公交数据过期 (FM-22)", lambda o: "FM-22" in fms(o))]),
 dict(tag="F9_parcel_layer_empty", q=BASE, faults={"parcel": "empty"}, checks=[
   ("宗地全空时触发位置闸门", lambda o: bool(span(o, "location_gate"))),
   ("不输出分区结论", lambda o: not span(o, "zoning_rules"))]),
 dict(tag="R1_consistency_a", q=BASE, checks=COMMON),
 dict(tag="R1_consistency_b", q=BASE, checks=COMMON),
 dict(tag="B0_no_llm_baseline", q=BASE, use_llm=False, checks=COMMON),
]
