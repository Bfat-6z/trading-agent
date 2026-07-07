"""Render the agent's second brain (memory/ + vault/ markdown + [[wikilinks]]) as an
HONEST graph: ONLY the real [[wikilink]] edges are drawn (no decorative mesh). Layout is
force-directed but computed ONCE in Python and FROZEN — positions reflect real relatedness
and never jitter. Only signal particles animate along the real links. Self-contained HTML.
Owner 2026-07-08 chose "honest mode": every edge = a real relationship, 100% correct logic."""
from __future__ import annotations

import json
import math
import random
import re
from pathlib import Path

MEM = Path(r"C:\Users\ACER\.claude\projects\E--keo-moi-mail\memory")
VAULT = Path(r"E:\keo-moi-mail\trading-agent\vault")
OUT = Path(r"E:\keo-moi-mail\horizon-ui\brain\index.html")

LINK_RE = re.compile(r"\[\[([^\]|#]+)")
MDLINK_RE = re.compile(r"\]\(([a-zA-Z0-9_\-]+)\.md\)")
NAME_RE = re.compile(r"^name:\s*(.+)$", re.M)
TYPE_RE = re.compile(r"type:\s*(user|feedback|project|reference)\b")
DESC_RE = re.compile(r"^description:\s*(.+)$", re.M)


def slug(p: Path) -> str:
    return p.stem.strip().lower()


def collect():
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str]] = []
    files: list[tuple[Path, str]] = [(p, "memory") for p in sorted(MEM.glob("*.md"))]
    for p in sorted(VAULT.rglob("*.md")):
        rel = p.relative_to(VAULT)
        grp = "vault-" + (rel.parts[0] if len(rel.parts) > 1 else "root")
        if grp == "vault-auto":
            continue
        files.append((p, grp))

    for p, grp in files:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        sid = slug(p)
        nm, tp, ds = NAME_RE.search(txt), TYPE_RE.search(txt), DESC_RE.search(txt)
        group = tp.group(1) if (grp == "memory" and tp) else grp
        label = (nm.group(1).strip() if nm else p.stem).replace("_", " ")[:38]
        nodes[sid] = {"id": sid, "label": label, "group": group,
                      "desc": (ds.group(1).strip()[:120] if ds else ""), "deg": 0,
                      "_txt": txt, "_stem": p.stem.lower()}

    by_key: dict[str, str] = {}
    for sid, n in nodes.items():
        by_key[sid] = sid
        by_key[n["label"].lower().replace(" ", "_")] = sid
        by_key[n["_stem"]] = sid

    seen = set()
    for sid, n in nodes.items():
        for t in set(LINK_RE.findall(n["_txt"])) | set(MDLINK_RE.findall(n["_txt"])):
            tk = t.strip().lower().replace(" ", "_").replace(".md", "")
            tgt = by_key.get(tk) or by_key.get(t.strip().lower())
            if tgt and tgt != sid and (sid, tgt) not in seen:
                seen.add((sid, tgt))
                edges.append((sid, tgt))
                nodes[sid]["deg"] += 1
                nodes[tgt]["deg"] += 1

    keep = {s for e in edges for s in e}
    fin = [{k: v for k, v in n.items() if not k.startswith("_")}
           for sid, n in nodes.items() if sid in keep]
    idx = {n["id"]: i for i, n in enumerate(fin)}
    elist = [(idx[a], idx[b]) for a, b in edges if a in idx and b in idx]
    return fin, elist


def layout(nodes, edges, iters=520, Wd=1500.0, Ht=820.0):
    """Fruchterman-Reingold, deterministic (seeded). Runs to convergence then we FREEZE
    the coords — no runtime sim, no jitter. Positions encode real [[wikilink]] relatedness."""
    rnd = random.Random(7)
    n = len(nodes)
    P = [[rnd.uniform(-Wd / 3, Wd / 3), rnd.uniform(-Ht / 3, Ht / 3)] for _ in range(n)]
    k = math.sqrt((Wd * Ht) / n) * 0.72          # ideal edge length
    temp = Wd / 8.0
    for _ in range(iters):
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):                        # repulsion (all pairs, n=65 -> cheap)
            for j in range(i + 1, n):
                dx = P[i][0] - P[j][0]; dy = P[i][1] - P[j][1]
                d = math.hypot(dx, dy) + 0.01
                fr = k * k / d
                ux, uy = dx / d * fr, dy / d * fr
                disp[i][0] += ux; disp[i][1] += uy
                disp[j][0] -= ux; disp[j][1] -= uy
        for a, b in edges:                        # attraction along REAL links only
            dx = P[a][0] - P[b][0]; dy = P[a][1] - P[b][1]
            d = math.hypot(dx, dy) + 0.01
            fa = d * d / k
            ux, uy = dx / d * fa, dy / d * fa
            disp[a][0] -= ux; disp[a][1] -= uy
            disp[b][0] += ux; disp[b][1] += uy
        for i in range(n):
            dl = math.hypot(*disp[i]) + 1e-9
            step = min(dl, temp)
            P[i][0] += disp[i][0] / dl * step - P[i][0] * 0.006   # + gentle centering gravity
            P[i][1] += disp[i][1] / dl * step - P[i][1] * 0.006
        temp *= 0.975
    # normalize to a centered landscape box
    xs = [p[0] for p in P]; ys = [p[1] for p in P]
    cx = (min(xs) + max(xs)) / 2; cy = (min(ys) + max(ys)) / 2
    sx = (Wd / 2) / max(1.0, max(abs(x - cx) for x in xs))
    sy = (Ht / 2) / max(1.0, max(abs(y - cy) for y in ys))
    for i, nd in enumerate(nodes):                # independent x/y scale -> fill the wide box
        nd["x"] = round((P[i][0] - cx) * sx, 1)   # (mild aspect distortion is fine for a graph;
        nd["y"] = round((P[i][1] - cy) * sy, 1)   #  topology matters, exact distances don't)


NODES, EDGES = collect()
layout(NODES, EDGES)
DATA = json.dumps({"nodes": NODES, "edges": [{"s": a, "t": b} for a, b in EDGES]},
                  ensure_ascii=False, separators=(",", ":"))
print(f"nodes={len(NODES)} real_links={len(EDGES)}")

HTML = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=4">
<title>Second Brain · Trading Agent</title>
<style>
  :root{--bg:#05070f;--ink:#e8ecf4;--dim:#828da3}
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{height:100%;background:var(--bg);color:var(--ink);overflow:hidden;
    font-family:'JetBrains Mono',ui-monospace,'SF Mono',Menlo,monospace;-webkit-font-smoothing:antialiased}
  #c{position:fixed;inset:0;display:block;cursor:grab}#c:active{cursor:grabbing}
  .hud{position:fixed;pointer-events:none;z-index:5}
  #top{top:20px;left:24px}
  #top h1{font-size:13px;letter-spacing:.34em;font-weight:600;color:#eaf0fb;text-transform:uppercase}
  #top .sub{font-size:10px;letter-spacing:.2em;color:var(--dim);margin-top:6px}
  #stat{top:22px;right:24px;text-align:right;font-size:10px;letter-spacing:.1em;color:var(--dim);line-height:1.7}
  #stat b{color:#5ad6b0;font-weight:600}
  #legend{bottom:20px;left:24px;font-size:10px;letter-spacing:.06em;line-height:1.85;color:var(--dim)}
  #legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;vertical-align:1px;box-shadow:0 0 8px currentColor}
  #tip{bottom:20px;right:24px;font-size:10px;letter-spacing:.1em;color:#5a6478;text-align:right;line-height:1.7}
  #card{position:fixed;z-index:6;pointer-events:none;max-width:280px;padding:12px 14px;
    background:rgba(10,13,22,.95);border:1px solid rgba(90,214,176,.35);border-radius:10px;
    box-shadow:0 10px 40px rgba(0,0,0,.6);opacity:0;transition:opacity .1s;backdrop-filter:blur(8px)}
  #card .t{font-size:12px;font-weight:600;color:#eaf0fb;letter-spacing:.03em;line-height:1.4}
  #card .g{font-size:9px;letter-spacing:.16em;text-transform:uppercase;margin:5px 0 7px;color:#5ad6b0}
  #card .d{font-size:10.5px;color:#aab3c5;line-height:1.55}
</style></head><body>
<canvas id="c"></canvas>
<div class="hud" id="top"><h1>Second Brain</h1><div class="sub">TRADING AGENT · HONEST MODE · REAL [[LINKS]] ONLY</div></div>
<div class="hud" id="stat"></div>
<div class="hud" id="legend"></div>
<div class="hud" id="tip">hover: soi neuron · kéo: di chuyển · lăn: zoom<br>mọi dây = 1 [[wikilink]] có thật</div>
<div id="card"><div class="t"></div><div class="g"></div><div class="d"></div></div>
<script>
const DATA=__DATA__;
const COL={user:'#e85d9c',feedback:'#f0b23f',project:'#5ad6b0',reference:'#4aa3ff',
  'vault-lessons':'#c86bd8','vault-root':'#7fd0ff','memory':'#ffffff'};
const ORDER={user:0,feedback:1,memory:2,project:3,'vault-root':4,reference:5,'vault-lessons':6};
function colOf(g){return COL[g]||'#6bd0a8';}
const cv=document.getElementById('c'),ctx=cv.getContext('2d');
let W,H,DPR;function size(){DPR=Math.min(2,devicePixelRatio||1);W=cv.width=innerWidth*DPR;H=cv.height=innerHeight*DPR;cv.style.width=innerWidth+'px';cv.style.height=innerHeight+'px';}size();addEventListener('resize',size);
const N=DATA.nodes,E=DATA.edges;
N.forEach(n=>{n.r=4.5+Math.min(12,Math.sqrt(n.deg)*2.4);});   // size by degree (real link count)
const adj=N.map(()=>new Set());E.forEach(e=>{adj[e.s].add(e.t);adj[e.t].add(e.s);e.ph=Math.random();});
// ---- static view (positions are FROZEN from Python; only pan/zoom) ----
let ox=0,oy=0,scale=Math.min((innerWidth-140)/1560,(innerHeight-170)/900);
let drag=false,px,py;
cv.addEventListener('pointerdown',e=>{drag=true;px=e.clientX;py=e.clientY;});
addEventListener('pointerup',()=>drag=false);
addEventListener('pointermove',e=>{if(drag){ox+=e.clientX-px;oy+=e.clientY-py;px=e.clientX;py=e.clientY;card.style.opacity=0;}else hoverAt(e);});
cv.addEventListener('wheel',e=>{e.preventDefault();scale=Math.max(.3,Math.min(4.5,scale*(e.deltaY<0?1.12:.893)));},{passive:false});
function sx(n){return n.x*scale+innerWidth/2+ox;}
function sy(n){return n.y*scale+innerHeight/2+oy;}
let hover=-1;const card=document.getElementById('card'),cT=card.querySelector('.t'),cG=card.querySelector('.g'),cD=card.querySelector('.d');
function hoverAt(e){const mx=e.clientX,my=e.clientY;let best=-1,bd=15;
  for(let i=0;i<N.length;i++){const d=Math.hypot(sx(N[i])-mx,sy(N[i])-my);if(d<Math.max(9,N[i].r*scale+4)&&d<bd){bd=d;best=i;}}
  hover=best;
  if(best>=0){const n=N[best];cT.textContent=n.label;cG.textContent=n.group.replace('vault-','vault · ');cG.style.color=colOf(n.group);
    cD.textContent=n.desc||'';cD.style.display=n.desc?'block':'none';card.style.borderColor=colOf(n.group)+'66';
    card.style.opacity=1;card.style.left=Math.min(innerWidth-292,mx+16)+'px';card.style.top=Math.min(innerHeight-120,my+14)+'px';}
  else card.style.opacity=0;}
function hexA(h,a){const n=parseInt(h.slice(1),16);return`rgba(${n>>16&255},${n>>8&255},${n&255},${a})`;}
let tk=0;
function draw(){
  tk++;
  ctx.setTransform(DPR,0,0,DPR,0,0);
  const g=ctx.createRadialGradient(innerWidth*.5,innerHeight*.5,60,innerWidth*.5,innerHeight*.5,Math.max(innerWidth,innerHeight)*.72);
  g.addColorStop(0,'#0b1224');g.addColorStop(.6,'#070b16');g.addColorStop(1,'#04060c');
  ctx.fillStyle=g;ctx.fillRect(0,0,innerWidth,innerHeight);
  const hp=hover>=0;
  // REAL edges only — every line is a [[wikilink]]
  ctx.lineWidth=1;
  for(const e of E){const a=N[e.s],b=N[e.t];const ax=sx(a),ay=sy(a),bx=sx(b),by=sy(b);
    const lit=hp&&(e.s===hover||e.t===hover);
    let al=lit?0.6:(hp?0.05:0.22);
    ctx.strokeStyle=lit?'rgba(150,240,205,0.7)':hexA(colOf(a.group),al);
    ctx.beginPath();ctx.moveTo(ax,ay);
    const mx=(ax+bx)/2,my=(ay+by)/2-Math.hypot(bx-ax,by-ay)*0.07;   // gentle arc
    ctx.quadraticCurveTo(mx,my,bx,by);ctx.stroke();
    if(!hp||lit){const t=((tk*0.006+e.ph)%1);
      const qx=(1-t)*(1-t)*ax+2*(1-t)*t*mx+t*t*bx, qy=(1-t)*(1-t)*ay+2*(1-t)*t*my+t*t*by;
      ctx.fillStyle=lit?'#d6fff0':hexA(colOf(a.group),0.65);ctx.beginPath();ctx.arc(qx,qy,lit?2.2:1.4,0,7);ctx.fill();}
  }
  // nodes (static, frozen positions)
  for(let i=0;i<N.length;i++){const n=N[i],x=sx(n),y=sy(n),c=colOf(n.group);
    const dim=hp&&i!==hover&&!adj[hover].has(i);
    ctx.shadowColor=c;ctx.shadowBlur=dim?0:(i===hover?26:9+n.r);
    ctx.fillStyle=dim?hexA(c,0.3):c;ctx.beginPath();ctx.arc(x,y,n.r*scale*(i===hover?1.5:1),0,7);ctx.fill();
    ctx.shadowBlur=0;
    if((n.deg>=4||i===hover)&&!dim&&scale>0.42){ctx.fillStyle=i===hover?'#fff':'rgba(220,228,244,.72)';
      ctx.font=(i===hover?'600 12px ':'10px ')+"ui-monospace,monospace";ctx.textAlign='center';
      ctx.fillText(n.label,x,y-n.r*scale-6);}
  }
  requestAnimationFrame(draw);
}
document.getElementById('stat').innerHTML=`<b>${N.length}</b> neurons &nbsp; <b>${E.length}</b> real links<br><span style="color:#556">chỉ [[wikilink]] thật · frozen layout</span>`;
const gs=[...new Set(N.map(n=>n.group))].sort((a,b)=>(ORDER[a]??9)-(ORDER[b]??9));
document.getElementById('legend').innerHTML=gs.map(g=>`<div><i style="color:${colOf(g)}"></i>${g.replace('vault-','vault · ')}</div>`).join('');
draw();
</script></body></html>"""

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(HTML.replace("__DATA__", DATA), encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size//1024} KB)")
