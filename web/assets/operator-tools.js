/* Operator analysis and future schedule editor. Native controls remain the source
   of truth; the page's shared enhancer styles controls added here as well. */
(() => {
  const style = document.createElement('style');
  style.textContent = `
    .tools-row { display:flex; flex-wrap:wrap; align-items:center; gap:12px; margin:16px 0; }
    .tools-row > input, .tools-row > select, .tools-row > .select-control { flex:1 1 140px; width:auto; min-width:0; }
    .tools-row .hint { margin:0; }
    .chart-options { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:16px 0; }
    .chart-options .field { margin:0; }
    .sat-choices { display:flex; flex-wrap:wrap; gap:8px 14px; margin:12px 0; max-height:120px; overflow:auto; }
    .sat-choices label { display:flex; align-items:center; gap:6px; font-size:12px; }
    .sat-choices input { width:auto; accent-color:var(--accent); }
    .chart-panel { margin-top:16px; }
    .chart-panel svg { height:190px; }
    .chart-panel h3 { font-size:12px; margin:0 0 8px; }
    .chart-panel .legend { font-size:10px; }
    #telemetryChart { height:auto; min-height:210px; }
    .timeline-cell.planned { outline:1px dashed var(--accent); background:var(--accent); color:var(--accent-contrast); }
    .timeline-cell.forecast { opacity:.65; outline:1px dashed var(--dim); }
    .timeline-cell.drop-target { outline:3px solid var(--accent); }
    .trace-link { background:transparent; color:var(--text); text-decoration:underline; }
    #scheduleEditor { margin-top:20px; padding-top:20px; border-top:1px solid var(--line); }
    #scheduleEditor .field { margin:0; }
    #scheduleEditor .event-form { margin:16px 0; }
    #scheduleList { max-height:180px; overflow:auto; margin:12px 0; }
    #scheduleList button { margin:4px 8px 4px 0; }
    @media(max-width:500px) { .chart-options { grid-template-columns:1fr; } }
  `;
  document.head.append(style);

  // All trace entries remain reachable; filtering applies before pagination.
  let tracePage = 0;
  $('trace').insertAdjacentHTML('beforebegin', `<div class="tools-row">
    <input id="traceFilter" aria-label="Поиск в журнале" placeholder="Аппарат, задание или причина…">
    <button class="button ghost" id="tracePrev">←</button><span id="tracePage" aria-live="polite"></span>
    <button class="button ghost" id="traceNext">→</button></div>`);
  renderTrace = function(telemetry) {
    const search = $('traceFilter').value.trim().toLowerCase();
    const all = Object.entries(telemetry).flatMap(([sid, rows]) => rows.map(r => ({...r, satellite_id:r.satellite_id || sid})))
      .filter(r => `${r.satellite_id} ${r.job_id || ''} ${r.reason} ${r.executed} ${r.step}`.toLowerCase().includes(search))
      .sort((a,b) => b.step - a.step || a.satellite_id.localeCompare(b.satellite_id));
    const pages = Math.max(1, Math.ceil(all.length / 120));
    tracePage = Math.max(0, Math.min(tracePage, pages - 1));
    $('tracePage').textContent = `${tracePage + 1} / ${pages} · ${all.length} записей`;
    $('tracePrev').disabled = !tracePage; $('traceNext').disabled = tracePage + 1 >= pages;
    $('trace').innerHTML = all.length ? `<table><thead><tr><th>Шаг</th><th>Аппарат</th><th>Операция</th><th>Причина</th><th>Энергия</th><th>Задание</th></tr></thead><tbody>${all.slice(tracePage*120, (tracePage+1)*120).map(r => `<tr><td><button class="trace-link" data-step="${r.step}" data-sat="${esc(r.satellite_id)}">${r.step}</button></td><td>${esc(r.satellite_id)}</td><td>${esc(r.executed)}</td><td>${esc(r.reason)}</td><td>${num(r.energy_wh)} Wh</td><td>${esc(r.job_id || '—')}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">Нет подходящих записей.</div>';
  };
  const updateTrace = () => renderTrace(current?.telemetry || {});
  $('traceFilter').addEventListener('input', () => { tracePage = 0; updateTrace(); });
  $('tracePrev').addEventListener('click', () => { tracePage--; updateTrace(); });
  $('traceNext').addEventListener('click', () => { tracePage++; updateTrace(); });
  $('trace').addEventListener('click', event => {
    const cell = event.target.closest('[data-step]');
    if (cell) { $('explainStep').value = cell.dataset.step; $('explainSat').value = cell.dataset.sat; explain(); $('explanation').scrollIntoView({block:'center'}); }
  });

  // Compare any subset with separate physical axes and a selectable step range.
  let chartIds = new Set(), chartRun = '';
  $('telemetryChart').insertAdjacentHTML('beforebegin', `<div class="chart-options">
    <div class="field"><label for="chartFrom">С шага</label><input id="chartFrom" type="number" min="0" value="0"></div>
    <div class="field"><label for="chartTo">По шаг (пусто — все)</label><input id="chartTo" type="number" min="0" placeholder="Все"></div></div>
    <details><summary>Сравнить несколько аппаратов</summary><div id="chartChoices" class="sat-choices"></div></details>
    <p class="hint" id="chartStats"></p>`);
  renderCharts = function(telemetry) {
    if (!current) return;
    if (chartRun !== currentRunId) { chartRun = currentRunId; chartIds.clear(); }
    const ids = Object.keys(current.observation.state);
    chartIds = new Set([...chartIds].filter(id => ids.includes(id)));
    const selected = ids.includes($('chartSat').value) ? $('chartSat').value : '';
    $('chartSat').innerHTML = '<option value="">Вся группировка (среднее)</option>' + ids.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join('');
    $('chartSat').value = ids.includes(selected) ? selected : '';
    $('chartChoices').innerHTML = ids.map(id => `<label><input type="checkbox" value="${esc(id)}" ${chartIds.has(id) ? 'checked' : ''}>${esc(id)}</label>`).join('');
    const from = Math.max(0, Math.trunc(Number($('chartFrom').value)) || 0);
    const to = $('chartTo').value === '' ? Infinity : Math.max(from, Number($('chartTo').value));
    let series;
    if (chartIds.size || selected) {
      series = [...(chartIds.size ? chartIds : [selected])].map(id => ({id, rows:telemetry[id] || []}));
    } else {
      const byStep = new Map();
      Object.values(telemetry).flat().forEach(row => {
        const value = byStep.get(row.step) || {step:row.step, energy_wh:0, temp_c:0, count:0};
        value.energy_wh += row.energy_wh; value.temp_c += row.temp_c; value.count++;
        byStep.set(row.step, value);
      });
      series = [{id:'Среднее', rows:[...byStep.values()].map(r => ({...r, energy_wh:r.energy_wh/r.count, temp_c:r.temp_c/r.count}))}];
    }
    series = series.map(s => ({...s, rows:s.rows.filter(r => r.step >= from && r.step <= to)}));
    const rows = series.flatMap(s => s.rows);
    if (!rows.length) { $('telemetryChart').innerHTML = '<div class="empty">В выбранном диапазоне пока нет данных.</div>'; $('chartStats').textContent = ''; return; }
    const minStep = Math.min(...rows.map(r => r.step)), maxStep = Math.max(...rows.map(r => r.step));
    const colors = ['#d58f08','#287bc1','#b257ae','#239782','#d1544c','#7373c9'];
    $('telemetryChart').innerHTML = [['energy_wh','Энергия','Wh'], ['temp_c','Температура','°C']].map(([key,title,unit]) => {
      const values = rows.map(r => r[key]), low = Math.min(0, ...values), high = Math.max(...values, low+1);
      const x = step => 48 + (step-minStep) / Math.max(1,maxStep-minStep)*628;
      const y = value => 156 - (value-low)/(high-low)*132;
      return `<section class="chart-panel"><h3>${title}, ${unit}</h3><div class="legend">${series.map((s,i) => `<span><i style="background:${colors[i%colors.length]}"></i>${esc(s.id)}</span>`).join('')}</div><svg viewBox="0 0 720 185" role="img" aria-label="${title}"><g class="chart-grid"><line x1="48" y1="24" x2="676" y2="24"/><line x1="48" y1="90" x2="676" y2="90"/><line x1="48" y1="156" x2="676" y2="156"/></g>${series.map((s,i) => `<path fill="none" stroke="${colors[i%colors.length]}" stroke-width="2" d="${s.rows.map((r,j) => `${j ? 'L':'M'}${x(r.step)},${y(r[key])}`).join(' ')}"/>${s.rows.map(r => `<circle cx="${x(r.step)}" cy="${y(r[key])}" r="3" fill="${colors[i%colors.length]}"><title>${esc(s.id)} · шаг ${r.step} · ${num(r[key],2)} ${unit}</title></circle>`).join('')}`).join('')}<text class="chart-axis" x="0" y="28">${num(high,1)}</text><text class="chart-axis" x="0" y="160">${num(low,1)}</text><text class="chart-axis" x="48" y="181">${minStep}</text><text class="chart-axis" x="650" y="181">${maxStep}</text></svg></section>`;
    }).join('');
    $('chartStats').textContent = `Шаги ${minStep}–${maxStep} · ${series.length} ряд(ов) · энергия ${num(Math.min(...rows.map(r=>r.energy_wh)))}–${num(Math.max(...rows.map(r=>r.energy_wh)))} Wh`;
  };
  ['chartFrom','chartTo'].forEach(id => $(id).addEventListener('input', () => current && renderCharts(current.telemetry)));
  $('chartChoices').addEventListener('change', event => {
    if (event.target.checked) chartIds.add(event.target.value); else chartIds.delete(event.target.value);
    renderCharts(current.telemetry);
  });
  $('chartSat').addEventListener('change', () => { chartIds.clear(); if (current) renderCharts(current.telemetry); });

  // Manual schedule: no future events are invented; forecasts use known inputs.
  let forecast = null, dragged = null;
  $('timeline').insertAdjacentHTML('afterend', `<section id="scheduleEditor">
    <h3>Будущие операции</h3><p class="hint">Назначения заменяют действия планировщика для выбранных ячеек. Модель проверяет ресурсы при исполнении; недопустимая операция будет отклонена. Прогноз учитывает только уже полученные события.</p>
    <div class="event-form"><div class="field"><label for="scheduleStep">Шаг</label><input id="scheduleStep" type="number" min="0" value="0"></div>
    <div class="field"><label for="scheduleSat">Аппарат</label><select id="scheduleSat"></select></div>
    <div class="field"><label for="scheduleAction">Операция</label><select id="scheduleAction"><option value="idle">Ожидание</option><option value="calibrate">Калибровка</option><option value="job">Задание</option></select></div>
    <div class="field"><label for="scheduleJob">Задание</label><select id="scheduleJob"></select></div></div>
    <div class="button-row"><button id="scheduleSave" class="button accent">Сохранить назначение</button><button id="scheduleRemove" class="button ghost">Вернуть планировщику</button><button id="schedulePreview" class="button">Прогноз 48 шагов</button></div>
    <p class="hint" id="forecastInfo" aria-live="polite">Клик по будущей ячейке открывает её для редактирования. Назначенную ячейку можно перетащить.</p><div id="scheduleList"></div></section>`);
  const entries = () => Object.entries(current?.metadata?.scheduled_actions || {}).flatMap(([step, actions]) => Object.entries(actions).map(([satellite_id, action]) => ({step:Number(step), satellite_id, action}))).filter(e => e.step >= current.observation.step);
  function fillJobs() {
    const previous = $('scheduleJob').value;
    const sid = $('scheduleSat').value;
    $('scheduleJob').innerHTML = Object.values(current?.observation.jobs || {}).filter(j => j.eligible_satellites.includes(sid)).map(j => `<option value="${esc(j.id)}">${esc(j.id)} · ${j.release_step}–${j.deadline_step}</option>`).join('');
    if ([...$('scheduleJob').options].some(o => o.value === previous)) $('scheduleJob').value = previous;
    $('scheduleJob').disabled = $('scheduleAction').value !== 'job';
  }
  function selectSlot(step, sid) {
    $('scheduleStep').value = step; $('scheduleSat').value = sid;
    const entry = entries().find(e => e.step === step && e.satellite_id === sid);
    $('scheduleAction').value = entry?.action.action || 'idle'; fillJobs();
    if (entry?.action.job_id) $('scheduleJob').value = entry.action.job_id;
  }
  async function saveEntries(next) {
    if (!current) return;
    try {
      const data = await api(`/api/runs/${active()}/schedule`, {method:'POST', body:JSON.stringify({entries:next})});
      forecast = null; render(data); status('Будущие назначения сохранены. История выполненных шагов не изменена.');
    } catch (error) { status(error.message, true); }
  }
  $('scheduleSat').addEventListener('change', fillJobs);
  $('scheduleAction').addEventListener('change', fillJobs);
  $('scheduleSave').addEventListener('click', () => {
    if (!current) return;
    const step = Number($('scheduleStep').value), satellite_id = $('scheduleSat').value;
    const action = {action:$('scheduleAction').value};
    if (action.action === 'job') action.job_id = $('scheduleJob').value;
    saveEntries([...entries().filter(e => e.step !== step || e.satellite_id !== satellite_id), {step, satellite_id, action}]);
  });
  $('scheduleRemove').addEventListener('click', () => current && saveEntries(entries().filter(e => e.step !== Number($('scheduleStep').value) || e.satellite_id !== $('scheduleSat').value)));
  $('schedulePreview').addEventListener('click', async () => {
    if (!current) return;
    $('schedulePreview').disabled = true; $('forecastInfo').textContent = 'Рассчитывается отдельная копия состояния…';
    try {
      forecast = await api(`/api/runs/${active()}/preview`, {method:'POST', body:JSON.stringify({until_step:Math.min(current.observation.step+48, current.available_actions.scenario_steps)})});
      $('timelineStart').value = forecast.from_step;
      $('forecastInfo').textContent = `Прогноз ${forecast.from_step}–${forecast.until_step}: выручка ${num(forecast.summary.revenue_usd,2)} USD. Полупрозрачные ячейки — прогноз, а не выполненные операции.`;
      renderTimeline();
    } catch (error) { status(error.message, true); $('forecastInfo').textContent = 'Прогноз не получен.'; }
    finally { $('schedulePreview').disabled = accountRole === 'viewer'; }
  });
  const originalTimeline = renderTimeline;
  renderTimeline = function() {
    originalTimeline();
    if (!current) return;
    if (forecast?.source_revision !== current.revision) forecast = null;
    const start = Number($('timelineStart').value);
    const scheduled = new Map(entries().map(e => [`${e.satellite_id}:${e.step}`, e]));
    document.querySelectorAll('#timeline tbody tr').forEach((row, index) => {
      const sid = Object.keys(current.observation.state)[index];
      row.querySelectorAll('td').forEach((cell, offset) => {
        const step = start + offset;
        if (step < current.observation.step) return;
        const entry = scheduled.get(`${sid}:${step}`);
        const predicted = forecast?.telemetry[sid]?.find(r => r.step === step);
        const text = entry ? (entry.action.action === 'job' ? 'J' : entry.action.action === 'calibrate' ? 'C' : '·') : predicted ? (predicted.executed === 'job' ? 'J' : predicted.executed === 'calibrate' ? 'C' : '·') : '+';
        cell.innerHTML = `<button class="timeline-cell ${entry ? 'planned' : predicted ? 'forecast' : ''}" data-edit-step="${step}" data-edit-sat="${esc(sid)}" draggable="${!!entry && accountRole !== 'viewer'}" aria-label="${esc(sid)} шаг ${step}: ${entry ? 'назначение' : 'редактировать'}" title="${esc(entry ? JSON.stringify(entry.action) : predicted ? `${predicted.executed}: ${predicted.reason}` : 'Назначить операцию')}">${text}</button>`;
      });
    });
  };
  $('timeline').addEventListener('click', event => {
    const cell = event.target.closest('[data-edit-step]');
    if (cell) { selectSlot(Number(cell.dataset.editStep), cell.dataset.editSat); $('scheduleEditor').scrollIntoView({block:'nearest'}); }
  });
  $('timeline').addEventListener('dragstart', event => {
    const cell = event.target.closest('[data-edit-step]');
    if (!cell || accountRole === 'viewer') return;
    dragged = entries().find(e => e.step === Number(cell.dataset.editStep) && e.satellite_id === cell.dataset.editSat);
    if (dragged) { event.dataTransfer.setData('text/plain', JSON.stringify(dragged)); event.dataTransfer.effectAllowed = 'move'; }
  });
  $('timeline').addEventListener('dragover', event => { if (dragged && event.target.closest('[data-edit-step]')) event.preventDefault(); });
  $('timeline').addEventListener('drop', event => {
    const cell = event.target.closest('[data-edit-step]');
    if (!dragged || !cell) return;
    event.preventDefault();
    const step = Number(cell.dataset.editStep), sid = cell.dataset.editSat;
    if (entries().some(e => e.step === step && e.satellite_id === sid)) { status('Ячейка уже занята назначением.', true); dragged = null; return; }
    const next = entries().filter(e => e.step !== dragged.step || e.satellite_id !== dragged.satellite_id);
    next.push({...dragged, step, satellite_id:sid}); dragged = null; saveEntries(next);
  });
  $('timeline').addEventListener('dragend', () => { dragged = null; });
  $('scheduleList').addEventListener('click', event => { const item = event.target.closest('[data-slot]'); if (item) { const entry = entries()[Number(item.dataset.slot)]; selectSlot(entry.step, entry.satellite_id); } });
  const originalRender = render;
  render = function(data) {
    const changed = current?.revision !== data.revision;
    if (changed) { forecast = null; $('forecastInfo').textContent = 'Прогноз нужно пересчитать после изменения состояния.'; }
    originalRender(data);
    const selected = $('scheduleSat').value, ids = Object.keys(data.observation.state);
    $('scheduleSat').innerHTML = ids.map(id => `<option>${esc(id)}</option>`).join('');
    $('scheduleSat').value = ids.includes(selected) ? selected : ids[0] || '';
    $('scheduleStep').min = data.observation.step; $('scheduleStep').max = data.available_actions.scenario_steps-1;
    if (Number($('scheduleStep').value) < data.observation.step) $('scheduleStep').value = data.observation.step;
    fillJobs();
    $('scheduleList').innerHTML = entries().map((e,i) => `<button class="button ghost" data-slot="${i}">${esc(e.satellite_id)} · ${e.step} · ${esc(e.action.job_id || e.action.action)}</button>`).join('');
    ['scheduleSave','scheduleRemove','schedulePreview'].forEach(id => $(id).disabled = accountRole === 'viewer' || data.observation.step >= data.available_actions.scenario_steps);
  };
  ['scheduleSave','scheduleRemove','schedulePreview'].forEach(id => $(id).disabled = true);
  updateTrace();
  if (current) render(current);
})();
