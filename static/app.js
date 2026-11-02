'use strict';
document.querySelectorAll('form[method="post"]').forEach(form=>{
  form.addEventListener('submit',event=>{
    if(form.dataset.confirm && !window.confirm(form.dataset.confirm)){event.preventDefault();return;}
    form.querySelectorAll('button').forEach(button=>{button.disabled=true;button.textContent='Выполняется…';});
  });
});
window.addEventListener('pageshow',()=>document.querySelectorAll('button:disabled').forEach(button=>{button.disabled=false;button.textContent='Повторить';}));
