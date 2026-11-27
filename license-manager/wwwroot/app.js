function renderAgreement(text){
 const target=document.getElementById('eula-text');target.replaceChildren();
 const lines=text.replace(/\r\n/g,'\n').split('\n');let paragraph=[];
 function add(tag,content,cls){const node=document.createElement(tag);node.textContent=content;if(cls)node.className=cls;target.append(node);}
 function flush(){if(paragraph.length){add('p',paragraph.join(' '),/^\([a-z]\)/.test(paragraph[0])?'clause':'');paragraph=[];}}
 lines.forEach((raw,i)=>{const line=raw.trim();if(i===0){add('h3',line,'doc-title');return;}if(i<4){add('p',line,'doc-meta');return;}if(!line){flush();return;}if(/^\d+(?:\.\d+)?\.?\s+[A-Z]/.test(line)){flush();add('h3',line);return;}if(line==='END OF AGREEMENT'){flush();add('p',line,'end');return;}if(/^\([a-z]\)/.test(line))flush();paragraph.push(line);});flush();
}
const form=document.getElementById('form'),msg=document.getElementById('message'),button=document.getElementById('register'),accepted=document.getElementById('eula-accepted');
let eula=null,submitting=false;
function refresh(){button.disabled=submitting||!eula||!accepted.checked;}
accepted.addEventListener('change',()=>{refresh();msg.textContent='';});
async function loadEula(){
 eula=null;accepted.checked=false;accepted.disabled=true;refresh();
 try{
  const response=await fetch('/api/eula?fresh='+Date.now(),{cache:'no-store'});
  if(!response.ok)throw Error('EULA unavailable');
  const data=await response.json();
  if(!data.text||!data.version||!data.sha256)throw Error('Incomplete EULA');
  eula=data;renderAgreement(data.text);
  document.getElementById('eula-version').textContent='Version '+data.version;
  accepted.disabled=false;accepted.checked=false;refresh();
 }catch{msg.textContent='The license agreement could not be loaded. Reload this page to try again.';}
}
form.addEventListener('submit',async event=>{
 event.preventDefault();
 if(!eula||!accepted.checked||submitting)return;
 submitting=true;refresh();msg.textContent='Submitting…';
 try{
  const response=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value.trim(),edition:'DeveloperPreview',eulaAccepted:accepted.checked,eulaVersion:eula.version,eulaSha256:eula.sha256})});
  const raw=await response.text();let body={};try{body=JSON.parse(raw);}catch{}
  if(response.status===409&&body.code==='eula_changed'){
   await loadEula();
   if(eula)msg.textContent='The agreement has been refreshed. Please review and accept it again.';
   return;
  }
  msg.textContent=response.ok?(body.message||'Your request has been accepted.'):(body.error||'Registration failed (HTTP '+response.status+'). Please try again.');
 }catch{msg.textContent='Network error: could not contact the registration service. Please check your connection.';}
 finally{submitting=false;refresh();}
});
loadEula();
