/* Advanced tools are tucked into the existing object card and Connections. */
const reasons={client:'Наш клиент',partner:'Партнёр',refused:'Отказ',other:'Другая причина'};
let identity={role:'admin',mode:'local'};
function applyRole() {
  const viewer=identity.role==='viewer';
  document.querySelectorAll('[data-action="settings"]').forEach(el=>el.hidden=identity.role!=='admin');
  if(viewer){
    document.querySelectorAll('form, [data-pdf], [data-op], [data-advanced], [data-completion]:not([data-completion="logout"]), [data-action="scan"], [data-action="demo-scan"], [data-action="import"], [data-action="chat-import"], [data-action="chat-demo"]').forEach(el=>el.hidden=true);
  }
}
async function loadCompletion(id) {
  const [reg,briefs]=await Promise.all([api('/records/'+id+'/registry'),api('/records/'+id+'/brief-history')]);
  const r=state.selected;
  if(r.blocked)document.querySelectorAll('#detail [data-pdf]').forEach(b=>{b.disabled=true;b.textContent='В стоп-листе';});
  const target=document.createElement('div');target.id='completion-card';
  $('#detail-content .dialog-body').append(target);
  target.innerHTML=`<details><summary>Реестр корпуса и история сроков</summary><p class="caption">Укажите ID именно корпуса из ЕИСЖС. Совпадение названия ЖК само по себе недостаточно. При недоступности реестра можно загрузить снимок по образцу.</p>
  <form id="complete-binding"><label class="field">ID корпуса в ЕИСЖС<input name="house_id" pattern="[0-9]{1,12}" required value="${esc(reg.binding?.house_id||'')}"></label><label class="check-line"><input type="checkbox" name="confirmed" ${reg.binding?.confirmed?'checked':''}> Я сверил адрес и корпус</label><button class="button">Сохранить привязку</button></form>
  <div class="chat-actions"><button class="button" data-completion="registry-refresh">Обновить из ЕИСЖС</button><button class="button" data-completion="registry-import">Загрузить JSON снимка</button><a href="/api/registry/example" download="registry-example.json">Образец JSON</a>${reg.binding?'<button class="button quiet" data-completion="registry-unbind">Убрать привязку</button>':''}</div>
  <p>Снимков: ${reg.snapshots.length}. ${reg.binding?.confirmed?'Корпус подтверждён оператором.':'Корпус ещё не подтверждён.'}</p>
  ${reg.snapshots.slice(-3).map(s=>`<p>${dateText(s.observed_at)} · ${esc(s.name)}<br>Ввод: ${esc(s.commissioning||'неизвестно')} · Передача: ${esc(s.transfer||'неизвестно')} · Готовность: ${s.readiness??'—'}%<br><a href="${safeURL(s.source_url)}" target="_blank" rel="noopener noreferrer">Источник ↗</a> · ${s.imported?'Загружено оператором':'Получено из реестра'}</p>`).join('')}
  ${reg.changes.map(c=>`<p>${esc(({commissioning:'Ввод',transfer:'Передача',readiness:'Готовность',rve:'РВЭ'})[c.field])}: ${esc(c.before)} → ${esc(c.after)} · наблюдение ${dateText(c.observed_at)}</p>`).join('')}
  ${reg.assessment?`<div class="notice">Предварительный приоритет: ${esc(({early:'Ранний риск',watch:'Наблюдение',none:'Не определён'})[reg.assessment.priority]||reg.assessment.priority)} · уверенность: ${esc(({low:'низкая',medium:'средняя',high:'высокая'})[reg.assessment.confidence])}. ${reg.assessment.eligible?'Доступен для рассмотрения':'Для коммерческого контакта данных недостаточно или есть ограничение'}<p>${reg.assessment.limitations.map(esc).join('<br>')}</p></div>`:'<p class="caption">Расчёт появится после подтверждения корпуса и загрузки снимка.</p>'}
  <details><summary>Коммерческий контекст для расчёта</summary><form id="complete-sales"><label class="field">Состояние заказчика<select name="state">${Object.entries({unknown:'Неизвестно',active:'Действующий',bankrupt:'Банкрот',third_party:'Третье лицо',disputed:'Спорный'}).map(([v,t])=>`<option value="${v}" ${reg.sales?.state===v?'selected':''}>${t}</option>`).join('')}</select></label>
  ${Object.entries({solvent:'Платёжеспособность подтверждена',buyer_known:'Известен принимающий решение',public_response:'Есть публичный ответ',demand:'Есть запрос на помощь',warm_contact:'Есть тёплый контакт',other_agency:'Уже работает другое агентство'}).map(([k,t])=>`<label class="check-line"><input type="checkbox" name="${k}" ${reg.sales?.[k]?'checked':''}>${t}</label>`).join('')}
  <label class="field">Других объектов<input type="number" name="other_objects" min="0" max="100000" value="${reg.sales?.other_objects||0}"></label><label class="field">На чём основана оценка<textarea name="note" required minlength="10" maxlength="2000">${esc(reg.sales?.note||'')}</textarea></label><button class="button">Сохранить оценку</button></form></details></details>
  <details id="brief-check"><summary>Версии PDF и проверка выводов</summary><p class="caption">Проверка модели оценивает опору на сохранённые источники. Достоверность и актуальность проверяет аналитик. Одобрение относится только к указанной версии.</p>
  ${briefs.map(b=>{const model=b.reviews.find(v=>v.kind==='model');const operator=b.reviews.find(v=>v.kind==='operator');return `<article class="weekly-item"><h4>Версия ${b.version} · ${dateText(b.created_at)}</h4><a href="/api/briefs/${b.id}/pdf" target="_blank">Открыть PDF ↗</a>${b.author?`<p>Автор: ${esc(b.author.model)} · ${b.author.status==='reviewed'?'проверка пройдена':'нужен ручной разбор'}. Раундов проверки: ${b.author.attempts.length}.</p>${b.author.attempts.at(-1).issues.map(i=>`<p class="caption">${esc(i.reason)}</p>`).join('')}`:''}<p>${model?esc(({passed:'Опора на текст проверена',revise:'Нужна доработка',manual:'Нужна проверка аналитика'})[model.payload.verdict]):'Моделью не проверено'} · ${operator?(operator.payload.approved?'Одобрено аналитиком':'Не одобрено аналитиком'):'Решение аналитика не принято'}</p>
  ${model?.payload.issues.map(i=>`<p class="caption">${i.severity==='blocker'?'●':'○'} ${esc(i.reason)}</p>`).join('')||''}${operator?`<p>${esc(operator.actor)}: ${esc(operator.payload.note)}</p>`:''}<p><a href="/api/briefs/${b.id}/client-preview" target="_blank">Предпросмотр для девелопера ↗</a></p>${operator?.payload.approved?`<button class="button" data-advanced="client-download" data-brief="${b.id}">Скачать одобренное предложение</button>`:''}
  <button class="button" data-completion="review" data-brief="${b.id}">Проверить моделью</button><form class="approval-form" data-brief="${b.id}"><label class="field">Решение<select name="approved"><option value="false">Требует доработки</option><option value="true">Проверено и одобрено</option></select></label><label class="field">Что проверено / что исправить<textarea name="note" required minlength="10" maxlength="2000"></textarea></label><button class="button">Сохранить решение</button></form></article>`;}).join('')||'<p>Сначала создайте PDF-разбор.</p>'}</details>
  <details><summary>Исключить из предложений</summary>${r.blocked?`<p>В стоп-листе: ${esc(reasons[r.blocked.reason])}. ${esc(r.blocked.note)}</p><button class="button" data-completion="stop-remove" data-id="${esc(r.blocked.id)}">Снять ограничение</button>`:`<form id="complete-stop"><label class="field">Исключение<select name="kind"><option value="record">Только этот объект</option><option value="developer">Все объекты этого девелопера</option></select></label><label class="field">Причина<select name="reason">${Object.entries(reasons).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label><label class="field">Комментарий<textarea name="note" maxlength="1000"></textarea></label><button class="button">Добавить в стоп-лист</button></form>`}</details>`;
  await loadAdvanced(id);
  applyRole();
}
async function loadCompletionSettings() {
  const [sheet,backups,stops]=await Promise.all([api('/sheets'),api('/maintenance'),api('/stoplist')]);
  const el=document.createElement('div');el.id='completion-settings';$('#operations-content').append(el);
  el.innerHTML=`<details><summary>Google Таблица — синхронизация</summary><p>Сохраните ID таблицы выше. Загрузите JSON сервисного аккаунта и предоставьте его адресу доступ редактора к таблице.</p><label class="field">JSON сервисного аккаунта<input id="google-credentials" type="file" accept=".json"></label><p id="google-credentials-status" role="status">${sheet.configured?'Настройки сохранены. Доступ проверится при синхронизации.':'Подключение не заполнено.'}</p><p class="caption">Ключ сохраняется в закрытых настройках приложения и не показывается повторно. Системные листы будут обновлены целиком; «Заметки», «Стоп-лист» и чужие вкладки сохраняются. В стоп-листе можно указать ID объекта или точное имя девелопера.</p><p>${Object.entries(sheet.preview).map(([k,v])=>`${esc(k)}: ${v}`).join(' · ')}</p><button class="button" data-completion="sheets-sync">Синхронизировать таблицу</button><div id="sheets-result" role="status"></div>${sheet.history.map(r=>`<p class="caption">${dateText(r.created_at)} · ${r.status==='completed'?'Обновлено':esc(JSON.parse(r.payload).error)}</p>`).join('')}</details>
  <details><summary>Стоп-лист агентства</summary><p>Совпадение девелопера проверяется по точному нормализованному имени. Проверьте варианты названий вручную.</p><div id="stop-list">${stops.map(s=>`<p>${esc(s.value)} · ${esc(reasons[s.reason])} · ${esc(s.note)} <button class="button quiet" data-completion="stop-remove" data-id="${esc(s.id)}">Убрать</button></p>`).join('')||'<p>Исключений нет. Добавляйте их из карточек объектов.</p>'}</div></details>
  <details><summary>Портфели девелоперов в ЕРЗ</summary><p class="caption">Первая страница рейтинга выбранного региона. Показатели портфеля не означают проблему конкретного корпуса.</p><form id="complete-erz"><label class="field">Регион<select name="region">${[['moskva','Москва'],['moskovskaya-oblast','Московская область'],['sankt-peterburg','Санкт-Петербург'],['leningradskaya-oblast','Ленинградская область']].map(([v,t])=>`<option value="${v}">${t}</option>`).join('')}</select></label><button class="button">Прочитать ЕРЗ</button></form><div id="erz-result" role="status"></div></details>
  <details><summary>Резервные копии</summary><p>Автоматически раз в сутки, пока приложение запущено. Храним минимум 14 последних копий. Это локальная защита от ошибки, отдельная копия вне сервера настраивается при размещении.</p><p class="caption">Копируется SQLite. Ключи .env и config нужно сохранять отдельно. Восстановление — по инструкции SERVER.md.</p><p>${esc(backups.error||'')}</p><button class="button" data-completion="backup">Создать копию сейчас</button><div id="backup-result" role="status">${backups.backups.slice(0,5).map(b=>`<p>${dateText(b.created_at)} · ${esc(b.payload.file)} · ${Math.ceil(b.payload.bytes/1024)} КБ</p>`).join('')||'Копий ещё нет'}</div></details>`;
  await loadAdvancedSettings();
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('[data-completion]');if(!b)return;
  const original=b.textContent;b.disabled=true;b.textContent='Выполняю…';
  try {
    const action=b.dataset.completion;const id=state.selected?.id;
    if(action==='logout'){await api('/auth/logout',{method:'POST'});location.reload();return;}
    if(action==='registry-import'){document.querySelector('#registry-file').click();return;}
    if(action==='registry-refresh'){await api('/records/'+id+'/registry/refresh',{method:'POST'});await openRecord(id);toast('Снимок реестра обновлён');}
    if(action==='registry-unbind'){await api('/records/'+id+'/registry',{method:'DELETE'});await openRecord(id);}
    if(action==='review'){await api('/briefs/'+b.dataset.brief+'/review',{method:'POST'});await openRecord(id);$('#brief-check').open=true;toast('Проверка завершена');}
    if(action==='stop-remove'){await api('/stoplist/'+b.dataset.id,{method:'DELETE'});await refresh();if($('#detail').open)await openRecord(id);if($('#connections').open)await openSettings();toast('Ограничение снято');}
    if(action==='sheets-sync'){const r=await api('/sheets/sync',{method:'POST'});$('#sheets-result').innerHTML=`<p>Обновлено вкладок: ${r.tabs}. <a href="${safeURL(r.url)}" target="_blank" rel="noopener noreferrer">Открыть таблицу ↗</a>${r.unresolved_stoplist_rows.length?' Проверьте строки стоп-листа: '+r.unresolved_stoplist_rows.join(', '):''}</p>`;}
    if(action==='backup'){const r=await api('/maintenance/backup',{method:'POST'});$('#backup-result').textContent='Проверенная копия: '+r.file;}
  }catch(err){toast(err.message);}finally{b.disabled=false;b.textContent=original;}
});
document.addEventListener('submit',async e=>{
  if(!e.target.id.startsWith('complete-')&&!e.target.matches('.approval-form'))return;
  e.preventDefault();const f=new FormData(e.target);const b=e.target.querySelector('button');b.disabled=true;
  try{
    const id=state.selected?.id;
    if(e.target.id==='complete-binding')await api('/records/'+id+'/registry',{method:'PUT',body:JSON.stringify({house_id:f.get('house_id'),confirmed:f.has('confirmed')})});
    if(e.target.id==='complete-sales'){const data=Object.fromEntries(f);data.other_objects=Number(data.other_objects);for(const k of ['solvent','buyer_known','public_response','demand','warm_contact','other_agency'])data[k]=f.has(k);await api('/records/'+id+'/sales',{method:'PUT',body:JSON.stringify(data)});}
    if(e.target.id==='complete-stop'){const data=Object.fromEntries(f);data.value=data.kind==='record'?id:state.selected.developer;await api('/stoplist',{method:'POST',body:JSON.stringify(data)});await refresh();}
    if(e.target.matches('.approval-form'))await api('/briefs/'+e.target.dataset.brief+'/approval',{method:'PUT',body:JSON.stringify({approved:f.get('approved')==='true',note:f.get('note')})});
    if(e.target.id==='complete-erz'){const r=await api('/registry/erz/'+f.get('region'),{method:'POST'});$('#erz-result').innerHTML=`<p>${esc(r.limitation)}</p>${r.rows.map(d=>`<p>${esc(d.developer)} · доля переноса: ${d.delayed_share_pct??'—'}% · средний перенос: ${d.shift_months??'—'} мес.</p>`).join('')}`;return;}
    const approval=e.target.matches('.approval-form');await openRecord(id);if(approval)$('#brief-check').open=true;toast('Сохранено');
  }catch(err){toast(err.message);}finally{b.disabled=false;}
});
document.addEventListener('change',async e=>{
  if(!['google-credentials','registry-file'].includes(e.target.id))return;
  const file=e.target.files[0];if(!file)return;
  try {
    if(e.target.id==='google-credentials'){
      if(file.size>30000)throw Error('Ключ должен быть не больше 30 КБ');
      const form=new FormData();form.append('file',file);const r=await api('/sheets/credentials',{method:'POST',body:form});
      $('#google-credentials-status').textContent='Ключ сохранён. Дайте доступ редактора: '+r.client_email;
    }else{
      if(file.size>100000)throw Error('Снимок должен быть не больше 100 КБ');
      const data=JSON.parse(await file.text());
      const reg=await api('/records/'+state.selected.id+'/registry');
      if(reg.binding?.house_id!==data.house_id)throw Error('ID снимка не совпадает с привязкой этого корпуса');
      await api('/registry/import',{method:'POST',body:JSON.stringify(data)});await openRecord(state.selected.id);toast('Снимок загружен');
    }
  }catch(err){toast(err.message);}finally{e.target.value='';}
});
