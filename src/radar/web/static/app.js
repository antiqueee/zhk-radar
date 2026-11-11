const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {new:'Новый',review:'На проверке',inwork:'В работе',proposal:'КП отправлено',meeting:'Встреча',closed:'Закрыт',later:'Отложен',rejected:'Не интересно'};
let state = {dataset:localStorage.getItem('megapolis-dataset') || 'imported',records:[],offers:[],selected:null,page:1,filter:''};
if (!['web','imported','demo'].includes(state.dataset)) state.dataset = 'imported';
let refreshId = 0;
async function api(path, options={}) {
  const response = await fetch('/api'+path, {...options,headers:{'X-Radar-Local':'1',...(options.body && !(options.body instanceof FormData) ? {'Content-Type':'application/json'} : {}),...options.headers}});
  if (response.status===401) {location.reload();throw Error('Сессия завершена');}
  if (!response.ok) {const d=await response.json().catch(()=>({detail:'Не удалось получить ответ сервера'}));throw Error(typeof d.detail==='string'?d.detail:'Проверьте заполненные поля');}
  return response.json();
}
function toast(text) {$('#toast').textContent=text;$('#toast').style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').style.display='none',5500);}
function safeURL(value) {try {const u=new URL(value);return ['https:','http:'].includes(u.protocol)?esc(u.href):'#';}catch{return '#';}}
function host(value) {try{return new URL(value).hostname;}catch{return 'Источник';}}
function dateText(value) {return new Date(value).toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'});}
function filtered() {const q=state.filter.toLocaleLowerCase('ru');return state.records.filter(r=>(!r.blocked||$('#show-blocked').checked)&&`${r.name} ${r.developer} ${r.region} ${r.summary}`.toLocaleLowerCase('ru').includes(q));}
function renderRecords() {
  const all=filtered(), pages=Math.max(1,Math.ceil(all.length/10));state.page=Math.min(state.page,pages);
  $('#count').textContent=state.records.length;
  $('#filter-count').textContent=state.filter?`Найдено: ${all.length}`:'';
  $('#data-note').textContent={imported:'Из ваших таблиц. Сведения ещё нужно сверить с источниками.',demo:'Вымышленные примеры — попробуйте весь сценарий.',web:'Найденные публикации. Выводы требуют проверки первоисточников.'}[state.dataset];
  $('#records').innerHTML=all.slice((state.page-1)*10,state.page*10).map(r=>`<article class="record"><div><button class="record-name" data-open="${esc(r.id)}">${esc(r.name)}${r.building?' · '+esc(r.building):''}</button><p class="record-meta">${esc(r.developer)}<br>${esc(r.region)}</p></div><div class="signal-wrap"><p class="signal">${esc(r.summary)}</p><span class="tag">${r.dataset==='demo'?'Демо-пример':r.dataset==='web'?'Сигнал из поиска':'Из материалов агентства'}</span></div><div class="record-actions">${r.blocked?'<span class="tag">В стоп-листе</span>':`<button class="button" data-pdf="${esc(r.id)}">↓ PDF-разбор</button>`}<button class="more" data-open="${esc(r.id)}">Подробнее и чаты</button></div></article>`).join('') || `<div class="empty"><h3>${state.filter?'Ничего не найдено':'Здесь появятся найденные объекты'}</h3><p>${state.filter?'Попробуйте другое название ЖК или девелопера.':'Выберите регион и нажмите «Найти объекты». Для первого поиска добавьте ключи в подключениях.'}</p>${state.filter?'':'<button class="button" data-action="settings">Настроить подключения</button>'}</div>`;
  $('#pagination').innerHTML=pages>1?`<button class="button quiet" data-action="prev" ${state.page===1?'disabled':''}>← Назад</button><span>${state.page} из ${pages}</span><button class="button quiet" data-action="next" ${state.page===pages?'disabled':''}>Далее →</button>`:'';
  applyRole();
}
async function refresh() {const ticket=++refreshId;const dataset=state.dataset;const records=await api('/records?dataset='+dataset);if(ticket!==refreshId)return;state.records=records.filter(r=>r.granularity!=='developer');$('#dataset').value=dataset;renderRecords();applyRole();}
async function chooseDataset(dataset) {state.dataset=dataset;state.page=1;state.filter='';$('#filter').value='';localStorage.setItem('megapolis-dataset',dataset);await refresh();}
function chatHTML(stats) {
  if (!stats) return '<p>Добавьте выгрузку открытого чата, чтобы увидеть частые темы и уточнить разбор.</p>';
  return `<div class="chat-stats"><span><b>${stats.messages_30d}</b>сообщений за 30 дней</span><span><b>${stats.authors_30d}</b>участников</span><span><b>${stats.negative_share===null?'—':stats.negative_share+'%'}</b>негативных из размеченных</span></div><div class="topics">${stats.topics.slice(0,5).map(([t,n])=>`<span>${esc(t.replaceAll('_',' '))} · ${n}</span>`).join('')}</div><p class="caption">${esc(stats.method)}</p>`;
}
function researchHTML(dossier) {
  if (!dossier?.signals?.length) return '';
  return `<section class="research section"><h3>Что нашли в публикациях</h3><p class="muted">Прочитано страниц: ${dossier.full_pages}. Только фрагменты выдачи: ${dossier.snippets}.${dossier.old_event?' Последнее датированное событие старше 90 дней.':''}</p><div class="research-timeline">${dossier.signals.map(signal=>`<div><span class="muted">${esc(signal.event_date || (signal.published_at ? signal.published_at+' · дата публикации' : 'Дата события не установлена'))}</span><strong>${esc(signal.kind_label)}${signal.building?' · '+esc(signal.building):''}</strong><p>${esc(signal.summary)}</p><a href="${safeURL(signal.source_url)}" target="_blank" rel="noopener noreferrer">${esc(signal.source_title)} ↗</a> <span class="tag">${signal.source_kind==='page'?'Текст страницы':'Фрагмент поиска'}</span></div>`).join('')}</div><p class="caption">${esc(dossier.limitation)}</p><details><summary>Сохранённые источники</summary>${dossier.sources.map(source=>`<div class="saved-source"><button class="button quiet" data-source="${esc(source.id)}">Читать сохранённый текст</button><span class="muted">${source.fetched_at?dateText(source.fetched_at):''}${source.truncated?' · текст ограничен по объёму':''}</span><div id="source-${esc(source.id)}"></div></div>`).join('')}</details></section>`;
}
let pollTimer;
let activeJob = null;
const terminalJobs = new Set(['completed','partial','failed','cancelled','interrupted']);
function renderJob(job) {
  activeJob=job;
  if (!job) return;
  const running=!terminalJobs.has(job.status);
  $('#find-button').disabled=running;
  const demo=job.mode==='demo'?'Учебный прогон · без сети и расходов. ':'';
  let message=demo+job.detail;
  if (job.cancel_requested && running) message=demo+'Останавливаем после текущего запроса…';
  const r=job.result;
  if(r) message=demo+`${job.status==='partial'?'Частичный результат':'Готово'}: ${r.found} объектов, ${r.added} новых карточек, ${r.signals_added} новых публикаций. Прочитано страниц: ${r.full_pages}.${r.shortlist?` Отобрано для проверки: ${r.shortlist.length}.`:""}`;
  if(job.error) message=demo+job.error;
  const warnings=r?.errors?.length?`<details><summary>Не все источники обработаны (${r.errors.length})</summary>${r.errors.slice(0,6).map(e=>`<p>${esc(e.detail)}${e.url?' · '+esc(host(e.url)):''}</p>`).join('')}</details>`:'';
  const limits=r&&(r.skipped_pages||r.skipped_batches)?`<p class="muted">Лимит прогона: отложено страниц ${r.skipped_pages}, групп текстов ${r.skipped_batches}. Это ограниченный обзор.</p>`:'';
  $('#scan-status').innerHTML=`<div class="job-status ${job.status==='failed'?'error':''}"><div class="job-top"><span>${running?'<span class="spinner"></span>':''}${esc(message)}</span>${running?`<button class="button quiet" data-action="cancel-scan" ${job.cancel_requested?'disabled':''}>Остановить</button>`:['failed','interrupted','cancelled'].includes(job.status)?'<button class="button" data-action="retry-scan">Повторить</button>':`<button class="button quiet" data-action="show-scan">Показать объекты</button>`}</div>${running?`<progress value="${job.percent}" max="100" aria-label="Прогресс поиска"></progress><p class="muted">${job.percent}% · Можно закрыть страницу и вернуться. Приложение должно оставаться запущенным.</p>`:''}${warnings}${limits}</div>`;
}
async function watchJob(id) {
  clearTimeout(pollTimer);
  try {
    const job=await api('/jobs/'+id);renderJob(job);
    if(!terminalJobs.has(job.status)) pollTimer=setTimeout(()=>watchJob(id),1200);
    else if(job.result) await chooseDataset(job.result.dataset);
  } catch(err) {
    $('#scan-status').innerHTML='<div class="notice">Связь с приложением прервалась. Повторно проверяю состояние поиска…</div>';
    pollTimer=setTimeout(()=>watchJob(id),3000);
  }
}
async function startScan(mode='live', request=null) {
  const job=await api('/search',{method:'POST',body:JSON.stringify(request || {region:$('#search-region').value,period:$('#search-period').value,mode,enrich:mode==='live'})});
  renderJob(job);await watchJob(job.id);
}
async function openRecord(id) {
  const r=await api('/records/'+id);state.selected=r;
  const chats=await api('/chats?dataset='+r.dataset);const stats=chats.find(c=>c.record_id===id);
  const topics=[...new Set(state.offers.flatMap(o=>o.pain_topics))].sort();
  $('#detail-content').innerHTML=`<div class="dialog-header"><button class="close" data-action="close-detail" aria-label="Закрыть карточку">×</button><span class="eyebrow">${r.dataset==='demo'?'ДЕМО-ПРИМЕР':'РАЗБОР ОБЪЕКТА'}</span><h2 id="detail-title">${esc(r.name)}${r.building?' · '+esc(r.building):''}</h2><p>${esc(r.developer)} · ${esc(r.region)}</p></div><div class="dialog-body"><div class="section"><h3>Что происходит</h3><div class="detail-signal">${esc(r.summary)}</div><div class="source-links">${r.urls.slice(0,5).map((url,i)=>`<a href="${safeURL(url)}" target="_blank" rel="noopener noreferrer">${i+1}. ${esc(host(url))} ↗</a>`).join('')||'<span class="muted">В этом демо-примере нет реальных источников</span>'}</div></div>${researchHTML(r.dossier)}<div class="pdf-action"><button class="button primary" data-pdf="${esc(r.id)}">↓ Получить PDF-разбор</button><p>Подробный отчёт: ситуация, схемы,<br>продукты под ЖК и план на 30 дней.</p></div><div id="pdf-result"></div><section class="chat-box"><h3>Уточнить по чатам <span class="status-label">По желанию</span></h3>${chatHTML(stats)}<div id="telegram-links"></div><div class="chat-actions"><button class="button" data-action="chat-import">Добавить выгрузку чата</button>${r.dataset==='demo'?'<button class="button quiet" data-action="chat-demo">Попробовать на примере</button>':''}<a href="/api/chats/example" download="chat-example.json">Образец JSON</a></div><p class="caption">Только открытые чаты. Вход в рабочий аккаунт — в «Подключениях». Для тематического разбора нажмите «Разобрать сообщения моделью». Результат модели требует проверки.</p>${stats?'<button class="button primary" data-pdf="'+esc(r.id)+'">Обновить PDF с данными чата</button> <button class="button" data-op="analyze-chat">Разобрать сообщения моделью</button>':''}</section><details><summary>Уточнить ситуацию и предложение</summary><form id="decision-form"><div class="form-grid"><label class="field">Тип ситуации<select name="situation">${['сроки','приёмка','после_заселения'].map(s=>`<option value="${s}" ${r.situation===s?'selected':''}>${s.replaceAll('_',' ')}</option>`).join('')}</select></label><label class="field">Стадия<select name="stage">${['Ранний риск','Первые жалобы','Открытый конфликт','Эскалация','Восстановление'].map((s,i)=>`<option value="${i}" ${r.stage===i?'selected':''}>${s}</option>`).join('')}</select></label></div><label class="field">Ваше уточнение<textarea name="note" maxlength="1500" placeholder="Что важно учесть при подготовке предложения">${esc(r.note)}</textarea></label><div class="muted">Какие проблемы обсуждают</div><div class="checks">${topics.map(t=>`<label><input type="checkbox" name="topic" value="${esc(t)}" ${r.topics.includes(t)?'checked':''}>${esc(t.replaceAll('_',' '))}</label>`).join('')}</div><label class="field">Статус работы<select name="status">${Object.entries(labels).map(([k,v])=>`<option value="${k}" ${r.status===k?'selected':''}>${v}</option>`).join('')}</select></label><label class="field">Причина отказа, если есть<input name="refusal_reason" maxlength="500" value="${esc(r.refusal_reason||'')}"></label><button class="button" type="submit">Сохранить уточнения</button></form></details><details><summary>Источники и что проверить</summary><p>${esc(r.check)}</p><p class="muted">${esc(r.file)} · ${esc(r.sheet)} · ${esc(r.row)}</p>${r.urls.map(url=>`<p><a target="_blank" rel="noopener noreferrer" href="${safeURL(url)}">${esc(host(url))} ↗</a></p>`).join('')}<div class="proof">${r.evidence.map(e=>`<blockquote>«${esc(e.quote)}»<br><a href="${safeURL(e.url)}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a></blockquote>`).join('')}</div><form id="proof-form"><h3>Добавить подтверждение</h3><label class="field">Название источника<input name="title" required maxlength="160"></label><label class="field">Ссылка<input name="url" type="url" required></label><label class="field">Текст источника<textarea name="source_text" required maxlength="100000"></textarea></label><label class="field">Дословная цитата<textarea name="quote" required maxlength="500"></textarea></label><button class="button" type="submit">Сохранить цитату</button></form><details><summary>Все поля исходной строки</summary><dl class="raw">${Object.entries(r.claims).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl></details></details><details><summary>История изменений</summary><div class="timeline">${r.events.map(e=>`<div>${dateText(e.created_at)} · ${esc(e.detail)}</div>`).join('')||'Изменений пока нет'}</div></details></div>`;
  await loadTelegramLinks(id);
  await loadCompletion(id);
  if (!$('#detail').open) $('#detail').showModal();
}
const connectionFields=[
 {title:'Поиск в интернете',hint:'Yandex находит публикации по ЖК и девелоперам.',keys:[['YANDEX_SEARCH_API_KEY','API-ключ Yandex','password'],['YANDEX_FOLDER_ID','Идентификатор каталога (Folder ID)','text']]},
 {title:'Анализ найденного',hint:'OpenRouter подключает выбранную модель для выделения объектов и цитат.',keys:[['OPENROUTER_API_KEY','API-ключ OpenRouter','password']]},
 {title:'Telegram',hint:'После сохранения ключей откройте «Вход в Telegram» или настройте недельный дайджест ниже.',keys:[['TG_API_ID','API ID приложения Telegram','text'],['TG_API_HASH','API Hash','password'],['TG_BOT_TOKEN','Токен бота для уведомлений','password'],['TG_GROUP_ID','ID группы для дайджеста','text']]},
 {title:'Сравнение поисковиков',hint:'Дополнительные API для сравнения на одинаковых русскоязычных запросах.',keys:[['PERPLEXITY_API_KEY','API-ключ Perplexity Search','password'],['BRAVE_SEARCH_API_KEY','API-ключ Brave Search','password']]},
 {title:'Дополнительные источники',hint:'Необязательно. Работодатель, открытая группа и ИНН указываются в карточке объекта.',keys:[['VK_SERVICE_TOKEN','Токен VK API для чтения','password'],['HH_USER_AGENT','hh.ru: название приложения и рабочий email','text']]},
 {title:'Внешние копии и рабочая почта',hint:'Необязательно. Отправки включаются отдельно ниже. S3 хранит зашифрованную базу; ключ шифрования сохраните в менеджере паролей.',keys:[['SMTP_HOST','SMTP-сервер (TLS, порт 465)','text'],['SMTP_USERNAME','Логин почты','text'],['SMTP_PASSWORD','Пароль приложения почты','password'],['SMTP_FROM','Адрес отправителя','text'],['SMTP_TO','Рабочие адреса получателей через запятую','text'],['BACKUP_REGION','Регион AWS S3','text'],['BACKUP_BUCKET','S3 bucket','text'],['BACKUP_ACCESS_KEY','S3 Access Key','password'],['BACKUP_SECRET_KEY','S3 Secret Key','password'],['BACKUP_ENCRYPTION_KEY','Ключ Fernet для шифрования копий','password']]},
 {title:'Google Sheets',hint:'Сохраните ID таблицы; JSON ключа загрузите в разделе синхронизации ниже.',keys:[['SHEET_ID','ID таблицы','text']]}
];
async function openSettings() {
  const settings=await api('/settings');
  function section(group) {return `<section class="connections-section"><h3>${group.title}<span class="status-label ${group.keys.every(([k])=>settings.fields[k])?'ready':''}">${group.keys.every(([k])=>settings.fields[k])?'Сохранено':'Не заполнено'}</span></h3><p>${group.hint}</p>${group.keys.map(([key,label,type])=>`<label class="field">${label}<input name="${key}" type="${type}" autocomplete="off" spellcheck="false" placeholder="${settings.fields[key]?'Уже сохранено · введите для замены':'Вставьте значение'}" maxlength="2000"></label>`).join('')}</section>`;}
  $('#connections-content').innerHTML=`<div class="dialog-header"><button class="close" data-action="close-settings" aria-label="Закрыть подключения">×</button><span class="eyebrow">НАСТРОЙКА СЕРВИСОВ</span><h2 id="connections-title">Подключения</h2><p>Добавьте ключи один раз — они сохранятся в рабочем приложении.</p></div><div class="dialog-body"><form id="settings-form">${section(connectionFields[0])}${section(connectionFields[1])}<label class="field">Модель для анализа<input name="model" value="${esc(settings.model)}" placeholder="Идентификатор модели в OpenRouter" maxlength="150"></label><label class="field">Модель автора предложения<input name="author_model" value="${esc(settings.author_model)}" placeholder="ID модели OpenRouter" maxlength="150"></label><label class="field">Отдельная модель проверки<input name="review_model" value="${esc(settings.review_model)}" placeholder="ID другой модели OpenRouter" maxlength="150"></label><p class="caption">Модель можно сменить без переделки проекта. Запросы к сервисам тарифицируются по вашим аккаунтам.</p><details><summary>Дополнительные подключения: Telegram, поиск и таблицы</summary>${connectionFields.slice(2).map(section).join('')}</details><div class="settings-foot"><button class="button primary" type="submit">Сохранить подключения</button><p>Пустые поля сохраняют прежние значения.<br>Сохранение ключей не запускает платных запросов.</p></div><div id="settings-status" class="form-status" role="status"></div></form><div id="operations-content"></div></div>`;
  await loadOperations();
  await loadCompletionSettings();
  if (!$('#connections').open) $('#connections').showModal();
}
async function saveDecision() {
  const form=new FormData($('#decision-form'));const r=state.selected;
  const saved=await api('/records/'+r.id,{method:'PUT',body:JSON.stringify({revision:r.revision,status:form.get('status'),note:form.get('note'),stage:Number(form.get('stage')),situation:form.get('situation'),topics:form.getAll('topic'),refusal_reason:form.get('refusal_reason')})});
  state.selected=saved;return saved;
}
async function pdf(id,button) {
  const old=button.textContent;button.disabled=true;button.textContent='Готовлю PDF…';
  try {
    if ($('#detail').open && state.selected?.id===id && $('#decision-form')) await saveDecision();
    const brief=await api('/records/'+id+'/brief',{method:'POST'});
    const link=document.createElement('a');link.href='/api/briefs/'+brief.id+'/pdf';link.download='Мегаполис — разбор объекта.pdf';document.body.append(link);link.click();link.remove();
    if ($('#detail').open) $('#pdf-result').innerHTML=`<p class="pdf-link">Версия ${brief.version} готова. <a href="/api/briefs/${brief.id}/pdf" target="_blank">Открыть PDF ↗</a></p>`;
    if($('#detail').open){$('#completion-card')?.remove();await loadCompletion(id);}
    toast('PDF-разбор готов и сохранён');
  } finally {button.disabled=false;button.textContent=old;}
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('[data-action],[data-open],[data-pdf],[data-source]');if(!b)return;
  try {
    if(b.dataset.source){const source=await api('/sources/'+b.dataset.source);$('#source-'+b.dataset.source).innerHTML=`<pre class="source-text">${esc(source.text)}</pre>`;return;}
    if(b.dataset.open){await openRecord(b.dataset.open);return;}
    if(b.dataset.pdf){await pdf(b.dataset.pdf,b);return;}
    const action=b.dataset.action;
    if(action==='settings')return await openSettings();
    if(action==='close-detail')return $('#detail').close();
    if(action==='close-settings')return $('#connections').close();
    if(action==='prev'||action==='next'){state.page+=action==='prev'?-1:1;renderRecords();return;}
    if(action==='import')return $('#excel-file').click();
    if(action==='chat-import')return $('#chat-file').click();
    if(action==='chat-demo'){b.disabled=true;const batch=await api('/chats/example');batch.record_id=state.selected.id;await api('/chats/import',{method:'POST',body:JSON.stringify(batch)});await openRecord(state.selected.id);toast('Учебная переписка добавлена');return;}
    if(action==='demo-scan'){await startScan('demo');return;}
    if(action==='cancel-scan'){renderJob(await api('/jobs/'+activeJob.id+'/cancel',{method:'POST'}));return;}
    if(action==='retry-scan'){await startScan(activeJob.mode,{...activeJob.request,mode:activeJob.mode});return;}
    if(action==='show-scan'){await chooseDataset(activeJob.result.dataset);return;}
    if(action==='scan'){
      const settings=await api('/search/readiness');
      if(!settings.ready){if(identity.role!=='admin'){toast('Попросите администратора настроить поиск в подключениях');return;}$('#scan-status').innerHTML='<div class="notice">Для поиска нужны Yandex и OpenRouter. Добавьте ключи — и можно запускать.<button class="button" data-action="settings">Подключить</button></div>';await openSettings();return;}
      await startScan();
    }
  } catch(err) {toast(err.message);if(b.dataset.action==='scan')$('#scan-status').innerHTML=`<div class="notice error">${esc(err.message)}</div>`;}
  finally{b.disabled=false;if(activeJob)$('#find-button').disabled=!terminalJobs.has(activeJob.status);}
});
document.addEventListener('submit',async e=>{
  e.preventDefault();const b=e.target.querySelector('[type="submit"]');if(!b)return;b.disabled=true;
  try {
    if(e.target.id==='settings-form'){
      const form=new FormData(e.target);const values=Object.fromEntries([...form].filter(([k,v])=>!['model','review_model','author_model'].includes(k)&&v.trim()).map(([k,v])=>[k,v.trim()]));
      await api('/settings',{method:'PUT',body:JSON.stringify({values,model:form.get('model').trim(),review_model:form.get('review_model').trim(),author_model:form.get('author_model').trim()})});
      await openSettings();$('#settings-status').textContent='Подключения сохранены. Можно закрыть это окно и запустить поиск.';toast('Ключи сохранены');
    }
    if(e.target.id==='decision-form'){const r=await saveDecision();await openRecord(r.id);await refresh();toast('Уточнения сохранены');}
    if(e.target.id==='proof-form'){await api('/records/'+state.selected.id+'/evidence',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});await openRecord(state.selected.id);toast('Цитата сохранена');}
  }catch(err){toast(err.message);if(e.target.id==='settings-form')$('#settings-status').textContent=err.message;}
  finally{b.disabled=false;}
});
$('#filter').addEventListener('input',e=>{state.filter=e.target.value;state.page=1;renderRecords();});
$('#dataset').addEventListener('change',async e=>{try{await chooseDataset(e.target.value);}catch(err){toast(err.message);}});
$('#excel-file').addEventListener('change',async e=>{try{const file=e.target.files[0];if(!file)return;const data=new FormData();data.append('file',file);const result=await api('/import',{method:'POST',body:data});await chooseDataset('imported');toast(result.found?`Добавлено карточек: ${result.added}. Повторы пропущены.`:'Структура таблицы не распознана');}catch(err){toast(err.message);}finally{e.target.value='';}});
$('#chat-file').addEventListener('change',async e=>{try{const file=e.target.files[0];if(!file)return;if(file.size>10*1024*1024)throw Error('Максимальный размер — 10 МБ');const batch=JSON.parse(await file.text());batch.record_id=state.selected.id;const result=await api('/chats/import',{method:'POST',body:JSON.stringify(batch)});await openRecord(state.selected.id);toast('Добавлено сообщений: '+result.added);}catch(err){toast(err.message);}finally{e.target.value='';}});
for(const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click',e=>{if(e.target===dialog){const rect=dialog.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)dialog.close();}});
(async()=>{try{identity=await api('/session');if(identity.mode==='server')$('#logout-button').hidden=false;state.offers=(await api('/offers')).offers;await refresh();const last=await api('/jobs/latest');if(last){renderJob(last);if(!terminalJobs.has(last.status))await watchJob(last.id);}}catch(err){$('#records').innerHTML=`<div class="empty">Не удалось загрузить данные: ${esc(err.message)}</div>`;}})();

$('#show-blocked').addEventListener('change',()=>{state.page=1;renderRecords();applyRole();});
