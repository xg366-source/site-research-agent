import json, time, pathlib
from fastapi import FastAPI
from pydantic import BaseModel
from agent.agent import run
from agent.core import Budget
app = FastAPI()
RUNS = pathlib.Path(__file__).parent / "runs"
class Req(BaseModel):
    question: str
    faults: dict = {}
    budget: dict = {}
    use_llm: bool = True
    tag: str = ""
@app.get("/health")
def health(): return {"ok": True}
@app.post("/run")
def do_run(r: Req):
    out = run(r.question, r.faults, Budget(**r.budget) if r.budget else None, r.use_llm)
    out["tag"] = r.tag
    (RUNS / f"{r.tag or out['run_id']}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return out
