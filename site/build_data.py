"""Bundle eval runs + failure-mode catalog into site/data.js (static, no backend)."""
import json, re, pathlib
ROOT = pathlib.Path(__file__).parent.parent
rep = json.loads((ROOT / "eval/report.json").read_text())
GROUP = {"N": "自然问题", "F": "故障注入", "R": "一致性复跑", "B": "无 LLM 基线"}
TITLE = {
 "N1_commons_6story": "Commons 6 层混合用途", "N2_collegetown_8story": "Collegetown 8 层学生公寓",
 "N3_split_zoned": "跨分区宗地（CBD-85/100）", "N4_out_of_jurisdiction": "超出 City 辖区",
 "N5_nonexistent_address": "不存在的地址", "N6_unmatched_parcel": "宗地名册无匹配", "N7_vague_question": "含糊问题，缺地址",
 "F1_flood_timeout": "FEMA + 镜像全部超时", "F2_geocoders_rate_limited": "两个地理编码都 429",
 "F3_pdf_misaligned": "PDF 表格列错位", "F4_zoning_empty": "分区图层返回空", "F5_planner_invalid_json": "规划器输出非法 JSON",
 "F6_synth_hallucination": "回答中出现伪造引用", "F7_budget_breaker": "预算上限 $0.002", "F8_gtfs_stale": "GTFS 已过期",
 "F9_parcel_layer_empty": "宗地图层返回空", "R1_consistency_a": "同一问题复跑 A", "R1_consistency_b": "同一问题复跑 B",
 "B0_no_llm_baseline": "关闭 LLM 的模板基线"}
def slim(o):
    for s in o["spans"]:
        s.pop("started", None)
        d = json.dumps(s.get("detail"), ensure_ascii=False)
        if len(d) > 2600: s["detail"] = {"_truncated": d[:2600] + " …"}
    return o
runs = []
for r in rep:
    o = slim(json.loads((ROOT / f"runs/{r['tag']}.json").read_text()))
    runs.append({"tag": r["tag"], "title": TITLE.get(r["tag"], r["tag"]), "group": GROUP[r["tag"][0]], "row": r, "run": o})
hist = {}
for tag, f in [("v1_guard_fp", "runs/V1_guard_false_positive.json"), ("v2_guard_fp", "runs/v2/F1_flood_timeout_guard_fp.json"), ("v1_budget", "runs/v1/F7_budget_breaker.json")]:
    o = json.loads((ROOT / f).read_text())
    first = next((s for s in o["spans"] if s["name"] == "synthesizer"), None)
    hist[tag] = {"usd": o["ledger"]["total_usd"], "ms": o["ledger"]["wall_ms"],
                 "guard": [s["summary"] for s in o["spans"] if s["name"] == "answer_guards"],
                 "first_answer": (first["detail"] or {}).get("output", {}).get("answer_md", "") if first else ""}
# failure-mode table from markdown (single source of truth)
md = (ROOT / "FAILURE_MODES.md").read_text()
fms = []
for line in md.splitlines():
    m = re.match(r"\| (FM-\d+) \|", line)
    if m:
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        fms.append(dict(zip(["id", "name", "layer", "observed", "signal", "strategy", "user", "sev", "state"], c)))
counts = {}
for r in rep:
    for f in r["fms"]: counts[f] = counts.get(f, 0) + 1
v = {k: json.loads((ROOT / f"eval/{k}.json").read_text()) for k in ["report_v1", "report_v2", "report"]}
iters = [{"v": k, "scen_pass": sum(x["passed"] == x["total"] for x in rows), "n": len(rows),
          "checks": sum(x["passed"] for x in rows), "checks_total": sum(x["total"] for x in rows),
          "usd": round(sum(x["usd"] for x in rows), 3)} for k, rows in v.items()]
out = {"runs": runs, "hist": hist, "fms": fms, "fm_counts": counts, "iters": iters}
(ROOT / "site/data.js").write_text("window.DATA=" + json.dumps(out, ensure_ascii=False) + ";")
print(len(runs), len(fms), iters, (ROOT / "site/data.js").stat().st_size)
