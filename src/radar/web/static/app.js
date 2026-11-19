const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {new:'Новый',review:'На проверке',inwork:'В работе',proposal:'КП отправлено',meeting:'Встреча',closed:'Закрыт',later:'Отложен',rejected:'Не интересно'};
const reportOrder=['ready','manual','hold','retry','partial','watch','outdated','none','hidden'];
const cardOrder=[...Object.keys(labels).filter(status=>status!=='rejected'),'unknown','hidden'];
const sortControl=$('#record-sort');
const savedSort=localStorage.getItem('megapolis-record-sort');
if([...sortControl.options].some(option=>option.value===savedSort))sortControl.value=savedSort;
let state = {dataset:localStorage.getItem('megapolis-dataset') || 'web',records:[],offers:[],selected:null,page:1,filter:'',runIds:null,sort:sortControl.value};
if (!['web','imported','demo'].includes(state.dataset)) state.dataset = 'web';
let refreshId = 0;
async function api(path, options={}) {
  const response = await fetch('/api'+path, {...options,headers:{'X-Radar-Local':'1',...(options.body && !(options.body instanceof FormData) ? {'Content-Type':'application/json'} : {}),...options.headers}});
  if (response.status===401) {location.reload();throw Error('Сессия завершена');}
  if (!response.ok) {const d=await response.json().catch(()=>({detail:'Не удалось получить ответ сервера'}));const error=Error(typeof d.detail==='string'?d.detail:'Проверьте заполненные поля');error.status=response.status;throw error;}
  return response.json();
}
function toast(text) {$('#toast').textContent=text;$('#toast').style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').style.display='none',5500);}
function safeURL(value) {try {const u=new URL(value);return ['https:','http:'].includes(u.protocol)?esc(u.href):'#';}catch{return '#';}}
function host(value) {try{return new URL(value).hostname;}catch{return 'Материал';}}
function dateText(value) {return new Date(value).toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'});}
function isHidden(r) { return Boolean(r.blocked) || r.status==='rejected'; }
function hideButton(r) {
  if(r.blocked?.kind==='developer')return '<span class="caption">Скрыт по застройщику</span>';
  return `<button class="more" data-disposition="${esc(r.id)}" data-restore="${isHidden(r)?'1':'0'}">${isHidden(r)?'Вернуть в работу':'Не берём в работу'}</button>`;
}
function coverageLabel(r) {
  const c=r.source_coverage;if(!c||r.dataset==='demo')return '';
  return `<span class="caption" title="${esc(c.note)}">${esc(c.label)} · публикаций: ${c.publications} · сайтов: ${c.sites}</span>`;
}
function renderRecord(record) {
  const id = esc(record.id);
  const title = esc(record.name) + (record.building ? ' · ' + esc(record.building) : '');
  const provenance = record.dataset === 'demo' ? 'Демо-пример'
    : record.verification === 'research_lead' ? 'Исходный объект'
    : record.dataset === 'web' ? 'Сигнал из поиска' : 'Материалы агентства';
  const hidden = isHidden(record);
  const status = hidden ? 'Скрыт' : labels[record.status];
  const showStatus = hidden || (state.sort !== 'card' && record.status !== 'new');
  const heading=state.sort==='priority'?'h3':'h4';
  return `<article class="record" tabindex="0" data-card="${id}" aria-label="Открыть карточку ${esc(record.name)}">
    <header class="record-header">
      <div>
        <${heading}><button class="record-name" data-open="${id}">${title}</button></${heading}>
        <p class="record-meta">${[record.developer, record.region].filter(Boolean).map(value =>
          `<span>${esc(value)}</span>`).join('')}</p>
      </div>
      ${showStatus && status ? `<span class="record-state">${esc(status)}</span>` : ''}
    </header>
    <div class="record-body">
      <div class="signal-wrap detail-signal"><p class="signal">${esc(record.summary)}</p></div>
      <div class="record-source">
        <span class="tag">${provenance}</span>
        ${record.possible_duplicates?.length ? '<span class="tag">Возможный дубль</span>' : ''}
        ${coverageLabel(record)}
      </div>
    </div>
    <div class="record-actions">
      <button class="button record-open" data-open="${id}">Открыть разбор <span aria-hidden="true">→</span></button>
      ${hidden ? '' : `<button class="button" data-pdf="${id}"><span aria-hidden="true">↓</span> PDF-разбор</button>`}
      ${hideButton(record)}
    </div>
  </article>`;
}
function filtered() {const q=state.filter.toLocaleLowerCase('ru');return state.records.filter(r=>(!state.runIds||state.runIds.includes(r.id))&&(!isHidden(r)||$('#show-blocked').checked)&&`${r.name} ${r.developer} ${r.region} ${r.summary}`.toLocaleLowerCase('ru').includes(q));}

function recordGroup(record) {
  if(isHidden(record))return {code:'hidden',label:'Скрытые'};
  if(state.sort==='report'){
    return record.report_status || {code:'none',label:'Разбор не начат'};
  }
  return Object.hasOwn(labels,record.status)
    ? {code:record.status,label:labels[record.status]}
    : {code:'unknown',label:'Без статуса'};
}

function renderRecordPage(records) {
  const start=(state.page-1)*10, end=start+10;
  if(state.sort==='priority')return records.slice(start,end).map(renderRecord).join('');
  const order=state.sort==='report'?reportOrder:cardOrder;
  const sorted=records.map(record=>({record,...recordGroup(record)})).sort(
    (a,b)=>order.indexOf(a.code)-order.indexOf(b.code)
  );
  const groups=new Map();
  for(const {record,code,label} of sorted.slice(start,end)){
    if(!groups.has(code))groups.set(code,{label,records:[]});
    groups.get(code).records.push(record);
  }
  return [...groups].map(([code,group])=>`<section class="record-group" data-status="${esc(code)}" aria-labelledby="record-group-${esc(code)}">
    <h3 id="record-group-${esc(code)}">${esc(group.label)}</h3>
    <div class="record-grid">${group.records.map(renderRecord).join('')}</div>
  </section>`).join('');
}

function renderRecords() {
  const all=filtered(), pages=Math.max(1,Math.ceil(all.length/10));state.page=Math.min(state.page,pages);
  $('#count').textContent=all.length;
  $('#hidden-count').textContent=state.records.filter(isHidden).length;
  $('#filter-count').textContent=state.filter?`Найдено: ${all.length}`:'';
  $('#data-note').textContent={imported:'Из ваших таблиц. Сведения ещё нужно сверить с источниками.',demo:'Вымышленные примеры — попробуйте весь сценарий.',web:'Найденные публикации. Выводы требуют проверки первоисточников.'}[state.dataset];
  $('#records').innerHTML=renderRecordPage(all) || `<div class="empty"><h3>${state.filter?'Ничего не найдено':state.records.some(isHidden)?'В этом списке нет активных объектов':'Здесь появятся найденные объекты'}</h3><p>${state.filter?'Попробуйте другое название ЖК или девелопера.':state.records.some(isHidden)?'Включите «Показать скрытые», чтобы вернуть объект.':'Выберите регион и нажмите «Найти объекты». Для первого поиска добавьте ключи в подключениях.'}</p>${state.filter?'':'<button class="button" data-action="settings">Настроить подключения</button>'}</div>`;
  $('#pagination').innerHTML=pages>1?`<button class="button quiet" data-action="prev" ${state.page===1?'disabled':''}>← Назад</button><span>${state.page} из ${pages}</span><button class="button quiet" data-action="next" ${state.page===pages?'disabled':''}>Далее →</button>`:'';
  renderDevelopers();
  applyRole();
}

function updateListReportStatus(id,brief) {
  const status=brief?.report_status;
  const record=state.records.find(row=>row.id===id);
  if(!status || !record || (record.report_status?.code===status.code && record.report_status?.label===status.label))return;
  record.report_status=status;
  if(state.sort==='report')renderRecords();
}
async function refresh() {
  const ticket=++refreshId, dataset=state.dataset, runKey=JSON.stringify(state.runIds);
  const records=await api('/records?dataset='+dataset);
  if(ticket!==refreshId || dataset!==state.dataset || runKey!==JSON.stringify(state.runIds))return;
  if(state.runIds){
    const present=new Set(records.map(record=>record.id));
    const missing=[...new Set(state.runIds)].filter(id=>!present.has(id));
    const extras=await Promise.allSettled(missing.map(id=>api('/records/'+id)));
    if(ticket!==refreshId || dataset!==state.dataset || runKey!==JSON.stringify(state.runIds))return;
    const aliases=new Map();
    extras.forEach((result,index)=>{
      if(result.status!=='fulfilled')return;
      const record=result.value;
      aliases.set(missing[index],record.id);
      if(!present.has(record.id)){records.push(record);present.add(record.id);}
    });
    state.runIds=[...new Set(state.runIds.map(id=>aliases.get(id)||id))];
  }
  state.records=records.filter(r=>r.granularity!=='developer' && !r.merged_into);
  $('#dataset').value=dataset;renderRecords();applyRole();
}
async function chooseDataset(dataset,ids=null) {state.runIds=ids;state.dataset=dataset;state.page=1;state.filter='';$('#filter').value='';localStorage.setItem('megapolis-dataset',dataset);await refresh();}
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
  $('#expand-search')?.remove();
  if(!running && job.mode==='live' && job.result && (job.request?.expansion||0)<2){
    const b=document.createElement('button');b.id='expand-search';b.className='button quiet';b.textContent='Расширить поиск · '+((job.request?.expansion||0)===0?'форумы и обращения':'реестры и проверки');
    b.onclick=()=>{b.disabled=true;startScan('live',{...job.request,mode:'live',enrich:true,expansion:(job.request?.expansion||0)+1}).catch(err=>{toast(err.message);b.disabled=false;});};
    $('.results-actions').prepend(b);
  }
  const demo=job.mode==='demo'?'Учебный прогон · без сети и расходов. ':'';
  let message=demo+job.detail;
  if (job.cancel_requested && running) message=demo+'Останавливаем после текущего запроса…';
  const r=job.result;
  if(r) message=demo+`${job.status==='partial'?'Частичный результат':'Готово'}: ${r.found} объектов, ${r.added} новых карточек, ${r.signals_added} новых публикаций. Прочитано страниц: ${r.full_pages}.${r.shortlist?` Отобрано для проверки: ${r.shortlist.length}.`:""}`;
  if(r?.object_research)message+=` Дополнительно проверено ЖК: ${r.object_research.checked}, подробно: ${r.object_research.deepened}${r.object_research.deferred?`, в очереди: ${r.object_research.deferred}`:''}.`;
  if(job.error) message=demo+job.error;
  const warnings=r?.errors?.length?`<details><summary>Не все источники обработаны (${r.errors.length})</summary>${r.errors.slice(0,6).map(e=>`<p>${esc(e.detail)}${e.url?' · '+esc(host(e.url)):''}</p>`).join('')}</details>`:'';
  const chat=r?.chat_screening;
  const chatSummary=chat?`<p class="muted">${esc(chat.note)}${chat.records.length?` · Прочитано сообщений: ${chat.records.reduce((n,x)=>n+x.chats.reduce((s,c)=>s+c.read,0),0)} · размечено: ${chat.records.reduce((n,x)=>n+x.labelled,0)}.`:''}</p>${chat.records.length?`<details><summary>Что получилось по чатам</summary>${chat.records.map(x=>`<p><b>${esc(x.name)}</b> · ${x.chats.length?'чат прочитан':'подходящий чат не прочитан'} · размечено ${x.labelled}${x.analysis_disabled?' · модельный анализ выключен':''}${x.more_unlabelled?' · остались неразмеченные сообщения':''}${x.chats.some(c=>c.partial)?' · история прочитана частично':''}</p>${x.skipped.map(c=>`<p class="caption">${esc(c.reason)}</p>`).join('')}`).join('')}</details>`:''}`:'';
  const coverage=r?.coverage;
  const coverageHTML=coverage?`<p class="caption">Охват: ${coverage.regions.length} регионов · ${coverage.queries_completed} поисковых запросов · ${coverage.developers.length} строк ЕРЗ. ${esc(coverage.note)}</p>`:'';
  const brand=r?.brand_analytics;
  const brandSummary=brand?.publications?`<p class="caption">Из Brand Analytics · публикаций ${esc(brand.publications)} · карточек ${esc(brand.records)}</p>`:'';
  const perplexity = r?.perplexity;
  const perplexitySummary = perplexity && (perplexity.queries_completed || perplexity.errors?.length)
    ? `<p class="caption">Perplexity · новых ссылок ${esc(perplexity.unique_urls ?? 0)}
      · прочитано страниц ${esc(perplexity.full_pages ?? 0)}${perplexity.errors?.length ? ' · часть запросов недоступна' : ''}</p>`
    : '';
  const limits=r&&(r.skipped_pages||r.skipped_batches)?`<p class="muted">Лимит прогона: отложено страниц ${r.skipped_pages}, групп текстов ${r.skipped_batches}. Это ограниченный обзор.</p>`:'';
  $('#scan-status').innerHTML=`<div class="job-status ${job.status==='failed'?'error':''}"><div class="job-top"><span>${running?'<span class="spinner"></span>':''}${esc(message)}</span>${running?`<button class="button quiet" data-action="cancel-scan" ${job.cancel_requested?'disabled':''}>Остановить</button>`:['failed','interrupted','cancelled'].includes(job.status)?'<button class="button" data-action="retry-scan">Повторить</button>':`<button class="button quiet" data-action="show-scan">Показать объекты</button>`}</div>${running?`<progress value="${job.percent}" max="100" aria-label="Прогресс поиска"></progress><p class="muted">${job.percent}% · Можно закрыть страницу и вернуться. Приложение должно оставаться запущенным.</p>`:''}${coverageHTML}${perplexitySummary}${brandSummary}${warnings}${chatSummary}${limits}</div>`;
}
async function watchJob(id) {
  clearTimeout(pollTimer);
  try {
    const job=await api('/jobs/'+id);renderJob(job);
    if(!terminalJobs.has(job.status)) pollTimer=setTimeout(()=>watchJob(id),1200);
    else if(job.result) await chooseDataset(job.result.dataset,job.result.record_ids);
  } catch(err) {
    $('#scan-status').innerHTML='<div class="notice">Связь с приложением прервалась. Повторно проверяю состояние поиска…</div>';
    pollTimer=setTimeout(()=>watchJob(id),3000);
  }
}
async function startScan(mode='live', request=null) {
  const job=await api('/search',{method:'POST',body:JSON.stringify(request || {region:$('#search-region').value,period:$('#search-period').value,target:$('#search-target').value.trim(),mode,enrich:mode==='live'})});
  renderJob(job);await watchJob(job.id);
}
let detailRequest=0;
let reportPollTimer;
let reportWatchGeneration=0;
let detailIntelligence;
function objectIntelligence(id) {
  if(!detailIntelligence || detailIntelligence.id!==id) detailIntelligence={id,promise:api('/records/'+id+'/intelligence')};
  return detailIntelligence.promise;
}
async function openRecord(id) {
  const request=++detailRequest;
  clearTimeout(reportPollTimer);reportWatchGeneration++;
  detailIntelligence=null;
  state.selected={id};
  $('#detail-content').innerHTML='<div class="dialog-header"><button class="close" data-action="close-detail" aria-label="Закрыть карточку">×</button><h2 id="detail-title">Открываем объект</h2></div><div class="dialog-body" role="status">Загружаем сохранённые сведения…</div>';
  if (!$('#detail').open) $('#detail').showModal();
  let r;
  try { r=await api('/records/'+id); }
  catch(err) { if(request===detailRequest && $('#detail').open) $('#detail-content .dialog-body').textContent=err.message; return; }
  if(request!==detailRequest || !$('#detail').open)return;
  id=r.id;
  state.selected=r;
  const stats=r.chat_stats?.chats?r.chat_stats:null;
  const topics=[...new Set(state.offers.flatMap(o=>o.pain_topics))].sort();
  $('#detail-content').innerHTML=`<div class="dialog-header"><button class="close" data-action="close-detail" aria-label="Закрыть карточку">×</button><span class="eyebrow">${r.dataset==='demo'?'ДЕМО-ПРИМЕР':'РАЗБОР ОБЪЕКТА'}</span><h2 id="detail-title">${esc(r.name)}${r.building?' · '+esc(r.building):''}</h2><p>${esc(r.developer)} · ${esc(r.region)}</p></div><div class="dialog-body"><div class="disposition-action">${hideButton(r)}</div><div class="section"><h3>Что происходит</h3><div class="detail-signal">${esc(r.summary)}</div><div class="source-links">${r.urls.slice(0,5).map((url,i)=>`<a href="${safeURL(url)}" target="_blank" rel="noopener noreferrer">${i+1}. ${esc(host(url))} ↗</a>`).join('')||'<span class="muted">В этом демо-примере нет реальных источников</span>'}</div></div>${researchHTML(r.dossier)}<div class="pdf-action"><button class="button primary" data-pdf="${esc(r.id)}">↓ Скачать PDF</button><button class="button quiet" data-pdf="${esc(r.id)}" data-regenerate="true">Пересобрать</button><p>Скачивание открывает сохранённую версию.<br>Ситуация, диагноз, сценарии и решения.<br>До шести страниц по новому образцу.</p></div><div id="report-progress"></div><div id="pdf-result"></div><section class="chat-box"><h3>Уточнить по чатам <span class="status-label">По желанию</span></h3>${chatHTML(stats)}<div id="telegram-links"></div><div class="chat-actions"><button class="button" data-action="chat-import">Добавить выгрузку чата</button>${r.dataset==='demo'?'<button class="button quiet" data-action="chat-demo">Попробовать на примере</button>':''}<a href="/api/chats/example" download="chat-example.json">Образец JSON</a></div><p class="caption">Только открытые чаты. Вход в рабочий аккаунт — в «Подключениях». Для тематического разбора нажмите «Разобрать сообщения моделью». Результат модели требует проверки.</p>${stats?'<button class="button primary" data-regenerate="true" data-pdf="'+esc(r.id)+'">Обновить PDF с данными чата</button> <button class="button" data-op="analyze-chat">Разобрать сообщения моделью</button>':''}</section><details><summary>Уточнить ситуацию и предложение</summary><form id="decision-form"><div class="form-grid"><label class="field">Тип ситуации<select name="situation">${['сроки','приёмка','после_заселения'].map(s=>`<option value="${s}" ${r.situation===s?'selected':''}>${s.replaceAll('_',' ')}</option>`).join('')}</select></label><label class="field">Стадия<select name="stage">${['Ранний риск','Первые жалобы','Открытый конфликт','Эскалация','Восстановление'].map((s,i)=>`<option value="${i}" ${r.stage===i?'selected':''}>${s}</option>`).join('')}</select></label></div><label class="field">Ваше уточнение<textarea name="note" maxlength="1500" placeholder="Что важно учесть при подготовке предложения">${esc(r.note)}</textarea></label><div class="muted">Какие проблемы обсуждают</div><div class="checks">${topics.map(t=>`<label><input type="checkbox" name="topic" value="${esc(t)}" ${r.topics.includes(t)?'checked':''}>${esc(t.replaceAll('_',' '))}</label>`).join('')}</div><label class="field">Статус работы<select name="status">${Object.entries(labels).map(([k,v])=>`<option value="${k}" ${r.status===k?'selected':''}>${v}</option>`).join('')}</select></label><label class="field">Причина отказа, если есть<input name="refusal_reason" maxlength="500" value="${esc(r.refusal_reason||'')}"></label><button class="button" type="submit">Сохранить уточнения</button></form></details><details><summary>Источники и что проверить</summary><p>${esc(r.check)}</p><p class="muted">${esc(r.file)} · ${esc(r.sheet)} · ${esc(r.row)}</p>${r.urls.map(url=>`<p><a target="_blank" rel="noopener noreferrer" href="${safeURL(url)}">${esc(host(url))} ↗</a></p>`).join('')}<div class="proof">${r.evidence.map(e=>`<blockquote>«${esc(e.quote)}»<br><a href="${safeURL(e.url)}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a></blockquote>`).join('')}</div><form id="proof-form"><h3>Добавить подтверждение</h3><label class="field">Название источника<input name="title" required maxlength="160"></label><label class="field">Ссылка<input name="url" type="url" required></label><label class="field">Текст источника<textarea name="source_text" required maxlength="100000"></textarea></label><label class="field">Дословная цитата<textarea name="quote" required maxlength="500"></textarea></label><button class="button" type="submit">Сохранить цитату</button></form><details><summary>Все поля исходной строки</summary><dl class="raw">${Object.entries(r.claims).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl></details></details><details><summary>История изменений</summary><div class="timeline">${r.events.map(e=>`<div>${dateText(e.created_at)} · ${esc(e.detail)}</div>`).join('')||'Изменений пока нет'}</div></details></div>`;
  $('#detail-content .pdf-action').insertAdjacentHTML('beforebegin',brandAnalyticsSources(r.brand_analytics));
  watchReport(id);
  // Main content is usable while independent sections load in parallel.
  const compact=compactCard(id);
  const results=await Promise.allSettled([loadTelegramLinks(id),loadCompletion(id).then(()=>compactCard(id)),compact,loadObjectConversation(id)]);
  if(request===detailRequest && $('#detail').open && results.some(r=>r.status==='rejected'))toast('Часть дополнительных разделов не загрузилась. Карточка доступна.');
}
const connectionFields=[
 {title:'Поиск в интернете',hint:'Yandex находит публикации по ЖК и девелоперам.',keys:[['YANDEX_SEARCH_API_KEY','API-ключ Yandex','password'],['YANDEX_FOLDER_ID','Идентификатор каталога (Folder ID)','text']]},
 {title:'Анализ найденного',hint:'OpenRouter подключает выбранную модель для выделения объектов и цитат.',keys:[['OPENROUTER_API_KEY','API-ключ OpenRouter','password']]},
 {title:'Telegram',hint:'После сохранения ключей откройте «Вход в Telegram» или настройте недельный дайджест ниже.',keys:[['TG_API_ID','API ID приложения Telegram','text'],['TG_API_HASH','API Hash','password'],['TG_BOT_TOKEN','Токен бота для уведомлений','password'],['TG_GROUP_ID','ID группы для дайджеста','text']]},
 {title:'Прямой доступ к Perplexity',hint:'Дополнительный ключ для поиска и сравнения поисковиков.',keys:[['PERPLEXITY_API_KEY','API-ключ Perplexity Search','password']]},
 {title:'Сравнение поисковиков',hint:'Brave Search доступен для сравнения на одинаковых русскоязычных запросах.',keys:[['BRAVE_SEARCH_API_KEY','API-ключ Brave Search','password']]},
 {title:'Дополнительные источники',hint:'Необязательно. Работодатель, открытая группа и ИНН указываются в карточке объекта.',keys:[['VK_SERVICE_TOKEN','Токен VK API для чтения','password'],['HH_USER_AGENT','hh.ru: название приложения и рабочий email','text']]},
 {title:'Внешние копии и рабочая почта',hint:'Необязательно. Отправки включаются отдельно ниже. S3 хранит зашифрованную базу; ключ шифрования сохраните в менеджере паролей.',keys:[['SMTP_HOST','SMTP-сервер (TLS, порт 465)','text'],['SMTP_USERNAME','Логин почты','text'],['SMTP_PASSWORD','Пароль приложения почты','password'],['SMTP_FROM','Адрес отправителя','text'],['SMTP_TO','Рабочие адреса получателей через запятую','text'],['BACKUP_REGION','Регион AWS S3','text'],['BACKUP_BUCKET','S3 bucket','text'],['BACKUP_ACCESS_KEY','S3 Access Key','password'],['BACKUP_SECRET_KEY','S3 Secret Key','password'],['BACKUP_ENCRYPTION_KEY','Ключ Fernet для шифрования копий','password']]},
 {title:'Google Sheets',hint:'Сохраните ID таблицы; JSON ключа загрузите в разделе синхронизации ниже.',keys:[['SHEET_ID','ID таблицы','text']]}
];
let modelChoices=[];
function modelSelectors(settings) {
  const roles=[['extract','model','Анализ источников'],['author','author_model','Автор предложения'],['review','review_model','Проверка предложения']];
  const selections=settings.model_selection||{};
  const choices=new Map(modelChoices.map(m=>[m.id,m]));
  Object.values(selections).forEach(m=>{if(m.default&&!choices.has(m.default))choices.set(m.default,{id:m.default,name:m.label||m.default});if(m.selected&&!choices.has(m.selected))choices.set(m.selected,{id:m.selected,name:m.selected});});
  return roles.map(([role,field,label])=>{
    const m=selections[role]||{};
    return `<label class="field">${label}<select name="${field}" data-model-role="${role}"><option value="">Автоматически · ${esc(m.label||'набор проекта')}</option>${[...choices.values()].map(c=>`<option value="${esc(c.id)}" ${m.selected===c.id?'selected':''}>${esc(c.name)}${c.prompt!=null&&c.completion!=null?` · $${c.prompt} / $${c.completion}`:''}</option>`).join('')}</select></label>`;
  }).join('');
}
async function openSettings() {
  const settings=await api('/settings');
  function section(group) {return `<section class="connections-section"><h3>${group.title}<span class="status-label ${group.keys.every(([k])=>settings.fields[k])?'ready':''}">${group.keys.every(([k])=>settings.fields[k])?'Сохранено':'Не заполнено'}</span></h3><p>${group.hint}${group.keys[0][0]==='OPENROUTER_API_KEY'&&settings.perplexity_search_route==='openrouter'?' Perplexity дополняет поиск через этот ключ.':''}</p>${group.keys.map(([key,label,type])=>`<label class="field">${label}<input name="${key}" type="${type}" autocomplete="off" spellcheck="false" placeholder="${settings.fields[key]?'Уже сохранено · введите для замены':'Вставьте значение'}" maxlength="2000"></label>`).join('')}</section>`;}
  $('#connections-content').innerHTML=`<div class="dialog-header"><button class="close" data-action="close-settings" aria-label="Закрыть подключения">×</button><span class="eyebrow">НАСТРОЙКА СЕРВИСОВ</span><h2 id="connections-title">Подключения</h2><p>Добавьте ключи один раз — они сохранятся в рабочем приложении.</p></div><div class="dialog-body"><form id="settings-form">${section(connectionFields[0])}${section(connectionFields[1])}<p class="caption">Модели анализа, автора и проверки настроены автоматически. Можно сразу запускать поиск.</p><details id="model-settings"><summary>Дополнительно: выбор моделей</summary><div id="model-selectors">${modelSelectors(settings)}</div><button class="button" type="button" data-action="load-models">Загрузить список OpenRouter</button><p class="caption">Ручной ввод ID не нужен. Цены в списке: вход / выход за миллион токенов в долларах. Автор и проверяющая модель должны отличаться. Фактический доступ зависит от аккаунта OpenRouter.</p><div id="model-catalog-status" role="status"></div></details><details><summary>Дополнительные подключения: Telegram, поиск и таблицы</summary>${connectionFields.slice(2).map(section).join('')}</details><div class="settings-foot"><button class="button primary" type="submit">Сохранить подключения</button><p>Пустые поля сохраняют прежние значения.<br>Сохранение ключей не запускает платных запросов.</p></div><div id="settings-status" class="form-status" role="status"></div></form><div id="operations-content"></div></div>`;
  $('#operations-content').insertAdjacentHTML('beforebegin','<div id="brand-analytics-content"></div>');
  await Promise.all([loadOperations(),loadBrandAnalytics().catch(error=>{
    $('#brand-analytics-content').textContent=error.message;
  })]);
  await loadCompletionSettings();
  if (!$('#connections').open) $('#connections').showModal();
  await watchTelegramQR();
}
async function saveDecision() {
  const form=new FormData($('#decision-form'));const r=state.selected;
  const saved=await api('/records/'+r.id,{method:'PUT',body:JSON.stringify({revision:r.revision,status:form.get('status'),note:form.get('note'),stage:Number(form.get('stage')),situation:form.get('situation'),topics:form.getAll('topic'),refusal_reason:form.get('refusal_reason')})});
  state.selected=saved;return saved;
}
function reportDownloadable(brief) {
  if(!brief)return false;
  if(typeof brief.downloadable==='boolean')return brief.downloadable;
  const p=brief.payload||{}, q=p.report_qualification||{}, pipeline=p.report_pipeline||{};
  if(['hold','watch'].includes(q.verdict)||['hold','watch','manual'].includes(pipeline.status))return false;
  if(['5','8'].includes(p.report_v2?.meta?.schema_version))return pipeline.passed===true;
  return !!p.report_v2 || (p.dataset==='demo' && !!p.report);
}
function reportFormatVersion(brief) {
  return String(brief?.format_version || brief?.payload?.report_v2?.meta?.schema_version || brief?.payload?.report_pipeline?.version || '').replace(/^report-kit-v/,'');
}
function reportIsOutdated(brief) {
  const version=reportFormatVersion(brief);
  return brief?.current_format===false || (brief?.current_format!==true && ['5','6','7'].includes(version));
}
function outdatedReportHTML(id,brief) {
  const version=reportFormatVersion(brief);
  return `<div class="notice" role="status"><strong>Сохранён PDF прежнего образца${version&&version!=='legacy'?' · v'+esc(version):''}</strong><p>Новый разбор использует макет v8. Предыдущую версию можно открыть отдельно; для нового результата подготовьте обновлённый отчёт.</p><button class="button primary" data-pdf="${esc(id)}" data-regenerate="true">Подготовить PDF v8</button>${reportDownloadable(brief)?` <a href="/api/briefs/${brief.id}/pdf" target="_blank" rel="noopener noreferrer">Открыть предыдущий PDF ↗</a>`:''}</div>`;
}
function previousApprovedV8HTML(brief) {
  const approved=brief?.previous_approved_v8;
  return approved?`<p><a href="/api/briefs/${esc(approved.id)}/pdf" download="Мегаполис — разбор объекта.pdf">↓ Скачать ранее одобренный PDF v8 · версия ${esc(approved.version)}</a></p>`:'';
}
function qualificationHTML(brief) {
  const q=brief?.payload?.report_qualification;
  if(!q||!Object.keys(q).length)return '';
  const problems=q.problem_registry||[];
  const coverage=q.coverage||{};
  const sources=brief.payload.research_sources||brief.payload.report_pipeline?.sources||[];
  const message=q.bot_message||brief.detail||'';
  const summary=brief.observation?message.replace('в наблюдении, PDF не сформирован','под наблюдением'):message;
  return `<details class="research-details"><summary>Паспорт, проблемы и покрытие поиска</summary><p style="white-space:pre-line">${esc(summary)}</p>${q.passport?`<p>${esc(q.passport.official_name)} · ${esc(q.passport.developer_brand||'Адресат уточняется')}</p>`:''}<p class="caption">Прочитаны сообщения чатов: ${coverage.chats_read?'да':'нет'} · Официальный срок: ${coverage.official_deadline_found?'найден':'не подтверждён'} · Позиция застройщика: ${coverage.developer_position_found?'найдена':'не подтверждена'}</p>${problems.map(p=>`<article><strong>${esc(p.name)}</strong><p>${esc(p.first_date||'')} — ${esc(p.last_date||'')} · подтверждений: ${p.evidence_count||0}; текущих: ${p.active_dated_evidence_count||0}</p><p class="caption">${(p.source_numbers||[]).map(n=>{const source=sources.find(s=>s.n===n);return source?.url?`<a href="${safeURL(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.publisher||source.title||'Источник '+n)} ↗</a>`:'';}).filter(Boolean).join(' · ')||(p.publishers||[]).map(esc).join(' · ')}</p></article>`).join('')}</details>`;
}
function renderReportProgress(id,task) {
  const el=$('#report-progress');
  if(!el || !$('#detail').open || state.selected?.id!==id || !task)return;
  const active=['queued','running'].includes(task.status);
  const failed=['failed','interrupted'].includes(task.status);
  const cancelled=task.status==='cancelled';
  const stopping=active&&task.cancel_requested;
  const progress=task.progress;
  const elapsed=Math.max(0,Math.floor(((active?Date.now():Date.parse(task.updated_at))-Date.parse(task.created_at))/1000));
  const duration=Math.floor(elapsed/60)+' мин '+String(elapsed%60).padStart(2,'0')+' с';
  const outdated=!active&&reportIsOutdated(task.result);
  const ready=!active&&!outdated&&reportDownloadable(task.result)?task.result:null;
  const label=stopping?'Останавливаем после текущего запроса':cancelled?'Подготовка остановлена':failed?'Подготовка остановилась':outdated?'Сохранён прежний формат PDF':task.status==='queued'?'Отчёт в очереди':(!active&&task.result?.label)||progress?.label||'Готовим отчёт';
  const detail=failed||cancelled?task.error:(!active&&task.result?.detail)||progress?.detail||'';
  const stopButton=active&&identity.role!=='viewer'&&!stopping
    ? `<button class="button quiet" data-action="cancel-report" data-task="${esc(task.id)}">Остановить подготовку</button>`:'';
  el.innerHTML=`<div class="job-status ${failed?'error':''}" role="status"><div class="job-top"><strong>${active?'<span class="spinner"></span>':''}${esc(label)}</strong><span>${active?'Прошло':'Время'}: ${duration}</span></div><progress ${progress?`value="${progress.completed}"`:''} max="6" aria-label="Подготовка отчёта"></progress><p class="muted">${progress?`Этапов завершено: ${progress.completed} из 6. `:''}${esc(detail)}</p>${ready?`<p><a href="/api/briefs/${ready.id}/pdf" target="_blank" download="Мегаполис — разбор объекта.pdf">↓ Скачать PDF</a></p>`:''}${outdated?outdatedReportHTML(id,task.result):''}${previousApprovedV8HTML(task.result)}${qualificationHTML(task.result)}${stopButton}${active&&!stopping?'<p class="caption">Можно закрыть карточку. Подготовка продолжится.</p>':''}</div>`;
}
function watchReport(id,taskId=null) {
  clearTimeout(reportPollTimer);
  const generation=++reportWatchGeneration;
  const poll=async()=>{
    if(generation!==reportWatchGeneration || !$('#detail').open || state.selected?.id!==id)return;
    try {
      const task=await api(taskId?'/tasks/'+taskId:'/records/'+id+'/report-task');
      if(generation!==reportWatchGeneration || !$('#detail').open || state.selected?.id!==id)return;
      if(!task)return;
      taskId=task.id;
      if(task.status==='completed' && task.result){
        // Latest metadata also includes any earlier approved version for a draft.
        const latest=await api('/records/'+id+'/brief/latest').catch(()=>null);
        if(generation!==reportWatchGeneration || !$('#detail').open || state.selected?.id!==id)return;
        if(latest && latest.id>=task.result.id)task.result=latest;
        updateListReportStatus(id,latest);
      }
      renderReportProgress(id,task);
      if(['queued','running'].includes(task.status))reportPollTimer=setTimeout(poll,1500);
    } catch(error) {
      if(generation!==reportWatchGeneration || !$('#detail').open || state.selected?.id!==id)return;
      $('#report-progress').innerHTML='<p role="status">Связь с сервисом прервалась. Проверяем состояние отчёта…</p>';
      reportPollTimer=setTimeout(poll,3000);
    }
  };
  poll();
}
async function pdf(id,button,regenerate=false) {
  const old=button.textContent;button.disabled=true;button.textContent='Готовлю PDF…';
  try {
    let brief=regenerate?null:await api('/records/'+id+'/brief/latest');
    if(!brief){
      const record=state.records.find(r=>r.id===id)||await api('/records/'+id);
      if(record.dataset!=='demo' && (!$('#detail').open || state.selected?.id!==id))await openRecord(id);
      if ($('#detail').open && state.selected?.id===id && $('#decision-form')) await saveDecision();
      if(record.dataset!=='demo'){
      const job=await api('/records/'+id+'/tasks',{method:'POST',body:JSON.stringify({kind:'author'})});
      watchReport(id,job.id);
      let task=job;
      while(['queued','running'].includes(task.status)){
        button.textContent='Готовим и проверяем разбор…';
        await new Promise(resolve=>setTimeout(resolve,1500));
        task=await api('/tasks/'+job.id);
      }
      if(task.status==='cancelled'){toast(task.error||'Подготовка остановлена.');return;}
      if(task.status!=='completed')throw Error(task.error||'Предложение не готово. Предыдущие версии сохранены.');
      brief=task.result;
      if(brief){
        const latest=await api('/records/'+id+'/brief/latest').catch(()=>null);
        if(latest && latest.id>=brief.id)brief=latest;
      }
      }else brief=await api('/records/'+id+'/brief',{method:'POST'});
    }
    updateListReportStatus(id,brief);
    if(brief && reportIsOutdated(brief)){
      if(!$('#detail').open || state.selected?.id!==id)await openRecord(id);
      if($('#detail').open && state.selected?.id===id)$('#pdf-result').innerHTML=outdatedReportHTML(id,brief);
      toast('Сохранён прежний формат. Новый PDF v8 можно подготовить в карточке.');
      return;
    }
    if(brief && !reportDownloadable(brief) && (brief.payload?.report_qualification || brief.payload?.report_pipeline?.status==='manual')){
      if(!$('#detail').open || state.selected?.id!==id)await openRecord(id);
      if($('#detail').open && state.selected?.id===id)$('#pdf-result').innerHTML=`<div role="status"><strong>${esc(brief.label||'Разбор источников завершён')}</strong><p>${esc(brief.detail||'')}</p>${brief.retryable?`<button class="button primary" data-pdf="${esc(id)}" data-regenerate="true">Повторить сборку PDF</button>`:''}${previousApprovedV8HTML(brief)}${qualificationHTML(brief)}</div>`;
      toast(brief.label||'Разбор завершён. Посмотрите результат в карточке.');
      if($('#detail').open && state.selected?.id===id){$('#completion-card')?.remove();await loadCompletion(id);}
      return;
    }
    if(!reportDownloadable(brief))throw Error(brief?.detail||'Версия ещё не готова к скачиванию. Посмотрите результат проверки в карточке.');
    const response=await fetch('/api/briefs/'+brief.id+'/pdf',{headers:{'X-Radar-Local':'1'}});
    if(!response.ok){const error=await response.json().catch(()=>({}));throw Error(error.detail||'Не удалось скачать PDF. Повторите загрузку сохранённой версии.');}
    if(!response.headers.get('Content-Type')?.includes('application/pdf'))throw Error('Сервер вернул файл другого типа. PDF не скачан; повторите загрузку.');
    const url=URL.createObjectURL(await response.blob());
    const link=document.createElement('a');link.href=url;
    link.download=brief.observation?'Мегаполис — наблюдение за объектом.pdf':'Мегаполис — разбор объекта.pdf';
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    if ($('#detail').open && state.selected?.id===id) $('#pdf-result').innerHTML=`<p class="pdf-link">Версия ${brief.version} готова. <a href="/api/briefs/${brief.id}/pdf" target="_blank">Открыть PDF ↗</a></p>`;
    if($('#detail').open && state.selected?.id===id){$('#completion-card')?.remove();await loadCompletion(id);await compactCard(id);}
    toast(brief.observation?'PDF v8 сформирован. Объект под наблюдением.':brief.payload?.report_pipeline?.status==='draft'?'Черновик PDF готов. Доработанная версия появится в истории.':'PDF-разбор готов и сохранён');
  } catch(error) {
    if ($('#detail').open && state.selected?.id===id) $('#pdf-result').innerHTML=`<p role="alert">PDF не сформирован: ${esc(error.message)}. Можно повторить — сохранённые этапы будут использованы заново.</p>`;
    throw error;
  } finally {button.disabled=false;button.textContent=old;}
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('[data-action],[data-open],[data-pdf],[data-source]');if(!b)return;
  try {
    if(b.dataset.source){const source=await api('/sources/'+b.dataset.source);$('#source-'+b.dataset.source).innerHTML=`<pre class="source-text">${esc(source.text)}</pre>`;return;}
    if(b.dataset.open){await openRecord(b.dataset.open);return;}
    if(b.dataset.pdf){await pdf(b.dataset.pdf,b,b.dataset.regenerate==='true');return;}
    const action=b.dataset.action;
    if(action==='settings')return await openSettings();
    if(action==='close-detail'){clearTimeout(reportPollTimer);reportWatchGeneration++;detailRequest++;state.selected=null;return $('#detail').close();}
    if(action==='close-settings')return $('#connections').close();
    if(action==='prev'||action==='next'){state.page+=action==='prev'?-1:1;renderRecords();return;}
    if(action==='import')return $('#excel-file').click();
    if(action==='chat-import')return $('#chat-file').click();
    if(action==='chat-demo'){b.disabled=true;const batch=await api('/chats/example');batch.record_id=state.selected.id;await api('/chats/import',{method:'POST',body:JSON.stringify(batch)});await openRecord(state.selected.id);toast('Учебная переписка добавлена');return;}
    if(action==='demo-scan'){await startScan('demo');return;}
    if(action==='cancel-scan'){renderJob(await api('/jobs/'+activeJob.id+'/cancel',{method:'POST'}));return;}
    if(action==='cancel-report'){
      const recordId=state.selected?.id;b.disabled=true;
      const task=await api('/tasks/'+b.dataset.task+'/cancel',{method:'POST'});
      renderReportProgress(recordId,task);
      if(recordId&&state.selected?.id===recordId)watchReport(recordId,task.id);
      return;
    }
    if(action==='retry-scan'){await startScan(activeJob.mode,{...activeJob.request,mode:activeJob.mode});return;}
    if(action==='all-records'){state.runIds=null;await refresh();return;}
    if(action==='show-scan'){await chooseDataset(activeJob.result.dataset,activeJob.result.record_ids);return;}
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
sortControl.addEventListener('change',()=>{
  state.sort=sortControl.value;
  localStorage.setItem('megapolis-record-sort',state.sort);
  state.page=1;
  renderRecords();
});
$('#dataset').addEventListener('change',async e=>{try{await chooseDataset(e.target.value);}catch(err){toast(err.message);}});
$('#excel-file').addEventListener('change',async e=>{try{const file=e.target.files[0];if(!file)return;const data=new FormData();data.append('file',file);const result=await api('/import',{method:'POST',body:data});await chooseDataset('imported');toast(result.found?`Добавлено карточек: ${result.added}. Повторы пропущены.`:'Структура таблицы не распознана');}catch(err){toast(err.message);}finally{e.target.value='';}});
$('#chat-file').addEventListener('change',async e=>{try{const file=e.target.files[0];if(!file)return;if(file.size>10*1024*1024)throw Error('Максимальный размер — 10 МБ');const batch=JSON.parse(await file.text());batch.record_id=state.selected.id;const result=await api('/chats/import',{method:'POST',body:JSON.stringify(batch)});await openRecord(state.selected.id);toast('Добавлено сообщений: '+result.added);}catch(err){toast(err.message);}finally{e.target.value='';}});
for(const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click',e=>{if(e.target===dialog){const rect=dialog.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)dialog.close();}});
(async()=>{try{identity=await api('/session');if(identity.mode==='server')$('#logout-button').hidden=false;state.offers=(await api('/offers')).offers;await refresh();const last=await api('/jobs/latest');if(last){renderJob(last);if(last.result&&last.result.dataset===state.dataset)await chooseDataset(state.dataset,last.result.record_ids);if(!terminalJobs.has(last.status))await watchJob(last.id);}}catch(err){$('#records').innerHTML=`<div class="empty">Не удалось загрузить данные: ${esc(err.message)}</div>`;}})();

$('#show-blocked').addEventListener('change',()=>{if($('#show-blocked').checked)state.runIds=null;state.page=1;renderRecords();applyRole();});

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action="load-models"]');if(!button)return;
  button.disabled=true;$('#model-catalog-status').textContent='Загружаем каталог…';
  try {
    const settings=await api('/settings');
    for(const select of document.querySelectorAll('[data-model-role]'))settings.model_selection[select.dataset.modelRole].selected=select.value;
    const result=await api('/settings/models');modelChoices=result.models;
    $('#model-selectors').innerHTML=modelSelectors(settings);
    $('#model-catalog-status').textContent=`Доступно в каталоге: ${modelChoices.length}. Выберите модели и сохраните подключения.`;
  }catch(error){$('#model-catalog-status').textContent=error.message;}
  finally{button.disabled=false;}
});

function renderDevelopers(){
  let el=$('#developer-overview');
  if(!el){el=document.createElement('details');el.id='developer-overview';$('#records').before(el);}
  const rows=state.dataset==='web'?(activeJob?.result?.coverage?.developers||[]).filter(r=>r.scope_verified):[];
  el.hidden=!rows.length;
  el.innerHTML=`<summary>Откуда начинается поиск: девелоперы ЕРЗ (${rows.length})</summary><p class="caption">Первая страница региональных рейтингов. Доля переноса в строящемся портфеле не равна доле просроченных квартир. Нажмите имя, чтобы найти ЖК в результатах.</p><div class="table-scroll"><table class="research-table"><thead><tr><th>Девелопер</th><th>Регион</th><th>С переносом</th></tr></thead><tbody>${[...rows].sort((a,b)=>(b.delayed_share_pct||0)-(a.delayed_share_pct||0)).map(r=>`<tr><td><button class="more" data-developer="${esc(r.developer)}">${esc(r.developer)}</button></td><td>${esc(r.region)}</td><td>${r.delayed_share_pct==null?'Нет данных':r.delayed_share_pct+'%'}</td></tr>`).join('')}</tbody></table></div></details>`;
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-developer]');if(!b)return;$('#filter').value=b.dataset.developer;state.filter=b.dataset.developer;state.page=1;renderRecords();});
async function compactCard(id){
  const target=$('#detail-content .dialog-body');if(!target||state.selected?.id!==id)return;
  let tools=$('#analyst-tools');
  if(!tools){tools=document.createElement('details');tools.id='analyst-tools';tools.innerHTML='<summary>Заметки, источники и дополнительные инструменты</summary>';target.append(tools);}
  if(state.selected.possible_duplicates?.length && !$('#duplicate-links')){
    const duplicates=document.createElement('p');duplicates.id='duplicate-links';duplicates.className='caption';
    duplicates.innerHTML='Возможный дубль. '+state.selected.possible_duplicates.map((candidate,index)=>`<button class="more" data-open="${esc(candidate)}">Открыть похожую карточку${state.selected.possible_duplicates.length>1?' '+(index+1):''}</button>`).join(' · ');
    tools.append(duplicates);
  }
  for(const el of [...target.children]){
    if(el!==tools&&(el.tagName==='DETAILS'||el.id==='completion-card'||el.classList.contains('research')))tools.append(el);
  }
  const chat=target.querySelector(':scope > .chat-box');
  if(chat){const d=document.createElement('details');d.className='simple-chats';d.innerHTML='<summary>Открытые чаты · последние 30 дней</summary>';chat.before(d);d.append(chat);}
  if(!$('#research-overview')){
    const value=await objectIntelligence(id);if(state.selected?.id!==id || !$('#detail').open)return;
    if($('#research-overview'))return;
    const el=document.createElement('section');el.id='research-overview';el.className='section';
    const seenSources=new Set();const signals=(state.selected.dossier?.signals||[]).filter(s=>{if(seenSources.has(s.source_url))return false;seenSources.add(s.source_url);return true;});
    const answers=value.research?.answers||[];
    const chat=value.screening;
    el.innerHTML=`<h3>Основания и позиция компании</h3><p>${coverageLabel(state.selected)}</p>${signals.slice(0,4).map(s=>`<article class="finding"><span class="caption">${esc(s.event_date||s.published_at||host(s.source_url))} · ${esc(s.kind_label)}${perplexityLabel(s)}</span><p>«${esc((s.quote||s.summary).slice(0,420))}»</p><a href="${safeURL(s.source_url)}" target="_blank" rel="noopener noreferrer">${esc(host(s.source_url))} ↗</a></article>`).join('')||'<p class="muted">Открытые источники ещё не подтверждены.</p>'}<h4>Что говорит застройщик</h4>${answers.filter(a=>a.question==='developer_response').map(a=>`<p>«${esc(a.quote)}» <a href="${safeURL(a.url)}" target="_blank" rel="noopener noreferrer">Источник ↗</a></p>`).join('')||'<p class="muted">Прямой ответ по выявленной проблеме в собранных материалах не найден.</p>'}<p class="caption">${esc(cardChatReadSummary(chat,state.selected.chat_stats))}</p>`;
    const chronology=answers.filter(a=>['deadlines','chronology'].includes(a.question));
    const causes=answers.filter(a=>a.question==='causes');
    const block=(title,items)=>`<h4>${title}</h4>${items.map(a=>`<article class="finding"><p>«${esc(a.quote)}»</p><a href="${safeURL(a.url)}" target="_blank" rel="noopener noreferrer">${esc(host(a.url))} ↗</a></article>`).join('')}`;
    if(chronology.length)el.insertAdjacentHTML('beforeend',block('Как менялись сроки и события',chronology));
    el.insertAdjacentHTML('beforeend',causes.length?block('Что источники сообщают о причинах',causes):'<p class="caption">Причина задержки в собранных материалах прямо не объяснена.</p>');
    el.insertAdjacentHTML('beforeend',extendedResearchHTML(state.selected.report_research));
    target.querySelector('.pdf-action').before(el);
  }
}

function cardChatReadSummary(screening,stats) {
  const read=(screening?.chats||[]).reduce((total,chat)=>total+(Number(chat.read)||0),0);
  if(read>0)return `Прочитано сообщений за 30 дней: ${read}. Размечено: ${Number(screening.labelled)||0}.`;
  if(Number(stats?.messages_30d)>0)return `Прочитано сообщений за 30 дней: ${Number(stats.messages_30d)}. Чатов: ${(stats.chats||[]).length}. Размечено: ${Number(stats.labelled)||0}.`;
  if(stats?.chats?.length)return 'В подключённых чатах нет прочитанных сообщений за последние 30 дней; выводов о текущих настроениях нет.';
  return 'Чаты не прочитаны — выводов о настроениях участников нет.';
}
function researchDate(value) {
  if(!value)return 'Дата публикации не указана';
  if(/^\d{4}-\d{2}-\d{2}(?:$|T)/.test(value)){
    const day=new Date(value);
    if(!Number.isNaN(day.valueOf()))return day.toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow'});
  }
  return String(value);
}
function perplexityLabel(source) {
  return source.search_provider === 'perplexity' ? ' · найдено через Perplexity' : '';
}
function extendedResearchHTML(research) {
  if(!research?.documents?.length)return '';
  return `<details id="extended-research"><summary>Материалы расширенного поиска · ${research.documents.length}</summary><p class="caption">${research.completed?'Поиск завершён':'Поиск выполнен частично'}${research.created_at?' · собрано '+esc(researchDate(research.created_at)):''}. Загрузка страницы сама по себе не подтверждает изложенные в ней тезисы.</p>${research.documents.map(source=>{const snippet=source.source_kind==='snippet';const status=snippet?'Фрагмент выдачи · полный текст не прочитан':source.read_status==='loaded'?'Текст загружен':source.read_status==='unavailable'?'Полный текст недоступен':'Полный текст не прочитан';return `<article class="saved-source"><a href="${safeURL(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title||host(source.url))} ↗</a><p class="caption">${esc(researchDate(source.published_at))} · ${status}${perplexityLabel(source)}</p></article>`;}).join('')}</details>`;
}

// Entire record surface opens the dossier; nested controls retain their own actions.
document.addEventListener('click',async e=>{
  const control=e.target.closest('[data-disposition]');
  if(control){
    control.disabled=true;
    try{
      const id=control.dataset.disposition;
      const r=await api('/records/'+id);
      if(control.dataset.restore==='1'){
        if(r.blocked?.kind==='developer')throw Error('Объект скрыт правилом для застройщика. Измените его в подключениях.');
        if(r.blocked)await api('/stoplist/'+r.blocked.id,{method:'DELETE'});
        if(r.status==='rejected')await api('/records/'+id,{method:'PUT',body:JSON.stringify({revision:r.revision,status:'new',note:r.note,stage:r.stage,situation:r.situation,topics:r.topics,refusal_reason:''})});
      }else{
        await api('/stoplist',{method:'POST',body:JSON.stringify({kind:'record',value:id,reason:'refused',note:'Не берём в работу'})});
      }
      if($('#detail').open)$('#detail').close();
      await refresh();
      toast(control.dataset.restore==='1'?'Объект возвращён в список':'Объект скрыт. Вернуть можно через «Показать скрытые».');
    }catch(err){toast(err.message);}finally{control.disabled=false;}
    return;
  }
  const card=e.target.closest('[data-card]');
  if(!card||e.target.closest('button,a,input,select,textarea,label')||window.getSelection()?.toString())return;
  try{await openRecord(card.dataset.card);}catch(err){toast(err.message);}
});
document.addEventListener('keydown',e=>{
  if(e.target.matches('[data-card]')&&['Enter',' '].includes(e.key)){
    e.preventDefault();openRecord(e.target.dataset.card).catch(err=>toast(err.message));
  }
});
