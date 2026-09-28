/* Text-only rendering keeps guest command lines and journal labels inert. */
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node;};
let snapshot = null, fetching = false, failed = false, rolesDirty = false, rolesSaving = false, sampleStarting = false;
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';} return node;}
function detail(list,label,value) {const row=el('div');row.append(el('dt',label),el('dd',value));list.append(row);}
function render(data) {
 snapshot=data; renderRoles(data); renderSamples(data); $('workflow').textContent=data.workflow_id||'';
 const notice=$('notice'); const errors=(data.errors||[]).map(x=>x.error);notice.hidden=!errors.length;notice.textContent=errors.join(' · ');
 const cards=$('machines');cards.replaceChildren();
 if(!data.vms.length)cards.append(el('div','Checking configured machines…','empty'));
 for(const vm of data.vms){
  const card=el('article',null,'card'), top=el('div',null,'card-top'), label=el('div');
  label.append(el('h3',vm.label),el('div',vm.vmid ? `VM ${vm.vmid}${vm.name?' · '+vm.name:''}`:'No VM selected','vm-id'));
  const power=vm.qmp_status==='paused'?'paused':vm.power;
  top.append(el('span',vm.role==='core'?'◈':'▤','vm-icon'),label,badge(power,power==='running'?'good':power==='unknown'?'warn':''));card.append(top);
  const list=el('dl');detail(list,'Guest agent',vm.guest_access);if(vm.qmp_status && vm.qmp_status!==vm.power)detail(list,'Emulator state',vm.qmp_status);detail(list,'Application',vm.application_present==null?'Not checked':vm.application_present?'Present':'Not found at configured location');
  detail(list,'Processes observed',vm.guest_access==='reachable'?String(vm.processes.length):'Unknown');
  card.append(list);
  for(const service of vm.services||[]){if(service.unit===vm.unit){card.append(el('p',`${service.unit} · ${service.error?'Check unavailable':service.LoadState==='not-found'?'not found':service.ActiveState+' / '+service.SubState}`,'small'));}}
  if(vm.error||vm.guest_error)card.append(el('div',vm.error||vm.guest_error,'error'));
  if(!vm.vmid)card.append(el('p',data.roles?'Choose this VM in your lab roles above.':'Set monitoring.core_vmid in your workflow YAML.','small'));
  card.append(el('div','APPLICATION & TOOL PROCESSES','process-heading'));
  const processes=el('div',null,'processes');
  for(const proc of vm.processes){const box=el('div',null,'process'), meta=el('div',null,'process-top');meta.append(el('span',`PID ${proc.pid}`),timer(proc.elapsed_seconds,vm.observed_at||data.checked_at,true));box.append(meta,el('code',proc.command));processes.append(box);}
  if(!vm.processes.length)processes.append(el('p',vm.guest_access==='reachable'?'No matching processes observed.':'Process state not available.','empty'));
  if(vm.processes_truncated)processes.append(el('p','Showing the first 40 matching processes.','small'));
  card.append(processes);cards.append(card);
 }
 const jobs=(data.vms||[]).flatMap(vm=>(vm.jobs||[]).map(job=>({...job,observed_at:vm.observed_at||data.checked_at})));
 $('command-count').textContent=`${jobs.length} recorded job${jobs.length===1?'':'s'}`; const commands=$('commands');commands.replaceChildren();
 for(const job of jobs){const row=el('article',null,'command'),head=el('div',null,'command-head');head.append(badge(job.live_state,job.live_state==='running'?'good':job.live_state==='unconfirmed'?'warn':''),el('span',`${job.run} / ${job.stage}`,'command-title'),timer(job.live_state==='finished'?null:job.elapsed_seconds,job.observed_at,job.live_state==='running'));row.append(head,el('code',job.command),el('div',`VM ${job.vmid} · ${job.unit} · ${job.live_state==='finished'?'No longer running; journal awaits cleanup':job.elapsed_source||'Last recorded job; live state unconfirmed'}`,'small'));commands.append(row);}
 if(!jobs.length)commands.append(el('p','No unfinished workflow commands recorded. Applications may still be running above.','empty'));
 const runs=$('runs');runs.replaceChildren(); $('no-runs').hidden=Boolean(data.runs.length);
 for(const run of data.runs){const row=el('tr'),evaluation=run.evaluation,summary=evaluation?.summary;const title=el('td'); if(data.owner){const id=run.output.split('/').pop();const button=el('button',run.workflow_id||id);button.type='button';button.addEventListener('click',()=>showResults(id));title.append(button,el('div',id,'small'));if(run.message)title.append(el('div',run.message,'small'));}else{title.textContent=run.workflow_id||run.output;}row.append(title);const state=el('td');state.append(badge(run.recorded_status||'read error',run.recorded_status==='completed'?'good':run.error?'warn':''));row.append(state,el('td',run.coordinator_active?'Active':'Idle'),el('td',evaluation?`${summary.trials_observed} / ${evaluation.planned_trials}`:'Not started'),el('td',summary?String(summary.verified_successes):'—'));if(run.error)row.title=run.error;runs.append(row);}
 tick();
}
function tick(){const age=snapshot?.checked_at?Math.max(0,(Date.now()-Date.parse(snapshot.checked_at))/1000):null;const stale=failed||(age!=null&&age>Math.max(60,(snapshot.poll_seconds||10)*3));$('connection').textContent=failed?'Dashboard unavailable':stale?'Observation is stale':snapshot?.checked_at?'Monitoring lab':'Checking machines…';$('pulse').className='dot'+(stale||!snapshot?.checked_at?' muted':'');$('checked').textContent=age==null?'Waiting for first check':`Checked ${duration(age)} ago${snapshot.refreshing?' · refreshing':''}`;for(const clock of document.querySelectorAll('[data-seconds]')){let seconds=Number(clock.dataset.seconds);if(clock.dataset.live==='yes'&&!stale&&clock.dataset.observed)seconds+=Math.max(0,(Date.now()-Date.parse(clock.dataset.observed))/1000);clock.textContent=duration(seconds);clock.title=stale?'Last observation; refresh required':'Elapsed time since process start';}}
async function refresh(){if(fetching)return;fetching=true;try{const response=await fetch('/api/status',{cache:'no-store'});if(response.status===401){clearPrivateView();location.replace('/login');return;}if(!response.ok)throw Error(response.status===504?'The dashboard request timed out (HTTP 504)':response.status===403?'Access not granted':`HTTP ${response.status}`);const data=await response.json();failed=false;render(data);}catch(error){failed=true;clearPrivateView();$('notice').hidden=false;$('notice').textContent=`Dashboard unavailable: ${error.message}. Refresh to try again.`;tick();}finally{fetching=false;}}
$('refresh').addEventListener('click',refresh);setInterval(refresh,3000);setInterval(tick,1000);refresh();

let csrfToken=null;
async function loadSession(){const response=await fetch('/api/session',{cache:'no-store'});if(response.status===401){location.replace('/login');return;}if(!response.ok)throw Error('Session unavailable');const session=await response.json();csrfToken=session.csrf;$('username-label').textContent=session.username;}
$('sign-out').addEventListener('click',async()=>{try{if(!csrfToken)await loadSession();const response=await fetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});if(response.ok||response.status===401){location.replace('/login');return;}throw Error('Sign out failed');}catch(error){$('notice').hidden=false;$('notice').textContent='Unable to sign out. Please try again.';}});
loadSession().catch(()=>{$('notice').hidden=false;$('notice').textContent='Unable to load your session. Refresh to try again.';});

function clearPrivateView(){snapshot=null;$('machines').replaceChildren();$('commands').replaceChildren();$('runs').replaceChildren();$('result-content').textContent='';$('result-panel').hidden=true;$('result-summary').replaceChildren();$('download-dataset').hidden=true;$('samples-panel').hidden=true;$('sample-cards').replaceChildren();for(const role of ['scenarioforge','participant','core'])$('role-'+role).replaceChildren();$('role-panel').hidden=true;}
function renderRoles(data){
 const panel=$('role-panel');panel.hidden=!data.roles;if(!data.roles)return;
 for(const role of ['scenarioforge','participant','core']){
  const select=$('role-'+role),wanted=rolesDirty?select.value:String(data.roles[role]??'');
  select.replaceChildren(new Option('Not selected',''));
  for(const vm of data.available_vms||[])select.add(new Option(`VM ${vm.vmid} · ${vm.name||'Unnamed'} · ${vm.status||'unknown'}${vm.pool?' · '+vm.pool:''}`,String(vm.vmid)));
  select.value=[...select.options].some(o=>o.value===wanted)?wanted:'';
 }
 if(!rolesDirty&&!rolesSaving)$('roles-message').textContent=data.available_vms.length?'Selections are private to your account. Changes apply to subsequent runs.':'No available QEMU VMs on this node. Check your PVE VM/pool permissions.';
}
for(const role of ['scenarioforge','participant','core'])$('role-'+role).addEventListener('change',()=>{rolesDirty=true;});
$('role-form').addEventListener('submit',async event=>{
 event.preventDefault();if(rolesSaving)return;rolesSaving=true;$('save-roles').disabled=true;
 try{
  if(!csrfToken)await loadSession();
  const roles=Object.fromEntries(['scenarioforge','participant','core'].map(role=>[role,$('role-'+role).value?Number($('role-'+role).value):null]));
  const response=await fetch('/api/roles',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(roles)});
  const body=await response.json().catch(()=>({}));if(!response.ok)throw Error(response.status===504?'Saving VM roles timed out. Refresh to check whether they were saved.':body.error||'Unable to save roles');
  rolesDirty=false;$('roles-message').textContent='VM roles saved. Refreshing your dashboard…';await refresh();
 }catch(error){$('roles-message').textContent=error.message;}finally{rolesSaving=false;$('save-roles').disabled=false;}
});
async function showResults(id){
 $('result-panel').hidden=false;$('result-content').textContent='Loading results…';$('result-summary').replaceChildren();$('download-dataset').hidden=true;
 try{const response=await fetch(`/api/runs/${encodeURIComponent(id)}/results`,{cache:'no-store'});if(!response.ok)throw Error('Results unavailable or access not granted');const data=await response.json();renderResultSummary(data);$('result-content').textContent=JSON.stringify(data,null,2);if(data.evaluation){$('download-dataset').href=`/api/runs/${encodeURIComponent(id)}/dataset.csv`;$('download-dataset').hidden=false;}}
 catch(error){$('result-content').textContent=error.message;}
}
$('close-results').addEventListener('click',()=>{$('result-panel').hidden=true;$('result-content').textContent='';});

function renderSamples(data){
 const samples=data.samples, panel=$('samples-panel');panel.hidden=!samples?.items?.length;if(panel.hidden)return;
 const vm=samples.participant_vmid;
 $('samples-context').textContent=vm?`Participant VM ${vm} · ${samples.provider} / ${samples.model}. Uses the saved VM selection and model settings. These samples use no ScenarioForge export.`:'Save a Cyber-agent-flow VM selection above to run a sample.';
 const busy=(data.runs||[]).some(run=>run.sample_id&&(run.coordinator_active||run.recorded_status==='queued'));
 const cards=$('sample-cards');cards.replaceChildren();
 for(const sample of samples.items){
  const card=el('article',null,'card sample-card');card.append(el('h3',sample.name),el('p',sample.description,'small'));
  card.append(el('p',`${sample.trials} trial${sample.trials===1?'':'s'} · up to ${sample.max_turns} turns and ${sample.wall_seconds}s per trial`,'sample-budget'));
  if(sample.id==='tools-vs-helper')card.append(el('p','The helper is a bundled example artifact. The demo site is created automatically inside the participant and cleaned up afterward.','small'));
  const button=el('button',sampleStarting?'Starting…':busy?'Sample running…':'Run sample');button.type='button';button.disabled=!vm||sampleStarting||busy;button.dataset.sampleId=sample.id;
  button.addEventListener('click',()=>startSample(sample.id));card.append(button);cards.append(card);
 }
}
async function startSample(id){
 if(sampleStarting)return;
 if(rolesDirty){$('sample-message').textContent='Save your VM role changes before starting a sample.';return;}
 sampleStarting=true;if(snapshot)renderSamples(snapshot);$('sample-message').textContent='Starting sample…';
 try{
  const requestId=crypto.randomUUID().replaceAll('-','');
  if(!csrfToken)await loadSession();
  const response=await fetch('/api/samples/run',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({sample_id:id,request_id:requestId})});
  const data=await response.json().catch(()=>({}));if(!response.ok)throw Error(data.error||`Request failed (HTTP ${response.status}); refresh to check your runs before retrying`);
  $('sample-message').textContent=`Started ${data.run_id}. Follow progress in Experiment runs below.`;await refresh();
 }catch(error){$('sample-message').textContent=error.message;}
 finally{sampleStarting=false;if(snapshot)renderSamples(snapshot);}
}
function renderResultSummary(data){
 const target=$('result-summary');target.replaceChildren();
 const workflow=data.workflow||{};target.append(el('p',`${workflow.recorded_status||'Unknown'} · ${workflow.message||''}`));
 if(workflow.error)target.append(el('p',workflow.error,'error'));
 if(!data.evaluation){target.append(el('p','Trial results will appear here once evaluation starts.','small'));return;}
 const groups=Object.entries(data.evaluation.conditions||{});
 const table=el('table'),head=el('tr');for(const text of ['Condition','Verified successes','Mean runtime','First flag'])head.append(el('th',text));const heading=el('thead');heading.append(head);table.append(heading);const body=el('tbody');
 for(const [name,summary] of groups){const row=el('tr');row.append(el('td',name),el('td',`${summary.verified_successes??0} / ${summary.verified_trials??0}`),el('td',duration(summary.mean_execution_seconds)),el('td',duration(summary.mean_time_to_first_flag_seconds)));body.append(row);}table.append(body);const wrapper=el('div',null,'scroll');wrapper.append(table);target.append(wrapper);
}
