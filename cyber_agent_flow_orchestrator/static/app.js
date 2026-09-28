/* Text-only rendering keeps guest command lines and journal labels inert. */
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node;};
let snapshot = null, fetching = false, failed = false, rolesDirty = false, rolesSaving = false, sampleStarting = false;
let initialized=false, operation=null, waitingForObservation=false, waitingForMaintenance=false, redirecting=false, maintenanceStarting=false;
let refreshPromise=null, sessionPromise=null, csrfToken=null, wasBusy=false, busySince=performance.now();
let blockingRefresh=false, foregroundChecks=true, pollTimer=null;
const clientLog=[];
const pages={overview:['Overview','Live machines, application processes, and workflow activity.'],experiments:['Experiments','Run a sample, follow its progress, and explore or export results.'],applications:['Applications','Check versions, update application source, and review maintenance.'],setup:['Lab setup','Choose your virtual machines and set your refresh preferences.']};
function routePage(focus=false){
 const route=location.hash.slice(1),page=Object.hasOwn(pages,route)?route:'overview';
 if(page!==route)history.replaceState(null,'','#'+page);
 for(const section of document.querySelectorAll('[data-page]'))section.hidden=section.dataset.page!==page;
 for(const link of document.querySelectorAll('[data-route]')){if(link.dataset.route===page)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}
 $('page-title').textContent=pages[page][0];$('page-intro').textContent=pages[page][1];document.title=pages[page][0]+' · CAF Orchestrator';
 if(focus){$('page-title').focus({preventScroll:true});window.scrollTo(0,0);}
}
window.addEventListener('hashchange',()=>routePage(true));routePage();
// Keep one console node mounted across routes; reserve its measured height so
// the last control on every page remains reachable above the fixed dock.
new ResizeObserver(()=>document.body.style.setProperty('--console-height',$('debug-console').getBoundingClientRect().height+'px')).observe($('debug-console'));
function logClient(kind,message){clientLog.push({at:new Date().toISOString(),kind,message});if(clientLog.length>100)clientLog.shift();renderConsole();}
function renderConsole(){
 const entries=[...clientLog];
 for(const run of (snapshot?.runs||[]).filter(r=>r.sample_progress).sort((a,b)=>(b.sample_progress.started_at||'').localeCompare(a.sample_progress.started_at||'')).slice(0,3))for(const event of run.sample_progress?.events||[])entries.push({...event,message:`[${run.sample_id} ${run.output.split('/').pop()}] ${event.message}`});
 for(const job of snapshot?.updates?.jobs||[])for(const event of job.console?.events||[])entries.push({...event,message:`[${job.role} VM ${job.vmid} ${job.action} ${job.id.slice(0,8)}] ${event.message}`});
 entries.sort((a,b)=>a.at.localeCompare(b.at));
 const output=$('console-output'),follow=output.scrollHeight-output.scrollTop-output.clientHeight<40;
 output.textContent=entries.slice(-300).map(event=>`${event.at} [${event.kind}] ${event.message}`).join('\n')||'Waiting for activity…';
 if(follow)output.scrollTop=output.scrollHeight;
}
async function apiFetch(path,options={}){
 const start=performance.now(),label=`${options.method||'GET'} ${path}`;logClient('request',label);
 try{const response=await fetch(path,options);logClient('response',`${label} → HTTP ${response.status} (${((performance.now()-start)/1000).toFixed(2)}s)`);return response;}
 catch(error){logClient('error',`${label} → network request failed (${((performance.now()-start)/1000).toFixed(2)}s)`);throw error;}
}
try{$('debug-console').open=localStorage.getItem('caf-console-open')!=='false';}catch{}
document.querySelector('#debug-console summary').addEventListener('click',event=>{event.preventDefault();$('debug-console').open=!$('debug-console').open;try{localStorage.setItem('caf-console-open',String($('debug-console').open));}catch{}});
$('debug-console').addEventListener('toggle',()=>{try{localStorage.setItem('caf-console-open',String($('debug-console').open));}catch{}});
$('download-console').addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([$('console-output').textContent],{type:'text/plain'})),link=el('a');link.href=url;link.download='orchestrator-console.log';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
const refreshPeriods=['0','1','2','5','10'];
try{const saved=localStorage.getItem('caf-refresh-minutes');if(refreshPeriods.includes(saved))$('refresh-period').value=saved;}catch{}
function isBusy(){return !initialized||blockingRefresh||Boolean(operation)||foregroundChecks||waitingForMaintenance||redirecting;}
function syncBusy(){
 const busy=isBusy(), loading=busy||fetching||waitingForObservation;if(loading&&!wasBusy)busySince=performance.now();wasBusy=loading;
 $('dashboard-controls').disabled=busy;$('dashboard-controls').setAttribute('aria-busy',String(busy));$('sign-out').disabled=busy;
 const lastChange=(snapshot?.updates?.jobs||[]).find(job=>job.action!=='inspect'&&(snapshot.updates.applications||[]).some(app=>app.role===job.role&&app.vmid===job.vmid));
 const maintenanceFailed=['failed','interrupted'].includes(lastChange?.status);
 const bar=$('loading-progress');bar.hidden=false;$('loading-status').classList.toggle('complete',!loading&&!failed&&!maintenanceFailed);
 $('loading-status').classList.toggle('maintenance-failed',!loading&&maintenanceFailed);
 let label, percent=null;
 if(redirecting)label='Opening sign-in…';
 else if(operation)label=operation;
 else if(waitingForMaintenance){const job=(snapshot?.updates?.jobs||[]).find(job=>['queued','running'].includes(job.status));label=job?.message||'Retrieving application state…';const transfer=job?.console?.transfer;if(transfer&&!transfer.verified&&label.startsWith('Transferring verified'))label+=` · ${transfer.percent}% of file acknowledged (${transfer.sent_bytes}/${transfer.total_bytes} bytes)`;}
 else if(waitingForObservation){if(snapshot?.loading){const info=snapshot.loading;percent=info.percent;label=`${info.status} · ${info.completed}/${info.total} VM checks complete · ${percent}%`;}else label='Checking VM power, guest access and applications…';}
 else if(fetching||!initialized)label=csrfToken?'Retrieving dashboard and verifying VM access…':'Checking your login session…';
 else if(failed){label='Loading failed. Use Refresh view to try again.';bar.hidden=true;}
 else{label=maintenanceFailed?`Dashboard loaded · ${lastChange.role} ${lastChange.action} ${lastChange.status}; see Applications`:'Dashboard loaded';bar.hidden=true;}
 if(percent==null)bar.removeAttribute('value');else bar.value=percent;
 $('loading-label').textContent=!busy&&loading?`Background refresh · ${label} · controls available`:label;$('loading-elapsed').textContent=loading?`${Math.floor((performance.now()-busySince)/1000)}s elapsed`:'';
 syncUpdateControls(busy?label:null);
}
function sessionExpired(){redirecting=true;clearPrivateView();syncBusy();location.replace('/login');throw Error('Your session expired. Sign in again.');}
async function loadSession(){
 if(sessionPromise)return sessionPromise;
 sessionPromise=(async()=>{const response=await apiFetch('/api/session',{cache:'no-store'});if(response.status===401)sessionExpired();if(!response.ok)throw Error('Unable to verify your session. Refresh to retry.');const session=await response.json();csrfToken=session.csrf;$('username-label').textContent=session.username;})();
 try{await sessionPromise;}finally{sessionPromise=null;}
}
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live, sample=false) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';if(sample)node.dataset.source='sample';} return node;}
function detail(list,label,value) {const row=el('div');row.append(el('dt',label),el('dd',value));list.append(row);}
function render(data) {
 snapshot=data; renderRoles(data); renderSamples(data); renderSampleActivity(data); renderUpdates(data); renderConsole();$('workflow').textContent=data.workflow_id||'';
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
  if(!vm.vmid)card.append(el('p',data.roles?'Choose this VM on the Lab setup page.':'Set monitoring.core_vmid in your workflow YAML.','small'));
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
function tick(){const age=snapshot?.checked_at?Math.max(0,(Date.now()-Date.parse(snapshot.checked_at))/1000):null;const stale=failed||(age!=null&&age>Math.max(60,Number($('refresh-period').value)*120));$('connection').textContent=failed?'Dashboard unavailable':stale?'Observation is stale':snapshot?.checked_at?'Monitoring lab':'Checking machines…';$('pulse').className='dot'+(stale||!snapshot?.checked_at?' muted':'');$('checked').textContent=age==null?'Waiting for first check':`Checked ${duration(age)} ago${snapshot.refreshing?' · refreshing':''}`;for(const clock of document.querySelectorAll('[data-seconds]')){let seconds=Number(clock.dataset.seconds);if(clock.dataset.live==='yes'&&(!stale||clock.dataset.source==='sample')&&clock.dataset.observed)seconds+=Math.max(0,(Date.now()-Date.parse(clock.dataset.observed))/1000);clock.textContent=duration(seconds);clock.title=stale&&clock.dataset.source!=='sample'?'Last observation; refresh required':clock.dataset.source==='sample'?'Elapsed time since sample or trial start':'Elapsed time since process start';}}
async function refresh(force=false, background=false){
 if(refreshPromise)return refreshPromise;
 clearTimeout(pollTimer);fetching=true;blockingRefresh=!background;if(force&&!background)foregroundChecks=true;syncBusy();
 refreshPromise=(async()=>{
  try{
   if(!csrfToken){await loadSession();syncBusy();}
   const response=await apiFetch(force?'/api/status?refresh=1':'/api/status?refresh=0',{cache:'no-store'});
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
 try{await finishDashboardRead();const response=await apiFetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});if(response.ok||response.status===401){redirecting=true;clearPrivateView();location.replace('/login');return;}throw Error('Sign out failed');}
 catch(error){$('notice').hidden=false;$('notice').textContent='Unable to sign out. Please try again.';}
 finally{operation=null;syncBusy();schedulePoll();}
});

function clearPrivateView(){$('sample-global-status').hidden=true;$('sample-global-status').replaceChildren();$('sample-activity-panel').hidden=true;$('sample-activity').replaceChildren();snapshot=null;clientLog.length=0;renderConsole();waitingForObservation=false;waitingForMaintenance=false;$('updates-panel').hidden=true;$('update-message').textContent='';$('update-cards').replaceChildren();$('update-jobs').replaceChildren();$('machines').replaceChildren();$('commands').replaceChildren();$('runs').replaceChildren();$('result-content').textContent='';$('result-panel').hidden=true;$('result-summary').replaceChildren();$('download-dataset').hidden=true;$('samples-panel').hidden=true;$('sample-cards').replaceChildren();for(const role of ['scenarioforge','participant','core'])$('role-'+role).replaceChildren();$('role-panel').hidden=true;}
function renderRoles(data){
 $('roles-unavailable').hidden=Boolean(data.roles);
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
  const response=await apiFetch('/api/roles',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(roles)});
  const body=await response.json().catch(()=>({}));if(!response.ok)throw Error(response.status===504?'Saving VM roles timed out. Refresh to check whether they were saved.':body.error||'Unable to save roles');
  rolesDirty=false;operation='Loading your updated VM selections…';syncBusy();$('roles-message').textContent='VM roles saved. Refreshing your dashboard…';await refresh(true);
 }catch(error){$('roles-message').textContent=error.message;}finally{rolesSaving=false;operation=null;syncBusy();schedulePoll();}
});
async function showResults(id){
 if(isBusy())return;operation='Loading experiment results…';syncBusy();
 $('result-panel').hidden=false;$('result-content').textContent='Loading results…';$('result-summary').replaceChildren();$('download-dataset').hidden=true;
 try{await finishDashboardRead();const response=await apiFetch(`/api/runs/${encodeURIComponent(id)}/results`,{cache:'no-store'});if(!response.ok)throw Error('Results unavailable or access not granted');const data=await response.json();renderResultSummary(data);$('result-content').textContent=JSON.stringify(data,null,2);if(data.evaluation){$('download-dataset').href=`/api/runs/${encodeURIComponent(id)}/dataset.csv`;$('download-dataset').hidden=false;}}
 catch(error){$('result-content').textContent=error.message;}
 finally{operation=null;syncBusy();schedulePoll();}
}
$('close-results').addEventListener('click',()=>{$('result-panel').hidden=true;$('result-content').textContent='';});

function renderSamples(data){
 const samples=data.samples, panel=$('samples-panel');panel.hidden=!samples?.items?.length;if(panel.hidden)return;
 const vm=samples.participant_vmid;
 $('samples-context').textContent=vm?`Participant VM ${vm} · ${samples.provider} / ${samples.model}. Uses the saved VM selection and model settings. These samples use no ScenarioForge export.`:'Select and save a Cyber-agent-flow VM on Lab setup to run a sample.';
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
function renderSampleActivity(data){
 const recent=(data.runs||[]).filter(r=>r.sample_progress).sort((a,b)=>(b.sample_progress.started_at||'').localeCompare(a.sample_progress.started_at||''));
 const active=recent.filter(r=>r.sample_progress.active),shown=active.length?active:recent.slice(0,1);
 const global=$('sample-global-status');global.replaceChildren();global.hidden=!active.length;
 if(active.length){const run=active[0],p=run.sample_progress,link=el('a','View sample activity →');link.href='#experiments';global.append(document.createTextNode(`${p.name} · ${run.recorded_status} · ${p.finished_trials}/${p.planned_trials} trials finished. `),link);}
 const target=$('sample-activity');
 const expanded=new Set([...target.querySelectorAll('details[open]')].map(n=>n.dataset.run));
 target.replaceChildren();$('sample-activity-panel').hidden=!shown.length;
 for(const run of shown){
  const p=run.sample_progress,trial=p.current_trial,transport=trial?.transport;
  const card=el('article',null,'panel sample-activity'),head=el('div',null,'result-actions');head.append(el('h3',p.name),badge(run.recorded_status,run.recorded_status==='completed'?'good':['failed','interrupted','completed_with_errors'].includes(run.recorded_status)?'warn':''));
  const button=el('button','Open results');button.type='button';button.addEventListener('click',()=>showResults(run.output.split('/').pop()));head.append(button);card.append(head);
  card.append(el('p',run.recorded_status==='interrupted'?'Coordinator is no longer active. Inspect results before retrying.':transport?.activity||run.message||p.phase,'sample-stage'));
  const progress=el('progress');progress.max=100;progress.value=p.percent??0;progress.setAttribute('aria-label',`${p.name} trials finished`);
  card.append(el('p',`${p.finished_trials} / ${p.planned_trials} trials finished · ${p.percent??0}% · ${p.verified_successes} verified successes · ${p.errors} trial errors`,'small'),progress);
  card.append(el('p','Percentage counts finished trials, including errors. Cleanup and finalization may still be pending at 100%.','small'));
  const elapsed=el('p',null,'small');elapsed.append(document.createTextNode('Run elapsed: '),timer(p.elapsed_seconds,p.observed_at,p.active,true));card.append(elapsed);
  if(trial){
   card.append(el('p',`${trial.trial_id} · ${trial.condition_id} · repetition ${trial.repetition} · attempt ${trial.attempt}`));
   const timing=el('p',null,'small');timing.append(document.createTextNode('Trial elapsed (includes setup and collection): '),timer(trial.elapsed_seconds,p.observed_at,p.active,true));card.append(timing);
   if(transport?.files_total)card.append(el('p',`Inputs transferred: ${transport.files_uploaded} / ${transport.files_total} files · ${(transport.bytes_uploaded??0).toLocaleString()} / ${(transport.bytes_total??0).toLocaleString()} bytes acknowledged`,'small'));
   if(transport?.service?.SubState)card.append(el('p',`Last observed guest service: ${transport.service.SubState}${transport.service.ExecMainPID?' · PID '+transport.service.ExecMainPID:''}`,'small'));
   if(transport?.updated_at)card.append(el('p',`Guest stage last recorded: ${new Date(transport.updated_at).toLocaleTimeString()}`,'small'));
  }
  card.append(el('p',`Per-trial limits: ${p.max_turns} turns · ${p.wall_seconds}s worker budget. Live model tokens and tool calls are not streamed; scores appear after collection.`,'small'));
  if(p.trials.length){const wrap=el('div',null,'scroll'),table=el('table'),header=el('tr'),thead=el('thead'),body=el('tbody');for(const title of ['Trial','Condition','Status','Verified','Score'])header.append(el('th',title));thead.append(header);for(const trial of p.trials){const row=el('tr');for(const value of [trial.trial_id,trial.condition_id,trial.status,trial.verified_success===true?'Yes':trial.verified_success===false?'No':trial.status==='running'?'Pending':'Unavailable',trial.score??'—'])row.append(el('td',value));body.append(row);}table.append(thead,body);wrap.append(table);card.append(wrap);}
  if(run.error)card.append(el('p',run.error,'error'));
  const details=el('details'),summary=el('summary','Recent sample events');details.dataset.run=run.output;details.open=expanded.has(run.output);details.append(summary,el('pre',p.events.slice(-12).map(e=>`${e.at} · ${e.message}`).join('\n')));card.append(details);target.append(card);
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
  const response=await apiFetch('/api/samples/run',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({sample_id:id,request_id:requestId})});
  const data=await response.json().catch(()=>({}));if(!response.ok)throw Error(data.error||`Request failed (HTTP ${response.status}); refresh to check your runs before retrying`);
  $('sample-message').textContent=`Started ${data.run_id}. Follow progress in Sample activity.`;await refresh();if(location.hash==='#experiments'&&!$('sample-activity-panel').hidden)$('sample-activity-panel').scrollIntoView({block:'start'});
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
  const selection=app.vmid?'':'Select and save a VM for this application on Lab setup.';
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
 const updates=data.updates;$('updates-panel').hidden=!updates;$('applications-unavailable').hidden=Boolean(updates);if(!updates)return;
 $('maintenance-count').textContent=`${updates.jobs?.length||0} recent jobs`;
 $('updates-context').textContent=updates.can_update?'Updates preserve local data and reuse installed dependencies. The application must be idle; its configured service is restarted when necessary.':`Check installed versions here. Updates and rollback require membership in the ${updates.group} PVE group.`;
 const cards=$('update-cards');
 // Preserve input focus while the dashboard polls.
 if(!document.activeElement?.matches('#update-cards input[data-update-ref]')){
  cards.replaceChildren();
  for(const app of updates.applications){
   const card=el('article',null,'card update-card');card.append(el('h3',app.role==='participant'?'Cyber-agent-flow':'ScenarioForge'),el('p',app.vmid?`VM ${app.vmid}`:'Choose and save a VM on Lab setup','small'));
   const known=(updates.jobs||[]).find(job=>job.role===app.role&&job.vmid===app.vmid&&job.installed);
   if(known){const info=known.installed;card.append(el('p',`Installed revision (last checked): ${info.revision.slice(0,12)}${info.modified?' · local edits':''}`,'small'));if(info.missing_controls?.length)card.append(el('p',`Missing evaluator controls: ${info.missing_controls.join(', ')}`,'error'));
    if(info.processes?.length){card.append(el('p','Processes referencing this checkout (up to 12):','small'));for(const process of info.processes)card.append(el('p',`PID ${process.pid} · ${process.name} · ${process.reason}`,'small'));card.append(el('p','Update asks for confirmation before stopping these processes. A configured service is restarted after activation.','small'));}
    if(info.modified){card.append(el('p',info.tools_config_replaceable?'Legacy kali_tools.json change detected. Update will back up and replace this runtime catalog with shipped defaults.':'Tracked local edits block updates and rollback. Preserve/commit them in the VM, then check again.','error'));if(info.modified_files?.length){card.append(el('pre',info.modified_files.join('\n'),'small'));card.append(el('p',`Showing ${info.modified_files.length} of ${info.modified_file_count??info.modified_files.length} changed files (paths limited to 300 characters).`,'small'));}}
   }
   const label=el('label','Branch, tag or commit','small'),input=el('input');input.type='text';input.value=maintenanceRefs[app.role]||app.ref;input.dataset.updateRef=app.role;input.addEventListener('input',()=>{maintenanceRefs[app.role]=input.value;});label.append(input);card.append(label);
   const status=el('p',null,'small');status.id=`update-status-${app.role}`;card.append(status);input.setAttribute('aria-describedby',status.id);
   const actions=el('div',null,'update-actions');
   for(const [action,title] of [['inspect','Check version'],['update','Update'],['rollback','Roll back']]){const button=el('button',title);button.type='button';button.dataset.updateRole=app.role;button.dataset.updateAction=action;button.setAttribute('aria-describedby',status.id);button.addEventListener('click',()=>maintain(app.role,action,input.value,action==='update'&&known?.installed.processes?.length?known:null));actions.append(button);}
   card.append(actions);cards.append(card);
  }
 }
 const jobs=$('update-jobs');jobs.replaceChildren();
 const latest=updates.jobs?.[0];
 if(latest){$('update-message').textContent=`Latest maintenance · ${latest.role} · VM ${latest.vmid} · ${latest.action}: ${latest.status}. ${latest.error||latest.message}`;$('update-message').classList.toggle('error',['failed','interrupted'].includes(latest.status));}
 for(const job of updates.jobs||[]){const row=el('article',null,'command');row.append(badge(job.status,job.status==='completed'?'good':job.status==='failed'?'warn':''),el('span',` ${job.role} · VM ${job.vmid} · ${job.action}`,'command-title'),el('p',job.message,'small'));if(job.console?.transfer){const transfer=job.console.transfer,progress=el('progress');progress.max=100;progress.value=transfer.percent;progress.setAttribute('aria-label',`${job.role} source bundle upload`);row.append(el('p',`${transfer.sent_bytes.toLocaleString()} / ${transfer.total_bytes.toLocaleString()} bytes acknowledged · ${transfer.percent}% · ${(transfer.bytes_per_second/1024).toFixed(1)} KiB/s · ${transfer.verified?'Upload checksum verified; see activation outcome below':['queued','running'].includes(job.status)?'Awaiting verified completion':'Transfer stopped; file not verified'}`,'small'),progress);if(!transfer.verified)row.append(el('p',`${transfer.sent_bytes?'Last acknowledgement':'Transfer started'}: ${transfer.updated_at}. Open the troubleshooting console below for command history.`,'small'));}if(job.revision)row.append(el('p',`Target revision: ${job.revision}`,'small'));if(job.action!=='inspect')row.append(el('p',job.status==='completed'?'Activation completed':['failed','interrupted'].includes(job.status)?'Activation not confirmed. Check version for the current installed revision.':'Activation pending','small'));if(job.error)row.append(el('p',job.error,'error'));const details=el('details'),summary=el('summary','Details');details.append(summary,el('pre',JSON.stringify(job,null,2)));row.append(details);jobs.append(row);}
 if(!(updates.jobs||[]).length)jobs.append(el('p','No application maintenance recorded for your account.','empty'));
}
async function maintain(role,action,ref,confirmation=null){
 if(isBusy()||maintenanceStarting)return;
 if(rolesDirty){$('update-message').textContent='Save VM role changes before application maintenance.';return;}
 if(confirmation&&!confirmation.installed.processes.every(p=>/^[0-9a-f]{64}$/.test(p.identity||''))){$('update-message').textContent='Click Check version to refresh the process list, then click Update to confirm stopping it.';return;}
 if(confirmation&&!window.confirm(`Stop these ${role} processes on VM ${confirmation.vmid} and update to ${ref}?\n\n${confirmation.installed.processes.map(p=>`PID ${p.pid} · ${p.name} · ${p.reason}`).join('\n')}\n\nThis sends SIGTERM. Listed terminal windows/sessions may close and unsaved work may be lost. Unmanaged applications will not restart automatically. Changed processes require a fresh confirmation.`))return;
 maintenanceStarting=true;operation='Submitting application maintenance…';syncBusy();if(snapshot)renderUpdates(snapshot);
 try{
  await finishDashboardRead();
  if(!csrfToken)await loadSession();
  const response=await apiFetch('/api/applications',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role,action,ref,request_id:crypto.randomUUID().replaceAll('-',''),...(confirmation?{process_confirmation:confirmation.id}:{})})});
  const result=await response.json().catch(()=>({}));if(!response.ok)throw Error(result.error||`Maintenance request failed (HTTP ${response.status})`);
  $('update-message').textContent=`Maintenance ${result.id} submitted. Follow its status below.`;await refresh();
 }catch(error){$('update-message').textContent=error.message;}
 finally{maintenanceStarting=false;operation=null;syncBusy();schedulePoll();}
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
