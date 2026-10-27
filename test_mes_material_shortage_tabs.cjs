const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const {test}=require('node:test');

const html=fs.readFileSync(require('node:path').join(__dirname,'templates/material_faltas.html'),'utf8');
const start=html.indexOf('function chassisSuffix(');
const end=html.indexOf('function selectTab(',start);
assert.notEqual(start,-1,'shortage-tab helpers must exist');
assert.notEqual(end,-1,'shortage-tab helpers must be delimited');
const helpers=html.slice(start,end);

function grouped(chassis){
 const context=vm.createContext({
  report:{chassis},chassisGroups:[],activeKey:'__ALL__',
  esc:value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;'),
  document:{getElementById:()=>({innerHTML:''})},
 });
 return JSON.parse(JSON.stringify(new vm.Script(helpers+';makeGroups();({groups:chassisGroups,labels:chassisGroups.map(tabLabel)})').runInContext(context)));
}

test('shortage tabs follow MES sequence, then item, and omit full chassis from labels',()=>{
 const {groups}=grouped([
  {chassi:'WF0LTBVF8TU022142',item_number:3186,sequencia:12},
  {chassi:'9VYF62001SJ064758',item_number:3174,sequencia:3},
  {chassi:'9VYF62001SJ064758',item_number:3175,sequencia:3},
  {chassi:'93YF62001SJ064799',item_number:3100,sequencia:null},
 ]);
 assert.deepEqual(groups.map(group=>group.chassi),[
  '9VYF62001SJ064758','WF0LTBVF8TU022142','93YF62001SJ064799',
 ]);
 assert.equal(groups[0].orders.length,2);
});

test('tab names include item number(s) and only the final eight chassis characters',()=>{
 const {labels}=grouped([
   {chassi:'WF0LTBVF8TU022142',item_number:3186,sequencia:12},
   {chassi:'9VYF62001SJ064758',item_number:3174,sequencia:3},
   {chassi:'9VYF62001SJ064758',item_number:3175,sequencia:3},
 ]);
 assert.deepEqual(labels,[
  'ITENS 3174/3175 · CHASSI SJ064758',
  'ITEM 3186 · CHASSI TU022142',
 ]);
 assert.ok(labels.every(label=>!label.includes('WF0LTBVF8TU022142')&&!label.includes('9VYF62001SJ064758')));
});
