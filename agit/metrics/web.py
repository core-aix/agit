"""Self-contained HTML dashboard for `agit --dashboard html` (#54).

Everything the page needs is the per-commit data computed from ``git log``;
that data is serialized to JSON and embedded in a single static HTML file. All
aggregation (coverage, AI vs human lines, tokens, per-backend/model/committer
breakdowns, loop detection) happens client-side in JavaScript so the filters —
"entire team" vs a single committer, by backend, by model — recompute every
metric live without a server. The file opens straight from disk in any browser
and, like the text report, shows numbers identical on every clone.

The visual language matches docs/index.html: a CRT/phosphor terminal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agit.git import GitRepo
from agit.metrics.collect import CommitStat, Dashboard, build_dashboard


def render_html(repo: GitRepo, ref: str = "HEAD") -> str:
    return format_html(build_dashboard(repo, ref))


def format_html(dash: Dashboard) -> str:
    payload = json.dumps(dashboard_data(dash), separators=(",", ":"))
    repo_name = dash.repo.rstrip("/").rsplit("/", 1)[-1] or dash.repo
    return (
        _TEMPLATE.replace("__DATA__", payload)
        .replace("__REPO_NAME__", _escape(repo_name))
        .replace("__REPO__", _escape(dash.repo))
        .replace("__BRANCH__", _escape(dash.branch))
    )


def dashboard_data(dash: Dashboard) -> dict:
    """Serialize the dashboard to a JSON-ready dict.

    Covered commits (backend-made, #58) carry no backend/model of their own;
    they inherit the *effective* backend/model of the cover commit that lists
    them, so a per-backend or per-model filter buckets their diff with the
    model that actually wrote it.
    """
    covers: dict[str, CommitStat] = {}
    for stat in dash.stats:
        for short in stat.covered_commits:
            covers[short] = stat

    commits = []
    for stat in dash.stats:
        eff_backend, eff_model = _effective(stat, covers)
        commits.append(
            {
                "short": stat.short,
                "author": stat.author,
                "subject": stat.subject,
                "kind": stat.kind,
                "backend": stat.backend,
                "model": stat.model,
                "eff_backend": eff_backend,
                "eff_model": eff_model,
                "tokens": stat.tokens,
                "ins": stat.insertions,
                "del": stat.deletions,
                "prompt": stat.prompt,
                "user_prompts": stat.user_prompts,
            }
        )

    return {
        "repo": dash.repo,
        "branch": dash.branch,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "committers": sorted({stat.author for stat in dash.stats if stat.author}),
        "backends": sorted({c["eff_backend"] for c in commits if c["eff_backend"]}),
        "models": sorted({c["eff_model"] for c in commits if c["eff_model"]}),
        "commits": commits,
    }


def _effective(stat: CommitStat, covers: dict[str, CommitStat]) -> tuple[str | None, str | None]:
    if stat.kind in ("agent", "agent-merge"):
        return stat.backend, stat.model
    if stat.kind == "covered":
        for short, cover in covers.items():
            if stat.sha.startswith(short):
                return cover.backend, cover.model
    return None, None


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# The page. Brace-heavy CSS/JS lives verbatim in the template (no f-strings);
# only the __PLACEHOLDER__ tokens are substituted.
# ---------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aGiT dashboard · __REPO_NAME__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#070b09; --panel:#0c120e; --panel-2:#101813; --line:#1d2a21;
  --phosphor:#3dffa0; --phosphor-dim:#1f7a52; --amber:#ffb454; --amber-dim:#8a5e2a;
  --fg:#cfe7d8; --fg-dim:#7e998a; --red:#ff6b6b;
  --mono:"IBM Plex Mono",ui-monospace,monospace; --display:"VT323",var(--mono);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--ink);color:var(--fg);font-family:var(--mono);font-size:15px;line-height:1.6;overflow-x:hidden}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:30;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px);opacity:.35}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:31;
  background:radial-gradient(ellipse at 50% 12%,transparent 60%,rgba(0,0,0,.55) 100%)}
::selection{background:var(--phosphor);color:var(--ink)}
a{color:var(--phosphor);text-decoration:none;border-bottom:1px solid var(--phosphor-dim)}
a:hover{color:var(--ink);background:var(--phosphor)}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 80px}

header{padding:54px 0 22px}
.brand{font-family:var(--display);font-weight:400;font-size:clamp(56px,11vw,104px);line-height:.85;color:var(--phosphor);
  text-shadow:0 0 14px rgba(61,255,160,.5),0 0 60px rgba(61,255,160,.22);letter-spacing:2px}
.brand .a{color:var(--amber);text-shadow:0 0 14px rgba(255,180,84,.5),0 0 60px rgba(255,180,84,.2)}
.brand .sub{font-family:var(--display);font-size:.42em;color:var(--fg-dim);letter-spacing:3px;text-shadow:none}
.meta{margin-top:12px;color:var(--fg-dim);font-size:13.5px}
.meta b{color:var(--fg);font-weight:600}
.meta .tag{color:var(--amber)}

/* ---- filter bar ---- */
.controls{position:sticky;top:0;z-index:20;margin:22px 0 30px;padding:14px 16px;background:var(--panel);
  border:1px solid var(--line);border-bottom-width:3px;display:flex;flex-wrap:wrap;gap:18px;align-items:flex-end}
.controls .prompt{color:var(--phosphor);font-weight:600;align-self:center}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:11px;color:var(--amber);letter-spacing:.6px;text-transform:uppercase}
.field select{appearance:none;background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:13.5px;padding:7px 30px 7px 11px;cursor:pointer;min-width:170px;
  background-image:linear-gradient(45deg,transparent 50%,var(--phosphor-dim) 50%),linear-gradient(135deg,var(--phosphor-dim) 50%,transparent 50%);
  background-position:calc(100% - 16px) 50%,calc(100% - 11px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.field select:focus{outline:none;border-color:var(--phosphor)}
.scope{margin-left:auto;align-self:center;color:var(--fg-dim);font-size:12.5px}
.scope b{color:var(--phosphor)}
.reset{cursor:pointer;border:1px solid var(--amber);color:var(--amber);background:transparent;
  font-family:var(--mono);font-size:12.5px;padding:7px 12px;align-self:flex-end}
.reset:hover{background:var(--amber);color:var(--ink)}

h2.section{font-family:var(--display);font-size:27px;font-weight:400;color:var(--phosphor);letter-spacing:1px;
  margin:38px 0 14px;text-shadow:0 0 16px rgba(61,255,160,.3)}
h2.section::before{content:"# ";color:var(--amber)}

/* ---- stat cards ---- */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);padding:16px 18px;position:relative;transition:border-color .15s}
.card:hover{border-color:var(--phosphor-dim)}
.card .label{font-size:11.5px;color:var(--amber);letter-spacing:.5px;text-transform:uppercase}
.card .value{font-family:var(--display);font-size:42px;line-height:1.05;color:var(--phosphor);margin-top:6px;
  text-shadow:0 0 14px rgba(61,255,160,.3)}
.card .value.amber{color:var(--amber);text-shadow:0 0 14px rgba(255,180,84,.3)}
.card .note{font-size:12px;color:var(--fg-dim);margin-top:4px}

/* ---- bar / table ---- */
.panel{background:var(--panel);border:1px solid var(--line);padding:6px 0}
.row{display:grid;grid-template-columns:minmax(120px,1.4fr) 2.6fr minmax(150px,1fr);gap:14px;align-items:center;
  padding:11px 18px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:none}
.row .name{color:var(--fg);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .name small{color:var(--fg-dim);font-weight:400}
.bar{position:relative;height:18px;background:var(--ink);border:1px solid var(--line);overflow:hidden}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--phosphor-dim);box-shadow:0 0 10px rgba(61,255,160,.4)}
.bar i.amber{background:var(--amber-dim);box-shadow:0 0 10px rgba(255,180,84,.35)}
.bar span{position:absolute;right:6px;top:0;font-size:11px;color:var(--fg-dim);line-height:18px}
.row .num{text-align:right;color:var(--fg-dim);font-size:12.5px}
.row .num b{color:var(--phosphor);font-weight:600}
.empty{padding:16px 18px;color:var(--fg-dim)}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media (max-width:760px){.split{grid-template-columns:1fr}.row{grid-template-columns:1fr;gap:6px}}

/* ---- loops ---- */
.loop{padding:13px 18px;border-bottom:1px solid var(--line)}
.loop:last-child{border-bottom:none}
.loop .where{color:var(--red)}
.loop .q{color:var(--fg);font-style:italic}
.loop .cost{color:var(--amber);font-size:12.5px}

/* ---- commit log rail ---- */
.log{position:relative;padding-left:34px;margin-top:8px}
.log::before{content:"";position:absolute;left:9px;top:6px;bottom:6px;width:2px;
  background:linear-gradient(var(--phosphor-dim),var(--line) 90%,transparent)}
.entry{position:relative;padding:9px 0;border-bottom:1px dashed var(--line);display:flex;flex-wrap:wrap;gap:10px;align-items:baseline}
.entry:last-child{border-bottom:none}
.entry::before{content:"";position:absolute;left:-30px;top:15px;width:9px;height:9px;border-radius:50%;
  background:var(--ink);border:2px solid var(--phosphor-dim)}
.entry.ai::before{border-color:var(--phosphor);box-shadow:0 0 8px rgba(61,255,160,.5)}
.entry.human::before{border-color:var(--amber)}
.entry .sha{color:var(--amber);font-size:12.5px}
.entry .ksub{flex:1;min-width:200px;color:var(--fg)}
.entry .badge{font-size:10.5px;letter-spacing:.5px;text-transform:uppercase;padding:1px 7px;border:1px solid var(--line);color:var(--fg-dim)}
.entry .badge.ai{color:var(--phosphor);border-color:var(--phosphor-dim)}
.entry .badge.human{color:var(--amber);border-color:var(--amber-dim)}
.entry .lc{font-size:12px;color:var(--fg-dim)}
.entry .lc .add{color:var(--phosphor)} .entry .lc .rem{color:var(--red)}
.more{padding:12px 0;color:var(--fg-dim);font-size:12.5px}

footer{margin-top:46px;padding-top:22px;border-top:1px dashed var(--line);color:var(--fg-dim);font-size:12.5px;
  display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><span class="a">a</span>GiT<span class="sub">&nbsp;dashboard</span></div>
    <div class="meta"><span class="tag">repo</span> <b>__REPO__</b> &nbsp;·&nbsp; <span class="tag">branch</span> <b>__BRANCH__</b> &nbsp;·&nbsp; <span id="genat"></span></div>
  </header>

  <div class="controls">
    <span class="prompt">&gt; filter</span>
    <div class="field"><label for="f-author">committer</label><select id="f-author"></select></div>
    <div class="field"><label for="f-backend">backend</label><select id="f-backend"></select></div>
    <div class="field"><label for="f-model">model</label><select id="f-model"></select></div>
    <button class="reset" id="reset">reset</button>
    <span class="scope" id="scope"></span>
  </div>

  <h2 class="section">overview</h2>
  <div class="cards" id="cards"></div>

  <h2 class="section">code changes &amp; tokens</h2>
  <div class="split">
    <div class="panel" id="lines"></div>
    <div class="panel" id="tokens"></div>
  </div>

  <h2 class="section">by backend</h2>
  <div class="panel" id="by-backend"></div>

  <h2 class="section">by model</h2>
  <div class="panel" id="by-model"></div>

  <h2 class="section">by committer</h2>
  <div class="panel" id="by-committer"></div>

  <h2 class="section">possible loops</h2>
  <div class="panel" id="loops"></div>

  <h2 class="section">commit log</h2>
  <div class="log" id="commitlog"></div>

  <footer>
    <span>aGiT · agent + git · metrics from commit metadata</span>
    <span id="count"></span>
  </footer>
</div>

<script type="application/json" id="agit-data">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("agit-data").textContent);
const COMMITS = DATA.commits;
const AI_KINDS = new Set(["agent","covered","agent-merge"]);
const HUMAN_KINDS = new Set(["user","untracked"]);
const TRACKED = new Set(["agent","covered","agent-merge","user"]);
const TOKEN_ORDER = [["input","input"],["output","output"],["reasoning","reasoning"],
  ["cache_read","cache read"],["cache_write","cache write"],
  ["subagent_input","subagent input"],["subagent_output","subagent output"],
  ["subagent_cache_read","subagent cache read"],["subagent_cache_write","subagent cache write"],
  ["summary_input","summarizer input"],["summary_output","summarizer output"]];
const SIM_THRESHOLD = 0.6, LOOP_MIN_RUN = 3, LOG_CAP = 80;

const state = {author:"", backend:"", model:""};
const $ = id => document.getElementById(id);
const fmt = n => (n||0).toLocaleString("en-US");
const pct = (a,b) => b ? (a/b*100).toFixed(1)+"%" : "0%";
const esc = s => (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

function filtered(){
  return COMMITS.filter(c =>
    (!state.author  || c.author === state.author) &&
    (!state.backend || c.eff_backend === state.backend) &&
    (!state.model   || c.eff_model === state.model));
}
function sumLines(cs, kinds){
  let ins=0, del=0;
  for(const c of cs) if(kinds.has(c.kind)){ ins+=c.ins; del+=c.del; }
  return {ins, del, total:ins+del};
}
function tokenTotals(cs){
  const t={};
  for(const c of cs) for(const k in c.tokens) t[k]=(t[k]||0)+c.tokens[k];
  return t;
}
function groupBy(cs, key){
  const g={};
  for(const c of cs){
    if(c.kind!=="agent" && c.kind!=="covered") continue;
    const label=c[key]||"unknown";
    const b=g[label]||(g[label]={commits:0,ins:0,del:0,output:0});
    if(c.kind==="agent") b.commits+=1;
    b.ins+=c.ins; b.del+=c.del; b.output+=(c.tokens.output||0);
  }
  return g;
}
function byCommitter(cs){
  const g={};
  for(const c of cs){
    const label=c.author||"unknown";
    const b=g[label]||(g[label]={commits:0,agit:0,ins:0,del:0});
    b.commits+=1; if(c.kind!=="untracked") b.agit+=1; b.ins+=c.ins; b.del+=c.del;
  }
  return g;
}

// --- loop detection: same word-overlap rule as the Python collector ---
function similar(a,b){
  a=(a||"").toLowerCase().split(/\s+/).join(" "); b=(b||"").toLowerCase().split(/\s+/).join(" ");
  if(!a||!b) return false; if(a===b) return true;
  const wa=new Set(a.match(/[a-z0-9]+/g)||[]), wb=new Set(b.match(/[a-z0-9]+/g)||[]);
  if(!wa.size||!wb.size) return false;
  let inter=0; for(const w of wa) if(wb.has(w)) inter++;
  return inter/(wa.size+wb.size-inter) >= SIM_THRESHOLD;
}
function detectLoops(cs){
  const out=[];
  const agents=cs.filter(c => c.kind==="agent" && c.prompt);
  let run=[];
  const flush=()=>{ if(run.length>=LOOP_MIN_RUN) out.push({
    shas:run.map(c=>c.short), prompt:run[0].prompt,
    output:run.reduce((s,c)=>s+(c.tokens.output||0),0), within:false}); };
  for(const c of agents){
    if(run.length && similar(run[run.length-1].prompt, c.prompt)){ run.push(c); continue; }
    flush(); run=[c];
  }
  flush();
  for(const c of cs){
    if(c.kind!=="agent" || (c.user_prompts||[]).length<LOOP_MIN_RUN) continue;
    let longest=1, cur=1;
    for(let i=1;i<c.user_prompts.length;i++){ cur = similar(c.user_prompts[i-1],c.user_prompts[i]) ? cur+1 : 1; longest=Math.max(longest,cur); }
    if(longest>=LOOP_MIN_RUN) out.push({shas:[c.short], prompt:c.user_prompts[0], output:(c.tokens.output||0), within:true});
  }
  return out;
}

function barRow(name, sub, value, max, numHtml, amber){
  const w = max ? Math.max(2, value/max*100) : 0;
  return `<div class="row"><div class="name">${esc(name)}${sub?` <small>${esc(sub)}</small>`:""}</div>`+
    `<div class="bar"><i class="${amber?"amber":""}" style="width:${w}%"></i></div>`+
    `<div class="num">${numHtml}</div></div>`;
}

function render(){
  const cs = filtered();
  const total = cs.length;
  const tracked = cs.filter(c=>TRACKED.has(c.kind)).length;
  const ai = sumLines(cs, AI_KINDS), human = sumLines(cs, HUMAN_KINDS);
  const tok = tokenTotals(cs);
  const eff = tok.output ? (ai.total/tok.output*1000) : null;

  $("genat").textContent = "generated " + DATA.generated_at;
  $("scope").innerHTML = state.author ? `scope: <b>${esc(state.author)}</b>` : `scope: <b>entire team</b>`;
  $("count").textContent = `${fmt(total)} commits shown of ${fmt(COMMITS.length)}`;

  // overview cards
  const kinds = k => cs.filter(c=>c.kind===k).length;
  $("cards").innerHTML = [
    card("commits", fmt(total), `${fmt(tracked)} via aGiT`),
    card("aGiT coverage", pct(tracked,total), `${fmt(total-tracked)} untracked`, true),
    card("AI lines", "+"+fmt(ai.ins), `−${fmt(ai.del)} · ${pct(ai.total, ai.total+human.total)} of changes`),
    card("human lines", "+"+fmt(human.ins), `−${fmt(human.del)}`, true),
    card("output tokens", fmt(tok.output||0), `${fmt(tok.input||0)} input`),
    card("efficiency", eff===null?"—":eff.toFixed(1), "AI lines / 1k output tok", true),
  ].join("");

  // lines panel
  $("lines").innerHTML =
    `<div class="row"><div class="name">AI <small>agent+covered</small></div>`+
      `<div class="bar"><i style="width:${ai.total+human.total?ai.total/(ai.total+human.total)*100:0}%"></i></div>`+
      `<div class="num"><b>+${fmt(ai.ins)}</b> / −${fmt(ai.del)}</div></div>`+
    `<div class="row"><div class="name">Human <small>user+untracked</small></div>`+
      `<div class="bar"><i class="amber" style="width:${ai.total+human.total?human.total/(ai.total+human.total)*100:0}%"></i></div>`+
      `<div class="num"><b>+${fmt(human.ins)}</b> / −${fmt(human.del)}</div></div>`+
    `<div class="row"><div class="name">agent ${kinds("agent")} · covered ${kinds("covered")} · merge ${kinds("agent-merge")}</div>`+
      `<div class="bar"></div><div class="num">user ${kinds("user")} · untracked ${kinds("untracked")}</div></div>`;

  // tokens panel
  const shown = TOKEN_ORDER.filter(([k])=>tok[k]);
  const maxTok = Math.max(1, ...shown.map(([k])=>tok[k]));
  $("tokens").innerHTML = shown.length
    ? shown.map(([k,label]) => barRow(label, "", tok[k], maxTok, `<b>${fmt(tok[k])}</b>`, k==="output")).join("")
    : `<div class="empty">no token metadata recorded</div>`;

  $("by-backend").innerHTML = groupPanel(groupBy(cs,"eff_backend"));
  $("by-model").innerHTML = groupPanel(groupBy(cs,"eff_model"));

  // committer table (clickable to focus a person)
  const comm = byCommitter(cs);
  const maxC = Math.max(1, ...Object.values(comm).map(b=>b.commits));
  const commEntries = Object.entries(comm).sort((a,b)=>b[1].commits-a[1].commits);
  $("by-committer").innerHTML = commEntries.length
    ? commEntries.map(([name,b]) =>
        barRow(name, `${b.agit} via aGiT`, b.commits, maxC,
          `<b>${fmt(b.commits)}</b> commits · +${fmt(b.ins)}/−${fmt(b.del)}`)).join("")
    : `<div class="empty">no commits</div>`;

  // loops
  const loops = detectLoops(cs);
  $("loops").innerHTML = loops.length
    ? loops.map(l => {
        const where = l.within ? `within commit ${l.shas[0]}` : `${l.shas.length} commits ${l.shas[0]}..${l.shas[l.shas.length-1]}`;
        const q = l.prompt.length>90 ? l.prompt.slice(0,87)+"…" : l.prompt;
        return `<div class="loop"><span class="where">${esc(where)}</span> — <span class="q">"${esc(q)}"</span>`+
          (l.output?` <span class="cost">${fmt(l.output)} output tokens</span>`:"")+`</div>`;
      }).join("")
    : `<div class="empty">none detected</div>`;

  // commit log (newest first)
  const ordered = cs.slice().reverse();
  const head = ordered.slice(0, LOG_CAP).map(c => {
    const ai_ = AI_KINDS.has(c.kind), cls = ai_?"ai":(HUMAN_KINDS.has(c.kind)?"human":"");
    const badge = ai_?`<span class="badge ai">${c.kind}</span>`:`<span class="badge human">${c.kind}</span>`;
    const lc = (c.ins||c.del)?`<span class="lc"><span class="add">+${fmt(c.ins)}</span> <span class="rem">−${fmt(c.del)}</span></span>`:"";
    const m = c.eff_model?` <span class="lc">${esc(c.eff_model)}</span>`:"";
    return `<div class="entry ${cls}"><span class="sha">${esc(c.short)}</span>${badge}`+
      `<span class="ksub">${esc(c.subject)}</span>${lc}${m}</div>`;
  }).join("");
  $("commitlog").innerHTML = (head || `<div class="empty">no commits</div>`) +
    (ordered.length>LOG_CAP ? `<div class="more">… ${fmt(ordered.length-LOG_CAP)} more</div>` : "");
}

function card(label, value, note, amber){
  return `<div class="card"><div class="label">${esc(label)}</div>`+
    `<div class="value ${amber?"amber":""}">${value}</div><div class="note">${esc(note||"")}</div></div>`;
}
function groupPanel(groups){
  const entries = Object.entries(groups).sort((a,b)=>b[1].commits-a[1].commits || (b[1].ins+b[1].del)-(a[1].ins+a[1].del));
  if(!entries.length) return `<div class="empty">no agent commits</div>`;
  const max = Math.max(1, ...entries.map(([,b])=>b.ins+b.del));
  return entries.map(([label,b]) =>
    barRow(label, `${b.commits} commits`, b.ins+b.del, max,
      `+${fmt(b.ins)}/−${fmt(b.del)}${b.output?` · <b>${fmt(b.output)}</b> tok`:""}`)).join("");
}

function fillSelect(id, values, allLabel){
  const sel = $(id);
  sel.innerHTML = `<option value="">${allLabel}</option>` +
    values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
}
function init(){
  fillSelect("f-author", DATA.committers, "— entire team —");
  fillSelect("f-backend", DATA.backends, "— all backends —");
  fillSelect("f-model", DATA.models, "— all models —");
  $("f-author").onchange = e => { state.author = e.target.value; render(); };
  $("f-backend").onchange = e => { state.backend = e.target.value; render(); };
  $("f-model").onchange = e => { state.model = e.target.value; render(); };
  $("reset").onclick = () => {
    state.author=state.backend=state.model="";
    $("f-author").value=$("f-backend").value=$("f-model").value="";
    render();
  };
  render();
}
init();
</script>
</body>
</html>
"""
