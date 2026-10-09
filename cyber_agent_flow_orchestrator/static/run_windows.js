/* Open directly from a click so browsers can permit a separate window. */
function runWindowURL(view,id){return `/run?view=${encodeURIComponent(view)}&run=${encodeURIComponent(id)}`;}
function openRunWindow(view,id){
 const url=runWindowURL(view,id),name=`caf_${view}_${id.replace(/[^a-zA-Z0-9_-]/g,'_')}`;
 const opened=window.open(url,name,'popup,width=1100,height=850,resizable=yes,scrollbars=yes');
 const notice=document.getElementById('window-notice');
 if(opened){opened.focus();if(notice)notice.hidden=true;}
 else if(notice){notice.replaceChildren(document.createTextNode('Your browser blocked the popup. Allow popups for this site, or '));const link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noopener';link.textContent=`open ${view} in a new tab`;notice.append(link);notice.hidden=false;}
 return opened;
}
function agentIsExecuting(run){const phase=run.sample_progress?.current_trial?.transport?.phase;return phase==='executing'||(!phase&&run.sample_progress?.current_trial?.status==='running');}
function agentTranscriptButton(run,backend='proxmox'){
 const button=document.createElement('button');button.type='button';button.className='agent-transcript'+(agentIsExecuting(run)?' is-executing':'');
 const id=run.output.split('/').pop();button.dataset.transcriptRun=id;button.title='Open live agent transcript';button.setAttribute('aria-label','Open live transcript for '+id);
 const dot=document.createElement('span');dot.className='agent-pulse';dot.setAttribute('aria-hidden','true');const label=document.createElement('span');label.textContent=backend==='fusion'?'VMware Tools agent':'QEMU guest agent';button.append(dot,label);return button;
}
document.addEventListener('click',event=>{const button=event.target.closest('[data-transcript-run]');if(button&&!button.disabled)openRunWindow('transcript',button.dataset.transcriptRun);});
