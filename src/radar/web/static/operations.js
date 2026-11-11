/* Additional workflows stay inside Connections and object cards. */
let benchmarkTimer = null;
let benchmarkRun = null;
const opLabels = {waiting:'Поиск выполняется',ready:'Готов к доставке',sent:'Отправлен',local:'Сохранён в приложении',uncertain:'Доставку нужно проверить',delivery_blocked:'Нет подключения бота'};
async function loadTelegramLinks(id) {
  const rows = await api('/records/'+id+'/telegram');
  $('#telegram-links').innerHTML = `<form id="ops-chat-link"><label class="field">Ссылка на открытый чат или канал<input name="url" type="url" placeholder="https://t.me/имя_чата" required></label><button class="button" type="submit">Добавить ссылку</button></form><div class="linked-chats">${rows.map(r=>`<div class="linked-chat"><a href="${safeURL(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title||r.url)}</a><p class="caption">${r.synced_at?'Прочитан: '+dateText(r.synced_at):'Сообщения ещё не прочитаны'}${r.status==='partial'?' · Есть ещё сообщения, нажмите «Прочитать» повторно':''}</p><button class="button" data-op="sync-chat" data-url="${esc(r.url)}">Прочитать</button> <button class="button quiet" data-op="remove-chat" data-url="${esc(r.url)}">Убрать ссылку</button></div>`).join('')}</div>`;
}
async function loadOperations() {
  const [tg,ops,bench,last] = await Promise.all([api('/telegram/status'),api('/operations'),api('/benchmark/config'),api('/benchmark/latest')]);
  const cfg=ops.settings;
  const days=['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'];
  $('#operations-content').innerHTML = `
  <details class="operations-section"><summary>Вход в Telegram</summary>
    <p>${tg.session_saved?'Сессия сохранена. Доступ проверяется при чтении чата.':'Сохраните API ID и API Hash выше, затем войдите с отдельного рабочего аккаунта.'}</p>
    <p class="caption">Чтение только открытых ссылок, добавленных в карточки. Приложение не вступает в группы и не отправляет сообщения от вашего аккаунта. При первом чтении — последние 30 дней; далее только новые сообщения.</p>
    <form id="ops-tg-login"><label class="field">Номер телефона<input name="phone" type="tel" placeholder="+79991234567" required pattern="[+][1-9][0-9]{7,14}"></label><button class="button" type="submit">Получить код входа</button></form>
    <form id="ops-tg-confirm"><div class="form-grid"><label class="field">Код из Telegram<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8"></label><label class="field">Пароль двухэтапной проверки, если запрошен<input name="password" type="password" autocomplete="off"></label></div><button class="button" type="submit">Завершить вход</button></form>
    <button class="button quiet" data-op="tg-disconnect">Отменить вход / забыть сессию на этом компьютере</button><p class="caption">Удалённую сессию можно отозвать в Telegram → Устройства. Импортированные сообщения останутся в карточках.</p><div id="tg-status" role="status"></div>
  </details>
  <details class="operations-section"><summary>Раз в неделю и лимиты запросов</summary>
    <p>Расписание работает, пока приложение запущено и компьютер не спит. Пропущенный срок обрабатывается при следующем запуске, без серии догоняющих прогонов.</p>
    <form id="ops-weekly"><label class="check-line"><input name="enabled" type="checkbox" ${cfg.enabled?'checked':''}> Запускать поиск каждую неделю</label>
    <div class="form-grid"><label class="field">День<select name="weekday">${days.map((d,i)=>`<option value="${i}" ${cfg.weekday===i?'selected':''}>${d}</option>`).join('')}</select></label><label class="field">Час по Москве<input name="hour" type="number" min="0" max="23" value="${cfg.hour}"></label></div>
    <label class="field">География<select name="region">${[['capitals','Москва, Санкт-Петербург и области'],['moscow','Москва и область'],['petersburg','Санкт-Петербург и область'],['russia','Вся Россия']].map(([v,t])=>`<option value="${v}" ${cfg.region===v?'selected':''}>${t}</option>`).join('')}</select></label>
    <label class="check-line"><input name="sync_chats" type="checkbox" ${cfg.sync_chats?'checked':''}> После поиска прочитать добавленные открытые чаты</label>
    <label class="check-line"><input name="sync_registry" type="checkbox" ${cfg.sync_registry?'checked':''}> Обновить подтверждённые корпуса из ЕИСЖС</label><label class="check-line"><input name="sync_sheets" type="checkbox" ${cfg.sync_sheets?'checked':''}> Синхронизировать Google Таблицу</label>
    <label class="check-line"><input name="delivery" type="checkbox" ${cfg.delivery?'checked':''}> Отправлять недельный дайджест ботом в указанную группу Telegram</label>
    <p class="caption">Отправка включается только этой настройкой. При неопределённом ответе Telegram повторная отправка автоматически не выполняется.</p>
    <h3>Платные вызовы поиска и моделей</h3><div class="form-grid"><label class="field">На один запуск<input name="per_run" type="number" min="1" max="100" value="${cfg.per_run}"></label><label class="field">В сутки UTC<input name="daily" type="number" min="1" max="1000" value="${cfg.daily}"></label><label class="field">В месяц UTC<input name="monthly" type="number" min="1" max="10000" value="${cfg.monthly}"></label></div>
    <p class="caption">Общие лимиты для ручного поиска, расписания и сравнения. Это ограничение числа вызовов, не гарантия денежного бюджета. Неудачные попытки тоже учитываются; кэш — нет.</p>
    <button class="button primary" type="submit">Сохранить расписание и лимиты</button></form>
    <div id="weekly-status" role="status"><p>${esc(ops.scheduler_error||'')}</p><p>Следующий запуск: ${cfg.next_run?dateText(cfg.next_run):'расписание выключено'}.</p><p>За ${esc(ops.usage.month)}: ${ops.usage.calls} платных попыток. Денежная сумма неизвестна.</p></div>
    <details><summary>История недельных запусков</summary>${ops.history.map(row=>`<article class="weekly-item"><h4>${dateText(row.due)} · ${esc(opLabels[row.status]||row.status)}</h4>${row.error?`<p>${esc(row.error)}</p>`:''}<pre class="source-text">${esc((JSON.parse(row.payload||'{}')).text||'Ожидается завершение поиска')}</pre></article>`).join('')||'<p>Запусков по расписанию пока нет.</p>'}</details>
  </details>
  <details class="operations-section"><summary>Сравнить поиск и модели</summary>
    <p>Одинаковые запросы, русский язык, последний месяц. Результаты сохраняются; повтор в тот же день использует кэш. Без оценок аналитика рейтинг качества не рассчитывается.</p>
    <form id="ops-benchmark-models"><div class="form-grid"><label class="field">Модель A<input name="benchmark_a" value="${esc(bench.models.benchmark_a)}" placeholder="ID модели OpenRouter"></label><label class="field">Модель B<input name="benchmark_b" value="${esc(bench.models.benchmark_b)}" placeholder="ID другой модели OpenRouter"></label></div><button class="button" type="submit">Сохранить модели для сравнения</button></form>
    <form id="ops-benchmark"><label class="field">До четырёх запросов — по одному на строку<textarea name="queries" rows="4" required>ЖК Москва перенос передачи ключей жалобы жителей\nЖК Санкт-Петербург протечки жалобы УК</textarea></label>
    <div class="checks">${Object.entries(bench.providers).map(([provider,ready])=>`<label><input name="provider" type="checkbox" value="${provider}" ${provider!=='brave'?'checked':''}> ${provider} · ${ready?'ключ сохранён':'нужен ключ'}</label>`).join('')}</div>
    <label class="check-line"><input name="models" type="checkbox"> Дополнительно сравнить две модели на одинаковых фрагментах</label>
    <div class="chat-actions"><button class="button primary" type="submit" name="mode" value="live">Запустить реальное сравнение</button><button class="button" type="submit" name="mode" value="demo">Учебный пример без ключей</button></div>
    <p class="caption">До 12 поисковых вызовов и 2 вызовов моделей. Ключи Perplexity и Brave добавляются в форме подключений выше. Сравнение не добавляет объекты в рабочий реестр.</p></form>
    <div id="benchmark-result" aria-live="polite"></div>
  </details>`;
  if(last) await watchBenchmark(last.id);
}
function renderBenchmark(run) {
  benchmarkRun=run;
  if(!$('#benchmark-result'))return;
  const result=run.result;
  if(!result){$('#benchmark-result').innerHTML=`<p>${run.status==='running'?'Сравнение выполняется. Можно закрыть окно и вернуться.':esc(run.error||run.status)}</p>`;return;}
  const demo=result.mode==='demo';
  function rated(row){return row.documents.map(d=>run.reviews.find(r=>r.query===row.query&&r.url===d.url)).filter(r=>r&&r.relevant!==null);}
  $('#benchmark-result').innerHTML=`<h3>${demo?'Учебная выдача — без интернета и без оценки провайдеров':'Результаты сравнения'}</h3><p>${esc(run.error||'')} ${esc(result.limitations)}</p><p class="caption">Завершённых внешних вызовов: ${result.external_requests}; шагов из кэша: ${result.cache_hits}. Время кэшированного шага не сравнивайте со временем API.</p>
  ${result.search.map((row,index)=>{const ratings=rated(row);return `<details><summary>${esc(row.provider)} · ${row.documents.length} ссылок · ${row.domains||0} доменов · ${row.seconds??'—'} сек${row.cached?' (кэш)':''}</summary><p>${esc(row.query)}</p>${row.error?`<p class="notice error">${esc(row.error)}</p>`:''}<p>Оценено ${ratings.length} из ${row.documents.length}; релевантных: ${ratings.filter(r=>r.relevant).length}${ratings.length?' ('+Math.round(100*ratings.filter(r=>r.relevant).length/ratings.length)+'% среди оценённых)':''}.</p>${row.documents.map((d,di)=>{const review=run.reviews.find(r=>r.query===row.query&&r.url===d.url);return `<article class="benchmark-source"><a href="${safeURL(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.title||host(d.url))} ↗</a><p>${esc(d.text)}</p>${demo?'<span class="caption">Вымышленный учебный источник</span>':`<label class="field">Оценка аналитика<select data-review-row="${index}" data-review-doc="${di}"><option value="" ${review?.relevant==null?'selected':''}>Не оценено</option><option value="1" ${review?.relevant===1?'selected':''}>Релевантно</option><option value="0" ${review?.relevant===0?'selected':''}>Не релевантно</option></select></label>`}</article>`;}).join('')}</details>`;}).join('')}
  ${result.models.map(m=>`<section><h4>${esc(m.role)} · ${esc(m.model||'учебный режим')}</h4>${m.error?`<p>${esc(m.error)}</p>`:`<p>Совпадение цитаты и названия с текстом: ${m.quote_match_rate===null?'нет объектов':Math.round(m.quote_match_rate*100)+'%'}. Отклонено: ${m.rejected}. Время: ${m.seconds} сек${m.cached?' (кэш)':''}.</p><p class="caption">Это проверка опоры на текст, а не истинности выводов.</p>${m.accepted.map(f=>`<p>${esc(f.name)}: ${esc(f.summary)}<br><span class="caption">«${esc(f.quote)}»</span></p>`).join('')}`}</section>`).join('')}`;
}
async function watchBenchmark(id) {
  clearTimeout(benchmarkTimer);
  const run=await api('/benchmark/'+id);renderBenchmark(run);
  if(run.status==='running')benchmarkTimer=setTimeout(()=>watchBenchmark(id).catch(e=>toast(e.message)),2000);
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-op]');if(!button)return;
  button.disabled=true;
  try {
    if(button.dataset.op==='tg-disconnect'){await api('/telegram/session',{method:'DELETE'});$('#tg-status').textContent='Локальная сессия удалена. Можно запросить новый код.';}
    if(button.dataset.op==='sync-chat'){
      button.textContent='Читаю…';
      const result=await api('/records/'+state.selected.id+'/telegram/sync',{method:'POST',body:JSON.stringify({url:button.dataset.url})});
      await openRecord(state.selected.id);toast('Добавлено сообщений: '+result.ingest.added+(result.partial?'. Есть ещё сообщения — повторите чтение.':''));
    }
    if(button.dataset.op==='analyze-chat'){button.textContent='Разбираю…';const r=await api('/records/'+state.selected.id+'/chats/analyze',{method:'POST'});await openRecord(state.selected.id);toast('Размечено сообщений: '+r.labelled+(r.more?'. Остались сообщения, повторите разбор.':''));}
    if(button.dataset.op==='remove-chat'){await api('/records/'+state.selected.id+'/telegram',{method:'DELETE',body:JSON.stringify({url:button.dataset.url})});await loadTelegramLinks(state.selected.id);}
  }catch(error){toast(error.message);if(button.dataset.op==='sync-chat')button.textContent='Прочитать';if(button.dataset.op==='analyze-chat')button.textContent='Разобрать сообщения моделью';}
  finally{button.disabled=false;}
});
document.addEventListener('submit',async event=>{
  if(!event.target.id.startsWith('ops-'))return;
  event.preventDefault();const form=new FormData(event.target);const buttons=[...event.target.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
  try{
    const id=event.target.id;
    if(id==='ops-chat-link'){await api('/records/'+state.selected.id+'/telegram',{method:'POST',body:JSON.stringify({url:form.get('url')})});await loadTelegramLinks(state.selected.id);toast('Ссылка добавлена. Нажмите «Прочитать» после входа в Telegram.');}
    if(id==='ops-tg-login'){await api('/telegram/login',{method:'POST',body:JSON.stringify({phone:form.get('phone').trim()})});$('#tg-status').textContent='Код запрошен. Введите его ниже.';event.target.reset();}
    if(id==='ops-tg-confirm'){const r=await api('/telegram/confirm',{method:'POST',body:JSON.stringify(Object.fromEntries(form))});$('#tg-status').textContent=r.status==='password_needed'?'Нужен пароль двухэтапной проверки.':'Вход выполнен. Теперь можно читать добавленные открытые чаты.';event.target.reset();}
    if(id==='ops-weekly'){
      const data={enabled:form.has('enabled'),delivery:form.has('delivery'),sync_chats:form.has('sync_chats'),sync_registry:form.has('sync_registry'),sync_sheets:form.has('sync_sheets'),region:form.get('region')};
      for(const name of ['weekday','hour','per_run','daily','monthly'])data[name]=Number(form.get(name));
      const r=await api('/operations',{method:'PUT',body:JSON.stringify(data)});$('#weekly-status').textContent=r.next_run?'Сохранено. Следующий запуск: '+dateText(r.next_run):'Сохранено. Расписание выключено.';toast('Расписание и лимиты сохранены');
    }
    if(id==='ops-benchmark-models'){await api('/benchmark/models',{method:'PUT',body:JSON.stringify(Object.fromEntries(form))});toast('Модели сравнения сохранены');}
    if(id==='ops-benchmark'){
      const r=await api('/benchmark',{method:'POST',body:JSON.stringify({mode:event.submitter?.value||'live',queries:form.get('queries').split('\n').map(s=>s.trim()).filter(Boolean),providers:form.getAll('provider'),model_roles:form.has('models')?['benchmark_a','benchmark_b']:[]})});
      await watchBenchmark(r.id);
    }
  }catch(error){toast(error.message);if(event.target.id.startsWith('ops-tg-'))$('#tg-status').textContent=error.message;}
  finally{buttons.forEach(b=>b.disabled=false);}
});
document.addEventListener('change',async event=>{
  const select=event.target.closest('[data-review-row]');if(!select||!benchmarkRun)return;
  const row=benchmarkRun.result.search[Number(select.dataset.reviewRow)];const doc=row.documents[Number(select.dataset.reviewDoc)];
  try{benchmarkRun=await api('/benchmark/'+benchmarkRun.id+'/review',{method:'PUT',body:JSON.stringify({query:row.query,url:doc.url,relevant:select.value===''?null:select.value==='1'})});toast('Оценка сохранена. Итог обновится при следующем открытии раздела.');}catch(error){toast(error.message);}
});
