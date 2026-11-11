let objectTaskTimer;
async function loadAdvanced(id) {
  const value=await api('/records/'+id+'/intelligence');
  const score=value.score;
  const el=document.createElement('details');el.id='advanced-card';
  el.innerHTML=`<summary>Дополнительный анализ и предложение</summary><p>Очередь исследования: ${score.rank_score} баллов. ${score.excluded?'Объект исключён из отбора.':score.needs_research?'Есть основания для дополнительной проверки.':'Недостаточно свежих подтверждённых сведений.'}</p><p class="caption">${score.reasons.map(esc).join('<br>')}</p>
  <div class="chat-actions"><button class="button" data-advanced="research">Углубить исследование</button><button class="button" data-advanced="author">Подготовить предложение моделью</button><button class="button" data-advanced="chat_discovery">Найти открытые чаты</button></div><p class="caption">Дополнительное исследование и автор используют платные API. Результат сохраняется в фоне, окно можно закрыть. Автор создаёт новую версию отчёта; одобрение аналитика обязательно для клиентского PDF.</p><div id="object-task" role="status"></div>
  ${value.research?`<details><summary>Результат дополнительного исследования</summary>${(value.research.answers||[]).map(a=>`<p><b>${esc(({rve:'Разрешение на ввод',developer_response:'Ответ девелопера',contact:'Рабочий адресат',demand:'Признаки спроса'})[a.question])}</b><br>«${esc(a.quote)}»<br><a href="${safeURL(a.url)}" target="_blank" rel="noopener noreferrer">Источник ↗</a> · ${a.kind==='snippet'?'Только фрагмент поиска':'Сохранённый текст'}</p>`).join('')||'<p>Прямые ответы не подтверждены. Просмотрите найденные документы.</p>'}${value.research.documents.map(d=>`<p><a href="${safeURL(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.title)} ↗</a></p>`).join('')}</details>`:''}
  <details><summary>Предложенные чаты</summary><p class="caption">Проверьте название, описание и адрес объекта. Подключение выполняется только по вашей кнопке; приложение не вступает в группы.</p>${value.chat_candidates.map(c=>`<article class="weekly-item"><a href="${safeURL(c.url)}" target="_blank" rel="noopener noreferrer">${esc(c.payload.title||c.url)}</a><p class="caption">${esc(c.url)} · ${esc(({suggested:'Требует проверки',accepted:'Добавлен',rejected:'Отклонён'})[c.status])}</p><button class="button" data-advanced="inspect" data-url="${esc(c.url)}">Проверить описание</button> ${c.status==='suggested'?`<button class="button" data-advanced="accept" data-url="${esc(c.url)}">Это нужный чат — добавить</button> <button class="button quiet" data-advanced="reject" data-url="${esc(c.url)}">Не подходит</button>`:''}<div class="chat-inspection"></div></article>`).join('')||'<p>Кандидатов пока нет. Нажмите «Найти открытые чаты».</p>'}</details>
  <details><summary>Динамика обсуждений: неделя к неделе</summary><p>Сообщения: ${value.dynamics.previous.messages} → ${value.dynamics.current.messages}. Авторы: ${value.dynamics.previous.authors} → ${value.dynamics.current.authors}.</p><p>Негативных среди размеченных: ${value.dynamics.previous.negative_share??'—'}% → ${value.dynamics.current.negative_share??'—'}%.</p><p>${value.dynamics.signals.map(esc).join('<br>')||'Заметных изменений с достаточной базой сравнения не обнаружено.'}</p><p class="caption">${esc(value.dynamics.note)}</p></details>`;
  $('#completion-card').prepend(el);
  const last=await api('/tasks/latest');
  if(last?.record_id===id)await watchObjectTask(last.id,false);
  await loadExtraSources(id);
  applyRole();
}
async function watchObjectTask(id,refreshOnFinish=true) {
  clearTimeout(objectTaskTimer);
  const task=await api('/tasks/'+id);
  if(!$('#object-task')||state.selected?.id!==task.record_id)return;
  const active=['queued','running'].includes(task.status);
  $('#object-task').textContent=active?'Операция выполняется. Можно закрыть окно и вернуться.':task.error||'Операция завершена. Результат сохранён.';
  if(active)objectTaskTimer=setTimeout(()=>watchObjectTask(id,true).catch(e=>toast(e.message)),1500);
  else if(refreshOnFinish){await openRecord(task.record_id);$('#advanced-card').open=true;if(task.kind==='author')$('#brief-check').open=true;}
}
async function loadAdvancedSettings() {
  const rows=await api('/cases');const el=document.createElement('details');
  el.innerHTML=`<summary>Кейсы агентства</summary><p>Загрузите реальный кейс по образцу. Автор использует только approved=true. В клиентский PDF попадают только кейсы с отдельным разрешением client_use=true.</p><label class="field">JSON кейса<input type="file" id="case-file" accept=".json"></label><a href="/api/cases/example" download="agency-case.json">Скачать образец JSON</a>${rows.map(c=>`<p>${esc(c.title)} · ${c.approved?'утверждён':'черновик'} · ${c.client_use?'можно клиенту':'только внутри'} <button class="button quiet" data-advanced="case-remove" data-id="${esc(c.id)}">Удалить</button></p>`).join('')||'<p class="caption">Кейсов пока нет; приложение не выдумывает опыт агентства.</p>'}`;
  $('#completion-settings').append(el);
  await loadAutomationSettings();
  await loadExternalSettings();
  await loadEvaluationSettings();
}
document.addEventListener('click',async e=>{
 const b=e.target.closest('[data-advanced]');if(!b)return;
 const old=b.textContent;b.disabled=true;
 try{
  const action=b.dataset.advanced;
  if(['research','author','chat_discovery','extra_sources'].includes(action)){
   const job=await api('/records/'+state.selected.id+'/tasks',{method:'POST',body:JSON.stringify({kind:action})});await watchObjectTask(job.id);
  }
  if(action==='accept'||action==='reject'){
   await api('/records/'+state.selected.id+'/chat-candidates',{method:'POST',body:JSON.stringify({url:b.dataset.url,accepted:action==='accept'})});await openRecord(state.selected.id);$('#advanced-card').open=true;
  }
  if(action==='inspect'){
   b.textContent='Проверяю…';const r=await api('/chat-candidates/inspect',{method:'POST',body:JSON.stringify({url:b.dataset.url,accepted:false})});
   b.closest('article').querySelector('.chat-inspection').textContent=`${r.title}. ${r.group?'Группа':'Канал'}. Участников: ${r.participants??'неизвестно'}. ${r.description}. ${r.note}`;
  }
  if(action==='client-download'){
   const response=await fetch('/api/briefs/'+b.dataset.brief+'/client-pdf');if(!response.ok){const r=await response.json();throw Error(r.detail||'Не удалось получить PDF');}
   const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='Мегаполис — предложение.pdf';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }
  if(action==='cloud-backup'){const r=await api('/external-operations/backup',{method:'POST'});$('#external-result').textContent='Зашифрованная копия сохранена: '+r.object_key;}
  if(action==='encryption-key'){const r=await api('/external-operations/encryption-key',{method:'POST'});$('#external-key').value=r.key;$('#external-key').hidden=false;$('#external-result').textContent=r.note;}
  if(action==='price-history'){await api('/budget/price-history',{method:'POST'});await openSettings();toast('Резервы проставлены');}
  if(action==='case-remove'){await api('/cases/'+b.dataset.id,{method:'DELETE'});await openSettings();toast('Кейс удалён');}
 }catch(err){toast(err.message);}finally{b.disabled=false;b.textContent=old;}
});
document.addEventListener('change',async e=>{
 if(e.target.id!=='case-file')return;const file=e.target.files[0];if(!file)return;
 try{if(file.size>15000)throw Error('Кейс должен быть не больше 15 КБ');await api('/cases',{method:'POST',body:await file.text()});await openSettings();toast('Кейс сохранён');}catch(err){toast(err.message);}finally{e.target.value='';}
});
async function loadAutomationSettings() {
 const [auto,budget]=await Promise.all([api('/automation'),api('/budget')]);const cfg=auto.settings;const money=budget.settings;
 const el=document.createElement('div');el.id='automation-settings';
 el.innerHTML=`<details><summary>Ночное чтение и уведомления</summary><form id="advanced-automation">
 ${Object.entries({nightly_chats:'Каждый день читать подключённые открытые чаты',analyze_chats:'Размечать новые сообщения моделью',auto_briefs:'Готовить новые версии предложений моделью',telegram_alerts:'Отправлять рабочей группе уведомления о сбоях и изменениях'}).map(([k,t])=>`<label class="check-line"><input type="checkbox" name="${k}" ${cfg[k]?'checked':''}>${t}</label>`).join('')}
 <div class="form-grid"><label class="field">Час по Москве<input name="hour" type="number" min="0" max="23" value="${cfg.hour}"></label><label class="field">Объектов за ночь<input name="max_records" type="number" min="1" max="50" value="${cfg.max_records}"></label><label class="field">Пакетов разметки на объект<input name="max_label_batches" type="number" min="1" max="5" value="${cfg.max_label_batches}"></label></div><p class="caption">Один пакет — до 120 сообщений. Частичный сбор продолжится на следующем запуске. Предложения остаются внутренними черновиками. Отправки девелоперам нет.</p><button class="button">Сохранить автоматизацию</button></form>
 <details><summary>История ночной обработки и уведомлений</summary>${auto.history.map(r=>`<p>${dateText(r.created_at)} · ${esc(r.name)} · ${esc(r.status)} ${esc(r.payload?.error||'')}</p>`).join('')||'<p>Обработки ещё не было.</p>'}${auto.notifications.map(n=>`<p>${esc(n.title)} · ${esc(n.status)} ${esc(n.error||'')}</p>`).join('')}</details></details>
 <details><summary>Денежный резерв бюджета</summary><p>Получено от провайдера: $${budget.billed_usd}. Ожидаемые/неподтверждённые расходы: $${budget.pending_reserved_usd}. Вызовов без оценки: ${budget.unpriced_calls}.</p><p class="caption">${esc(budget.note)}</p><form id="advanced-budget"><label class="check-line"><input type="checkbox" name="enabled" ${money.enabled?'checked':''}> Останавливать вызовы при исчерпании резерва</label>${[['monthly_usd','Лимит на месяц, USD'],['search_reserve_usd','Резерв на один поиск, USD'],['model_reserve_usd','Резерв на один вызов модели, USD']].map(([k,t])=>`<label class="field">${t}<input type="number" step="0.001" min="0.001" name="${k}" value="${money[k]}" required></label>`).join('')}<button class="button">Сохранить бюджет</button></form>${budget.unpriced_calls?'<button class="button quiet" data-advanced="price-history">Оценить прошлые вызовы по сохранённым резервам</button>':''}</details>`;
 $('#completion-settings').append(el);
}
document.addEventListener('submit',async e=>{
 if(!['advanced-automation','advanced-budget'].includes(e.target.id))return;
 e.preventDefault();const f=new FormData(e.target);const b=e.target.querySelector('button');b.disabled=true;
 try{
  const data=Object.fromEntries(f);
  if(e.target.id==='advanced-budget'){for(const k of ['monthly_usd','search_reserve_usd','model_reserve_usd'])data[k]=Number(data[k]);data.enabled=f.has('enabled');await api('/budget',{method:'PUT',body:JSON.stringify(data)});}
  else{for(const k of ['nightly_chats','analyze_chats','auto_briefs','telegram_alerts'])data[k]=f.has(k);for(const k of ['hour','max_records','max_label_batches'])data[k]=Number(data[k]);await api('/automation',{method:'PUT',body:JSON.stringify(data)});}
  toast('Настройки сохранены');
 }catch(err){toast(err.message);}finally{b.disabled=false;}
});

async function loadExternalSettings(){
 const r=await api('/external-operations');const el=document.createElement('details');
 el.innerHTML=`<summary>Внешние копии и дубль дайджеста на почту</summary><form id="advanced-external"><label class="check-line"><input type="checkbox" name="email_digest" ${r.settings.email_digest?'checked':''}> Отправлять новые недельные дайджесты на рабочую почту</label><label class="check-line"><input type="checkbox" name="cloud_backup" ${r.settings.cloud_backup?'checked':''}> Ежедневно копировать зашифрованную базу в S3</label><button class="button">Сохранить</button></form><p class="caption">Настройки доступа — в дополнительных подключениях выше. В копию входят база и конфиги; ключи сервисов сохраняйте отдельно. Нужен приватный bucket AWS S3. Предельный размер базы для этой выгрузки — 100 МБ.</p><div class="chat-actions"><button class="button" data-advanced="encryption-key">Создать ключ шифрования</button><button class="button" data-advanced="cloud-backup">Выгрузить копию сейчас</button></div><input id="external-key" readonly hidden aria-label="Сохраните ключ шифрования в менеджере паролей"><div id="external-result" role="status"></div><details><summary>История доставки и копий</summary>${[...r.email,...r.backups].map(x=>`<p>${dateText(x.created_at)} · ${esc(x.status)} ${esc(x.error||'')}</p>`).join('')||'<p>Операций пока нет.</p>'}</details>`;
 $('#completion-settings').append(el);
}
document.addEventListener('submit',async e=>{
 if(e.target.id!=='advanced-external')return;e.preventDefault();const f=new FormData(e.target);const b=e.target.querySelector('button');b.disabled=true;
 try{await api('/external-operations',{method:'PUT',body:JSON.stringify({email_digest:f.has('email_digest'),cloud_backup:f.has('cloud_backup')})});toast('Настройки сохранены');}catch(err){toast(err.message);}finally{b.disabled=false;}
});

async function loadExtraSources(id){
 const value=await api('/records/'+id+'/extra-sources');const b=value.binding;const el=document.createElement('details');el.id='extra-sources-card';
 el.innerHTML=`<summary>Вакансии, VK и упоминания в ЕФРСБ</summary><p class="caption">Укажите проверенные идентификаторы. Эти источники дают контекст для аналитика; они не меняют юридический статус и не подтверждают проблемы ЖК автоматически.</p><form id="extra-source-binding">
 ${[['hh_employer','ID работодателя hh.ru'],['vk_group','Адрес группы VK без vk.com/'],['inn','ИНН юридического лица'],['legal_name','Полное наименование юрлица']].map(([k,t])=>`<label class="field">${t}<input name="${k}" value="${esc(b[k])}"></label>`).join('')}
 <label class="check-line"><input type="checkbox" name="confirmed" ${b.confirmed?'checked':''}> Я сверил связь источников и юрлица с объектом</label><label class="check-line"><input type="checkbox" name="automatic" ${b.automatic?'checked':''}> Проверять с недельным мониторингом (ЕФРСБ — раз в месяц)</label><button class="button">Сохранить источники</button></form><button class="button" data-advanced="extra_sources">Собрать дополнительные сигналы</button>
 <p class="caption">Ключ VK и User-Agent hh.ru добавляются в подключениях. Поиск ЕФРСБ использует Yandex Search и не охватывает весь реестр.</p>
 ${Object.entries(value.result?.sources||{}).map(([k,r])=>`<details><summary>${esc(k)} · ${esc(r.status)} · ${r.items.length} материалов</summary><p class="caption">${esc(r.note||r.error||'')}</p>${r.items.map(i=>`<article class="weekly-item"><a href="${safeURL(i.url)}" target="_blank" rel="noopener noreferrer">${esc(i.title)} ↗</a><p>${esc(i.text)}</p><p class="caption">${esc(i.published_at||'Дата публикации неизвестна')} · класс ${esc(i.trust)}</p></article>`).join('')}</details>`).join('')}`;
 $('#completion-card').append(el);
}
document.addEventListener('submit',async e=>{
 if(e.target.id!=='extra-source-binding')return;e.preventDefault();const form=new FormData(e.target);const b=e.target.querySelector('button');b.disabled=true;
 try{const data=Object.fromEntries(form);data.confirmed=form.has('confirmed');data.automatic=form.has('automatic');await api('/records/'+state.selected.id+'/extra-sources',{method:'PUT',body:JSON.stringify(data)});await openRecord(state.selected.id);$('#extra-sources-card').open=true;toast('Источники сохранены');}catch(err){toast(err.message);}finally{b.disabled=false;}
});
async function loadEvaluationSettings(){
 const rows=await api('/evaluations');const el=document.createElement('details');el.id='evaluation-settings';
 el.innerHTML=`<summary>Оценка качества на проверенной выборке</summary><p>Сверьте события вручную, присвойте им единые ID с корпусом и датой, внесите результаты сравниваемых поисков в JSON. Одни и те же события из перепечаток не должны иметь разные ID.</p><a href="/api/evaluations/example" download="evaluation-example.json">Скачать учебный формат</a><label class="field">JSON оценки<input type="file" id="evaluation-file" accept=".json"></label><p class="caption">Победитель выбирается только на проверенной реальной выборке от 20 событий при точности не ниже 90%. Полнота относится только к этой выборке.</p>${rows.map(r=>`<article class="weekly-item"><h4>${esc(r.title)} ${r.synthetic?'· учебный':''}</h4><p>${esc(r.scope)}</p><p>Эталон: ${r.reference_size} событий. Кандидат: ${esc(r.candidate||'не определён')}.</p>${r.runs.map(v=>`<p>${esc(v.name)}: точность ${v.precision===null?'—':Math.round(v.precision*100)+'%'}, полнота ${Math.round(v.recall*100)}%, пропущено ${v.false_negative}, лишних ${v.false_positive}.</p>`).join('')}<p class="caption">Размеченных сообщений: ${r.chats.count}. Принятых разборов с максимум одной фактической правкой: ${r.reports.accepted_with_at_most_one_correction}.</p><details><summary>Расчёт и пропущенные события</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(JSON.stringify(r,null,2))}</pre></details></article>`).join('')}`;
 $('#completion-settings').append(el);
}
document.addEventListener('change',async e=>{
 if(e.target.id!=='evaluation-file')return;const file=e.target.files[0];if(!file)return;
 try{if(file.size>1000000)throw Error('Оценка должна быть не больше 1 МБ');await api('/evaluations',{method:'POST',body:await file.text()});await openSettings();toast('Оценка сохранена');}catch(err){toast(err.message);}finally{e.target.value='';}
});
