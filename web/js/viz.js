/* System 7 — visualización pura (sin Alpine): cartas, gráficas, rejillas, reproductor. */
const RANKS = "AKQJT98765432";

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function ch(c){if(!c)return"";const up=c.toUpperCase();const r=up.startsWith("10")?"T":up[0];const s=c.slice(-1).toLowerCase();
  const red=(s==="h"||s==="d");const su={h:"♥",d:"♦",s:"♠",c:"♣"}[s]||"";return `<span class="pc${red?" red":""}">${r}${su}</span>`;}
function chs(str){return (str||"").split(/[,\s]+/).filter(Boolean).map(ch).join("");}
function sgn(v){return v==null?'<span class="dim">—</span>':(v>0?`<span class="pos">+${v}</span>`:(v<0?`<span class="neg">${v}</span>`:"<span>0</span>"));}
function pct(x){return x==null?"—":((Math.abs(x)<=1?x*100:x).toFixed(1)+"%");}
function tt(ts){return ts?new Date(ts*1000).toLocaleTimeString():"";}
function combo(i,j){if(i===j)return RANKS[i]+RANKS[i];return i<j?RANKS[i]+RANKS[j]+"s":RANKS[j]+RANKS[i]+"o";}

function meter(label,val,max,suffix){const w=Math.max(2,Math.min(100,100*val/(max||1)));
  return `<div class="row" style="gap:8px;margin:3px 0"><span class="dim" style="width:44px">${label}</span>
    <div class="meter" style="flex:1"><i style="width:${w}%"></i><span>${val}${suffix||""}</span></div></div>`;}

function spark(arr,w=160,h=30){if(!arr||!arr.length)return"";
  const mn=Math.min(0,...arr),mx=Math.max(0,...arr),rng=(mx-mn)||1;
  const pts=arr.map((v,i)=>[(arr.length<2?0:i/(arr.length-1))*(w-4)+2,h-2-((v-mn)/rng)*(h-4)]);
  const d=pts.map((p,i)=>(i?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1)).join(" ");
  const zy=(h-2-((0-mn)/rng)*(h-4)).toFixed(1);
  return `<svg width="${w}" height="${h}"><line x1="0" y1="${zy}" x2="${w}" y2="${zy}" stroke="var(--border)"/>
    <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.5"/></svg>`;}

/* Curva equity REAL vs EV por estrategia/agente */
function equityChart(eqd, opt){
  opt = opt || {ev:true, off:{}};
  const labels = Object.keys(eqd||{}).filter(k=>(eqd[k]||[]).length>=2 && !opt.off[k]).sort();
  if(!labels.length) return '<div class="empty">sin curva todavía — lanza una evaluación en el LAB</div>';
  const PAL=["#3584e4","#2ec27e","#e5a50a","#9141ac","#e01b24","#1c71d8","#26a269"];
  const W=760,H=560,L=54,Rr=16,T=14,B=30,x0=L,x1=W-Rr,y0=T,y1=H-B;
  let xmn=Infinity,xmx=-Infinity,ymn=0,ymx=0;
  labels.forEach(l=>eqd[l].forEach(p=>{xmn=Math.min(xmn,p.h);xmx=Math.max(xmx,p.h);
    ymn=Math.min(ymn,p.raw,opt.ev?p.adj:p.raw);ymx=Math.max(ymx,p.raw,opt.ev?p.adj:p.raw);}));
  const pad=(ymx-ymn)*0.08||1; ymn-=pad; ymx+=pad; const xr=(xmx-xmn)||1,yr=(ymx-ymn)||1;
  const X=v=>x0+((v-xmn)/xr)*(x1-x0), Y=v=>y1-((v-ymn)/yr)*(y1-y0);
  const line=(s,k)=>s.map((p,i)=>(i?"L":"M")+X(p.h).toFixed(1)+" "+Y(p[k]).toFixed(1)).join(" ");
  let grid="",ax="";
  for(let i=0;i<=4;i++){const v=ymn+yr*i/4,yy=Y(v).toFixed(1);
    grid+=`<line x1="${x0}" y1="${yy}" x2="${x1}" y2="${yy}" stroke="var(--border)"/>`;
    ax+=`<text x="${x0-8}" y="${+yy+4}" text-anchor="end" font-size="11" fill="var(--dim)">${Math.round(v)}</text>`;}
  for(let i=0;i<=4;i++){const hv=xmn+xr*i/4,xx=X(hv).toFixed(1);
    ax+=`<text x="${xx}" y="${H-8}" text-anchor="middle" font-size="11" fill="var(--dim)">${Math.round(hv)}</text>`;}
  const zy=Y(0).toFixed(1); let paths="",leg="";
  labels.forEach((l,i)=>{const c=PAL[i%PAL.length],s=eqd[l],last=s[s.length-1];
    if(opt.ev) paths+=`<path d="${line(s,"adj")}" fill="none" stroke="${c}" stroke-width="1.5" stroke-dasharray="5 4" opacity=".6" vector-effect="non-scaling-stroke"/>`;
    paths+=`<path d="${line(s,"raw")}" fill="none" stroke="${c}" stroke-width="2.4" vector-effect="non-scaling-stroke"/>`;
    paths+=`<circle cx="${X(last.h).toFixed(1)}" cy="${Y(last.raw).toFixed(1)}" r="4" fill="${c}"/>`;
    leg+=`<span style="color:${c};font-weight:700">${esc(l)}: ${last.raw}${opt.ev?(" · EV "+last.adj):""}</span>`;});
  return `<div style="display:flex;flex-direction:column;flex:1;min-height:240px">
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;flex:1;min-height:0;display:block">${grid}${ax}
    <line x1="${x0}" y1="${zy}" x2="${x1}" y2="${zy}" stroke="var(--dim)" stroke-width="1.3" vector-effect="non-scaling-stroke"/>${paths}</svg>
    <div class="row" style="gap:16px;margin-top:6px;font-size:12px">${leg}<span class="dim">sólido REAL${opt.ev?" · discont. EV":""}</span></div></div>`;
}

/* Heatmap 13×13 (VPIP) para el panel */
function heatGrid(classes){
  let g="";
  for(let i=0;i<13;i++)for(let j=0;j<13;j++){
    const cl=combo(i,j), c=classes[cl], v=c?c.vpip:0;
    const L=v>0?(22+v*0.45):(getCSSdark()?14:96), bg=v>0?`hsl(213 75% ${L}%)`:"var(--bg)";
    const fg=v>55?"#fff":"var(--dim)";
    g+=`<div class="heat" title="${cl}  VPIP ${v}%  PFR ${c?c.pfr:0}%  n=${c?c.n:0}" style="background:${bg};color:${fg}">${cl}</div>`;
  }
  return g;
}
// rango de showdown de un rival: classes[hand_class] = nº de veces mostrada
function showdownGrid(classes){
  const mx=Math.max(1,...Object.values(classes||{}));
  let g="";
  for(let i=0;i<13;i++)for(let j=0;j<13;j++){
    const cl=combo(i,j), n=(classes&&classes[cl])||0;
    const L=n>0?(58-32*n/mx):(getCSSdark()?14:96), bg=n>0?`hsl(145 58% ${L}%)`:"var(--bg)";
    const fg=(n>0&&n/mx>0.5)?"#fff":"var(--dim)";
    g+=`<div class="heat" title="${cl}${n?(" · mostrada "+n+"x"):""}" style="background:${bg};color:${fg}">${cl}</div>`;
  }
  return g;
}
function getCSSdark(){return window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches;}

/* Rejilla 13×13 clicable para el builder. selected = Set de combos. */
function rangeGrid(pos, selected, ctx){
  ctx = ctx || "cash";
  let rows="";
  for(let i=0;i<13;i++){rows+="<tr>";for(let j=0;j<13;j++){const cmb=combo(i,j),on=selected.has(cmb);
    rows+=`<td class="${i===j?"pair":""}${on?" on":""}" data-ctx="${ctx}" data-pos="${pos}" data-cmb="${cmb}">${cmb.slice(0,2)}</td>`;}rows+="</tr>";}
  return `<div class="card" style="padding:8px"><div class="row" style="justify-content:space-between;margin-bottom:4px">
    <b>${pos}</b><span class="dim" data-cnt="${ctx}-${pos}">${selected.size}</span></div>
    <table class="rangegrid"><tbody>${rows}</tbody></table></div>`;
}

/* ───────── Reproductor de mano ───────── */
function tstate(evs, step){
  const seats={}, order=[]; let base=0, board=[], curMax=0, acting=null, street="preflop"; const blinds=[];
  const see=sn=>{if(!(sn in seats)){seats[sn]={name:null,bet:0,inv:0,folded:false,action:null};order.push(sn);}return seats[sn];};
  for(let i=0;i<=step&&i<evs.length;i++){const e=evs[i],s=e.summary||{},sn=s.seatNumber;
    if(sn==null && e.type!=="StreetDealt") continue;
    if(e.type==="BlindPosted"){const o=see(sn);o.name=o.name||s.agentName;const a=s.amount||0;o.inv+=Math.max(0,a-o.bet);o.bet=a;curMax=Math.max(curMax,a);blinds.push({seat:sn,amount:a});}
    else if(e.type==="ActionTaken"){const o=see(sn);o.name=s.agentName||o.name;o.action=s.action;acting=sn;street=e.street||street;
      if(s.action==="fold")o.folded=true;
      else if(s.action==="check"){}
      else if(s.action==="call"){const a=Math.max(curMax,s.amount||0);o.inv+=Math.max(0,a-o.bet);o.bet=a;curMax=Math.max(curMax,a);}
      else{const a=(s.toAmount!=null?s.toAmount:(s.amount!=null?s.amount:o.bet));o.inv+=Math.max(0,a-o.bet);o.bet=a;curMax=Math.max(curMax,o.bet);}}
    else if(e.type==="StreetDealt"){for(const k in seats){base+=seats[k].bet||0;seats[k].bet=0;}curMax=0;if(s.boardCards)board=s.boardCards;street=e.street||street;}
  }
  let pot=base; for(const k in seats)pot+=seats[k].bet||0; order.sort((a,b)=>a-b);
  let sb=null,bb=null,btn=null;
  if(blinds.length){sb=blinds[0].seat;bb=blinds.length>1?blinds[1].seat:null;const si=order.indexOf(sb);if(si>=0)btn=order[(si-1+order.length)%order.length];if(order.length===2)btn=sb;}
  return {seats,order,pot,board,acting,street,sb,bb,btn};
}

function buildTimeline(h){
  const my=h.seat, nrm=x=>{x=String(x||"preflop").toLowerCase();return x==="predeal"?"preflop":x;};
  let ev=(h.events||[]).filter(e=>e.type&&e.type!=="Joined"&&e.type!=="TableStarted"&&e.type!=="HoleCardsDealt");
  ev.sort((a,b)=>((a.sequence==null?0:a.sequence)-(b.sequence==null?0:b.sequence)));
  const have={}; ev.forEach(e=>{if(e.type==="ActionTaken"&&e.summary&&e.summary.seatNumber===my)have[nrm(e.street)]=true;});
  (h.decisions||[]).forEach(d=>{const st=nrm(d.street);if(have[st])return;have[st]=true;
    let pos=-1;for(let i=0;i<ev.length;i++)if(nrm(ev[i].street)===st)pos=i;
    const agg=(d.action==="bet"||d.action==="raise"||d.action==="all-in");
    const node={type:"ActionTaken",street:d.street,sequence:(pos>=0&&ev[pos].sequence!=null?ev[pos].sequence+0.5:1e9),
      summary:{seatNumber:my,agentName:"S7",action:d.action,amount:(d.action==="call"?d.amount:null),toAmount:(agg?d.amount:null)}};
    if(pos>=0)ev.splice(pos+1,0,node);else ev.push(node);});
  return ev;
}

function minitable(h, evs, step){
  const my=h.seat, isResult=step>=evs.length, est=Math.min(step,evs.length-1);
  const st=tstate(evs,est), full=tstate(evs,evs.length-1);
  const chips={}, nameBy={};
  (h.seats||[]).forEach(s=>{if(s&&s.seat!=null){chips[s.seat]=s.chips;if(s.name)nameBy[s.seat]=s.name;}});
  for(const k in full.seats){if(nameBy[k]==null&&full.seats[k].name)nameBy[k]=full.seats[k].name;}
  const set=new Set(); (h.seats||[]).forEach(s=>{if(s&&s.seat!=null)set.add(+s.seat);}); full.order.forEach(x=>set.add(+x));
  const reveal=isResult&&h.result&&(h.result.seats_shown||[]).length;
  if(reveal)(h.result.seats_shown||[]).forEach(s=>{if(s.seat!=null){set.add(+s.seat);if(nameBy[s.seat]==null&&s.name)nameBy[s.seat]=s.name;}});
  const order=[...set].sort((a,b)=>a-b), n=order.length||1; if(!order.length)return"";
  const stackOf=sn=>{if(chips[sn]==null)return null;const S=chips[sn]+((full.seats[sn]&&full.seats[sn].inv)||0);return Math.max(0,Math.round(S-((st.seats[sn]&&st.seats[sn].inv)||0)));};
  const mi=Math.max(0,order.indexOf(my)),cx=50,cy=50,rx=39,ry=36;
  const xy=sn=>{const k=order.indexOf(sn),rel=((k-mi)+n)%n,a=(90+rel*360/n)*Math.PI/180;return [cx+rx*Math.cos(a),cy+ry*Math.sin(a)];};
  let bcards=st.board.length?st.board:(h.board||"").split(/[,\s]+/).filter(Boolean);
  if(reveal&&h.result.board){const rb=h.result.board.split(/[,\s]+/).filter(Boolean);if(rb.length>bcards.length)bcards=rb;}
  let eff=null;order.forEach(sn=>{const o=st.seats[sn]||{};if(!o.folded){const c=stackOf(sn);if(c!=null)eff=(eff==null)?c:Math.min(eff,c);}});
  const spr=(eff!=null&&st.pot>0)?(eff/st.pot).toFixed(1):"—";
  const shownBy={}; if(reveal)(h.result.seats_shown||[]).forEach(s=>{if(s.seat!=null)shownBy[s.seat]=s.hole;});
  const winSeats=new Set(((h.result&&h.result.winners)||[]).map(w=>w.seatNumber));
  const winAmt={}, payout={}; let potEnd=0;
  ((h.result&&h.result.seats_shown)||[]).forEach(s=>{if(s.seat!=null){payout[s.seat]=s.payout||0;potEnd+=(s.payout||0);}});
  ((h.result&&h.result.winners)||[]).forEach(w=>{if(w&&w.seatNumber!=null)winAmt[w.seatNumber]=w.amount;});
  const potShown=(reveal&&potEnd)?potEnd:st.pot;
  let html=`<div class="felt"><div class="center"><div>${chs(bcards.join(","))||'<span style="opacity:.5">— preflop —</span>'}</div>
    <div class="pot">☷ BOTE ${potShown} · SPR ${spr} · ${reveal?(h.endStreet||"showdown"):(st.street||"").toLowerCase()}</div></div>`;
  order.forEach(sn=>{const p=xy(sn),o=st.seats[sn]||{},mine=(sn===my);
    const isWin=reveal&&(winSeats.has(sn)||payout[sn]>0), ef=reveal?!isWin:o.folded;
    let badge=""; if(sn===st.sb)badge="SB"; else if(sn===st.bb)badge="BB";
    const name=mine?"TÚ":(nameBy[sn]?String(nameBy[sn]).slice(0,9):("as."+sn));
    const cur=stackOf(sn), stk=cur!=null?`<div class="stk">● ${cur}</div>`:"";
    let cards;
    if(mine)cards=`<div>${chs(h.hole)}</div>`;
    else if(shownBy[sn]&&shownBy[sn].length)cards=`<div>${chs(shownBy[sn].join(","))}</div>`;
    else cards=`<div>${ef?'<span class="dim" style="font-size:9px">fold</span>':'<span class="cb"></span><span class="cb"></span>'}</div>`;
    const act=reveal?(isWin?`<span class="pos">+${winAmt[sn]!=null?winAmt[sn]:payout[sn]}</span>`:'<span class="dim">fold</span>'):(o.folded?"":(o.action||"")+(o.bet&&!o.folded?(' <span class="dim">'+o.bet+'</span>'):""));
    html+=`<div class="seat${ef?" fold":""}${!reveal&&sn===st.acting?" act":""}${mine?" me":""}${isWin?" win":""}" style="left:${p[0]}%;top:${p[1]}%">
      <div class="name">${esc(name)}${badge?`<span class="badge">${badge}</span>`:""}</div>${stk}${cards}<div class="dim" style="font-size:10px;min-height:12px">${act}</div></div>`;
  });
  if(st.btn!=null){const p=xy(st.btn),dx=p[0]+(cx-p[0])*0.3,dy=p[1]+(cy-p[1])*0.3;html+=`<div class="dealer" style="left:${dx}%;top:${dy}%">D</div>`;}
  return html+"</div>";
}

function streetSections(h, evs){
  const my=h.seat, nrm=x=>{x=(x||"preflop").toLowerCase();return x==="predeal"?"preflop":x;};
  const decBy={}; (h.decisions||[]).forEach(d=>{(decBy[nrm(d.street)]=decBy[nrm(d.street)]||[]).push(d);});
  const amtOf=s=>{if(s.action==="bet"||s.action==="raise"||s.action==="all-in")return s.toAmount!=null?s.toAmount:s.amount;if(s.action==="call")return s.amount;return null;};
  let out="";
  ["preflop","flop","turn","river"].forEach(stn=>{
    const acts=evs.filter(e=>nrm(e.street)===stn&&(e.type==="ActionTaken"||e.type==="BlindPosted"));
    const dec=decBy[stn]||[]; if(!acts.length&&!dec.length)return;
    const body=acts.map(e=>{const s=e.summary||{},mine=s.seatNumber!=null&&s.seatNumber===my,am=amtOf(s);
      const who=mine?"TÚ":("as."+s.seatNumber+(s.agentName?" "+esc(String(s.agentName).slice(0,10)):""));
      const a=e.type==="BlindPosted"?("ciega "+s.amount):(esc(s.action||"")+(am!=null?` <b>${am}</b>`:""));
      return `<div style="padding:2px 0;${mine?"color:var(--green)":""}">${who} · ${a}</div>`;}).join("");
    const reads=dec.map(d=>{let m3="";if(d.m3)m3=`<div class="mono dim" style="font-size:11px;white-space:pre-wrap;margin-top:3px">M3(${esc(d.m3.model||"")}): ${esc((d.m3.answer||"").slice(0,300))}</div>`;
      return `<div class="dim" style="font-size:12px;padding:2px 0">▷ nuestra <b>${esc(d.action||"")}</b>${d.amount?(" "+d.amount):""} · ${d.strength||"?"} · SPR ${d.spr}${d.outs?(" · "+d.outs+" outs"):""}${d.engine==="M3"?' <span class="pill amber">M3</span>':""}</div>${m3}`;}).join("");
    out+=`<div class="card" style="margin-top:8px"><h3>${stn}</h3><div class="body tight">${body}${reads}</div></div>`;
  });
  return out;
}

/* Razonamiento del LLM (M3): answer + think completos por decisión, desplegable. */
function m3Block(h){
  const ds=(h.decisions||[]).filter(d=>d.m3);
  if(!ds.length) return "";
  const items=ds.map(d=>`<details style="margin:4px 0"><summary style="cursor:pointer;color:var(--purple);font-weight:600">🤖 ${esc(d.street||"")} · ${esc(d.action||"")}${d.amount?(" "+d.amount):""} <span class="dim" style="font-weight:400">(${esc(d.m3.model||"M3")})</span></summary><div class="mono" style="font-size:11px;white-space:pre-wrap;background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:8px;margin-top:4px"><b>📤 enviado a la LLM:</b>\n${esc(d.m3.sent||"(no guardado — solo en manos nuevas)")}\n\n<b>📥 respuesta de la LLM:</b>\n${esc(d.m3.answer||"(vacío)")}\n\n<b>🧠 think:</b>\n${esc(d.m3.think||"(sin think)")}</div></details>`).join("");
  return `<div class="card" style="margin-top:8px;border-color:var(--purple)"><h3>🤖 Razonamiento del LLM · ${ds.length} decisión(es) con M3</h3><div class="body">${items}</div></div>`;
}

function showdownBlock(h){
  const r=h.result; if(!r)return"";
  const w=(r.winners&&r.winners[0])||null, wn=w?(w.agentName||w.agentId||"?"):"—";
  const wa=w&&w.amount!=null?` <b class="pos">+${w.amount}</b>`:"", wh=w&&w.handName?(" con "+esc(w.handName)):"";
  const d=r.chip_delta, ds=(d==null)?"":` · tú <b class="${d>=0?"pos":"neg"}">${d>=0?"+":""}${d}</b>`;
  const rev=(r.seats_shown||[]).map(s=>`<span style="margin-right:12px">${s.name==="S7 test"?"TÚ":esc(String(s.name||("as."+s.seat)).slice(0,12))} ${chs((s.hole||[]).join(","))}${s.hand?` <span class="dim">${esc(s.hand)}</span>`:""}</span>`).join("");
  return `<div class="card" style="margin-bottom:8px;border-color:var(--yellow)"><div class="body">🏆 <b>${esc(wn)}</b>${wa}${wh}${ds}
    ${r.board?` · ${chs(r.board)}`:""}${rev?`<div class="dim" style="margin-top:6px">${rev}</div>`:""}</div></div>`;
}

/* Campana de Gauss: histograma de las muestras + curva normal ajustada (media/σ). */
function gaussChart(samples, label){
  const s=(samples||[]).filter(x=>x!=null);
  if(s.length<2) return '<div class="empty">distribución: hacen falta ≥2 evaluaciones completadas (500 manos c/u)</div>';
  const n=s.length, mean=s.reduce((a,b)=>a+b,0)/n, sd=Math.sqrt(s.reduce((a,b)=>a+(b-mean)*(b-mean),0)/(n-1))||1;
  const mn=Math.min(...s), mx=Math.max(...s), pad=(mx-mn)*0.2||10, lo=mn-pad, hi=mx+pad;
  const W=560,H=210,R=12,T=12,B=26,x0=34,x1=W-R,y0=T,y1=H-B;
  const bins=Math.min(14,Math.max(5,Math.round(Math.sqrt(n)))), bw=(hi-lo)/bins;
  const hist=new Array(bins).fill(0); s.forEach(v=>{const i=Math.min(bins-1,Math.max(0,Math.floor((v-lo)/bw)));hist[i]++;});
  const maxc=Math.max(...hist,1), X=v=>x0+((v-lo)/(hi-lo))*(x1-x0);
  let bars="";
  for(let i=0;i<bins;i++){const bx=X(lo+i*bw),bx2=X(lo+(i+1)*bw),bh=(hist[i]/maxc)*(y1-y0);
    bars+=`<rect x="${(bx+1).toFixed(1)}" y="${(y1-bh).toFixed(1)}" width="${Math.max(1,bx2-bx-2).toFixed(1)}" height="${bh.toFixed(1)}" fill="var(--accent)" opacity="0.45"/>`;}
  const peak=1/(sd*Math.sqrt(2*Math.PI)); let path="";
  for(let i=0;i<=80;i++){const v=lo+(hi-lo)*i/80, y=peak*Math.exp(-((v-mean)*(v-mean))/(2*sd*sd));
    path+=(i?"L":"M")+X(v).toFixed(1)+" "+(y1-(y/peak)*(y1-y0)).toFixed(1)+" ";}
  const mxn=X(mean).toFixed(1), zx=(lo<=0&&hi>=0)?X(0).toFixed(1):null;
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${bars}
    <path d="${path}" fill="none" stroke="var(--purple)" stroke-width="2"/>
    ${zx?`<line x1="${zx}" y1="${y0}" x2="${zx}" y2="${y1}" stroke="var(--border)"/>`:""}
    <line x1="${mxn}" y1="${y0}" x2="${mxn}" y2="${y1}" stroke="var(--green)" stroke-dasharray="4 3"/>
    <text x="${x0}" y="${H-6}" font-size="11" fill="var(--dim)">${Math.round(lo)}</text>
    <text x="${x1}" y="${H-6}" text-anchor="end" font-size="11" fill="var(--dim)">${Math.round(hi)}</text></svg>
    <div class="dim" style="font-size:12px">${label||"bb/100"} · media <b class="${mean>=0?"pos":"neg"}">${mean>=0?"+":""}${mean.toFixed(1)}</b> · σ ${sd.toFixed(1)} · n ${n}</div>`;
}

/* Decisiones postflop por calle: barra apilada bet/call/check/fold + leyenda. */
function postflopBars(post){
  const cols={bet:"var(--green)",call:"var(--accent)",check:"var(--dim)",fold:"var(--red)"};
  const lab={bet:"apuesta/sube",call:"iguala",check:"pasa",fold:"folda"};
  let out="";
  ["flop","turn","river"].forEach(s=>{
    const d=post[s]||{}, tot=(d.bet||0)+(d.call||0)+(d.check||0)+(d.fold||0);
    if(!tot){ out+=`<div class="row" style="align-items:center;gap:6px;margin:3px 0"><span style="width:42px" class="dim">${s}</span><span class="dim">—</span></div>`; return; }
    let bar="";
    ["bet","call","check","fold"].forEach(k=>{const p=100*(d[k]||0)/tot; if(p>0) bar+=`<span title="${lab[k]} ${Math.round(p)}%" style="display:inline-block;height:14px;width:${p}%;background:${cols[k]}"></span>`;});
    out+=`<div class="row" style="align-items:center;gap:6px;margin:3px 0"><span style="width:42px" class="dim">${s}</span><span style="flex:1;display:flex;border-radius:4px;overflow:hidden;border:1px solid var(--border)">${bar}</span><span class="dim" style="width:40px;text-align:right">${tot}</span></div>`;
  });
  out+=`<div class="dim" style="font-size:12px;margin-top:6px">`+Object.keys(lab).map(k=>`<span style="margin-right:10px"><span style="display:inline-block;width:9px;height:9px;background:${cols[k]};border-radius:2px;vertical-align:middle"></span> ${lab[k]}</span>`).join("")+`</div>`;
  return out;
}
