const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'templates/material_faltas.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\nloadReport\(\);\s*$/, '');

function setup(ok){
  const elements={'export':{disabled:false},message:{},filter:{value:' Banco PME '}};
  const calls=[];
  let downloaded=false;
  const context=vm.createContext({
    document:{getElementById:id=>elements[id],body:{appendChild(){}},createElement:()=>({click(){downloaded=true},remove(){}})},
    URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},setTimeout(){},
    fetch:async(url,options)=>{calls.push({url,options});return {ok,headers:{get:()=> 'attachment; filename="Faltas.xlsx"'},blob:async()=>({}),json:async()=>({error:'Sessão encerrada'})}},
  });
  new vm.Script(script).runInContext(context);
  return {context,elements,calls,downloaded:()=>downloaded};
}

test('export posts the displayed snapshot, active chassis and filter without recalculation',async()=>{
  const state=setup(true);
  await new vm.Script('report={rows:[{reserva:4,faltante:4.5}],chassis:[],gerado_em:"2026-10-28"};activeKey="VIN";exportExcel()').runInContext(state.context);
  assert.equal(state.calls.length,1);
  assert.equal(state.calls[0].url,'/api/erp/material-shortages/export');
  assert.equal(state.calls[0].options.method,'POST');
  const payload=JSON.parse(state.calls[0].options.body);
  assert.equal(payload.report.rows[0].reserva,4);
  assert.equal(payload.report.rows[0].faltante,4.5);
  assert.equal(payload.query,' Banco PME ');
  assert.equal(payload.active_key,'VIN');
  assert.equal(state.downloaded(),true);
  assert.equal(state.elements.export.disabled,false);
});

test('failed export shows error, creates no download, and restores button',async()=>{
  const state=setup(false);
  await new vm.Script('report={rows:[],chassis:[]};exportExcel()').runInContext(state.context);
  assert.equal(state.downloaded(),false);
  assert.equal(state.elements.message.textContent,'Sessão encerrada');
  assert.equal(state.elements.message.className,'message error');
  assert.equal(state.elements.export.disabled,false);
});
