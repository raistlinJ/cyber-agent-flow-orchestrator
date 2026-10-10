const stageDetailExpansion=new Map();
const stageDetailScroll=new Map();
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
 for(const role of p.roles){
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
 if(p.readiness){
  const ready=el('section',null,'workflow-readiness');ready.append(el('h4','Scenario readiness evidence'));
  ready.append(el('p','CORE session '+(p.readiness.session_id??'unknown')+' · '+(p.readiness.overall??'checked')+' · Checked '+(p.readiness.checked_at?new Date(p.readiness.checked_at).toLocaleString():'time unavailable'),'small'));
  for(const check of p.readiness.checks||[])ready.append(el('p',check.key+': '+check.status+(check.items==null?'':' · '+check.items+' reported items'),'small'));
  ready.append(el('p','Saved readiness evidence from the deployment, not a continuous health check.','small'));section.append(ready);
 }
 const transfer=p.transfer;
 if(transfer&&transfer.step===current?.id){
  section.append(el('p','Download from VM '+transfer.vmid+': '+transfer.file+' · '+(transfer.received_bytes??0).toLocaleString()+' / '+(transfer.total_bytes==null?'unknown':transfer.total_bytes.toLocaleString())+' bytes · '+(transfer.verified?'Complete, checksum verified':transfer.status),'small'));
 }
 const list=el('ol',null,'workflow-stages');
 for(const stage of p.steps){
  const item=el('li',null,'workflow-step '+stage.status);
  const heading=el('div',null,'result-actions');heading.append(el('strong',stage.label),badge(stage.status,stage.status==='completed'?'good':['failed','interrupted','completed_with_errors'].includes(stage.status)?'warn':''));
  item.append(heading,el('p',(stage.vmid?'VM '+stage.vmid:'Orchestrator host')+' · '+stage.description,'small'));
  if(stage.started_at){
   const timing=el('p',null,'small');timing.append(document.createTextNode('Started '+new Date(stage.started_at).toLocaleTimeString()+' · '),timer(stage.elapsed_seconds,p.observed_at,stage.active));
   if(stage.ended_at)timing.append(document.createTextNode(' · Finished '+new Date(stage.ended_at).toLocaleTimeString()));
   if(stage.attempt_count)timing.append(document.createTextNode(' · '+stage.attempt_count+' command attempt(s)'));
   item.append(timing);
  }
  if(stage.error)item.append(el('p',stage.error,'error'));
  if(stage.live_log_bytes!=null)item.append(el('p','Command output collected: '+stage.live_log_bytes+' bytes'+(stage.last_output_at?' · Last output '+new Date(stage.last_output_at).toLocaleTimeString():''),'small'));
  if(stage.log)item.append(el('p','Saved command log: '+stage.log+' (included in the run bundle)','small'));
  const details=el('details',null,'stage-details'),key=run.output+'|'+stage.id;
  details.dataset.stageKey=key;details.open=stageDetailExpansion.has(key)?stageDetailExpansion.get(key):false;
  details.addEventListener('toggle',()=>{stageDetailExpansion.set(key,details.open);if(stageDetailExpansion.size>200)stageDetailExpansion.delete(stageDetailExpansion.keys().next().value);});
  details.append(el('summary','Stage details and output'));
  const output=el('div',null,'stage-output');output.tabIndex=0;output.setAttribute('role','region');output.setAttribute('aria-label',stage.label+' details and output');
  output.addEventListener('scroll',()=>{stageDetailScroll.set(key,output.scrollTop);if(stageDetailScroll.size>200)stageDetailScroll.delete(stageDetailScroll.keys().next().value);});
  output.append(el('p',stage.operation|| (stage.status==='pending'?'Waiting for earlier stages.':'Detailed checkpoints were not recorded for this stage.'),'small'));
  if(stage.timeout_seconds!=null)output.append(el('p','Command limit: '+stage.timeout_seconds+'s'+(stage.exitcode!=null?' · Exit code: '+stage.exitcode:''),'small'));
  for(const check of stage.vm_checks||[])output.append(el('p','VM '+check.vmid+' · '+(check.ready?'Ready':'Not ready')+' · '+(check.stopped||[]).length+' recorded job(s) stopped','small'));
  const observation=stage.guest_observation;
  if(observation)output.append(el('p','Last guest response: '+new Date(observation.at).toLocaleTimeString()+' · '+observation.state+(observation.pid?' · PID '+observation.pid:''),'small'));
  for(const transfer of stage.transfers||[])output.append(el('p','VM '+transfer.vmid+' · '+transfer.file+' · '+transfer.received_bytes+' / '+(transfer.total_bytes??'unknown')+' bytes · '+(transfer.verified?'Checksum verified':transfer.status),'small'));
  if(stage.readiness)for(const check of stage.readiness.checks||[])output.append(el('p','Readiness '+check.key+': '+check.status,'small'));
  if(stage.file_count){output.append(el('p',stage.file_count+' saved file(s)'+(stage.file_count>100?' · showing first 100':''),'small'));output.append(el('pre',stage.files.join('\n')));}
  for(const trial of stage.trials||[]){output.append(el('p',trial.trial_id+' · '+trial.condition_id+' · '+trial.status+' · Verified: '+(trial.verified_success??'pending'),'small'));if(trial.transport)output.append(el('p',trial.transport.activity+' · '+(trial.transport.files_uploaded??0)+' / '+(trial.transport.files_total??'unknown')+' files transferred','small'));for(const error of trial.errors||[])output.append(el('p',error,'error'));}
  if(stage.events?.length)output.append(el('h4','Checkpoints and recent output'),el('pre',stage.events.map(event=>`${event.at} [${event.kind}] ${event.message}`).join('\n')));
  if(stage.log_tail!=null)output.append(el('h4','Saved command log · last 60 lines'),el('pre',stage.log_tail||'(No output recorded yet)'));
  details.append(output);
  requestAnimationFrame(()=>{output.scrollTop=stageDetailScroll.get(key)||0;});
  item.append(details);
  list.append(item);
 }
 section.append(list);target.append(section);
}
/* Shared visual conventions; all run content is rendered as text. */
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live, sample=false) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';if(sample)node.dataset.source='sample';} return node;}
function renderProgress(run){
 const shown=run.sample_progress?[run]:[];
 const target=$('sample-activity');
 const expanded=new Set([...target.querySelectorAll('details[open]')].map(n=>n.dataset.run));
 target.replaceChildren();renderTrialFailures(run,target);renderWorkflowProgress(run,target);if(!shown.length)target.append(el('p',run.message||'Progress is not available yet.','small'));
 for(const run of shown){
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
   if(transport?.files_total)card.append(el('p',`Inputs transferred: ${transport.files_uploaded} / ${transport.files_total} files · ${(transport.bytes_uploaded??0).toLocaleString()} / ${(transport.bytes_total??0).toLocaleString()} bytes acknowledged`,'small'));
   if(transport?.service?.SubState)card.append(el('p',`Last observed guest service: ${transport.service.SubState}${transport.service.ExecMainPID?' · PID '+transport.service.ExecMainPID:''}`,'small'));
   if(transport?.updated_at)card.append(el('p',`Guest stage last recorded: ${new Date(transport.updated_at).toLocaleTimeString()}`,'small'));
  }
  card.append(el('p',`Per-trial limits: ${p.max_turns} turns · ${p.wall_seconds}s worker budget. Live model tokens and tool calls are not streamed; scores appear after collection.`,'small'));
  if(p.trials.length){const wrap=el('div',null,'scroll'),table=el('table'),header=el('tr'),thead=el('thead'),body=el('tbody');for(const title of ['Trial','Condition','Status','Verified','Score'])header.append(el('th',title));thead.append(header);for(const trial of p.trials){const row=el('tr');for(const value of [trial.trial_id,trial.condition_id,trial.status,trial.verified_success===true?'Yes':trial.verified_success===false?'No':trial.status==='running'?'Pending':'Unavailable',trial.score??'—'])row.append(el('td',value));body.append(row);}table.append(thead,body);wrap.append(table);card.append(wrap);}
  for(const trial of p.trials)for(const error of trial.errors||[])card.append(el('p',`${trial.trial_id} · ${error}`,'error'));
  if(run.error)card.append(el('p',run.error,'error'));
  const details=el('details'),summary=el('summary','Recent run events');details.dataset.run=run.output;details.open=expanded.has(run.output);details.append(summary,el('pre',p.events.slice(-100).map(e=>`${e.at} · ${e.message}`).join('\n')));card.append(details);if(trial&&run.workflow_progress)target.querySelector('.workflow-progress').insertBefore(card,target.querySelector('.workflow-vms'));else target.append(card);
 }
}
function renderResultSummary(data){
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
 const table=el('table'),head=el('tr');for(const text of ['Condition','Verified successes','Unassisted successes','Hint-assisted successes','Solution-assisted successes','Hints released','Solutions provided','Facts revealed','Mean runtime','First flag'])head.append(el('th',text));const heading=el('thead');heading.append(head);table.append(heading);const body=el('tbody');
 for(const [name,summary] of groups){const row=el('tr');row.append(el('td',name),el('td',`${summary.verified_successes??0} / ${summary.verified_trials??0}`),el('td',summary.unassisted_successes??'—'),el('td',summary.hints_assisted_successes??summary.assisted_successes??'—'),el('td',summary.solution_assisted_successes??0),el('td',summary.hints_released??0),el('td',summary.solutions_released??0),el('td',summary.facts_revealed??0),el('td',duration(summary.mean_execution_seconds)),el('td',duration(summary.mean_time_to_first_flag_seconds)));body.append(row);}table.append(body);const wrapper=el('div',null,'scroll');wrapper.append(table);target.append(wrapper);
}

function renderTrialFailures(run,target,providedDiagnostics){
 const failures=run.trial_failures||[],diagnostics=providedDiagnostics||run.failure_diagnostics||[];
 if(!failures.length&&!diagnostics.length&&run.recorded_status!=='completed_with_errors')return;
 const section=el('section',null,'panel failure-summary');section.setAttribute('aria-label','Experiment errors');
 section.append(el('h3','Experiment errors'),el('p',failures.length?`${failures.length} trial(s) ended with an execution error. Deployment may still have succeeded.`:'This experiment completed with errors. Inspect the trial records and collected worker logs below.','error'));
 for(const trial of failures){
  section.append(el('h4',`${trial.trial_id} · attempt ${trial.attempt} · ${trial.status}`),el('p',`Condition: ${trial.condition_id||'unknown'} · Task: ${trial.task_id||'unknown'}`,'small'));
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
