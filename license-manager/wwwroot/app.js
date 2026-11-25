const form=document.getElementById('form'),msg=document.getElementById('message'),button=document.getElementById('register');
form.addEventListener('submit',async event=>{
 event.preventDefault();
 button.disabled=true;msg.textContent='Submitting…';
 try {
  const response=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value.trim()})});
  const raw=await response.text();
  let body={};
  try { body=JSON.parse(raw); } catch {}
  if (response.ok) {
   msg.textContent=body.message||'Your request has been accepted.';
  } else if (response.status===429) {
   msg.textContent=body.error||'Too many attempts. Please try again in 15 minutes.';
  } else {
   msg.textContent=body.error||'Registration failed (HTTP '+response.status+'). Please try again.';
  }
 } catch {
  msg.textContent='Network error: could not contact the registration service. Please check your connection.';
 } finally {button.disabled=false;}
});