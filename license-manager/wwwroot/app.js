const form=document.getElementById('form'),msg=document.getElementById('message'),button=document.getElementById('register');
form.addEventListener('submit',async event=>{
 event.preventDefault();
 button.disabled=true;msg.textContent='Submitting…';
 try {
  const response=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value.trim()})});
  const body=await response.json();
  msg.textContent=response.ok?body.message:(body.error||'Registration could not be completed.');
 } catch {msg.textContent='Could not reach the registration service. Try again later.';}
 finally {button.disabled=false;}
});