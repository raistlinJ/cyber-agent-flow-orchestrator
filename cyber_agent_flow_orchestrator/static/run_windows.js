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
