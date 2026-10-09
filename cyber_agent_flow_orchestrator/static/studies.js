let studyPoll=null,renderedStudies=null,renderedStudyMembers=null;
function renderStudyMembers(data){
 const serialized=JSON.stringify((data.runs||[]).map(r=>[r.output,r.recorded_status]));if(serialized===renderedStudyMembers)return;renderedStudyMembers=serialized;
 const references=$('study-references'),referenceSelected=new Set([...references.querySelectorAll('input:checked')].map(i=>i.value));references.replaceChildren();
 const target=$('study-members'),selected=new Set([...target.querySelectorAll('input:checked')].map(i=>i.value));target.replaceChildren();
 for(const run of data.runs||[]){if(!run.scenario_experiment)continue;const id=run.output?.split('/').pop()||run.workflow_id;const label=el('label',null,'study-member'),check=el('input');check.type='checkbox';check.value=id;check.checked=selected.has(id);label.append(check,el('span',(run.scenario_experiment.scenario||id)+' · '+id+' · '+run.recorded_status));target.append(label);if(['completed','completed_with_errors'].includes(run.recorded_status)){const copy=label.cloneNode(true),ref=copy.querySelector('input');ref.checked=referenceSelected.has(id);references.append(copy);}}
}
async function studyRequest(action,data){const response=await apiFetch('/api/studies/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(data)});const value=await dashboardJSON(response);if(!response.ok)throw Error(value.error||'Study request failed');return value;}
async function refreshStudies(background=false){
 clearTimeout(studyPoll);if(!background)loadingModal.set('studies',true,'Loading study status…');
 try{const value=await studyRequest('list',{}),target=$('studies-list');const serialized=JSON.stringify(value);if(serialized===renderedStudies){if(value.items.some(s=>s.status==='running')&&location.hash==='#studies')studyPoll=setTimeout(()=>refreshStudies(true),5000);return;}renderedStudies=serialized;target.replaceChildren();let active=false;
  for(const study of value.items){const card=el('section',null,'experiment-settings');card.append(el('h3',study.name),el('p',study.status+' · '+study.message));const actions=el('div',null,'result-actions');
   const button=(text,action)=>{const b=el('button',text);b.type='button';b.addEventListener('click',async()=>{loadingModal.set('study-action',true,text+'…');try{const result=await studyRequest(action,{study_id:study.id});if(action==='summary')$('study-message').textContent=result.evaluable_trials+' evaluable / '+result.planned_trials+' planned trials';await refreshStudies();}catch(e){$('study-message').textContent=e.message;}finally{loadingModal.set('study-action',false);}});actions.append(b);return b;};
   if(study.status==='running'){active=true;button('Stop study','stop');}else{button('Run study','run');button('Build comparison report','summary');}
   for(const [name,label] of [['study-summary.html','Open formatted report'],['study-summary.md','Download Markdown'],['study-charts.svg','Download chart'],['study-summary.json','Download JSON'],['study-bundle.zip','Download complete study bundle']]){const a=el('a',label);a.href='/api/studies/'+study.id+'/artifact?name='+encodeURIComponent(name);if(name.endsWith('.html')){a.target='_blank';a.rel='noopener';}else a.download=name;actions.append(a);}
   card.append(actions);target.append(card);
  }
  if(active&&location.hash==='#studies')studyPoll=setTimeout(()=>refreshStudies(true),5000);
 }catch(error){$('study-message').textContent=error.message;}finally{loadingModal.set('studies',false);}
}
$('reload-studies').addEventListener('click',()=>refreshStudies());
$('create-study').addEventListener('click',async()=>{loadingModal.set('study-create',true,'Validating study members and saving the study…');try{await studyRequest('create',{name:$('study-name').value,baseline:$('study-baseline').value,run_ids:[...$('study-members').querySelectorAll('input:checked')].map(i=>i.value),reference_run_ids:[...$('study-references').querySelectorAll('input:checked')].map(i=>i.value),require_references:$('study-require-references').checked});await refreshStudies();}catch(error){$('study-message').textContent=error.message;}finally{loadingModal.set('study-create',false);}});
window.addEventListener('hashchange',()=>{if(location.hash==='#studies')refreshStudies();else clearTimeout(studyPoll);});
if(location.hash==='#studies')refreshStudies();
