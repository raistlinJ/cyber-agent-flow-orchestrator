/* One loading dialog per window, shared by overlapping operations. */
const loadingModal=(()=>{
 const dialog=document.createElement('dialog');
 dialog.id='loading-modal';dialog.className='loading-modal';
 dialog.setAttribute('aria-labelledby','loading-modal-title');
 dialog.setAttribute('aria-describedby','loading-modal-message');
 const title=document.createElement('h2');title.id='loading-modal-title';title.textContent='Loading';
 const message=document.createElement('p');message.id='loading-modal-message';message.setAttribute('role','status');
 const progress=document.createElement('progress');progress.setAttribute('aria-label','Loading data');
 const dismiss=document.createElement('button');dismiss.type='button';dismiss.textContent='Keep running in queue';dismiss.id='dismiss-loading';dismiss.hidden=true;
 dialog.append(title,message,progress,dismiss);document.body.append(dialog);
 const pending=new Map(),dismissed=new Set();let currentKey=null;
 function close(){if(currentKey&&pending.get(currentKey)?.dismissible){dismissed.add(currentKey);dialog.close();}}
 dismiss.addEventListener('click',close);
 dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
 return {set(key,active,label='Loading data…',percent=null,dismissible=false){
  if(active)pending.set(key,{label,percent,dismissible});else{pending.delete(key);dismissed.delete(key);}
  const visible=[...pending.entries()].filter(([key])=>!dismissed.has(key));
  if(!visible.length){if(dialog.open)dialog.close();return;}
  const [selected,current]=visible.at(-1);currentKey=selected;dismiss.hidden=!current.dismissible;
  message.textContent=current.label;
  if(current.percent==null)progress.removeAttribute('value');else{progress.max=100;progress.value=current.percent;}
  if(!dialog.open)dialog.showModal();
 }};
})();
