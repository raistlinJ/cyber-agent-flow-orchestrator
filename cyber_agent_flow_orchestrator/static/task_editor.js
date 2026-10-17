/* Scenario tasks remain separate from participant prompts and private verifiers. */
let scenarioTaskDefinitions=null,scenarioTaskSelection=null,scenarioTaskContext=null,scenarioTaskSuggested=false,scenarioTaskLoadError='',scenarioTaskLoadToken=0,scenarioTaskLoadTimer=null;
const taskFields=new Set(['id','family','prompt','verifier','flag_nodes','required_checks','split','discovery','starting_facts','discoverable_facts','objective_requires','progressive_hints','rubric','verification_mode','challenge_plan']);
function taskAnswerStrings(value){if(typeof value==='string')return [value];if(Array.isArray(value))return value.flatMap(taskAnswerStrings);if(value&&typeof value==='object')return Object.values(value).flatMap(taskAnswerStrings);return value==null?[]:[JSON.stringify(value)];}
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
  const mode=task.verification_mode||'exact';if(!['exact','judge','both'].includes(mode))fail('Choose exact, judge or both.');
  if(task.rubric)validateRubric(task.rubric);
  if(task.challenge_plan){const mapped=new Set((task.challenge_plan.steps||[]).flatMap(s=>s.criterion_ids||[]));if(task.challenge_plan.version!==1||!mapped.size||!task.rubric?.criteria.every(c=>mapped.has(c.id)))fail('Challenge plan must map the rubric criterion IDs. Update the advanced plan when changing IDs.');}
  if(mode!=='exact'&&!task.rubric)fail('Judge review requires a rubric.');
  if(mode==='judge'){if(!task.prompt||task.verifier||task.flag_nodes)fail('Judge-only tasks require a prompt and rubric without exact checks.');}
  else if('flag_nodes' in task){
   if('verifier' in task||!Array.isArray(task.flag_nodes)||!task.flag_nodes.length||task.flag_nodes.some(n=>typeof n!=='string'||!n)||new Set(task.flag_nodes).size!==task.flag_nodes.length)fail('Use distinct flag node IDs without an explicit verifier.');
  }else{
   if(!task.prompt)fail('Enter a prompt.');
   const v=task.verifier;
   if(!v||Object.keys(v).sort().join(',')!=='expected,type'||!['json_equals','contains_all'].includes(v.type))fail('Use a json_equals or contains_all verifier with an expected value.');
   if(v.type==='contains_all'&&(!Array.isArray(v.expected)||!v.expected.length||v.expected.some(x=>typeof x!=='string'||!x)))fail('contains_all expects a nonempty array of strings.');
  }
  if('progressive_hints' in task&&(!Array.isArray(task.progressive_hints)||task.progressive_hints.length>16||task.progressive_hints.some(h=>typeof h!=='string'||!h.trim()||h.length>1500)))fail('Progressive hints must be up to 16 nonempty strings of at most 1500 characters.');
  if(taskAnswerStrings(task.verifier?.expected).some(answer=>answer&&(task.progressive_hints||[]).some(h=>h.includes(answer))))fail('A progressive hint contains a verifier answer; use guidance instead of the solution.');
  if('discovery' in task&&typeof task.discovery!=='boolean')fail('Discovery must be true or false.');
  if(!task.discovery&&['starting_facts','discoverable_facts','objective_requires'].some(k=>k in task))fail('Fact declarations require discovery: true.');
 });
 return tasks;
}
function taskValidationMessage(){
 if($('experiment-sample').value!=='scenarioforge-xml')return '';
 const selection=$('scenario-selection').value;
 if(!selection)return '';
 if(scenarioTaskLoadError)return scenarioTaskLoadError;
 if(scenarioTaskSelection!==selection)return 'Wait for the selected scenario task to finish loading.';
 try{validateTaskList(scenarioTaskDefinitions);return '';}catch(error){return error.message;}
}
function taskVerificationSummary(task){
 const mode=task.verification_mode||'exact';
 if(mode==='judge'){const count=task.rubric?.criteria?.length||0;return `Judge reviews the transcript against ${count} rubric ${count===1?'criterion':'criteria'}.`;}
 if(Array.isArray(task.flag_nodes)){const count=task.flag_nodes.length;return `ScenarioForge verifies ${count} private generated flag ${count===1?'target':'targets'}.`;}
 if(task.verifier?.type==='json_equals')return mode==='both'?'Exact JSON result plus judge review.':'Final JSON must match the private expected result.';
 if(task.verifier?.type==='contains_all')return mode==='both'?'Required response evidence plus judge review.':'Final response must contain every required private value.';
 return mode==='both'?'Exact verification plus judge review.':'ScenarioForge performs the saved exact verification.';
}
function taskBadge(text,kind=''){return el('span',text,'task-meta-badge'+(kind?' '+kind:''));}
function taskHintGroups(task){
 const steps=(task.challenge_plan?.steps||[]).filter(step=>step.hints?.length);
 // The evaluator uses scoped challenge hints instead of the flat task list.
 return steps.length?steps.map(step=>({title:step.title||step.id,hints:step.hints})):task.progressive_hints?.length?[{title:null,hints:task.progressive_hints}]:[];
}
function taskHintSummary(task){
 const groups=taskHintGroups(task),count=groups.reduce((total,group)=>total+group.hints.length,0);
 if(groups.some(group=>group.title))return `${count} challenge ${count===1?'hint':'hints'} across ${groups.length} ${groups.length===1?'step':'steps'} when enabled`;
 if(count)return `${count} available when enabled`;
 if(task.discoverable_facts?.length)return 'Guidance available from discoverable facts when enabled';
 if(task.challenge_plan?.steps?.some(step=>step.solution))return 'Solution walkthroughs available; no intermediate hints';
 return 'None included in this task';
}
const taskTabViews=new WeakMap(),sampleTaskTabState={};
function taskTabs(id,label,owner,items){
 const holder=el('div',null,'task-detail-tabs'),navigation=el('div',null,'task-detail-navigation');
 navigation.setAttribute('role','tablist');navigation.setAttribute('aria-label',label);holder.append(navigation);
 const entries=items.map(([name,title])=>{
  const button=el('button',title),panel=el('section',null,'task-detail-panel task-evaluation-body');button.type='button';button.id=id+'-tab-'+name;button.dataset.taskTab=name;
  button.setAttribute('role','tab');button.setAttribute('aria-controls',id+'-panel-'+name);
  panel.id=id+'-panel-'+name;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',button.id);panel.tabIndex=0;
  navigation.append(button);holder.append(panel);return {name,button,panel};
 });
 const select=(name,focus=false)=>{taskTabViews.set(owner,name);for(const entry of entries){const selected=entry.name===name;entry.button.setAttribute('aria-selected',String(selected));entry.button.tabIndex=selected?0:-1;entry.panel.hidden=!selected;if(selected&&focus)entry.button.focus();}};
 entries.forEach((entry,index)=>{
  entry.button.addEventListener('click',()=>select(entry.name));
  entry.button.addEventListener('keydown',event=>{const next=event.key==='ArrowRight'?(index+1)%entries.length:event.key==='ArrowLeft'?(index+entries.length-1)%entries.length:event.key==='Home'?0:event.key==='End'?entries.length-1:null;if(next!==null){event.preventDefault();select(entries[next].name,true);}});
 });
 const saved=taskTabViews.get(owner);select(entries.some(entry=>entry.name===saved)?saved:entries[0].name);
 return {holder,panels:Object.fromEntries(entries.map(entry=>[entry.name,entry.panel]))};
}
function taskReadinessList(parent,checks){
 if(!checks?.length){parent.append(el('p','No readiness checks declared.'));return;}
 const list=el('ul',null,'task-readiness-list');for(const check of checks)list.append(el('li',check));parent.append(list);
}
function renderTaskRows(tasks){
 const target=$('task-editor');target.replaceChildren();
 if(!Array.isArray(tasks)||!tasks.length)return;
 tasks.forEach((task,index)=>{
  const article=el('article',null,'task-review-card'),header=el('header',null,'task-review-header'),title=el('div');
  title.append(el('span','Task '+(index+1),'task-eyebrow'),el('h4',task.id||'Scenario task'));
  const meta=el('div',null,'task-meta');meta.append(taskBadge(task.family||'scenario'),taskBadge(task.verification_mode||'exact','mode'),taskBadge(task.split||'development'));
  header.append(title,meta);article.append(header);
  article.append(el('h5','Agent prompt'),el('div',task.prompt||'ScenarioForge supplies this prompt when the scenario is deployed.','task-prompt-review'));
  const {holder,panels}=taskTabs('task-review-'+index,'Task '+(index+1)+' evaluation',task,[['verification','Verification'],['readiness','Readiness'],['hints','Progressive hints']]);
  panels.verification.append(el('p',taskVerificationSummary(task),'task-detail-summary'));
  if(task.rubric?.criteria?.length){
   panels.verification.append(el('h5','Success criteria'));const list=el('ol',null,'task-criteria-list');
   for(const criterion of task.rubric.criteria){const item=el('li');item.append(el('strong',criterion.requirement||criterion.id));if(criterion.evidence)item.append(el('p','Evidence: '+criterion.evidence,'small'));item.append(el('span',criterion.essential===false?'Contributes to partial credit':'Required for success','task-criterion-state'));list.append(item);}panels.verification.append(list);
  }
  if(task.challenge_plan?.steps?.length){panels.verification.append(el('h5','Challenge steps'));const steps=el('ol',null,'task-step-list');for(const step of task.challenge_plan.steps)steps.append(el('li',`${step.title||step.id} · criteria: ${(step.criterion_ids||[]).join(', ')||'none'} · prerequisites: ${(step.requires||[]).join(', ')||'none'}`));panels.verification.append(steps);}
  const exact=task.flag_nodes?{flag_nodes:task.flag_nodes}:task.verifier;
  if(exact){const verifier=el('details',null,'task-private-verifier');verifier.append(el('summary','Private exact verifier (experiment author only)'),el('pre',JSON.stringify(exact,null,2)));panels.verification.append(verifier);}
  panels.readiness.append(el('p','Required checks are evaluated after deployment, before the agent starts.','task-detail-summary'));taskReadinessList(panels.readiness,task.required_checks);
  panels.hints.append(el('p',taskHintSummary(task),'task-detail-summary'));
  for(const group of taskHintGroups(task)){if(group.title)panels.hints.append(el('h5',group.title));const hints=el('ol',null,'task-hint-list');for(const hint of group.hints)hints.append(el('li',hint));panels.hints.append(hints);}
  const solutions=(task.challenge_plan?.steps||[]).filter(step=>step.solution).length;
  if(solutions)panels.hints.append(el('p',`${solutions} challenge ${solutions===1?'walkthrough is':'walkthroughs are'} available. Release timing is set on Hints & solutions.`,'small'));
  const declared=['discovery','starting_facts','discoverable_facts','objective_requires'].filter(key=>key in task);
  if(declared.length){const advanced=el('details',null,'task-private-verifier');advanced.append(el('summary','Discovery and progress declarations'),el('pre',JSON.stringify(Object.fromEntries(declared.map(key=>[key,task[key]])),null,2)));panels.hints.append(advanced);}
  article.append(holder);target.append(article);
 });
}
function renderTaskContext(){
 const target=$('task-scenario-context'),body=$('task-scenario-context-body');body.replaceChildren();
 const context=scenarioTaskContext,chain=Array.isArray(context?.chain)?context.chain:[],files=Array.isArray(context?.bundle_files)?context.bundle_files:[];
 target.hidden=!chain.length&&!files.length;
 if(target.hidden)return;
 if(chain.length){
  const table=el('table',null,'scenario-chain-table'),head=el('thead'),labels=el('tr'),rows=el('tbody'),hasTargets=chain.some(node=>node.ipv4),hasGenerators=chain.some(node=>node.generator);
  for(const label of ['Challenge',...(hasTargets?['Target']:[]),...(hasGenerators?['Generator']:[])])labels.append(el('th',label));head.append(labels);
  for(const [index,node] of chain.entries()){
   const row=el('tr'),name=el('td');name.append(el('strong',`${index+1}. ${node.name||node.id}`));if(node.is_vuln)name.append(el('small','Vulnerability'));
   row.append(name);if(hasTargets)row.append(el('td',node.ipv4||'—'));if(hasGenerators)row.append(el('td',node.generator||'—'));rows.append(row);
  }
  table.append(head,rows);body.append(table);
 }
 if(files.length){body.append(el('h5','Imported files'));const list=el('ul',null,'scenario-imported-files');for(const file of files)list.append(el('li',file));body.append(list);}
}
function renderSampleTaskTabs(){
 const target=$('sample-task-details'),verification=$('sample-verification-fields'),readiness=$('sample-readiness-fields');
 const {holder,panels}=taskTabs('sample-task','Sample evaluation',sampleTaskTabState,[['verification','Verification'],['readiness','Readiness'],['hints','Progressive hints']]);
 panels.verification.append(verification,el('p','Fixed by this sample. The deployed address and fresh expected token or flags are resolved at run time; exact values are captured in Results.','small'));
 panels.readiness.append(readiness,el('p','Required checks are evaluated after deployment, before the agent starts.','small'));
 panels.hints.append(el('p','Sample guidance is resolved with the deployed scenario. Choose whether and when to release it on Hints & solutions.'));
 target.replaceChildren(holder);
}

function renderTaskEditor(){
 renderTaskRows(scenarioTaskDefinitions||[]);
 renderTaskContext();
 $('task-editor-error').textContent=taskValidationMessage();
}
function setTaskLoadStatus(state,text){
 $('task-load-summary').hidden=state==='ready';
 const badge=$('task-load-state');badge.textContent=text;badge.className='badge '+(state==='ready'?'good':state==='error'?'warn':state==='loading'?'running':'');
 $('retry-scenario-tasks').hidden=state!=='error';
}
function resetTaskEditor(){
 taskTabViews.delete(sampleTaskTabState);
 clearTimeout(scenarioTaskLoadTimer);scenarioTaskLoadToken++;scenarioTaskDefinitions=null;scenarioTaskSelection=null;scenarioTaskContext=null;scenarioTaskSuggested=false;scenarioTaskLoadError='';
 $('task-source-note').textContent='Select a scenario to load its task.';setTaskLoadStatus('waiting','Waiting for scenario');
 renderTaskEditor();
}
function configureEvaluationTasks(scenario,sample){
 $('sample-task-info').hidden=scenario;$('custom-task-info').hidden=!scenario;
 $('sample-task-id').value=sample?.id||'';
 $('sample-success').value=sample?.id==='smoke'?'Exact JSON match: service_token must equal the freshly generated token.':'Exact JSON match: flags must contain both freshly generated flags in discovery order.';
 if(!scenario)renderSampleTaskTabs();
 if(scenario)renderTaskEditor();
}
function taskScenarioChanged(){
 clearTimeout(scenarioTaskLoadTimer);scenarioTaskLoadToken++;scenarioTaskDefinitions=null;scenarioTaskSelection=null;scenarioTaskContext=null;scenarioTaskSuggested=false;scenarioTaskLoadError='';renderTaskEditor();
 const selection=$('scenario-selection').value;
 if(selection&&scenarioChoiceVM===snapshot?.roles?.scenarioforge&&$('experiment-sample').value==='scenarioforge-xml'){
  $('task-source-note').textContent='The selected scenario’s task will load automatically.';setTaskLoadStatus('loading','Queued');scheduleScenarioTaskLoad();
 }else{$('task-source-note').textContent='Select a resolved scenario to load its task.';setTaskLoadStatus('waiting','Waiting for scenario');}
 syncCreateExperiment();
}
function taskProgressBytes(value){return Number.isFinite(value)?Number(value).toLocaleString()+' bytes':'size pending';}
async function loadScenarioTaskRequest(selection){
 const requestId=crypto.randomUUID().replaceAll('-','');
 let finished=false,timer=null,last={step:0,total:5,percent:0,message:'Submitting task-draft request…'};
 const started=Date.now();
 const render=status=>{
  if(status)last=status;
  const parts=[`Loading task draft · Step ${last.step||0}/${last.total||5}`,last.message||'Working'];
  if(Number.isFinite(last.source_bytes))parts.push(`Scenario XML: ${taskProgressBytes(last.source_bytes)}`);
  if(Number.isFinite(last.received_bytes))parts.push(`${taskProgressBytes(last.received_bytes)} / ${Number.isFinite(last.total_bytes)?taskProgressBytes(last.total_bytes):'total pending'}`);
  if(Number.isFinite(last.percent))parts.push(Math.round(last.percent)+'%');
  parts.push(Math.floor((Date.now()-started)/1000)+'s elapsed');
  const label=parts.join(' · ');
  operation=label;syncBusy();loadingModal.set('scenario-tasks',true,label,Number.isFinite(last.percent)?last.percent:null);
 };
 const poll=async()=>{
  const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),10000);
  try{
   const response=await apiFetch('/api/scenarios/tasks-status?request_id='+encodeURIComponent(requestId),{cache:'no-store',signal:controller.signal});
   if(response.ok&&!finished)render(await dashboardJSON(response));
  }catch(error){if(!finished)render({...last,message:(last.message||'Loading task draft')+` · progress update unavailable (${error.name==='AbortError'?'timed out':error.message}); retrying`});}
  finally{clearTimeout(deadline);}
  if(!finished)timer=setTimeout(poll,1000);
 };
 render();const heartbeat=setInterval(()=>render(),1000);timer=setTimeout(poll,200);
 try{return await apiFetch('/api/scenarios/tasks',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({selection_id:selection,request_id:requestId})});}
 finally{finished=true;clearTimeout(timer);clearInterval(heartbeat);loadingModal.set('scenario-tasks',false);}
}
function scheduleScenarioTaskLoad(delay=0){
 clearTimeout(scenarioTaskLoadTimer);const token=scenarioTaskLoadToken;
 const attempt=()=>{if(token!==scenarioTaskLoadToken)return;const selection=$('scenario-selection').value;if(!selection||scenarioChoiceVM!==snapshot?.roles?.scenarioforge||$('experiment-sample').value!=='scenarioforge-xml')return;if(isBusy()){scenarioTaskLoadTimer=setTimeout(attempt,250);return;}loadSelectedScenarioTasks(selection,token);};
 scenarioTaskLoadTimer=setTimeout(attempt,delay);
}
async function loadSelectedScenarioTasks(selection,token=scenarioTaskLoadToken){
 if(!selection||scenarioChoiceVM!==snapshot?.roles?.scenarioforge)return;
 scenarioTaskLoadError='';setTaskLoadStatus('loading','Loading automatically');$('task-source-note').textContent='Reading the saved task and ScenarioForge evaluation scaffold…';$('task-editor-error').textContent='';operation='Loading tasks from ScenarioForge…';syncBusy();
 try{
  const response=await loadScenarioTaskRequest(selection),result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Unable to load scenario tasks');
  if(token!==scenarioTaskLoadToken||selection!==$('scenario-selection').value)return;
  if(result.tasks!==null&&!Array.isArray(result.tasks))throw Error('Scenario task definitions must be a JSON array.');
  if(!Array.isArray(result.suggested_tasks||[]))throw Error('Scenario task draft must be a JSON array.');
  scenarioTaskSuggested=result.tasks===null&&(result.suggested_tasks||[]).length>0;
  scenarioTaskDefinitions=result.tasks!==null?result.tasks:(scenarioTaskSuggested?result.suggested_tasks:null);
  if(!scenarioTaskDefinitions?.length)throw Error('ScenarioForge could not build a task from this scenario. Save a resolved Flow with evaluation guidance, then reload the scenario.');
  validateTaskList(scenarioTaskDefinitions);scenarioTaskContext=result.context||null;scenarioTaskSelection=selection;
  const source=scenarioTaskSuggested?'Generated from the resolved Flow':'Saved with the scenario';
  const count=scenarioTaskDefinitions.length;$('task-source-note').textContent=`${source} · ${count} ${count===1?'task':'tasks'}. Review the agent prompt and evaluation summary below.`;
  setTaskLoadStatus('ready','Ready');renderTaskEditor();
 }catch(error){if(token===scenarioTaskLoadToken){scenarioTaskLoadError=error.message;$('task-source-note').textContent='The task could not be loaded automatically.';$('task-editor-error').textContent=error.message;setTaskLoadStatus('error','Needs attention');renderTaskEditor();}}
 finally{operation=null;syncBusy();syncCreateExperiment();schedulePoll();}
}
function initTaskEditor(){
 $('retry-scenario-tasks').addEventListener('click',()=>{scenarioTaskLoadError='';scenarioTaskLoadToken++;scheduleScenarioTaskLoad();syncCreateExperiment();});
}

function validateRubric(rubric){
 if(rubric?.version!==1||!Array.isArray(rubric.criteria)||!rubric.criteria.length||rubric.criteria.length>32)throw Error('Rubric needs version 1 and 1–32 criteria.');
 const ids=new Set();for(const c of rubric.criteria){if(!c.id||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(c.id)||ids.has(c.id))throw Error('Criterion IDs must be valid and unique.');ids.add(c.id);if(!c.requirement?.trim()||!c.evidence?.trim())throw Error('Each criterion needs a requirement and evidence description.');if(typeof (c.essential??true)!=='boolean'||!Number.isFinite(c.weight??1)||(c.weight??1)<=0||(c.weight??1)>100)throw Error('Invalid criterion weight or essential setting.');}
 if(!rubric.criteria.some(c=>c.essential!==false))throw Error('At least one criterion must be essential.');return rubric;
}
