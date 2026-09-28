/* Shared visual conventions; all run content is rendered as text. */
function duration(seconds) {if (seconds == null || !Number.isFinite(Number(seconds))) return 'Unknown'; seconds=Math.max(0, Math.floor(seconds)); const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), s=seconds%60; return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;}
function badge(text, tone='') {return el('span', text, 'badge '+tone);}
function timer(seconds, observed, live, sample=false) {const node=el('span',duration(seconds),'clock'); if(seconds!=null){node.dataset.seconds=seconds;node.dataset.observed=observed||'';node.dataset.live=live?'yes':'no';if(sample)node.dataset.source='sample';} return node;}
function renderProgress(run){
 const shown=run.sample_progress?[run]:[];
 const target=$('sample-activity');
 const expanded=new Set([...target.querySelectorAll('details[open]')].map(n=>n.dataset.run));
 target.replaceChildren();if(!shown.length)target.append(el('p',run.message||'Progress is not available yet.','small'));
 for(const run of shown){
  const p=run.sample_progress,trial=p.current_trial,transport=trial?.transport;
  const card=el('article',null,'panel sample-activity'),head=el('div',null,'result-actions');head.append(el('h3',p.name),badge(run.recorded_status,run.recorded_status==='completed'?'good':['failed','interrupted','completed_with_errors'].includes(run.recorded_status)?'warn':''));
  const button=el('button','Open results');button.type='button';button.addEventListener('click',()=>showResults(run.output.split('/').pop()));head.append(button);card.append(head);
  card.append(el('p',run.recorded_status==='interrupted'?'Coordinator is no longer active. Inspect results before retrying.':run.recorded_status==='stopping'?run.message:transport?.activity||run.message||p.phase,'sample-stage'));
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
  for(const trial of p.trials)for(const error of trial.errors||[])card.append(el('p',`${trial.trial_id} · ${error}`,'error'));
  if(run.error)card.append(el('p',run.error,'error'));
  const details=el('details'),summary=el('summary','Recent sample events');details.dataset.run=run.output;details.open=expanded.has(run.output);details.append(summary,el('pre',p.events.slice(-12).map(e=>`${e.at} · ${e.message}`).join('\n')));card.append(details);target.append(card);
 }
}
function renderResultSummary(data){
 const target=$('result-summary');target.replaceChildren();
 const workflow=data.workflow||{};target.append(el('p',`${workflow.recorded_status||'Unknown'} · ${workflow.message||''}`));
 if(workflow.error)target.append(el('p',workflow.error,'error'));
 if(!data.evaluation){target.append(el('p','Trial results will appear here once evaluation starts.','small'));return;}
 for(const trial of data.evaluation.attempts||[])for(const error of trial.errors||[])target.append(el('p',`${trial.trial_id} · ${trial.status}: ${error}`,'error'));
 for(const item of data.failure_diagnostics||[]){
  const section=el('section',null,'failure-diagnostics');section.append(el('h3',`${item.trial_id} · attempt ${item.attempt} · Failure details`));
  section.append(el('p','Saved on the orchestrator host. Log tails are limited to 80 lines; common credential fields are redacted.','small'));
  for(const record of item.model_errors||[])section.append(el('p',record.error,'error'));
  for(const log of item.logs||[]){const details=el('details');details.append(el('summary','Collected worker log'),el('div',log.path,'small'),el('pre',log.text||'(Log is empty)'));section.append(details);}
  for(const note of item.notes||[])section.append(el('p',note,'small'));
  target.append(section);
 }

 const groups=Object.entries(data.evaluation.conditions||{});
 const table=el('table'),head=el('tr');for(const text of ['Condition','Verified successes','Mean runtime','First flag'])head.append(el('th',text));const heading=el('thead');heading.append(head);table.append(heading);const body=el('tbody');
 for(const [name,summary] of groups){const row=el('tr');row.append(el('td',name),el('td',`${summary.verified_successes??0} / ${summary.verified_trials??0}`),el('td',duration(summary.mean_execution_seconds)),el('td',duration(summary.mean_time_to_first_flag_seconds)));body.append(row);}table.append(body);const wrapper=el('div',null,'scroll');wrapper.append(table);target.append(wrapper);
}
