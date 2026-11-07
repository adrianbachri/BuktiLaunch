'use strict';
const data = window.BUKTI_DATA;
const $ = (id) => document.getElementById(id);
function el(tag, text, className) { const n = document.createElement(tag); if (text !== undefined) n.textContent=text; if(className) n.className=className; return n; }
if (!data) { $('status').textContent='Data belum tersedia'; throw new Error('Run tools/build_review_snapshot.py first'); }
$('campaign').textContent=data.campaign;
$('status').textContent=data.historical_issue?'Perlu koreksi review':(data.status==='READY_FOR_APPROVAL'?'Menunggu persetujuan':data.status);
$('round').textContent=`Putaran ${data.round} dari ${data.max_rounds}`;
$('historical-alert').hidden=!data.historical_issue;
$('format-count').textContent=`${data.outputs.length} format`;
const tabButtons=[];
function selectOutput(i) { const out=data.outputs[i]; $('copy').textContent=out.text; $('file-path').textContent=out.file; $('file-hash').textContent=out.sha256; tabButtons.forEach((b,j)=>{b.setAttribute('aria-selected',String(i===j));b.tabIndex=i===j?0:-1;}); $('content-panel').setAttribute('aria-labelledby',`format-${i}`); }
data.outputs.forEach((out,i)=>{const file=out.file.toLowerCase();const name=file.includes('caption')?'Caption Instagram':file.includes('landing')?'Copy landing page':file.includes('listing')?'Listing produk digital':file.includes('video')||file.includes('script')?'Skrip video 30 detik':out.file; const b=el('button',name); b.id=`format-${i}`;b.setAttribute('role','tab');b.setAttribute('aria-controls','content-panel');b.addEventListener('click',()=>selectOutput(i));b.addEventListener('keydown',(e)=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const j=e.key==='Home'?0:e.key==='End'?data.outputs.length-1:(i+(e.key==='ArrowRight'?1:-1)+data.outputs.length)%data.outputs.length;selectOutput(j);tabButtons[j].focus();});$('tabs').append(b);tabButtons.push(b);});
selectOutput(0);
data.sources.forEach((source)=>{const d=el('details');d.append(el('summary',source.file.replace('inputs/','')));d.append(el('pre',source.text));$('sources').append(d);});
const first=data.reviews.find(r=>r.payload.verdict==='REVISE') || data.reviews[0];
const findings=first?.payload.findings || first?.payload.issues || [];
$('finding-count').textContent=`${findings.length} temuan`;
findings.forEach((f,i)=>{const article=el('article',undefined,'finding');article.append(el('span',String(i+1).padStart(2,'0'),'finding-index'));const body=el('div');body.append(el('h3',f.claim || f.text));body.append(el('span',`${f.rule || f.ruleID} · ${f.severity}`,'rule'));const d=el('details');d.append(el('summary','Baca alasan & sumber'));d.append(el('p',f.why || f.reason));d.append(el('p',f.source_citation || f.rule_reference,'citation'));d.append(el('p',`Perbaikan: ${f.remediation || f.required_action || f.fix_suggestion || ''}`));body.append(d);article.append(body);$('finding-list').append(article);});
data.history.forEach(e=>{const row=el('tr');[e.at,e.event,e.round,e.status].forEach(v=>row.append(el('td',String(v))));$('events').append(row);});
