/* Saved inputs only: never substitute today's model defaults or source files. */
let renderedConfiguration=null;
const scenarioPreviews=new Map();
function artifactURL(id){return '/api/runs/'+encodeURIComponent(runId)+'/artifact?id='+encodeURIComponent(id);}
function configurationDetails(title,value,key){
 const details=el('details',null,'configuration-details');details.dataset.configKey=key;
 details.append(el('summary',title),el('pre',typeof value==='string'?value:JSON.stringify(value,null,2)));
 return details;
}
function artifactLink(item){
 const link=el('a',item.name);link.href=artifactURL(item.id);link.download='';
 link.addEventListener('click',async event=>{
  event.preventDefault();const key='download-'+item.id;
  loadingModal.set(key,true,'Preparing '+item.name+'…');
  try{
   const response=await fetch(link.href,{cache:'no-store'});
   if(!response.ok)throw Error('Download unavailable (HTTP '+response.status+'). Wait for an active run to finish, or refresh to retry.');
   const blob=await response.blob(),url=URL.createObjectURL(blob),download=el('a');
   download.href=url;download.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||item.name;
   download.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
  }catch(error){$('run-error').hidden=false;$('run-error').textContent=error.message;}
  finally{loadingModal.set(key,false);}
 });
 return link;
}
function renderRunConfiguration(config){
 const target=$('run-configuration');
 if(!config){target.replaceChildren(el('p','Saved run configuration is unavailable.'));renderedConfiguration=null;return;}
 const encoded=JSON.stringify(config);
 if(encoded===renderedConfiguration)return;
 const opened=new Set([...target.querySelectorAll('details[open]')].map(n=>n.dataset.configKey));
 renderedConfiguration=encoded;target.replaceChildren();
 target.append(el('h2','Task and run configuration'),el('p','Captured inputs used for this run.','small'));
 if(!(config.tasks||[]).length)target.append(el('p','Task prompts have not been captured yet. They will appear when the study is prepared.','small'));
 for(const task of config.tasks||[]){
  const section=el('section',null,'run-task');
  section.append(el('h3',task.id),el('p',[task.family,task.scenario_id,task.split].filter(Boolean).join(' · '),'small'));
  section.append(el('h4','Exact task prompt'),el('pre',task.prompt,'task-prompt'));target.append(section);
 }
 const settings=el('dl',null,'run-settings');
 for(const [label,value] of [
  ['Model',config.model?.name],['Provider',config.model?.provider],['Endpoint',config.model?.url],
  ['Repetitions',config.repetitions],['Order seed',config.order_seed],
  ['Progressive hints',config.execution?.provide_progressive_hints?'On · up to 3 hints, then current-challenge solution':'Off'],['Max tries before solution',config.execution?.max_tries_before_solution??6],['Maximum turns',config.execution?.max_turns],['Worker budget',config.execution?.wall_seconds==null?null:config.execution.wall_seconds+' seconds'],
  ['Tool timeout',config.execution?.tool_timeout==null?null:config.execution.tool_timeout+' seconds'],
  ['Context window',config.execution?.context_window]]){
  const row=el('div');row.append(el('dt',label),el('dd',value??'Not recorded'));settings.append(row);
 }
 target.append(settings);if(config.judge?.enabled)target.append(configurationDetails('Judge LLM settings',config.judge,'judge-config'));target.append(el('h3','Tools and guidance'));
 for(const condition of config.conditions||[]){
  const section=el('section',null,'run-condition');section.append(el('h4',condition.id),
   el('p',condition.tools?.length?condition.tools.join(', '):'No tools'));
  if(condition.catalog_snapshot)section.append(configurationDetails('Exact tool catalog',condition.catalog_snapshot,'catalog-'+condition.id));
  if(condition.guidance_snapshot?.length)section.append(configurationDetails('Additional guidance',condition.guidance_snapshot,'guidance-'+condition.id));
  target.append(section);
 }
 target.append(el('h3','System prompts'),el('p',config.system_prompt_note,'small'));
 for(const [index,prompt] of (config.system_prompts||[]).entries())target.append(configurationDetails('Captured system prompt '+(index+1),prompt.text,'system-'+index));
 target.append(configurationDetails('All saved settings and provenance',{
  source:config.source,model:config.model,engine:config.engine,backend:config.backend,execution:config.execution,
  judge:config.judge,repetitions:config.repetitions,order_seed:config.order_seed,schedule:config.schedule,
  provenance:config.provenance,workflow:config.workflow},'settings'));
 const scenario=config.scenarioforge||{};
 target.append(el('h3','ScenarioForge'),el('p',scenario.message));
 if(scenario.used){
  target.append(configurationDetails('Scenario information and readiness',scenario.metadata?{...scenario.metadata,readiness:scenario.readiness,package_hash:scenario.package_hash}:{},'scenario'));
  if(scenario.reproduction){
   const bundled=(scenario.reproduction.artifact_sources||[]).filter(s=>s.bundled).length;
   const missing=(scenario.reproduction.artifact_sources||[]).filter(s=>!s.bundled);
   target.append(el('p','Re-import package: '+scenario.reproduction.fidelity+' · '+bundled+' artifact sources included · '+missing.length+' not included.'));
   if(scenario.reproduction_source==='saved-xml')target.append(el('p','This package restores the saved scenario definition. Generated artifacts and external images must be regenerated or supplied before deployment.','small'));
   target.append(configurationDetails('Re-import package contents and missing artifacts',scenario.reproduction,'reproduction'));
  }
  if(scenario.xml_available){
   const details=el('details',null,'configuration-details');details.dataset.configKey='xml';
   const pre=el('pre',scenarioPreviews.get('xml')||'Expand to load the saved XML. Large files show a preview; the download contains the complete XML.');
   details.append(el('summary','View scenario XML'),pre);
   details.addEventListener('toggle',async()=>{
    if(!details.open||scenarioPreviews.has('xml')||details.dataset.loading)return;
    details.dataset.loading='yes';loadingModal.set('xml',true,'Loading scenario XML…');
    try{
     const response=await fetch(artifactURL('scenario-xml'),{cache:'no-store'});
     if(!response.ok)throw Error('Unable to load scenario XML (HTTP '+response.status+').');
     const text=await response.text(),preview=text.length>131072?text.slice(0,131072)+'\n\n[Preview truncated. Download Scenario XML for the complete file.]':text;
     scenarioPreviews.set('xml',preview);pre.textContent=preview;
    }catch(error){pre.textContent=error.message;}
    finally{delete details.dataset.loading;loadingModal.set('xml',false);}
   });
   target.append(details);
  }
 }
 target.append(el('h3','Saved files and downloads'),el('p',config.capture_note,'small'));
 const downloads=el('ul',null,'run-downloads');
 for(const item of config.downloads||[]){const row=el('li');row.append(artifactLink(item),el('p',item.description,'small'));downloads.append(row);}
 target.append(downloads);
 const inventory=el('details',null,'configuration-details');inventory.dataset.configKey='files';
 inventory.append(el('summary','Individual saved files ('+(config.files||[]).length+')'));
 const list=el('ul',null,'artifact-list');
 for(const item of config.files||[]){const row=el('li');row.append(artifactLink({...item,name:item.path}),el('span',' · '+item.bytes.toLocaleString()+' bytes','small'));list.append(row);}
 inventory.append(list);target.append(inventory);
 for(const details of target.querySelectorAll('details'))details.open=opened.has(details.dataset.configKey);
}
