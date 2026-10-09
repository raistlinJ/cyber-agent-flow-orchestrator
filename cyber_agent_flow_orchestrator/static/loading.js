/* One loading dialog per window, shared by overlapping operations. */
const loadingModal=(()=>{
 const dialog=document.createElement('dialog');
 dialog.id='loading-modal';dialog.className='loading-modal';
 dialog.setAttribute('aria-labelledby','loading-modal-title');
 dialog.setAttribute('aria-describedby','loading-modal-message');
 const title=document.createElement('h2');title.id='loading-modal-title';title.textContent='Loading';
 const message=document.createElement('p');message.id='loading-modal-message';message.setAttribute('role','status');
 const progress=document.createElement('progress');progress.setAttribute('aria-label','Loading data');
 dialog.append(title,message,progress);document.body.append(dialog);
 dialog.addEventListener('cancel',event=>event.preventDefault());
 const pending=new Map();
 return {set(key,active,label='Loading data…',percent=null){
  if(active)pending.set(key,{label,percent});else pending.delete(key);
  if(!pending.size){if(dialog.open)dialog.close();return;}
  const current=[...pending.values()].at(-1);
  message.textContent=current.label;
  if(current.percent==null)progress.removeAttribute('value');else{progress.max=100;progress.value=current.percent;}
  if(!dialog.open)dialog.showModal();
 }};
})();
