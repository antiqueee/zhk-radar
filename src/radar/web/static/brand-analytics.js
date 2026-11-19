const brandAnalyticsPath = '/settings/brand-analytics';

function brandAnalyticsSources(documents) {
  if (!documents?.length) return '';
  const links = documents.map(doc => `<li><a href="${safeURL(doc.url)}" target="_blank"
    rel="noopener noreferrer">${esc(doc.title || host(doc.url))} ↗</a>
    <span class="muted">${doc.published_at ? esc(dateText(doc.published_at)) : ''}</span></li>`);
  return `<section class="section"><h3>Публикации из Brand Analytics</h3><ul>${links.join('')}</ul></section>`;
}

async function loadBrandAnalytics() {
  const container = $('#brand-analytics-content');
  if (!container) return;
  const connection = await api(brandAnalyticsPath);
  const latest = connection.last_run;
  const states = {queued:'В очереди', running:'Загружаем', completed:'Обновлено',
    partial:'Загружена часть', failed:'Импорт остановлен'};
  const inventory = connection.inventory;
  container.innerHTML = `<details ${container.dataset.brandOpen ? 'open' : ''}>
    <summary>Brand Analytics · ${connection.configured ? 'подключение сохранено' : 'настройка'}</summary>
    <p>Одна или две общие темы на каталог ЖК. Импорт обновляет карточки и источники для PDF.</p>
    <label class="field">Лимит символов на тему из кабинета, если известен
      <input id="brand-query-limit" type="number" min="1" step="1"></label>
    <div class="chat-actions"><button class="button" type="button" data-brand="queries">Собрать запросы для тем</button></div>
    <div id="brand-queries"></div>
    <form id="brand-connection-form">
      <label class="field">Адрес метода ленты сообщений<input name="endpoint" type="text"
        autocomplete="off" spellcheck="false" maxlength="1500" value="${esc(connection.endpoint)}"></label>
      <label class="field">ID одной или двух тем через запятую<input name="topic_ids" maxlength="400"
        autocomplete="off" spellcheck="false" value="${esc(connection.topic_ids)}"></label>
      <label class="field">API-ключ<input name="token" type="password" maxlength="2000" autocomplete="off"
        placeholder="${connection.token_saved ? 'Ключ сохранён' : 'Вставьте ключ из кабинета'}"></label>
      <details><summary>Параметры метода из документации аккаунта</summary>
        <label class="field">Профиль JSON<textarea name="profile" maxlength="16000" spellcheck="false"
          placeholder="${connection.profile_saved ? 'Профиль сохранён · пустое поле его сохраняет' : 'Необязательный профиль JSON'}"></textarea></label>
        <p class="caption">В профиле можно указать METHOD, TOKEN_HEADER, TOKEN_SCHEME, REQUEST_FIELDS,
          MESSAGES_PATH, NEXT_CURSOR_PATH, FIELD_MAP, DATE_FORMAT и PAGE_SIZE. Введите {}, чтобы сбросить профиль.</p>
      </details>
      <label class="check-line"><input type="checkbox" name="scheduled" ${connection.scheduled ? 'checked' : ''}>
        <span>Обновлять ежедневно после 03:00 по Москве</span></label>
      <button class="button primary" type="submit">Сохранить Brand Analytics</button>
    </form>
    <p class="caption">Сообщений в базе — ${inventory.messages}. ЖК с совпадениями — ${inventory.matched_records}.</p>
    <p role="status">${latest ? esc(states[latest.state] || latest.state) : 'Первая синхронизация ещё не запускалась'}
      ${latest?.result?.error ? ` · ${esc(latest.result.error)}` : ''}</p>
    <div class="chat-actions"><button class="button" type="button" data-brand="sync"
      ${connection.configured ? '' : 'disabled'}>Синхронизировать</button>
      <button class="button quiet" type="button" data-brand="refresh">Обновить статус</button></div>
    <form id="brand-import-form"><label class="field">Выгрузка CSV или JSON<input name="file"
      type="file" accept=".csv,.json" required></label><button class="button" type="submit">Импортировать выгрузку</button></form>
    <p id="brand-result" class="form-status" role="status"></p></details>`;
}

document.addEventListener('click', async event => {
  const button = event.target.closest('#brand-analytics-content [data-brand]');
  if (!button) return;
  button.disabled = true;
  $('#brand-analytics-content').dataset.brandOpen = '1';
  try {
    if (button.dataset.brand === 'queries') {
      const limit = $('#brand-query-limit');
      if (!limit.reportValidity()) return;
      const query = limit.value ? '?max_chars=' + encodeURIComponent(limit.value) : '';
      const result = await api(brandAnalyticsPath + '/queries' + query);
      $('#brand-queries').innerHTML = `<p>В запросах ${result.covered_cards} из ${result.total_cards} карточек.</p>
        ${result.queries.map((item, index) => `<label class="field">Тема ${index + 1} · ${item.characters} символов
          <textarea readonly rows="4" spellcheck="false">${esc(item.query)}</textarea></label>`).join('')}
        ${result.uncovered.length ? `<details><summary>Карточки для уточнения</summary><ul>
          ${result.uncovered.map(item => `<li>${esc(item.name)} · ${esc(item.reason)}</li>`).join('')}
          </ul></details>` : ''}`;
      return;
    }
    if (button.dataset.brand === 'sync') await api(brandAnalyticsPath + '/sync', {method:'POST'});
    await loadBrandAnalytics();
  } catch (error) {
    $('#brand-result').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

document.addEventListener('submit', async event => {
  const form = event.target;
  if (!['brand-connection-form', 'brand-import-form'].includes(form.id)) return;
  event.preventDefault();
  const button = form.querySelector('[type=submit]');
  button.disabled = true;
  $('#brand-analytics-content').dataset.brandOpen = '1';
  const values = new FormData(form);
  try {
    let message;
    if (form.id === 'brand-connection-form') {
      const [endpoint, topic_ids, token, profile] = ['endpoint','topic_ids','token','profile'].map(
        key => values.get(key).trim());
      await api(brandAnalyticsPath, {method:'PUT', body:JSON.stringify({
        endpoint, topic_ids, token, scheduled:values.has('scheduled'), ...(profile ? {profile} : {})
      })});
      message = 'Подключение сохранено';
    } else {
      await api(brandAnalyticsPath + '/import', {method:'POST', body:values});
      message = 'Выгрузка обработана';
    }
    await loadBrandAnalytics();
    $('#brand-result').textContent = message;
  } catch (error) {
    $('#brand-result').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
