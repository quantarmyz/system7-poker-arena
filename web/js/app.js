/* System 7 — app Alpine: estado + carga de datos + render de las 3 zonas. Reusa viz.js. */
let CTXGAME = "cash";          // contexto de datos: cash | tournament (bifurcación de BD)
const _g = u => u + (u.includes("?") ? "&" : "?") + "game=" + CTXGAME;
const jget = async u => { try { return await (await fetch(_g(u))).json(); } catch (e) { return { error: String(e) }; } };
const jpost = async (u, b) => { b = Object.assign({ game: CTXGAME }, b || {}); try { return await (await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })).json(); } catch (e) { return { error: String(e) }; } };
const POS6 = ["UTG", "MP", "CO", "BTN", "SB", "BB"];
const BUCKETS = ["deep", "mid", "short", "push"];
const COMPNAMES = { eval: "Eval S1", seed_poker_eval_s1: "Eval S1", cmqf827h30u7dfca3x2aqvzjv: "Playground S3", cmqggiv9k37am11ydmppz466e: "Tournament S2" };
const compName = c => COMPNAMES[c] || c || "";   // id de competición → nombre legible

function app() {
  return {
    zone: "lab", game: "cash", live: false, state: {},
    agents: [], strategies: [], evalMaxc: 3,
    evalForm: { agent: "", total: 6, maxc: 2, group: "" }, evalMsg: "", runs: [], groups: [],
    builder: null, report: null, task: "", taskData: null, taskLog: "",
    coachForm: { agent: "", window: "all" }, coachData: null, coachText: "", sgMode: "leaks", sgMsg: "",
    prod: {}, prodComps: [], rank: [], prodSel: "", prodSelName: "", prodSession: null, prodLog: "", account: null, deployForm: { agent: "", competition: "eval" }, hands: [], handFilter: "", llmOnly: false, opponents: [],
    modalHand: null, step: 0, embed: true, eqOpt: { ev: true, off: {} },
    settings: {}, keyInput: {}, baseInput: {}, liveModel: "", defModel: "", settingsMsg: "", settingsMsg2: "",
    settingsProviders: [{ id: "minimax", label: "MiniMax" }, { id: "xiaomi", label: "Xiaomi MiMo", needBase: true }, { id: "openrouter", label: "OpenRouter" }, { id: "deepseek", label: "DeepSeek" }],

    init() {
      CTXGAME = this.game; this.tick(); this.loadAgents(); this.loadRuns();
      setInterval(() => this.tick(), 4000);
      setInterval(() => { if (this.zone === "lab") { this.loadRuns(); this.loadGroups(); if (this.task) this.loadTask(); } }, 15000);
      setInterval(() => { if (this.zone === "production" && ((this.prod || {}).active || []).length) { this.loadHands(); this.loadProdLog(); } }, 2000);   // manos + log casi en vivo
    },
    setGameCtx(g) { this.game = g; CTXGAME = g; this.builder = null; this.report = null; this.setZone(this.zone); },

    async tick() {
      const d = await jget("/api/state");
      this.live = !d.error; if (!d.error) this.state = d;
      if (this.zone === "production") { this.loadProd(); this.loadHands(); this.loadRank(); this.loadSession(); }
    },
    setZone(z) {
      this.zone = z;
      if (z === "lab") { this.loadAgents(); this.loadRuns(); this.loadGroups(); }
      if (z === "coach") { this.loadAgents(); this.loadCoach(); }
      if (z === "production") { this.loadProd(); this.loadCompetitions(); this.loadRank(); this.loadHands(); this.loadOpponents(); this.loadSession(); this.loadAccount(); }
      if (z === "settings") { this.loadSettings(); }
    },

    renderKpis() {
      const d = this.state || {};
      const k = [["manos", d.hands ?? "—"], ["decisiones", d.decisions ?? "—"], ["M3 %", (d.m3pct ?? 0) + "%"]];
      return k.map(x => `<div class="kpi"><div class="l">${x[0]}</div><div class="v">${x[1]}</div></div>`).join("");
    },

    /* ───── LAB: agentes ───── */
    async loadAgents() { const d = await jget("/api/agents"); this.agents = d.agents || []; this.strategies = d.strategies || [];
      if (this.agents.length) { if (!this.deployForm.agent) this.deployForm.agent = this.agents[0].name; if (!this.evalForm.agent) this.evalForm.agent = this.agents[0].name; } },
    renderAgents() {
      if (!this.agents.length) return '<div class="empty">Aún no hay agentes. Crea uno con «+ nuevo».</div>';
      const rows = this.agents.map(a => {
        const bb = a.mean == null ? '<span class="dim">sin eval</span>' : `<b class="${a.mean >= 0 ? "pos" : "neg"}">${a.mean >= 0 ? "+" : ""}${a.mean}</b>${a.ci != null ? ` <span class="dim">±${a.ci}</span>` : ""}`;
        return `<tr><td><b>${esc(a.title || a.name)}</b>${a.title ? ` <span class="dim" style="font-size:11px">${esc(a.name)}</span>` : ""}${a.note ? `<div class="dim" style="font-size:11px;white-space:normal;max-width:300px">${esc(a.note)}</div>` : ""}</td><td>${esc(a.strategy || "std")}</td><td>${esc(a.engine)}</td>
          <td>${esc(a.model || "—")}</td><td class="num">${bb}</td><td class="num dim">${a.n_evals || 0}</td>
          <td class="num"><button class="btn sm" data-edit="${esc(a.name)}">editar</button>
            <button class="btn sm" data-eval="${esc(a.name)}">eval</button>
            <button class="btn sm" data-report="${esc(a.name)}">report</button>
            <button class="btn sm destructive" data-del="${esc(a.name)}">✕</button></td></tr>`;
      }).join("");
      return `<table class="list"><thead><tr><th>agente</th><th>estrategia</th><th>motor</th><th>modelo</th><th class="num">bb/100</th><th class="num">evals</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
    },
    onAgentsClick(e) {
      const b = e.target.closest("[data-edit],[data-eval],[data-report],[data-del]"); if (!b) return;
      if (b.dataset.edit != null) this.editAgent(b.dataset.edit);
      else if (b.dataset.eval != null) { this.evalForm.agent = b.dataset.eval; this.evalAgent(); }
      else if (b.dataset.report != null) this.loadReport(b.dataset.report);
      else if (b.dataset.del != null && confirm("¿Borrar el perfil " + b.dataset.del + "?")) this.deleteAgent(b.dataset.del);
    },
    async deleteAgent(name) { await jpost("/api/agent/delete", { name }); this.loadAgents(); },

    /* ───── LAB: builder ───── */
    async newAgent(game) { const t = await jget("/api/strats/template?base=std&gametype=" + game); this.mountBuilder(t, { game: game }); },
    async editAgent(name) {
      const a = this.agents.find(x => x.name === name) || {};
      const t = await jget("/api/strats/template?base=" + (a.base || "std") + "&name=" + encodeURIComponent(a.strategy || name));
      this.mountBuilder(t, a);
    },
    mountBuilder(t, profile) {
      profile = profile || {};
      const ranges = {}; POS6.forEach(p => ranges[p] = new Set((t.ranges && t.ranges[p]) || []));
      const tourn = {}; BUCKETS.forEach(b => { tourn[b] = {}; POS6.forEach(p => tourn[b][p] = new Set(((t.tournament_ranges || {})[b] || {})[p] || [])); });
      this.builder = {
        name: profile.name || t.name || "", base: t.base || "std",
        game: profile.game || t.game || "cash", mode: t.mode || "std", bucket: "deep",
        ranges, tourn, sizing: JSON.parse(JSON.stringify(t.sizing || {})),
        knobs: Object.assign({}, t.knobs || {}), limits: t.knob_limits || {},
        tbv: (t.threebet_value || []).join(" "), tbb: (t.threebet_bluff || []).join(" "),
        engine: profile.engine || "hybrid", model: profile.model || "", provider: profile.provider || "minimax",
        hud: profile.hud !== false, tracker: profile.tracker !== false, prose: t.prose || "",
        title: profile.title || "", note: profile.note || "",
      };
      this.zone = "lab"; this.report = null;
    },
    renderBuilder() {
      const b = this.builder; if (!b) return "";
      const klab = { value_eq: "equity valor", station_mult: "mult. estación", cbet_bluff_frac: "frac. cbet farol", commit_spr: "SPR commit", perejil_flop: "perejil flop", perejil_turn: "perejil turn", perejil_relief: "perejil alivio", open_size_bb: "tamaño apertura bb", threebet_mult: "mult. 3bet" };
      const knobUI = Object.keys(b.limits).map(k => { const lim = b.limits[k] || [0, 99];
        return `<label class="field">${klab[k] || k} <span class="dim">(${lim[0]}–${lim[1]})</span><input type="number" step="0.05" id="k-${k}" value="${b.knobs[k] == null ? "" : b.knobs[k]}" min="${lim[0]}" max="${lim[1]}"></label>`; }).join("");
      let head = `${b.prose ? `<div class="dim" style="margin-bottom:8px;white-space:pre-wrap">${esc(b.prose)}</div>` : ""}<div class="row">
          <label class="field">nombre (id) <input id="b-name" value="${esc(b.name)}" maxlength="24" placeholder="mi-agente"></label>
          <label class="field">nombre visible <input id="b-title" value="${esc(b.title)}" maxlength="60" placeholder="(opcional, libre)" style="width:160px"></label>
          <label class="field">juego <select id="b-game"><option value="cash" ${b.game === "cash" ? "selected" : ""}>cash</option><option value="tournament" ${b.game === "tournament" ? "selected" : ""}>torneo</option></select></label>`;
      if (b.game === "cash") head += `<label class="field">modalidad <select id="b-mode"><option value="agr" ${b.mode === "agr" ? "selected" : ""}>AGR</option><option value="std" ${b.mode === "std" ? "selected" : ""}>STD</option><option value="nit" ${b.mode === "nit" ? "selected" : ""}>NIT</option></select></label>`;
      head += `<label class="field">motor <select id="b-engine"><option ${b.engine === "hybrid" ? "selected" : ""}>hybrid</option><option ${b.engine === "heur" ? "selected" : ""}>heur</option></select></label>
          <label class="field">modelo LLM <input id="b-model" value="${esc(b.model)}" placeholder="MiniMax-M3" style="width:140px"></label>
          <label class="field">proveedor <select id="b-provider"><option ${b.provider === "minimax" ? "selected" : ""}>minimax</option><option ${b.provider === "openrouter" ? "selected" : ""}>openrouter</option><option ${b.provider === "xiaomi" ? "selected" : ""}>xiaomi</option></select></label>
          <label class="toggle"><input type="checkbox" id="b-hud" ${b.hud ? "checked" : ""}> HUD</label>
          <label class="toggle"><input type="checkbox" id="b-tracker" ${b.tracker ? "checked" : ""}> tracker</label></div>
        <div class="row" style="margin-top:8px"><label class="field" style="flex:1 1 100%">descripción <textarea id="b-note" rows="2" maxlength="400" placeholder="¿para qué es este agente? estilo, notas, recordatorios…" style="width:100%;resize:vertical">${esc(b.note)}</textarea></label></div>`;
      let pre;
      if (b.game === "cash") {
        pre = `<h3 style="border:0;padding:10px 0 4px">Preflop · cash ${b.mode.toUpperCase()} <span class="dim">(modalidad: cambia y resiembra)</span></h3><div class="row" style="align-items:flex-start">${POS6.map(p => rangeGrid(p, b.ranges[p], "cash")).join("")}</div>`;
      } else {
        const tabs = BUCKETS.map(bk => `<button class="btn sm ${b.bucket === bk ? "suggested" : ""}" data-bucket="${bk}">${bk}${bk === "push" ? " · shove" : ""}</button>`).join(" ");
        pre = `<h3 style="border:0;padding:10px 0 4px">Preflop · torneo por BBs <span class="dim">(&gt;40 / 20-40 / 10-20 / &lt;10bb push-fold)</span></h3><div class="row" style="margin-bottom:6px">${tabs}</div><div class="row" style="align-items:flex-start">${POS6.map(p => rangeGrid(p, b.tourn[b.bucket][p], b.bucket)).join("")}</div>`;
      }
      const TEX = ["dry", "semi", "coord", "extreme"], ST = ["flop", "turn", "river"];
      const siz = `<h3 style="border:0;padding:12px 0 4px">Postflop · sizing (fracción del bote)</h3><table class="list"><thead><tr><th>textura</th>${ST.map(s => `<th class="num">${s}</th>`).join("")}</tr></thead><tbody>${TEX.map(t => `<tr><td>${t}</td>${ST.map(s => `<td class="num"><input type="number" step="0.05" min="0" max="3" style="width:64px" id="sz-${t}-${s}" value="${(b.sizing[t] && b.sizing[t][s] != null) ? b.sizing[t][s] : ""}"></td>`).join("")}</tr>`).join("")}</tbody></table>`;
      const knb = `<h3 style="border:0;padding:12px 0 4px">Postflop · knobs</h3><div class="row">${knobUI}</div>`;
      const tb = `<div class="row" style="margin-top:10px"><label class="field">3bet valor <input id="tb-value" value="${esc(b.tbv)}" style="width:260px"></label><label class="field">3bet farol <input id="tb-bluff" value="${esc(b.tbb)}" style="width:260px"></label></div>`;
      const save = `<div class="row" style="margin-top:12px"><button class="btn suggested" data-action="save">💾 guardar agente</button><button class="btn flat" data-action="cancel">cancelar</button><span id="save-msg" class="dim"></span></div>`;
      return head + pre + siz + knb + tb + save;
    },
    onBuilderClick(e) {
      const cell = e.target.closest("td[data-cmb]");
      if (cell && this.builder) {
        const ctx = cell.dataset.ctx, p = cell.dataset.pos, c = cell.dataset.cmb;
        const set = ctx === "cash" ? this.builder.ranges[p] : this.builder.tourn[ctx][p];
        if (set.has(c)) { set.delete(c); cell.classList.remove("on"); } else { set.add(c); cell.classList.add("on"); }
        const cnt = document.querySelector(`[data-cnt="${ctx}-${p}"]`); if (cnt) cnt.textContent = set.size; return;
      }
      const bk = e.target.closest("[data-bucket]"); if (bk) { this.builder.bucket = bk.dataset.bucket; return; }
      const act = e.target.closest("[data-action]"); if (!act) return;
      if (act.dataset.action === "save") this.saveAgent();
      else if (act.dataset.action === "cancel") this.builder = null;
    },
    onBuilderChange(e) {
      if (e.target.id === "b-game" || e.target.id === "b-mode") {
        this.builder.game = (document.getElementById("b-game") || {}).value || this.builder.game;
        const mEl = document.getElementById("b-mode"); if (mEl) this.builder.mode = mEl.value;
        this.reseedBuilder();
      }
    },
    async reseedBuilder() {
      const b = this.builder, g = id => document.getElementById(id);
      const prof = { name: (g("b-name") || {}).value || b.name, game: b.game, engine: (g("b-engine") || {}).value || b.engine, model: (g("b-model") || {}).value || b.model, provider: (g("b-provider") || {}).value || b.provider, hud: (g("b-hud") || {}).checked, tracker: (g("b-tracker") || {}).checked };
      const t = await jget("/api/strats/template?base=" + b.base + "&gametype=" + b.game + "&mode=" + b.mode);
      this.mountBuilder(t, prof);
    },
    async saveAgent() {
      const b = this.builder, g = id => document.getElementById(id); const m = g("save-msg");
      const name = (g("b-name").value || "").trim().toLowerCase();
      if (!/^[a-z0-9_-]{1,24}$/.test(name)) { m.innerHTML = '<span class="neg">nombre inválido (a-z 0-9 _ - máx 24)</span>'; return; }
      const game = g("b-game").value, mode = g("b-mode") ? g("b-mode").value : "std";
      const opening = {}; POS6.forEach(p => opening[p] = Array.from(b.ranges[p]));
      const tourn = {}; BUCKETS.forEach(bk => { tourn[bk] = {}; POS6.forEach(p => tourn[bk][p] = Array.from(b.tourn[bk][p])); });
      const knobs = {}; Object.keys(b.limits).forEach(k => { const el = g("k-" + k); if (el && el.value !== "") knobs[k] = parseFloat(el.value); });
      const TEX = ["dry", "semi", "coord", "extreme"], ST = ["flop", "turn", "river"], sizing = {};
      TEX.forEach(t => { const row = {}; ST.forEach(s => { const el = g("sz-" + t + "-" + s); if (el && el.value !== "") row[s] = parseFloat(el.value); }); if (Object.keys(row).length) sizing[t] = row; });
      if (Object.keys(sizing).length) knobs.sizing = sizing;
      const sp = s => (s || "").split(/[\s,]+/).filter(Boolean);
      m.textContent = "guardando…";
      const s = await jpost("/api/strats/save", { name, base: b.base, game, mode, opening_ranges: opening, tournament_ranges: tourn, knobs,
        threebet_value: sp(g("tb-value").value), threebet_bluff: sp(g("tb-bluff").value) });
      if (s.error) { m.innerHTML = `<span class="neg">${esc(s.error)}</span>`; return; }
      const a = await jpost("/api/agent/save", { name, strategy: name, game, engine: g("b-engine").value,
        model: g("b-model").value.trim(), provider: g("b-provider").value, hud: g("b-hud").checked, tracker: g("b-tracker").checked,
        title: (g("b-title") ? g("b-title").value.trim() : ""), note: (g("b-note") ? g("b-note").value.trim() : "") });
      if (a.error) { m.innerHTML = `<span class="neg">${esc(a.error)}</span>`; return; }
      m.innerHTML = '<span class="pos">✓ guardado</span>'; this.builder = null; this.loadAgents();
    },

    /* ───── LAB: evaluar + runs + report ───── */
    async evalAgent() {
      if (!this.evalForm.agent) { this.evalMsg = "elige un agente"; return; }
      this.evalMsg = "lanzando…";
      const d = await jpost("/api/lab/eval", this.evalForm);
      this.evalMsg = d.error ? d.error : `▶ grupo «${d.group}» · ${d.total} agentes (${d.maxc} a la vez)`;
      setTimeout(() => { this.loadRuns(); this.loadGroups(); }, 800);
    },
    async loadRuns() { const d = await jget("/api/runs"); this.runs = d.runs || []; },
    async loadGroups() { const d = await jget("/api/lab/groups"); this.groups = d.groups || []; },
    async loadTask() {
      if (!this.task) { this.taskData = null; this.taskLog = ""; return; }
      const d = await jget("/api/lab/task?agent=" + encodeURIComponent(this.task)); this.taskData = d;
      let unit = "arena-run-lab-" + this.task;
      const ch = (d.jobs || []).find(j => j.state === "active" && j.label.indexOf("clasif-" + this.task) === 0);
      if (ch) unit = ch.unit;
      const lg = await jget("/api/run/log?unit=" + encodeURIComponent(unit) + "&n=80"); this.taskLog = lg.log || lg.error || "";
      this.loadHands();
    },
    async stopTask(agent) { await jpost("/api/lab/stop", { agent }); setTimeout(() => { this.loadTask(); this.loadRuns(); }, 700); },
    onTaskClick(e) {
      const st = e.target.closest("[data-stoptask]"); if (st) { this.stopTask(st.dataset.stoptask); return; }
      const r = e.target.closest("[data-key]"); if (r) this.openHand(decodeURIComponent(r.dataset.key));
    },
    renderTaskMonitor() {
      if (!this.task) return '<div class="empty">elige una tarea (agente) arriba para ver su evaluación en vivo</div>';
      const d = this.taskData; if (!d) return '<div class="empty">cargando…</div>'; if (d.error) return `<div class="neg">${esc(d.error)}</div>`;
      const kpi = (l, v) => `<div class="kpi" style="text-align:left"><div class="l">${l}</div><div class="v" style="font-size:18px">${v}</div></div>`;
      const head = `<div class="row" style="gap:18px;align-items:center">
        ${kpi("estado", d.active ? '<span class="pos">▶ ' + d.active + ' activo</span>' : '<span class="dim">parado</span>')}
        ${kpi("manos", (d.hands || 0).toLocaleString())}${kpi("decisiones", (d.decisions || 0).toLocaleString())}
        ${kpi("M3 %", (d.m3pct || 0) + "%")}${kpi("bb/100 ± IC", d.agg.mean == null ? "—" : (d.agg.mean >= 0 ? "+" : "") + d.agg.mean + (d.agg.ci != null ? " ±" + d.agg.ci : ""))}
        <span class="spacer"></span>${d.active ? `<button class="btn destructive" data-stoptask="${esc(d.agent)}">⏹ parar evaluación</button>` : ""}</div>`;
      const dist = `<div class="dim" style="margin-top:8px">distribución de bb/100 por agente (campana de Gauss)</div>${gaussChart(d.samples, "bb/100")}`;
      const pos = `<div class="dim" style="margin-top:8px">VPIP / PFR por posición</div><div>${(d.bypos || []).map(p => `<span style="margin-right:12px">${p.pos} <b>${p.vpip}%</b>/<b>${p.pfr}%</b> <span class="dim">n${p.n}</span></span>`).join("") || '<span class="dim">—</span>'}</div>`;
      const heat = `<div class="dim" style="margin-top:8px">rango preflop · VPIP heatmap</div><div style="display:grid;grid-template-columns:repeat(13,1fr);gap:1px;max-width:420px">${heatGrid(d.classes || {})}</div>`;
      const log = `<div class="dim" style="margin-top:10px">log en vivo</div><pre class="log">${esc(this.taskLog || "(sin salida todavía)")}</pre>`;
      const hs = (this.hands || []).filter(h => (h.label || "").indexOf("clasif-" + this.task) === 0).slice(0, 120);
      const handsT = `<div class="dim" style="margin-top:10px">historial de manos de la tarea (clic → reproductor)</div>` +
        (hs.length ? `<div class="scroll" style="max-height:240px"><table class="list"><thead><tr><th>hora</th><th>pos</th><th>mano</th><th>board</th><th>calle</th><th class="num">result</th></tr></thead><tbody>` +
          hs.map(h => `<tr class="clk" data-key="${encodeURIComponent(h.key)}"><td class="dim">${tt(h.ts)}</td><td>${esc(h.pos || "")}</td><td>${chs(h.hole) || "—"}</td><td>${chs(h.board) || "—"}</td><td>${esc(h.reached || "")}</td><td class="num">${h.delta == null ? "·" : `<b class="${h.delta >= 0 ? "pos" : "neg"}">${h.delta >= 0 ? "+" : ""}${h.delta}</b>`}</td></tr>`).join("") +
          `</tbody></table></div>` : '<div class="dim">sin manos todavía</div>');
      return head + `<div class="grid" style="margin-top:6px"><div class="col-6">${dist}${pos}</div><div class="col-6">${heat}</div></div>` + log + handsT;
    },
    renderRuns() {
      if (!this.runs.length) return '<div class="dim">sin entrenamientos activos</div>';
      return `<table class="list"><tbody>${this.runs.slice(0, 12).map(r => `<tr><td><span class="dot ${r.state === "active" ? "up" : "down"}"></span> ${esc(r.label)}</td>
        <td class="num">${r.matches || 0}</td><td class="num">${r.bb100 == null ? "—" : (r.bb100 >= 0 ? "+" : "") + r.bb100}</td>
        <td>${r.state === "active" ? `<button class="btn sm" data-stoprun="${esc(r.unit)}">parar</button>` : ""}</td></tr>`).join("")}</tbody></table>`;
    },
    async loadReport(name) { this.report = await jget("/api/lab/report?agent=" + encodeURIComponent(name)); },
    renderReport() {
      const r = this.report; if (!r) return ""; if (r.error) return `<div class="neg">${esc(r.error)}</div>`;
      const agg = r.agg || {};
      const head = `<div class="row" style="gap:24px;margin-bottom:10px">
        <div><div class="dim">bb/100 agregado (IC95)</div><div style="font-size:24px"><b class="${(agg.mean || 0) >= 0 ? "pos" : "neg"}">${agg.mean == null ? "—" : (agg.mean >= 0 ? "+" : "") + agg.mean}</b> <span class="dim">${agg.ci != null ? "± " + agg.ci : ""}</span></div></div>
        <div><div class="dim">evals</div><div style="font-size:24px">${agg.n_evals || 0}</div></div>
        <div><div class="dim">manos</div><div style="font-size:24px">${(r.hands || 0).toLocaleString()}</div></div></div>`;
      const runs = (r.runs || []).slice(0, 30).map(x => `<tr><td>${esc(x.label || "")}</td><td class="num">${x.hands || 0}</td>
        <td class="num"><b class="${(x.bb100 || 0) >= 0 ? "pos" : "neg"}">${x.bb100 == null ? "—" : (x.bb100 >= 0 ? "+" : "") + x.bb100}</b></td>
        <td class="dim">${x.note || ""}</td></tr>`).join("");
      const pos = (r.bypos || []).map(p => `<span style="margin-right:14px">${p.pos} <b>${p.vpip}%</b>/<b>${p.agg}%</b> <span class="dim">n${p.n}</span></span>`).join("");
      const worst = (r.worst || []).map(w => `<tr class="clk" data-key="${encodeURIComponent(w.key)}"><td>${chs(w.hole) || "?"}</td><td>${chs(w.board)}</td><td class="num neg">${w.delta}</td></tr>`).join("");
      return `${head}<div class="row"><div style="flex:1;min-width:280px"><div class="dim">runs</div><table class="list"><thead><tr><th>run</th><th class="num">manos</th><th class="num">bb/100</th><th>nota</th></tr></thead><tbody>${runs}</tbody></table></div>
        <div style="flex:1;min-width:260px"><div class="dim">VPIP/agresión por posición</div><div style="margin:6px 0">${pos || '<span class="dim">—</span>'}</div>
        <div class="dim">peores manos (clic → reproductor)</div><table class="list"><tbody>${worst}</tbody></table></div></div>`;
    },

    /* ───── COACH ───── */
    async loadCoach() { this.coachData = await jget("/api/coach?window=" + this.coachForm.window); },
    renderCoach() {
      const d = this.coachData; if (!d) return '<div class="empty">cargando…</div>';
      if (d.locked) { const pc = Math.min(100, Math.round(100 * d.hands / d.need));
        return `<div class="empty">Diagnóstico bloqueado 🔒 — ${d.hands.toLocaleString()}/${d.need.toLocaleString()} manos<div class="meter" style="max-width:320px;margin:10px auto"><i style="width:${pc}%"></i><span>${pc}%</span></div></div>`; }
      const vcol = v => v === "✓" ? "pos" : (v === "✗" ? "neg" : "");
      const panel = d.vs_panel ? `<div style="margin-bottom:8px">vs panel near-GTO: <b class="${(d.vs_panel.bb100 || 0) >= 0 ? "pos" : "neg"}">${(d.vs_panel.bb100 || 0) >= 0 ? "+" : ""}${d.vs_panel.bb100} bb/100</b> <span class="dim">(${d.vs_panel.runs} runs)</span></div>` : "";
      const vsopt = (d.vs_opt || []).map(o => `<tr><td><b>${esc(o.k)}</b></td><td>${esc(o.you)}</td><td class="dim">${esc(o.target)}</td><td class="${vcol(o.verdict)}" style="text-align:center;font-weight:700">${esc(o.verdict)}</td><td class="dim">${esc(o.note)}</td></tr>`).join("");
      const adv = (d.advice || []).map(a => `<div style="padding:2px 0">▷ ${esc(a)}</div>`).join("");
      return `${panel}<table class="list"><thead><tr><th>métrica</th><th>tú</th><th>óptimo</th><th>✓</th><th>nota</th></tr></thead><tbody>${vsopt}</tbody></table>
        <div style="margin-top:10px">${adv}</div>`;
    },
    coachLLM() {
      const w = this.coachForm.window; let n = 0; this.coachText = "⏳ pidiendo análisis a M3…";
      const poll = async () => { const d = await jget("/api/coach/llm?window=" + w);
        if (d.locked) { this.coachText = `bloqueado: ${d.hands}/${d.need} manos`; return; }
        if (d.error) { this.coachText = `<span class="neg">${esc(d.error)}</span>`; return; }
        if (d.running) { n++; this.coachText = `⏳ M3 analizando… (${n * 4}s)`; if (n < 75) setTimeout(poll, 4000); return; }
        this.coachText = esc(d.text || "") + (d.version ? `<div style="margin-top:8px"><span class="pill accent">propuesta ${esc(d.version)}</span></div>` : ""); };
      poll();
    },
    genStrategy() {
      const w = this.coachForm.window, mode = this.sgMode; let n = 0; this.sgMsg = "⏳ pidiendo estrategia a M3…";
      const poll = async () => { const d = await jget("/api/coach/strategy?window=" + w + "&mode=" + mode);
        if (d.locked) { this.sgMsg = `bloqueado: ${d.hands}/${d.need} manos (usa «ideal desde cero»)`; return; }
        if (d.error) { this.sgMsg = `error: ${d.error}`; return; }
        if (d.running) { n++; this.sgMsg = `⏳ diseñando… (${n * 4}s)`; if (n < 75) setTimeout(poll, 4000); return; }
        this.sgMsg = "✓ propuesta lista — revísala en el LAB"; this.mountBuilder(d, {}); };
      poll();
    },

    /* ───── PRODUCCIÓN ───── */
    async loadProd() { this.prod = await jget("/api/production/status"); this.loadProdLog(); },
    async loadProdLog() {
      const a = ((this.prod || {}).active || [])[0];
      if (!a || !a.unit) { this.prodLog = ""; return; }
      const d = await jget("/api/run/log?unit=" + encodeURIComponent(a.unit) + "&n=80");
      this.prodLog = d.log || d.error || "";
    },
    async loadCompetitions() { const d = await jget("/api/production/competitions"); this.prodComps = d.competitions || []; },
    async loadRank() { const d = await jget("/api/rank"); this.rank = d.agents || []; },
    async loadAccount() { this.account = null; this.account = await jget("/api/production/account"); },
    renderAccount() {
      const a = this.account;
      if (!a) return '<div class="empty">cargando… (busca tu posición en cada leaderboard)</div>';
      if (a.error) return `<div class="neg">${esc(a.error)}</div>`;
      const ag = a.agent || {};
      const head = `<div class="row" style="gap:12px;align-items:center"><b style="font-size:15px">${esc(ag.handle || ag.name || ag.agentId || "?")}</b>
        ${ag.claimed ? `<span class="pill green">reclamado${ag.owner ? " · @" + esc(ag.owner) : ""}</span>` : '<span class="pill amber">sin reclamar</span>'}
        <span class="dim" style="font-size:11px">${esc(ag.agentId || "")}</span></div>`;
      const evs = (a.events || []).map(e => `<tr><td><b>${esc(e.name)}</b></td>
        <td class="num">${e.rank != null ? `<b class="pos">#${e.rank}</b> <span class="dim">/ ${e.total || "?"}</span>` : '<span class="dim">no registrado / sin posición</span>'}</td>
        <td class="num">${e.score != null ? e.score : "—"}</td></tr>`).join("");
      return head + `<table class="list" style="margin-top:8px"><thead><tr><th>evento</th><th class="num">posición</th><th class="num">score</th></tr></thead><tbody>${evs}</tbody></table>`;
    },
    async loadSession() { this.prodSession = await jget("/api/production/session?label=" + encodeURIComponent(this.prodSel || "")); },
    renderProdControl() {
      const p = this.prod || {};
      const act = (p.active || []).map(a => `<div class="row" style="margin:4px 0"><span class="dot warn"></span> <b>${esc(a.agent || a.label)}</b> <span class="dim">· ${esc(compName(a.competition))}</span>${a.continuous ? ` <span class="pill amber">continuo · la cola espera</span>` : ""}
        <button class="btn sm destructive" data-stopprod="${esc(a.unit)}">parar</button>
        <button class="btn sm" data-claim="${esc(a.label)}">🏆 reclamar</button></div>`).join("") || `<div class="dim">nada jugando ahora</div>`;
      const q = (p.queue || []).map((it, i) => `<div class="row" style="margin:3px 0"><span class="dim">${i + 1}.</span> ${esc(it.agent)} <span class="dim">· ${esc(compName(it.competition || "eval"))}</span>
        <button class="btn sm flat" data-dequeue="${i}">✕ quitar</button></div>`).join("") || `<div class="dim">cola vacía</div>`;
      return `<h3 style="border:0;padding:10px 0 2px">Jugando</h3>${act}<h3 style="border:0;padding:8px 0 2px">Cola</h3>${q}`;
    },
    renderRank() {
      const r = this.rank || [];
      if (!r.length) return '<div class="empty">sin agentes puntuados aún — juega el Eval desde aquí</div>';
      const badge = a => a.kind === "eval" ? '<span class="pill green">Eval</span>' : `<span class="pill amber">${esc(a.type || "PvP")}</span>`;
      const rows = r.map((a, i) => `<tr><td class="num">${i + 1}</td><td><b>${esc(a.name || a.label)}</b></td><td>${badge(a)}</td><td>${esc(a.strategy || "")}</td>
        <td class="num"><b class="${(a.bb100 || 0) >= 0 ? "pos" : "neg"}">${a.bb100 == null ? "—" : (a.bb100 >= 0 ? "+" : "") + a.bb100}</b></td>
        <td class="num">${a.hands || 0}</td><td><button class="btn sm" data-sel="${esc(a.label)}" data-selname="${esc(a.name || a.label)}">👁</button>${a.claimable ? ` <button class="btn sm" data-claim="${esc(a.label)}">🏆</button>` : ""} <button class="btn sm destructive" data-rankdel="${esc(a.label)}" title="quitar del ranking">🗑</button></td></tr>`).join("");
      return `<table class="list"><thead><tr><th>#</th><th>agente</th><th>tipo</th><th>estrategia</th><th class="num">bb/100</th><th class="num">manos</th><th></th></tr></thead><tbody>${rows}</tbody></table><div id="claim-msg" class="dim" style="margin-top:6px"></div>`;
    },
    renderLive() {
      const p = this.prod || {}, act = p.active || [], br = p.bankroll;
      if (!act.length) return '<div class="empty">ningún agente jugando ahora — despliega uno arriba</div>';
      return act.map(a => `<div class="row" style="margin:6px 0;align-items:center">
        <span class="dot warn"></span> <b>${esc(a.agent || a.label)}</b> <span class="dim">· ${esc(compName(a.competition))}</span>${a.continuous ? ` <span class="pill amber">continuo</span>` : ""}
        <span class="dim">· ${(a.hands || 0).toLocaleString()} manos</span>${(a.continuous && br && br.stack != null) ? ` <span class="dim">· stack <b>${br.stack}</b> · ${br.rebuys} rebuys</span>` : ""}
        <span class="spacer"></span>
        <button class="btn sm" data-sel="${esc(a.label)}" data-selname="${esc(a.agent || a.label)}">👁 ver</button>
        <button class="btn sm destructive" data-stopprod="${esc(a.unit)}">parar</button>
        <button class="btn sm" data-claim="${esc(a.label)}">🏆 reclamar</button></div>`).join("");
    },
    renderSession() {
      const d = this.prodSession;
      if (!d) return '<div class="empty">cargando…</div>';
      if (d.error) return `<div class="neg">${esc(d.error)}</div>`;
      const kpi = (l, v) => `<div class="kpi" style="text-align:left"><div class="l">${l}</div><div class="v" style="font-size:17px">${v}</div></div>`;
      const tag = this.prodSel ? `<span class="pill accent">solo «${esc(this.prodSelName || this.prodSel)}»</span>` : '<span class="dim">sesión global · todos los agentes</span>';
      const stats = `<div class="row" style="gap:14px;align-items:center;margin-bottom:6px">${tag}<span class="spacer"></span>
        ${kpi("manos", (d.hands || 0).toLocaleString())}${kpi("decisiones", (d.decisions || 0).toLocaleString())}${kpi("M3 %", (d.m3pct || 0) + "%")}
        ${kpi("bb/100", d.agg && d.agg.mean != null ? (d.agg.mean >= 0 ? "+" : "") + d.agg.mean : "—")}</div>`;
      const posrow = `<div class="dim" style="margin-top:6px">VPIP / PFR por posición</div><div>${(d.bypos || []).map(p => `<span style="margin-right:10px">${p.pos} <b>${p.vpip}%</b>/<b>${p.pfr}%</b> <span class="dim">n${p.n}</span></span>`).join("") || '<span class="dim">—</span>'}</div>`;
      const grid = `<div class="dim">preflop · manos que jugamos (VPIP, 13×13)</div><div style="display:grid;grid-template-columns:repeat(13,1fr);gap:1px;max-width:460px;margin-top:4px">${heatGrid(d.classes || {})}</div>`;
      const post = `<div class="dim">decisiones postflop por calle</div>${postflopBars(d.postflop || {})}`;
      return stats + `<div class="row" style="align-items:flex-start;gap:26px"><div>${grid}${posrow}</div><div style="flex:1;min-width:240px">${post}</div></div>`;
    },
    onProdClick(e) {
      const s = e.target.closest("[data-stopprod]"), q = e.target.closest("[data-dequeue]"), c = e.target.closest("[data-claim]"),
        sel = e.target.closest("[data-sel]"), all = e.target.closest("[data-selall]"), rd = e.target.closest("[data-rankdel]"), k = e.target.closest("[data-key]");
      if (s) this.stopProd(s.dataset.stopprod);
      else if (q) this.dequeue(+q.dataset.dequeue);
      else if (rd) this.rankDelete(rd.dataset.rankdel);
      else if (c) this.claim(c.dataset.claim);
      else if (sel) { this.prodSel = sel.dataset.sel; this.prodSelName = sel.dataset.selname || sel.dataset.sel; this.loadHands(); this.loadSession(); }
      else if (all) { this.prodSel = ""; this.prodSelName = ""; this.loadSession(); }
      else if (k) this.openHand(decodeURIComponent(k.dataset.key));
    },
    async rankDelete(label) {
      if (!confirm("¿Eliminar «" + label + "» del ranking?")) return;
      const r = await jpost("/api/rank/delete", { label });
      if (r && r.error) { alert("No se pudo: " + r.error); return; }
      if (this.prodSel === label) { this.prodSel = ""; this.prodSelName = ""; this.loadSession(); this.loadHands(); }
      this.loadRank();
    },
    async deploy(agent, competition) { const m = document.getElementById("prod-msg"); if (m) m.textContent = "desplegando…";
      const r = await jpost("/api/production/deploy", { agent, competition });
      if (m) { if (r.error) m.innerHTML = `<span class="neg">${esc(r.error)}</span>`;
        else { const t = r.queued ? `a la cola (#${r.queue_len})` : "▶ jugando"; m.innerHTML = `<span class="pos">${t}</span>` + (r.warn ? ` <span class="dim">⚠ ${esc(r.warn)}</span>` : ""); } }
      setTimeout(() => this.loadProd(), 900); },
    async stopProd(unit) { await jpost("/api/production/stop", { unit }); setTimeout(() => this.loadProd(), 700); },
    async dequeue(i) { await jpost("/api/production/queue-remove", { index: i }); setTimeout(() => this.loadProd(), 400); },
    async claim(label) { const m = document.getElementById("claim-msg") || document.getElementById("prod-msg"); if (m) m.textContent = "obteniendo enlace de claim…";
      const d = await jget("/api/claim?label=" + encodeURIComponent(label)); if (m) m.innerHTML = d.claim_url ? `🏆 <a href="${esc(d.claim_url)}" target="_blank" rel="noopener">${esc(d.claim_url)}</a>` : `<span class="neg">${esc(d.error || "sin claim_url")}</span>`; },
    renderEquity() {
      let eq = (this.state || {}).equity || {}, hdr = "";
      if (this.prodSel) {
        const k = Object.keys(eq).find(x => x === this.prodSel || x.indexOf(this.prodSel) === 0);
        eq = k ? { [k]: eq[k] } : {};
        hdr = `<div class="row" style="margin-bottom:4px"><span class="pill accent">solo «${esc(this.prodSelName || this.prodSel)}»</span> <button class="btn sm flat" data-selall="1">ver todos</button></div>`;
      }
      return hdr + equityChart(eq, this.eqOpt);
    },

    async loadHands() { const d = await jget("/api/hands"); this.hands = d.hands || []; },
    renderHands() {
      const f = (this.handFilter || "").toLowerCase().trim();
      let rows = this.hands;
      if (this.prodSel) rows = rows.filter(h => (h.label || "") === this.prodSel || (h.label || "").indexOf(this.prodSel) === 0);
      if (this.llmOnly) rows = rows.filter(h => h.m3 > 0);
      rows = rows.filter(h => !f || (h.pos || "").toLowerCase().includes(f) || (h.hole || "").toLowerCase().includes(f)
        || (h.hclass || "").toLowerCase().includes(f) || (h.reached || "").toLowerCase().includes(f) || (h.label || "").toLowerCase().includes(f)
        || (f === "win" && h.won) || (f === "m3" && h.m3 > 0)).slice(0, 250);
      const hdr = this.prodSel ? `<div class="row" style="margin-bottom:6px"><span class="pill accent">solo «${esc(this.prodSelName || this.prodSel)}»</span> <button class="btn sm flat" data-selall="1">ver todos</button></div>` : "";
      if (!rows.length) return hdr + `<div class="empty">sin manos${this.prodSel ? " de este agente todavía" : ""}</div>`;
      return hdr + `<table class="list"><thead><tr><th>hora</th><th>arm</th><th>modo</th><th>pos</th><th>mano</th><th>board</th><th>flop</th><th>turn</th><th>river</th><th>fold</th><th>calle</th><th class="num">bote</th><th class="num">result</th></tr></thead><tbody>${rows.map(h => `<tr class="clk" data-key="${encodeURIComponent(h.key)}"><td class="dim">${tt(h.ts)}</td><td>${esc(h.label || "·")}</td><td>${h.m3 > 0 ? `<b class="pos" title="click en la mano → ver mensaje enviado y respuesta de la LLM" style="cursor:pointer;text-decoration:underline dotted">LLM</b>${h.m3 > 1 ? ` <span class="dim">${h.m3}</span>` : ""}` : '<span class="dim">HEUR</span>'}</td><td>${esc(h.pos || "")}</td><td>${chs(h.hole) || "—"}</td><td>${chs(h.board) || "—"}</td><td class="dim">${esc(h.act_flop || "·")}</td><td class="dim">${esc(h.act_turn || "·")}</td><td class="dim">${esc(h.act_river || "·")}</td><td>${!h.fold ? '<span class="pos">—</span>' : (h.fold === "preflop" ? '<span class="dim">PF</span>' : '<span class="pill amber">' + esc(h.fold) + '</span>')}</td><td>${esc(h.reached || "")}</td><td class="num">${h.pot}</td><td class="num">${h.delta == null ? "·" : `<b class="${h.delta >= 0 ? "pos" : "neg"}">${h.delta >= 0 ? "+" : ""}${h.delta}</b>`}</td></tr>`).join("")}</tbody></table>`;
    },
    async loadOpponents() { const d = await jget("/api/tracker/opponents"); this.opponents = d.opponents || []; },
    renderOpponents() {
      if (!this.opponents.length) return '<div class="empty">sin rivales aún — se llena solo durante el juego (HUD del Arena)</div>';
      return `<table class="list"><thead><tr><th>rival</th><th>estilo</th><th class="num">N</th><th class="num">VPIP</th><th class="num">PFR</th><th class="num">AF</th><th class="num">WTSD</th><th class="num">vistas</th></tr></thead><tbody>${this.opponents.map(o => `<tr><td><b>${esc(o.name || o.agent_id)}</b></td><td>${o.adapting ? `<span class="pill green">${esc(o.archetype)} · adaptando</span>` : `<span class="dim">${o.archetype === "UNKNOWN" ? "&lt;500 manos" : esc(o.archetype || "?")}</span>`}</td><td class="num">${o.n == null ? "—" : Number(o.n).toLocaleString()}</td><td class="num">${pct(o.vpip)}</td><td class="num">${pct(o.pfr)}</td><td class="num">${o.af == null ? "—" : (+o.af).toFixed(1)}</td><td class="num">${pct(o.wtsd)}</td><td class="num">${o.shown_hands || 0}</td></tr>`).join("")}</tbody></table>`;
    },
    async harvest() { await jpost("/api/tracker/harvest", {}); setTimeout(() => this.loadOpponents(), 1500); },
    async loadSettings() {
      this.settings = await jget("/api/settings");
      this.liveModel = (this.settings.live || {}).id || "";
      this.defModel = (this.settings.default || {}).id || "";
    },
    providerReady(p) { return !!((this.settings.providers || {})[p]); },
    async saveKey(provider) {
      const key = (this.keyInput[provider] || "").trim(), base = (this.baseInput[provider] || "").trim();
      if (!key && !base) { this.settingsMsg = "pega una API key primero"; return; }
      const r = await jpost("/api/settings/key", { provider, key, base });
      this.keyInput[provider] = "";
      this.settingsMsg = r.error ? ("error: " + r.error) : (provider + ": guardada ✓");
      await this.loadSettings();
      setTimeout(() => { this.settingsMsg = ""; }, 4000);
    },
    async applyLive() {
      if (!this.liveModel) return;
      await jpost("/api/settings/model", { scope: "live", model: this.liveModel });
      const r = await jpost("/api/settings/apply", {});
      this.settingsMsg2 = r.error ? ("error: " + r.error) : (r.note || ("aplicado en vivo: " + this.liveModel + " (re-desplegado)"));
      setTimeout(() => { this.settingsMsg2 = ""; }, 6000);
    },
    async saveDefault() {
      if (!this.defModel) return;
      const r = await jpost("/api/settings/model", { scope: "default", model: this.defModel });
      this.settingsMsg2 = r.error ? ("error: " + r.error) : ("default: " + this.defModel + " ✓");
      setTimeout(() => { this.settingsMsg2 = ""; }, 4000);
    },

    /* ───── reproductor ───── */
    async openHand(key) { if (!key) return; const h = await jget("/api/hand?key=" + encodeURIComponent(key)); h._ev = buildTimeline(h); this.modalHand = h; this.step = 0; this.embed = !!(h.result && h.result.replay_url); },
    closeHand() { this.modalHand = null; },
    onRowClick(e) { const r = e.target.closest("[data-key]"); if (r) this.openHand(decodeURIComponent(r.dataset.key)); const sr = e.target.closest("[data-stoprun]"); if (sr) { jpost("/api/run/stop", { unit: sr.dataset.stoprun }); setTimeout(() => this.loadRuns(), 700); } },
    renderHandModal() {
      const h = this.modalHand; if (!h) return ""; if (h.error) return `<div class="mh"><b>error</b></div><div class="mb neg">${esc(h.error)}</div>`;
      const ru = h.result && h.result.replay_url;
      const head = `<div class="mh"><b>▶ Reproductor</b> <span class="dim">${esc(h.key || "")}</span>
        ${ru ? `<a class="btn sm" href="${esc(ru)}" target="_blank" rel="noopener">▶ repro oficial</a>
        <button class="btn sm flat" data-act="embed">${this.embed ? "ver reconstrucción" : "ver oficial"}</button>` : ""}
        <span class="spacer"></span><button class="btn flat" data-act="close">✕</button></div>`;
      if (ru && this.embed) return `${head}<div class="mb"><iframe src="${esc(ru)}" style="width:100%;height:72vh;border:0;border-radius:10px" allow="fullscreen"></iframe>${m3Block(h)}</div>`;
      const ev = h._ev || [], hasRes = !!(h.result && ((h.result.seats_shown || []).length || (h.result.winners || []).length));
      const total = ev.length + (hasRes ? 1 : 0), maxStep = Math.max(0, total - 1);
      if (this.step > maxStep) this.step = maxStep;
      const isResult = hasRes && this.step >= ev.length;
      const ctl = ev.length ? `<div class="steps"><button class="btn sm" data-act="prev">◀</button><button class="btn sm" data-act="next">▶</button>
        <span class="dim">paso ${this.step + 1}/${total}${isResult ? " · RESULTADO" : ""}</span></div>` : "";
      return `${head}<div class="mb">${!ru ? '<div class="dim" style="margin-bottom:6px">repro oficial no disponible — reconstrucción local</div>' : ""}
        ${isResult ? showdownBlock(h) : ""}${ev.length ? minitable(h, ev, this.step) : ""}${ctl}${ev.length ? streetSections(h, ev) : ""}${m3Block(h)}</div>`;
    },
    onModalClick(e) { const a = e.target.closest("[data-act]"); if (!a) return; const act = a.dataset.act;
      if (act === "close") this.closeHand(); else if (act === "embed") this.embed = !this.embed;
      else if (act === "prev") this.step = Math.max(0, this.step - 1); else if (act === "next") this.step++; },
  };
}
