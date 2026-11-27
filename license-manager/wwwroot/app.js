const form=document.getElementById('form'),msg=document.getElementById('message'),button=document.getElementById('register'),accepted=document.getElementById('eula-accepted');
let eula=null,submitting=false;
function refresh(){button.disabled=submitting||!eula||!accepted.checked;}
accepted.addEventListener('change',refresh);
async function loadEula(){
 try{
  const response=await fetch('/api/eula',{cache:'no-store'});
  if(!response.ok)throw Error('EULA unavailable');
  const data=await response.json();
  if(!data.text||!data.version||!data.sha256)throw Error('Incomplete EULA');
  eula=data;document.getElementById('eula-text').textContent=data.text;
  document.getElementById('eula-version').textContent='Version '+data.version;
  accepted.disabled=false;accepted.checked=false;refresh();
 }catch{msg.textContent='The license agreement could not be loaded. Reload this page to try again.';}
}
form.addEventListener('submit',async event=>{
 event.preventDefault();
 if(!eula||!accepted.checked||submitting)return;
 submitting=true;refresh();msg.textContent='Submitting…';
 try{
  const response=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value.trim(),edition:'DeveloperPreview',eulaAccepted:true,eulaVersion:eula.version,eulaSha256:eula.sha256})});
  const raw=await response.text();let body={};try{body=JSON.parse(raw);}catch{}
  msg.textContent=response.ok?(body.message||'Your request has been accepted.'):(body.error||'Registration failed (HTTP '+response.status+'). Please try again.');
 }catch{msg.textContent='Network error: could not contact the registration service. Please check your connection.';}
 finally{submitting=false;refresh();}
});
loadEula();
