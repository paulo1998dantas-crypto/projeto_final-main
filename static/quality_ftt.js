"use strict";
const $=id=>document.getElementById(id);
const labels={"PREP":"Preparação","EXPE":"Expedição","SERRA":"Serralheria","VIDROS":"Vidros","A/C":"Ar-condicionado","DESMONT":"Desmontagem","REVEST":"Revestimento","ELÉTRICA":"Elétrica","BCO":"Bancos","ACESSÓRIO":"Acessório","PLOTAGEM":"Plotagem"};
const canWrite=document.body.dataset.write==="true";
let catalog={defects:[],stages:[],work_orders:[]},key=crypto.randomUUID(),targetSelect=null,searchSeq=0,reportSeq=0;
let historySearchSeq=0,daySeq=0,workSeq=0,correction=null,reportMutation=null;
const historyRecords=new Map();
const escapeHTML=value=>{const span=document.createElement("span");span.textContent=String(value??"");return span.innerHTML;};
const cell=(v,cls="")=>'<td class="'+cls+'">'+escapeHTML(v)+'</td>';
const number=v=>v===null||v===undefined?"—":Number(v).toLocaleString("pt-BR",{maximumFractionDigits:2});
const pct=v=>v===null?"Sem base":number(v)+"%";
const today=new Intl.DateTimeFormat("en-CA",{timeZone:"America/Sao_Paulo",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
const dateBR=value=>value?String(value).slice(0,10).split("-").reverse().join("/"):"—";
const timeBR=value=>{const iso=String(value||"");return iso?new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(iso)?iso:iso+"Z").toLocaleString("pt-BR",{timeZone:"America/Sao_Paulo"}):"—";};
function message(text,error=false){$("message").hidden=false;$("message").className=error?"error":"";$("message").textContent=text;}
async function api(url,data){
 const response=await fetch(url,{method:data?"POST":"GET",headers:data?{"Content-Type":"application/json","X-FTT-Request":"1"}:{},body:data?JSON.stringify(data):undefined,cache:"no-store"});
 let result;try{result=await response.json();}catch{throw Error("O servidor não retornou uma resposta válida. Confira o histórico antes de repetir.");}
 if(!response.ok||!result.ok)throw Error(result.error||"Não foi possível concluir.");
 return result;
}
function options(list,value,label){return '<option value="">Selecione</option>'+list.map(x=>'<option value="'+escapeHTML(value(x))+'">'+escapeHTML(label(x))+'</option>').join("");}
function fillDefects(){document.querySelectorAll(".defect-select").forEach(s=>{const old=s.value;s.innerHTML=options(catalog.defects,x=>x.id,x=>x.description);s.value=old;});}
function addIssue(){
 const row=document.createElement("div");row.className="line issue-line";
 row.innerHTML='<label class="defect">Descrição do defeito<select required class="defect-select"></select><button type="button" class="add-defect" aria-label="Cadastrar nova descrição de defeito">+</button></label><label>Área responsável<select required class="area-select">'+options(catalog.stages,x=>x,x=>labels[x]||x)+'</select></label><button type="button" class="remove">Remover</button>';
 row.querySelector(".defect-select").innerHTML=options(catalog.defects,x=>x.id,x=>x.description);
 row.querySelector(".add-defect").onclick=()=>{targetSelect=row.querySelector(".defect-select");$("defect-dialog").showModal();$("new-description").focus();};
 row.querySelector(".remove").onclick=()=>{if(document.querySelectorAll(".issue-line").length>1)row.remove();};
 $("issue-lines").append(row);
}
function query(){return new URLSearchParams({from:$("filter-from").value,to:$("filter-to").value});}
function isoDate(value){return value.toISOString().slice(0,10);}
function setWeek(value){
 if(!/^\d{4}-W\d{2}$/.test(value||""))return;
 const [year,week]=value.split("-W").map(Number);
 const jan4=new Date(Date.UTC(year,0,4));
 const monday=new Date(jan4);monday.setUTCDate(jan4.getUTCDate()-((jan4.getUTCDay()+6)%7)+(week-1)*7);
 const sunday=new Date(monday);sunday.setUTCDate(monday.getUTCDate()+6);
 const floor=new Date(Date.UTC(2026,9,26));
 const ceiling=new Date(today+"T00:00:00Z");
 $("filter-from").value=isoDate(monday<floor?floor:monday);
 $("filter-to").value=isoDate(sunday>ceiling?ceiling:sunday);
}
async function refresh(){
 try{const seq=++reportSeq;const data=await api("/api/ftt/quality?"+query());if(seq!==reportSeq)return;
 $("k-failures").textContent=number(data.total_failures);$("k-audited").textContent=number(data.vehicles.audited);$("k-defective").textContent=number(data.vehicles.defective);$("k-ftt").textContent=pct(data.vehicles.ftt);
 $("pareto").innerHTML=data.pareto.map(r=>"<tr>"+cell(r.rank)+cell(r.description)+cell(labels[r.area]||r.area)+cell(r.frequency,"num")+cell(pct(r.accumulated),"num")+"</tr>").join("")||'<tr><td colspan="5">Sem ocorrências neste período.</td></tr>';
 $("pareto-total").textContent=number(data.total_failures);
 $("areas").innerHTML=data.areas.map(r=>"<tr>"+cell(labels[r.area]||r.area)+["produced","audited","defective","first_pass"].map(k=>cell(number(r[k]),"num")).join("")+cell(pct(r.ftt),"num")+cell(r.status,"status "+(r.ftt===null?"":r.ftt>=95?"good":r.ftt>=90?"warn":"bad"))+"</tr>").join("");
 $("area-total").innerHTML="<tr>"+cell("TOTAL / PASSAGENS")+["produced","audited","defective","first_pass"].map(k=>cell(number(data.totals[k]),"num")).join("")+cell(pct(data.totals.ftt),"num")+cell("Média ponderada")+"</tr>";
 $("pending").textContent=data.pending+" ocorrência(s) do período sem etapas aplicáveis identificadas na O.S. Confira a parametrização do MES.";
 $("export").href="/api/ftt/export.xlsx?"+query();
 }catch(e){message(e.message,true);}
}
async function loadOptions(q=""){
 const seq=++searchSeq;
 const result=await api("/api/ftt/options?q="+encodeURIComponent(q));
 if(seq!==searchSeq)return;
 catalog=result;
 if(canWrite){const selected=$("work-order").value;const previous=$("work-order").selectedOptions[0]?.cloneNode(true);$("work-order").innerHTML=options(catalog.work_orders,x=>x.id,x=>"O.S. "+x.numero_os+" · Item "+x.item_number+" · "+x.chassi);if(selected&&!catalog.work_orders.some(x=>x.id===selected)&&previous)$("work-order").append(previous);$("work-order").value=selected;}
}
const workLabel=x=>"O.S. "+x.numero_os+" · Item "+x.item_number+" · "+x.chassi;
async function searchHistory(q=""){
 const seq=++historySearchSeq,result=await api("/api/ftt/options?history=1&q="+encodeURIComponent(q));
 if(seq!==historySearchSeq)return;
 const select=$("history-work"),old=select.value,previous=select.selectedOptions[0]?.cloneNode(true);
 select.innerHTML=options(result.work_orders,x=>x.id,workLabel);
 if(old&&!result.work_orders.some(x=>x.id===old)&&previous)select.append(previous);
 select.value=old;
}
function renderHistory(target,data){
 const container=$(target),open=new Set([...container.querySelectorAll("details[open]")].map(x=>x.dataset.report));
 container.innerHTML=data.reports.map(r=>{
  historyRecords.set(r.id,r);
  const title="O.S. "+r.numero_os+" · Item "+r.item_number+" · "+r.chassi+" · "+dateBR(r.observed_on);
  const verdict=r.voided?"EXCLUÍDO · não conta nos indicadores":r.audit_complete?(r.issues.length?"Auditoria com defeitos":"Auditoria sem defeitos"):"Histórico de defeitos · sem auditoria completa";
  const issueRows=r.issues.map(i=>"<tr>"+cell(i.description)+cell(labels[i.stage_code]||i.stage_code)+
   (canWrite?'<td>'+(!r.voided?'<button type="button" data-correct-report="'+escapeHTML(r.id)+'" data-issue="'+escapeHTML(i.id)+'">Corrigir área</button>':"—")+'</td>':"")+"</tr>").join("");
  const changes=r.corrections.map(c=>"<li>"+escapeHTML((r.issues.find(i=>i.id===c.issue_id)?.description||"Defeito")+": "+(labels[c.from_stage]||c.from_stage)+" → "+(labels[c.to_stage]||c.to_stage)+" · "+c.reason+" · "+c.actor+" · "+timeBR(c.created_at))+"</li>").join("");
  return '<details data-report="'+escapeHTML(r.id)+'"'+(open.has(r.id)?" open":"")+'><summary style="padding:12px;cursor:pointer;border-bottom:1px solid #e2e8f0">'+escapeHTML(title)+" — "+escapeHTML(verdict)+"</summary><p class=\"note\">Registrado por "+escapeHTML(r.actor)+" · "+escapeHTML(timeBR(r.created_at))+" · Revisão de correção "+number(r.version-1)+"</p>"+
   (r.audit_complete?'<p class="note">Áreas auditadas: '+escapeHTML(r.areas.map(c=>labels[c]||c).join(" · "))+'</p>':"")+
   (issueRows?'<div class="table-wrap"><table><thead><tr><th>Defeito</th><th>Área responsável</th>'+(canWrite?"<th>Ação</th>":"")+"</tr></thead><tbody>"+issueRows+"</tbody></table></div>":'<p class="good">Nenhum defeito registrado nesta auditoria.</p>')+
   (canWrite&&!r.voided?'<p class="toolbar"><button type="button" data-edit-report="'+escapeHTML(r.id)+'">Editar apontamento</button><button type="button" class="bad" data-delete-report="'+escapeHTML(r.id)+'">Excluir apontamento</button></p>':"")+
   (changes?'<h3 class="sr">Histórico de correções</h3><ul>'+changes+'</ul>':"")+
   (r.events?.length?'<h3 class="sr">Edições e exclusões auditadas</h3>'+r.events.map(e=>'<details><summary>'+escapeHTML((e.action==="DELETE"?"Exclusão":"Edição")+" · "+e.actor+" · "+timeBR(e.created_at)+" · "+e.reason)+'</summary><p class="note">Antes: '+escapeHTML(snapshotLabel(e.before_data))+'</p><p class="note">Depois: '+escapeHTML(snapshotLabel(e.after_data))+'</p></details>').join(""):"")+"</details>";
 }).join("")||'<p class="note">Nenhum apontamento encontrado.</p>';
}
async function loadDay(){
 const seq=++daySeq,value=$("history-day").value;if(!value)return;
 const data=await api("/api/ftt/history?day="+encodeURIComponent(value)+($("include-voided").checked?"&include_voided=1":""));if(seq!==daySeq)return;
 renderHistory("day-history",data);$("day-summary").textContent=dateBR(value)+" · "+number(data.vehicles)+" veículo(s) · "+number(data.reports.length)+" apontamento(s), incluindo reinspeções e histórico de defeitos.";
}
async function loadWork(){
 const seq=++workSeq,work=$("history-work").value;
 if(!work){$("work-history").innerHTML='<p class="note">Selecione uma O.S. para consultar seu histórico.</p>';return;}
 const data=await api("/api/ftt/history?work_order_id="+encodeURIComponent(work)+($("include-voided").checked?"&include_voided=1":""));if(seq!==workSeq)return;renderHistory("work-history",data);
}
function snapshotLabel(data){return dateBR(data.observed_on)+" · "+(data.voided?"Excluído":data.audit_complete?"Auditoria completa":"Histórico de defeitos")+" · "+(data.issues.map(i=>i.description+" ("+(labels[i.stage_code]||i.stage_code)+")").join("; ")||"Sem defeitos");}
function addEditIssue(issue={}){
 const row=document.createElement("div");row.className="line edit-issue";row.dataset.id=issue.id||"";
 row.innerHTML='<label>Descrição do defeito<select class="edit-defect" required>'+options(catalog.defects,x=>x.id,x=>x.description)+'</select></label><label>Setor responsável<select class="edit-area" required>'+options(reportMutation.areas,c=>c,c=>labels[c]||c)+'</select></label><button type="button">Remover linha</button>';
 row.querySelector('.edit-defect').value=issue.defect_id||"";row.querySelector('.edit-area').value=issue.stage_code||"";
 row.querySelector('button').onclick=()=>row.remove();$("edit-issues").append(row);
}
async function refreshAll(){await Promise.all([refresh(),loadDay(),loadWork()]);}
function handleError(fn){return ()=>Promise.resolve().then(fn).catch(e=>message(e.message,true));}
$("filter-to").value=today<"2026-10-26"?"2026-10-26":today;
$("history-day").value=$("filter-to").value;$("history-day").max=$("filter-to").value;
$("history-day").onchange=handleError(loadDay);$("history-work").onchange=handleError(loadWork);
$("include-voided").onchange=handleError(refreshAll);
let historyTimer;$("history-search").oninput=()=>{clearTimeout(historyTimer);historyTimer=setTimeout(handleError(()=>searchHistory($("history-search").value)),300);};
$("refresh").onclick=handleError(refreshAll);
for(const id of ["filter-from","filter-to"])$(id).onchange=()=>{$("filter-week").value="";refresh();};
$("filter-week").onchange=event=>{setWeek(event.target.value);refresh();};
if(canWrite){
 $("cancel-report-edit").onclick=()=>$("report-dialog").close();
 $("edit-add-issue").onclick=()=>addEditIssue();
 $("edit-clean").onchange=()=>{const clean=$("edit-clean").checked;$("edit-issues").hidden=clean;$("edit-add-issue").disabled=clean;$("edit-issues").querySelectorAll("select").forEach(s=>s.disabled=clean);};
 document.addEventListener("click",event=>{
  const button=event.target.closest("[data-edit-report],[data-delete-report]");if(!button)return;
  const deleting=!!button.dataset.deleteReport,r=historyRecords.get(button.dataset.deleteReport||button.dataset.editReport);if(!r||r.voided)return;
  reportMutation={id:r.id,version:r.version,key:crypto.randomUUID(),deleting,areas:r.areas.length?r.areas:catalog.stages};
  $("report-edit-title").textContent=deleting?"Excluir apontamento":"Editar apontamento";
  $("report-edit-identity").textContent="O.S. "+r.numero_os+" · Item "+r.item_number+" · "+r.chassi;
  $("report-edit-error").textContent="";$("edit-reason").value="";$("delete-confirm").checked=false;
  $("delete-confirm-label").hidden=!deleting;$("delete-confirm").required=deleting;
  $("edit-fields").hidden=deleting;$("edit-issues").replaceChildren();r.issues.forEach(addEditIssue);
  $("edit-observed").value=String(r.observed_on).slice(0,10);$("edit-observed").max=today;
  $("edit-clean").checked=r.issues.length===0;$("edit-clean").onchange();
  $("edit-fields").querySelectorAll("input,select").forEach(el=>el.disabled=deleting||(el.closest('#edit-issues')&&$("edit-clean").checked));
  $("save-report-edit").textContent=deleting?"Excluir apontamento":"Salvar alterações";$("report-dialog").showModal();
 });
 $("report-edit-form").onsubmit=async event=>{event.preventDefault();$("save-report-edit").disabled=true;try{
  const m=reportMutation,data={idempotency_key:m.key,version:m.version,reason:$("edit-reason").value};
  if(m.deleting)data.confirm=$("delete-confirm").checked;
  else{data.observed_on=$("edit-observed").value;data.audit_complete=true;
   data.issues=$("edit-clean").checked?[]:[...$("edit-issues").querySelectorAll('.edit-issue')].map(row=>({id:row.dataset.id,defect_id:row.querySelector('.edit-defect').value,stage_code:row.querySelector('.edit-area').value}));
   if(!$("edit-clean").checked&&!data.issues.length)throw Error("Inclua um defeito ou marque Auditoria sem defeitos.");
  }
  await api('/api/ftt/reports/'+m.id+(m.deleting?'/delete':'/edit'),data);$("report-dialog").close();
  message(m.deleting?"Apontamento excluído dos indicadores. Evidência preservada no histórico.":"Apontamento alterado. Versão anterior preservada e indicadores atualizados.");await refreshAll();
 }catch(e){$("report-edit-error").textContent=e.message;}finally{$("save-report-edit").disabled=false;}};
 $("observed-on").value=today;$("observed-on").max=today;
 if(today<"2026-10-26"){$("save-report").disabled=true;message("O registro de apontamentos começa em 26/10/2026. As descrições de defeito já podem ser cadastradas.");}
 $("add-issue").onclick=addIssue;
 $("no-defects").onchange=()=>{const clean=$("no-defects").checked;$("issue-lines").hidden=clean;$("add-issue").disabled=clean;$("issue-lines").querySelectorAll("select").forEach(s=>s.disabled=clean);};
 $("work-order").onchange=handleError(async()=>{const chosen=$("work-order").selectedOptions[0];if(chosen&&chosen.value){if(![...$("history-work").options].some(o=>o.value===chosen.value))$("history-work").append(chosen.cloneNode(true));$("history-work").value=chosen.value;await loadWork();}});
 let timer;$("work-search").oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>loadOptions($("work-search").value).catch(e=>message(e.message,true)),300);};
 $("quality-form").onsubmit=async event=>{event.preventDefault();$("save-report").disabled=true;
 const data={idempotency_key:key,audit_complete:$("audit-confirm").checked,work_order_id:$("work-order").value,observed_on:$("observed-on").value,issues:$("no-defects").checked?[]:[...document.querySelectorAll(".issue-line")].map(row=>({defect_id:row.querySelector(".defect-select").value,stage_code:row.querySelector(".area-select").value}))};
 try{await api("/api/ftt/reports",data);key=crypto.randomUUID();$("issue-lines").replaceChildren();addIssue();$("no-defects").checked=false;$("no-defects").onchange();$("audit-confirm").checked=false;$("history-day").value=data.observed_on;message("Auditoria registrada. O.S. do dia e indicadores atualizados automaticamente.");await refreshAll();}catch(e){message(e.message,true);}finally{$("save-report").disabled=today<"2026-10-26";}};
 document.addEventListener("click",event=>{
  const button=event.target.closest("[data-correct-report]");if(!button)return;
  const report=historyRecords.get(button.dataset.correctReport),issue=report?.issues.find(i=>i.id===button.dataset.issue);if(!issue)return;
  correction={issue:issue.id,version:report.version,key:crypto.randomUUID()};
  $("correction-description").textContent="O.S. "+report.numero_os+" · "+issue.description+" · Atual: "+(labels[issue.stage_code]||issue.stage_code);
  $("correction-area").innerHTML=options(report.audit_complete?report.areas:catalog.stages,c=>c,c=>labels[c]||c);$("correction-area").value=issue.stage_code;
  $("correction-reason").value="";$("correction-error").textContent="";$("correction-dialog").showModal();
 });
 $("cancel-correction").onclick=()=>$("correction-dialog").close();
 $("correction-form").onsubmit=async event=>{event.preventDefault();$("save-correction").disabled=true;try{
  await api("/api/ftt/issues/"+correction.issue+"/area",{idempotency_key:correction.key,version:correction.version,stage_code:$("correction-area").value,reason:$("correction-reason").value});
  $("correction-dialog").close();message("Área corrigida com histórico preservado. Indicadores atualizados.");await refreshAll();
 }catch(e){$("correction-error").textContent=e.message;}finally{$("save-correction").disabled=false;}};
 $("cancel-defect").onclick=()=>$("defect-dialog").close();
 $("defect-form").onsubmit=async event=>{event.preventDefault();$("save-defect").disabled=true;try{const r=await api("/api/ftt/defects",{description:$("new-description").value});await loadOptions($("work-search").value);fillDefects();if(targetSelect)targetSelect.value=r.defect.id;$("new-description").value="";$("defect-dialog").close();message("Descrição de defeito cadastrada.");}catch(e){message(e.message,true);}finally{$("save-defect").disabled=false;}};
}
Promise.all([loadOptions(),searchHistory()]).then(()=>{if(canWrite)addIssue();return refreshAll();}).catch(e=>message(e.message,true));
setInterval(()=>{if(document.visibilityState==="visible")handleError(refreshAll)();},60000);
document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")handleError(refreshAll)();});

