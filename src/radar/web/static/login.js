document.querySelector('#login-form').addEventListener('submit',async event=>{
 event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;
 try{const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-Radar-Local':'1'},body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});const result=await response.json();if(!response.ok)throw Error(result.detail||'Не удалось войти');location.reload();}
 catch(error){document.querySelector('#login-status').textContent=error.message;event.target.elements.password.value='';}finally{button.disabled=false;}
});
