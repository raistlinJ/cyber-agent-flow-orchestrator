/* Text-only rendering keeps guest command lines and journal labels inert. */
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => {const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node;};
// Retain mounted cards and rows so refreshes preserve focus and scroll state.
function patchDisplayNode(current, next){
 if(current.nodeType!==next.nodeType||current.nodeName!==next.nodeName){current.replaceWith(next);return;}
 if(current.nodeType===Node.TEXT_NODE){if(current.nodeValue!==next.nodeValue)current.nodeValue=next.nodeValue;return;}
 for(const attr of [...current.attributes])if(!next.hasAttribute(attr.name))current.removeAttribute(attr.name);
 for(const attr of next.attributes)if(current.getAttribute(attr.name)!==attr.value)current.setAttribute(attr.name,attr.value);
 const children=[...next.childNodes];
 for(let i=0;i<children.length;i++){
  if(current.childNodes[i])patchDisplayNode(current.childNodes[i],children[i]);else current.append(children[i]);
 }
 while(current.childNodes.length>children.length)current.lastChild.remove();
}
function updateDisplayList(container, next){
 const existing=new Map([...container.children].map(node=>[node.dataset.displayKey,node]));
 let cursor=container.firstChild;
 for(const node of [...next.children]){
  const mounted=existing.get(node.dataset.displayKey);
  if(mounted){patchDisplayNode(mounted,node);existing.delete(node.dataset.displayKey);}
  const item=mounted||node;
  if(item!==cursor)container.insertBefore(item,cursor);
  cursor=item.nextSibling;
 }
 for(const node of existing.values())node.remove();
}
let snapshot = null, fetching = false, failed = false, rolesDirty = false, rolesSaving = false, sampleStarting = false;
let initialized=false, operation=null, waitingForObservation=false, waitingForMaintenance=false, redirecting=false, maintenanceStarting=false;
let refreshPromise=null, sessionPromise=null, csrfToken=null, wasBusy=false, busySince=performance.now();
let experimentCreating=false,sessionAccount=null,deletingExperiments=false;
const selectedExperiments=new Set();
let blockingRefresh=false, foregroundChecks=true, pollTimer=null;
const clientLog=[];
let wizardReady=false,wizardDraft=false,wizardStep=0,wizardShowErrors=false,wizardRoles=null;
const wizardVisited=new Set();
const pages={'experiments/new':['New experiment','Configure, review and save your experiment.'],studies:['Studies','Controlled comparisons across ScenarioForge scenarios.'],overview:['Overview','Live machines, application processes, and workflow activity.'],experiments:['Experiments','Run a sample, follow its progress, and explore or export results.'],applications:['Applications','Inspect selected application VMs and manage source when maintenance is enabled.'],setup:['Lab setup','Choose your virtual machines and set your refresh preferences.']};
function routePage(focus=false){
 const route=location.hash.slice(1),page=Object.hasOwn(pages,route)?route:'overview';
 if(page!==route)history.replaceState(null,'','#'+page);
 for(const section of document.querySelectorAll('[data-page]'))section.hidden=section.dataset.page!==page;
 for(const link of document.querySelectorAll('[data-route]')){if(link.dataset.route===(page==='experiments/new'?'experiments':page))link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}
 document.body.classList.toggle('experiment-builder-active',page==='experiments/new');
 $('page-title').textContent=pages[page][0];$('page-intro').textContent=pages[page][1];document.title=pages[page][0]+' · CAF Orchestrator';
 if(focus){$('page-title').focus({preventScroll:true});window.scrollTo(0,0);}
 if(page==='experiments/new'&&wizardReady&&!wizardDraft&&initialized&&!isBusy()&&snapshot)startNewExperiment();
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
 output.textContent=entries.slice(-300).map(event=>`${event.at} [${event.kind}] ${String(event.message||'').replaceAll('Judge endpoint and host credentials','Test LLM Judge').replaceAll('Testing judge is working','Test LLM Judge')}`).join('\n')||'Waiting for activity…';
 if(follow)output.scrollTop=output.scrollHeight;
}
async function apiFetch(path,options={}){
 const send=async request=>{
  const start=performance.now(),label=`${request.method||'GET'} ${path}`;logClient('request',label);
  try{const response=await fetch(path,request);logClient('response',`${label} → HTTP ${response.status} (${((performance.now()-start)/1000).toFixed(2)}s)`);return response;}
  catch(error){logClient('error',`${label} → network request failed (${((performance.now()-start)/1000).toFixed(2)}s)`);throw error;}
 };
 let response=await send(options);
 const headers=new Headers(options.headers);
 if(response.status===403&&headers.has('X-CSRF-Token')){
  const rejected=await response.clone().json().catch(()=>null);
  // The server rejects CSRF before performing any operation. Refresh once,
  // then retry only this explicit rejection, never other failed mutations.
  if(rejected?.error==='Invalid CSRF token'){
   await loadSession();headers.set('X-CSRF-Token',csrfToken);
   response=await send({...options,headers});
  }
 }
 return response;
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
 loadingModal.set('dashboard',loading&&busy,label,percent);
 syncUpdateControls(busy?label:null);
 syncCreateExperiment();syncExperimentSelection();
}
function sessionExpired(){redirecting=true;clearPrivateView();syncBusy();location.replace('/login');throw Error('Your session expired. Sign in again.');}
async function loadSession(){
 if(sessionPromise)return sessionPromise;
 sessionPromise=(async()=>{const response=await apiFetch('/api/session',{cache:'no-store'});if(response.status===401)sessionExpired();if(!response.ok)throw Error('Unable to verify your session. Refresh to retry.');const session=await dashboardJSON(response);csrfToken=session.csrf;sessionAccount=[session.provider,session.username];$('username-label').textContent=session.username;$('sign-out').hidden=session.provider==='desktop';})();
 try{await sessionPromise;}finally{sessionPromise=null;}
}
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live, sample=false) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';if(sample)node.dataset.source='sample';} return node;}
function detail(list,label,value) {const row=el('div');row.append(el('dt',label),el('dd',value));list.append(row);}
function render(data) {
 snapshot=data; renderRoles(data); renderModelConfigs(data); renderSamples(data); renderSampleActivity(data); renderUpdates(data); renderConsole();$('workflow').textContent=data.workflow_id||'';
 const notice=$('notice'); const errors=(data.errors||[]).map(x=>x.error);notice.hidden=!errors.length;notice.textContent=errors.join(' · ');
 const cards=el('div');
 if(!data.vms.length)cards.append(el('div','Checking configured machines…','empty'));
 for(const vm of data.vms){
  const card=el('article',null,'card'), top=el('div',null,'card-top'), label=el('div');
  card.dataset.displayKey=vm.role;
  label.append(el('h3',vm.label),el('div',vm.vmid ? `VM ${vm.vmid}${vm.name?' · '+vm.name:''}`:'No VM selected','vm-id'));
  const power=vm.qmp_status==='paused'?'paused':vm.power;
  top.append(el('span',vm.role==='core'?'◈':'▤','vm-icon'),label,badge(power,power==='running'?'good':power==='unknown'?'warn':''));card.append(top);
  if(vm.guest_cached){const note=el('p',null,'small');note.append(document.createTextNode('Cached guest observation · '),timer(0,vm.observed_at,true),document.createTextNode(' ago · Recheck VMs for a fresh scan'));card.append(note);}
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
 updateDisplayList($('machines'),cards);
 const jobs=(data.vms||[]).flatMap(vm=>(vm.jobs||[]).map(job=>({...job,observed_at:vm.observed_at||data.checked_at})));
 $('command-count').textContent=`${jobs.length} recorded job${jobs.length===1?'':'s'}`; const commands=el('div');
 for(const job of jobs){const row=el('article',null,'command'),head=el('div',null,'command-head');row.dataset.displayKey=job.unit+'|'+job.run+'|'+job.stage;head.append(badge(job.live_state,job.live_state==='running'?'good':job.live_state==='unconfirmed'?'warn':''),el('span',`${job.run} / ${job.stage}`,'command-title'),timer(job.live_state==='finished'?null:job.elapsed_seconds,job.observed_at,job.live_state==='running'));row.append(head,el('code',job.command),el('div',`VM ${job.vmid} · ${job.unit} · ${job.live_state==='finished'?'No longer running; journal awaits cleanup':job.elapsed_source||'Last recorded job; live state unconfirmed'}`,'small'));commands.append(row);}
 if(!jobs.length)commands.append(el('p','No unfinished workflow commands recorded. Applications may still be running above.','empty'));
 updateDisplayList($('commands'),commands);
 renderExperiments(data);
 if(window.renderStudyMembers)renderStudyMembers(data);
 waitingForMaintenance=Boolean((data.updates?.jobs||[]).some(job=>['queued','running'].includes(job.status)));waitingForObservation=Boolean(data.refreshing&&(data.loading?data.loading.total>0:(!data.checked_at||data.vms.length>0)));if(!waitingForObservation)foregroundChecks=false;tick();syncBusy();
}
function tick(){const age=snapshot?.checked_at?Math.max(0,(Date.now()-Date.parse(snapshot.checked_at))/1000):null;const stale=failed||(age!=null&&age>Math.max(60,Number($('refresh-period').value)*120));$('connection').textContent=failed?'Dashboard unavailable':stale?'Observation is stale':snapshot?.checked_at?'Monitoring lab':'Checking machines…';$('pulse').className='dot'+(stale||!snapshot?.checked_at?' muted':'');$('checked').textContent=age==null?'Waiting for first check':`Checked ${duration(age)} ago${snapshot.refreshing?' · refreshing':''}`;for(const clock of document.querySelectorAll('[data-seconds]')){let seconds=Number(clock.dataset.seconds);if(clock.dataset.live==='yes'&&(!stale||clock.dataset.source==='sample')&&clock.dataset.observed)seconds+=Math.max(0,(Date.now()-Date.parse(clock.dataset.observed))/1000);clock.textContent=duration(seconds);clock.title=stale&&clock.dataset.source!=='sample'?'Last observation; refresh required':clock.dataset.source==='sample'?'Elapsed time since sample or trial start':'Elapsed time since process start';}}
async function refresh(force=false, background=false, fresh=false){
 if(refreshPromise)return refreshPromise;
 clearTimeout(pollTimer);fetching=true;blockingRefresh=!background;if(force&&!background)foregroundChecks=true;syncBusy();
 refreshPromise=(async()=>{
  try{
   if(!csrfToken){await loadSession();syncBusy();}
   const response=await apiFetch(force?'/api/status?refresh=1'+(fresh?'&fresh=1':''):'/api/status?refresh=0',{cache:'no-store'});
   if(response.status===401)sessionExpired();
   if(response.status===403)clearPrivateView();
   if(!response.ok)throw Error(response.status===504?'The dashboard request timed out (HTTP 504)':response.status===403?'Access not granted':`HTTP ${response.status}`);
   const data=await dashboardJSON(response);failed=false;render(data);return true;
  }catch(error){failed=true;foregroundChecks=false;waitingForObservation=false;if(!snapshot)clearPrivateView();$('notice').hidden=false;$('notice').textContent=`Dashboard unavailable: ${error.message}. Refresh to try again.`;tick();return false;}
  finally{fetching=false;blockingRefresh=false;initialized=true;syncBusy();}
 })();
 try{return await refreshPromise;}finally{refreshPromise=null;schedulePoll();if(wizardReady&&location.hash==='#experiments/new'&&!wizardDraft&&!failed)startNewExperiment();}
}
async function refreshView(fresh=false){
 if(isBusy())return;operation=fresh?'Rechecking selected VMs…':null;syncBusy();
 try{await finishDashboardRead(true);await refresh(true,!fresh,fresh);}
 catch(error){if(!redirecting){$('notice').hidden=false;$('notice').textContent=error.message;}}
 finally{operation=null;syncBusy();schedulePoll();}
}
$('refresh').addEventListener('click',()=>refreshView());
$('recheck-vms').addEventListener('click',()=>refreshView(true));
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

function clearPrivateView(){selectedExperiments.clear();$('window-notice').hidden=true;$('window-notice').replaceChildren();$('sample-global-status').hidden=true;$('sample-global-status').replaceChildren();wizardDraft=false;$('judge-api-key').value='';snapshot=null;modelDrafts={};$('model-config-cards').replaceChildren();$('model-config-panel').hidden=true;clientLog.length=0;renderConsole();waitingForObservation=false;waitingForMaintenance=false;$('updates-panel').hidden=true;$('update-message').textContent='';$('update-cards').replaceChildren();$('update-jobs').replaceChildren();$('machines').replaceChildren();$('commands').replaceChildren();$('runs').replaceChildren();leaveExperimentWizard();$('experiment-sample').replaceChildren();$('new-experiment').disabled=true;for(const role of ['scenarioforge','participant','core'])$('role-'+role).replaceChildren();$('role-panel').hidden=true;}
function renderRoles(data){
 $('roles-unavailable').hidden=Boolean(data.roles);
 const panel=$('role-panel');panel.hidden=!data.roles;if(!data.roles)return;
 for(const role of ['scenarioforge','participant','core']){
  const select=$('role-'+role),wanted=rolesDirty?select.value:String(data.roles[role]??'');
  const options=[new Option('Not selected',''),...(data.available_vms||[]).map(vm=>new Option(`VM ${vm.vmid} · ${vm.name||'Unnamed'} · ${vm.status||'unknown'}${vm.pool?' · '+vm.pool:''}`,String(vm.vmid)))];
  if(JSON.stringify([...select.options].map(o=>[o.value,o.text]))!==JSON.stringify(options.map(o=>[o.value,o.text])))select.replaceChildren(...options);
  select.value=[...select.options].some(o=>o.value===wanted)?wanted:'';
 }
 if(!rolesDirty&&!rolesSaving)$('roles-message').textContent=data.available_vms.length?'Selections are private to your account. Changes apply to subsequent runs.':'No available lab VMs. Check the configured inventory and your access.';
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
function modelFormSettings(role){
 return {provider:$(`model-${role}-provider`).value,url:$(`model-${role}-url`).value.trim(),model:$(`model-${role}-model`).value.trim(),ssl_verify:$(`model-${role}-ssl`).checked};
}
function modelSettingsChanged(role){
 const saved=modelDrafts[role]?.settings;if(!saved)return false;
 const current=modelFormSettings(role);
 return Object.keys(current).some(key=>current[key]!==saved[key])||Boolean($(`model-${role}-key`).value)||$(`model-${role}-clear`).checked;
}
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
  const actions=el('div',null,'result-actions'),apply=el('button','Apply settings');apply.type='button';apply.id=`model-${role}-apply`;apply.addEventListener('click',()=>{if(validateExperimentFields(form))modelConfigAction(role,'save');});actions.append(apply);form.append(actions);
  const message=el('p','Edit the saved experiment defaults, then select Apply settings. Pull from VM is optional and replaces the displayed values.','small');message.id=`model-${role}-message`;message.setAttribute('role','status');form.append(message);
  const changed=()=>{if(modelDrafts[role]){modelDrafts[role].dirty=modelSettingsChanged(role);delete modelDrafts[role].saveError;}syncModelControls(role);syncCreateExperiment();};
  form.addEventListener('input',changed);form.addEventListener('change',changed);
  form.addEventListener('submit',event=>{event.preventDefault();if(validateExperimentFields(form))modelConfigAction(role,'save');});
  if(card)card.replaceWith(form);else $('model-config-cards').append(form);
  const defaults=data.samples?.model_settings;
  if(defaults){for(const key of ['provider','url','model'])$(`model-${role}-${key}`).value=defaults[key]??'';$(`model-${role}-ssl`).checked=defaults.ssl_verify!==false;}
  modelDrafts[role]={vmid,token:null,dirty:false,settings:modelFormSettings(role)};
  syncModelControls(role);
 }
}
function syncModelControls(role){
 const form=$('model-config-'+role);if(!form)return;const available=Boolean(form.dataset.vmid),draft=modelDrafts[role],canWrite=Boolean(snapshot?.model_config_writable??snapshot?.updates?.can_update);
 for(const input of form.querySelectorAll('input,select'))input.disabled=!available||!canWrite;
 $(`model-${role}-read`).disabled=!available;
 $(`model-${role}-apply`).disabled=!available||!draft||(!draft.dirty&&!draft.saveError&&Boolean(draft.token))||!canWrite;
 if(!canWrite)$(`model-${role}-message`).textContent=`Model settings are read-only. You can create experiments using saved settings. Applying changes requires the ${snapshot?.updates?.group||'caf-maintainers'} group and enabled application maintenance.`;
}
async function modelConfigAction(role,action,creating=false){
 if(isBusy()&&!creating)return;
 if(rolesDirty){const message='Save VM role selections before changing model settings.';if(creating)throw Error(message);$(`model-${role}-message`).textContent=message;return;}
 if(action==='read'&&!confirm('Pull from VM will replace all model settings currently shown in this form. Continue?'))return;
 const body={role,action};if(action!=='read')body.token=modelDrafts[role]?.token;
 if(action==='save'){
  body.settings=modelFormSettings(role);
  const key=$(`model-${role}-key`);if($(`model-${role}-clear`).checked)body.api_key='';else if(key.value)body.api_key=key.value;key.value='';
 }
 operation=action==='read'?'Reading model configuration from the VM…':role==='participant'?'Saving model settings, synchronizing the participant route and checking connectivity…':'Saving model settings to the VM and experiments…';syncBusy();$(`model-${role}-message`).textContent=operation;
 try{
  await finishDashboardRead();
  if(action==='save'&&!modelDrafts[role]?.token){
   // Acquire the guest revision for optimistic concurrency without replacing
   // any form settings or API key captured before this background read.
   const current=await apiFetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role,action:'read'})});
   const fresh=await dashboardJSON(current);if(!current.ok)throw Error(fresh.error||'Unable to read the VM configuration before applying settings');
   body.token=fresh.token;modelDrafts[role].token=fresh.token;
  }
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
  modelDrafts[role]={token:data.token,vmid:data.vmid,dirty:false};
  if(role==='participant'&&data.network_scope)applyModelNetworkScope(data.network_scope);
  for(const key of ['provider','url','model'])$(`model-${role}-${key}`).value=data.settings[key];$(`model-${role}-ssl`).checked=data.settings.ssl_verify;$(`model-${role}-key`).value='';$(`model-${role}-clear`).checked=false;
  modelDrafts[role].settings=modelFormSettings(role);
  $(`model-${role}-message`).textContent=(action==='save'?'Applied. These values are saved on the VM and will be used by new experiments. ':data.exists?'Pulled from VM. Edit the values, then select Apply settings. ':'No configuration file exists yet. Enter values, then select Apply settings. ')+(data.api_key_set?'A stored API key is present; its value stays in the VM. ':`No key stored in this file. Environment key: ${data.api_key_env}. `)+(data.routing?'Route: '+(data.routing.status==='updated'?`${data.routing.destination} through ${data.routing.interface}${data.routing.gateway?' via '+data.routing.gateway:''}. `:data.routing.message+'. '):'')+(data.routing?.helper_updated?`Routing helper updated; backup: ${data.routing.helper_backup}. `:'')+(data.routing?.dns?`DNS: ${data.routing.dns.domain} through ${data.routing.dns.server}. `:'')+(data.connectivity?data.connectivity.message+'. ':'')+(data.backup?'Backup: '+data.backup:'');
 }catch(error){$(`model-${role}-message`).textContent=error.message;if(action==='save'&&modelDrafts[role])modelDrafts[role].saveError=error.message;if(creating)throw error;}
 finally{delete body.api_key;if(!creating)operation=null;syncModelControls(role);syncBusy();if(!creating)schedulePoll();}
}

function showResults(id){return openRunWindow('results',id);}

function runActive(run){return run.coordinator_active||run.sample_progress?.active||['queued','preparing','evaluating','running','stopping'].includes(run.recorded_status);}
function renderSamples(data){
 const samples=data.samples,vm=samples?.participant_vmid;
 $('new-experiment').disabled=!samples?.items?.length&&!data.scenarios?.enabled;
 if(scenarioChoiceVM!==null&&scenarioChoiceVM!==data.roles?.scenarioforge){scenarioChoices=[];scenarioChoiceVM=null;taskScenarioChanged();$('scenario-selection').replaceChildren(new Option('Reload scenarios for the selected VM',''));}
 $('samples-context').textContent=vm?`New experiment defaults: Participant VM ${vm} · ${samples.provider} / ${samples.model}. Saved experiments retain their own settings.`:'Save a participant VM on Lab setup before running an experiment.';
}
function iconAction(label,icon,disabled,callback){const button=el('button',null,'icon-action');button.type='button';button.title=label;button.setAttribute('aria-label',label);const glyph=el('span',icon);glyph.setAttribute('aria-hidden','true');button.append(glyph);button.disabled=disabled;button.addEventListener('click',callback);return button;}
function renderExperiments(data){
 const runs=el('tbody');$('no-runs').hidden=Boolean(data.runs.length);
 const active=data.runs.some(runActive),items=data.samples?.items||[];
 for(const run of [...data.runs].sort((a,b)=>(b.sample_progress?.started_at||'').localeCompare(a.sample_progress?.started_at||''))){
  const id=run.output?.split('/').pop()||run.workflow_id||'',sample=items.find(s=>s.id===run.sample_id),live=runActive(run),ready=run.recorded_status==='ready',p=run.sample_progress,summary=run.evaluation?.summary;
  const row=el('tr');row.dataset.runId=id;row.dataset.displayKey=id;const selection=el('td',null,'experiment-selection-cell'),checkbox=el('input');checkbox.type='checkbox';checkbox.className='experiment-selector';checkbox.dataset.runId=id;checkbox.setAttribute('aria-label','Select experiment '+id);checkbox.addEventListener('change',()=>{checkbox.checked?selectedExperiments.add(id):selectedExperiments.delete(id);syncExperimentSelection();});selection.append(checkbox);const title=el('td');title.append(el('strong',sample?.name||run.scenario_experiment?.scenario||run.workflow_id||id),el('div',id,'small'));
  if(run.saved_settings){const settings=run.saved_settings;title.append(el('div',`VM ${settings.participant_vmid} · ${settings.provider} / ${settings.model}`,'small'));}
  const state=el('td');state.append(badge(run.recorded_status||'unavailable',run.recorded_status==='completed'?'good':['failed','cancelled','interrupted','completed_with_errors'].includes(run.recorded_status)?'warn':''));if(run.message)state.append(el('div',run.message,'small'));
  if(run.trial_failures?.length)state.append(el('div',`${run.trial_failures.length} trial error(s) · Open Progress or Results for details`,'error'));
  const actions=el('td',null,'experiment-actions');
  actions.append(iconAction(run.scenario_experiment?(ready?'Deploy and run':'Deploy and run again'):(ready?'Run experiment':'Run again'),'▶',!data.owner||!(sample||run.scenario_experiment)||!(run.saved_settings?.participant_vmid||data.samples.participant_vmid)||active||sampleStarting,()=>experimentAction('run',id)));
  actions.append(iconAction(run.scenario_experiment?'Stop after current stage or trial':'Stop after current trial','■',!data.owner||!(run.sample_id||run.scenario_experiment)||!live||run.recorded_status==='stopping',()=>experimentAction('stop',id)));
  actions.append(iconAction('View results','▤',!data.owner||ready,()=>showResults(id)));
  actions.append(iconAction('Open progress','◴',!p||ready,()=>openProgress(id)));
  row.append(selection,title,state,el('td',p?`${p.finished_trials} / ${p.planned_trials}`:run.evaluation?`${summary.trials_observed} / ${run.evaluation.planned_trials}`:'—'),el('td',p?.verified_successes??summary?.verified_successes??'—'),actions);runs.append(row);
 }
 updateDisplayList($('runs'),runs);syncExperimentSelection();
}
function selectableExperiments(){return snapshot?.owner?(snapshot.runs||[]).filter(run=>!runActive(run)):[];}
function syncExperimentSelection(){
 const eligible=new Set(selectableExperiments().map(run=>run.output.split('/').pop()));
 for(const id of selectedExperiments)if(!eligible.has(id))selectedExperiments.delete(id);
 const busy=isBusy()||deletingExperiments;
 for(const input of $('runs').querySelectorAll('.experiment-selector')){input.checked=selectedExperiments.has(input.dataset.runId);input.disabled=busy||!eligible.has(input.dataset.runId);input.title=!eligible.has(input.dataset.runId)?'Running experiments cannot be deleted.':'';}
 const all=$('select-experiments');all.disabled=busy||!eligible.size;all.checked=Boolean(eligible.size)&&selectedExperiments.size===eligible.size;all.indeterminate=selectedExperiments.size>0&&selectedExperiments.size<eligible.size;
 $('experiment-selection-count').textContent=selectedExperiments.size+' selected';
 const remove=$('delete-experiments');remove.disabled=busy||!selectedExperiments.size;remove.title=busy?'Wait for the current operation to finish.':selectedExperiments.size?'Delete selected experiments and their saved inputs and results.':'Select at least one inactive experiment.';
}
$('select-experiments').addEventListener('change',()=>{selectedExperiments.clear();if($('select-experiments').checked)for(const run of selectableExperiments())selectedExperiments.add(run.output.split('/').pop());syncExperimentSelection();});
$('delete-experiments').addEventListener('click',async()=>{
 if(isBusy()||deletingExperiments||!selectedExperiments.size)return;
 const ids=[...selectedExperiments],names=selectableExperiments().filter(run=>ids.includes(run.output.split('/').pop())).map(run=>(run.scenario_experiment?.scenario||snapshot.samples?.items?.find(item=>item.id===run.sample_id)?.name||run.workflow_id)+' · '+run.output.split('/').pop());
 if(!confirm(`Delete ${ids.length} selected experiment${ids.length===1?'':'s'}?\n\n${names.join('\n')}\n\nThis permanently removes their saved inputs, results, logs and bundles from the orchestrator.`))return;
 deletingExperiments=true;operation='Deleting selected experiments…';syncBusy();
 try{
  await finishDashboardRead();
  const response=await apiFetch('/api/experiments/delete',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({run_ids:ids,confirmation:'delete-selected-experiments'})}),result=await dashboardJSON(response);
  if(!response.ok)throw Error(result.error||'Unable to delete experiments');
  const deleted=new Set(result.deleted);for(const id of deleted)selectedExperiments.delete(id);
  snapshot.runs=snapshot.runs.filter(run=>!deleted.has(run.output.split('/').pop()));renderExperiments(snapshot);
  $('sample-message').textContent=`Deleted ${deleted.size} experiment${deleted.size===1?'':'s'}.`;await refresh();
 }catch(error){$('sample-message').textContent=error.message;await refresh();}
 finally{deletingExperiments=false;operation=null;syncBusy();schedulePoll();}
});
function openProgress(id){return openRunWindow('progress',id);}
function wizardRolesChanged(){return wizardDraft&&wizardRoles&&['participant','scenarioforge','core'].some(role=>wizardRoles[role]!==snapshot?.roles?.[role]);}
function experimentMissingFields(){
 if(!snapshot||!$('experiment-sample').value)return 'Choose an experiment type.';
 if(rolesDirty)return 'Save your VM role selections in Lab setup.';
 if(wizardRolesChanged())return 'Lab VMs changed. Return to Experiments and choose New to load the new VMs’ settings.';
 const scenario=$('experiment-sample').value==='scenarioforge-xml';
 const roles=scenario?['scenarioforge','participant']:['scenarioforge','participant','core'];
 if(roles.some(role=>!snapshot.roles?.[role]))return 'Select and save '+(scenario?'ScenarioForge and participant':'ScenarioForge, participant and CoreVM')+' roles in Lab setup.';
 if(scenario){
  const choice=scenarioChoices.find(item=>item.id===$('scenario-selection').value);
  if(scenarioChoiceVM!==snapshot.roles.scenarioforge||!choice?.resolved_chain)return 'Choose a resolved scenario on the Scenario step.';
  if(!$('scenario-allowed').value.trim())return 'Enter allowed target IPs or CIDRs on the Agent & network step.';
 }
 const taskError=taskValidationMessage();if(taskError)return 'Evaluation: '+taskError;
 try{experimentControls();}catch(error){return error.message;}
 const taskDefs=scenarioTaskDefinitions;
 if(taskDefs?.some(t=>['judge','both'].includes(t.verification_mode))&&!$('judge-enabled').checked)return 'Enable Judge LLM for judge/both task verification.';
 const invalid=[...$('experiment-form').elements].find(input=>input.willValidate&&(!input.validity.valid||(input.required&&typeof input.value==='string'&&!input.value.trim())));
 if(invalid){const panel=invalid.closest('[data-evaluation-page], [id^="experiment-panel-"], [role="tabpanel"]');return 'Complete the required fields on the '+(panel?(panel.dataset.evaluationPage?wizardSteps.find(step=>step.evaluation===panel.dataset.evaluationPage)?.label:wizardSteps.find(step=>step.panel===panel.id)?.label)||'Experiment':'Experiment')+' step.';}
 if(modelDrafts.participant){
  const form=$('model-config-participant');
  if(!form||[...form.elements].some(input=>input.willValidate&&(!input.validity.valid||(input.required&&!input.value.trim()))))return 'Complete the required model fields on the Agent & network step.';
  // Pulling settings is read-only. Maintenance access is needed only when
  // applying edits, not when creating an experiment with saved defaults.
  if(modelDrafts.participant.dirty||modelDrafts.participant.saveError){
   if(!(snapshot.model_config_writable??snapshot.updates?.can_update))return 'Applying changed model settings requires application maintenance access.';
   if(modelDrafts.participant.saveError)return 'Model settings were not applied: '+modelDrafts.participant.saveError;
   return 'Apply the changed model settings on the Agent & network step.';
  }
 }
 return '';
}
function syncScenarioReferences(){
 $('scenario-reference-choice').disabled=!(scenarioChoiceVM===snapshot?.roles?.scenarioforge&&$('scenario-selection').value&&$('experiment-sample').value==='scenarioforge-xml');
}
$('scenario-reference-choice').addEventListener('change',event=>{
 const selection=$('scenario-selection').value,kind=event.target.value;
 if(!kind)return;event.target.value='';
 const url='/scenario-reference?selection='+encodeURIComponent(selection)+'&kind='+encodeURIComponent(kind);
 const opened=window.open(url,'caf_reference_'+kind,'popup,width=1100,height=850,resizable=yes,scrollbars=yes');
 if(opened){opened.focus();$('scenario-reference-notice').hidden=true;}else{const notice=$('scenario-reference-notice');notice.hidden=false;notice.replaceChildren(el('span','Allow popups for this site, or '));const link=el('a','open the scenario reference');link.href=url;link.target='_blank';link.rel='noopener';notice.append(link);}
});
function syncCreateExperiment(){
 syncJudge();
 syncScenarioReferences();
 const busy=isBusy()||experimentCreating;
 const reason=busy?'Wait for the current operation to finish.':experimentMissingFields();
 const button=$('create-experiment'),tooltip=$('create-experiment-tooltip');
 button.disabled=Boolean(reason);
 const hoverReason=reason?'Create experiment unavailable: '+reason:'';
 button.title=hoverReason;tooltip.title=hoverReason;
 const hint=$('create-experiment-hint');if(hint.textContent!==reason)hint.textContent=reason;
 hint.hidden=!reason;
 syncWizard();
}
$('experiment-builder').addEventListener('input',event=>{if(event.target.id!=='wizard-step-select')syncCreateExperiment();});
$('experiment-builder').addEventListener('change',syncCreateExperiment);

// Navigation is separate from saved experiment data: moving between steps never
// rebuilds inputs, pulls VM settings, or changes the selected resources.
const wizardSteps=[
 {id:'overview',label:'Experiment',group:'Setup',panel:'experiment-panel-overview',description:'Start with a sample or use your own ScenarioForge scenario.'},
 {id:'scenario',label:'Scenario',group:'Setup',panel:'experiment-panel-scenario',description:'Choose the scenario to deploy. Samples include a fixed scenario.'},
 {id:'caf',label:'Agent & network',group:'Setup',panel:'experiment-panel-caf',description:'Review the participant model and target network. Apply any changed model settings before continuing.'},
 {id:'tasks',label:'Task',group:'Agent resources',evaluation:'settings',description:'Review what the agent will be asked to do and how completion is checked.'},
 {id:'tools',label:'Run configurations',group:'Agent resources',evaluation:'tools',description:'Choose tools and guidance for each run configuration. One configuration is usually enough.'},
 {id:'helpers',label:'Helpers',group:'Agent resources',evaluation:'helpers',description:'Optionally add tool helpers or generated guidance to your run configurations.'},
 {id:'limits',label:'Trial limits',group:'Evaluation',evaluation:'limits',description:'Set repetitions, agent turns, time limits and context size. Sample defaults are editable.'},
 {id:'hints',label:'Hints & solutions',group:'Evaluation',evaluation:'hints',description:'Choose whether and when the agent receives progressive help.'},
 {id:'controls',label:'Run controls',group:'Evaluation',evaluation:'controls',description:'Review lab resets, configuration order and optional cost accounting.'},
 {id:'judge',label:'LLM judge',group:'Evaluation',evaluation:'judge',description:'Choose the model that reviews execution evidence and scores task completion.'},
 {id:'review',label:'Review & create',group:'Review',panel:'experiment-review',description:'Check the complete experiment before saving. Creating it does not start a run.'},
];
function selectExperimentTab(name,options={}){showWizardStep(name==='evaluation'?'tasks':name,options);}
function selectEvaluationPage(name,options={}){showWizardStep(wizardSteps.find(step=>step.evaluation===name)?.id||'tasks',options);}
function leaveExperimentWizard(){location.hash='experiments';if(wizardReady)syncWizard();}
function wizardInvalidInput(panel){return [...panel.querySelectorAll('input,select,textarea')].find(input=>input.willValidate&&(!input.validity.valid||(input.required&&!input.value.trim())));}
function wizardStepProblem(step){
 if(!wizardDraft||!snapshot)return '';
 const scenario=$('experiment-sample').value==='scenarioforge-xml';
 if(step.id==='overview'){
  if(wizardRolesChanged())return 'Lab VMs changed. Return to Experiments and choose New to load the new VMs’ settings.';
  if(!$('experiment-sample').value)return 'Choose an experiment type.';
  if(rolesDirty)return 'Save your VM selections in Lab setup first.';
  if(!snapshot.roles?.participant||!snapshot.roles?.scenarioforge||(!scenario&&!snapshot.roles?.core))return 'Choose the participant, ScenarioForge and CORE VMs in Lab setup first.';
 }
 if(step.id==='scenario'&&scenario){
  if(scenarioChoiceVM!==snapshot.roles?.scenarioforge||!$('scenario-selection').value)return 'Load and select a scenario from your ScenarioForge VM.';
  if(!scenarioChoices.find(item=>item.id===$('scenario-selection').value)?.resolved_chain)return 'Resolve and save the scenario’s Flow chain in ScenarioForge first.';
 }
 if(step.id==='caf'){
  if(scenario&&!$('scenario-allowed').value.trim())return 'Enter allowed target IPs or CIDRs.';
  const invalid=wizardInvalidInput($('experiment-panel-caf'));if(invalid)return 'Complete '+(invalid.labels?.[0]?.textContent.trim()||'the required model fields')+'.';
  if(modelDrafts.participant?.saveError)return 'Model settings were not applied: '+modelDrafts.participant.saveError;
  if(modelDrafts.participant?.dirty)return 'Apply the changed participant model settings first.';
 }
 if(step.id==='tasks'){const problem=taskValidationMessage();if(problem)return problem;}
 if(step.id==='tools'){try{validateRunConfigurations();}catch(error){return error.message;}}
 if(step.id==='controls'){try{experimentPricing();}catch(error){return error.message;}}
 if(step.id==='judge'&&!$('judge-enabled').checked&&scenarioTaskDefinitions?.some(task=>['judge','both'].includes(task.verification_mode)))return 'Enable the LLM judge for these tasks.';
 const panel=$(step.panel||'evaluation-page-'+step.evaluation);
 if(step.id!=='review'&&panel){const invalid=wizardInvalidInput(panel);if(invalid){const label=invalid.labels?.[0]?.textContent.trim()||'required field';return 'Check '+label+'.';}}
 return '';
}
function wizardBlocker(index,problems=wizardSteps.map(wizardStepProblem)){
 if(!wizardDraft||!snapshot)return null;
 const blocked=problems.findIndex((problem,position)=>position<index&&problem);
 return blocked<0?null:{step:wizardSteps[blocked],problem:problems[blocked]};
}
function wizardBlockReason(blocker){return 'Complete '+blocker.step.label+' first: '+blocker.problem;}
function showWizardStep(id,{focus=false}={}){
 const next=wizardSteps.findIndex(step=>step.id===id);if(next<0)return false;
 if(wizardBlocker(next)){syncWizard();return false;}
 wizardStep=next;wizardShowErrors=false;if(wizardDraft)wizardVisited.add(id);
 const step=wizardSteps[next];
 for(const name of ['overview','scenario','caf','evaluation'])$('experiment-panel-'+name).hidden=step.panel!=='experiment-panel-'+name&&!(name==='evaluation'&&step.evaluation);
 for(const panel of document.querySelectorAll('[data-evaluation-page]'))panel.hidden=panel.dataset.evaluationPage!==step.evaluation;
 $('experiment-review').hidden=id!=='review';
 $('wizard-step-title').textContent=step.label;$('wizard-step-description').textContent=step.description;
 $('wizard-step-number').textContent=`${step.group.toUpperCase()} · STEP ${next+1} OF ${wizardSteps.length}`;
 syncWizard();
 if(focus){$('wizard-step-title').focus({preventScroll:true});$('experiment-builder').scrollIntoView({block:'start',behavior:'instant'});}
 return true;
}
function syncWizard(){
 if(!wizardReady)return;
 const problems=wizardSteps.map(wizardStepProblem),blocker=wizardBlocker(wizardStep,problems);
 if(blocker){showWizardStep(blocker.step.id);return;}
 const step=wizardSteps[wizardStep],busy=isBusy()||experimentCreating;
 $('resume-experiment').hidden=!wizardDraft;
 $('wizard-position').textContent=`Step ${wizardStep+1} of ${wizardSteps.length}`;
 $('wizard-progress').value=wizardStep+1;$('wizard-progress').setAttribute('aria-valuetext',`${step.label}, step ${wizardStep+1} of ${wizardSteps.length}`);
 $('wizard-step-select').value=step.id;$('wizard-step-select').disabled=busy;
 for(const option of $('wizard-step-select').options){const index=wizardSteps.findIndex(item=>item.id===option.value),blocker=wizardBlocker(index,problems);option.disabled=Boolean(blocker);option.title=blocker?wizardBlockReason(blocker):'';}
 for(const button of document.querySelectorAll('[data-wizard-step]')){
  const current=button.dataset.wizardStep===step.id,descriptor=wizardSteps.find(item=>item.id===button.dataset.wizardStep);
  if(current)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');
  const index=wizardSteps.indexOf(descriptor),blocker=wizardBlocker(index,problems),visited=wizardVisited.has(descriptor.id),problem=visited&&descriptor.id!=='review'?problems[index]:'';
  const reason=busy?'Wait for the current operation to finish.':blocker?wizardBlockReason(blocker):'';
  button.disabled=Boolean(reason);button.title=reason;button.parentElement.title=reason;button.setAttribute('aria-disabled',String(Boolean(reason)));
  button.dataset.state=blocker?'locked':problem?'incomplete':visited?'visited':'pending';
  button.querySelector('.wizard-nav-state').textContent=blocker?'Complete '+blocker.step.label:problem?'Needs attention':visited&&descriptor.id!=='review'?'Reviewed':'';
 }
 $('wizard-back').disabled=busy||wizardStep===0;
 const nextReason=busy?'Wait for the current operation to finish.':problems[wizardStep];
 $('wizard-next').disabled=Boolean(nextReason);$('wizard-next').title=nextReason?'Continue unavailable: '+nextReason:'';$('wizard-next').hidden=step.id==='review';
 $('wizard-next-label').textContent=step.id==='review'?'Save now; start from the experiment list.':nextReason?'To continue: '+nextReason:'Next: '+wizardSteps[wizardStep+1].label;
 $('create-experiment-tooltip').hidden=step.id!=='review';$('create-experiment-hint').hidden=step.id!=='review'||!$('create-experiment-hint').textContent;
 const problem=wizardShowErrors?wizardStepProblem(step):'';$('wizard-step-error').textContent=problem;$('wizard-step-error').hidden=!problem;
 if(step.id==='review'&&wizardDraft)renderWizardReview();
}
function renderWizardReview(){
 const target=$('wizard-review');target.replaceChildren();
 const sample=snapshot?.samples?.items?.find(item=>item.id===$('experiment-sample').value),scenario=$('experiment-sample').value==='scenarioforge-xml';
 const selection=scenarioChoices.find(item=>item.id===$('scenario-selection').value);
 const invalid=wizardSteps.filter(step=>step.id!=='review').map(step=>({step,problem:wizardStepProblem(step)})).filter(item=>item.problem);
 const missing=experimentMissingFields(),incomplete=invalid.length||missing;
 const status=el('div',null,'wizard-review-status '+(incomplete?'incomplete':'ready'));
 status.append(el('strong',incomplete?'A few details still need attention':'Ready to create'),el('p',incomplete?'Use the edit links below to finish your experiment.':'The settings below will be saved together. You can start the experiment from the list when ready.'));
 if(missing&&!invalid.length)status.append(el('p',missing,'error'));target.append(status);
 for(const {step,problem} of invalid){const button=el('button',step.label+': '+problem,'wizard-review-fix');button.type='button';button.addEventListener('click',()=>showWizardStep(step.id,{focus:true}));target.append(button);}
 const section=(id,title,rows)=>{
  const card=el('section',null,'wizard-review-card'),heading=el('div',null,'wizard-review-heading'),edit=el('button','Edit '+title);
  edit.type='button';edit.addEventListener('click',()=>showWizardStep(id,{focus:true}));heading.append(el('h3',title),edit);card.append(heading);
  const list=el('dl');for(const [key,value] of rows){list.append(el('dt',key),el('dd',value==null||value===''?'Not set':String(value)));}card.append(list);target.append(card);return card;
 };
 section('overview','Experiment', [['Type',scenario?'ScenarioForge XML or bundle':sample?.name],['Lab VMs',`ScenarioForge ${snapshot?.roles?.scenarioforge||'not selected'} · CORE ${snapshot?.roles?.core||'not selected'} · Participant ${snapshot?.roles?.participant||'not selected'}`]]);
 section('scenario','Scenario',[['Scenario',scenario?(selection?.name||selection?.scenario||selection?.label||$('scenario-selection').selectedOptions[0]?.textContent):sample?.profile?.companion_xml||'Fixed sample scenario'],['Source',scenario?'Saved on the selected ScenarioForge VM':'Included with the sample'],['XML',scenario?selection?.path||selection?.xml||selection?.id:sample?.profile?.companion_xml]]);
 const modelValue=key=>$('model-participant-'+key)?.value||snapshot?.model_configs?.participant?.[key];
 section('caf','Agent & network',[['Provider',modelValue('provider')],['Endpoint',modelValue('url')],['Model',modelValue('model')],['Verify participant TLS',$('model-participant-ssl')?.checked?'Yes':'No'],['Allowed targets',scenario?$('scenario-allowed').value:'Fixed by the sample scenario'],['Excluded targets',scenario?$('scenario-disallowed').value:'Sample scope and model exclusions'],['Credentials','Stored credentials are reused; secret values are not displayed.']]);
 const tasks=scenario?scenarioTaskDefinitions||[]:[];
 const taskCard=section('tasks','Task',[['Task source',scenario?'ScenarioForge saved/default tasks':'Fixed sample task'],['Tasks',scenario?tasks.length:1]]);
 if(tasks.length)for(const [index,task] of tasks.entries()){const block=el('details');block.open=tasks.length===1;block.append(el('summary',task.name||task.id||'Task '+(index+1)),el('p',task.prompt||'No prompt available','wizard-review-prompt'),el('p',taskVerificationSummary(task),'small'),el('p','Required readiness: '+(task.required_checks||[]).join(', '),'small'));if(task.rubric?.criteria?.length){const criteria=el('ul');for(const item of task.rubric.criteria)criteria.append(el('li',item.description||item.requirement||item.id));block.append(criteria);}taskCard.append(block);}
 else taskCard.append(el('p',sample?.profile?.prompt||$('sample-prompt').value||'Select a task to review its prompt.','wizard-review-prompt'));
 const configCard=section('tools','Run configurations',[['Configurations',customConditions.length||((sample?.id==='tools-vs-helper')?2:1)],['Planned trials',(customConditions.length||((sample?.id==='tools-vs-helper')?2:1))*Number($('eval-repetitions').value)*(scenario?tasks.length:1)],['Dangerous-level actions',$('auto-approve-dangerous').checked?'Allowed for selected tools':'Require approval']]);
 if(customConditions.length)for(const row of customConditions){const details=el('details');details.append(el('summary',`${configurationName(row)} · ${row.tools.length} tools · ${(row.guidance_assets||[]).length} guidance documents`),el('p',row.tools.join(', ')||'No tools selected','wizard-review-prompt'));const assets=(snapshot?.participant_guidance_assets||[]).filter(item=>(row.guidance_assets||[]).includes(item.id));if(assets.length)details.append(el('p','Guidance: '+assets.map(item=>item.name).join(', ')));if(row.guidance)details.append(el('p',row.guidance,'wizard-review-prompt'));configCard.append(details);}
 else configCard.append(el('p',sample?.profile?.tools||'Use the sample’s preset tools and guidance.'));
 const helpers=[...selectedToolHelpers,...selectedGeneratedHelpers].map(id=>[...helperCatalogEntries(),...(snapshot?.generated_helpers||[])].find(item=>item.id===id)?.name||id);
 const helperCard=section('helpers','Helpers',[['Enabled helpers',helpers.join(', ')||'None']]);for(const helper of helperCatalogEntries().filter(item=>selectedToolHelpers.has(item.id)))helperCard.append(el('p',helper.name+' adds: '+(helper.added_tools||helper.tools||[]).join(', '),'small'));
 section('limits','Trial limits',[['Repetitions per task / run configuration',$('eval-repetitions').value],['Maximum agent turns',$('eval-max_turns').value],['Participant active time',timeoutLabel($('eval-wall_seconds').value)],['Tool timeout',timeoutLabel($('eval-tool_timeout').value)],['Context window',$('eval-context_window').value+' tokens']]);
 section('hints','Hints & solutions',[['Progressive hints',$('provide-progressive-hints').checked?'Enabled':'Disabled'],['Turns without progress before a hint',$('eval-hint_stalled_turns').value],['Tries before walkthrough and answer',$('eval-max_tries_before_solution').value]]);
 const prices=['participant-input','participant-output','judge-input','judge-output','generation'].filter(key=>$('price-'+key).value).map(key=>key.replaceAll('-',' ')+': $'+$('price-'+key).value);
 section('controls','Run controls',[['Reset lab before every trial',$('reset-each-trial').checked?'Yes':'No'],['Extra reset hooks',$('extra-reset-hooks').checked?'Enabled':'Disabled'],['Run configuration order seed',$('condition-order-seed').value],['Cost accounting',prices.join('; ')||'No prices supplied']]);
 section('judge','LLM judge',[['Judge',$('judge-enabled').checked?'Enabled':'Disabled'],['Model',$('judge-inherit').checked?'Uses participant model: '+(modelValue('model')||'not set'):$('judge-name').value],['Endpoint',$('judge-inherit').checked?modelValue('url'):$('judge-url').value],['Maximum judge turns',$('judge-max_turns').value],['Judge time limit',timeoutLabel($('judge-timeout_seconds').value)],['Output tokens per call',$('judge-max_tokens').value],['Intermediate progress review',$('judge-monitor_progress').checked?'Enabled':'Disabled'],['Progress check time limit',timeoutLabel($('judge-progress_timeout_seconds').value)],['Maximum progress checks',$('judge-progress_max_checks').value],['Credentials',$('judge-inherit').checked?'Stored host key for the participant endpoint':$('judge-clear-api-key').checked?'Clear stored key':$('judge-api-key').value?'New key supplied (hidden)':'Reuse stored host key']]);
}
function initExperimentWizard(){
 const navigation=$('wizard-navigation');let group='';
 for(const [index,step] of wizardSteps.entries()){
  if(step.group!==group){group=step.group;navigation.append(el('p',group,'wizard-nav-group'));}
  const button=el('button',null,'wizard-nav-step');button.type='button';button.dataset.wizardStep=step.id;button.setAttribute('aria-controls',step.panel||'evaluation-page-'+step.evaluation);
  const text=el('span',null,'wizard-nav-text');text.append(el('strong',step.label),el('small','','wizard-nav-state'));button.append(el('span',String(index+1),'wizard-nav-number'),text);
  button.addEventListener('click',()=>showWizardStep(step.id,{focus:true}));const item=el('div',null,'wizard-nav-item');item.append(button);navigation.append(item);$('wizard-step-select').add(new Option(`${index+1}. ${step.label}`,step.id));
 }
 $('wizard-step-select').addEventListener('change',event=>showWizardStep(event.target.value,{focus:true}));
 $('wizard-back').addEventListener('click',()=>{if(wizardStep>0)showWizardStep(wizardSteps[wizardStep-1].id,{focus:true});});
 $('wizard-next').addEventListener('click',()=>{const step=wizardSteps[wizardStep],problem=wizardStepProblem(step);if(problem){wizardShowErrors=true;syncWizard();const panel=$(step.panel||'evaluation-page-'+step.evaluation);const input=panel&&wizardInvalidInput(panel);if(input){input.reportValidity();input.focus();}return;}if(wizardStep<wizardSteps.length-1)showWizardStep(wizardSteps[wizardStep+1].id,{focus:true});});
 $('resume-experiment').addEventListener('click',()=>{location.hash='experiments/new';showWizardStep(wizardSteps[wizardStep].id);});
 wizardReady=true;showWizardStep('overview');
}

function validateExperimentFields(form){
 const invalid=[...form.elements].find(input=>input.willValidate&&!input.validity.valid);
 if(!invalid)return true;
 const panel=invalid.closest('[data-evaluation-page], [id^="experiment-panel-"], [role="tabpanel"]');
 if(panel?.dataset.evaluationPage){selectExperimentTab('evaluation');selectEvaluationPage(panel.dataset.evaluationPage);}else if(panel)selectExperimentTab(panel.id.replace('experiment-panel-',''));
 invalid.reportValidity();invalid.focus();return false;
}

function judgeSettings(){
 const enabled=$('judge-enabled').checked;
 if(!enabled)return {enabled:false};
 const settings={enabled:true,monitor_progress:$('judge-monitor_progress').checked,use_participant_model:$('judge-inherit').checked,...Object.fromEntries(['max_turns','timeout_seconds','max_tokens','progress_timeout_seconds','progress_max_checks'].map(key=>[key,Number($('judge-'+key).value)]))};
 if(!settings.use_participant_model)settings.model={provider:$('judge-provider').value,url:$('judge-url').value.trim(),name:$('judge-name').value.trim(),ssl_verify:$('judge-ssl').checked};
 if(!settings.use_participant_model){
  if($('judge-api-key').value&&!$('judge-clear-api-key').checked)settings.api_key=$('judge-api-key').value;
  if($('judge-clear-api-key').checked)settings.clear_api_key=true;
 }
 return settings;
}
function syncJudge(){
 const enabled=$('judge-enabled').checked,inherited=$('judge-inherit').checked;
 $('judge-inherit').disabled=!enabled;
 $('judge-model-settings').disabled=!enabled||$('judge-inherit').checked;
 if(enabled&&$('judge-inherit').checked&&$('model-config-participant')){const current=modelFormSettings('participant');$('judge-provider').value=current.provider;$('judge-url').value=current.url;$('judge-name').value=current.model;$('judge-ssl').checked=current.ssl_verify;}
 $('judge-credentials').disabled=!enabled||inherited;
 $('judge-api-key').disabled=!enabled||inherited||$('judge-clear-api-key').checked;
 $('judge-credentials').title=inherited?'Uncheck the participant model option to edit judge credentials.':'';
 const keyStatus=(snapshot?.experiment_defaults?.judge_credential_status||[]).find(row=>row.endpoint===$('judge-url').value.trim().replace(/\/+$/,''));
 $('judge-key-status').textContent=!enabled?'':inherited?(keyStatus?.configured?`Using the stored judge key from ${keyStatus.source} for the inherited endpoint.`:'No host judge key is available for the inherited endpoint. Uncheck the participant model option to enter a key if authentication is required.'):$('judge-clear-api-key').checked?'New experiments will use no judge key for this endpoint.':$('judge-api-key').value?'New key entered; saved when the experiment is created.':keyStatus?.configured?`A judge key is available from ${keyStatus.source}. Leave the field blank to reuse it.`:'No saved judge key for this endpoint. Enter one if the server requires authentication.';
 $('judge-limits').disabled=!enabled;
 $('judge-monitor_progress').disabled=!enabled;
 for(const key of ['progress_timeout_seconds','progress_max_checks'])$('judge-'+key).disabled=!enabled||!$('judge-monitor_progress').checked;
 syncTimeoutControls();
}
function resetJudge(){
 $('judge-api-key').value='';$('judge-clear-api-key').checked=false;
 const config=snapshot?.experiment_defaults?.judge;
 $('judge-enabled').checked=true;
 $('judge-monitor_progress').checked=config?.monitor_progress!==false;
 $('judge-inherit').checked=true;
 const model=config?.model||snapshot?.experiment_defaults?.model||{};
 $('judge-provider').value=['openai','litellm','ollama_direct'].includes(model.provider)?model.provider:'openai';
 $('judge-url').value=model.url||'';$('judge-name').value=model.name||'';$('judge-ssl').checked=model.ssl_verify!==false;
 for(const [key,value] of Object.entries({max_turns:6,timeout_seconds:600,max_tokens:2048,progress_timeout_seconds:600,progress_max_checks:32}))$('judge-'+key).value=config?.[key]??value;
 syncTimeoutControls(true);syncJudge();
}
for(const id of ['judge-enabled','judge-inherit','judge-monitor_progress','judge-clear-api-key'])$(id).addEventListener('change',()=>{syncJudge();syncCreateExperiment();});
for(const id of ['judge-api-key','judge-url'])$(id).addEventListener('input',syncJudge);

let customEvaluation={};
const evaluationKeys=['repetitions','max_turns','wall_seconds','tool_timeout','context_window','hint_stalled_turns','max_tries_before_solution'];
function syncHintThreshold(){
 const input=$('eval-hint_stalled_turns'),enabled=$('provide-progressive-hints').checked,maxTurns=Number($('eval-max_turns').value);
 input.disabled=!enabled;
 input.max=String(Math.max(1,maxTurns-1));
 input.setCustomValidity(enabled&&maxTurns<=1?'Maximum agent turns must be at least 2 when progressive hints are enabled.':enabled&&Number(input.value)>=maxTurns?'Turns without progress before a hint must be less than maximum agent turns.':'');
}
function rememberEvaluationSettings(){
 if(!$('evaluation-settings').disabled)customEvaluation[$('experiment-sample').value]=Object.fromEntries(evaluationKeys.map(key=>[key,Number($('eval-'+key).value)]));
 updateEvaluationBudget();syncConditionOverview();
}
function renderEvaluationSettings(scenario,sample){
 const defaults={hint_stalled_turns:2,max_tries_before_solution:6,...(snapshot?.experiment_defaults?.evaluation||{repetitions:1,max_turns:snapshot?.scenarios?.max_turns||20,wall_seconds:snapshot?.scenarios?.wall_seconds||300,tool_timeout:60,context_window:8192})};
 const preset=scenario?defaults:{...defaults,repetitions:sample?.profile?.repetitions||1,max_turns:sample?.max_turns||3,wall_seconds:sample?.wall_seconds||120,tool_timeout:sample?.profile?.tool_timeout||30};
 const values=customEvaluation[$('experiment-sample').value]||preset;
 for(const key of evaluationKeys)$('eval-'+key).value=values[key];
 $('evaluation-settings').disabled=false;syncTimeoutControls(true);
 configureEvaluationTasks(scenario,sample);
 $('sample-scenario-info').hidden=scenario;
 if(!scenario){
  $('sample-scenario-xml').href='/demo-'+sample.id+'.xml';
  $('sample-scenario-bundle').href='/demo-'+sample.id+'.zip';
  $('sample-sf-use').value=sample?.profile?.scenarioforge_used?'Required · deploy, readiness check and evaluation export':'Not used by this bundled sample';
  $('sample-sf-companion').value=(sample?.profile?.companion_xml||'Unavailable')+' · fixed sample XML';
  $('sample-environment').value=sample?.profile?.environment||'Sample-defined environment';
 $('sample-prompt').value=sample?.profile?.prompt||'Sample-defined prompt';
 }
 syncHintThreshold();
 $('eval-settings-note').textContent=scenario?'These settings are saved with this experiment. Reruns reuse them.':'Sample defaults are prefilled. Trial settings are editable and saved with this experiment; reruns reuse them. Sample tasks remain fixed. Choose resources on Run configurations and Helpers.';
 $('eval-tools').textContent='Run configurations: '+(scenario?'Use the run configuration editor, or load installed CAF MCP tools from the participant VM. Enable optional additions on Helpers.':sample?.profile?.tools||'Sample-defined');
 $('eval-task-source').textContent=scenario?'ScenarioForge exports the selected task definitions after deployment. Prompts and private verifiers are captured in Results.':'The deployed host address and fresh token/flags are resolved at run time. Exact prompts and XML are captured in Results.';
}
function updateEvaluationBudget(){
 const selection=$('experiment-sample').value;
 $('experiment-budget').textContent=selection==='scenarioforge-xml'?'Baseline tools · configure repetitions and trial limits below':`${Number($('eval-repetitions').value)*(selection==='tools-vs-helper'?2:1)} trials · up to ${$('eval-max_turns').value} turns · ${timeoutLabel($('eval-wall_seconds').value)} participant active time per trial`;
}
$('evaluation-settings').addEventListener('input',()=>{syncHintThreshold();rememberEvaluationSettings();});
for(const key of ['hint_stalled_turns','max_tries_before_solution'])$('eval-'+key).addEventListener('input',rememberEvaluationSettings);
$('provide-progressive-hints').addEventListener('change',()=>{syncHintThreshold();rememberEvaluationSettings();syncCreateExperiment();});

function describeSample(){
 const scenario=$('experiment-sample').value==='scenarioforge-xml',sample=snapshot?.samples?.items.find(s=>s.id===$('experiment-sample').value);
 $('scenario-options').hidden=!scenario;
 $('scenario-caf-scope').hidden=!scenario;
 $('scenario-allowed').required=scenario;
 renderEvaluationSettings(scenario,sample);
 ensureParticipantCondition();renderConditions();
 $('experiment-description').textContent=scenario?'Deploy and evaluate an existing ScenarioForge scenario.':sample?.description||'';
 updateEvaluationBudget();
}

async function startNewExperiment(){if(isBusy())return;if(wizardDraft&&!confirm('Discard the current experiment draft and start a new one?'))return;customEvaluation={};customConditions=[];resourceDefaultsLoaded=false;participantCatalogReady=false;activeCondition=null;selectedToolHelpers=new Set();selectedGeneratedHelpers=new Set();$('helper-search').value='';selectHelperView('tools');renderConditions();for(const id of ['price-participant-input','price-participant-output','price-judge-input','price-judge-output','price-generation'])$(id).value='';$('condition-order-seed').value='42';$('reset-each-trial').checked=true;$('extra-reset-hooks').checked=false;$('extra-reset-hooks').disabled=!snapshot?.extra_reset_hooks_available;$('auto-approve-dangerous').checked=Boolean(snapshot?.experiment_defaults?.auto_approve_dangerous);const cachedTools=snapshot?.condition_catalogs?.['participant-vm']?.tools?.length||0,cachedAssets=snapshot?.participant_guidance_assets?.length||0;$('participant-tools-status').textContent=cachedTools?`${cachedTools} participant tools and ${cachedAssets} CAF guidance assets are cached. Load again to refresh them from the VM.`:'Load the participant’s command-line and generated MCP tools plus CAF guides, playbooks and document artifacts.';selectEvaluationPage('settings');resetJudge();lastUploadedScenarioFile=null;$('scenario-file').value='';syncScenarioUploadButton();$('provide-progressive-hints').checked=true;resetTaskEditor();const select=$('experiment-sample');select.replaceChildren();for(const sample of snapshot?.samples?.items||[])select.add(new Option('Sample - '+sample.name,sample.id));if(snapshot?.scenarios?.enabled)select.add(new Option('ScenarioForge XML or bundle','scenarioforge-xml'));$('scenario-allowed').value='';$('scenario-disallowed').value=snapshot?.scenarios?.disallowed_targets||'';$('experiment-error').textContent='';$('experiment-save-progress').hidden=true;creationSteps=[];describeSample();selectExperimentTab('overview');wizardRoles={...snapshot.roles};wizardDraft=true;wizardStep=0;wizardVisited.clear();wizardShowErrors=false;location.hash='experiments/new';showWizardStep('overview');describeScenarioSelection();syncCreateExperiment();await loadModelNetworkScope();await loadParticipantTools();}
$('new-experiment').addEventListener('click',startNewExperiment);
let scenarioChoices=[],scenarioChoiceVM=null,lastUploadedScenarioFile=null;
let autoExcludedTargets=[];
function applyModelNetworkScope(scope){
 const input=$('scenario-disallowed');
 const existing=input.value.split(',').map(value=>value.trim()).filter(Boolean).filter(value=>!autoExcludedTargets.includes(value));
 autoExcludedTargets=scope.excluded_targets||[];
 input.value=[...new Set([...existing,...autoExcludedTargets])].join(', ');
 $('scenario-scope-info').textContent=`LLM provider: ${scope.provider_host} · excluded: ${autoExcludedTargets.join(', ')||'unresolved'}. ${(scope.warnings||[]).join(' ')}`;
 syncCreateExperiment();
}
async function loadModelNetworkScope(){
 autoExcludedTargets=[];
 if(!snapshot?.roles?.participant)return;
 operation='Resolving the applied LLM provider and route on the participant VM…';syncBusy();
 try{
  const response=await apiFetch('/api/model-network-scope',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});
  const scope=await dashboardJSON(response);if(!response.ok)throw Error(scope.error||'Unable to resolve the LLM route');
  applyModelNetworkScope(scope);
 }catch(error){$('scenario-scope-info').textContent='Review exclusions: '+error.message;}
 finally{operation=null;syncBusy();schedulePoll();}
}
function scenarioModifiedTime(item){const epoch=Number(item?.modified_epoch);if(Number.isFinite(epoch))return epoch*1000;const parsed=Date.parse(item?.modified_at||'');return Number.isFinite(parsed)?parsed:0;}
function scenarioModifiedLabel(item){const value=scenarioModifiedTime(item);return value?new Date(value).toLocaleString([], {dateStyle:'short',timeStyle:'short'}):'timestamp unavailable';}
function scenarioFileIdentity(file){return file?file.name+'\0'+file.size+'\0'+file.lastModified:'';}
function syncScenarioUploadButton(){const file=$('scenario-file').files[0];$('upload-scenario').disabled=!file||scenarioFileIdentity(file)===lastUploadedScenarioFile;}
for(const choice of document.querySelectorAll('input[name="scenario-source"]'))choice.addEventListener('change',()=>{
 const upload=document.querySelector('input[name="scenario-source"]:checked').value==='upload';
 $('scenario-upload-options').hidden=!upload;$('scenario-find-options').hidden=upload;
 scenarioChoices=[];scenarioChoiceVM=null;taskScenarioChanged();
 $('scenario-selection').replaceChildren(new Option(upload?'Upload a scenario to choose an XML':'Load scenarios to choose an XML',''));
 $('scenario-allowed').value='';$('scenario-selection-info').textContent='';$('scenario-upload-info').textContent='Import saves the scenario on the selected VM; it does not deploy it. Maximum 32 MiB (128 MiB expanded).';
 $('scenario-file').value='';syncScenarioUploadButton();syncCreateExperiment();
});
function describeScenarioSelection(){
 const choice=scenarioChoices.find(item=>item.id===$('scenario-selection').value);
 $('scenario-allowed').value=(choice?.target_subnets||[]).join(', ');
 syncCreateExperiment();
 $('scenario-selection-info').textContent=choice?choice.path+' · '+choice.scenario+' · modified '+scenarioModifiedLabel(choice)+' · '+choice.chain_length+' Flow steps':'Select a saved scenario with a resolved Flow chain.';
}
$('scenario-selection').addEventListener('change',()=>{describeScenarioSelection();taskScenarioChanged();});
$('load-scenarios').addEventListener('click',async()=>{
 if(isBusy())return;
 operation='Loading saved scenarios from the ScenarioForge VM…';syncBusy();
 try{
  const response=await apiFetch('/api/scenarios/list',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({query:''})});
  const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Unable to load scenarios');
  showScenarioChoices(result);
  const newest=scenarioChoices.find(item=>item.resolved_chain);
  if(newest){$('scenario-selection').value=newest.id;describeScenarioSelection();taskScenarioChanged();$('scenario-selection-info').textContent='Selected newest resolved scenario on VM '+result.vmid+': '+newest.path+' · '+newest.scenario+' · modified '+scenarioModifiedLabel(newest)+' · '+newest.chain_length+' Flow steps'+(result.truncated?' · searched the 50 newest matches':'');}
  else $('scenario-selection-info').textContent=result.items.length?'Saved XML files were found on VM '+result.vmid+', but none has a resolved Flow chain. Finish and save Flag Sequencing, then Generate Preview Plan and reload.':'No saved XML found in '+result.roots.join(', ')+'.';
 }catch(error){$('scenario-selection-info').textContent=error.message;}
 finally{operation=null;syncBusy();schedulePoll();}
});

function showScenarioChoices(result){
 scenarioChoices=[...result.items].sort((left,right)=>scenarioModifiedTime(right)-scenarioModifiedTime(left)||String(left.scenario||'').localeCompare(String(right.scenario||''))||String(left.path||'').localeCompare(String(right.path||'')));scenarioChoiceVM=result.vmid;taskScenarioChanged();
 const select=$('scenario-selection');select.replaceChildren(new Option('Choose a saved scenario',''));
 $('scenario-allowed').value='';
 for(const item of scenarioChoices){const option=new Option(scenarioModifiedLabel(item)+' — '+item.scenario+' — '+item.path.split('/').pop()+(item.resolved_chain?'':' (resolve Flow chain first)'),item.id);option.disabled=!item.resolved_chain;select.add(option);}
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
  taskScenarioChanged();
  $('scenario-upload-info').textContent='Imported to VM '+result.vmid+' · '+result.kind+' · '+result.fidelity+'. '+(ready.length?'Select the scenario below, review scope, then create the experiment.':'Open the imported XML in ScenarioForge to resolve and save its Flow chain, then reload it here.');
  lastUploadedScenarioFile=scenarioFileIdentity(file);
  $('scenario-file').value='';
 }catch(error){$('scenario-upload-info').textContent=error.message;}
 finally{operation=null;syncScenarioUploadButton();syncBusy();schedulePoll();}
}
$('scenario-file').addEventListener('change',syncScenarioUploadButton);
$('upload-scenario').addEventListener('click',()=>sendScenario($('scenario-file').files[0]));

$('experiment-sample').addEventListener('change',()=>{describeSample();taskScenarioChanged();});
$('cancel-experiment').addEventListener('click',()=>{if(!experimentCreating&&!operation)leaveExperimentWizard();});
$('experiment-form').addEventListener('submit',async event=>{
 event.preventDefault();if(isBusy()||experimentCreating)return;
 if(wizardSteps[wizardStep].id!=='review'){showWizardStep('review');return;}
 const missing=experimentMissingFields();if(missing){syncCreateExperiment();return;}
 const scenario=$('experiment-sample').value==='scenarioforge-xml';
 if(!validateExperimentFields($('experiment-form')))return;
 if(scenario&&(!scenarioChoices.some(item=>item.id===$('scenario-selection').value)||rolesDirty)){selectExperimentTab('scenario');$('experiment-error').textContent=rolesDirty?'Save VM roles before creating an experiment.':'Load and choose a saved scenario first.';return;}
 $('experiment-error').textContent='';
 startCreationProgress(false);
 experimentCreating=true;operation='Creating experiment…';syncBusy();for(const input of $('experiment-form').querySelectorAll('button,select'))input.disabled=true;
 try{creationProgress(2,'running','Waiting for earlier dashboard reads to finish…');operation='Creating experiment · Waiting for earlier dashboard reads to finish…';syncBusy();await finishDashboardRead();const response=await experimentRequestWithProgress({request_id:crypto.randomUUID().replaceAll('-',''),judge:judgeSettings(),controls:experimentControls(),...($('provide-progressive-hints').checked?{provide_progressive_hints:true}:{}),...(scenario?{selection_id:$('scenario-selection').value,allowed_targets:$('scenario-allowed').value,disallowed_targets:$('scenario-disallowed').value}:{sample_id:$('experiment-sample').value}),evaluation:Object.fromEntries(evaluationKeys.map(key=>[key,Number($('eval-'+key).value)]))});const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Unable to create experiment');creationProgress(2,'complete','Experiment saved · complete');$('judge-api-key').value='';wizardDraft=false;leaveExperimentWizard();$('sample-message').textContent='Experiment created. Press its Run icon when ready.';await refresh();}
 catch(error){const current=creationSteps.findIndex(step=>step.state==='running');if(current>=0)creationProgress(current,'failed',creationSteps[current].message+' · failed: '+error.message);$('experiment-error').textContent=error.message;}
 finally{experimentCreating=false;operation=null;for(const input of $('experiment-form').querySelectorAll('button,select'))input.disabled=false;syncBusy();schedulePoll();}
});
async function experimentRequestWithProgress(payload,action="create"){
 let finished=false,timer=null,lastStatus='Submitting request and checking access…';
 const started=Date.now(),title=action==='run'?'Starting experiment':'Creating experiment';
 const render=()=>{if(finished)return;const label=`${title} · ${lastStatus} · ${Math.floor((Date.now()-started)/1000)}s elapsed`;operation=label;if(action==='create')creationProgress(2,'running',label);syncBusy();};
 render();
 const heartbeat=setInterval(render,1000);
 const poll=async()=>{
  const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),10000);
  try{
   const response=await apiFetch('/api/experiments/'+(action==='run'?'start-status':'creation-status')+'?request_id='+encodeURIComponent(payload.request_id),{cache:'no-store',signal:controller.signal});
   if(!response.ok)throw Error(`Progress status HTTP ${response.status}`);
   const status=await dashboardJSON(response);
   if(!finished){lastStatus=(status.total?`Step ${status.step}/${status.total}`:'Awaiting server checkpoint')+' · '+status.message;render();}
  }catch(error){if(!finished){lastStatus=`Progress update unavailable (${error.name==='AbortError'?'request timed out':error.message}); retrying. Creation/launch request is still pending`;render();}}
  finally{clearTimeout(deadline);}
  if(!finished)timer=setTimeout(poll,2000);
 };
 timer=setTimeout(poll,250);
 try{return await apiFetch('/api/experiments/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(payload)});}
 finally{finished=true;clearTimeout(timer);clearInterval(heartbeat);}
}

async function experimentAction(action,id){
 if(isBusy()||sampleStarting)return;
 if(action==='run'&&rolesDirty){$('sample-message').textContent='Save VM role changes on Lab setup before running.';return;}
 sampleStarting=true;operation=action==='stop'?'Requesting experiment stop…':'Starting experiment…';syncBusy();
 try{await finishDashboardRead();const payload={run_id:id,...(action==='run'?{request_id:crypto.randomUUID().replaceAll('-','')}:{})};const response=action==='run'?await experimentRequestWithProgress(payload,'run'):await apiFetch('/api/experiments/stop',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(payload)});const result=await dashboardJSON(response);if(!response.ok)throw Error(result.error||'Experiment action failed');$('sample-message').textContent=action==='stop'?'Stop requested. The current stage or trial will finish and results will be collected.':'Experiment started.';const link=el('a',' Open progress ↗');link.href=runWindowURL('progress',result.run_id);link.addEventListener('click',event=>{event.preventDefault();openProgress(result.run_id);});$('sample-message').append(link);await refresh();}
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
 const updates=data.updates;
 $('updates-panel').hidden=false;$('applications-unavailable').hidden=true;
 if(!updates){
  $('updates-title').textContent='Local applications';$('updates-mode').textContent='Fusion inventory · guest-agent status';
  $('updates-context').textContent='Applications selected from the local Fusion inventory. Source update and rollback are unavailable in desktop mode; status comes from the latest guest-agent scan.';
  $('maintenance-history').hidden=true;$('update-message').textContent='';
  const cards=$('update-cards');cards.replaceChildren();
  const applications=['scenarioforge','participant'].map(role=>{
   const observed=(data.vms||[]).find(vm=>vm.role===role);if(observed)return observed;
   const vmid=data.roles?.[role],available=(data.available_vms||[]).find(vm=>vm.vmid===vmid);
   return {role,vmid,name:available?.name,power:vmid?'checking':'not selected',guest_access:vmid?'Checking':'Not checked',application_present:null,processes:[],services:[]};
  });
  for(const vm of applications){
   const card=el('article',null,'card update-card'),title=vm.role==='participant'?'Cyber-agent-flow':'ScenarioForge';
   card.append(el('h3',title),el('p',vm.vmid?`VM ${vm.vmid}${vm.name?' · '+vm.name:''}`:'No VM selected','small'));
   const power=vm.qmp_status==='paused'?'paused':vm.power;
   card.append(badge(power,power==='running'?'good':power==='unknown'?'warn':''));
   const details=el('dl');
   detail(details,'Guest agent',vm.guest_access||'Not checked');
   detail(details,'Application',vm.application_present==null?'Not checked':vm.application_present?'Present':'Not found');
   detail(details,'Processes observed',vm.guest_access==='reachable'?String((vm.processes||[]).length):'Unknown');
   card.append(details);
   if(vm.root)card.append(el('p',`Configured path: ${vm.root}`,'small'));
   for(const service of vm.services||[]){if(service.unit===vm.unit)card.append(el('p',`${service.unit} · ${service.error?'Check unavailable':service.LoadState==='not-found'?'not found':service.ActiveState+' / '+service.SubState}`,'small'));}
   if(vm.error||vm.guest_error)card.append(el('div',vm.error||vm.guest_error,'error'));
   cards.append(card);
  }
  return;
 }
 $('updates-title').textContent='Application versions';$('updates-mode').textContent=updates.desktop?'Desktop downloads · Fusion guest-agent transfer':'Host downloads · guest-agent transfer';$('maintenance-history').hidden=false;
 $('maintenance-count').textContent=`${updates.jobs?.length||0} recent jobs`;
 $('updates-context').textContent=updates.can_update?(updates.desktop?'Check or update the applications installed in the selected local VMs. Updates preserve local data and reuse installed dependencies; managed services restart when necessary.':'Updates preserve local data and reuse installed dependencies. The application must be idle; its configured service is restarted when necessary.'):`Check installed versions here. Updates and rollback require membership in the ${updates.group} PVE group.`;
 const cards=$('update-cards');
 // Preserve input focus while the dashboard polls.
 if(!document.activeElement?.matches('#update-cards input[data-update-ref]')){
  cards.replaceChildren();
  for(const app of updates.applications){
   const card=el('article',null,'card update-card');card.append(el('h3',app.role==='participant'?'Cyber-agent-flow':'ScenarioForge'),el('p',app.vmid?`VM ${app.vmid}`:'Choose and save a VM on Lab setup','small'));
   const known=(updates.jobs||[]).find(job=>job.role===app.role&&job.vmid===app.vmid&&job.installed);
   if(known){const info=known.installed;card.append(el('p',`Installed revision (last checked): ${info.revision.slice(0,12)}${info.modified?' · local edits':''}`,'small'));if(info.missing_controls?.length)card.append(el('p',`Missing evaluator controls: ${info.missing_controls.join(', ')}`,'error'));
    if(info.processes?.length){card.append(el('p','Processes referencing this checkout (up to 12):','small'));for(const process of info.processes)card.append(el('p',`PID ${process.pid} · ${process.name} · ${process.reason}`,'small'));card.append(el('p','Update asks for confirmation before stopping these processes. A configured service is restarted after activation.','small'));}
    if(info.modified){card.append(el('p',info.tools_config_replaceable?'Legacy kali_tools.json change detected. Update will back up and replace this runtime catalog with shipped defaults.':'Tracked local edits require a Force update. Confirming it permanently discards these tracked VM changes; they are not recoverable through rollback.','error'));if(info.modified_files?.length){card.append(el('pre',info.modified_files.join('\n'),'small'));card.append(el('p',`Showing ${info.modified_files.length} of ${info.modified_file_count??info.modified_files.length} changed files (paths limited to 300 characters).`,'small'));}}
   }
   const label=el('label','Branch, tag or commit','small'),input=el('input');input.type='text';input.value=maintenanceRefs[app.role]||app.ref;input.dataset.updateRef=app.role;input.addEventListener('input',()=>{maintenanceRefs[app.role]=input.value;});label.append(input);card.append(label);
   const status=el('p',null,'small');status.id=`update-status-${app.role}`;card.append(status);input.setAttribute('aria-describedby',status.id);
   const actions=el('div',null,'update-actions');
   for(const [action,defaultTitle] of [['inspect','Check version'],['update','Update'],['rollback','Roll back']]){const force=action==='update'&&known?.installed.modified&&!known.installed.tools_config_replaceable,title=force?'Force update':defaultTitle;const button=el('button',title);button.type='button';button.dataset.updateRole=app.role;button.dataset.updateAction=action;button.setAttribute('aria-describedby',status.id);button.addEventListener('click',()=>{const inspection=force?(updates.jobs||[]).find(job=>job.role===app.role&&job.vmid===app.vmid&&job.action==='inspect'&&job.status==='completed'&&job.installed?.modified):null;maintain(app.role,action,input.value,action==='update'?known:null,inspection);});actions.append(button);}
   card.append(actions);cards.append(card);
  }
 }
 const jobs=$('update-jobs');jobs.replaceChildren();
 const latest=updates.jobs?.[0];
 if(latest){$('update-message').textContent=`Latest maintenance · ${latest.role} · VM ${latest.vmid} · ${latest.action}: ${latest.status}. ${latest.error||latest.message}`;$('update-message').classList.toggle('error',['failed','interrupted'].includes(latest.status));}
 for(const job of updates.jobs||[]){const row=el('article',null,'command');row.append(badge(job.status,job.status==='completed'?'good':job.status==='failed'?'warn':''),el('span',` ${job.role} · VM ${job.vmid} · ${job.action}`,'command-title'),el('p',job.message,'small'));if(job.console?.transfer){const transfer=job.console.transfer,progress=el('progress');progress.max=100;progress.value=transfer.percent;progress.setAttribute('aria-label',`${job.role} source bundle upload`);row.append(el('p',`${transfer.sent_bytes.toLocaleString()} / ${transfer.total_bytes.toLocaleString()} bytes acknowledged · ${transfer.percent}% · ${(transfer.bytes_per_second/1024).toFixed(1)} KiB/s · ${transfer.verified?'Upload checksum verified; see activation outcome below':['queued','running'].includes(job.status)?'Awaiting verified completion':'Transfer stopped; file not verified'}`,'small'),progress);if(!transfer.verified)row.append(el('p',`${transfer.sent_bytes?'Last acknowledgement':'Transfer started'}: ${transfer.updated_at}. Open the troubleshooting console below for command history.`,'small'));}if(job.revision)row.append(el('p',`Target revision: ${job.revision}`,'small'));if(job.action!=='inspect')row.append(el('p',job.status==='completed'?(job.unchanged?'Already up to date; no activation needed':'Activation completed'):['failed','interrupted'].includes(job.status)?'Activation not confirmed. Check version for the current installed revision.':'Activation pending','small'));if(job.error)row.append(el('p',job.error,'error'));const details=el('details'),summary=el('summary','Details');details.append(summary,el('pre',JSON.stringify(job,null,2)));row.append(details);jobs.append(row);}
 if(!(updates.jobs||[]).length)jobs.append(el('p','No application maintenance recorded for your account.','empty'));
}
async function maintain(role,action,ref,known=null,forceConfirmation=null){
 if(isBusy()||maintenanceStarting)return;
 if(rolesDirty){$('update-message').textContent='Save VM role changes before application maintenance.';return;}
 const info=known?.installed||{},processes=action==='update'?(info.processes||[]):[],force=Boolean(action==='update'&&info.modified&&!info.tools_config_replaceable);
 if(processes.length&&!processes.every(p=>/^[0-9a-f]{64}$/.test(p.identity||''))){$('update-message').textContent='Click Check version to refresh the process list, then click Update to confirm stopping it.';return;}
 if(force&&!forceConfirmation){$('update-message').textContent='Click Check version to refresh the changed-file list before forcing the update.';return;}
 if(force||processes.length){const sections=[`${force?'Force update':'Update'} ${role} on VM ${known.vmid} to ${ref}?`];if(force){sections.push(`WARNING: This permanently discards all ${info.modified_file_count??info.modified_files?.length??'listed'} tracked local VM change(s). They are not saved in rollback and cannot be recovered by the orchestrator.\n\n${(info.modified_files||[]).join('\n')}`);}if(processes.length){sections.push(`The following processes will be sent SIGTERM:\n\n${processes.map(p=>`PID ${p.pid} · ${p.name} · ${p.reason}`).join('\n')}\n\nListed terminal windows/sessions may close and unsaved work may be lost. Unmanaged applications will not restart automatically.`);}sections.push('Continue with this update?');if(!window.confirm(sections.join('\n\n')))return;}
 maintenanceStarting=true;operation='Submitting application maintenance…';syncBusy();if(snapshot)renderUpdates(snapshot);
 try{
  await finishDashboardRead();
  if(!csrfToken)await loadSession();
  const response=await apiFetch('/api/applications',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role,action,ref,request_id:crypto.randomUUID().replaceAll('-',''),...(processes.length?{process_confirmation:known.id}:{}),...(force?{force_confirmation:forceConfirmation.id}:{})})});
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
initTaskEditor();syncBusy();refresh(true);

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

let customConditions=[];
let activeCondition=null;
const conditionViews=new WeakMap();
let selectedToolHelpers=new Set(),selectedGeneratedHelpers=new Set();
let participantToolsPromise=null;
const generatedHelperContent=new Map();
function helperCatalogEntries(){return snapshot?.helper_catalog||[];}
let resourceDefaultsLoaded=false,participantCatalogReady=false;
function resourceDefaultsKey(){return sessionAccount&&snapshot?.workflow_id?'caf-resource-defaults:v1:'+JSON.stringify([sessionAccount,snapshot.workflow_id]):null;}
function rememberResourceDefaults(){
 const key=resourceDefaultsKey();if(!key||customConditions.some(row=>configurationNameError(row)))return;
 const rows=customConditions.map(row=>({name:configurationName(row),tools:[...row.tools],guidance_assets:[...(row.guidance_assets||[])]}));
 try{localStorage.setItem(key,JSON.stringify(rows));}catch{}
}
function restoreResourceDefaults(choice){
 const key=resourceDefaultsKey();if(!key)return;
 try{
  const rows=JSON.parse(localStorage.getItem(key));
  if(!Array.isArray(rows)||!rows.length||rows.some(row=>!row||typeof row.name!=='string'||!row.name.trim()||row.name.length>80||/[\x00-\x1f\x7f]/.test(row.name)||!Array.isArray(row.tools)||!Array.isArray(row.guidance_assets)||[...row.tools,...row.guidance_assets].some(value=>typeof value!=='string'))||new Set(rows.map(row=>row.name.trim().toLowerCase())).size!==rows.length)return;
  const assets=snapshot?.participant_guidance_assets||[];
  customConditions=rows.map((row,index)=>({id:'configuration-'+(index+1),name:row.name,source:'participant-vm',tools:choice.tools.filter(name=>row.tools.includes(name)),guidance:'',guidance_assets:assets.map(item=>item.id).filter(id=>row.guidance_assets.includes(id))}));
 }catch{}
}
function ensureParticipantCondition(){
 if(customConditions.length||!participantCatalogReady)return;
 const choice=snapshot?.condition_catalogs?.['participant-vm'];if(!choice)return;
 if(!resourceDefaultsLoaded){resourceDefaultsLoaded=true;restoreResourceDefaults(choice);}
 if(customConditions.length||$('experiment-sample').value!=='scenarioforge-xml')return;
 customConditions.push({id:'configuration-1',name:'Configuration 1',source:'participant-vm',tools:[...choice.tools],guidance:'',guidance_assets:[]});
}
function validateRunConfigurations(){
 const ids=new Set();for(const row of customConditions){const error=configurationNameError(row);if(error)throw Error(error);if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(row.id)||ids.has(row.id))throw Error('Run configuration IDs must be valid and unique.');ids.add(row.id);if(row.source!=='participant-vm'||!snapshot?.condition_catalogs?.['participant-vm'])throw Error('Load tools and guidance from the participant VM first.');if(new Set(row.tools).size!==row.tools.length)throw Error('Run configuration tools must be distinct.');}
}
function experimentPricing(){
 const pricing={};for(const key of ['participant','judge']){const a=$('price-'+key+'-input').value,b=$('price-'+key+'-output').value;if(Boolean(a)!==Boolean(b))throw Error('Enter both input and output prices, or leave both empty.');if(a){pricing[key]={input_per_million:Number(a),output_per_million:Number(b)};}}if($('price-generation').value)pricing.generation_cost_usd=Number($('price-generation').value);
 return pricing;
}
function experimentControls(){
 validateRunConfigurations();const pricing=experimentPricing();
 const conditions=customConditions.map(row=>({...row,name:configurationName(row).trim(),guidance_assets:[...(row.guidance_assets||[])]}));
 return {order_seed:Number($('condition-order-seed').value),reset_each_trial:$('reset-each-trial').checked,use_configured_reset_hooks:$('extra-reset-hooks').checked,auto_approve_dangerous:$('auto-approve-dangerous').checked,conditions,helpers:[...selectedToolHelpers],generated_helpers:[...selectedGeneratedHelpers],...(Object.keys(pricing).length?{pricing}:{})};
}
let resourceInfoVersion=0,resourceInfoTrigger=null;
function shortResourceDescription(value,limit=360){const text=String(value||'').replace(/\s+/g,' ').trim();return text.length>limit?text.slice(0,limit-1).trimEnd()+'…':text;}
function resourceInfoButton(name,description,facts=[],loadDescription=null){
 const button=el('button','?','resource-info-button');button.type='button';button.title='About '+name;button.setAttribute('aria-label','About '+name);button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-controls','resource-info-popover');button.setAttribute('aria-expanded','false');
 button.addEventListener('click',async()=>{
  let popup=$('resource-info-popover');
  if(!popup){
   popup=el('div',null,'resource-info-popover');popup.id='resource-info-popover';popup.setAttribute('popover','auto');popup.setAttribute('role','dialog');popup.setAttribute('aria-labelledby','resource-info-title');document.body.append(popup);
   popup.addEventListener('toggle',()=>{if(!popup.matches(':popover-open'))resourceInfoTrigger?.setAttribute('aria-expanded','false');});
  }
  if(resourceInfoTrigger===button&&popup.matches(':popover-open')){popup.hidePopover();return;}
  resourceInfoTrigger?.setAttribute('aria-expanded','false');resourceInfoTrigger=button;
  const version=++resourceInfoVersion,heading=el('div',null,'resource-info-heading'),title=el('h4',name),close=el('button','×'),body=el('p',shortResourceDescription(description)||'No description supplied.');title.id='resource-info-title';close.type='button';close.setAttribute('aria-label','Close description');close.addEventListener('click',()=>{popup.hidePopover();button.focus();});heading.append(title,close);popup.replaceChildren(heading,body);
  for(const fact of (typeof facts==='function'?facts():facts).filter(Boolean))popup.append(el('p',shortResourceDescription(fact,260),'small'));
  const position=()=>{const anchor=button.getBoundingClientRect(),bounds=popup.getBoundingClientRect(),consoleTop=$('debug-console')?.getBoundingClientRect().top??innerHeight,bottom=Math.min(innerHeight,consoleTop);popup.style.left=Math.max(12,Math.min(anchor.left,innerWidth-bounds.width-12))+'px';popup.style.top=Math.max(12,Math.min(anchor.bottom+8+bounds.height>bottom?anchor.top-bounds.height-8:anchor.bottom+8,bottom-bounds.height-8))+'px';};
  if(!popup.matches(':popover-open'))popup.showPopover();button.setAttribute('aria-expanded','true');position();close.focus({preventScroll:true});
  if(loadDescription){body.textContent='Loading description…';try{const text=await loadDescription();if(version===resourceInfoVersion&&popup.matches(':popover-open')){body.textContent=shortResourceDescription(text)||'No description supplied.';position();}}catch(error){if(version===resourceInfoVersion&&popup.matches(':popover-open')){body.textContent='Description could not be loaded: '+error.message;position();}}}
 });return button;
}
function compactResource(name,checked,onChange,info,markers=[]){
 const row=el('div',null,'resource-item'),label=el('label',null,'condition-tool-option'),input=el('input'),content=el('span',null,'resource-item-content'),heading=el('span',null,'tool-option-heading');input.type='checkbox';input.checked=checked;input.addEventListener('change',()=>onChange(input.checked));heading.append(el('strong',name));for(const marker of markers)heading.append(marker);content.append(heading);label.append(input,content);row.append(label,info);return row;
}
function initTrialLimitHelp(){
 const descriptions={
  repetitions:'Repeat every task once per run configuration for each repetition. Total trials = tasks × run configurations × repetitions.',
  max_turns:'Maximum agent conversation turns per trial. The trial ends when the agent finishes or reaches a turn or time limit. With progressive hints enabled, this must exceed the hint interval.',
  wall_seconds:'Maximum participant active time per trial, in seconds. This is the limit behind “Guest wall-clock budget exceeded” and “Participant active-time budget exceeded”. Select No timeout to disable it (saved as wall_seconds: 0). Intermediate evidence-transfer and progress-judge waiting time are excluded and recorded separately. Judge reviews have separate time limits; maximum agent turns still applies.',
  tool_timeout:'Time in seconds before a running tool reaches its timeout checkpoint. CAF handles waiting, backgrounding or stopping according to the trial policy. If the participant active time limit is enabled, it can still stop an active tool. No timeout disables elapsed and idle tool checkpoints; a tool can wait indefinitely.',
  context_window:'Token budget used by CAF to manage conversation history and summarize older context. Choose a budget supported by the participant model; this setting does not increase the model’s own context capacity.'
 };
 for(const [key,description] of Object.entries(descriptions)){
  const input=$('eval-'+key),label=input.closest('label'),name=label.firstChild.textContent.trim(),field=el('div',null,'trial-limit-field'),heading=el('div',null,'trial-limit-heading');
  label.replaceWith(field);label.textContent=name;label.htmlFor=input.id;
  heading.append(label,resourceInfoButton(name,description,()=>['Minimum: '+Number(input.min).toLocaleString(),'Maximum: '+Number(input.max).toLocaleString()]));field.append(heading,input);
 }
}
initTrialLimitHelp();

function experimentTimeoutFields(){return ['eval-wall_seconds','eval-tool_timeout','judge-timeout_seconds','judge-progress_timeout_seconds'];}
function timeoutLabel(value){return Number(value)===0?'No timeout (may wait indefinitely)':value+' seconds';}
function syncTimeoutControls(fromValues=false){
 for(const id of experimentTimeoutFields()){const input=$(id),toggle=$(id+'-no-timeout');if(!toggle)continue;
  if(fromValues){toggle.checked=input.value==='0';if(Number(input.value)>0)input.dataset.finiteTimeout=input.value;}
  const unavailable=id.startsWith('judge-')? !$('judge-enabled').checked || id==='judge-progress_timeout_seconds'&&!$('judge-monitor_progress').checked:$('evaluation-settings').disabled;
  toggle.disabled=unavailable;input.disabled=unavailable||toggle.checked;
  $(id+'-timeout-warning').hidden=!toggle.checked||unavailable;
 }
}
function initTimeoutControls(){
 for(const id of experimentTimeoutFields()){const input=$(id);input.dataset.finiteTimeout=input.value||String(id==='eval-tool_timeout'?60:id==='eval-wall_seconds'?300:600);
  let field=input.parentElement;
  if(field.tagName==='LABEL'){const label=field;field=el('div',null,'timeout-field');label.replaceWith(field);label.htmlFor=id;field.append(label,input);}
  const row=el('div',null,'timeout-choice'),label=el('label',null,'timeout-toggle'),toggle=el('input'),warning=el('span',null,'timeout-warning'),icon=el('button','!','timeout-warning-icon'),message=el('span','No timeout can lead to an infinite wait if the operation hangs. Other enabled limits still apply.','timeout-warning-message');
  toggle.type='checkbox';toggle.id=id+'-no-timeout';warning.id=id+'-timeout-warning';warning.hidden=true;message.id=warning.id+'-message';message.setAttribute('role','tooltip');icon.type='button';icon.id=warning.id+'-icon';icon.setAttribute('aria-label','No timeout warning');icon.setAttribute('aria-describedby',message.id);toggle.setAttribute('aria-describedby',message.id);icon.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();icon.blur();}});warning.append(icon,message);label.append(toggle,document.createTextNode(' No timeout'));row.append(label,warning);field.append(row);
  toggle.addEventListener('change',()=>{if(toggle.checked){if(Number(input.value)>0)input.dataset.finiteTimeout=input.value;input.value='0';}else input.value=input.dataset.finiteTimeout||'600';syncTimeoutControls();rememberEvaluationSettings();syncCreateExperiment();renderWizardReview();});
 }
}
initTimeoutControls();


function conditionToolPicker(row,choice){
 const holder=el('fieldset',null,'condition-tools'),legend=el('legend','Allowed tools'),definitions=new Map((choice.definitions||[]).map(item=>[item.name,item]));holder.append(legend);
 const tools=(choice.tools||[]).map(name=>({name,definition:definitions.get(name)||{}}));
 const toolbar=el('div',null,'condition-tool-toolbar'),search=el('input'),filter=el('select'),count=el('span',null,'tool-selection-count');search.type='search';search.placeholder='Search tools';search.setAttribute('aria-label','Search available tools');filter.setAttribute('aria-label','Filter tools');for(const [value,label] of [['all','All tools'],['selected','Selected'],['generated','Generated'],['guided','Has guide'],['dangerous','Dangerous'],['interactive','Interactive']])filter.add(new Option(label,value));
 const actions=el('div',null,'condition-tool-actions');for(const [label,choose] of [['Select visible',true],['Clear visible',false]]){const button=el('button',label);button.type='button';button.addEventListener('click',()=>{const values=selected();for(const item of visible)choose?values.add(item.name):values.delete(item.name);row.tools=tools.map(item=>item.name).filter(name=>values.has(name));rememberResourceDefaults();render();});actions.append(button);}toolbar.append(search,filter,count,actions);holder.append(toolbar);
 const groups=el('div',null,'condition-tool-groups');holder.append(groups);let visible=[];
 const selected=()=>new Set(row.tools||[]),sync=()=>{count.textContent=`${selected().size} of ${tools.length} selected`;for(const group of groups.querySelectorAll('.tool-group')){const items=visible.filter(item=>(item.definition.category||'Other tools')===group.dataset.category);group.querySelector('summary').textContent=`${group.dataset.category} · ${items.filter(item=>selected().has(item.name)).length}/${items.length} selected`;}syncConditionOverview();syncCreateExperiment();};
 const matches=item=>{const query=search.value.trim().toLowerCase(),definition=item.definition,text=[item.name,definition.description,definition.category,definition.origin].join(' ').toLowerCase();if(query&&!text.includes(query))return false;const mode=filter.value;if(mode==='selected'&&!selected().has(item.name))return false;if(mode==='generated'&&definition.origin!=='generated')return false;if(mode==='guided'&&!definition.has_guide)return false;if(mode==='dangerous'&&definition.risk!=='dangerous')return false;if(mode==='interactive'&&definition.risk!=='interactive')return false;return true;};
 const render=()=>{
  groups.replaceChildren();visible=tools.filter(matches);const grouped=new Map();for(const item of visible){const key=item.definition.category||'Other tools';if(!grouped.has(key))grouped.set(key,[]);grouped.get(key).push(item);}if(!visible.length){groups.append(el('p','No tools match this search and filter.','empty'));sync();return;}
  for(const [category,items] of grouped){const details=el('details',null,'tool-group');details.dataset.category=category;details.open=true;details.append(el('summary',category));const list=el('div',null,'tool-group-list');
   for(const {name,definition} of items){const markers=[];if(definition.risk&&definition.risk!=='standard')markers.push(el('span',definition.risk,'tool-risk '+definition.risk));if(definition.origin==='generated')markers.push(el('span','generated','tool-origin'));if(definition.has_guide)markers.push(el('span','guide','tool-origin guided'));
    list.append(compactResource(name,selected().has(name),enabled=>{const values=selected();enabled?values.add(name):values.delete(name);row.tools=tools.map(item=>item.name).filter(tool=>values.has(tool));rememberResourceDefaults();if(filter.value==='selected')render();else sync();},resourceInfoButton(name,definition.description||'No description supplied by CAF.',[category,definition.risk&&definition.risk!=='standard'?'Action level: '+definition.risk:null,definition.has_guide?'A CAF tool guide is available.':null]),markers));
   }details.append(list);groups.append(details);
  }sync();
 };
 search.addEventListener('input',render);filter.addEventListener('change',render);render();return holder;
}
function conditionGuidancePicker(row,assets){
 if(!assets?.length)return null;row.guidance_assets=row.guidance_assets||[];
 const holder=el('section',null,'condition-guidance-assets'),summary=el('h5'),body=el('div',null,'guidance-assets-body'),toolbar=el('div',null,'guidance-assets-toolbar'),search=el('input'),filter=el('select'),count=el('span',null,'tool-selection-count'),groups=el('div',null,'guidance-asset-groups');search.type='search';search.placeholder='Search guidance and documents';search.setAttribute('aria-label','Search CAF guidance');const kinds=[...new Map(assets.map(item=>[item.kind,item.label||item.kind])).entries()];filter.add(new Option('All types',''));for(const [kind,label] of kinds)filter.add(new Option(label,kind));filter.setAttribute('aria-label','Filter guidance types');
 let visible=[];const actions=el('div',null,'condition-tool-actions');for(const [label,choose] of [['Select visible',true],['Clear visible',false]]){const button=el('button',label);button.type='button';button.addEventListener('click',()=>{const values=new Set(row.guidance_assets);for(const item of visible)choose?values.add(item.id):values.delete(item.id);row.guidance_assets=assets.map(asset=>asset.id).filter(id=>values.has(id));rememberResourceDefaults();render();syncConditionOverview();syncCreateExperiment();});actions.append(button);}toolbar.append(search,filter,count,actions);body.append(toolbar,groups);holder.append(summary,body);
 const render=()=>{groups.replaceChildren();const query=search.value.trim().toLowerCase(),selected=new Set(row.guidance_assets);visible=assets.filter(item=>(!filter.value||item.kind===filter.value)&&(!query||[item.name,item.label,item.description,...(item.files||[])].join(' ').toLowerCase().includes(query)));summary.textContent=`Guidance and documents · ${selected.size} selected · ${assets.length} available`;count.textContent=`${selected.size} selected`;if(!visible.length){groups.append(el('p','No guidance matches this search and filter.','empty'));return;}
  const grouped=new Map();for(const item of visible){const key=item.label||item.kind;if(!grouped.has(key))grouped.set(key,[]);grouped.get(key).push(item);}for(const [label,items] of grouped){const section=el('section',null,'guidance-asset-group'),list=el('div',null,'guidance-compact-list');section.append(el('h5',label));for(const item of items)list.append(compactResource(item.name,selected.has(item.id),enabled=>{const values=new Set(row.guidance_assets);enabled?values.add(item.id):values.delete(item.id);row.guidance_assets=assets.map(asset=>asset.id).filter(id=>values.has(id));rememberResourceDefaults();render();syncConditionOverview();syncCreateExperiment();},resourceInfoButton(item.name,item.description||`${label} supplied to the agent when selected.`,[(item.files||[]).join(', '),`${Math.max(1,Math.ceil((item.bytes||0)/1024))} KiB`])));section.append(list);groups.append(section);}
 };
 search.addEventListener('input',render);filter.addEventListener('change',render);render();return holder;
}

function helperSourceLabel(helper){const source=helper.source_scenario||{};return source.scenario||helper.source_run_id||'Unknown scenario';}
async function loadGeneratedHelper(helperId){
 if(generatedHelperContent.has(helperId))return generatedHelperContent.get(helperId);
 const response=await apiFetch('/api/generated-helpers/'+encodeURIComponent(helperId),{cache:'no-store'}),data=await dashboardJSON(response);
 if(!response.ok)throw Error(data.error||'Unable to load generated helper');generatedHelperContent.set(helperId,data.content);return data.content;
}
async function setGeneratedHelperReviewed(helper,reviewed){
 const status=$('generated-helper-status');status.textContent=reviewed?'Marking reviewed…':'Returning helper to unreviewed…';
 try{const response=await apiFetch('/api/generated-helpers/review',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({helper_id:helper.id,reviewed})}),data=await dashboardJSON(response);if(!response.ok)throw Error(data.error||'Unable to update helper review state');const index=(snapshot.generated_helpers||[]).findIndex(item=>item.id===helper.id);if(index>=0)snapshot.generated_helpers[index]=data;renderGeneratedHelpers();status.textContent=reviewed?'Helper marked reviewed.':'Helper marked unreviewed.';}
 catch(error){status.textContent=error.message;}
}
function renderHelperSelection(){
 const enabled=[...helperCatalogEntries().filter(item=>selectedToolHelpers.has(item.id)),...(snapshot?.generated_helpers||[]).filter(item=>selectedGeneratedHelpers.has(item.id))];
 const target=$('helper-selection-summary');target.replaceChildren();
 if(!enabled.length){target.append(el('p','No optional helpers enabled.','small'));syncConditionOverview();return;}
 const list=el('ul');for(const item of enabled)list.append(el('li',item.name));target.append(list);syncConditionOverview();
}
function helperMatches(helper){const query=$('helper-search').value.trim().toLowerCase();return !query||[helper.name,helper.description,helper.kind,helper.origin,helperSourceLabel(helper),...(helper.tags||[]),...(helper.tools||[])].join(' ').toLowerCase().includes(query);}
function compactHelperHeading(helper,selected,onChange,info){
 const item=compactResource(helper.name,selected,onChange,info);item.querySelector('input').setAttribute('aria-label','Enable '+helper.name);return item;
}
function syncHelperBulkControls(kind,all,selected){
 const visible=all.filter(helperMatches),prefix=kind==='tools'?'tool-helper':'generated-helper';
 $(prefix+'-selection-count').textContent=selected.size+' selected';
 for(const action of ['select','clear'])$(prefix+'-'+action).disabled=!visible.length;
}
function selectHelperView(view,focus=false){
 const buttons=[...document.querySelectorAll('#helper-resource-tabs [role=tab]')];
 for(const button of buttons){const selected=button.dataset.helperView===view;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;$(button.getAttribute('aria-controls')).hidden=!selected;if(selected&&focus)button.focus();}
}
for(const button of document.querySelectorAll('#helper-resource-tabs [role=tab]')){
 button.addEventListener('click',()=>selectHelperView(button.dataset.helperView));
 button.addEventListener('keydown',event=>{const buttons=[...document.querySelectorAll('#helper-resource-tabs [role=tab]')],index=buttons.indexOf(button),next=event.key==='ArrowRight'?(index+1)%buttons.length:event.key==='ArrowLeft'?(index+buttons.length-1)%buttons.length:event.key==='Home'?0:event.key==='End'?buttons.length-1:null;if(next!==null){event.preventDefault();selectHelperView(buttons[next].dataset.helperView,true);}});
}
for(const [prefix,entries,selection,render] of [['tool-helper',helperCatalogEntries,()=>selectedToolHelpers,renderToolHelpers],['generated-helper',()=>snapshot?.generated_helpers||[],()=>selectedGeneratedHelpers,renderGeneratedHelpers]]){
 for(const action of ['select','clear'])$(prefix+'-'+action).addEventListener('click',()=>{const selected=selection();for(const helper of entries().filter(helperMatches))action==='select'?selected.add(helper.id):selected.delete(helper.id);render();renderHelperSelection();syncCreateExperiment();});
}
function renderToolHelpers(){
 const target=$('tool-helper-library'),all=helperCatalogEntries(),helpers=all.filter(helperMatches);target.replaceChildren();$('tool-helper-count').textContent=all.length+' available';syncHelperBulkControls('tools',all,selectedToolHelpers);
 if(!helpers.length){target.append(el('p',all.length?'No tool helpers match this search.':'No reusable tool helpers are configured.','empty'));return;}
 for(const helper of helpers){
  const card=el('article',null,'helper-catalog-card resource-helper-card'),tools=helper.added_tools?.length?helper.added_tools:helper.tools||[];
  card.append(compactHelperHeading(helper,selectedToolHelpers.has(helper.id),enabled=>{enabled?selectedToolHelpers.add(helper.id):selectedToolHelpers.delete(helper.id);renderHelperSelection();syncHelperBulkControls('tools',all,selectedToolHelpers);syncCreateExperiment();},resourceInfoButton(helper.name,helper.description||'Reusable tool helper.',[helper.kind,helper.origin,'Adds: '+(tools.join(', ')||'guidance only'),helper.tags?.join(' · ')])));
  target.append(card);
 }
}
function renderGeneratedHelpers(){
 const target=$('generated-helper-library'),all=snapshot?.generated_helpers||[],helpers=all.filter(helperMatches);target.replaceChildren();$('generated-helper-count').textContent=all.length+' saved';syncHelperBulkControls('guidance',all,selectedGeneratedHelpers);
 if(!helpers.length){target.append(el('p',all.length?'No generated guidance matches this search.':'No generated helpers yet. Finish a run and choose Analyze, generate helper & rerun from Results.','empty'));return;}
 for(const helper of helpers){
  const card=el('article',null,'generated-helper-card resource-helper-card');card.append(compactHelperHeading(helper,selectedGeneratedHelpers.has(helper.id),enabled=>{enabled?selectedGeneratedHelpers.add(helper.id):selectedGeneratedHelpers.delete(helper.id);renderHelperSelection();syncHelperBulkControls('guidance',all,selectedGeneratedHelpers);syncCreateExperiment();},resourceInfoButton(helper.name,'Reusable guidance generated from a previous run.',[helperSourceLabel(helper),helper.model?.name],async()=>{const content=await loadGeneratedHelper(helper.id);return content.split(/\n\s*\n/).map(part=>part.trim()).find(part=>part&&!part.startsWith('#'))?.replace(/[`*_]/g,'')||content;})));
  const actions=el('div',null,'helper-compact-actions'),preview=el('details'),body=el('pre','Open to load the saved helper.'),review=el('button',helper.reviewed?'Mark unreviewed':'Mark reviewed');preview.append(el('summary','Preview generated guidance'),body);preview.addEventListener('toggle',async()=>{if(!preview.open||preview.dataset.loaded)return;body.textContent='Loading…';try{body.textContent=await loadGeneratedHelper(helper.id);preview.dataset.loaded='yes';}catch(error){body.textContent=error.message;}});
  review.type='button';review.addEventListener('click',()=>setGeneratedHelperReviewed(helper,!helper.reviewed));actions.append(badge(helper.reviewed?'reviewed':'unreviewed',helper.reviewed?'good':'warn'),review);card.append(actions,preview);target.append(card);
 }
}

function conditionSourceName(source){return source==='participant-vm'?'Participant CAF catalog':source==='baseline'?'Bundled basic tools':source==='http-helper'?'Bundled HTTP helper catalog':source;}
function syncConditionOverview(){
 const overview=$('condition-overview');overview.replaceChildren();
 const count=customConditions.length,helpers=selectedToolHelpers.size+selectedGeneratedHelpers.size;
 const sample=snapshot?.samples?.items?.find(item=>item.id===$('experiment-sample').value);
 const total=count||(sample?.id==='tools-vs-helper'?2:1);
 for(const [value,label] of [[total,total===1?'run configuration':'run configurations'],[Number($('eval-repetitions').value)||1,Number($('eval-repetitions').value)===1?'repetition per task':'repetitions per task'],[helpers,'optional helpers enabled']]){const metric=el('div');metric.append(el('strong',String(value)),el('span',label));overview.append(metric);}
 for(const [index,row] of customConditions.entries()){
  const choice=snapshot?.condition_catalogs?.[row.source],text=`${row.tools.length} tools · ${(row.guidance_assets||[]).length} ${(row.guidance_assets||[]).length===1?'document':'documents'}`;
  const button=$('condition-choice-'+index),status=$('condition-summary-'+index),input=$('condition-name-'+index),name=configurationName(row)||'Unnamed configuration';
  if(button){button.setAttribute('aria-label',name+' · '+text);button.querySelector('small').textContent=text;}if(input?.readOnly)input.value=name;if(status)status.textContent=conditionSourceName(row.source)+' · '+text;
  const remove=$('condition-delete-'+index);if(remove){const label='Remove run configuration '+name;remove.setAttribute('aria-label',label);remove.title=label;}
  const edit=$('condition-edit-'+index);if(edit){const label=(input?.readOnly?'Rename run configuration ':'Save name for ')+name;edit.setAttribute('aria-label',label);edit.title=label;}
 }
 $('add-condition').title=customConditions.length?'Add run configuration with the current tools and guidance':'Add run configuration';
 $('condition-permission-summary').textContent=$('auto-approve-dangerous').checked?'Unattended dangerous actions allowed':'Dangerous actions require approval';
}
function selectCondition(row){
 activeCondition=row;
 for(const [index,item] of customConditions.entries()){const selected=item===row,button=$('condition-choice-'+index);button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;$('condition-panel-'+index).hidden=!selected;}
}
function configurationName(row){return row.name??row.id;}
function configurationNameError(row){
 const name=configurationName(row).trim();
 if(!name||name.length>80||/[\x00-\x1f\x7f]/.test(name))return 'Use a run configuration name between 1 and 80 characters.';
 if(customConditions.some(item=>item!==row&&configurationName(item).trim().toLowerCase()===name.toLowerCase()))return 'Run configuration names must be unique.';
 return '';
}
function configurationIcon(pathData){
 const icon=document.createElementNS('http://www.w3.org/2000/svg','svg'),path=document.createElementNS('http://www.w3.org/2000/svg','path');
 for(const [name,value] of Object.entries({viewBox:'0 0 24 24',width:'16',height:'16',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false'}))icon.setAttribute(name,value);
 path.setAttribute('d',pathData);icon.append(path);return icon;
}
function configurationNameControl(row,index){
 const input=el('input',null,'condition-name-input'),edit=el('button',null,'condition-edit');input.type='text';input.id='condition-name-'+index;input.value=configurationName(row);input.readOnly=true;input.tabIndex=-1;input.required=true;input.maxLength=80;input.setAttribute('aria-label','Run configuration name');
 edit.type='button';edit.id='condition-edit-'+index;edit.append(configurationIcon('M12 20h9M16 3a2.1 2.1 0 0 1 3 3L8 17l-4 1 1-4L16 3Z'));
 let original=input.value;
 const commit=(focus=false)=>{
  row.name=input.value.trim();const error=configurationNameError(row);input.setCustomValidity(error);syncCreateExperiment();
  if(error){input.reportValidity();return;}
  rememberResourceDefaults();input.value=row.name;input.readOnly=true;input.tabIndex=-1;syncConditionOverview();if(focus)$('condition-choice-'+index)?.focus();
 };
 edit.addEventListener('pointerdown',event=>event.preventDefault());
 edit.addEventListener('click',()=>{if(!input.readOnly){commit(true);return;}selectCondition(row);original=configurationName(row);input.readOnly=false;input.tabIndex=0;syncConditionOverview();input.focus();input.select();});
 input.addEventListener('input',()=>{row.name=input.value;input.setCustomValidity(configurationNameError(row));syncConditionOverview();syncCreateExperiment();});
 input.addEventListener('blur',()=>{if(!input.readOnly)commit();});
 input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();commit(true);}else if(event.key==='Escape'){event.preventDefault();row.name=original;input.value=original;input.setCustomValidity('');input.readOnly=true;input.tabIndex=-1;syncConditionOverview();syncCreateExperiment();$('condition-choice-'+index)?.focus();}});
 return [input,edit];
}

function removeCondition(row){
 const index=customConditions.indexOf(row);if(index<0)return;
 customConditions.splice(index,1);rememberResourceDefaults();
 if(activeCondition===row)activeCondition=customConditions[Math.min(index,customConditions.length-1)]||null;
 renderConditions();syncCreateExperiment();
 const selected=customConditions.indexOf(activeCondition);(selected>=0?$('condition-choice-'+selected):$('add-condition')).focus();
}
function conditionDeleteButton(row,index){
 const button=el('button',null,'condition-delete');button.type='button';button.id='condition-delete-'+index;
 const icon=document.createElementNS('http://www.w3.org/2000/svg','svg'),path=document.createElementNS('http://www.w3.org/2000/svg','path');
 for(const [name,value] of Object.entries({viewBox:'0 0 24 24',width:'16',height:'16',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false'}))icon.setAttribute(name,value);
 path.setAttribute('d','M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6');icon.append(path);button.append(icon);
 button.addEventListener('click',()=>removeCondition(row));return button;
}
function renderConditions(){
 const target=$('condition-editor');target.replaceChildren();const picker=$('condition-picker');picker.replaceChildren();
 if(!customConditions.includes(activeCondition))activeCondition=customConditions[0]||null;
 if(customConditions.length){
  const navigation=el('div',null,'condition-navigation');navigation.setAttribute('role','tablist');navigation.setAttribute('aria-label','Agent configurations');
  customConditions.forEach((row,index)=>{const wrapper=el('div',null,'condition-choice-wrap'),button=el('button',null,'condition-choice');wrapper.setAttribute('role','presentation');button.type='button';button.id='condition-choice-'+index;button.setAttribute('role','tab');button.setAttribute('aria-controls','condition-panel-'+index);button.setAttribute('aria-selected',String(row===activeCondition));button.tabIndex=row===activeCondition?0:-1;const space=el('span',null,'condition-name-space');space.setAttribute('aria-hidden','true');button.append(space,el('small',''));button.addEventListener('click',()=>selectCondition(row));button.addEventListener('keydown',event=>{if(event.key==='Delete'){event.preventDefault();removeCondition(row);return;}const next=event.key==='ArrowRight'?(index+1)%customConditions.length:event.key==='ArrowLeft'?(index+customConditions.length-1)%customConditions.length:event.key==='Home'?0:event.key==='End'?customConditions.length-1:null;if(next!==null){event.preventDefault();selectCondition(customConditions[next]);$('condition-choice-'+next).focus();}});wrapper.append(button,...configurationNameControl(row,index),conditionDeleteButton(row,index));navigation.append(wrapper);});picker.append(navigation);
 }else target.append(el('p',$('experiment-sample').value==='scenarioforge-xml'?'Your participant configuration will appear after the CAF catalog loads.':'Using the sample’s preset configuration. Add a run configuration to choose from all participant tools and guidance.','condition-empty-state'));
 customConditions.forEach((row,index)=>{
  const card=el('article',null,'condition-row');card.id='condition-panel-'+index;card.hidden=row!==activeCondition;card.setAttribute('role','tabpanel');card.setAttribute('aria-labelledby','condition-choice-'+index);
  const heading=el('div',null,'condition-heading');heading.append(el('h4','Configure this run configuration'),el('p','','small'));heading.lastChild.id='condition-summary-'+index;card.append(heading);
  const tabs=el('div',null,'condition-content-tabs');tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','Run configuration '+(index+1)+' resources');card.append(tabs);
  const panes={};for(const [name,label] of [['tools','Tools'],['guidance','Guidance & documents']]){const button=el('button',label),pane=el('section',null,'condition-content-pane');button.type='button';button.id=`condition-${index}-tab-${name}`;button.setAttribute('role','tab');button.dataset.conditionView=name;button.setAttribute('aria-controls',`condition-${index}-view-${name}`);pane.id=`condition-${index}-view-${name}`;pane.setAttribute('role','tabpanel');pane.setAttribute('aria-labelledby',button.id);panes[name]=pane;const choose=()=>{conditionViews.set(row,name);for(const b of tabs.children){const selected=b===button;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;panes[b.dataset.conditionView].hidden=!selected;}};button.addEventListener('click',choose);button.addEventListener('keydown',event=>{const all=[...tabs.children],n=all.indexOf(button),next=event.key==='ArrowRight'?(n+1)%all.length:event.key==='ArrowLeft'?(n+all.length-1)%all.length:event.key==='Home'?0:event.key==='End'?all.length-1:null;if(next!==null){event.preventDefault();all[next].click();all[next].focus();}});tabs.append(button);card.append(pane);}
  const selectedView=['tools','guidance'].includes(conditionViews.get(row))?conditionViews.get(row):'tools';for(const button of tabs.children){const selected=button.dataset.conditionView===selectedView;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;panes[button.dataset.conditionView].hidden=!selected;}
  panes.tools.append(el('p','Select tools from the participant. Use ? for a short description.','small'),conditionToolPicker(row,snapshot?.condition_catalogs?.['participant-vm']||{tools:[]}));
  panes.guidance.append(el('p','Selected documents and notes are supplied to the agent in this run configuration. Optional helpers are enabled separately on Helpers.','small'));
  const guidancePicker=conditionGuidancePicker(row,snapshot?.participant_guidance_assets||[]);if(guidancePicker)panes.guidance.append(guidancePicker);else panes.guidance.append(el('p','No CAF documents are available in the loaded participant catalog.','empty'));
  const field=(parent,label,node)=>{const holder=el('label',label);holder.append(node);parent.append(holder);};
  const guidance=el('textarea');guidance.rows=4;guidance.value=row.guidance||'';guidance.maxLength=32000;guidance.placeholder='Optional instructions for this configuration';guidance.addEventListener('input',()=>{row.guidance=guidance.value;});field(panes.guidance,'Additional participant guidance (optional)',guidance);
  target.append(card);
 });
 renderToolHelpers();renderGeneratedHelpers();renderHelperSelection();syncConditionOverview();
}
$('add-condition').addEventListener('click',()=>{let n=1;while(customConditions.some(c=>c.id==='configuration-'+n||configurationName(c).toLowerCase()==='configuration '+n))n++;const previous=activeCondition,source='participant-vm';const row={id:'configuration-'+n,name:'Configuration '+n,source,tools:[...(previous?.tools||snapshot?.condition_catalogs?.[source]?.tools||['nmap','curl','python3'])],guidance:previous?.guidance||'',guidance_assets:[...(previous?.guidance_assets||[])]};customConditions.push(row);rememberResourceDefaults();activeCondition=row;renderConditions();syncCreateExperiment();$('condition-choice-'+(customConditions.length-1)).focus();});
$('auto-approve-dangerous').addEventListener('change',()=>{syncConditionOverview();syncCreateExperiment();});
async function loadParticipantTools(){
 if(participantToolsPromise)return participantToolsPromise;
 if(!snapshot?.roles?.participant)return;
 const vmid=snapshot.roles.participant;
 const run=async()=>{
  operation='Loading CAF tools and guidance from the participant…';syncBusy();$('participant-tools-status').textContent=operation;
  try{
   await finishDashboardRead();await loadSession();
   const response=await apiFetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role:'participant',action:'tools'})}),data=await dashboardJSON(response);
   if(!response.ok)throw Error(data.error||'Unable to load participant tools');
   if(snapshot?.roles?.participant!==vmid)return;
   snapshot.condition_catalogs=snapshot.condition_catalogs||{};snapshot.condition_catalogs['participant-vm']={tools:data.tools.map(item=>item.name),definitions:data.tools};snapshot.participant_guidance_assets=data.guidance_assets||[];participantCatalogReady=true;
   for(const row of customConditions){if(row.source==='participant-vm')row.tools=row.tools.filter(name=>data.tools.some(item=>item.name===name));row.guidance_assets=(row.guidance_assets||[]).filter(id=>snapshot.participant_guidance_assets.some(item=>item.id===id));}
   ensureParticipantCondition();renderConditions();
   $('participant-tools-status').textContent=`Loaded ${data.tools.length} tools and ${snapshot.participant_guidance_assets.length} CAF guidance assets from participant VM ${data.vmid}. Select what this experiment will use.`;
  }catch(error){$('participant-tools-status').textContent='Tool catalog load failed: '+error.message;}
  finally{operation=null;syncBusy();schedulePoll();}
 };
 participantToolsPromise=run();try{await participantToolsPromise;}finally{participantToolsPromise=null;}
}
$('load-participant-tools').addEventListener('click',()=>{if(!isBusy())loadParticipantTools();});

initExperimentWizard();

$('helper-search').addEventListener('input',()=>{renderToolHelpers();renderGeneratedHelpers();});
