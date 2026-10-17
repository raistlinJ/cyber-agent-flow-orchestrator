/* Present structured event content as readable text. */
function transcriptReadableText(value,depth=0){
 if(depth>8)return typeof value==='string'?value:JSON.stringify(value);
 if(typeof value==='string'){
  const trimmed=value.trim(),fenced=trimmed.match(/^```(?:json)?\s*\n([\s\S]*?)\n```$/i),candidate=fenced?fenced[1]:trimmed;
  if(/^[{["]/.test(candidate))try{return transcriptReadableText(JSON.parse(candidate),depth+1);}catch{}
  return value;
 }
 if(value===null)return 'None';
 if(Array.isArray(value))return value.length?value.map(item=>'- '+transcriptReadableText(item,depth+1).replaceAll('\n','\n  ')).join('\n'):'None';
 if(typeof value==='object')return Object.entries(value).map(([key,item])=>{
  const text=transcriptReadableText(item,depth+1);
  if(['text','message','content','reasoning','reasoning_content'].includes(key))return text;
  const label=key.replace(/^_+/,'').replace(/([a-z])([A-Z])/g,'$1 $2').replaceAll('_',' '),title=label.charAt(0).toUpperCase()+label.slice(1);
  return title+((item!==null&&typeof item==='object'||text.includes('\n'))?':\n'+text.replace(/^/gm,'  '):': '+text);
 }).join('\n\n');
 return value===true?'Yes':value===false?'No':String(value??'');
}
function renderTranscriptText(target,event){
 const raw=String(event.text??''),readable=transcriptReadableText(raw),suffix=event.truncated||raw.length>8000?'\n[Shortened; full event is in the run bundle.]':'';
 let text=target.querySelector(':scope > pre');if(!text){text=el('pre');target.append(text);}
 text.textContent=readable.slice(0,8000)+suffix;

}
const stageDetailExpansion=new Map();
const stageDetailScroll=new Map();
const transcriptSelection=new Map();
function renderTranscript(run,parent,kind='caf'){
 const judge=kind!=='caf',field=judge?'judge_transcript':'transcript',title=kind==='preflight'?'Test LLM Judge':judge?'Judge output':'Cyber-agent-flow transcript';
 const key=run.output+'|'+kind+'-transcript',trials=kind==='preflight'?[{trial_id:'Judge startup test',attempt:1,condition_id:'synthetic connection and response check',judge_transcript:run.judge_preflight_transcript}]:(run.sample_progress?.trials||[]).filter(t=>t[field]);
 const details=el('details',null,'stage-details transcript-details');details.dataset.stageKey=key;
 details.open=stageDetailExpansion.get(key)||false;
 details.addEventListener('toggle',()=>stageDetailExpansion.set(key,details.open));
 details.append(el('summary',title));
 if(kind==='preflight')details.append(el('p','Testing that the judge is working before deployment. This checks connectivity, authentication and JSON response format using synthetic data. A sample “passed: false” verdict is expected; it does not mean the experiment failed.','notice judge-test-explanation'));
 const label=el('label','Trial / attempt ','small'),select=el('select');select.setAttribute('aria-label',title+' trial');
 for(const trial of trials)select.add(new Option(`${trial.trial_id} · ${trial.condition_id} · attempt ${trial.attempt}`,trial.trial_id+'|'+trial.attempt));
 const selected=transcriptSelection.get(key),current=run.sample_progress?.current_trial;
 const currentValue=current?current.trial_id+'|'+current.attempt:null;select.value=trials.some(t=>t.trial_id+'|'+t.attempt===selected)?selected:trials.some(t=>t.trial_id+'|'+t.attempt===currentValue)?currentValue:select.options[select.options.length-1]?.value||'';
 label.append(select);details.append(label);
 const status=el('p',null,'small'),output=el('div',null,'stage-output transcript-output');output.tabIndex=0;output.setAttribute('role','region');output.setAttribute('aria-label',title+' output');
 details.append(status,output);
 let restoring=false;
 function draw(){
  restoring=true;
  const trial=trials.find(t=>t.trial_id+'|'+t.attempt===select.value),transcript=trial?.[field];
  const scrollKey=key+'|'+select.value,previous=stageDetailScroll.get(scrollKey);
  output.replaceChildren();
  status.textContent=kind==='preflight'?(transcript?.live?'Testing judge connection and response format…':transcript?'Saved judge startup test output':'Waiting for the judge startup test.'):transcript?.live?'Live · updates with the progress window':transcript?'Saved '+title.toLowerCase():'Waiting for trials to start.';
  if(transcript?.updated_at)status.textContent+=' · Last received '+new Date(transcript.updated_at).toLocaleTimeString();
  if(transcript?.warning)output.append(el('p',transcript.warning,'notice'));
  if(transcript?.truncated)output.append(el('p','Showing recent events. The full transcript is included in the run bundle.','small'));
  for(const event of transcript?.events||[]){
   const entry=el('article',null,'transcript-event');entry.append(el('h4',(event.elapsed_seconds==null?'':duration(event.elapsed_seconds)+' · ')+event.type));renderTranscriptText(entry,event);output.append(entry);
  }
  if(!transcript?.events?.length)output.append(el('p',transcript?.live?'Waiting for '+title.toLowerCase()+' events…':'No transcript events collected yet.','small'));
  const restore=()=>{output.scrollTop=previous?.follow!==false?output.scrollHeight:previous.top;stageDetailScroll.set(scrollKey,{top:output.scrollTop,follow:output.scrollHeight-output.clientHeight-output.scrollTop<40});restoring=false;};
  if(output.isConnected)restore();else requestAnimationFrame(restore);
 }
 output.addEventListener('scroll',()=>{if(!restoring&&output.isConnected)stageDetailScroll.set(key+'|'+select.value,{top:output.scrollTop,follow:output.scrollHeight-output.clientHeight-output.scrollTop<40});});
 select.addEventListener('change',()=>{transcriptSelection.set(key,select.value);draw();});draw();
 details.append(el('p',kind==='preflight'?'Startup diagnostics only. No task evidence is scored in this check; experiment grading happens during evaluation.':judge?'Streamed judge responses, provider-emitted reasoning and review status, including intermediate checks. This may contain private solutions; it is visible only to the experiment owner. Full output is saved in the run bundle.':'Emitted prompts, responses, tool calls/results, and status. Provider reasoning appears only if emitted by cyber-agent-flow. Common credential fields are redacted. The selector shows the latest 10 attempts.','small'));
 parent.append(details);
}

function renderWorkflowProgress(run,target){
 const p=run.workflow_progress;if(!p)return;
 const section=el('section',null,'workflow-progress');section.setAttribute('aria-label','Workflow stages and VM activity');
 section.append(el('h3','Workflow across VMs'));
 const current=p.current;
 if(current){
  const line=el('p',null,'sample-stage');line.append(document.createTextNode(current.label+' · '+current.status+' · '),timer(current.elapsed_seconds,p.observed_at,current.active));
  section.append(line,el('p',current.description,'small'));
 }
 section.append(el('p',p.completed_steps+' / '+p.total_steps+' workflow stages completed. Stage counts do not estimate remaining time.','small'));
 const roles=el('div',null,'workflow-vms');
 for(const role of p.roles||[]){
  const card=el('article',null,'workflow-vm');card.append(el('h4',role.label+(role.vmid?' · VM '+role.vmid:role.role==='orchestrator'?' · host':' · VM not selected')));
  let activity=role.responsibility;
  if(current?.active){
   if(role.role==='orchestrator')activity='Coordinating: '+current.label+'. '+role.responsibility;
   else if(role.role==='core'&&current.id==='deploy')activity='Deployment and readiness checks requested through ScenarioForge. Waiting for its outcome.';
   else if(role.vmid===current.vmid)activity='Current stage: '+current.label+'. '+role.responsibility;
   else if(role.role==='participant'&&current.id!=='evaluate')activity='Waiting for scenario preparation and evaluation inputs. '+role.responsibility;
  }
  card.append(el('p',activity,'small'));roles.append(card);
 }
 section.append(roles);
 const observation=p.guest_observation;
 if(observation&&current?.active&&observation.step===current.id){
  section.append(el('p','Last guest-agent response: VM '+observation.vmid+' · '+observation.state+' · '+new Date(observation.at).toLocaleTimeString()+(observation.pid?' · guest PID '+observation.pid:'')+'. This confirms guest-agent activity, not completion of the scenario.','small'));
 }
 if(p.readiness)renderReadiness(p.readiness,section);
 const transfer=p.transfer;
 if(transfer&&transfer.step===current?.id){
  section.append(el('p','Download from VM '+transfer.vmid+': '+transfer.file+' · '+(transfer.received_bytes??0).toLocaleString()+' / '+(transfer.total_bytes==null?'unknown':transfer.total_bytes.toLocaleString())+' bytes · '+(transfer.verified?'Complete, checksum verified':transfer.status),'small'));
 }
 target.append(section);
}
function renderReadiness(readiness,section){
  const ready=el('section',null,'workflow-readiness');ready.append(el('h4','Scenario readiness evidence'));
  ready.append(el('p','CORE session '+(readiness.session_id??'unknown')+' · '+(readiness.overall??'checked')+' · Checked '+(readiness.checked_at?new Date(readiness.checked_at).toLocaleString():'time unavailable'),'small'));
  for(const check of readiness.checks||[])ready.append(el('p',check.key+': '+check.status+(check.items==null?'':' · '+check.items+' reported items'),'small'));
  ready.append(el('p','Saved readiness evidence from the deployment, not a continuous health check.','small'));section.append(ready);
 }
function renderWorkflowStage(run,stage,p){
  const item=el('section',null,'workflow-step '+stage.status);
  item.dataset.phaseId=stage.id;item.append(badge(stage.status,phaseStatusTone(stage.status)),el('p',stage.vmid?'VM '+stage.vmid:'Orchestrator host','small'));
  if(stage.operation)item.append(el('p',stage.operation,'sample-stage'));
  if(stage.started_at){
   const timing=el('p',null,'small');timing.append(document.createTextNode('Started '+new Date(stage.started_at).toLocaleTimeString()+' · '),timer(stage.elapsed_seconds,p.observed_at,stage.active));
   if(stage.ended_at)timing.append(document.createTextNode(' · Finished '+new Date(stage.ended_at).toLocaleTimeString()));
   if(stage.attempt_count)timing.append(document.createTextNode(' · '+stage.attempt_count+' command attempt(s)'));
   item.append(timing);
  }
  if(stage.error)item.append(el('p',stage.error,'error'));
  if(stage.live_log_bytes!=null)item.append(el('p','Command output collected: '+stage.live_log_bytes+' bytes'+(stage.last_output_at?' · Last output '+new Date(stage.last_output_at).toLocaleTimeString():''),'small'));
  if(stage.log)item.append(el('p','Saved command log: '+stage.log+' (included in the run bundle)','small'));
  if(stage.id==='evaluate'){renderTrialFailures(run,item);renderTrialActivity(run,item);}
  if(['deploy','readiness'].includes(stage.id)&&p.readiness)renderReadiness(p.readiness,item);
  const details=el('details',null,'stage-details'),key=run.output+'|'+stage.id;
  details.dataset.stageKey=key;details.open=stageDetailExpansion.has(key)?stageDetailExpansion.get(key):false;
  details.addEventListener('toggle',()=>{stageDetailExpansion.set(key,details.open);if(stageDetailExpansion.size>200)stageDetailExpansion.delete(stageDetailExpansion.keys().next().value);});
  details.append(el('summary','Stage details and output'));
  const output=el('div',null,'stage-output');output.tabIndex=0;output.setAttribute('role','region');output.setAttribute('aria-label',stage.label+' details and output');
  output.addEventListener('scroll',()=>{stageDetailScroll.set(key,output.scrollTop);if(stageDetailScroll.size>200)stageDetailScroll.delete(stageDetailScroll.keys().next().value);});
  output.append(el('p',stage.operation|| (stage.status==='pending'?'Waiting for earlier stages.':'Detailed checkpoints were not recorded for this stage.'),'small'));
  if(stage.timeout_seconds!=null)output.append(el('p','Command limit: '+stage.timeout_seconds+'s'+(stage.exitcode!=null?' · Exit code: '+stage.exitcode:''),'small'));
  for(const check of stage.input_checks||[])output.append(el('p',(check.id==='judge'||check.label==='Judge endpoint and host credentials'?'Test LLM Judge':check.label)+' · '+check.status+' · '+check.message+(check.seconds==null?'':' · '+duration(check.seconds)),check.status==='fail'?'error':check.status==='warning'?'notice':'small'));
  for(const check of stage.vm_checks||[])output.append(el('p','VM '+check.vmid+' · '+(check.ready?'Ready':'Not ready')+' · '+(check.stopped||[]).length+' recorded job(s) stopped','small'));
  const observation=stage.guest_observation;
  if(observation)output.append(el('p','Last guest response: '+new Date(observation.at).toLocaleTimeString()+' · '+observation.state+(observation.pid?' · PID '+observation.pid:''),'small'));
  for(const transfer of stage.transfers||[])output.append(el('p','VM '+transfer.vmid+' · '+transfer.file+' · '+transfer.received_bytes+' / '+(transfer.total_bytes??'unknown')+' bytes · '+(transfer.verified?'Checksum verified':transfer.status),'small'));
  if(stage.readiness)for(const check of stage.readiness.checks||[]){output.append(el('p','Readiness '+check.key+': '+check.status+(check.summary?' · '+check.summary:''),check.status==='fail'?'error':'small'));for(const detail of check.details||[])output.append(el('p',detail,check.status==='fail'?'error':'small'));}
  if(stage.file_count){output.append(el('p',stage.file_count+' saved file(s)'+(stage.file_count>100?' · showing first 100':''),'small'));output.append(el('pre',stage.files.join('\n')));}
  for(const trial of stage.trials||[]){output.append(el('p',trial.trial_id+' · '+trial.condition_id+' · '+trial.status+' · Verified: '+(trial.verified_success??'pending'),'small'));if(trial.transport)output.append(el('p',trial.transport.activity+' · '+(trial.transport.files_uploaded??0)+' / '+(trial.transport.files_total??'unknown')+' files transferred','small'));for(const error of trial.errors||[])output.append(el('p',error,'error'));}
  if(stage.events?.length)output.append(el('h4','Checkpoints and recent output'),el('pre',stage.events.map(event=>`${event.at} [${event.kind}] ${stage.id==='preflight'?event.message.replaceAll('Judge endpoint and host credentials','Test LLM Judge').replaceAll('Testing judge is working','Test LLM Judge'):event.message}`).join('\n')));
  if(stage.log_tail!=null)output.append(el('h4','Saved command log · last 60 lines'),el('pre',stage.log_tail||'(No output recorded yet)'));
  details.append(output);
  requestAnimationFrame(()=>{output.scrollTop=stageDetailScroll.get(key)||0;});
  item.append(details);
  if(stage.id==='evaluate'){renderTranscript(run,item);renderTranscript(run,item,'judge');}
  if(stage.id==='preflight'&&run.judge_preflight_transcript)renderTranscript(run,item,'preflight');
 return item;
}
/* Shared visual conventions; all run content is rendered as text. */
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live, sample=false) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';if(sample)node.dataset.source='sample';} return node;}
function renderTrialActivity(run,target){
 if(!run.sample_progress)return;
 const expanded=new Set([...$('phase-content').querySelectorAll('details[open]')].map(node=>node.dataset.run));
  const p=run.sample_progress,trial=p.current_trial,transport=trial?.transport;
  const card=el('article',null,'panel sample-activity'),head=el('div',null,'result-actions');head.append(el('h3',p.name),badge(run.recorded_status,run.recorded_status==='completed'?'good':['failed','interrupted','completed_with_errors'].includes(run.recorded_status)?'warn':''));
  card.append(head);
  card.append(el('p',run.recorded_status==='interrupted'?'Coordinator is no longer active. Inspect results before retrying.':run.recorded_status==='stopping'?run.message:transport?.activity||run.message||p.phase,'sample-stage'));
  const progress=el('progress');progress.max=100;progress.value=p.percent??0;progress.setAttribute('aria-label',`${p.name} trials finished`);
  card.append(el('p',p.planned_trials?`${p.finished_trials} / ${p.planned_trials} trials finished · ${p.percent??0}% · ${p.verified_successes} verified successes · ${p.errors} trial errors`:'Trial plan pending ScenarioForge export and evaluation initialization','small'),progress);
  card.append(el('p','Percentage counts finished trials, including errors. Cleanup and finalization may still be pending at 100%.','small'));
  const elapsed=el('p',null,'small');elapsed.append(document.createTextNode('Run elapsed: '),timer(p.elapsed_seconds,p.observed_at,p.active,true));card.append(elapsed);
  if(trial){
   card.append(el('h4','Current trial on participant VM '+(run.saved_settings?.participant_vmid??'—')));
   card.append(el('p','Task: '+trial.task_id,'small'));
   if(trial.progress_monitor){const m=trial.progress_monitor;const complete=Object.values(m.criteria).filter(s=>s==='satisfied').length;card.append(el('p',`${complete} / ${m.planned_criteria??Object.keys(m.criteria).length} criteria observed complete · ${m.checks} progress checks · ${m.latest?.status||'pending'}`,'small'));if(trial.completed_steps?.length)card.append(el('p','Completed steps: '+trial.completed_steps.join(', '),'small'));if(trial.progress_monitor_error)card.append(el('p','Intermediate status unverified: '+trial.progress_monitor_error,'notice'));}
   if(trial.progress_monitor?.steps?.length)for(const step of trial.progress_monitor.steps)card.append(el('p',step.id+' · '+step.title+' · '+step.status+(step.eligible?'':' · prerequisites pending'),'small'));
   if(trial.hints_released!=null)card.append(el('p',`${trial.hints_released} hints released · ${trial.solutions_released??0} solutions provided · ${trial.facts_revealed??0} required facts revealed`,'small'));
   card.append(el('p',`${trial.trial_id} · ${trial.condition_id} · repetition ${trial.repetition} · attempt ${trial.attempt}`));
   const timing=el('p',null,'small');timing.append(document.createTextNode('Trial elapsed (includes setup and collection): '),timer(trial.elapsed_seconds,p.observed_at,p.active,true));card.append(timing);
   if(transport?.participant_active_seconds!=null)card.append(el('p',`Participant active time: ${duration(transport.participant_active_seconds)} · Assistance wait: ${duration(transport.assistance_wait_seconds)} (excluded from participant limit)`,'small'));
   if(trial.deferred_steps?.length)card.append(el('p','Deferred, still unverified: '+trial.deferred_steps.join(', '),'notice'));
   if(transport?.files_total)card.append(el('p',`Inputs transferred: ${transport.files_uploaded} / ${transport.files_total} files · ${(transport.bytes_uploaded??0).toLocaleString()} / ${(transport.bytes_total??0).toLocaleString()} bytes acknowledged`,'small'));
   if(transport?.service?.SubState)card.append(el('p',`Last observed guest service: ${transport.service.SubState}${transport.service.ExecMainPID?' · PID '+transport.service.ExecMainPID:''}`,'small'));
   if(transport?.updated_at)card.append(el('p',`Guest stage last recorded: ${new Date(transport.updated_at).toLocaleTimeString()}`,'small'));
  }
  card.append(el('p',`Per-trial limits: ${p.max_turns} turns · ${p.wall_seconds===0?"no timeout":p.wall_seconds+"s"} participant active budget; assistance waits excluded. Agent and judge transcripts are available on this phase page; scores appear after collection.`,'small'));
  if(p.trials.length){const wrap=el('div',null,'scroll'),table=el('table'),header=el('tr'),thead=el('thead'),body=el('tbody');for(const title of ['Trial','Run configuration','Status','Verified','Score'])header.append(el('th',title));thead.append(header);for(const trial of p.trials){const row=el('tr');for(const value of [trial.trial_id,trial.condition_id,trial.status,trial.verified_success===true?'Yes':trial.verified_success===false?'No':trial.status==='running'?'Pending':'Unavailable',trial.score??'—'])row.append(el('td',value));body.append(row);}table.append(thead,body);wrap.append(table);card.append(wrap);}
  for(const trial of p.trials)for(const error of trial.errors||[])card.append(el('p',`${trial.trial_id} · ${error}`,'error'));
  if(run.error)card.append(el('p',run.error,'error'));
  const details=el('details'),summary=el('summary','Recent run events');details.dataset.run=run.output;details.dataset.stageKey=run.output+'|trial-events';details.open=stageDetailExpansion.get(details.dataset.stageKey)||expanded.has(run.output);details.addEventListener('toggle',()=>stageDetailExpansion.set(details.dataset.stageKey,details.open));details.append(summary,el('pre',p.events.slice(-100).map(e=>`${e.at} · ${e.message}`).join('\n')));card.append(details);target.append(card);
}
const progressView={run:null,selected:null,follow:true,rendered:null,content:null};
function phaseStatusTone(status){return ['completed','skipped'].includes(status)?'good':['failed','interrupted','completed_with_errors','cancelled'].includes(status)?'warn':'';}
function phasePages(run){return [{id:'overview',label:'Overview',description:'Overall run status, trial totals and activity across the lab.',status:run.recorded_status},...(run.workflow_progress?.steps||[])];}
function lastReachedPhaseIndex(run,pages=phasePages(run)){
 let last=0;pages.forEach((page,index)=>{if(index&&(page.started_at||page.ended_at||page.active||(page.status&&page.status!=='pending')))last=index;});return last;
}
function preferredPhase(run){const p=run.workflow_progress,steps=p?.steps||[],pages=phasePages(run),last=lastReachedPhaseIndex(run,pages),id=steps.find(stage=>stage.active)?.id||steps.find(stage=>['failed','interrupted'].includes(stage.status))?.id||p?.current?.id||steps.find(stage=>stage.id===p?.current_step)?.id||pages[last].id;const index=pages.findIndex(page=>page.id===id);return index>=0&&index<=last?id:pages[last].id;}
function phaseLabel(page){return page.label.replace(/^\d+\.\s*/, '');}
function requestedPhase(){return new URLSearchParams(location.hash.slice(1)).get('phase');}
function setProgressPhase(id,{focus=true,follow=false,history=true}={}){
 if(!progressView.run)return false;
 const pages=phasePages(progressView.run),index=pages.findIndex(page=>page.id===id);if(index<0||index>lastReachedPhaseIndex(progressView.run,pages))return false;
 progressView.follow=follow;progressView.selected=id;
 if(history)window.history.pushState(null,'','#'+new URLSearchParams({phase:follow?'active':id}));
 renderProgress(progressView.run,{navigate:true,focus});return true;
}
function resetProgressView(){
 progressView.run=null;progressView.selected=null;progressView.rendered=null;progressView.content=null;progressView.follow=true;
 for(const id of ['phase-content','phase-navigation','phase-select'])$(id).replaceChildren();
 $('phase-title').textContent='Progress unavailable';$('phase-description').textContent='';$('phase-number').textContent='EXPERIMENT PROGRESS';$('phase-completion').value=0;$('phase-completion-label').textContent='';$('phase-follow').checked=true;$('phase-previous').disabled=true;$('phase-next').disabled=true;$('phase-position').textContent='';
}
function initProgressNavigation(){
 $('phase-select').addEventListener('change',()=>{if(!setProgressPhase($('phase-select').value))$('phase-select').value=progressView.selected;});
 $('phase-follow').addEventListener('change',()=>{progressView.follow=$('phase-follow').checked;if(progressView.follow&&progressView.run)setProgressPhase(preferredPhase(progressView.run),{follow:true});});
 for(const [id,offset] of [['phase-previous',-1],['phase-next',1]])$(id).addEventListener('click',()=>{const pages=phasePages(progressView.run),index=pages.findIndex(page=>page.id===progressView.selected);if(pages[index+offset])setProgressPhase(pages[index+offset].id);});
 window.addEventListener('hashchange',()=>{const id=requestedPhase();if(progressView.run&&!setProgressPhase(!id||id==='active'?preferredPhase(progressView.run):id,{history:false,follow:!id||id==='active'}))window.history.replaceState(null,'','#'+new URLSearchParams({phase:progressView.follow?'active':progressView.selected}));});
}
function syncPhaseNavigation(run,pages){
 const last=lastReachedPhaseIndex(run,pages),nav=$('phase-navigation'),select=$('phase-select'),existing=new Map([...nav.children].map(node=>[node.dataset.phaseId,node])),options=new Map([...select.options].map(option=>[option.value,option]));
 pages.forEach((page,index)=>{
  let button=existing.get(page.id);existing.delete(page.id);
  if(!button){button=el('button',null,'wizard-nav-step progress-phase-link');button.type='button';button.dataset.phaseId=page.id;button.append(el('span',index||'•','wizard-nav-number'));const text=el('span',null,'wizard-nav-text');text.append(el('strong'),el('span',null,'wizard-nav-state'));button.append(text);button.addEventListener('click',()=>setProgressPhase(page.id));button.addEventListener('keydown',event=>{const pages=phasePages(progressView.run),index=pages.findIndex(item=>item.id===page.id),last=lastReachedPhaseIndex(progressView.run,pages),next=event.key==='ArrowDown'?Math.min(index+1,last):event.key==='ArrowUp'?Math.max(index-1,0):event.key==='Home'?0:event.key==='End'?last:null;if(next!==null){event.preventDefault();setProgressPhase(pages[next].id,{focus:false});nav.querySelector('[aria-current=step]').focus();}});nav.append(button);}
  if(nav.children[index]!==button)nav.insertBefore(button,nav.children[index]||null);
  button.disabled=index>last;button.title=index>last?'This phase is unavailable until the run reaches it.':'';
  const selected=page.id===progressView.selected;button.querySelector('strong').textContent=phaseLabel(page);button.querySelector('.wizard-nav-state').textContent=(page.status||'pending').replaceAll('_',' ')+(page.elapsed_seconds==null?'':' · '+duration(page.elapsed_seconds));button.dataset.state=page.status||'pending';button.setAttribute('aria-label',(index?'Phase '+index+': ':'')+phaseLabel(page)+' · '+(page.status||'pending'));if(selected)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');
  let option=options.get(page.id);options.delete(page.id);if(!option){option=new Option('',page.id);select.add(option);}option.disabled=index>last;option.textContent=(index?index+'. ':'')+phaseLabel(page)+' · '+(page.status||'pending')+(index>last?' · not reached':'');
 });
 for(const node of existing.values())node.remove();for(const option of options.values())option.remove();select.value=progressView.selected;
 const completed=run.workflow_progress?.completed_steps??0,total=run.workflow_progress?.total_steps??pages.length-1;$('phase-completion').max=Math.max(1,total);$('phase-completion').value=completed;$('phase-completion-label').textContent=completed+' / '+total+' phases completed';$('phase-follow').checked=progressView.follow;
}
function renderProgress(run,{navigate=false,focus=false}={}){
 progressView.run=run;const pages=phasePages(run);
 if(progressView.selected===null){const requested=requestedPhase();progressView.selected=pages.some(page=>page.id===requested)?requested:preferredPhase(run);if(requested&&requested!=='active')progressView.follow=false;}
 if(progressView.follow)progressView.selected=preferredPhase(run);
 const selectedIndex=pages.findIndex(page=>page.id===progressView.selected);
 if(selectedIndex<0||selectedIndex>lastReachedPhaseIndex(run,pages)){progressView.selected=preferredPhase(run);if(requestedPhase()&&requestedPhase()!=='active')window.history.replaceState(null,'','#'+new URLSearchParams({phase:progressView.selected}));}
 syncPhaseNavigation(run,pages);
 const index=pages.findIndex(page=>page.id===progressView.selected),page=pages[index];$('phase-title').textContent=phaseLabel(page);$('phase-description').textContent=page.description||'';$('phase-number').textContent=index?'PHASE '+index+' OF '+(pages.length-1):'EXPERIMENT OVERVIEW';$('phase-position').textContent=index?'Phase '+index+' of '+(pages.length-1):'Overview';$('phase-previous').disabled=index===0;$('phase-next').disabled=index>=lastReachedPhaseIndex(run,pages);$('phase-next').title=index===pages.length-1?'This is the last phase.':$('phase-next').disabled?'The run has not reached the next phase.':'';$('phase-next').textContent=pages[index+1]?'Next: '+phaseLabel(pages[index+1]):'Last phase';
 const content=$('phase-content'),fingerprint=JSON.stringify(index?[page,page.id==='evaluate'?[run.sample_progress,run.trial_failures,run.failure_diagnostics,run.error]:null,page.id==='preflight'?run.judge_preflight_transcript:null,page.active?run.workflow_progress?.observed_at:null,['deploy','readiness'].includes(page.id)?run.workflow_progress?.readiness:null]:run),changed=progressView.rendered!==page.id;
 if(changed)requestAnimationFrame(()=>{const nav=$('phase-navigation'),button=nav.querySelector('[aria-current=step]');if(!button||!nav.clientHeight)return;const bounds=button.getBoundingClientRect(),viewport=nav.getBoundingClientRect();if(bounds.top<viewport.top)nav.scrollTop-=viewport.top-bounds.top;else if(bounds.bottom>viewport.bottom)nav.scrollTop+=bounds.bottom-viewport.bottom;});
 if(changed||progressView.content!==fingerprint){
  const top=window.scrollY,active=document.activeElement,focusLabel=content.contains(active)?active.getAttribute('aria-label'):null,focusDetails=content.contains(active)&&active.tagName==='SUMMARY'?active.parentElement.dataset.stageKey:null;
  // Expansion state belongs to the run/phase, not to the replaced DOM nodes.
  for(const details of content.querySelectorAll('details[data-stage-key]'))stageDetailExpansion.set(details.dataset.stageKey,details.open);
  const next=el('div');
  if(index){next.append(renderWorkflowStage(run,page,run.workflow_progress));}
  else{
   next.append(badge(run.recorded_status,phaseStatusTone(run.recorded_status)),el('p',run.message||'Waiting for workflow activity.','sample-stage'));
   if(run.error&&run.error!==run.message)next.append(el('p',run.error,'error'));
   if((run.error||run.message||'').includes('Workflow, host artifacts, or source changed'))next.append(el('p','The experiment was saved before a change to the workflow, host inputs or source code. Run it again to create a new output directory using the current source and saved settings.','notice'));
   const summary=run.sample_progress;if(summary)next.append(el('p',`${summary.finished_trials} / ${summary.planned_trials||'pending'} trials finished · ${summary.verified_successes} verified successes · ${summary.errors} trial errors`,'small'));
   if(run.workflow_progress)renderWorkflowProgress(run,next);else{renderTrialFailures(run,next);renderTrialActivity(run,next);}
  }
  content.replaceChildren(...next.childNodes);progressView.rendered=page.id;progressView.content=fingerprint;
  if(!navigate&&!changed){if(focusLabel)[...content.querySelectorAll('[aria-label]')].find(node=>node.getAttribute('aria-label')===focusLabel)?.focus({preventScroll:true});if(focusDetails)[...content.querySelectorAll('details[data-stage-key]')].find(node=>node.dataset.stageKey===focusDetails)?.querySelector('summary')?.focus({preventScroll:true});window.scrollTo({top});}
 }
 if(navigate){$('phase-title').focus({preventScroll:true});$('phase-title').scrollIntoView({block:'start'});if(!focus)document.activeElement?.blur();}
}

function renderResultSummary(data){
 const configurationNames=new Map((data.run_configuration?.conditions||[]).map(row=>[row.id,row.name||row.id]));
 renderRunConfiguration(data.run_configuration);
 const target=$('result-summary');target.replaceChildren();
 const workflow=data.workflow||{};target.append(el('p',`${workflow.recorded_status||'Unknown'} · ${workflow.message||''}`));
 if(workflow.error)target.append(el('p',workflow.error,'error'));
 if(!data.evaluation){target.append(el('p','Trial results will appear here once evaluation starts.','small'));return;}
 for(const trial of data.evaluation.attempts||[])for(const error of trial.errors||[])target.append(el('p',`${trial.trial_id} · ${trial.status}: ${error}`,'error'));
 for(const trial of data.evaluation.attempts||[])if(trial.provide_progressive_hints&&trial.progressive_hints_available===false)target.append(el('p',`${trial.trial_id} · Progressive hints unavailable: ${trial.progressive_hints_reason||'No usable guidance was supplied; ran unassisted.'}`,'notice'));
 for(const trial of data.evaluation.attempts||[])if(trial.progress_monitor_enabled){
  const detail=el('details');detail.append(el('summary',`${trial.trial_id} · Intermediate progress · ${trial.progress_monitor_checks??0} checks · ${trial.progress_monitor_errors??0} unverified checks`));
  for(const check of trial.progress_monitor?.checks||[])detail.append(el('p',`Turn ${check.turn}: ${check.status}${check.error?' · '+check.error:' · '+(check.completed_steps||[]).join(', ')}`,'small'));
  for(const [id,finding] of Object.entries(trial.progress_monitor?.criteria||{}))detail.append(el('p',id+' · '+finding.status+' · '+finding.reason,'small'));
  detail.append(el('p',`Checkpoint time: ${duration(trial.progress_monitor_seconds)} · cost: ${trial.progress_monitor_cost_usd??'unknown'} USD · Full reviews: progress-checks/ in the run bundle`,'small'));target.append(detail);
 }
 for(const trial of data.evaluation.attempts||[])if(trial.judge_enabled){
  const details=el('details'),summary=el('summary',`${trial.trial_id} · Judge: ${trial.judge_error?'error':trial.task_outcome|| (trial.judge_passed?'pass':'fail')} · ${duration(trial.judge_seconds)}`);details.append(summary,el('p',trial.judge_error||trial.judge_reason||'Judge verdict recorded.'));details.append(el('p',`${trial.judge_calls??0} model calls · ${trial.judge_prompt_tokens??'unknown'} input / ${trial.judge_output_tokens??'unknown'} output tokens · Full evidence review: judge.json in the run bundle`,'small'));target.append(details);
  if(trial.judge_evidence_warning)details.append(el('p',trial.judge_evidence_warning,'notice'));
  if(trial.judge_evidence_files?.length)details.append(el('p','Evidence read: '+trial.judge_evidence_files.join(', '),'small'));
 }
 for(const trial of data.evaluation.attempts||[]){
  const card=el('details'),summary=el('summary',trial.trial_id+' · '+(trial.task_outcome||'unverified')+' · execution: '+trial.status+' · assistance: '+(trial.assistance_level||'none'));card.append(summary);
  for(const criterion of trial.criterion_results||[]){card.append(el('h4',criterion.id+' · '+criterion.status),el('p',criterion.reason),el('pre',JSON.stringify(criterion.evidence,null,2)));}
  card.append(el('p','Participant cost: '+(trial.participant_cost_usd??'unknown')+' USD · judge cost: '+(trial.judge_cost_usd??'unknown')+' USD · reset: '+duration(trial.reset_seconds),'small'));target.append(card);
 }
 for(const comparison of data.evaluation.paired_comparisons||[]){target.append(el('p',comparison.condition+' vs '+comparison.baseline+' · success difference: '+(comparison.mean_success_difference??'unknown')+' · 95% interval: '+(comparison.confidence_interval_95?.join(' to ')||'not available')+' · '+comparison.independent_scenarios+' scenario clusters','small'));}
 renderTrialFailures(workflow,target,data.failure_diagnostics);

 const groups=Object.entries(data.evaluation.conditions||{});
 const table=el('table'),head=el('tr');for(const text of ['Run configuration','Verified successes','Unassisted successes','Hint-assisted successes','Solution-assisted successes','Hints released','Solutions provided','Facts revealed','Mean runtime','First flag'])head.append(el('th',text));const heading=el('thead');heading.append(head);table.append(heading);const body=el('tbody');
 for(const [name,summary] of groups){const row=el('tr');row.append(el('td',configurationNames.get(name)||name),el('td',`${summary.verified_successes??0} / ${summary.verified_trials??0}`),el('td',summary.unassisted_successes??'—'),el('td',summary.hints_assisted_successes??summary.assisted_successes??'—'),el('td',summary.solution_assisted_successes??0),el('td',summary.hints_released??0),el('td',summary.solutions_released??0),el('td',summary.facts_revealed??0),el('td',duration(summary.mean_execution_seconds)),el('td',duration(summary.mean_time_to_first_flag_seconds)));body.append(row);}table.append(body);const wrapper=el('div',null,'scroll');wrapper.append(table);target.append(wrapper);
}

function renderTrialFailures(run,target,providedDiagnostics){
 const failures=run.trial_failures||[],diagnostics=providedDiagnostics||run.failure_diagnostics||[];
 if(!failures.length&&!diagnostics.length&&run.recorded_status!=='completed_with_errors')return;
 const section=el('section',null,'panel failure-summary');section.setAttribute('aria-label','Experiment errors');
 section.append(el('h3','Experiment errors'),el('p',failures.length?`${failures.length} trial(s) ended with an execution error. Deployment may still have succeeded.`:'This experiment completed with errors. Inspect the trial records and collected worker logs below.','error'));
 for(const trial of failures){
  section.append(el('h4',`${trial.trial_id} · attempt ${trial.attempt} · ${trial.status}`),el('p',`Run configuration: ${trial.condition_id||'unknown'} · Task: ${trial.task_id||'unknown'}`,'small'));
  for(const error of trial.errors||[])section.append(el('p',error,'error'));
  if(trial.attempt_path)section.append(el('p','Run bundle: evaluation/'+trial.attempt_path+'/attempt.json','small'));
 }
 target.append(section);
 for(const item of diagnostics||[]){
  const section=el('section',null,'failure-diagnostics');section.append(el('h3',`${item.trial_id} · attempt ${item.attempt} · Failure details`));
  section.append(el('p','Saved on the orchestrator host. Log tails are limited to 80 lines; common credential fields are redacted.','small'));
  for(const decision of item.interactions||[])section.append(el('p',decision.explanation+(decision.tool?' Tool: '+decision.tool:'')+(decision.timeout_seconds!=null?' · Checkpoint: '+decision.timeout_seconds+'s':''),'error'));
  if(item.status)section.append(el('p','Trial status: '+item.status,'error'));
  for(const error of item.errors||[])section.append(el('p',error,'error'));
  const transport=item.transport;
  if(transport){section.append(el('p','Last saved guest phase: '+transport.phase+' · Service: '+transport.unit,'small'));const state=transport.service||{};section.append(el('p',Object.entries(state).map(([key,value])=>key+': '+value).join(' · ')||'No saved service exit state.','small'));}
  for(const result of item.worker_results||[]){section.append(el('p','Saved worker result: '+result.status+' · '+result.path,'small'));for(const error of result.errors||[])section.append(el('p',error,'error'));}
  for(const record of item.model_errors||[])section.append(el('p',record.error,'error'));
  for(const log of item.logs||[]){const details=el('details');details.open=true;details.append(el('summary','Collected worker log'),el('div',log.path,'small'),el('pre',log.text||'(Log is empty)'));section.append(details);}
  for(const note of item.notes||[])section.append(el('p',note,'small'));
  target.append(section);
 }
}

/* Two stable, owner-scoped transcript panes, synchronized with the graph trial. */
const visualTranscriptViews=new Map(),visualTranscriptPanels={caf:null,judge:null};
function visualTranscriptView(kind){const panel=visualTranscriptPanels[kind];return panel?visualTranscriptViews.get(panel.scope):null;}
function initVisualTranscripts(){
 for(const kind of ['caf','judge']){
  const output=$('visual-'+kind+'-transcript'),follow=$('visual-'+kind+'-follow');
  output.addEventListener('scroll',()=>{if(!output.clientHeight)return;const state=visualTranscriptView(kind);if(!state)return;if(state.programmaticTop!=null&&Math.abs(output.scrollTop-state.programmaticTop)<1){state.programmaticTop=null;return;}state.programmaticTop=null;state.top=output.scrollTop;state.follow=output.scrollHeight-output.clientHeight-output.scrollTop<40;follow.checked=state.follow;});
  const pause=()=>{const state=visualTranscriptView(kind);if(state){state.follow=false;follow.checked=false;}};output.addEventListener('wheel',event=>{if(event.deltaY<0)pause();},{passive:true});output.addEventListener('keydown',event=>{if(['Home','PageUp','ArrowUp'].includes(event.key)||event.key===' '&&event.shiftKey)pause();});
  follow.addEventListener('change',()=>{const state=visualTranscriptView(kind);if(state){state.follow=follow.checked;if(state.follow)output.scrollTop=output.scrollHeight;state.top=output.scrollTop;}});
  $('visual-'+kind+'-latest').addEventListener('click',()=>{const state=visualTranscriptView(kind);if(state){state.follow=true;follow.checked=true;output.scrollTop=output.scrollHeight;state.top=output.scrollTop;}});
 }
}
function resetVisualTranscripts(){
 $('visual-speaker-status').textContent='';visualTranscriptViews.clear();for(const kind of ['caf','judge']){visualTranscriptPanels[kind]=null;const card=$('visual-'+kind+'-panel');card.classList.remove('is-active','is-paused');card.setAttribute('aria-busy','false');$('visual-'+kind+'-badge').textContent='Waiting';$('visual-'+kind+'-context').textContent='No trial selected';$('visual-'+kind+'-updated').textContent='';$('visual-'+kind+'-events').replaceChildren();$('visual-'+kind+'-info').textContent='Waiting for transcript output.';$('visual-'+kind+'-info').hidden=false;$('visual-'+kind+'-follow').checked=true;}
}
function renderVisualTranscripts(run,graphTrial){
 const trials=run.sample_progress?.trials||[],current=run.sample_progress?.current_trial;
 const row=graphTrial?.trial_id?trials.find(item=>item.trial_id===graphTrial.trial_id&&item.attempt===graphTrial.attempt):trials.find(item=>item.trial_id===current?.trial_id&&item.attempt===current?.attempt)||trials.at(-1);
 const preflight=!row&&Boolean(run.judge_preflight_transcript),trialScope=row?row.trial_id+'|'+row.attempt:graphTrial?.trial_id?graphTrial.trial_id+'|'+graphTrial.attempt:preflight?'preflight':'waiting';
 for(const kind of ['caf','judge']){
  const transcript=kind==='judge'&&preflight?run.judge_preflight_transcript:row?.[kind==='judge'?'judge_transcript':'transcript'];
  const scope=[run.output,trialScope,kind].join('|'),previous=visualTranscriptPanels[kind];
  let state=visualTranscriptViews.get(scope);if(!state){state={follow:true,top:0,visible:false,programmaticTop:null};visualTranscriptViews.set(scope,state);if(visualTranscriptViews.size>60)visualTranscriptViews.delete(visualTranscriptViews.keys().next().value);}
  visualTranscriptPanels[kind]={scope,run,row,graphTrial,transcript,preflight:kind==='judge'&&preflight};
  const context=kind==='judge'&&preflight?'Test LLM Judge · startup check':row?row.trial_id+' · '+(row.condition_id||row.task_id||'')+' · attempt '+row.attempt:graphTrial?.trial_id?graphTrial.trial_id+' · attempt '+graphTrial.attempt:'Waiting for trials to start';
  if($('visual-'+kind+'-context').textContent!==context)$('visual-'+kind+'-context').textContent=context;
  const updated=transcript?.updated_at,date=updated?new Date(updated):null;$('visual-'+kind+'-updated').textContent=date&&Number.isFinite(date.getTime())?'Last received '+date.toLocaleTimeString():'';$('visual-'+kind+'-panel').querySelector('.visual-transcript-heading').title=context+($('visual-'+kind+'-updated').textContent?' · '+$('visual-'+kind+'-updated').textContent:'');
  const events=(transcript?.events||[]).slice(-80),info=[transcript?.warning,transcript?.truncated?'Showing recent events; full output is in the run bundle.':'',preflight&&kind==='judge'?'Synthetic startup check; this is not an experiment verdict.':'',!events.length?(graphTrial?.trial_id&&!row?'Transcript unavailable for this selected attempt.':transcript?'Waiting for transcript events…':'No transcript output collected yet.'):''].filter(Boolean).join(' ');
  const fingerprint=JSON.stringify([events,info]);
  if(previous?.scope===scope&&state.fingerprint===fingerprint){const output=$('visual-'+kind+'-transcript');if(!output.clientHeight)state.visible=false;else if(!state.visible||state.follow)restoreVisualTranscriptPosition(kind,state);continue;}
  const holder=$('visual-'+kind+'-events');
  if(previous?.scope!==scope)holder.replaceChildren();
  $('visual-'+kind+'-info').textContent=info;$('visual-'+kind+'-info').hidden=!info;
  for(const [index,event] of events.entries()){
   const mark=JSON.stringify(event);let entry=holder.children[index];if(entry?.dataset.event===mark)continue;
   if(!entry){entry=el('article',null,'visual-transcript-event');entry.append(el('h5'),el('pre'));holder.append(entry);}
   entry.dataset.event=mark;entry.dataset.eventType=String(event.type||'event');
   const seconds=event.elapsed_seconds;entry.querySelector('h5').textContent=(Number.isFinite(seconds)?duration(seconds)+' · ':'')+String(event.type||'event').slice(0,80);
   renderTranscriptText(entry,event);
  }
  while(holder.children.length>events.length)holder.lastElementChild.remove();
  state.fingerprint=fingerprint;$('visual-'+kind+'-follow').checked=state.follow;
  restoreVisualTranscriptPosition(kind,state);
 }
 syncVisualTranscriptActivity();
}
function restoreVisualTranscriptPosition(kind,state){const output=$('visual-'+kind+'-transcript');if(!output.clientHeight){state.visible=false;return;}output.scrollTop=state.follow?output.scrollHeight:state.top;state.top=output.scrollTop;state.programmaticTop=output.scrollTop;state.visible=true;}
function judgeTranscriptEnded(transcript){
 const statuses=(transcript?.events||[]).filter(event=>event.type==='status');const last=statuses.at(-1);if(!last)return false;
 let text=last.text;try{const fields=JSON.parse(text);if(typeof fields.text==='string')text=fields.text;}catch{}
 return /^Judge review (completed|error)\b/.test(text);
}
function syncVisualTranscriptActivity(){
 const panels=visualTranscriptPanels,caf=panels.caf,judge=panels.judge;if(!caf||!judge)return;
 const fresh=visualRunLive()&&performance.now()-visualState.receivedAt<45000,row=caf.row,selectedRunning=row?.status==='running',graph=caf.graphTrial,phase=caf.run.workflow_progress?.current;
 const startup=judge.preflight&&phase?.id==='preflight'&&phase.active&&(phase.input_checks||[]).some(check=>check.id==='judge'&&check.status==='running');
 const review=selectedRunning&&(graph?.activity?.phase==='reviewing'||graph?.monitor?.latest?.status==='running'||row.progress_monitor?.latest?.status==='running');
 const finalReview=selectedRunning&&row.transport?.phase==='collected'&&judge.transcript?.events?.length&&!judgeTranscriptEnded(judge.transcript);
 const judgeWorking=Boolean(fresh&&(startup||review||finalReview));
 const transportAt=Date.parse(row?.transport?.updated_at),serverNow=Date.parse(caf.run.sample_progress?.observed_at||caf.run.workflow_progress?.observed_at),transportAge=(Number.isFinite(serverNow)?serverNow:Date.now())-transportAt,transportFresh=!Number.isFinite(transportAt)||transportAge>=-15000&&transportAge<=45000;
 const worker=selectedRunning&&(transportFresh&&['preparing','uploading','starting','executing','stopping','collecting'].includes(row.transport?.phase)||visualActivityLive());
 const cafWorking=Boolean(fresh&&worker&&!judgeWorking);
 for(const [kind,working] of [['caf',cafWorking],['judge',judgeWorking]]){
  const panel=panels[kind],events=panel.transcript?.events?.length,ongoing=Boolean(selectedRunning||panel.preflight&&phase?.active),stale=visualRunLive()&&!fresh;
  const paused=kind==='caf'&&judgeWorking,label=working?'Active':paused?'Paused':stale&&ongoing?'Updates stale':!visualRunLive()||row&&row.status!=='running'?events?'Saved':'No output':'Waiting';
  const card=$('visual-'+kind+'-panel');card.classList.toggle('is-active',working);card.classList.toggle('is-paused',paused);card.setAttribute('aria-busy',String(working));
  const badge=$('visual-'+kind+'-badge');if(badge.textContent!==label)badge.textContent=label;
 }
 const speaker=$('visual-speaker-status'),speakerLabel=judgeWorking?'Judge active':cafWorking?'CAF active':'';if(speaker.textContent!==speakerLabel)speaker.textContent=speakerLabel;speaker.classList.toggle('judge',judgeWorking);scheduleVisualActionFollow();
}
