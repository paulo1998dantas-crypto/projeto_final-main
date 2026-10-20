const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const {test}=require('node:test');
const html=fs.readFileSync(require('node:path').join(__dirname,'templates/gestao_os.html'),'utf8');
const code=html.slice(html.indexOf('function materialQuantity('),html.indexOf('async function loadMaterialCockpit('));

function render(overrides={}){
 const elements=Object.fromEntries(['materials-kpis','materials-cockpit-content','materials-cockpit-status'].map(k=>[k,{innerHTML:'',textContent:''}]));
 const context=vm.createContext({document:{getElementById:id=>elements[id]},
  esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),date:v=>v});
 new vm.Script(code).runInContext(context);
 context.payload={documento_encontrado:true,documento_numero:'4100',summary:{total:1,faltante_total:1},
  reserva_sequencia:{disponivel:true,aplicavel:true,completa:true,posicao:6,ordens_sem_composicao:[]},
  items:[{codigo:'1001',descricao:'Material <seguro>',unidade:'pc',necessario:1,saldo_atual:5,
   empenhado_na_os:0,saldo_empenhado:1,saldo_disponivel:4,reserva:4,demanda_anterior:4,
   saldo_disponivel_sequencia:0,faltante:1,em_chegada:0,previsoes:[],status:'SEM_PREVISAO',status_label:'Sem saldo e sem previsão'}],...overrides};
 new vm.Script('renderMaterialCockpit(payload)').runInContext(context);
 return elements;
}

test('shows reservation, sequenced balance and shortage without claiming global free stock belongs to card',()=>{
 const e=render();const s=e['materials-cockpit-content'].innerHTML;
 assert.match(s,/<th>Reserva<\/th>/);
 assert.match(s,/<th>Disponível na sequência<\/th>/);
 assert.match(s,/<th>Falta na O.S.<\/th>/);
 assert.match(s,/<strong>4<\/strong><small>demanda anterior: 4/);
 assert.match(s,/<strong>0<\/strong><small>livre global: 4/);
 assert.match(s,/Material &lt;seguro&gt;/);
 assert.match(e['materials-cockpit-status'].textContent,/sequência 6/);
 assert.doesNotMatch(s,/livre para novo empenho/);
});

test('missing preceding composition is explicit and escapes document numbers',()=>{
 const e=render({reserva_sequencia:{disponivel:true,aplicavel:true,completa:false,posicao:6,
  ordens_sem_composicao:[{numero_os:'<script>bad</script>'}]}});
 assert.match(e['materials-cockpit-content'].innerHTML,/Reserva incompleta/);
 assert.match(e['materials-cockpit-content'].innerHTML,/&lt;script&gt;bad&lt;\/script&gt;/);
 assert.doesNotMatch(e['materials-cockpit-content'].innerHTML,/<script>bad/);
 assert.match(e['materials-kpis'].innerHTML,/Falta identificada/);
});

test('old endpoint and absent WIP schema are still rendered with an explicit warning',()=>{
 const e=render({reserva_sequencia:{disponivel:false,aplicavel:false,aviso:'Sequenciamento indisponível'}});
 assert.match(e['materials-cockpit-content'].innerHTML,/Sequenciamento indisponível/);
 assert.match(e['materials-cockpit-content'].innerHTML,/<strong>—<\/strong><small>não aplicada à fila/);
});

test('unconfirmed positive balance is not painted as available when reservation is incomplete',()=>{
 const e=render({reserva_sequencia:{disponivel:true,aplicavel:true,completa:false,posicao:2,
  ordens_sem_composicao:[{numero_os:'4101'}]},items:[{codigo:'1001',necessario:1,
   saldo_disponivel:5,saldo_disponivel_sequencia:5,reserva:0,demanda_anterior:0,
   status:'RESERVA_INCOMPLETA',status_label:'Conferir composições anteriores',previsoes:[]}]});
 assert.doesNotMatch(e['materials-cockpit-content'].innerHTML,/class="material-balance-cell available/);
});

test('material table has its own bounded scroll area and an opaque sticky header',()=>{
 assert.match(html,/\.material-table-wrap\s*\{[^}]*overflow:auto/);
 assert.match(html,/\.material-table-wrap\s*\{[^}]*max-height:min\(60vh,560px\)/);
 assert.match(html,/\.material-table thead th\s*\{[^}]*position:sticky;top:0;z-index:2;background:#f8fafc/);
 assert.match(html,/\.material-table\s*\{[^}]*border-collapse:separate;border-spacing:0/);
});
