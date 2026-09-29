/* Text-only rendering keeps guest command lines and journal labels inert. */
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node;};
let snapshot = null, fetching = false, failed = false, rolesDirty = false, rolesSaving = false, sampleStarting = false;
let initialized=false, operation=null, waitingForObservation=false, waitingForMaintenance=false, redirecting=false, maintenanceStarting=false;
let refreshPromise=null, sessionPromise=null, csrfToken=null, wasBusy=false, busySince=performance.now();
let experimentCreating=false;
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
 for(const run of (snapshot?.runs||[]).filter(r=>r.sample_progress).sort((a,b)=>(b.sample_progress.started_at||'').localeCompare(a.sample_progress.started_at||'')).slice(0,3))for(const event of run.sample_progress?.events||[])entries.push({...event,message:`[${run.sample_id||run.scenario_experiment?.scenario} ${run.output.split('/').pop()}] ${event.message}`});
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
function autoRefreshEnabled(){return Number($('refresh-period').value)>0;}
function isBusy(){return !initialized||blockingRefresh||Boolean(operation)||(autoRefreshEnabled()&&(foregroundChecks||waitingForMaintenance))||redirecting;}
function syncBusy(){
 const busy=isBusy(), loading=busy||fetching||(autoRefreshEnabled()&&waitingForObservation);if(loading&&!wasBusy)busySince=performance.now();wasBusy=loading;
 $('experiment-controls').disabled=busy;$('cancel-experiment').disabled=Boolean(operation)||experimentCreating;
 $('dashboard-controls').disabled=busy;$('dashboard-controls').setAttribute('aria-busy',String(busy));$('sign-out').disabled=busy;
 const lastChange=(snapshot?.updates?.jobs||[]).find(job=>job.action!=='inspect'&&(snapshot.updates.applications||[]).some(app=>app.role===job.role&&app.vmid===job.vmid));
 const maintenanceFailed=['failed','interrupted'].includes(lastChange?.status);
 const bar=$('loading-progress');bar.hidden=false;$('loading-status').classList.toggle('complete',!loading&&!failed&&!maintenanceFailed);
 $('loading-status').classList.toggle('maintenance-failed',!loading&&maintenanceFailed);
 let label, percent=null;
 if(redirecting)label='Opening sign-in…';
 else if(operation)label=operation;
 else if(autoRefreshEnabled()&&waitingForMaintenance){const job=(snapshot?.updates?.jobs||[]).find(job=>['queued','running'].includes(job.status));label=job?.message||'Retrieving application state…';const transfer=job?.console?.transfer;if(transfer&&!transfer.verified&&label.startsWith('Transferring verified'))label+=` · ${transfer.percent}% of file acknowledged (${transfer.sent_bytes}/${transfer.total_bytes} bytes)`;}
 else if(autoRefreshEnabled()&&waitingForObservation){if(snapshot?.loading){const info=snapshot.loading;percent=info.percent;label=`${info.status} · ${info.completed}/${info.total} VM checks complete · ${percent}%`;}else label='Checking VM power, guest access and applications…';}
 else if(fetching||!initialized)label=csrfToken?'Retrieving dashboard and verifying VM access…':'Checking your login session…';
 else if(failed){label='Loading failed. Use Refresh view to try again.';bar.hidden=true;}
 else{label=maintenanceFailed?`Dashboard loaded · ${lastChange.role} ${lastChange.action} ${lastChange.status}; see Applications`:'Dashboard loaded';bar.hidden=true;}
 if(percent==null)bar.removeAttribute('value');else bar.value=percent;
 $('loading-label').textContent=!busy&&loading?`Background refresh · ${label}`:label;$('loading-elapsed').textContent=loading?`${Math.floor((performance.now()-busySince)/1000)}s elapsed`:'';
 loadingModal.set('dashboard',loading,label,percent);
 syncUpdateControls(busy?label:null);
 syncCreateExperiment();
}
function sessionExpired(){redirecting=true;clearPrivateView();syncBusy();location.replace('/login');throw Error('Your session expired. Sign in again.');}
async function loadSession(){
 if(sessionPromise)return sessionPromise;
 sessionPromise=(async()=>{const response=await apiFetch('/api/session',{cache:'no-store'});if(response.status===401)sessionExpired();if(!response.ok)throw Error('Unable to verify your session. Refresh to retry.');const session=await dashboardJSON(response);csrfToken=session.csrf;$('username-label').textContent=session.username;})();
 try{await sessionPromise;}finally{sessionPromise=null;}
}
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live, sample=false) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';if(sample)node.dataset.source='sample';} return node;}
function detail(list,label,value) {const row=el('div');row.append(el('dt',label),el('dd',value));list.append(row);}
function render(data) {
 snapshot=data; renderRoles(data); renderModelConfigs(data); renderSamples(data); renderSampleActivity(data); renderUpdates(data); renderConsole();$('workflow').textContent=data.workflow_id||'';
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
 renderExperiments(data);
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
   const data=await dashboardJSON(response);failed=false;render(data);return true;
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

function clearPrivateView(){$('window-notice').hidden=true;$('window-notice').replaceChildren();$('sample-global-status').hidden=true;$('sample-global-status').replaceChildren();snapshot=null;modelDrafts={};$('model-config-cards').replaceChildren();$('model-config-panel').hidden=true;clientLog.length=0;renderConsole();waitingForObservation=false;waitingForMaintenance=false;$('updates-panel').hidden=true;$('update-message').textContent='';$('update-cards').replaceChildren();$('update-jobs').replaceChildren();$('machines').replaceChildren();$('commands').replaceChildren();$('runs').replaceChildren();$('experiment-dialog').close();$('experiment-sample').replaceChildren();$('new-experiment').disabled=true;for(const role of ['scenarioforge','participant','core'])$('role-'+role).replaceChildren();$('role-panel').hidden=true;}
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
  const body=await dashboardJSON(response);if(!response.ok)throw Error(response.status===504?'Saving VM roles timed out. Refresh to check whether they were saved.':body.error||'Unable to save roles');
  rolesDirty=false;operation='Loading your updated VM selections…';syncBusy();$('roles-message').textContent='VM roles saved. Refreshing your dashboard…';await refresh(true);
 }catch(error){$('roles-message').textContent=error.message;}finally{rolesSaving=false;operation=null;syncBusy();schedulePoll();}
});
let creationSteps=[];
function creationProgress(index,state,message){
 creationSteps[index]={state,message};const done=creationSteps.filter(step=>['complete','skipped'].includes(step.state)).length;
 $('experiment-save-progress').hidden=false;$('experiment-save-bar').value=done;
 $('experiment-save-status').textContent=state==='failed'?`Creation stopped · ${done} of 3 steps completed`:`${Math.round(done/3*100)}% · ${done} of 3 steps completed`;
 for(const [i,id] of ['local','push','record'].entries()){const step=creationSteps[i];if(step){$('experiment-save-'+id).textContent=step.message;$('experiment-save-'+id).dataset.state=step.state;}}
}
function startCreationProgress(saveModel){
 creationSteps=[{state:'pending',message:'Save file locally on orchestrator · pending'},{state:'pending',message:'Push settings to VM · pending'},{state:'pending',message:'Save experiment · pending'}];
 $('experiment-save-note').textContent=saveModel?'VM transfer uses one small guest-agent request: 0% until acknowledged, then 100%. Overall progress counts completed steps, not elapsed time.':'Using saved defaults; no model file needs to be pushed.';
 creationProgress(0,saveModel?'running':'skipped',saveModel?'Saving file locally on orchestrator…':'Local settings · using saved defaults');
 if(!saveModel)creationProgress(1,'skipped','VM push · not needed');
}
let modelDrafts={};
function renderModelConfigs(data){
 $('model-config-panel').hidden=!data.owner;
 if(!data.owner)return;
 for(const role of ['participant']){
  const vmid=data.roles?.[role];let card=$('model-config-'+role);
  if(card&&card.dataset.vmid===String(vmid??'')){syncModelControls(role);continue;}
  delete modelDrafts[role];
  const form=el('form',null,'card model-config');form.id='model-config-'+role;form.dataset.vmid=vmid??'';
  const heading=el('div',null,'model-card-heading'),pull=el('button','Pull from VM');pull.type='button';pull.id=`model-${role}-read`;pull.addEventListener('click',()=>modelConfigAction(role,'read'));heading.append(el('h3','Cyber-agent-flow'),pull);
  form.append(heading,el('p',vmid?`VM ${vmid} · configs/cli.json`:'Select and save a VM first.','small'));
  const fields=[['provider','Provider'],['url','API base URL'],['model','Model name']];
  for(const [key,title] of fields){const label=el('label',title);const input=el(key==='provider'?'select':'input');input.id=`model-${role}-${key}`;input.name=key;input.required=true;
   if(key==='provider')for(const value of ['openai','litellm','ollama_direct','claude'])input.add(new Option(value==='openai'?'OpenAI / compatible':value,value));
   else{input.type=key==='url'?'url':'text';input.maxLength=2048;input.placeholder=key==='url'?'https://model-server.example/v1':'Model ID from your server';}
   label.append(input);form.append(label);
  }
  const tls=el('label','Verify TLS certificates '),checkbox=el('input');checkbox.type='checkbox';checkbox.checked=true;checkbox.id=`model-${role}-ssl`;tls.append(checkbox);form.append(tls);
  const keyLabel=el('label','Replacement API key'),key=el('input');key.type='password';key.autocomplete='new-password';key.maxLength=8192;key.placeholder='Leave blank to preserve the guest key';key.id=`model-${role}-key`;keyLabel.append(key);form.append(keyLabel);
  const clearLabel=el('label','Clear stored API key '),clear=el('input');clear.type='checkbox';clear.id=`model-${role}-clear`;clearLabel.append(clear);form.append(clearLabel);
  const message=el('p','Pull the configuration before editing.','small');message.id=`model-${role}-message`;message.setAttribute('role','status');form.append(message);
  form.addEventListener('input',()=>{if(modelDrafts[role])modelDrafts[role].dirty=true;syncModelControls(role);renderCAFSettings(snapshot);});
  form.addEventListener('submit',event=>{event.preventDefault();$('experiment-form').requestSubmit();});
  if(card)card.replaceWith(form);else $('model-config-cards').append(form);syncModelControls(role);
 }
}
function syncModelControls(role){
 const form=$('model-config-'+role);if(!form)return;const available=Boolean(form.dataset.vmid),draft=modelDrafts[role],canWrite=Boolean(snapshot?.updates?.can_update);
 for(const input of form.querySelectorAll('input,select'))input.disabled=!draft||!canWrite;
 $(`model-${role}-read`).disabled=!available;
 if(!canWrite)$(`model-${role}-message`).textContent=`Saving model settings requires the ${snapshot?.updates?.group||'caf-maintainers'} group and enabled application maintenance.`;
}
async function modelConfigAction(role,action,creating=false){
 if(isBusy()&&!creating)return;
 if(rolesDirty){const message='Save VM role selections before changing model settings.';if(creating)throw Error(message);$(`model-${role}-message`).textContent=message;return;}
 if(action==='read'&&modelDrafts[role]?.dirty&&!confirm('Discard unsaved model settings and pull from the VM?'))return;
 const body={role,action};if(action!=='read')body.token=modelDrafts[role]?.token;
 if(action==='save'){
  body.settings={provider:$(`model-${role}-provider`).value,url:$(`model-${role}-url`).value.trim(),model:$(`model-${role}-model`).value.trim(),ssl_verify:$(`model-${role}-ssl`).checked};
  const key=$(`model-${role}-key`);if($(`model-${role}-clear`).checked)body.api_key='';else if(key.value)body.api_key=key.value;key.value='';
 }
 operation=action==='read'?'Reading model configuration from the VM…':'Saving model settings to the VM and experiments…';syncBusy();$(`model-${role}-message`).textContent=operation;
 try{
  await finishDashboardRead();
  if(creating){
   creationProgress(0,'running','Saving file locally on orchestrator…');
   const staged=await apiFetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role,action:'stage',token:body.token,settings:body.settings})});
   const stagedData=await dashboardJSON(staged);if(!staged.ok)throw Error(stagedData.error||'Unable to save local model draft');
   creationProgress(0,'complete','File saved locally on orchestrator · complete');
   creationProgress(1,'running','Pushing to VM · 0% acknowledged · awaiting guest save');
  }
  const response=await apiFetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(body)});delete body.api_key;
  const data=await dashboardJSON(response);if(!response.ok)throw Error(data.error||'Model configuration operation failed');
  if(creating)creationProgress(1,'complete','VM push · 100% · guest save acknowledged');
  modelDrafts[role]={token:data.token,vmid:data.vmid,dirty:action==='read'};
  for(const key of ['provider','url','model'])$(`model-${role}-${key}`).value=data.settings[key];$(`model-${role}-ssl`).checked=data.settings.ssl_verify;$(`model-${role}-key`).value='';$(`model-${role}-clear`).checked=false;
  $(`model-${role}-message`).textContent=(action==='save'?'Saved. Settings copied to the VM and selected for new experiments. ':data.exists?'Configuration loaded. Create experiment will save these settings. ':'No configuration file yet; saving will create it. ')+(data.api_key_set?'A stored API key is present; its value stays in the VM. ':`No key stored in this file. Environment key: ${data.api_key_env}. `)+(data.backup?'Backup: '+data.backup:'');
 }catch(error){$(`model-${role}-message`).textContent=error.message;if(creating)throw error;}
 finally{delete body.api_key;if(!creating)operation=null;syncModelControls(role);syncBusy();if(!creating)schedulePoll();}
}

function showResults(id){return openRunWindow('results',id);}

function runActive(run){return run.coordinator_active||run.sample_progress?.active||['queued','stopping'].includes(run.recorded_status);}
function renderSamples(data){
 const samples=data.samples,vm=samples?.participant_vmid;
 $('new-experiment').disabled=!samples?.items?.length&&!data.scenarios?.enabled;
 if(scenarioChoiceVM!==null&&scenarioChoiceVM!==data.roles?.scenarioforge){scenarioChoices=[];scenarioChoiceVM=null;$('scenario-selection').replaceChildren(new Option('Reload scenarios for the selected VM',''));}
 renderCAFSettings(data);
 $('experiment-model-context').textContent=vm?`Current experiment model: ${samples.provider} / ${samples.model} · Participant VM ${vm}. Pull from VM below to read its saved application settings.`:'Select and save a participant VM on Lab setup to enable Pull from VM and run experiments.';
 $('samples-context').textContent=vm?`New experiment defaults: Participant VM ${vm} · ${samples.provider} / ${samples.model}. Saved experiments retain their own settings.`:'Save a participant VM on Lab setup before running an experiment.';
}
function iconAction(label,icon,disabled,callback){const button=el('button',null,'icon-action');button.type='button';button.title=label;button.setAttribute('aria-label',label);const glyph=el('span',icon);glyph.setAttribute('aria-hidden','true');button.append(glyph);button.disabled=disabled;button.addEventListener('click',callback);return button;}
function renderExperiments(data){
 const runs=$('runs');runs.replaceChildren();$('no-runs').hidden=Boolean(data.runs.length);
 const active=data.runs.some(runActive),items=data.samples?.items||[];
 for(const run of [...data.runs].sort((a,b)=>(b.sample_progress?.started_at||'').localeCompare(a.sample_progress?.started_at||''))){
  const id=run.output?.split('/').pop()||run.workflow_id||'',sample=items.find(s=>s.id===run.sample_id),live=runActive(run),ready=run.recorded_status==='ready',p=run.sample_progress,summary=run.evaluation?.summary;
  const row=el('tr');row.dataset.runId=id;const title=el('td');title.append(el('strong',sample?.name||run.scenario_experiment?.scenario||run.workflow_id||id),el('div',id,'small'));
  if(run.saved_settings){const settings=run.saved_settings;title.append(el('div',`VM ${settings.participant_vmid} · ${settings.provider} / ${settings.model}`,'small'));}
  const state=el('td');state.append(badge(run.recorded_status||'unavailable',run.recorded_status==='completed'?'good':['failed','cancelled','interrupted','completed_with_errors'].includes(run.recorded_status)?'warn':''));if(run.message)state.append(el('div',run.message,'small'));
  const actions=el('td',null,'experiment-actions');
  actions.append(iconAction(run.scenario_experiment?(ready?'Deploy and run':'Deploy and run again'):(ready?'Run experiment':'Run again'),'▶',!data.owner||!(sample||run.scenario_experiment)||!(run.saved_settings?.participant_vmid||data.samples.participant_vmid)||active||sampleStarting,()=>experimentAction('run',id)));
  actions.append(iconAction(run.scenario_experiment?'Stop after current stage or trial':'Stop after current trial','■',!data.owner||!(run.sample_id||run.scenario_experiment)||!live||run.recorded_status==='stopping',()=>experimentAction('stop',id)));
  actions.append(iconAction('View results','▤',!data.owner||ready,()=>showResults(id)));
  actions.append(iconAction('Open progress','◴',!p||ready,()=>openProgress(id)));
  row.append(title,state,el('td',p?`${p.finished_trials} / ${p.planned_trials}`:run.evaluation?`${summary.trials_observed} / ${run.evaluation.planned_trials}`:'—'),el('td',p?.verified_successes??summary?.verified_successes??'—'),actions);runs.append(row);
 }
}
function openProgress(id){return openRunWindow('progress',id);}
function experimentMissingFields(){
 if(!snapshot||!$('experiment-sample').value)return 'Choose an experiment type.';
 if(rolesDirty)return 'Save your VM role selections in Lab setup.';
 const scenario=$('experiment-sample').value==='scenarioforge-xml';
 const roles=scenario?['scenarioforge','participant']:['scenarioforge','participant','core'];
 if(roles.some(role=>!snapshot.roles?.[role]))return 'Select and save '+(scenario?'ScenarioForge and participant':'ScenarioForge, participant and CoreVM')+' roles in Lab setup.';
 if(scenario){
  const choice=scenarioChoices.find(item=>item.id===$('scenario-selection').value);
  if(scenarioChoiceVM!==snapshot.roles.scenarioforge||!choice?.resolved_chain)return 'Choose a resolved scenario on the ScenarioForge tab.';
  if(!$('scenario-allowed').value.trim())return 'Enter allowed target IPs or CIDRs on the ScenarioForge tab.';
 }
 const invalid=[...$('experiment-form').elements].find(input=>input.willValidate&&(!input.validity.valid||(input.required&&typeof input.value==='string'&&!input.value.trim())));
 if(invalid){const panel=invalid.closest('[role="tabpanel"]');return 'Complete the required fields on the '+(panel?$(panel.getAttribute('aria-labelledby')).textContent:'Experiment')+' tab.';}
 if(modelDrafts.participant){
  if(!snapshot.updates?.can_update)return 'Saving the model draft requires application maintenance access.';
  const form=$('model-config-participant');
  if(!form||[...form.elements].some(input=>input.willValidate&&(!input.validity.valid||(input.required&&!input.value.trim()))))return 'Complete the required model fields on the Cyber-agent-flow tab.';
 }
 return '';
}
function syncCreateExperiment(){
 const busy=isBusy()||experimentCreating;
 const reason=busy?'Wait for the current operation to finish.':experimentMissingFields();
 $('create-experiment').disabled=Boolean(reason);
 const hint=$('create-experiment-hint');if(hint.textContent!==reason)hint.textContent=reason;
 hint.hidden=!reason;
}
$('experiment-dialog').addEventListener('input',syncCreateExperiment);
$('experiment-dialog').addEventListener('change',syncCreateExperiment);

function selectExperimentTab(name,{focus=false}={}){
 for(const button of document.querySelectorAll('[data-experiment-tab]')){
  const selected=button.dataset.experimentTab===name;
  button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;
  $('experiment-panel-'+button.dataset.experimentTab).hidden=!selected;
  if(selected&&focus)button.focus();
 }
 document.querySelector('.experiment-tab-content').scrollTop=0;
}
const experimentTabs=[...document.querySelectorAll('[data-experiment-tab]')];
for(const button of experimentTabs){
 button.addEventListener('click',()=>selectExperimentTab(button.dataset.experimentTab));
 button.addEventListener('keydown',event=>{
  const index=experimentTabs.indexOf(button);
  const target=event.key==='ArrowRight'?(index+1)%experimentTabs.length:event.key==='ArrowLeft'?(index+experimentTabs.length-1)%experimentTabs.length:event.key==='Home'?0:event.key==='End'?experimentTabs.length-1:null;
  if(target!==null){event.preventDefault();selectExperimentTab(experimentTabs[target].dataset.experimentTab,{focus:true});}
 });
}
function validateExperimentFields(form){
 const invalid=[...form.elements].find(input=>input.willValidate&&!input.validity.valid);
 if(!invalid)return true;
 const panel=invalid.closest('[role="tabpanel"]');
 if(panel)selectExperimentTab(panel.id.replace('experiment-panel-',''));
 invalid.reportValidity();invalid.focus();return false;
}

let customEvaluation=null;
const evaluationKeys=['repetitions','max_turns','wall_seconds','tool_timeout','context_window'];
function renderCAFSettings(data){
 const defaults=data?.experiment_defaults;
 const summary=$('caf-settings-summary');summary.replaceChildren();
 const draft=modelDrafts.participant,model=draft?{provider:$('model-participant-provider')?.value,name:$('model-participant-model')?.value,url:$('model-participant-url')?.value}:defaults?.model;
 for(const [title,value] of [['Participant VM',data?.roles?.participant??'Not selected'],['Provider / model',model?[model.provider,model.name].join(' / '):'Unavailable'],['Model endpoint',model?.url||'Unavailable'],['CAF checkout',defaults?.engine?.path||'Unavailable'],['CAF Python',defaults?.engine?.python||'Unavailable']]){
  summary.append(el('dt',title),el('dd',String(value)));
 }
 $('caf-settings-source').textContent=draft?'Pending model settings below will be saved when you create the experiment.':defaults?.source||'Settings inherited from the server runtime.';
}
function renderEvaluationSettings(scenario,sample){
 const defaults=snapshot?.experiment_defaults?.evaluation||{repetitions:1,max_turns:snapshot?.scenarios?.max_turns||20,wall_seconds:snapshot?.scenarios?.wall_seconds||300,tool_timeout:60,context_window:8192};
 const values=scenario?(customEvaluation||defaults):{...defaults,repetitions:sample?.profile?.repetitions||1,max_turns:sample?.max_turns||3,wall_seconds:sample?.wall_seconds||120,tool_timeout:sample?.profile?.tool_timeout||30};
 for(const key of evaluationKeys)$('eval-'+key).value=values[key];
 $('evaluation-settings').disabled=!scenario;
 $('sample-scenario-info').hidden=scenario;
 if(!scenario){
  $('sample-scenario-xml').href='/demo-'+sample.id+'.xml';
  $('sample-scenario-bundle').href='/demo-'+sample.id+'.zip';
  $('sample-sf-use').value=sample?.profile?.scenarioforge_used?'Required · deploy, readiness check and evaluation export':'Not used by this bundled sample';
  $('sample-sf-companion').value=(sample?.profile?.companion_xml||'Unavailable')+' · fixed sample XML';
  $('sample-environment').value=sample?.profile?.environment||'Sample-defined environment';
  $('sample-prompt').value=sample?.profile?.prompt||'Sample-defined prompt';
 }
 $('eval-settings-note').textContent=scenario?'These settings are saved with this experiment. Reruns reuse them.':'Fixed by the sample to keep its prompt, tool comparison, and scoring consistent. CAF model settings remain configurable.';
 $('eval-tools').textContent='Tools / conditions: '+(scenario?'Baseline — nmap, curl, python3. Custom tool conditions are not editable here yet.':sample?.profile?.tools||'Sample-defined');
 $('eval-task-source').textContent=scenario?'Tasks, prompts, and verifiers are exported by ScenarioForge after deployment and shown in Results. This form does not override them.':'The deployed host address and fresh token/flags are resolved at run time. Exact prompts and XML are captured in Results.';
 renderCAFSettings(snapshot);
}
$('evaluation-settings').addEventListener('input',()=>{if(!$('evaluation-settings').disabled)customEvaluation=Object.fromEntries(evaluationKeys.map(key=>[key,Number($('eval-'+key).value)]));});

function describeSample(){
 const scenario=$('experiment-sample').value==='scenarioforge-xml',sample=snapshot?.samples?.items.find(s=>s.id===$('experiment-sample').value);
 $('scenario-options').hidden=!scenario;
 $('scenario-allowed').required=scenario;
 renderEvaluationSettings(scenario,sample);
 $('experiment-description').textContent=scenario?'Deploy and evaluate an existing ScenarioForge scenario.':sample?.description||'';
 $('experiment-budget').textContent=scenario?'Baseline tools · configure repetitions and trial limits below':sample?sample.trials+' trials · up to '+sample.max_turns+' turns and '+sample.wall_seconds+'s per trial':'';
}

$('new-experiment').addEventListener('click',()=>{if(isBusy())return;customEvaluation=null;const select=$('experiment-sample');select.replaceChildren();for(const sample of snapshot?.samples?.items||[])select.add(new Option(sample.name,sample.id));if(snapshot?.scenarios?.enabled)select.add(new Option('ScenarioForge XML or bundle','scenarioforge-xml'));$('scenario-allowed').value=snapshot?.scenarios?.allowed_targets||'';$('scenario-disallowed').value=snapshot?.scenarios?.disallowed_targets||'';$('experiment-error').textContent='';$('experiment-save-progress').hidden=true;creationSteps=[];describeSample();selectExperimentTab('overview');$('experiment-dialog').showModal();syncCreateExperiment();});
let scenarioChoices=[],scenarioChoiceVM=null;
function describeScenarioSelection(){
 const choice=scenarioChoices.find(item=>item.id===$('scenario-selection').value);
 $('scenario-selection-info').textContent=choice?choice.path+' · '+choice.scenario+' · '+choice.chain_length+' Flow steps':'Select a saved scenario with a resolved Flow chain.';
}
$('scenario-selection').addEventListener('change',describeScenarioSelection);
$('scenario-query').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();$('load-scenarios').click();}});
$('load-scenarios').addEventListener('click',async()=>{
 if(isBusy())return;
 operation='Loading saved scenarios from the ScenarioForge VM…';syncBusy();
 try{
  const response=await apiFetch('/api/scenarios/list',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({query:$('scenario-query').value})});
  const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Unable to load scenarios');
  showScenarioChoices(result);
  $('scenario-selection-info').textContent=result.items.length?(result.truncated?'Showing a limited list. Narrow the search to find another XML.':'Choose a scenario from VM '+result.vmid+'.'):'No matching saved XML found in '+result.roots.join(', ')+'.';
 }catch(error){$('scenario-selection-info').textContent=error.message;}
 finally{operation=null;syncBusy();schedulePoll();}
});

function showScenarioChoices(result){
 scenarioChoices=result.items;scenarioChoiceVM=result.vmid;
 const select=$('scenario-selection');select.replaceChildren(new Option('Choose a saved scenario',''));
 for(const item of scenarioChoices){const option=new Option(item.scenario+' — '+item.path.split('/').pop()+(item.resolved_chain?'':' (resolve Flow chain first)'),item.id);option.disabled=!item.resolved_chain;select.add(option);}
}
async function sendScenario(file){
 if(isBusy())return;
 if(rolesDirty||!snapshot?.roles?.scenarioforge){$('scenario-upload-info').textContent='Select and save a ScenarioForge VM on Lab setup first.';return;}
 if(!file||file.size===0||file.size>32*1024*1024){$('scenario-upload-info').textContent='Choose an XML or ScenarioForge reproduction ZIP up to 32 MiB.';return;}
 operation='Uploading and importing scenario on the ScenarioForge VM…';syncBusy();
 try{
  await finishDashboardRead();
  const response=await apiFetch('/api/scenarios/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':csrfToken},body:file});
  const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Scenario import failed');
  showScenarioChoices(result);
  const ready=result.items.filter(item=>item.resolved_chain);
  if(ready.length===1)$('scenario-selection').value=ready[0].id;
  describeScenarioSelection();
  $('scenario-upload-info').textContent='Imported to VM '+result.vmid+' · '+result.kind+' · '+result.fidelity+'. '+(ready.length?'Select the scenario below, review scope, then create the experiment.':'Open the imported XML in ScenarioForge to resolve and save its Flow chain, then reload it here.');
  $('scenario-file').value='';
 }catch(error){$('scenario-upload-info').textContent=error.message;}
 finally{operation=null;syncBusy();schedulePoll();}
}
$('upload-scenario').addEventListener('click',()=>sendScenario($('scenario-file').files[0]));

$('experiment-sample').addEventListener('change',describeSample);
$('cancel-experiment').addEventListener('click',()=>{if(!experimentCreating&&!operation)$('experiment-dialog').close();});
$('experiment-dialog').addEventListener('cancel',event=>{if(experimentCreating||operation)event.preventDefault();});
$('experiment-form').addEventListener('submit',async event=>{
 event.preventDefault();if(isBusy()||experimentCreating)return;
 const missing=experimentMissingFields();if(missing){syncCreateExperiment();return;}
 const scenario=$('experiment-sample').value==='scenarioforge-xml';
 if(!validateExperimentFields($('experiment-form')))return;
 if(scenario&&(!scenarioChoices.some(item=>item.id===$('scenario-selection').value)||rolesDirty)){selectExperimentTab('scenario');$('experiment-error').textContent=rolesDirty?'Save VM roles before creating an experiment.':'Load and choose a saved scenario first.';return;}
 const modelForm=$('model-config-participant'),saveModel=Boolean(modelDrafts.participant);
 if(saveModel&&(!snapshot?.updates?.can_update||rolesDirty)){$('experiment-error').textContent=rolesDirty?'Save VM role selections before creating an experiment.':'Model settings cannot be saved without application maintenance access.';return;}
 if(saveModel&&!validateExperimentFields(modelForm))return;
 $('experiment-error').textContent='';
 startCreationProgress(saveModel);
 experimentCreating=true;operation='Creating experiment…';syncBusy();for(const input of $('experiment-form').querySelectorAll('button,select'))input.disabled=true;
 try{if(saveModel){await modelConfigAction('participant','save',true);operation='Creating experiment with saved model settings…';syncBusy();}creationProgress(2,'running','Saving experiment on orchestrator…');await finishDashboardRead();const response=await apiFetch('/api/experiments/create',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({request_id:crypto.randomUUID().replaceAll('-',''),...(scenario?{selection_id:$('scenario-selection').value,allowed_targets:$('scenario-allowed').value,disallowed_targets:$('scenario-disallowed').value,evaluation:Object.fromEntries(evaluationKeys.map(key=>[key,Number($('eval-'+key).value)]))}:{sample_id:$('experiment-sample').value})})});const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Unable to create experiment');creationProgress(2,'complete','Experiment saved · complete');$('experiment-dialog').close();$('sample-message').textContent='Experiment created. Press its Run icon when ready.';await refresh();}
 catch(error){const current=creationSteps.findIndex(step=>step.state==='running');if(current>=0)creationProgress(current,'failed',creationSteps[current].message+' · failed');$('experiment-error').textContent=error.message;}
 finally{experimentCreating=false;operation=null;for(const input of $('experiment-form').querySelectorAll('button,select'))input.disabled=false;syncBusy();schedulePoll();}
});
async function experimentAction(action,id){
 if(isBusy()||sampleStarting)return;
 if(action==='run'&&rolesDirty){$('sample-message').textContent='Save VM role changes on Lab setup before running.';return;}
 sampleStarting=true;operation=action==='stop'?'Requesting experiment stop…':'Starting experiment…';syncBusy();
 try{await finishDashboardRead();const response=await apiFetch('/api/experiments/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({run_id:id,...(action==='run'?{request_id:crypto.randomUUID().replaceAll('-','')}:{})})});const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Experiment action failed');$('sample-message').textContent=action==='stop'?'Stop requested. The current stage or trial will finish and results will be collected.':'Experiment started.';const link=el('a',' Open progress ↗');link.href=runWindowURL('progress',result.run_id);link.addEventListener('click',event=>{event.preventDefault();openProgress(result.run_id);});$('sample-message').append(link);await refresh();}
 catch(error){$('sample-message').textContent=error.message;}
 finally{sampleStarting=false;operation=null;if(snapshot)renderExperiments(snapshot);syncBusy();schedulePoll();}
}
function renderSampleActivity(data){
 const active=(data.runs||[]).find(run=>run.sample_progress?.active),target=$('sample-global-status');target.replaceChildren();target.hidden=!active;
 if(active){const p=active.sample_progress,id=active.output.split('/').pop(),link=el('a','View experiment activity ↗');link.href=runWindowURL('progress',id);link.addEventListener('click',event=>{event.preventDefault();openProgress(id);});target.append(document.createTextNode(`${p.name} · ${active.recorded_status} · ${p.finished_trials}/${p.planned_trials} trials finished. `),link);}
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
  const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||`Maintenance request failed (HTTP ${response.status})`);
  $('update-message').textContent=`Maintenance ${result.id} submitted. Follow its status below.`;await refresh();
 }catch(error){$('update-message').textContent=error.message;}
 finally{maintenanceStarting=false;operation=null;syncBusy();schedulePoll();}
}

// Progress reads collect an existing check/job without starting another VM probe.
// Idle automatic refreshes start a new batch at the selected cadence.
function schedulePoll(){
 clearTimeout(pollTimer);
 if(fetching||operation||redirecting)return;
 const active=Boolean(snapshot?.refreshing||waitingForMaintenance||(snapshot?.runs||[]).some(runActive));
 const minutes=Number($('refresh-period').value);
 if(!minutes){if((snapshot?.runs||[]).some(runActive))pollTimer=setTimeout(checkExperimentCompletion,5000);return;}
 pollTimer=setTimeout(()=>{if(autoRefreshEnabled()&&!fetching&&!operation&&!redirecting)refresh(!active,true);},active?5000:minutes*60000);
}
$('refresh-period').addEventListener('change',()=>{try{localStorage.setItem('caf-refresh-minutes',$('refresh-period').value);}catch{}schedulePoll();tick();syncBusy();});
setInterval(()=>{tick();syncBusy();},1000);
syncBusy();refresh(true);

window.addEventListener('storage',event=>{if(event.key==='caf-refresh-minutes'&&refreshPeriods.includes(event.newValue)){$('refresh-period').value=event.newValue;schedulePoll();tick();syncBusy();}});

async function checkExperimentCompletion(){
 if(fetching||operation||redirecting){schedulePoll();return;}
 const active=(snapshot?.runs||[]).filter(runActive);let changed=false;
 try{
  for(const previous of active){
   const id=previous.output?.split('/').pop()||previous.workflow_id;
   const response=await apiFetch('/api/runs/'+encodeURIComponent(id)+'/status',{cache:'no-store'});
   if(response.status===401){sessionExpired();return;}
   if(!response.ok)continue;
   const current=await dashboardJSON(response);
   if(['completed','completed_with_errors','cancelled','failed','interrupted'].includes(current.recorded_status)&&!runActive(current)){
    const index=snapshot?.runs.indexOf(previous)??-1;
    if(index>=0){snapshot.runs[index]=current;changed=true;}
   }
  }
  if(changed){renderExperiments(snapshot);syncBusy();}
 }catch(error){console.warn('Experiment completion check failed',error.message);}
 finally{schedulePoll();}
}
