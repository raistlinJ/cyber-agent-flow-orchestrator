/* Task definitions remain separate from participant prompts and private verifiers. */
let customTaskRows=[],scenarioTaskDefinitions=null,scenarioTaskSelection=null,scenarioTaskContext=null,scenarioTaskSuggested=false;
const taskFields=new Set(['id','family','prompt','verifier','flag_nodes','required_checks','split','discovery','starting_facts','discoverable_facts','objective_requires','progressive_hints','rubric','verification_mode','challenge_plan']);
const readinessChecks=[
 ['containers','Workloads started'],['services','Expected services running'],['ports','Expected ports listening'],
 ['injects','Required files placed'],['segmentation','Firewall rules applied'],['traffic','Traffic agents running'],
 ['reachability','Configured traffic reaches its target'],['flow_pivot','Flow pivot path works'],
 ['pivot_access','Participant can reach pivot providers']
];
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
function taskRow(task){
 const {id='',family='custom',prompt='',required_checks=['containers','services','ports'],verifier,flag_nodes,progressive_hints,rubric,verification_mode='exact',split='development',...extra}=task;
 return {id,family,prompt,split,mode:verification_mode,rubric:JSON.stringify(rubric||defaultRubric(prompt)),hints:(progressive_hints||[]).join('\n'),hintsDeclared:progressive_hints!==undefined,checks:required_checks.join(', '),criteria:JSON.stringify(flag_nodes?{flag_nodes}:verifier||{type:'json_equals',expected:{}},null,2),extra:JSON.stringify(extra,null,2)};
}
function readTaskRows(){return [...$('task-editor').querySelectorAll('[data-task-row]')].map(row=>({...Object.fromEntries(['id','family','prompt','hints','checks','criteria','extra','mode','rubric','split'].map(key=>[key,row.querySelector('[data-task-field="'+key+'"]').value])),hintsDeclared:row.dataset.hintsDeclared==='true'}));}
function parseTaskRows(rows){
 return validateTaskList(rows.map((row,index)=>{
  let criteria,extra;try{criteria=row.mode==='judge'?{}:JSON.parse(row.criteria);extra=JSON.parse(row.extra||'{}');}catch{throw Error('Task '+(index+1)+': success criteria and advanced settings must be valid JSON.');}
  if(!criteria||Array.isArray(criteria)||typeof criteria!=='object'||!extra||Array.isArray(extra)||typeof extra!=='object')throw Error('Task '+(index+1)+': criteria and advanced settings must be JSON objects.');
  if(Object.keys(extra).some(k=>!['split','discovery','starting_facts','discoverable_facts','objective_requires','progressive_hints','challenge_plan'].includes(k)))throw Error('Task '+(index+1)+': unsupported advanced field.');
  const scoring=row.mode==='judge'?{}:'flag_nodes' in criteria?criteria:{verifier:criteria};
  const review=row.mode&&row.mode!=='exact'?{verification_mode:row.mode,rubric:JSON.parse(row.rubric)}:{verification_mode:'exact',...(extra.challenge_plan?{rubric:JSON.parse(row.rubric)}:{})};
  if('flag_nodes' in criteria&&Object.keys(criteria).length!==1)throw Error('Task '+(index+1)+': flag_nodes criteria cannot contain other fields.');
  const hints=(row.hints||'').split('\n').map(h=>h.trim()).filter(Boolean);
  const hintFields=hints.length||row.hintsDeclared?{progressive_hints:hints}:{};
  return {...extra,...hintFields,...review,split:row.split||extra.split||'development',id:row.id.trim(),family:row.family.trim(),...(row.prompt.trim()?{prompt:row.prompt}:{}),required_checks:row.checks.split(',').map(s=>s.trim()).filter(Boolean),...scoring};
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
  const fieldset=el('fieldset',null,'task-card');fieldset.dataset.taskRow=String(index);fieldset.dataset.hintsDeclared=String(Boolean(row.hintsDeclared));fieldset.disabled=!editable;
  fieldset.append(el('legend','Task '+(index+1)));
  appendRubricEditor(fieldset,row,index);
  for(const [key,label,multiline] of [['id','Task ID',false],['family','Task family',false],['prompt','Task prompt',true],['hints','Progressive hints (one per line)',true],['criteria','Success verification',true],['extra','Advanced settings (JSON)',true]]){
   const input=el(multiline?'textarea':'input');input.id='task-'+index+'-'+key;input.dataset.taskField=key;input.value=row[key];if(multiline)input.rows=key==='prompt'?5:3;
   const caption=el('label',label);caption.htmlFor=input.id;fieldset.append(caption,input);
   if(key==='hints'){input.addEventListener('input',()=>{fieldset.dataset.hintsDeclared='true';});fieldset.append(el('p','Optional guidance released when the agent stalls. Use one hint per line; exclude flags, solutions and verifier answers.','small'));}
   if(key==='prompt')fieldset.append(el('p','Participant-facing instructions. A scenario draft uses the guides, solutions and challenge graph. Review its objectives; Judge mode permits a free-form evidence report.','small'));
   if(key==='criteria'){input.disabled=row.mode==='judge';
    const help=el('p',criteriaDescription(row.criteria),'small criteria-description');
    input.addEventListener('input',()=>{help.textContent=criteriaDescription(input.value);});
    fieldset.append(help,el('p','Formats: {"flag_nodes":["node-id"]}, {"type":"json_equals","expected":{...}}, or {"type":"contains_all","expected":["text"]}.','small'));
   }
   if(key==='extra')fieldset.append(el('p','Optional discovery and fact declarations. A challenge_plan maps rubric criteria to steps and prerequisites for progress monitoring; keep its criterion IDs in sync when editing the rubric.','small'));
  }
  try{const plan=JSON.parse(row.extra||'{}').challenge_plan;if(plan){const detail=el('details');detail.append(el('summary','Intermediate challenge scaffold · '+plan.steps.length+' steps'));for(const step of plan.steps)detail.append(el('p',step.id+' · '+step.title+' · criteria: '+step.criterion_ids.join(', ')+' · prerequisites: '+(step.requires.join(', ')||'none'),'small'));detail.append(el('p','Private solutions stay on the evaluator host. Review these generated objectives before running.','small'));fieldset.append(detail);}}catch{}
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
function syncTaskSourceTooltip(){
 const source=$('task-source'),option=source.selectedOptions[0];
 if(option?.title)source.title=option.title;
}
function renderTaskEditor(){
 syncTaskSourceTooltip();
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
 $('task-source').addEventListener('change',()=>{if($('task-source').value==='custom'&&!customTaskRows.length)customTaskRows=(scenarioTaskDefinitions||[{id:'task-1',family:'custom',verification_mode:'judge',rubric:defaultRubric('')}]).map(taskRow);renderTaskEditor();syncCreateExperiment();});
 $('add-task').addEventListener('click',()=>{let number=1;while(customTaskRows.some(row=>row.id==='task-'+number))number++;customTaskRows.push(taskRow({id:'task-'+number,family:'custom',verification_mode:'judge',rubric:defaultRubric('')}));renderTaskEditor();syncCreateExperiment();});
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

function defaultRubric(prompt){return {version:1,criteria:[{id:'complete',requirement:prompt||'Complete the challenge objective.',evidence:'Successful tool output demonstrating the required result; a claim of completion alone is insufficient.',essential:true,weight:1}]};}
function validateRubric(rubric){
 if(rubric?.version!==1||!Array.isArray(rubric.criteria)||!rubric.criteria.length||rubric.criteria.length>32)throw Error('Rubric needs version 1 and 1–32 criteria.');
 const ids=new Set();for(const c of rubric.criteria){if(!c.id||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(c.id)||ids.has(c.id))throw Error('Criterion IDs must be valid and unique.');ids.add(c.id);if(!c.requirement?.trim()||!c.evidence?.trim())throw Error('Each criterion needs a requirement and evidence description.');if(typeof (c.essential??true)!=='boolean'||!Number.isFinite(c.weight??1)||(c.weight??1)<=0||(c.weight??1)>100)throw Error('Invalid criterion weight or essential setting.');}
 if(!rubric.criteria.some(c=>c.essential!==false))throw Error('At least one criterion must be essential.');return rubric;
}
function appendRubricEditor(fieldset,row,index){
 const split=el('select');split.dataset.taskField='split';for(const name of ['development','validation','test'])split.add(new Option(name,name));split.value=row.split||'development';const splitLabel=el('label','Dataset split');splitLabel.append(split);fieldset.append(splitLabel,el('p','Use a specific scenario family above. A family must stay in one split across a study.','small'));

 const modeLabel=el('label','Verification mode');modeLabel.htmlFor='task-'+index+'-mode';
 const mode=el('select');mode.id=modeLabel.htmlFor;mode.dataset.taskField='mode';for(const [value,label] of [['exact','Exact checks'],['judge','Judge review'],['both','Exact checks and judge review']])mode.add(new Option(label,value));mode.value=row.mode||'exact';fieldset.append(modeLabel,mode);
 const stored=el('input');stored.type='hidden';stored.dataset.taskField='rubric';stored.value=row.rubric||JSON.stringify(defaultRubric(row.prompt));fieldset.append(stored);
 const review=el('section',null,'rubric-editor');fieldset.append(review);
 const render=()=>{review.replaceChildren();review.hidden=mode.value==='exact';let rubric;try{rubric=JSON.parse(stored.value);}catch{rubric=defaultRubric(row.prompt);}
  review.append(el('p','Essential criteria must be satisfied for success. Other criteria contribute partial credit. Private references are sent only to the judge.','small'));
  rubric.criteria.forEach((criterion,n)=>{const card=el('div',null,'rubric-criterion');card.append(el('h4','Criterion '+(n+1)));
   for(const [key,label] of [['id','Criterion ID'],['requirement','Required result'],['evidence','Acceptable evidence'],['weight','Weight'],['private_reference','Private reference / solution (optional)']]){const input=el(['requirement','evidence','private_reference'].includes(key)?'textarea':'input');input.value=criterion[key]??(key==='weight'?1:'');if(key==='weight'){input.type='number';input.min='0.01';input.max='100';input.step='0.01';}const caption=el('label',label);caption.append(input);card.append(caption);input.addEventListener('input',()=>{criterion[key]=key==='weight'?Number(input.value):input.value;stored.value=JSON.stringify(rubric);});}
   const essential=el('input');essential.type='checkbox';essential.checked=criterion.essential!==false;const label=el('label',' Essential for success');label.prepend(essential);essential.addEventListener('change',()=>{criterion.essential=essential.checked;stored.value=JSON.stringify(rubric);});card.append(label);
   const remove=el('button','Remove criterion');remove.type='button';remove.addEventListener('click',()=>{rubric.criteria.splice(n,1);stored.value=JSON.stringify(rubric);render();customTaskRows=readTaskRows();syncCreateExperiment();});card.append(remove);review.append(card);
  });
  const add=el('button','Add criterion');add.type='button';add.addEventListener('click',()=>{let n=1;while(rubric.criteria.some(c=>c.id==='criterion-'+n))n++;rubric.criteria.push({id:'criterion-'+n,requirement:'',evidence:'',essential:true,weight:1});stored.value=JSON.stringify(rubric);render();customTaskRows=readTaskRows();syncCreateExperiment();});review.append(add);
 };
 mode.addEventListener('change',()=>{const exact=fieldset.querySelector('[data-task-field="criteria"]');if(exact)exact.disabled=mode.value==='judge';render();customTaskRows=readTaskRows();syncCreateExperiment();});render();
}
