/* Task definitions remain separate from participant prompts and private verifiers. */
let customTaskRows=[],scenarioTaskDefinitions=null,scenarioTaskSelection=null;
const taskFields=new Set(['id','family','prompt','verifier','flag_nodes','required_checks','split','discovery','starting_facts','discoverable_facts','objective_requires','progressive_hints']);
function validateTaskList(tasks){
 if(!Array.isArray(tasks)||!tasks.length||tasks.length>32)throw Error('Supply 1–32 tasks.');
 if(new TextEncoder().encode(JSON.stringify(tasks)).length>65536)throw Error('Task definitions exceed 64 KiB.');
 const ids=new Set();
 tasks.forEach((task,index)=>{
  const fail=message=>{throw Error('Task '+(index+1)+': '+message);};
  if(!task||Array.isArray(task)||typeof task!=='object'||Object.keys(task).some(k=>!taskFields.has(k)))fail('Unknown task fields.');
  if(typeof task.id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(task.id))fail('Enter a valid task ID (1–80 letters, numbers, dots, underscores or hyphens).');
  if(ids.has(task.id))fail('Task IDs must be unique.');ids.add(task.id);
  if(typeof task.family!=='string'||!task.family.trim())fail('Enter a task family.');
  if('split' in task&&!['development','validation','test'].includes(task.split))fail('Invalid split.');
  if('prompt' in task&&(typeof task.prompt!=='string'||!task.prompt.trim()))fail('Enter a prompt.');
  if(!Array.isArray(task.required_checks)||!task.required_checks.length||task.required_checks.some(c=>typeof c!=='string'||!c.trim()))fail('Enter required readiness checks.');
  if('flag_nodes' in task){
   if('verifier' in task||!Array.isArray(task.flag_nodes)||!task.flag_nodes.length||task.flag_nodes.some(n=>typeof n!=='string'||!n)||new Set(task.flag_nodes).size!==task.flag_nodes.length)fail('Use distinct flag node IDs without an explicit verifier.');
  }else{
   if(!task.prompt)fail('Enter a prompt.');
   const v=task.verifier;
   if(!v||Object.keys(v).sort().join(',')!=='expected,type'||!['json_equals','contains_all'].includes(v.type))fail('Use a json_equals or contains_all verifier with an expected value.');
   if(v.type==='contains_all'&&(!Array.isArray(v.expected)||!v.expected.length||v.expected.some(x=>typeof x!=='string'||!x)))fail('contains_all expects a nonempty array of strings.');
  }
  if('progressive_hints' in task&&(!Array.isArray(task.progressive_hints)||task.progressive_hints.length>16||task.progressive_hints.some(h=>typeof h!=='string'||!h.trim()||h.length>1500)))fail('Progressive hints must be up to 16 nonempty strings of at most 1500 characters.');
  if('discovery' in task&&typeof task.discovery!=='boolean')fail('Discovery must be true or false.');
  if(!task.discovery&&['starting_facts','discoverable_facts','objective_requires','progressive_hints'].some(k=>k in task))fail('Fact declarations require discovery: true.');
 });
 return tasks;
}
function taskRow(task){
 const {id='',family='custom',prompt='',required_checks=['containers','services','ports','injects'],verifier,flag_nodes,...extra}=task;
 return {id,family,prompt,checks:required_checks.join(', '),criteria:JSON.stringify(flag_nodes?{flag_nodes}:verifier||{type:'json_equals',expected:{}},null,2),extra:JSON.stringify(extra,null,2)};
}
function readTaskRows(){return [...$('task-editor').querySelectorAll('[data-task-row]')].map(row=>Object.fromEntries(['id','family','prompt','checks','criteria','extra'].map(key=>[key,row.querySelector('[data-task-field="'+key+'"]').value])));}
function parseTaskRows(rows){
 return validateTaskList(rows.map((row,index)=>{
  let criteria,extra;try{criteria=JSON.parse(row.criteria);extra=JSON.parse(row.extra||'{}');}catch{throw Error('Task '+(index+1)+': success criteria and advanced settings must be valid JSON.');}
  if(!criteria||Array.isArray(criteria)||typeof criteria!=='object'||!extra||Array.isArray(extra)||typeof extra!=='object')throw Error('Task '+(index+1)+': criteria and advanced settings must be JSON objects.');
  if(Object.keys(extra).some(k=>!['split','discovery','starting_facts','discoverable_facts','objective_requires','progressive_hints'].includes(k)))throw Error('Task '+(index+1)+': unsupported advanced field.');
  const scoring='flag_nodes' in criteria?criteria:{verifier:criteria};
  if('flag_nodes' in criteria&&Object.keys(criteria).length!==1)throw Error('Task '+(index+1)+': flag_nodes criteria cannot contain other fields.');
  return {...extra,id:row.id.trim(),family:row.family.trim(),...(row.prompt.trim()?{prompt:row.prompt}:{}),required_checks:row.checks.split(',').map(s=>s.trim()).filter(Boolean),...scoring};
 }));
}
function taskValidationMessage(){
 if($('experiment-sample').value!=='scenarioforge-xml'||$('task-source').value!=='custom')return '';
 try{parseTaskRows(customTaskRows);return '';}catch(error){return error.message;}
}
function experimentTasks(){return $('task-source').value==='custom'?parseTaskRows(customTaskRows):null;}
function renderTaskRows(rows,editable){
 const target=$('task-editor');target.replaceChildren();
 rows.forEach((row,index)=>{
  const fieldset=el('fieldset',null,'task-card');fieldset.dataset.taskRow=String(index);fieldset.disabled=!editable;
  fieldset.append(el('legend','Task '+(index+1)));
  for(const [key,label,multiline] of [['id','Task ID',false],['family','Task family',false],['prompt','Task prompt',true],['criteria','Success criteria (JSON)',true],['checks','Required readiness checks (comma separated)',false],['extra','Advanced settings (JSON)',true]]){
   const input=el(multiline?'textarea':'input');input.id='task-'+index+'-'+key;input.dataset.taskField=key;input.value=row[key];if(multiline)input.rows=key==='prompt'?5:3;
   const caption=el('label',label);caption.htmlFor=input.id;fieldset.append(caption,input);
   if(key==='criteria')fieldset.append(el('p','Use {"type":"json_equals","expected":{...}}, {"type":"contains_all","expected":["text"]}, or {"flag_nodes":["node-id"]}. A flag-node task may omit its prompt to use ScenarioForge’s generated prompt.','small'));
   if(key==='extra')fieldset.append(el('p','Optional split, discovery, starting_facts, discoverable_facts and objective_requires. Imported settings are preserved.','small'));
  }
  if(editable){const remove=el('button','Remove task');remove.type='button';remove.addEventListener('click',()=>{customTaskRows.splice(index,1);renderTaskEditor();syncCreateExperiment();});fieldset.append(remove);}
  target.append(fieldset);
 });
}
function renderTaskEditor(){
 const editable=$('task-source').value==='custom';
 $('task-edit-actions').hidden=!editable;
 $('edit-scenario-tasks').hidden=!Array.isArray(scenarioTaskDefinitions)||editable;
 renderTaskRows(editable?customTaskRows:(scenarioTaskDefinitions||[]).map(taskRow),editable);
 $('task-editor-error').textContent=taskValidationMessage();
}
function resetTaskEditor(){
 customTaskRows=[];scenarioTaskDefinitions=null;scenarioTaskSelection=null;$('task-source').value='scenario';$('task-import').value='';
 $('task-source-note').textContent='Select a scenario, then load its tasks to review them. Without overrides, ScenarioForge uses its saved task definitions or its default flag-collection task.';
 renderTaskEditor();
}
function configureEvaluationTasks(scenario,sample){
 $('sample-task-info').hidden=scenario;$('custom-task-info').hidden=!scenario;
 $('sample-task-id').value=sample?.id||'';
 $('sample-success').value=sample?.id==='smoke'?'Exact JSON match: service_token must equal the freshly generated token.':'Exact JSON match: flags must contain both freshly generated flags in discovery order.';
 if(scenario)renderTaskEditor();
}
function taskScenarioChanged(){
 scenarioTaskDefinitions=null;scenarioTaskSelection=null;
 $('task-source-note').textContent='Load tasks from the selected scenario to review them. Custom task edits, if any, remain selected for this experiment.';
 if($('task-source').value==='scenario')renderTaskEditor();
}
function initTaskEditor(){
 $('task-editor').addEventListener('input',()=>{if($('task-source').value==='custom'){customTaskRows=readTaskRows();$('task-editor-error').textContent=taskValidationMessage();syncCreateExperiment();}});
 $('task-source').addEventListener('change',()=>{if($('task-source').value==='custom'&&!customTaskRows.length)customTaskRows=(scenarioTaskDefinitions||[{id:'task-1',family:'custom'}]).map(taskRow);renderTaskEditor();syncCreateExperiment();});
 $('add-task').addEventListener('click',()=>{let number=1;while(customTaskRows.some(row=>row.id==='task-'+number))number++;customTaskRows.push(taskRow({id:'task-'+number,family:'custom'}));renderTaskEditor();syncCreateExperiment();});
 $('edit-scenario-tasks').addEventListener('click',()=>{try{validateTaskList(scenarioTaskDefinitions);customTaskRows=scenarioTaskDefinitions.map(taskRow);$('task-source').value='custom';renderTaskEditor();syncCreateExperiment();}catch(error){$('task-editor-error').textContent=error.message;}});
 $('task-import').addEventListener('change',async()=>{
  const file=$('task-import').files[0];if(!file)return;
  loadingModal.set('task-import',true,'Reading task definitions…');
  try{if(file.size>65536)throw Error('Task import is limited to 64 KiB.');const tasks=validateTaskList(JSON.parse(await file.text()));customTaskRows=tasks.map(taskRow);$('task-source').value='custom';renderTaskEditor();$('task-source-note').textContent='Imported '+tasks.length+' task(s) from '+file.name+'. These definitions will be saved in this experiment’s XML.';}
  catch(error){$('task-editor-error').textContent=error.message;}
  finally{loadingModal.set('task-import',false);$('task-import').value='';syncCreateExperiment();}
 });
 $('load-scenario-tasks').addEventListener('click',async()=>{
  if(isBusy())return;
  const selection=$('scenario-selection').value;
  if(!selection||scenarioChoiceVM!==snapshot?.roles?.scenarioforge){$('task-editor-error').textContent='Choose a saved scenario on the ScenarioForge tab first.';return;}
  operation='Loading tasks from ScenarioForge…';syncBusy();
  try{
   const response=await apiFetch('/api/scenarios/tasks',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({selection_id:selection})});
   const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Unable to load scenario tasks');
   if(result.tasks!==null&&!Array.isArray(result.tasks))throw Error('Scenario task definitions must be a JSON array.');
   scenarioTaskDefinitions=result.tasks;scenarioTaskSelection=selection;
   $('task-source-note').textContent=result.tasks===null?'No explicit tasks are saved in this scenario. ScenarioForge will generate a flag-collection task from resolved flag values. Define custom tasks to supply your own prompt and success criteria.':'Loaded '+result.tasks.length+' task(s) from the selected scenario. Use Edit loaded tasks to create experiment-specific overrides.';
   renderTaskEditor();
  }catch(error){$('task-editor-error').textContent=error.message;}
  finally{operation=null;syncBusy();schedulePoll();}
 });
}
