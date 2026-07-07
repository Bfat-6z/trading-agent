"""Rebuild the LAYERED 'v2' brain view (owner wants to A/B it vs the honest v3). Same nodes/
edges as build_brain_graph.py, but laid out in STATIC neural-net layers (spindle) with a dense
adjacent-layer mesh (the fanning-lens look from the X video). Outputs to /brain-v2/ so both
views are served side by side. Nodes are real; the dense mesh is DECORATIVE (v3 is the honest one)."""
from __future__ import annotations

import json
import re
from pathlib import Path

MEM = Path(r"C:\Users\ACER\.claude\projects\E--keo-moi-mail\memory")
VAULT = Path(r"E:\keo-moi-mail\trading-agent\vault")
OUT = Path(r"E:\keo-moi-mail\horizon-ui\brain-v2\index.html")

LINK_RE = re.compile(r"\[\[([^\]|#]+)")
MDLINK_RE = re.compile(r"\]\(([a-zA-Z0-9_\-]+)\.md\)")
NAME_RE = re.compile(r"^name:\s*(.+)$", re.M)
TYPE_RE = re.compile(r"type:\s*(user|feedback|project|reference)\b")
DESC_RE = re.compile(r"^description:\s*(.+)$", re.M)


def collect():
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str]] = []
    files = [(p, "memory") for p in sorted(MEM.glob("*.md"))]
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
        sid = p.stem.strip().lower()
        nm, tp, ds = NAME_RE.search(txt), TYPE_RE.search(txt), DESC_RE.search(txt)
        group = tp.group(1) if (grp == "memory" and tp) else grp
        label = (nm.group(1).strip() if nm else p.stem).replace("_", " ")[:38]
        nodes[sid] = {"id": sid, "label": label, "group": group,
                      "desc": (ds.group(1).strip()[:120] if ds else ""), "deg": 0,
                      "_txt": txt, "_stem": p.stem.lower()}
    by_key = {}
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
    elist = [{"s": idx[a], "t": idx[b]} for a, b in edges if a in idx and b in idx]
    return fin, elist


NODES, EDGES = collect()
DATA = json.dumps({"nodes": NODES, "edges": EDGES}, ensure_ascii=False, separators=(",", ":"))
print(f"nodes={len(NODES)} edges={len(EDGES)}")

HTML = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=4">
<title>Second Brain v2 (layered) · Trading Agent</title>
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
<div class="hud" id="top"><h1>Second Brain · v2</h1><div class="sub">LAYERED NEURAL · dense mesh (mỹ thuật)</div></div>
<div class="hud" id="stat"></div>
<div class="hud" id="legend"></div>
<div class="hud" id="tip">hover: soi neuron · kéo: di chuyển · lăn: zoom</div>
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
N.forEach(n=>{n.r=4.5+Math.min(12,Math.sqrt(n.deg)*2.4);});
const sorted=[...N].sort((a,b)=>(ORDER[a.group]??9)-(ORDER[b.group]??9)||b.deg-a.deg);
const NL=8, per=Math.ceil(sorted.length/NL);
const layers=Array.from({length:NL},()=>[]);
sorted.forEach((n,i)=>{n.layer=Math.min(NL-1,Math.floor(i/per));layers[n.layer].push(n);});
const COLW=210, HSPAN=430;
layers.forEach((L,k)=>{
  const e=Math.pow(Math.sin(Math.PI*(k+0.5)/NL),0.75);
  const h=70+e*HSPAN, m=L.length, gx=(k-(NL-1)/2)*COLW;
  L.forEach((n,j)=>{const t=m>1?(j/(m-1)-0.5):0; n.gx=gx+Math.sin(t*Math.PI)*10; n.gy=t*h;});
});
const conns=[];
for(let k=0;k<NL-1;k++)for(const a of layers[k])for(const b of layers[k+1]){
  conns.push({a,b,red:((a.layer*31+b.gy*7+a.gy*3)|0)%5===0,ph:Math.random()});
}
const realpairs=new Set(E.map(e=>N[e.s].id+'|'+N[e.t].id));
const adj=N.map(()=>new Set());E.forEach(e=>{adj[e.s].add(e.t);adj[e.t].add(e.s);});
let ox=0,oy=0,scale=Math.min((innerWidth-140)/1680,(innerHeight-170)/900);
let drag=false,px,py;
cv.addEventListener('pointerdown',e=>{drag=true;px=e.clientX;py=e.clientY;});
addEventListener('pointerup',()=>drag=false);
addEventListener('pointermove',e=>{if(drag){ox+=e.clientX-px;oy+=e.clientY-py;px=e.clientX;py=e.clientY;card.style.opacity=0;}else hoverAt(e);});
cv.addEventListener('wheel',e=>{e.preventDefault();scale=Math.max(.3,Math.min(4.5,scale*(e.deltaY<0?1.12:.893)));},{passive:false});
function sx(n){return n.gx*scale+innerWidth/2+ox;}
function sy(n){return n.gy*scale+innerHeight/2+oy;}
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
  tk++;ctx.setTransform(DPR,0,0,DPR,0,0);
  const g=ctx.createRadialGradient(innerWidth*.5,innerHeight*.5,60,innerWidth*.5,innerHeight*.5,Math.max(innerWidth,innerHeight)*.72);
  g.addColorStop(0,'#0b1224');g.addColorStop(.6,'#070b16');g.addColorStop(1,'#04060c');
  ctx.fillStyle=g;ctx.fillRect(0,0,innerWidth,innerHeight);
  const hp=hover>=0;ctx.lineWidth=1;
  for(const c of conns){
    const ax=sx(c.a),ay=sy(c.a),bx=sx(c.b),by=sy(c.b);
    const lit=hp&&(c.a===N[hover]||c.b===N[hover]);
    const real=realpairs.has(c.a.id+'|'+c.b.id)||realpairs.has(c.b.id+'|'+c.a.id);
    let al=real?0.16:0.05;if(lit)al=0.5;if(hp&&!lit)al=0.02;
    ctx.strokeStyle=c.red?`rgba(248,81,73,${al})`:`rgba(63,200,150,${al})`;
    ctx.beginPath();ctx.moveTo(ax,ay);ctx.lineTo(bx,by);ctx.stroke();
    if(!hp||lit){const t=((tk*0.006+c.ph)%1);const x=ax+(bx-ax)*t,y=ay+(by-ay)*t;
      ctx.fillStyle=lit?'#d6fff0':(c.red?'rgba(255,140,130,.7)':'rgba(150,255,205,.6)');
      ctx.beginPath();ctx.arc(x,y,lit?2.2:1.3,0,7);ctx.fill();}
  }
  for(let i=0;i<N.length;i++){const n=N[i],x=sx(n),y=sy(n),c=colOf(n.group);
    const dim=hp&&i!==hover&&!adj[hover].has(i);
    ctx.shadowColor=c;ctx.shadowBlur=dim?0:(i===hover?26:9+n.r);
    ctx.fillStyle=dim?hexA(c,0.3):c;ctx.beginPath();ctx.arc(x,y,n.r*scale*(i===hover?1.5:1),0,7);ctx.fill();
    ctx.shadowBlur=0;
    if((n.deg>=4||i===hover)&&!dim&&scale>0.45){ctx.fillStyle=i===hover?'#fff':'rgba(220,228,244,.7)';
      ctx.font=(i===hover?'600 12px ':'10px ')+"ui-monospace,monospace";ctx.textAlign='center';
      ctx.fillText(n.label,x,y-n.r*scale-6);}
  }
  requestAnimationFrame(draw);
}
document.getElementById('stat').innerHTML=`<b>${N.length}</b> neurons &nbsp; <b>${conns.length}</b> synapses<br><span style="color:#556">${NL} layers · v2 layered</span>`;
const gs=[...new Set(N.map(n=>n.group))].sort((a,b)=>(ORDER[a]??9)-(ORDER[b]??9));
document.getElementById('legend').innerHTML=gs.map(g=>`<div><i style="color:${colOf(g)}"></i>${g.replace('vault-','vault · ')}</div>`).join('');
draw();
</script></body></html>"""

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(HTML.replace("__DATA__", DATA), encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size//1024} KB)")
