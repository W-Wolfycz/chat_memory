const PLUGIN_NAME = 'chat_memory';
const bridge = window.AstrBotPluginPage || null;

const state = {
  about: null,
  settings: null,
  overview: null,
  facets: null,
  page: 1,
  pageCount: 0,
  total: 0,
  loading: false,
  requestToken: 0,
  facetRequestToken: 0,
  lastQuery: null,
  toolPage: 1,
  toolPageCount: 0,
  toolTotal: 0,
  toolRequestToken: 0,
  lastToolQuery: null,
  mediaObjectUrls: [],
};

const FACET_SELECTORS = [
  '#filter-umo',
  '#filter-conversation',
  '#filter-user',
  '#filter-persona',
  '#filter-role',
  '#filter-status',
  '#filter-kind',
  '#filter-platform',
  '#filter-message-type',
];

const $ = (selector) => document.querySelector(selector);

async function apiGet(endpoint, params = {}) {
  if (bridge && typeof bridge.apiGet === 'function') return bridge.apiGet(endpoint, params);
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  });
  const suffix = search.toString() ? `?${search}` : '';
  const response = await fetch(`/api/plug/${PLUGIN_NAME}/${endpoint}${suffix}`);
  return response.json();
}

async function apiPost(endpoint, body) {
  if (bridge && typeof bridge.apiPost === 'function') return bridge.apiPost(endpoint, body);
  const response = await fetch(`/api/plug/${PLUGIN_NAME}/${endpoint}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return response.json();
}

function unwrap(response) {
  if (response === null || response === undefined) {
    throw new Error('服务端没有返回数据');
  }
  if (response.success === false || response.status === 'error') {
    throw new Error(response.error || response.message || '服务端查询失败');
  }
  if (response.success === true) {
    return Object.prototype.hasOwnProperty.call(response, 'data')
      ? response.data
      : response;
  }
  if (response.status === 'ok' && Object.prototype.hasOwnProperty.call(response, 'data')) {
    return response.data;
  }
  // AstrBot 4.26.x Plugin Page 父窗口会把 response.data.data 自动解包，
  // Bridge 模式下这里收到的就是业务 payload；直接 fetch 才会收到 success envelope。
  if (typeof response === 'object') return response;
  throw new Error(`服务端返回了无法识别的数据：${String(response)}`);
}

function formatNumber(value) {
  return new Intl.NumberFormat('zh-CN').format(Number(value || 0));
}

function formatBytes(value) {
  const amount = Number(value || 0);
  if (amount < 1024) return `${amount} B`;
  if (amount < 1024 ** 2) return `${(amount / 1024).toFixed(1)} KB`;
  if (amount < 1024 ** 3) return `${(amount / 1024 ** 2).toFixed(1)} MB`;
  return `${(amount / 1024 ** 3).toFixed(2)} GB`;
}

function base64ToObjectUrl(dataBase64, mime) {
  const binary = atob(dataBase64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return URL.createObjectURL(new Blob([bytes], { type: mime || 'application/octet-stream' }));
}

async function fetchMediaObjectUrl(mediaId) {
  const data = unwrap(await apiGet(`media/${mediaId}`, { as: 'base64' }));
  return base64ToObjectUrl(data.data, data.mime);
}

const ARCHIVE_MEDIA_KINDS = new Set(['image', 'video', 'voice', 'file']);

function downloadMedia(mediaId, name) {
  // 优先走 Plugin Page 原生下载（带鉴权、直接取原始字节）；
  // 无 bridge 时回退 base64 → blob → <a download>。
  // 用 Promise.resolve().then 包裹：bridge.download 同步抛错也会进入 reject，
  // 调用方的 .catch 才能恢复按钮状态。
  if (bridge && typeof bridge.download === 'function') {
    return Promise.resolve().then(() => bridge.download(`media/${mediaId}`, {}, name || 'download'));
  }
  return fetchMediaObjectUrl(mediaId).then((url) => {
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = name || 'download';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  });
}

function mediaDetailHtml(media) {
  if (media.archived) {
    const mid = escapeHtml(media.media_id);
    const size = escapeHtml(formatBytes(media.size));
    const okTag = '<span class="media-state-ok">已归档</span>';
    const downloadButton = `<button type="button" class="media-download-button" data-download-id="${mid}" data-download-name="${escapeHtml(media.name || media.file || media.kind)}">下载</button>`;
    if (media.kind === 'image') {
      return `<figure class="media-item"><div class="media-loading" data-media-id="${mid}" data-kind="image"><span class="loader"></span> 缩略图加载中…</div><figcaption class="media-actions">image · ${size} ${okTag}${downloadButton}</figcaption></figure>`;
    }
    if (media.kind === 'voice') {
      return `<figure class="media-item"><audio controls preload="none" data-media-id="${mid}" data-kind="voice"></audio><figcaption class="media-actions">voice · ${size} ${okTag}${downloadButton}</figcaption></figure>`;
    }
    if (media.kind === 'video') {
      return `<figure class="media-item"><video controls preload="none" data-media-id="${mid}" data-kind="video"></video><figcaption class="media-actions">video · ${size} ${okTag}${downloadButton}</figcaption></figure>`;
    }
    if (media.kind === 'file') {
      const name = escapeHtml(media.name || media.file || 'file');
      return `<figure class="media-item"><figcaption class="media-actions">file ${name} · ${size} ${okTag}${downloadButton}</figcaption></figure>`;
    }
  }
  let label;
  if (media.kind === 'poke') {
    label = `poke${media.poke_label ? `·${media.poke_label}` : ''}${media.id ? ` · 目标 ${media.id}` : ''}`;
  } else if (media.kind === 'emoji' && media.id) {
    label = `emoji #${media.id}`;
  } else if (media.kind === 'forward' && media.id) {
    label = `forward #${media.id}`;
  } else if (media.kind === 'file') {
    label = media.name ? `file ${media.name}` : 'file';
  } else {
    label = media.kind;
  }
  const archiveKind = ARCHIVE_MEDIA_KINDS.has(media.kind);
  const stateClass = archiveKind && !media.archived ? 'not-archived' : 'plain';
  const stateTag = archiveKind && !media.archived ? '<span class="media-state-warn">未归档</span>' : '';
  return `<figure class="media-item ${stateClass}">
    <div class="media-chip">
      <span class="media-chip-label">${escapeHtml(label)}</span>
      ${stateTag}
    </div>
  </figure>`;
}

function attachDetailMedia(root) {
  root.querySelectorAll('[data-media-id]').forEach((element) => {
    const mediaId = element.dataset.mediaId;
    const kind = element.dataset.kind;
    const as = kind === 'image' ? 'thumb' : 'base64';
    apiGet(`media/${mediaId}`, { as })
      .then((response) => {
        const data = unwrap(response);
        const url = base64ToObjectUrl(data.data, data.mime);
        state.mediaObjectUrls.push(url);
        if (kind === 'image') {
          element.outerHTML = `<img class="media-image thumb" src="${url}" alt="历史图片" title="双击查看原图" />`;
          const img = root.querySelector(`img.media-image[src="${url}"]`);
          if (img) bindImageFullView(img, mediaId, url);
        } else if (kind === 'voice' || kind === 'video') {
          element.src = url;
        }
      })
      .catch((error) => {
        const message = (error && error.message) ? error.message : '';
        const hint = message.includes('过大') ? '（媒体过大，仅可下载）' : '（归档可能已清理，仍可尝试下载）';
        if (kind === 'image') {
          element.innerHTML = `图片加载失败${hint}`;
        } else {
          element.classList.add('failed');
          element.insertAdjacentHTML('afterend', `<div class="media-error">${kind} 加载失败${hint}</div>`);
        }
      });
  });
  root.querySelectorAll('[data-download-id]').forEach((button) => {
    button.addEventListener('click', () => {
      const mediaId = button.dataset.downloadId;
      const name = button.dataset.downloadName || 'download';
      button.disabled = true;
      button.textContent = '下载中…';
      downloadMedia(mediaId, name)
        .then(() => { button.disabled = false; button.textContent = '下载'; })
        .catch(() => { button.disabled = false; button.textContent = '下载失败'; });
    });
  });
}

function bindImageFullView(img, mediaId, thumbUrl) {
  let fullUrl = null;
  img.addEventListener('dblclick', () => {
    if (fullUrl) {
      img.src = thumbUrl;
      img.classList.add('thumb');
      img.classList.remove('full');
      // 切回缩略图后释放原图 objectURL，防反复切换泄漏
      try { URL.revokeObjectURL(fullUrl); } catch (error) { /* ignore */ }
      fullUrl = null;
      return;
    }
    img.classList.add('loading');
    fetchMediaObjectUrl(mediaId)
      .then((url) => {
        fullUrl = url;
        state.mediaObjectUrls.push(url);
        img.src = url;
        img.classList.remove('thumb');
        img.classList.add('full');
        img.classList.remove('loading');
      })
      .catch(() => img.classList.remove('loading'));
  });
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function highlight(value, keyword) {
  const text = String(value ?? '');
  const needle = String(keyword || '');
  if (!needle) return escapeHtml(text);
  const lower = text.toLocaleLowerCase();
  const lowerNeedle = needle.toLocaleLowerCase();
  const parts = [];
  let cursor = 0;
  while (cursor < text.length) {
    const index = lower.indexOf(lowerNeedle, cursor);
    if (index < 0) break;
    parts.push(escapeHtml(text.slice(cursor, index)));
    parts.push(`<mark>${escapeHtml(text.slice(index, index + needle.length))}</mark>`);
    cursor = index + needle.length;
  }
  parts.push(escapeHtml(text.slice(cursor)));
  return parts.join('');
}

function showToast(message, type = '') {
  const toast = document.createElement('div');
  toast.className = `toast ${type}`.trim();
  toast.textContent = message;
  $('#toast-stack').appendChild(toast);
  window.setTimeout(() => toast.remove(), 3600);
}

function setDatabaseState(database, error = '') {
  const dot = $('#sidebar-status-dot');
  const text = $('#sidebar-status-text');
  const banner = $('#database-banner');
  dot.classList.remove('ok', 'warning', 'error');
  banner.classList.remove('error');

  if (error) {
    dot.classList.add('error');
    text.textContent = '数据库不可用';
    $('#database-banner-title').textContent = '数据库不可用';
    $('#database-banner-copy').textContent = error;
    banner.classList.add('error');
    banner.classList.remove('hidden');
    return;
  }
  if (!database) return;
  if (database.mode === 'main_snapshot') {
    dot.classList.add('warning');
    text.textContent = '主库快照模式';
    $('#database-banner-title').textContent = '已回退到主库快照';
    $('#database-banner-copy').textContent = database.warning || 'WAL 旁路文件未参与当前查询。';
    banner.classList.remove('hidden');
  } else {
    dot.classList.add('ok');
    text.textContent = 'WAL 感知只读';
    banner.classList.add('hidden');
  }
}

function metricCard(icon, tone, value, label) {
  return `
    <article class="metric-card">
      <div class="metric-icon ${tone}">${icon}</div>
      <div class="metric-value">${escapeHtml(formatNumber(value))}</div>
      <div class="metric-label">${escapeHtml(label)}</div>
    </article>`;
}

const icons = {
  records: '<svg viewBox="0 0 24 24"><path d="M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"/><path d="M8 9h8M8 13h8M8 17h5"/></svg>',
  source: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="7" cy="6" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="17" cy="18" r="1"/></svg>',
  conversation: '<svg viewBox="0 0 24 24"><path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8l-5 3v-3H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z"/></svg>',
  users: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="4"/><path d="M3 21v-2a6 6 0 0 1 12 0v2M16 4.5a4 4 0 0 1 0 7M17 15a6 6 0 0 1 4 5.7"/></svg>',
  tools: '<svg viewBox="0 0 24 24"><path d="M14.7 6.3a4.5 4.5 0 0 0-6 6L3 18l3 3 5.7-5.7a4.5 4.5 0 0 0 6-6L14 13l-3-3 3.7-3.7Z"/></svg>',
  media: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m5 17 4.5-4.5 3 3L16 12l3 3"/></svg>',
};

function renderOverview(data) {
  state.overview = data;
  const summary = data.summary || {};
  const tools = data.tools || {};
  const media = data.media || {};
  const toolLabel = tools.present ? '工具调用' : '工具调用（无表）';
  const toolValue = tools.present ? tools.total_calls : '—';
  const mediaLabel = media.present ? '媒体归档' : '媒体归档（无表）';
  const mediaValue = media.present ? media.total_files : '—';
  $('#metrics-grid').innerHTML = [
    metricCard(icons.records, '', summary.total_records, '归档记录'),
    metricCard(icons.source, 'teal', summary.total_umos, 'UMO 来源'),
    metricCard(icons.conversation, '', summary.total_conversations, 'Conversation'),
    metricCard(icons.users, 'amber', summary.total_users, '独立用户'),
    metricCard(icons.tools, '', toolValue, toolLabel),
    metricCard(icons.media, 'teal', mediaValue, mediaLabel),
  ].join('');

  renderActivity(data.daily || []);
  renderDistribution(data.roles || {}, data.statuses || {}, tools.tool_names || {});
  renderDatabaseDetails(data.database || {});
  setDatabaseState(data.database);
}

function renderActivity(daily) {
  const chart = $('#activity-chart');
  if (!daily.length) {
    chart.innerHTML = '<div class="empty-state compact">近 30 天没有记录</div>';
    $('#activity-total').textContent = '0 条';
    return;
  }
  const maximum = Math.max(...daily.map((item) => Number(item.count || 0)), 1);
  const total = daily.reduce((sum, item) => sum + Number(item.count || 0), 0);
  $('#activity-total').textContent = `${formatNumber(total)} 条`;
  chart.innerHTML = daily.map((item, index) => {
    const height = Math.max(3, Math.round((Number(item.count || 0) / maximum) * 190));
    const showLabel = index % 5 === 0 || index === daily.length - 1;
    return `<div class="chart-bar-wrap" title="${escapeHtml(item.day)} · ${formatNumber(item.count)} 条">
      <div class="chart-bar" style="height:${height}px"></div>
      <div class="chart-day">${showLabel ? escapeHtml(String(item.day).slice(5)) : ''}</div>
    </div>`;
  }).join('');
}

function renderDistribution(roles, statuses, toolNames) {
  const groups = [
    { title: 'ROLE', values: roles, tone: '' },
    { title: 'LLM STATUS', values: statuses, tone: 'teal' },
    { title: 'TOOLS', values: toolNames, tone: 'amber' },
  ];
  $('#distribution-content').innerHTML = groups.map((group) => {
    const entries = Object.entries(group.values).sort((a, b) => Number(b[1]) - Number(a[1]));
    const max = Math.max(...entries.map(([, amount]) => Number(amount)), 1);
    const rows = entries.slice(0, 6).map(([label, amount]) => `
      <div class="progress-row">
        <div class="progress-label" title="${escapeHtml(label)}">${escapeHtml(label)}</div>
        <div class="progress-track"><div class="progress-fill ${group.tone}" style="width:${Math.max(2, Number(amount) / max * 100)}%"></div></div>
        <div class="progress-value">${formatNumber(amount)}</div>
      </div>`).join('');
    return `<div class="distribution-group"><div class="distribution-title"><span>${group.title}</span><span>${entries.length} 类</span></div>${rows || '<div class="empty-state compact">暂无数据</div>'}</div>`;
  }).join('');
}

function renderDatabaseDetails(database) {
  $('#database-details').innerHTML = `
    <div><dt>路径</dt><dd title="${escapeHtml(database.path || '')}">${escapeHtml(database.path || '—')}</dd></div>
    <div><dt>读取模式</dt><dd>${escapeHtml(database.mode || '—')}</dd></div>
    <div><dt>Schema</dt><dd>user_version=${escapeHtml(database.user_version ?? '—')}${database.tool_table_present ? ' · 工具表已存在' : ' · 无工具表（旧库）'}${database.media_table_present ? ' · 媒体归档表已存在' : ' · 无归档表（旧库）'}</dd></div>
    <div><dt>Journal</dt><dd>${escapeHtml(database.journal_mode || '—')} · 主库 ${escapeHtml(formatBytes(database.main_size))} · WAL ${escapeHtml(formatBytes(database.wal_size))}</dd></div>`;
}

function renderSettings(settings) {
  state.settings = settings;
  $('#database-path-input').value = settings.database_path || '';
  $('#immutable-fallback-input').checked = settings.immutable_fallback !== false;
  $('#default-database-path').textContent = settings.default_database_path || '—';
  $('#default-database-path').title = settings.default_database_path || '';
  $('#database-path-mode').textContent = settings.using_default_path ? '默认路径' : '自定义路径';
}

function setSettingsMessage(message = '', type = '') {
  const element = $('#database-settings-message');
  element.textContent = message;
  element.className = `source-settings-message ${type}`.trim();
}

function resetDataSourceState() {
  state.requestToken += 1;
  state.facetRequestToken += 1;
  state.toolRequestToken += 1;
  state.lastQuery = null;
  state.lastToolQuery = null;
  state.page = 1;
  state.pageCount = 0;
  state.total = 0;
  state.toolPage = 1;
  state.toolPageCount = 0;
  state.toolTotal = 0;
  $('#query-form').reset();
  $('#tool-query-form').reset();
  $('#filter-umo').value = '';
  $('#filter-conversation').value = '';
  $('#filter-user').value = '';
  $('#filter-persona').value = '__all__';
  $('#filter-role').value = '';
  $('#filter-status').value = '__all__';
  $('#filter-kind').value = '';
  $('#filter-platform').value = '';
  $('#filter-message-type').value = '';
  $('#filter-page-size').value = '50';
  $('#tool-filter-page-size').value = '50';
  $('#records-list').innerHTML = '<div class="empty-state"><strong>数据源已切换</strong><p>打开查询页后将读取新数据库</p></div>';
  $('#tools-list').innerHTML = '<div class="empty-state"><strong>数据源已切换</strong><p>打开工具调用页后将读取新数据库</p></div>';
  $('#result-summary').textContent = '载入后将显示最新记录';
  $('#tool-result-summary').textContent = '载入后将显示最近的工具调用';
  $('#pagination').classList.add('hidden');
  $('#tool-pagination').classList.add('hidden');
}

async function loadDashboardData() {
  const [overviewResponse, facetsResponse] = await Promise.all([
    apiGet('overview'), apiGet('facets', collectQuery(1)),
  ]);
  renderOverview(unwrap(overviewResponse));
  populateFacets(unwrap(facetsResponse));
}

async function saveDatabaseSettings(event) {
  event.preventDefault();
  const form = $('#database-settings-form');
  const submit = $('#save-database-settings');
  const payload = {
    database_path: $('#database-path-input').value.trim(),
    immutable_fallback: $('#immutable-fallback-input').checked,
  };
  form.classList.add('busy');
  submit.disabled = true;
  setSettingsMessage('正在校验数据库并保存…');
  try {
    const settings = unwrap(await apiPost('settings', payload));
    renderSettings(settings);
    resetDataSourceState();
    await loadDashboardData();
    setSettingsMessage('设置已保存，页面已切换到新的只读数据源。', 'success');
    showToast('数据库路径已更新');
  } catch (error) {
    setSettingsMessage(error.message, 'error');
    showToast(error.message, 'error');
  } finally {
    form.classList.remove('busy');
    submit.disabled = false;
  }
}

function restoreDefaultDatabasePath() {
  if (!state.settings?.default_database_path) return;
  $('#database-path-input').value = state.settings.default_database_path;
  $('#database-settings-form').requestSubmit();
}

function option(value, label, selectedValue = '') {
  return `<option value="${escapeHtml(value)}"${String(value) === String(selectedValue) ? ' selected' : ''}>${escapeHtml(label)}</option>`;
}

function facetOptions({ allValue, allLabel, items, current, valueOf, labelOf, missingLabel }) {
  const available = new Set(items.map((item) => String(valueOf(item))));
  const entries = [option(allValue, allLabel, current)];
  if (String(current) !== String(allValue) && !available.has(String(current))) {
    entries.push(option(current, `${missingLabel(current)} · 当前组合 0 条`, current));
  }
  entries.push(...items.map((item) => option(valueOf(item), labelOf(item), current)));
  return entries.join('');
}

function populateFacets(data) {
  state.facets = data;
  const current = {
    umo: $('#filter-umo').value,
    conversation: $('#filter-conversation').value,
    user: $('#filter-user').value,
    persona: $('#filter-persona').value,
    role: $('#filter-role').value,
    status: $('#filter-status').value,
    kind: $('#filter-kind').value,
    platform: $('#filter-platform').value,
    messageType: $('#filter-message-type').value,
  };
  $('#filter-umo').innerHTML = facetOptions({
    allValue: '', allLabel: '全部 UMO', items: data.umos || [], current: current.umo,
    valueOf: (item) => item.value,
    labelOf: (item) => {
      const suffix = item.message_type || item.platform_name || '';
      return `${compactId(item.value, 50)} · ${formatNumber(item.count)}${suffix ? ` · ${suffix}` : ''}`;
    },
    missingLabel: (value) => compactId(value, 50),
  });
  $('#filter-conversation').innerHTML = facetOptions({
    allValue: '', allLabel: '全部 CID', items: data.conversations || [], current: current.conversation,
    valueOf: (item) => item.value,
    labelOf: (item) => `${compactId(item.value, 34)} · ${formatNumber(item.count)}`,
    missingLabel: (value) => compactId(value, 34),
  });
  $('#filter-user').innerHTML = facetOptions({
    allValue: '', allLabel: '全部用户', items: data.users || [], current: current.user,
    valueOf: (item) => item.value,
    labelOf: (item) => `${item.nickname || '未命名'} · ${compactId(item.value, 24)} · ${formatNumber(item.count)}`,
    missingLabel: (value) => compactId(value, 24),
  });
  $('#filter-persona').innerHTML = facetOptions({
    allValue: '__all__', allLabel: '全部 Persona', items: data.personas || [], current: current.persona,
    valueOf: (item) => item.value ? item.value : '__empty__',
    labelOf: (item) => `${item.value || '未标记 Persona'} · ${formatNumber(item.count)}`,
    missingLabel: (value) => value === '__empty__' ? '未标记 Persona' : value,
  });
  $('#filter-role').innerHTML = facetOptions({
    allValue: '', allLabel: '全部 Role', items: data.roles || [], current: current.role,
    valueOf: (item) => item.value,
    labelOf: (item) => `${item.value} · ${formatNumber(item.count)}`,
    missingLabel: (value) => value,
  });
  $('#filter-status').innerHTML = facetOptions({
    allValue: '__all__', allLabel: '全部状态', items: data.statuses || [], current: current.status,
    valueOf: (item) => item.value ? item.value : '__empty__',
    labelOf: (item) => `${item.value || 'no_llm（空值）'} · ${formatNumber(item.count)}`,
    missingLabel: (value) => value === '__empty__' ? 'no_llm（空值）' : value,
  });
  $('#filter-kind').innerHTML = facetOptions({
    allValue: '', allLabel: '全部类型', items: data.kinds || [], current: current.kind,
    valueOf: (item) => item.value,
    labelOf: (item) => `${item.value} · ${formatNumber(item.count)}`,
    missingLabel: (value) => value,
  });
  $('#filter-platform').innerHTML = facetOptions({
    allValue: '', allLabel: '全部平台', items: data.platforms || [], current: current.platform,
    valueOf: (item) => item.value,
    labelOf: (item) => `${item.value} · ${formatNumber(item.count)}`,
    missingLabel: (value) => value,
  });
  $('#filter-message-type').innerHTML = facetOptions({
    allValue: '', allLabel: '全部消息类型', items: data.message_types || [], current: current.messageType,
    valueOf: (item) => item.value,
    labelOf: (item) => `${item.value} · ${formatNumber(item.count)}`,
    missingLabel: (value) => value,
  });

  // 工具调用页复用主表的 UMO / CID 下拉（不参与级联，仅静态选项）
  $('#tool-filter-umo').innerHTML = facetOptions({
    allValue: '', allLabel: '全部 UMO', items: data.umos || [], current: $('#tool-filter-umo').value,
    valueOf: (item) => item.value,
    labelOf: (item) => `${compactId(item.value, 50)} · ${formatNumber(item.count)}`,
    missingLabel: (value) => compactId(value, 50),
  });
  $('#tool-filter-conversation').innerHTML = facetOptions({
    allValue: '', allLabel: '全部 CID', items: data.conversations || [], current: $('#tool-filter-conversation').value,
    valueOf: (item) => item.value,
    labelOf: (item) => `${compactId(item.value, 34)} · ${formatNumber(item.count)}`,
    missingLabel: (value) => compactId(value, 34),
  });
}

function compactId(value, max) {
  const text = String(value || '');
  if (text.length <= max) return text;
  const side = Math.floor((max - 1) / 2);
  return `${text.slice(0, side)}…${text.slice(-side)}`;
}

async function loadFacets() {
  const token = ++state.facetRequestToken;
  const params = collectQuery(1);
  const data = unwrap(await apiGet('facets', params));
  if (token !== state.facetRequestToken) return;
  populateFacets(data);
  setDatabaseState(data.database);
}

function collectQuery(page = state.page) {
  return {
    keyword: $('#filter-keyword').value.trim(),
    umo: $('#filter-umo').value,
    conversation_id: $('#filter-conversation').value,
    user_id: $('#filter-user').value,
    persona_id: $('#filter-persona').value,
    role: $('#filter-role').value,
    llm_status: $('#filter-status').value,
    content_kind: $('#filter-kind').value,
    platform_name: $('#filter-platform').value,
    message_type: $('#filter-message-type').value,
    since: $('#filter-since').value,
    until: $('#filter-until').value,
    paired_only: $('#filter-paired').checked,
    page,
    page_size: Number($('#filter-page-size').value),
  };
}

async function runQuery(page = 1) {
  const token = ++state.requestToken;
  state.loading = true;
  state.page = page;
  const payload = collectQuery(page);
  state.lastQuery = payload;
  $('#records-list').innerHTML = '<div class="empty-state"><span class="loader"></span><strong>正在查询记录</strong><p>正在读取 ChatMemory 数据库</p></div>';
  $('#pagination').classList.add('hidden');
  try {
    const data = unwrap(await apiPost('query', payload));
    if (token !== state.requestToken) return;
    state.page = data.page;
    state.pageCount = data.page_count;
    state.total = data.total;
    renderRecords(data.items || [], payload.keyword);
    renderPagination();
    $('#result-summary').textContent = data.total
      ? `共 ${formatNumber(data.total)} 条 · 第 ${formatNumber(data.page)} / ${formatNumber(data.page_count)} 页`
      : '没有匹配当前条件的记录';
    setDatabaseState(data.database);
  } catch (error) {
    if (token !== state.requestToken) return;
    $('#records-list').innerHTML = `<div class="empty-state"><strong>查询失败</strong><p>${escapeHtml(error.message)}</p></div>`;
    $('#result-summary').textContent = '查询失败';
    setDatabaseState(null, error.message);
  } finally {
    if (token === state.requestToken) state.loading = false;
  }
}

function renderRecords(items, keyword) {
  if (!items.length) {
    $('#records-list').innerHTML = '<div class="empty-state"><strong>没有匹配记录</strong><p>尝试减少筛选条件或扩大时间范围</p></div>';
    return;
  }
  $('#records-list').innerHTML = items.map((record) => {
    const assistant = record.role === 'assistant';
    const sender = assistant ? 'Assistant' : (record.sender_nickname || record.user_id || 'User');
    const status = record.llm_status || 'no_llm';
    const kinds = Array.isArray(record.content_kind) ? record.content_kind : [];
    const badges = [
      `<span class="badge status">${escapeHtml(status)}</span>`,
      ...kinds.map((kind) => `<span class="badge kind">${escapeHtml(kind)}</span>`),
      record.persona_id ? `<span class="badge">persona:${escapeHtml(record.persona_id)}</span>` : '',
      `<span class="badge" title="${escapeHtml(record.umo)}">${escapeHtml(compactId(record.umo, 44))}</span>`,
    ].filter(Boolean).join('');
    const replyView = record.reply_view;
    const replyBlock = replyView
      ? `<div class="reply-quote" title="回复关系（resolution=${escapeHtml(replyView.resolution)}）">↩ 回复 <strong>${escapeHtml(replyView.target)}</strong>${replyView.text ? `：${escapeHtml(replyView.text)}` : ''}</div>`
      : '';
    return `<article class="record-card ${assistant ? 'assistant' : 'user'}">
      <div class="role-avatar">${assistant ? 'AI' : 'U'}</div>
      <div class="record-main">
        <div class="record-header"><span class="record-sender" title="${escapeHtml(sender)}">${escapeHtml(sender)}</span><span class="record-time">${escapeHtml(record.created_at || record.created_at_utc || '—')}</span></div>
        ${replyBlock}
        <div class="record-content">${highlight(record.content || '(空消息)', keyword)}${record.content_truncated ? '\n… 列表预览已截断，打开详情查看完整内容' : ''}</div>
        <div class="record-meta">${badges}</div>
      </div>
      <button class="record-detail-button" data-record-id="${record.record_id}">详情 #${record.record_id} ›</button>
    </article>`;
  }).join('');
  document.querySelectorAll('[data-record-id]').forEach((button) => {
    button.addEventListener('click', () => openDetail(button.dataset.recordId));
  });
}

function renderPagination() {
  const pagination = $('#pagination');
  if (!state.pageCount) {
    pagination.classList.add('hidden');
    return;
  }
  pagination.classList.remove('hidden');
  $('#page-label').textContent = `第 ${formatNumber(state.page)} / ${formatNumber(state.pageCount)} 页`;
  $('#page-prev').disabled = state.page <= 1;
  $('#page-next').disabled = state.page >= state.pageCount;
}

async function openDetail(id) {
  const modal = $('#detail-modal');
  modal.classList.remove('hidden');
  $('#detail-content').innerHTML = '<div class="empty-state compact"><span class="loader"></span></div>';
  try {
    const data = unwrap(await apiGet('record', { id }));
    renderDetail(data.record);
    setDatabaseState(data.database);
  } catch (error) {
    $('#detail-content').innerHTML = `<div class="empty-state compact"><strong>详情加载失败</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function renderDetail(record) {
  $('#detail-title').textContent = `记录 #${record.record_id}`;
  const omitted = new Set(['content', 'content_template', 'relation_data', 'reply_view', 'media_view']);
  const fields = Object.entries(record).filter(([key]) => !omitted.has(key));
  const relation = record.relation_data ? JSON.stringify(record.relation_data, null, 2) : 'null';
  const mentions = Array.isArray(record.relation_data?.mentions) ? record.relation_data.mentions : [];
  const mediaView = Array.isArray(record.media_view) ? record.media_view : [];
  const replyView = record.reply_view;
  const relationBlocks = [
    replyView ? `<section class="detail-section"><h3>引用（回复关系）</h3>
      <div class="detail-item"><div class="detail-key">回复对象</div><div class="detail-value">${escapeHtml(replyView.target)} · ${escapeHtml(replyView.resolution)}</div></div>
      ${replyView.text ? `<pre class="detail-json">${escapeHtml(replyView.text)}</pre>` : '<div class="detail-value">（turn 解析未找到目标原文）</div>'}
    </section>` : '',
    mediaView.length ? `<section class="detail-section"><h3>媒体（${mediaView.length}）</h3><div class="media-gallery">${mediaView.map((media) => mediaDetailHtml(media)).join('')}</div></section>` : '',
    mentions.length ? `<section class="detail-section"><h3>提及（At）</h3><div class="detail-grid">${mentions.map((mention) => `
      <div class="detail-item"><div class="detail-key">${mention.all ? '全体成员' : (mention.nickname || '未知成员')}</div><div class="detail-value">${escapeHtml(mention.all ? '@全体' : `@${mention.nickname || '未知成员'}`)}</div></div>`).join('')}</div></section>` : '',
  ].filter(Boolean).join('');
  $('#detail-content').innerHTML = `
    <div class="detail-message">${escapeHtml(record.content || '(空消息)')}</div>
    ${relationBlocks}
    <section class="detail-section"><h3>字段</h3><div class="detail-grid">${fields.map(([key, value]) => `
      <div class="detail-item"><div class="detail-key">${escapeHtml(key)}</div><div class="detail-value">${escapeHtml(Array.isArray(value) ? value.join(', ') : value ?? 'null')}</div></div>`).join('')}</div></section>
    <section class="detail-section"><h3>原始 content_template</h3><pre class="detail-json">${escapeHtml(record.content_template || '')}</pre></section>
    <section class="detail-section"><h3>relation_data</h3><pre class="detail-json">${escapeHtml(relation)}</pre></section>`;
  attachDetailMedia($('#detail-content'));
}

function collectToolQuery(page = state.toolPage) {
  return {
    tool_name: $('#tool-filter-name').value.trim(),
    umo: $('#tool-filter-umo').value,
    conversation_id: $('#tool-filter-conversation').value,
    turn_id: $('#tool-filter-turn').value.trim(),
    since: $('#tool-filter-since').value,
    until: $('#tool-filter-until').value,
    page,
    page_size: Number($('#tool-filter-page-size').value),
  };
}

async function runToolQuery(page = 1) {
  const token = ++state.toolRequestToken;
  state.toolPage = page;
  const payload = collectToolQuery(page);
  state.lastToolQuery = payload;
  $('#tools-list').innerHTML = '<div class="empty-state"><span class="loader"></span><strong>正在查询工具调用</strong><p>正在读取 chat_memory_tool_records</p></div>';
  $('#tool-pagination').classList.add('hidden');
  try {
    const data = unwrap(await apiPost('tools', payload));
    if (token !== state.toolRequestToken) return;
    state.toolPage = data.page;
    state.toolPageCount = data.page_count;
    state.toolTotal = data.total;
    renderToolRecords(data.items || []);
    renderToolPagination();
    $('#tool-result-summary').textContent = data.total
      ? `共 ${formatNumber(data.total)} 条 · 第 ${formatNumber(data.page)} / ${formatNumber(data.page_count)} 页`
      : '没有匹配当前条件的工具调用';
    setDatabaseState(data.database);
  } catch (error) {
    if (token !== state.toolRequestToken) return;
    $('#tools-list').innerHTML = `<div class="empty-state"><strong>查询失败</strong><p>${escapeHtml(error.message)}</p></div>`;
    $('#tool-result-summary').textContent = '查询失败';
    setDatabaseState(null, error.message);
  }
}

function renderToolRecords(items) {
  if (!items.length) {
    $('#tools-list').innerHTML = '<div class="empty-state"><strong>没有匹配的工具调用</strong><p>尝试减少筛选条件或扩大时间范围；旧版数据库（schema &lt; v4）没有工具表</p></div>';
    return;
  }
  $('#tools-list').innerHTML = items.map((record) => {
    let argsPreview = '';
    try {
      argsPreview = JSON.stringify(JSON.parse(record.tool_args || '{}'));
    } catch (error) {
      argsPreview = record.tool_args || '';
    }
    return `<article class="tool-card">
      <div class="tool-card-head">
        <span class="badge tool-name">${escapeHtml(record.tool_name || '(empty)')}</span>
        <span class="tool-card-time">${escapeHtml(record.created_at || record.created_at_utc || '—')} · #${record.call_index}</span>
      </div>
      <div class="tool-card-meta">turn <code>${escapeHtml(compactId(record.turn_id, 40))}</code> · <code title="${escapeHtml(record.umo)}">${escapeHtml(compactId(record.umo, 48))}</code></div>
      <div class="tool-block"><div class="tool-block-title">参数</div><pre class="tool-json">${escapeHtml(argsPreview)}</pre></div>
      <div class="tool-block"><div class="tool-block-title">返回</div><pre class="tool-json">${escapeHtml(record.tool_result || '(空)')}</pre></div>
    </article>`;
  }).join('');
}

function renderToolPagination() {
  const pagination = $('#tool-pagination');
  if (!state.toolPageCount) {
    pagination.classList.add('hidden');
    return;
  }
  pagination.classList.remove('hidden');
  $('#tool-page-label').textContent = `第 ${formatNumber(state.toolPage)} / ${formatNumber(state.toolPageCount)} 页`;
  $('#tool-page-prev').disabled = state.toolPage <= 1;
  $('#tool-page-next').disabled = state.toolPage >= state.toolPageCount;
}

function resetToolFilters() {
  $('#tool-query-form').reset();
  $('#tool-filter-page-size').value = '50';
  runToolQuery(1);
}

function closeDetail() {
  state.mediaObjectUrls.forEach((url) => {
    try { URL.revokeObjectURL(url); } catch (error) { /* ignore */ }
  });
  state.mediaObjectUrls = [];
  $('#detail-modal').classList.add('hidden');
  $('#detail-content').innerHTML = '';
}

function switchSection(target) {
  document.querySelectorAll('.page-section').forEach((section) => section.classList.toggle('active', section.id === target));
  document.querySelectorAll('.nav-button').forEach((button) => button.classList.toggle('active', button.dataset.target === target));
  const titles = {
    'query-section': '记录查询',
    'tools-section': '工具调用',
  };
  $('#page-title').textContent = titles[target] || '数据概览';
  if (target === 'query-section' && !state.lastQuery) runQuery(1);
  if (target === 'tools-section' && !state.lastToolQuery) runToolQuery(1);
}

function resetFilters() {
  $('#query-form').reset();
  $('#filter-persona').value = '__all__';
  $('#filter-status').value = '__all__';
  $('#filter-page-size').value = String(state.about?.default_page_size || 50);
  loadFacets().then(() => runQuery(1)).catch((error) => showToast(error.message, 'error'));
}

async function copyQuery() {
  const payload = state.lastQuery || collectQuery(1);
  try {
    await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
    showToast('查询条件已复制');
  } catch (error) {
    showToast('浏览器未允许复制', 'error');
  }
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'light';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('astrbot-theme', next); } catch (error) { /* ignore */ }
}

function bindEvents() {
  document.querySelectorAll('.nav-button').forEach((button) => button.addEventListener('click', () => switchSection(button.dataset.target)));
  $('#query-form').addEventListener('submit', (event) => { event.preventDefault(); runQuery(1); });
  $('#reset-filters').addEventListener('click', resetFilters);
  $('#copy-query-button').addEventListener('click', copyQuery);
  $('#page-prev').addEventListener('click', () => state.page > 1 && runQuery(state.page - 1));
  $('#page-next').addEventListener('click', () => state.page < state.pageCount && runQuery(state.page + 1));
  $('#tool-query-form').addEventListener('submit', (event) => { event.preventDefault(); runToolQuery(1); });
  $('#reset-tool-filters').addEventListener('click', resetToolFilters);
  $('#tool-page-prev').addEventListener('click', () => state.toolPage > 1 && runToolQuery(state.toolPage - 1));
  $('#tool-page-next').addEventListener('click', () => state.toolPage < state.toolPageCount && runToolQuery(state.toolPage + 1));
  $('#detail-close').addEventListener('click', closeDetail);
  $('#detail-modal').addEventListener('click', (event) => { if (event.target.id === 'detail-modal') closeDetail(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDetail(); });
  $('#theme-toggle').addEventListener('click', toggleTheme);
  $('#reload-button').addEventListener('click', initialize);
  $('#database-settings-form').addEventListener('submit', saveDatabaseSettings);
  $('#restore-database-path').addEventListener('click', restoreDefaultDatabasePath);
  FACET_SELECTORS.forEach((selector) => $(selector).addEventListener('change', async () => {
    try { await loadFacets(); } catch (error) { showToast(error.message, 'error'); }
  }));
}

async function initialize() {
  try {
    if (bridge && typeof bridge.ready === 'function') await bridge.ready();
    const [aboutResponse, settingsResponse] = await Promise.all([
      apiGet('about'), apiGet('settings'),
    ]);
    state.about = unwrap(aboutResponse);
    renderSettings(unwrap(settingsResponse));
    $('#version-label').textContent = `${state.about.name} · v${state.about.version}`;
    $('#filter-page-size').value = String(state.about.default_page_size || 50);
    setSettingsMessage('路径设置保存在 AstrBot 插件存储中。');
    try {
      await loadDashboardData();
    } catch (error) {
      setDatabaseState(null, error.message);
      $('#metrics-grid').innerHTML = `<div class="empty-state" style="grid-column:1/-1"><strong>无法读取 ChatMemory</strong><p>${escapeHtml(error.message)}</p></div>`;
      showToast(error.message, 'error');
    }
  } catch (error) {
    setDatabaseState(null, error.message);
    setSettingsMessage(`无法读取 UI 设置：${error.message}`, 'error');
    showToast(error.message, 'error');
  }
}

if (bridge && typeof bridge.onContext === 'function') {
  bridge.onContext((context) => {
    if (typeof context?.isDark === 'boolean') document.documentElement.setAttribute('data-theme', context.isDark ? 'dark' : 'light');
  });
}

bindEvents();
initialize();
