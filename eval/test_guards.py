"""Guard precision tests — guards are classifiers and need their own eval set.
Cases marked 'v1_fp' are real false positives observed in live runs."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from agent.agent import flood_negative_claims, NUM_RE
CASES = [  # (text, should_flag, origin)
 ("该地块不在洪水区，可以放心建设。", True, "synthetic"),
 ("洪水风险较低。", True, "synthetic"),
 ("查询无结果，因此没有洪水风险。", True, "synthetic"),
 ("这不代表不在洪水区，需在 FEMA FIRM Viewer 人工核对。", False, "v1_fp (live run N1)"),
 ("空结果≠不在洪水区。", False, "synthetic"),
 ("不能据此判断是否不在洪水区。", False, "synthetic"),
 ("洪水分区未能核实 [E7]。", False, "synthetic"),
 ("这不能解读为“不在洪水区”，需人工核对 FEMA FIRM。", False, "v2_fp (live run F1)"),
 ("虽然不能确定，但该地块不在洪水区。", True, "synthetic: 免责声明在前一分句，不应豁免"),
 ("结果为空，不应视为没有洪水风险。", False, "synthetic"),
]
ok = 0
for t, want, origin in CASES:
    got = bool(flood_negative_claims(t)); ok += got == want
    print(("PASS" if got == want else "FAIL"), want, got, origin, "|", t)
print(f"flood guard: {ok}/{len(CASES)}")
