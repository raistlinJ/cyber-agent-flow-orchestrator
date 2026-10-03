/* Task definitions remain separate from participant prompts and private verifiers. */
let customTaskRows=[],scenarioTaskDefinitions=null,scenarioTaskSelection=null,scenarioTaskContext=null,scenarioTaskSuggested=false;
const taskFields=new Set(['id','family','prompt','verifier','flag_nodes','required_checks','split','discovery','starting_facts','discoverable_facts','objective_requires','progressive_hints']);
const readinessChecks=[
 ['containers','Workloads started'],['services','Expected services running'],['ports','Expected ports listening'],
 ['injects','Required files placed'],['segmentation','Firewall rules applied'],['traffic','Traffic agents running'],
 ['reachability','Configured traffic reaches its target'],['flow_pivot','Flow pivot path works'],
 ['pivot_access','Participant can reach pivot providers']
];
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
  if(!task.discovery&&['starting_facts','discoverable_facts','objective_requires'].some(k=>k in task))fail('Fact declarations require discovery: true.');
 });
 return tasks;
}
function taskRow(task){
 const {id='',family='custom',prompt='',required_checks=['containers','services','ports'],verifier,flag_nodes,...extra}=task;
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
function criteriaDescription(value){
 try{
  const criteria=JSON.parse(value);
  if(Array.isArray(criteria.flag_nodes))return 'ScenarioForge verifies the fresh private flag generated for each listed Flow node. The expected flag values stay out of the participant prompt.';
  if(criteria.type==='json_equals')return 'The agent’s final JSON must exactly equal the expected JSON value shown here.';
  if(criteria.type==='contains_all')return 'The agent’s final response must contain every listed string.';
 }catch{}
 return 'Choose flag_nodes for ScenarioForge-generated flags, json_equals for a structured answer, or contains_all for required response text.';
}
function appendReadinessEditor(fieldset,row,index){
 const caption=el('div','Required readiness checks','task-field-label');fieldset.append(caption);
 const selected=new Set(row.checks.split(',').map(value=>value.trim()).filter(Boolean));
 const known=new Set(readinessChecks.map(([key])=>key));
 const hidden=el('input');hidden.type='hidden';hidden.dataset.taskField='checks';hidden.value=row.checks;
 const grid=el('div',null,'readiness-checks');
 const boxes=[];
 const sync=()=>{
  const values=boxes.filter(box=>box.checked).map(box=>box.value);
  values.push(...other.value.split(',').map(value=>value.trim()).filter(Boolean));
  hidden.value=[...new Set(values)].join(', ');
 };
 readinessChecks.forEach(([key,description])=>{
  const label=el('label',null,'readiness-check');
  const box=el('input');box.type='checkbox';box.value=key;box.checked=selected.has(key);boxes.push(box);
  box.addEventListener('change',sync);
  const copy=el('span');copy.append(el('strong',key),el('small',description));label.append(box,copy);grid.append(label);
 });
 const otherLabel=el('label','Additional check names','task-subfield-label');otherLabel.htmlFor='task-'+index+'-checks-other';
 const other=el('input');other.id=otherLabel.htmlFor;other.value=[...selected].filter(key=>!known.has(key)).join(', ');other.placeholder='Optional, comma separated';other.addEventListener('input',sync);
 fieldset.append(hidden,grid,otherLabel,other,el('p','Only select checks that must pass before this task can run. Scenario-derived drafts choose checks from the saved Flow topology.','small'));
}
function renderTaskRows(rows,editable){
 const target=$('task-editor');target.replaceChildren();
 rows.forEach((row,index)=>{
  const fieldset=el('fieldset',null,'task-card');fieldset.dataset.taskRow=String(index);fieldset.disabled=!editable;
  fieldset.append(el('legend','Task '+(index+1)));
  for(const [key,label,multiline] of [['id','Task ID',false],['family','Task family',false],['prompt','Task prompt',true],['criteria','Success verification',true],['extra','Advanced settings (JSON)',true]]){
   const input=el(multiline?'textarea':'input');input.id='task-'+index+'-'+key;input.dataset.taskField=key;input.value=row[key];if(multiline)input.rows=key==='prompt'?5:3;
   const caption=el('label',label);caption.htmlFor=input.id;fieldset.append(caption,input);
   if(key==='prompt')fieldset.append(el('p','Participant-facing instructions. A scenario draft starts with the resolved Flow targets and expected response shape; edit it to describe the intended objective.','small'));
   if(key==='criteria'){
    const help=el('p',criteriaDescription(row.criteria),'small criteria-description');
    input.addEventListener('input',()=>{help.textContent=criteriaDescription(input.value);});
    fieldset.append(help,el('p','Formats: {"flag_nodes":["node-id"]}, {"type":"json_equals","expected":{...}}, or {"type":"contains_all","expected":["text"]}.','small'));
   }
   if(key==='extra')fieldset.append(el('p','Optional split, discovery, starting_facts, discoverable_facts and objective_requires. Imported settings are preserved.','small'));
  }
  appendReadinessEditor(fieldset,row,index);
  if(editable){const remove=el('button','Remove task');remove.type='button';remove.addEventListener('click',()=>{customTaskRows.splice(index,1);renderTaskEditor();syncCreateExperiment();});fieldset.append(remove);}
  target.append(fieldset);
 });
}
function renderTaskContext(){
 const target=$('task-scenario-context');target.replaceChildren();
 const context=scenarioTaskContext;
 if(!context||!Array.isArray(context.chain)){target.hidden=true;return;}
 target.hidden=false;
 const flags=Array.isArray(context.flag_nodes)?context.flag_nodes:[];
 const hints=Array.isArray(context.progressive_hints)?context.progressive_hints:[];
 target.append(el('h4','What ScenarioForge supplied'));
 target.append(el('p',context.chain.length+' Flow step(s) · '+flags.length+' generated-flag target(s) · '+hints.length+' hint(s)','small'));
 if(context.chain.length){
  const list=el('ol',null,'scenario-chain-summary');
  context.chain.forEach(node=>{
   const details=[node.name||node.id,node.ipv4,node.is_vuln?'vulnerability':'',node.generator?'generator: '+node.generator:'',node.has_flag?'generated flag':''].filter(Boolean);
   list.append(el('li',details.join(' · ')));
  });
  target.append(list);
 }
 const sources=context.sources||{};
 target.append(el('p','Prompt: '+(sources.prompt||'not supplied')+' · Success: '+(sources.success||'not supplied')+' · Readiness: '+(sources.readiness||'not supplied')+' · Hints: '+(sources.hints||'not supplied'),'small'));
 if(Array.isArray(context.bundle_files)&&context.bundle_files.length)target.append(el('p','Imported bundle also contains: '+context.bundle_files.join(', ')+'.','small'));
 if(!flags.length&&scenarioTaskDefinitions===null)target.append(el('p','This scenario does not expose a safe machine-verifiable answer in its Flow data. Add an explicit expected result before using a custom task.','notice task-context-warning'));
 target.append(el('p','The attack graph and participant guide are derived from this same saved Flow state. When building a draft from Flow data, facilitator answers and resolved secret values are excluded.','small'));
}
function renderTaskEditor(){
 const editable=$('task-source').value==='custom';
 $('task-edit-actions').hidden=!editable;
 $('edit-scenario-tasks').hidden=!Array.isArray(scenarioTaskDefinitions)||editable;
 $('edit-scenario-tasks').textContent=scenarioTaskSuggested?'Use and edit scenario draft':'Edit loaded tasks';
 renderTaskRows(editable?customTaskRows:(scenarioTaskDefinitions||[]).map(taskRow),editable);
 renderTaskContext();
 $('task-editor-error').textContent=taskValidationMessage();
}
function resetTaskEditor(){
 customTaskRows=[];scenarioTaskDefinitions=null;scenarioTaskSelection=null;scenarioTaskContext=null;scenarioTaskSuggested=false;$('task-source').value='scenario';$('task-import').value='';
 $('task-source-note').textContent='Select a scenario, then load its task draft. Saved evaluation tasks are used first; otherwise the Flow chain, generated flags, public hints and topology seed an editable draft.';
 renderTaskEditor();
}
function configureEvaluationTasks(scenario,sample){
 $('sample-task-info').hidden=scenario;$('custom-task-info').hidden=!scenario;
 $('sample-task-id').value=sample?.id||'';
 $('sample-success').value=sample?.id==='smoke'?'Exact JSON match: service_token must equal the freshly generated token.':'Exact JSON match: flags must contain both freshly generated flags in discovery order.';
 if(scenario)renderTaskEditor();
}
function taskScenarioChanged(){
 scenarioTaskDefinitions=null;scenarioTaskSelection=null;scenarioTaskContext=null;scenarioTaskSuggested=false;
 $('task-source-note').textContent='Load a task draft from the selected scenario. Custom task edits, if any, remain selected for this experiment.';
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
   if(!Array.isArray(result.suggested_tasks||[]))throw Error('Scenario task draft must be a JSON array.');
   scenarioTaskSuggested=result.tasks===null&&(result.suggested_tasks||[]).length>0;
   scenarioTaskDefinitions=result.tasks!==null?result.tasks:(scenarioTaskSuggested?result.suggested_tasks:null);
   scenarioTaskContext=result.context||null;scenarioTaskSelection=selection;
   $('task-source-note').textContent=result.tasks!==null
    ?'Loaded '+result.tasks.length+' task(s) saved in ScenarioForge. Their prompts, verifiers, readiness checks and hints came from FlowState.evaluation_tasks. Use Edit loaded tasks for experiment-specific changes.'
    :scenarioTaskSuggested
     ?'No evaluation tasks were saved in the XML. Built '+result.suggested_tasks.length+' editable draft(s) from '+((result.context||{}).sources?.tasks||'the resolved Flow data')+'. Use and edit the draft to save it with this experiment.'
     :'No evaluation tasks or generated flags were found. The Flow context is shown below, but ScenarioForge cannot infer a safe success criterion; define one explicitly.';
   renderTaskEditor();
  }catch(error){$('task-editor-error').textContent=error.message;}
  finally{operation=null;syncBusy();schedulePoll();}
 });
}
