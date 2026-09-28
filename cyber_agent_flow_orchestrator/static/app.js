/* Text-only rendering keeps guest command lines and journal labels inert. */
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node;};
let snapshot = null, fetching = false, failed = false, rolesDirty = false, rolesSaving = false, sampleStarting = false;
let initialized=false, operation=null, waitingForObservation=false, waitingForMaintenance=false, redirecting=false, maintenanceStarting=false;
let refreshPromise=null, sessionPromise=null, csrfToken=null, wasBusy=false, busySince=performance.now();
let blockingRefresh=false, foregroundChecks=true, pollTimer=null;
const refreshPeriods=['0','1','2','5','10'];
try{const saved=localStorage.getItem('caf-refresh-minutes');if(refreshPeriods.includes(saved))$('refresh-period').value=saved;}catch{}
function isBusy(){return !initialized||blockingRefresh||Boolean(operation)||foregroundChecks||waitingForMaintenance||redirecting;}
function syncBusy(){
 const busy=isBusy(), loading=busy||fetching||waitingForObservation;if(loading&&!wasBusy)busySince=performance.now();wasBusy=loading;
 $('dashboard-controls').disabled=busy;$('dashboard-controls').setAttribute('aria-busy',String(busy));$('sign-out').disabled=busy;
 const bar=$('loading-progress');bar.hidden=false;$('loading-status').classList.toggle('complete',!loading&&!failed);
 let label, percent=null;
 if(redirecting)label='Opening sign-in…';
 else if(operation)label=operation;
 else if(waitingForMaintenance)label=(snapshot?.updates?.jobs||[]).find(job=>['queued','running'].includes(job.status))?.message||'Retrieving application state…';
 else if(waitingForObservation){if(snapshot?.loading){const info=snapshot.loading;percent=info.percent;label=`${info.status} · ${info.completed}/${info.total} VM checks complete · ${percent}%`;}else label='Checking VM power, guest access and applications…';}
 else if(fetching||!initialized)label=csrfToken?'Retrieving dashboard and verifying VM access…':'Checking your login session…';
 else if(failed){label='Loading failed. Use Refresh view to try again.';bar.hidden=true;}
 else{label='Dashboard ready · 100%';percent=100;}
 if(percent==null)bar.removeAttribute('value');else bar.value=percent;
 $('loading-label').textContent=!busy&&loading?`Background refresh · ${label} · controls available`:label;$('loading-elapsed').textContent=loading?`${Math.floor((performance.now()-busySince)/1000)}s elapsed`:'';
 syncUpdateControls(busy?label:null);
}
function sessionExpired(){redirecting=true;clearPrivateView();syncBusy();location.replace('/login');throw Error('Your session expired. Sign in again.');}
async function loadSession(){
 if(sessionPromise)return sessionPromise;
 sessionPromise=(async()=>{const response=await fetch('/api/session',{cache:'no-store'});if(response.status===401)sessionExpired();if(!response.ok)throw Error('Unable to verify your session. Refresh to retry.');const session=await response.json();csrfToken=session.csrf;$('username-label').textContent=session.username;})();
 try{await sessionPromise;}finally{sessionPromise=null;}
}
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';} return node;}
function detail(list,label,value) {const row=el('div');row.append(el('dt',label),el('dd',value));list.append(row);}
function render(data) {
 snapshot=data; renderRoles(data); renderSamples(data); renderUpdates(data); $('workflow').textContent=data.workflow_id||'';
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
 waitingForMaintenance=Boolean((data.updates?.jobs||[]).some(job=>['queued','running'].includes(job.status)));waitingForObservation=Boolean(data.refreshing&&(data.loading?data.loading.total>0:(!data.checked_at||data.vms.length>0)));if(!waitingForObservation)foregroundChecks=false;tick();syncBusy();
}
function tick(){const age=snapshot?.checked_at?Math.max(0,(Date.now()-Date.parse(snapshot.checked_at))/1000):null;const stale=failed||(age!=null&&age>Math.max(60,Number($('refresh-period').value)*120));$('connection').textContent=failed?'Dashboard unavailable':stale?'Observation is stale':snapshot?.checked_at?'Monitoring lab':'Checking machines…';$('pulse').className='dot'+(stale||!snapshot?.checked_at?' muted':'');$('checked').textContent=age==null?'Waiting for first check':`Checked ${duration(age)} ago${snapshot.refreshing?' · refreshing':''}`;for(const clock of document.querySelectorAll('[data-seconds]')){let seconds=Number(clock.dataset.seconds);if(clock.dataset.live==='yes'&&!stale&&clock.dataset.observed)seconds+=Math.max(0,(Date.now()-Date.parse(clock.dataset.observed))/1000);clock.textContent=duration(seconds);clock.title=stale?'Last observation; refresh required':'Elapsed time since process start';}}
async function refresh(force=false, background=false){
 if(refreshPromise)return refreshPromise;
 clearTimeout(pollTimer);fetching=true;blockingRefresh=!background;if(force&&!background)foregroundChecks=true;syncBusy();
 refreshPromise=(async()=>{
  try{
   if(!csrfToken){await loadSession();syncBusy();}
   const response=await fetch(force?'/api/status?refresh=1':'/api/status?refresh=0',{cache:'no-store'});
   if(response.status===401)sessionExpired();
   if(!response.ok)throw Error(response.status===504?'The dashboard request timed out (HTTP 504)':response.status===403?'Access not granted':`HTTP ${response.status}`);
   const data=await response.json();failed=false;render(data);return true;
  }catch(error){failed=true;foregroundChecks=false;waitingForObservation=false;clearPrivateView();$('notice').hidden=false;$('notice').textContent=`Dashboard unavailable: ${error.message}. Refresh to try again.`;tick();return false;}
  finally{fetching=false;blockingRefresh=false;initialized=true;syncBusy();}
 })();
 try{return await refreshPromise;}finally{refreshPromise=null;schedulePoll();}
}
$('refresh').addEventListener('click',async()=>{
 if(isBusy())return;operation='Refreshing dashboard…';syncBusy();
 try{await finishDashboardRead(true);await refresh(true);}
 catch(error){if(!redirecting){$('notice').hidden=false;$('notice').textContent=error.message;}}
 finally{operation=null;syncBusy();schedulePoll();}
});
async function finishDashboardRead(allowFailure=false){
 if(refreshPromise&&!await refreshPromise&&!allowFailure)throw Error('Dashboard refresh failed. Refresh the view before retrying.');
 if(redirecting)throw Error('Your session expired. Sign in again.');
}
$('dashboard-controls').addEventListener('click',event=>{if(isBusy()&&!event.target.closest('summary')){event.preventDefault();event.stopImmediatePropagation();}},true);
$('sign-out').addEventListener('click',async()=>{
 if(isBusy())return;operation='Signing out…';syncBusy();
 try{await finishDashboardRead();const response=await fetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});if(response.ok||response.status===401){redirecting=true;clearPrivateView();location.replace('/login');return;}throw Error('Sign out failed');}
 catch(error){$('notice').hidden=false;$('notice').textContent='Unable to sign out. Please try again.';}
 finally{operation=null;syncBusy();schedulePoll();}
});

function clearPrivateView(){snapshot=null;waitingForObservation=false;waitingForMaintenance=false;$('updates-panel').hidden=true;$('update-cards').replaceChildren();$('update-jobs').replaceChildren();$('machines').replaceChildren();$('commands').replaceChildren();$('runs').replaceChildren();$('result-content').textContent='';$('result-panel').hidden=true;$('result-summary').replaceChildren();$('download-dataset').hidden=true;$('samples-panel').hidden=true;$('sample-cards').replaceChildren();for(const role of ['scenarioforge','participant','core'])$('role-'+role).replaceChildren();$('role-panel').hidden=true;}
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
 event.preventDefault();if(isBusy()||rolesSaving)return;rolesSaving=true;operation='Saving VM roles and checking access…';syncBusy();
 try{
  await finishDashboardRead();
  if(!csrfToken)await loadSession();
  const roles=Object.fromEntries(['scenarioforge','participant','core'].map(role=>[role,$('role-'+role).value?Number($('role-'+role).value):null]));
  const response=await fetch('/api/roles',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(roles)});
  const body=await response.json().catch(()=>({}));if(!response.ok)throw Error(response.status===504?'Saving VM roles timed out. Refresh to check whether they were saved.':body.error||'Unable to save roles');
  rolesDirty=false;operation='Loading your updated VM selections…';syncBusy();$('roles-message').textContent='VM roles saved. Refreshing your dashboard…';await refresh(true);
 }catch(error){$('roles-message').textContent=error.message;}finally{rolesSaving=false;operation=null;syncBusy();schedulePoll();}
});
async function showResults(id){
 if(isBusy())return;operation='Loading experiment results…';syncBusy();
 $('result-panel').hidden=false;$('result-content').textContent='Loading results…';$('result-summary').replaceChildren();$('download-dataset').hidden=true;
 try{await finishDashboardRead();const response=await fetch(`/api/runs/${encodeURIComponent(id)}/results`,{cache:'no-store'});if(!response.ok)throw Error('Results unavailable or access not granted');const data=await response.json();renderResultSummary(data);$('result-content').textContent=JSON.stringify(data,null,2);if(data.evaluation){$('download-dataset').href=`/api/runs/${encodeURIComponent(id)}/dataset.csv`;$('download-dataset').hidden=false;}}
 catch(error){$('result-content').textContent=error.message;}
 finally{operation=null;syncBusy();schedulePoll();}
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
 if(isBusy()||sampleStarting)return;
 if(rolesDirty){$('sample-message').textContent='Save your VM role changes before starting a sample.';return;}
 sampleStarting=true;operation='Submitting sample experiment…';syncBusy();if(snapshot)renderSamples(snapshot);$('sample-message').textContent='Starting sample…';
 try{
  await finishDashboardRead();
  const requestId=crypto.randomUUID().replaceAll('-','');
  if(!csrfToken)await loadSession();
  const response=await fetch('/api/samples/run',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({sample_id:id,request_id:requestId})});
  const data=await response.json().catch(()=>({}));if(!response.ok)throw Error(data.error||`Request failed (HTTP ${response.status}); refresh to check your runs before retrying`);
  $('sample-message').textContent=`Started ${data.run_id}. Follow progress in Experiment runs below.`;await refresh();
 }catch(error){$('sample-message').textContent=error.message;}
 finally{sampleStarting=false;operation=null;if(snapshot)renderSamples(snapshot);syncBusy();schedulePoll();}
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

const maintenanceRefs={};
function syncUpdateControls(loadingLabel){
 const updates=snapshot?.updates;if(!updates)return;
 for(const app of updates.applications){
  const status=$(`update-status-${app.role}`);if(!status)continue;
  const permission=updates.can_update?'':`Update permission required: your signed-in account must belong to the ${updates.group} PVE group. Orchestration access alone does not grant update access.`;
  const selection=app.vmid?'':'Select and save a VM for this application above.';
  const waiting=loadingLabel?`Please wait: ${loadingLabel}`:'';
  status.textContent=[permission,selection,waiting].filter(Boolean).join(' ')||'Ready to check, update or roll back this application.';
  status.classList.toggle('error',Boolean(permission));
  const input=document.querySelector(`[data-update-ref="${app.role}"]`);
  input.disabled=Boolean(permission||selection||waiting);
  for(const button of document.querySelectorAll(`[data-update-role="${app.role}"]`)){
   const reason=[button.dataset.updateAction==='inspect'?'':permission,selection,waiting].filter(Boolean).join(' ');
   button.disabled=Boolean(reason);button.title=reason;
  }
 }
}
function renderUpdates(data){
 const updates=data.updates;$('updates-panel').hidden=!updates;if(!updates)return;
 $('updates-context').textContent=updates.can_update?'Updates preserve local data and reuse installed dependencies. The application must be idle; its configured service is restarted when necessary.':`Check installed versions here. Updates and rollback require membership in the ${updates.group} PVE group.`;
 const cards=$('update-cards');
 // Preserve input focus while the dashboard polls.
 if(!document.activeElement?.matches('#update-cards input[data-update-ref]')){
  cards.replaceChildren();
  for(const app of updates.applications){
   const card=el('article',null,'card update-card');card.append(el('h3',app.role==='participant'?'Cyber-agent-flow':'ScenarioForge'),el('p',app.vmid?`VM ${app.vmid}`:'Choose and save a VM above','small'));
   const known=(updates.jobs||[]).find(job=>job.role===app.role&&job.vmid===app.vmid&&job.installed);
   if(known){const info=known.installed;card.append(el('p',`Last checked: ${info.revision.slice(0,12)}${info.modified?' · local edits':''}`,'small'));if(info.missing_controls?.length)card.append(el('p',`Missing evaluator controls: ${info.missing_controls.join(', ')}`,'error'));}
   const label=el('label','Branch, tag or commit','small'),input=el('input');input.type='text';input.value=maintenanceRefs[app.role]||app.ref;input.dataset.updateRef=app.role;input.addEventListener('input',()=>{maintenanceRefs[app.role]=input.value;});label.append(input);card.append(label);
   const status=el('p',null,'small');status.id=`update-status-${app.role}`;card.append(status);input.setAttribute('aria-describedby',status.id);
   const actions=el('div',null,'update-actions');
   for(const [action,title] of [['inspect','Check version'],['update','Update'],['rollback','Roll back']]){const button=el('button',title);button.type='button';button.dataset.updateRole=app.role;button.dataset.updateAction=action;button.setAttribute('aria-describedby',status.id);button.addEventListener('click',()=>maintain(app.role,action,input.value));actions.append(button);}card.append(actions);cards.append(card);
  }
 }
 const jobs=$('update-jobs');jobs.replaceChildren();
 for(const job of updates.jobs||[]){const row=el('article',null,'command');row.append(badge(job.status,job.status==='completed'?'good':job.status==='failed'?'warn':''),el('span',` ${job.role} · VM ${job.vmid} · ${job.action}`,'command-title'),el('p',job.message,'small'));if(job.revision)row.append(el('code',job.revision));if(job.error)row.append(el('p',job.error,'error'));const details=el('details'),summary=el('summary','Details');details.append(summary,el('pre',JSON.stringify(job,null,2)));row.append(details);jobs.append(row);}
 if(!(updates.jobs||[]).length)jobs.append(el('p','No application maintenance recorded for your account.','empty'));
}
async function maintain(role,action,ref){
 if(isBusy()||maintenanceStarting)return;
 if(rolesDirty){$('update-message').textContent='Save VM role changes before application maintenance.';return;}
 maintenanceStarting=true;operation='Submitting application maintenance…';syncBusy();if(snapshot)renderUpdates(snapshot);
 try{
  await finishDashboardRead();
  if(!csrfToken)await loadSession();
  const response=await fetch('/api/applications',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role,action,ref,request_id:crypto.randomUUID().replaceAll('-','')})});
  const result=await response.json().catch(()=>({}));if(!response.ok)throw Error(result.error||`Maintenance request failed (HTTP ${response.status})`);
  $('update-message').textContent=`Maintenance ${result.id} submitted. Follow its status below.`;await refresh();
 }catch(error){$('update-message').textContent=error.message;}
 finally{maintenanceStarting=false;operation=null;if(snapshot)renderUpdates(snapshot);syncBusy();schedulePoll();}
}

// Progress reads collect an existing check/job without starting another VM probe.
// Idle automatic refreshes start a new batch at the selected cadence.
function schedulePoll(){
 clearTimeout(pollTimer);
 if(fetching||operation||redirecting)return;
 const active=Boolean(snapshot?.refreshing||waitingForMaintenance||(snapshot?.runs||[]).some(run=>run.coordinator_active||run.recorded_status==='queued'));
 const minutes=Number($('refresh-period').value);
 if(!active&&!minutes)return;
 pollTimer=setTimeout(()=>{if(!fetching&&!operation&&!redirecting)refresh(!active,true);},active?5000:minutes*60000);
}
$('refresh-period').addEventListener('change',()=>{try{localStorage.setItem('caf-refresh-minutes',$('refresh-period').value);}catch{}schedulePoll();tick();});
setInterval(()=>{tick();syncBusy();},1000);
syncBusy();refresh(true);
