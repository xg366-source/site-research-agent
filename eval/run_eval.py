import json, sys, time, pathlib, requests
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from scenarios import S
ROOT = pathlib.Path(__file__).parent.parent
only = sys.argv[1:]  # optional tags
rows = []
for sc in S:
    if only and sc["tag"] not in only: continue
    t0 = time.time()
    body = {"question": sc["q"], "faults": sc.get("faults", {}), "budget": sc.get("budget", {}), "use_llm": sc.get("use_llm", True), "tag": sc["tag"]}
    o = requests.post("http://localhost:8077/run", json=body, timeout=600).json()
    checks = []
    for name, fn in sc["checks"]:
        try: ok = bool(fn(o))
        except Exception as e: ok = False
        checks.append({"name": name, "pass": ok})
    row = {"tag": sc["tag"], "question": sc["q"], "faults": sc.get("faults", {}), "budget": sc.get("budget"), "use_llm": sc.get("use_llm", True),
           "verdict": o["result"].get("verdict"), "confidence": o["result"].get("confidence"),
           "checks": checks, "passed": sum(c["pass"] for c in checks), "total": len(checks),
           "fms": o.get("failure_modes_triggered", []), "usd": o["ledger"]["total_usd"], "wall_ms": o["ledger"]["wall_ms"],
           "http_calls": o["ledger"]["http_calls"], "tokens_in": o["ledger"]["llm_tokens_in"], "tokens_out": o["ledger"]["llm_tokens_out"],
           "wasted_ms": o["ledger"]["ms_spent_on_failed_or_empty_tools"], "n_spans": len(o["spans"])}
    rows.append(row)
    print(f"{row['tag']:28s} {row['passed']}/{row['total']} verdict={row['verdict']} conf={row['confidence']} ${row['usd']:.4f} {row['wall_ms']}ms {row['fms']}", flush=True)
    for c in checks:
        if not c["pass"]: print("    FAIL:", c["name"], flush=True)
out = ROOT / "eval" / ("report.json" if not only else "report_partial.json")
out.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
