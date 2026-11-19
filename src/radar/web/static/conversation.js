let conversationPoll;
let conversationLoadGeneration=0;
async function loadObjectConversation(id) {
  if (state.selected?.id !== id || !$('#detail').open) return;
  const generation=++conversationLoadGeneration;
  clearTimeout(conversationPoll);
  let box = $('#object-conversation');
  if (!box) {
    box = document.createElement('section'); box.id = 'object-conversation'; box.className = 'section';
    box.innerHTML = `<h3>Обсудить объект с аналитиком</h3><p class="caption">Факты — из материалов этого ЖК, предложения — с учётом методов Мегаполиса. Переписка сохраняется.</p><div id="conversation-history" aria-live="polite"></div><form id="conversation-form"><label class="field">Ваш вопрос<textarea name="question" required minlength="3" maxlength="2000" rows="3" placeholder="Почему перенесли сроки? Что предложить застройщику? Как построить диалог с дольщиками?"></textarea></label><label class="check-line"><input name="search" type="checkbox"> Дополнительно поискать в интернете · до 2 поисковых запросов</label><button class="button primary" type="submit">Задать вопрос</button><span id="conversation-status" class="caption"></span></form>`;
    $('#analyst-tools').before(box);
    box.querySelector('form').addEventListener('submit', async e => {
      e.preventDefault(); e.stopPropagation();
      const form=e.currentTarget, button=form.querySelector('button');button.disabled=true;
      const question=form.elements.question.value, search=form.elements.search.checked;
      const requestKey=JSON.stringify([question,search]);
      if(form.dataset.requestKey!==requestKey){form.dataset.requestId=crypto.randomUUID();form.dataset.requestKey=requestKey;}
      const requestId=form.dataset.requestId;
      try {
        await api('/records/'+id+'/conversation',{method:'POST',body:JSON.stringify({question,search,request_id:requestId})});
        if(form.elements.question.value===question)form.elements.question.value='';
        delete form.dataset.requestId;delete form.dataset.requestKey;
        await loadObjectConversation(id);
      } catch(err) {
        // A lost response may hide an accepted question: retry the same request ID.
        if(err.status && err.status<500){delete form.dataset.requestId;delete form.dataset.requestKey;}
        if(state.selected?.id===id && $('#detail').open && $('#object-conversation')===box)box.querySelector('#conversation-status').textContent=err.message;
        button.disabled=false;
      }
    });
  }
  try {
    const turns=await api('/records/'+id+'/conversation');
    if (generation!==conversationLoadGeneration || state.selected?.id!==id || !$('#detail').open || $('#object-conversation')!==box) return;
    const pending=turns.some(t=>t.status==='pending');
    const signature=turns.map(t=>t.id+':'+t.status).join('|');
    const historyPane=$('#conversation-history');const changed=historyPane.dataset.signature!==signature;
    historyPane.innerHTML=turns.map(t=>`<article class="conversation-turn"><p class="conversation-question">${esc(t.question)}</p>${t.status==='pending'?'<p class="caption">Аналитик готовит ответ…</p>':t.status==='failed'?`<p class="caption">${esc(t.error)}</p>`:(t.answer?.sections||[]).map(s=>`<div class="conversation-answer"><strong>${esc(s.title)}</strong>${s.kind==='proposal'?'<span class="tag">Предлагаемое действие</span>':s.kind==='fact'?'<span class="tag">По собранным источникам</span>':''}<p>${esc(s.text)}</p><div class="conversation-links">${s.source_ids.map(i=>t.answer.sources.find(x=>x.id===i)).filter(Boolean).filter((source,index,items)=>items.findIndex(x=>x.url===source.url)===index).map(source=>`<a href="${safeURL(source.url)}" target="_blank" rel="noopener noreferrer">${esc(host(source.url))}${source.kind==='snippet'?' · фрагмент поиска':''} ↗</a>`).join(' ')}</div></div>`).join('')}${t.answer?`<p class="caption">Использовано материалов: ${t.answer.source_count} из ${t.answer.available_sources}. ${t.answer.queries.length?'Выполнен дополнительный поиск.':''}</p>`:''}</article>`).join('');
    historyPane.dataset.signature=signature;
    if(changed)historyPane.scrollTop=historyPane.scrollHeight;
    $('#conversation-form button').disabled=pending;
    $('#conversation-status').textContent=pending?' Можно закрыть карточку — ответ сохранится.':'';
    if (pending) conversationPoll=setTimeout(()=>loadObjectConversation(id),2500);
  } catch(err) {if(generation===conversationLoadGeneration && state.selected?.id===id && $('#detail').open && $('#object-conversation')===box)box.querySelector('#conversation-status').textContent=err.message;}
}
