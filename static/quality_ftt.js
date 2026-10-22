"use strict";
const $=id=>document.getElementById(id);
const labels={"PREP":"Preparação","EXPE":"Expedição","SERRA":"Serralheria","VIDROS":"Vidros","A/C":"Ar-condicionado","DESMONT":"Desmontagem","REVEST":"Revestimento","ELÉTRICA":"Elétrica","BCO":"Bancos","ACESSÓRIO":"Acessório","PLOTAGEM":"Plotagem","LIBERAÇÃO":"Liberação"};
const canWrite=document.body.dataset.write==="true";
let catalog={defects:[],stages:[],work_orders:[]},key=crypto.randomUUID(),targetSelect=null,searchSeq=0,reportSeq=0;
const escapeHTML=value=>{const span=document.createElement("span");span.textContent=String(value??"");return span.innerHTML;};
const cell=(v,cls="")=>'<td class="'+cls+'">'+escapeHTML(v)+'</td>';
const number=v=>v===null||v===undefined?"—":Number(v).toLocaleString("pt-BR",{maximumFractionDigits:2});
const pct=v=>v===null?"Sem base":number(v)+"%";
const today=new Intl.DateTimeFormat("en-CA",{timeZone:"America/Sao_Paulo",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
const dateBR=value=>value?String(value).slice(0,10).split("-").reverse().join("/"):"—";
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
 row.innerHTML='<label class="defect">Descrição do defeito<select required class="defect-select"></select><button type="button" class="add-defect" aria-label="Cadastrar nova descrição de defeito">+</button></label><label>Área responsável<select required class="area-select">'+options(catalog.stages,x=>x,x=>labels[x]||x)+'</select></label><label>Classificação<select class="disposition"><option value="DEFEITO">Defeito</option><option value="REFUGO">Refugo</option></select></label><button type="button" class="remove">Remover</button>';
 row.querySelector(".defect-select").innerHTML=options(catalog.defects,x=>x.id,x=>x.description);
 row.querySelector(".add-defect").onclick=()=>{targetSelect=row.querySelector(".defect-select");$("defect-dialog").showModal();$("new-description").focus();};
 row.querySelector(".remove").onclick=()=>{if(document.querySelectorAll(".issue-line").length>1)row.remove();};
 $("issue-lines").append(row);
}
function query(){return new URLSearchParams({from:$("filter-from").value,to:$("filter-to").value});}
async function refresh(){
 try{const seq=++reportSeq;const data=await api("/api/ftt?"+query());if(seq!==reportSeq)return;
 $("k-failures").textContent=number(data.total_failures);$("k-produced").textContent=number(data.totals.produced);$("k-ftt").textContent=pct(data.totals.ftt);
 $("pareto").innerHTML=data.pareto.map(r=>"<tr>"+cell(r.rank)+cell(r.description)+cell(labels[r.area]||r.area)+cell(r.frequency,"num")+cell(pct(r.accumulated),"num")+"</tr>").join("")||'<tr><td colspan="5">Sem ocorrências neste período.</td></tr>';
 $("pareto-total").textContent=number(data.total_failures);
 $("areas").innerHTML=data.areas.map(r=>"<tr>"+cell(labels[r.area]||r.area)+["produced","defective","scrapped","first_pass"].map(k=>cell(number(r[k]),"num")).join("")+cell(pct(r.ftt),"num")+cell(r.status,"status "+(r.ftt===null?"":r.ftt>=95?"good":r.ftt>=90?"warn":"bad"))+"</tr>").join("");
 $("area-total").innerHTML="<tr>"+cell("TOTAL / PASSAGENS")+["produced","defective","scrapped","first_pass"].map(k=>cell(number(data.totals[k]),"num")).join("")+cell(pct(data.totals.ftt),"num")+cell("Média ponderada")+"</tr>";
 $("records").innerHTML=data.records.map(r=>"<tr>"+cell(r.numero_os)+cell(r.chassi)+cell(dateBR(r.observed_on))+cell(r.description)+cell(labels[r.stage_code]||r.stage_code)+cell(r.disposition==="REFUGO"?"Refugo":"Defeito")+cell(r.actor)+cell(new Date(r.created_at).toLocaleString("pt-BR",{timeZone:"America/Sao_Paulo"}))+"</tr>").join("")||'<tr><td colspan="8">Nenhum apontamento no período.</td></tr>';
 $("pending").textContent=data.pending+" ocorrência(s) do período ainda sem conclusão da respectiva área neste intervalo. Aparecem no Pareto; entram no FTT quando a etapa for concluída.";
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
$("filter-to").value=today<"2026-10-26"?"2026-10-26":today;
$("refresh").onclick=refresh;
if(canWrite){
 $("observed-on").value=today;$("observed-on").max=today;
 if(today<"2026-10-26"){$("save-report").disabled=true;message("O registro de apontamentos começa em 26/10/2026. As descrições de defeito já podem ser cadastradas.");}
 $("add-issue").onclick=addIssue;
 let timer;$("work-search").oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>loadOptions($("work-search").value).catch(e=>message(e.message,true)),300);};
 $("quality-form").onsubmit=async event=>{event.preventDefault();$("save-report").disabled=true;
 const data={idempotency_key:key,work_order_id:$("work-order").value,observed_on:$("observed-on").value,issues:[...document.querySelectorAll(".issue-line")].map(row=>({defect_id:row.querySelector(".defect-select").value,stage_code:row.querySelector(".area-select").value,disposition:row.querySelector(".disposition").value}))};
 try{await api("/api/ftt/reports",data);key=crypto.randomUUID();$("issue-lines").replaceChildren();addIssue();message("Apontamento registrado com sucesso.");await refresh();}catch(e){message(e.message,true);}finally{$("save-report").disabled=today<"2026-10-26";}};
 $("cancel-defect").onclick=()=>$("defect-dialog").close();
 $("defect-form").onsubmit=async event=>{event.preventDefault();$("save-defect").disabled=true;try{const r=await api("/api/ftt/defects",{description:$("new-description").value});await loadOptions($("work-search").value);fillDefects();if(targetSelect)targetSelect.value=r.defect.id;$("new-description").value="";$("defect-dialog").close();message("Descrição de defeito cadastrada.");}catch(e){message(e.message,true);}finally{$("save-defect").disabled=false;}};
}
loadOptions().then(()=>{if(canWrite)addIssue();return refresh();}).catch(e=>message(e.message,true));
setInterval(()=>{if(document.visibilityState==="visible")refresh();},60000);
document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")refresh();});

