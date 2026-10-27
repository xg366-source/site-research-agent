# 场地调研 Agent

用户问一个选址问题（City of Ithaca, NY），Agent 自己规划步骤：查分区/宗地 GIS、读规划 PDF、查洪水与公交、做确定性计算，最后给出带 [E#] 引用的回答，全程记录调用链、延迟与成本。

在线展示：https://site-research-agent.pplx.app

重点不在“跑通”，而在失败处理，见 [FAILURE_MODES.md](FAILURE_MODES.md)。

- `agent/`：core（预算、计量、重试、故障注入）、tools（五态 ToolResult 与降级阶梯）、agent（规划 → 执行 → 证据 → 合成 → 护栏）
- `eval/`：19 个场景（自然 / 故障注入 / 一致性 / 无 LLM 基线），`run_eval.py`、`test_guards.py`，v1/v2/v3 报告
- `runs/`：每次运行的完整 trace JSON
- `site/`：静态展示页（调用链回放、失败模式清单、成本、迭代），`python3 site/build_data.py` 重新打包数据

运行：先 `export SITE_AGENT_CONTACT=你的邮箱`（Nominatim 要求在 User-Agent 中提供联系方式），再 `PYTHONPATH=. python3 -m uvicorn server:app --port 8077`，再 `python3 eval/run_eval.py`。
