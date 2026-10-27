(() => {
const D = window.DATA;
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const usd = v => v > 0 ? "$" + (v < 0.01 ? v.toFixed(4) : v.toFixed(3)) : "$0";
const sec = ms => ms >= 1000 ? (ms / 1000).toFixed(1) + " 秒" : ms + " ms";
const FM = Object.fromEntries(D.fms.map(f => [f.id, f]));
const KIND = {plan: "规划", tool: "工具", validate: "校验", fallback: "降级", compute: "计算", synth: "合成", guard: "护栏"};
const STATUS = {ok: "ok", degraded: "降级", unverified: "未核实", error: "错误", empty: "空"};
const runs = D.runs;
const byTag = Object.fromEntries(runs.map(r => [r.tag, r]));

/* ---------- hero stats ---------- */
const rows = runs.map(r => r.row);
const llm = rows.filter(r => r.use_llm && r.usd > 0.02).map(r => r.usd).sort((a, b) => a - b);
const med = llm.length % 2 ? llm[(llm.length - 1) / 2] : (llm[llm.length / 2 - 1] + llm[llm.length / 2]) / 2;
const pass = rows.filter(r => r.passed === r.total).length;
const ck = rows.reduce((a, r) => a + r.passed, 0), ckt = rows.reduce((a, r) => a + r.total, 0);
const unsafe = rows.filter(r => r.checks.some(c => c.name.startsWith("洪水未核实") && !c.pass)).length;
$("#stats").innerHTML = [
  [`${pass}/${rows.length}`, `评测场景全部通过（${ck}/${ckt} 项检查）`],
  [`${D.fms.length}`, "类失败模式，23 类来自真实服务调用"],
  [usd(med), "完整回答的中位成本，约 22 秒"],
  [`${unsafe}/${rows.length}`, "危险错误：证据未核实却说“安全”"],
].map(([b, s]) => `<div class="stat"><b>${b}</b><span>${s}</span></div>`).join("");

/* ---------- picker ---------- */
const groups = [...new Set(runs.map(r => r.group))];
$("#picker").innerHTML = groups.map(g => `<div class="pgroup"><h4>${g}</h4><div class="items">${
  runs.filter(r => r.group === g).map(r => `<button class="pbtn" data-tag="${r.tag}" aria-pressed="false"><span class="lbl">${esc(r.title)}</span><span class="tag">${r.tag.split("_")[0]}</span></button>`).join("")
}</div></div>`).join("");
$("#picker").addEventListener("click", e => { const b = e.target.closest(".pbtn"); if (b) select(b.dataset.tag); });

let cur = null, tab = "answer";
function select(tag, scroll) {
  cur = byTag[tag];
  document.querySelectorAll(".pbtn").forEach(b => b.setAttribute("aria-pressed", b.dataset.tag === tag));
  render();
  if (scroll) $("#replay").scrollIntoView({behavior: "smooth"});
}

function md(text, evIds) {
  const cite = s => s.replace(/\[(E\d+)\]/g, (m, id) => `<button class="cite${evIds.has(id) ? "" : " bad"}" data-ev="${id}" title="${evIds.has(id) ? "查看证据 " + id : "不存在的证据"}">${id}</button>`);
  const inline = s => cite(esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"));
  const lines = text.split("\n");
  let html = "", depth = -1;
  for (const raw of lines) {
    const m = raw.match(/^(\s*)[-*] (.*)$/);
    if (m) {
      const d = Math.floor(m[1].length / 2);
      while (depth < d) { html += "<ul>"; depth++; }
      while (depth > d) { html += "</ul>"; depth--; }
      html += `<li>${inline(m[2])}</li>`;
    } else {
      while (depth >= 0) { html += "</ul>"; depth--; }
      const h = raw.match(/^#{1,6}\s+(.*)$/);
      if (h) html += `<h4 class="ah">${inline(h[1])}</h4>`;
      else if (raw.trim()) html += `<p>${inline(raw)}</p>`;
    }
  }
  while (depth >= 0) { html += "</ul>"; depth--; }
  return html.replace(/<\/li><ul>/g, "<ul>").replace(/<\/ul>(?=<li>|<\/ul>)/g, "</ul></li>");
}

function render() {
  const {run, row} = cur, res = run.result || {};
  const ev = res.evidence || [];
  const evIds = new Set(ev.map(e => e.id));
  const faults = Object.entries(row.faults || {}).map(([k, v]) => `<span class="chip fault mono">注入 ${esc(k)}=${esc(v)}</span>`).join("");
  const budget = row.budget && row.budget.max_usd ? `<span class="chip fault mono">预算 $${row.budget.max_usd}</span>` : "";
  const nollm = row.use_llm ? "" : `<span class="chip">关闭 LLM</span>`;
  $("#stage").innerHTML = `
    <p class="q">${esc(run.question)}</p>
    <div class="meta">
      <span class="chip v-${esc(res.verdict)}">${esc(res.verdict || "—")}</span>
      <span class="chip mono">置信度 ${res.confidence ?? "—"}</span>
      <span class="chip mono">${usd(run.ledger.total_usd)}</span>
      <span class="chip mono">${sec(run.ledger.wall_ms)}</span>
      <span class="chip mono">检查 ${row.passed}/${row.total}</span>
      ${faults}${budget}${nollm}
    </div>
    <div class="tabs" role="tablist">
      ${[["answer", "回答"], ["trace", `调用链 · ${run.spans.length}`], ["evidence", `证据 · ${ev.length}`]].map(([k, l]) =>
        `<button class="tab" role="tab" data-tab="${k}" aria-selected="${tab === k}">${l}</button>`).join("")}
    </div>
    <div class="panel" id="panel"></div>`;
  $("#stage .tabs").addEventListener("click", e => { const b = e.target.closest(".tab"); if (b) { tab = b.dataset.tab; render(); } });
  const p = $("#panel");
  if (tab === "answer") {
    const oq = (res.open_questions || []).filter(Boolean);
    p.innerHTML = `<div class="answer">${md(res.answer_md || "（无回答）", evIds)}</div>` +
      (oq.length ? `<div class="oq"><h4>需要人工核实</h4><ul>${oq.map(q => `<li>${md(q, evIds).replace(/^<p>|<\/p>$/g, "")}</li>`).join("")}</ul></div>` : "");
  } else if (tab === "trace") {
    const L = run.ledger, maxLat = Math.max(...run.spans.map(s => s.latency_ms || 0), 1);
    p.innerHTML = `<div class="ledger">
      <div><b>${usd(L.total_usd)}</b><span>总成本</span></div>
      <div><b>${sec(L.wall_ms)}</b><span>总耗时</span></div>
      <div><b>${L.http_calls}</b><span>HTTP 请求</span></div>
      <div><b>${(L.llm_tokens_in + L.llm_tokens_out).toLocaleString()}</b><span>LLM token</span></div>
    </div>
    <ol class="tl">${run.spans.map((s, i) => `
      <li class="${s.parent ? "child" : ""}">
        <button class="row" aria-expanded="false">
          <span class="i">${String(i + 1).padStart(2, "0")}</span>
          <span class="kind kind-${s.kind}">${KIND[s.kind] || s.kind}</span>
          <span class="nm"><b>${esc(s.name)}</b>${s.failure_modes.length ? `<span class="fmt">${s.failure_modes.map(f => `<i title="${esc(FM[f]?.name || "")}">${f}</i>`).join("")}</span>` : ""}<small>${esc(s.summary)}</small></span>
          <span class="st st-${s.status}">${STATUS[s.status] || s.status}</span>
          <span class="lat"><span class="bar" style="width:${Math.max(2, 56 * (s.latency_ms || 0) / maxLat)}px"></span>${sec(s.latency_ms || 0)}</span>
          <span class="usd ${s.cost_usd > 0 ? "paid" : ""}">${s.cost_usd > 0 ? usd(s.cost_usd) : "—"}</span>
        </button>
        <div class="det">
          <p>${esc(s.summary)}</p>
          ${s.failure_modes.map(f => `<p><strong>${f} ${esc(FM[f]?.name || "")}</strong>：${esc(FM[f]?.strategy || "")}</p>`).join("")}
          ${(s.warnings || []).map(w => `<p class="small">提示：${esc(w)}</p>`).join("")}
          ${s.tokens_in ? `<p class="small mono">tokens ${s.tokens_in} in / ${s.tokens_out} out · HTTP ${s.http_calls}</p>` : s.http_calls ? `<p class="small mono">HTTP ${s.http_calls}</p>` : ""}
          ${s.args && Object.keys(s.args).length ? `<pre>${esc(JSON.stringify(s.args, null, 1))}</pre>` : ""}
          ${s.detail ? `<pre>${esc(JSON.stringify(s.detail, null, 1))}</pre>` : ""}
        </div>
      </li>`).join("")}</ol>`;
    p.querySelector(".tl").addEventListener("click", e => {
      const b = e.target.closest(".row"); if (!b) return;
      const li = b.parentElement; li.classList.toggle("open"); b.setAttribute("aria-expanded", li.classList.contains("open"));
    });
  } else {
    p.innerHTML = ev.length ? `<div class="ev">${ev.map(e => `
      <div class="evi" id="ev-${e.id}">
        <span class="id">${e.id}</span>
        <div><p>${esc(e.text)}</p>
          <div class="src"><span class="st st-${e.status}">${STATUS[e.status] || e.status}</span><span class="conf">置信度 ${e.confidence}</span>
          ${e.source?.url ? `<a href="${esc(e.source.url)}" target="_blank" rel="noopener">${esc(e.source.title)}</a>` : `<span>${esc(e.source?.title || "")}</span>`}
          ${e.source?.data_as_of ? `<span>数据日期 ${esc(e.source.data_as_of)}</span>` : ""}
          ${(e.failure_modes || []).map(f => `<span class="mono">${f}</span>`).join("")}</div>
        </div>
      </div>`).join("")}</div>` : `<p class="small">这次运行在收集证据之前就停止了（例如预算熔断或需要澄清），所以没有证据条目。</p>`;
  }
}
document.addEventListener("click", e => {
  const c = e.target.closest(".cite"); if (!c) return;
  const id = c.dataset.ev; tab = "evidence"; render();
  const el = document.getElementById("ev-" + id);
  if (el) { el.scrollIntoView({behavior: "smooth", block: "center"}); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1600); }
});
select("N1_commons_6story");

/* ---------- catalog ---------- */
const layers = ["数据", "工具", "推理", "产品"];
let fLayer = "全部", fSev = "全部";
function renderFilters() {
  $("#filters").innerHTML = ["全部", ...layers].map(l => `<button class="fbtn" data-k="layer" data-v="${l}" aria-pressed="${fLayer === l}">${l === "全部" ? "全部层" : l}</button>`).join("") +
    `<span class="fsep" aria-hidden="true"></span>` +
    [["全部", "全部严重度"], ["S1", "S1 可能导致错误决策"], ["S2", "S2 质量下降"], ["S3", "S3 成本或体验"]].map(([v, l]) => `<button class="fbtn" data-k="sev" data-v="${v}" aria-pressed="${fSev === v}">${l}</button>`).join("");
}
function strip(s) { const t = document.createElement("div"); t.innerHTML = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>'); return t.innerHTML; }
const scenFor = id => runs.filter(r => r.row.fms.includes(id)).map(r => r.tag);
function renderFM() {
  const list = D.fms.filter(f => (fLayer === "全部" || f.layer.includes(fLayer)) && (fSev === "全部" || f.sev === fSev));
  $("#fmlist").innerHTML = list.map(f => {
    const n = D.fm_counts[f.id] || 0, sc = scenFor(f.id);
    return `<div class="fm" id="fm-${f.id}">
      <button aria-expanded="false"><span class="fid">${f.id}</span><span class="fn">${esc(f.name)}<small>${esc(f.layer)}层</small></span>
        <span class="sev sev-${f.sev}">${f.sev}</span><span class="state ${f.state.includes("未") ? "todo" : ""}">${esc(f.state)}</span><span class="cnt ${n ? "on" : ""}" title="v3 评测中触发次数">${n}</span></button>
      <dl class="body">
        <div><dt>观察到 / 注入方式</dt><dd>${strip(esc(f.observed).replace(/&quot;/g, '"'))}</dd></div>
        <div><dt>检测信号</dt><dd>${esc(f.signal)}</dd></div>
        <div><dt>处理策略</dt><dd>${esc(f.strategy)}</dd></div>
        <div><dt>用户看到什么</dt><dd>${esc(f.user)}</dd></div>
        ${sc.length ? `<div><dt>在这些运行中出现</dt><dd>${sc.map(t => `<a href="#replay" class="jump" data-tag="${t}">${esc(byTag[t].title)}</a>`).join("、")}</dd></div>` : ""}
      </dl></div>`;
  }).join("") || `<p class="small">没有符合条件的失败模式。</p>`;
}
$("#filters").addEventListener("click", e => {
  const b = e.target.closest(".fbtn"); if (!b) return;
  if (b.dataset.k === "layer") fLayer = b.dataset.v; else fSev = b.dataset.v;
  renderFilters(); renderFM();
});
$("#fmlist").addEventListener("click", e => {
  const j = e.target.closest(".jump"); if (j) { e.preventDefault(); tab = "trace"; select(j.dataset.tag, true); return; }
  const b = e.target.closest(".fm>button"); if (!b) return;
  const el = b.parentElement; el.classList.toggle("open"); b.setAttribute("aria-expanded", el.classList.contains("open"));
});
renderFilters(); renderFM();

/* ---------- ladder ---------- */
const ladder = [
  ["L0", "FEMA NFHL 有效洪水图层", 0.9, "权威源点查询。真实运行中多次 504，间歇恢复。", "超时 / 5xx / 熔断已打开 → L1"],
  ["L1", "Esri 洪水镜像", 0.6, "先做覆盖探测：Ithaca 范围要素数 0，Binghamton 附近 367。计数为 0 视为“没覆盖”，不是“没风险”。", "无覆盖或失败 → L2"],
  ["L2", "宗地属性 FZONE", 0.2, "字段引用旧版 FIRM 面板 0003B，比 2025 年生效的新图早几十年。只作参考，并标为冲突。", "仍不足以下结论 → L3"],
  ["L3", "未核实 + 人工指引", 0, "回答必须写“未能核实”，护栏禁止“无洪水风险”“不在洪水区”等表述，并给出核对路径。", "终点：交给人"],
];
$("#ladderlist").innerHTML = ladder.map(([lv, h, c, p, n]) => `<li><span class="lv">${lv}</span><h3>${h}</h3>
  <div class="cbar"><i style="width:${c * 100}%"></i></div><span class="cv">置信度 ${c}</span><p>${p}</p><p class="next">${n}</p></li>`).join("");

/* ---------- cost ---------- */
const kinds = {}; let tot = 0;
runs.forEach(r => Object.entries(r.run.ledger.by_kind).forEach(([k, v]) => { kinds[k] = (kinds[k] || 0) + v.usd; tot += v.usd; }));
const kc = {synth: "var(--accent)", plan: "var(--emp)", tool: "var(--ok)", guard: "var(--deg)", compute: "var(--faint)", fallback: "var(--unv)"};
const ks = Object.entries(kinds).sort((a, b) => b[1] - a[1]);
$("#kindbar").innerHTML = `<div class="stack" role="img" aria-label="成本构成">${ks.filter(([, v]) => v > 0).map(([k, v]) => `<i style="width:${100 * v / tot}%;background:${kc[k]}"></i>`).join("")}</div>
  <div class="legend">${ks.map(([k, v]) => `<span><i class="sw" style="background:${kc[k]}"></i>${KIND[k] || k}<em>${usd(v)} · ${(100 * v / tot).toFixed(1)}%</em></span>`).join("")}
  <span><strong>19 次合计</strong><em>${usd(tot)}</em></span></div>`;
const maxU = Math.max(...rows.map(r => r.usd));
const retried = new Set(runs.filter(r => r.run.spans.some(s => s.kind === "fallback" && s.name.startsWith("regenerate"))).map(r => r.tag));
$("#runbars").innerHTML = [...runs].sort((a, b) => b.row.usd - a.row.usd).map(r => `
  <button class="rb ${retried.has(r.tag) ? "retry" : ""}" data-tag="${r.tag}" title="${esc(r.title)}"><span class="t">${esc(r.title)}</span><span class="b"><i style="width:${Math.max(0.5, 100 * r.row.usd / maxU)}%"></i></span><span class="v">${usd(r.row.usd)}</span></button>`).join("") +
  `<p class="small" style="margin-top:var(--space-3)">橙色 = 护栏拦截后重写了一次（F6 是故意注入的伪造引用）。点击任一行查看它的调用链。</p>`;
$("#runbars").addEventListener("click", e => { const b = e.target.closest(".rb"); if (b) { tab = "trace"; select(b.dataset.tag, true); } });

/* ---------- iterations ---------- */
const notes = {
  report_v1: ["评测脚本自己误报（检查逻辑与护栏不一致）", "跨分区宗地只解析了一个分区", "预算熔断事后才检查：上限 $0.002，实际花了 $" + D.hist.v1_budget.usd.toFixed(3), "护栏误报 2 条，该次运行 " + usd(D.hist.v1_guard_fp.usd) + "、" + sec(D.hist.v1_guard_fp.ms)],
  report_v2: ["只剩 F8 失败。追查发现公交工具在服务进程里一直抛 KeyError（PDF 缓存和 GTFS 缓存共用一个字典），被兜底吞掉", "LLM 有时不规划公交查询"],
  report: ["修复缓存冲突；工具异常必须生成证据，评测加“无静默工具异常”", "必查项（辖区、分区、洪水、公交）改由策略强制", "F1 出现新的护栏误报，修复后成本 " + usd(D.hist.v2_guard_fp.usd) + " → " + usd(byTag.F1_flood_timeout.row.usd)],
};
const vname = {report_v1: "v1", report_v2: "v2", report: "v3"};
$("#iters").innerHTML = D.iters.map(it => `<div class="card it"><h3>${vname[it.v]}<span>${it.checks}/${it.checks_total} 项</span></h3>
  <div class="big">${it.scen_pass}/${it.n}</div><span class="small">场景全部通过</span>
  <div class="meter"><i style="width:${100 * it.scen_pass / it.n}%"></i></div>
  <ul>${notes[it.v].map(n => `<li>${esc(n)}</li>`).join("")}</ul></div>`).join("");
const fa = D.hist.v2_guard_fp.first_answer || "";
const line = fa.split("\n").find(l => l.includes("不能解读为")) || "";
$("#fpquote").innerHTML = esc(line.replace(/^\s*-\s*/, "").replace(/\*\*/g, "")).replace("这不能解读为“不在洪水区”", "<mark>这不能解读为“不在洪水区”</mark>");
$("#fpcmp").innerHTML = `<div><span>修复前（被误判，重写一次）</span><b>${usd(D.hist.v2_guard_fp.usd)} · ${sec(D.hist.v2_guard_fp.ms)}</b></div>
  <div><span>修复后（护栏识别免责语境）</span><b>${usd(byTag.F1_flood_timeout.row.usd)} · ${sec(byTag.F1_flood_timeout.row.wall_ms)}</b></div>`;

/* ---------- PM metrics ---------- */
const needHuman = runs.filter(r => (r.run.result?.evidence || []).some(e => e.status !== "ok" || e.confidence < 0.5) || !(r.run.result?.evidence || []).length).length;
const cites = rows.filter(r => r.checks.some(c => c.name === "引用全部有效"));
const consist = ["N1_commons_6story", "R1_consistency_a", "R1_consistency_b"].map(t => byTag[t].row.verdict);
$("#pm").innerHTML = [
  ["危险错误率", `${unsafe}/${rows.length}`, "证据未核实却给出“安全”类结论"],
  ["引用有效率", `${cites.filter(r => r.checks.find(c => c.name === "引用全部有效").pass).length}/${cites.length}`, "回答中的 [E#] 都能对应到证据"],
  ["需人工核实比例", `${needHuman}/${rows.length}`, "主要来自规则时效与 PDF 噪声，这是现实，不应隐藏"],
  ["护栏自然触发精确率", "0/3", "非注入运行中拦截 3 条，全部误报，已修复"],
  ["每答中位成本", usd(med), "合成占 94%，工具调用几乎免费"],
  ["一致性", `${consist.every(v => v === consist[0]) ? 3 : 0}/3`, "同一问题复跑三次，结论一致"],
].map(([t, v, s]) => `<div><dt>${t}</dt><dd>${v}<small>${s}</small></dd></div>`).join("");
})();
