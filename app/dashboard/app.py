"""A single page showing every camera, its score, its forecast and the last
report the VLM wrote about it."""
import os

import requests
from flask import Flask, Response, render_template_string, jsonify

ANALYZER = os.environ.get("ANALYZER_URL", "http://analyzer:8080")
app = Flask(__name__)

PAGE = """<!doctype html><meta charset=utf-8>
<title>Visual inspection</title>
<style>
 :root{--bg:#0f1216;--card:#171b21;--line:#252b34;--fg:#e6e9ee;--dim:#96a0ae;
       --ok:#3fb950;--warn:#d29922;--bad:#f85149}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
 header{padding:16px 20px;border-bottom:1px solid var(--line)}
 h1{margin:0;font-size:16px;font-weight:600}
 .sub{color:var(--dim);font-size:12px;margin-top:2px}
 .grid{display:grid;gap:14px;padding:20px;
       grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;
       overflow:hidden}
 .card img{width:100%;display:block;aspect-ratio:16/9;object-fit:cover;
           background:#000}
 .body{padding:12px}
 .row{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
 .name{font-weight:600}
 .score{font-variant-numeric:tabular-nums;font-weight:600}
 .ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
 .eta{color:var(--dim);font-size:12px;margin-top:4px}
 .report{margin-top:10px;padding:10px;background:#0e1217;border-radius:6px;
         border-left:3px solid var(--bad);font-size:13px;color:#cfd6e0}
 /* Everything about a camera lives under that camera: where it is running and
    what it is costing, so a single card is complete on its own. */
 .stats{margin-top:10px;padding-top:10px;border-top:1px solid var(--line);
        display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:12px;
        color:var(--dim);font-variant-numeric:tabular-nums}
 .stats b{color:var(--fg);font-weight:600}
 .stats .k{color:#6f7a89}
 .bar{height:4px;background:#0e1217;border-radius:2px;margin-top:6px;overflow:hidden}
 .bar i{display:block;height:100%;background:var(--ok)}
 .verdict{font-size:11px;font-weight:700;letter-spacing:.06em;padding:1px 6px;
          border-radius:4px;background:#0e1217}
</style>
<header>
  <h1>Predictive visual quality inspection</h1>
  <div class=sub>fast path scores every frame &middot; the VLM is asked only about flagged ones</div>
</header>
<div class=grid id=grid></div>
<script>
function gib(b){ return (b/1073741824).toFixed(1); }

let RT = {};

async function runtime(){
  try {
    RT = await (await fetch('api/runtime')).json();
  } catch (e) { /* the analyzer is still fitting; the cards say so */ }
}

async function tick(){
  const r = await fetch('api/state'); const s = await r.json();
  const grid = document.getElementById('grid');
  for (const [cam, d] of Object.entries(s).sort()) {
    let el = document.getElementById(cam);
    if (!el) {
      el = document.createElement('div'); el.className='card'; el.id=cam;
      el.innerHTML = `<img id="${cam}-img"><div class=body>
        <div class=row><span class=name>${cam}</span>
        <span class=score id="${cam}-score">-</span></div>
        <div class=eta id="${cam}-eta"></div>
        <div class=stats id="${cam}-stats"></div>
        <div class=bar><i id="${cam}-bar" style="width:0%"></i></div>
        <div id="${cam}-report"></div></div>`;
      grid.appendChild(el);
    }
    // The threshold is fitted per model, not a constant. It used to be
    // hardcoded at 3.5 here, which after the detector changed meant every
    // camera rendered green whatever it scored.
    const sc = d.score ?? 0;
    const th = d.threshold ?? 1;
    const cls = sc >= th ? 'bad' : sc >= th*0.7 ? 'warn' : 'ok';
    const scoreEl = document.getElementById(`${cam}-score`);
    scoreEl.textContent = `${sc.toFixed(3)} / ${th.toFixed(3)}`;
    scoreEl.className = 'score ' + cls;

    // Where this camera is being processed, and what it costs there. The node
    // and the card are the analyzer's, and so the same for every camera --
    // repeated per card deliberately, so a card can be read on its own.
    const g = d.gpu;
    const dev = RT.gpu || {};
    const uuid = (dev.uuid || '').replace(/^GPU-/,'').slice(0,8);
    const rows = [
      ['node', RT.node ? `<b>${RT.node}</b>` : '&hellip;'],
      ['gpu', dev.name ? `<b>${dev.name}</b>${uuid ? ' &middot; ' + uuid : ''}`
                       : '<b>cpu only</b>'],
    ];
    if (g && g.share != null) {
      rows.push(['share', `<b>${(g.share*100).toFixed(1)}%</b> of the device`]);
      rows.push(['rate', `<b>${(g.fps ?? 0).toFixed(1)}</b> fps &middot; ` +
                         `${g.ms_p50.toFixed(1)} / ${g.ms_p95.toFixed(1)} ms p50/p95`]);
    } else {
      rows.push(['share', 'waiting for the first scored frame']);
    }
    if (dev.utilization != null) {
      rows.push(['device', `<b>${dev.utilization}%</b> util &middot; ` +
        `${gib(dev.memory_used)}/${gib(dev.memory_total)} GiB &middot; ` +
        `${dev.temperature}&deg;C &middot; ${dev.power_watts.toFixed(0)} W`]);
    }
    document.getElementById(`${cam}-stats`).innerHTML =
      rows.map(([k,v]) => `<span class=k>${k}</span><span>${v}</span>`).join('');

    const bar = document.getElementById(`${cam}-bar`);
    const share = g && g.share != null ? g.share : 0;
    bar.style.width = Math.min(100, share*100).toFixed(1) + '%';
    bar.style.background = share > 0.6 ? 'var(--bad)'
                         : share > 0.3 ? 'var(--warn)' : 'var(--ok)';
    const eta = document.getElementById(`${cam}-eta`);
    eta.textContent = d.eta == null ? 'trend flat'
      : `trending to threshold in ~${Math.round(d.eta/60)} min`;
    const rep = document.getElementById(`${cam}-report`);
    rep.innerHTML = d.report ? `<div class=report>${d.report}</div>` : '';
    document.getElementById(`${cam}-img`).src =
      `api/frame/${cam}.jpg?t=${Date.now()}`;
  }
}
tick(); setInterval(tick, 1000);
runtime(); setInterval(runtime, 5000);
</script>"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/api/state")
def state():
    try:
        return jsonify(requests.get(f"{ANALYZER}/api/state", timeout=5).json())
    except Exception as exc:                                  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.route("/api/runtime")
def api_runtime():
    r = requests.get(f"{ANALYZER}/api/runtime", timeout=5)
    return Response(r.content, mimetype="application/json", status=r.status_code)


@app.route("/api/frame/<path:name>")
def frame(name):
    try:
        r = requests.get(f"{ANALYZER}/api/frame/{name}", timeout=5)
        return Response(r.content, mimetype="image/jpeg", status=r.status_code)
    except Exception:                                         # noqa: BLE001
        return Response(status=502)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
