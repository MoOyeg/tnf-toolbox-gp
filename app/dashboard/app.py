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
</style>
<header>
  <h1>Predictive visual quality inspection</h1>
  <div class=sub>fast path scores every frame &middot; the VLM is asked only about flagged ones</div>
</header>
<div class=grid id=grid></div>
<script>
const THRESH = 3.5;
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
        <div id="${cam}-report"></div></div>`;
      grid.appendChild(el);
    }
    const sc = d.score ?? 0;
    const cls = sc >= THRESH ? 'bad' : sc >= THRESH*0.7 ? 'warn' : 'ok';
    const scoreEl = document.getElementById(`${cam}-score`);
    scoreEl.textContent = sc.toFixed(2);
    scoreEl.className = 'score ' + cls;
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


@app.route("/api/frame/<path:name>")
def frame(name):
    try:
        r = requests.get(f"{ANALYZER}/api/frame/{name}", timeout=5)
        return Response(r.content, mimetype="image/jpeg", status=r.status_code)
    except Exception:                                         # noqa: BLE001
        return Response(status=502)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
