/* API gateways can fail with text or HTML; never expose a JSON parser error. */
async function dashboardJSON(response){
 let body;
 try{body=JSON.parse(await response.text());}
 catch{
  const messages={
   502:'Dashboard unavailable. Refresh to check the current state before retrying.',
   503:'Dashboard service is unavailable. Try refreshing shortly.',
   504:'Dashboard request timed out. Refresh to check the current state before retrying.'
  };
  throw Error((messages[response.status]||'Dashboard returned an unexpected response.')+' (HTTP '+response.status+')');
 }
 return body;
}
