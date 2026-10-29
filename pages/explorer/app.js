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
  listMediaUrls: [],
  groupNames: {},
  groupNamesLoaded: false,
  voiceAudio: null,
  voiceUrls: [],
  voicePlayingId: null,
  detailRequestToken: 0,
  detailRecordId: null,
  // 列表是"第几条"（跨页连续），详情标题沿用这个编号；真实 id 只用于跳转
  // 三种模式：只读（默认）/ 高级只读（加轮次 chip）/ 删除（出现单条软删除图标）
  deleteMode: false,
  advancedMode: false,
  personaOptions: [],
  pairIndex: new Map(),
  toolItems: new Map(),
};

// 下拉改动即重查的控件（文本类走"查询记录"按钮）
const FACET_SELECTORS = [
  '#filter-chat-type',
  '#filter-kind',
  '#filter-event-type',
  '#filter-deleted',
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
  return readJson(response);
}

async function readJson(response) {
  // 401/网关错误会返回 HTML，直接 response.json() 只会抛 "Unexpected token '<'"
  const type = (response.headers.get('content-type') || '').toLowerCase();
  if (!response.ok || !type.includes('json')) {
    throw new Error(`请求失败（HTTP ${response.status}）`);
  }
  return response.json();
}

async function apiPost(endpoint, body) {
  if (bridge && typeof bridge.apiPost === 'function') return bridge.apiPost(endpoint, body);
  const response = await fetch(`/api/plug/${PLUGIN_NAME}/${endpoint}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return readJson(response);
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

// 会话类型是消息表自己的列（message_type），列表里给一个可读标签
const CHAT_TYPE_LABELS = {
  GroupMessage: '群聊',
  FriendMessage: '私聊',
  GroupTempMessage: '群临时会话',
};

function subtypeBadges(record) {
  // 只保留消息自身派生的信息：事件子类（relation_data.event.type）。
  // media·<kind> 与上面的 kinds 徽章重复，媒体实际内容看缩略图/详情，不再重复标。
  const eventType = record.relation_data
    && record.relation_data.event
    && record.relation_data.event.type;
  return eventType
    ? [`<span class="badge sub">event·${escapeHtml(eventType)}</span>`]
    : [];
}

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
      // 详情上方消息正文已展示内联缩略图（可双击看原图），媒体栏不再重复挂图
      return `<figure class="media-item"><div class="media-chip"><span class="media-chip-label">image</span><span class="media-state-ok">已归档</span></div><figcaption class="media-actions">${size}${downloadButton}</figcaption></figure>`;
    }
    if (media.kind === 'voice') {
      return `<figure class="media-item"><audio controls preload="none" data-media-id="${mid}" data-kind="voice"></audio><figcaption class="media-actions">voice · ${size} ${okTag}${downloadButton}</figcaption></figure>`;
    }
    if (media.kind === 'video') {
      return `<figure class="media-item"><video controls preload="none" data-media-id="${mid}" data-kind="video" data-size="${escapeHtml(media.size || 0)}"></video><figcaption class="media-actions">video · ${size} ${okTag}${downloadButton}</figcaption></figure>`;
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
    // 中文名已由正文占位（⟦表情:问号脸⟧）展示，chip 保持简洁
    label = 'emoji';
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
    if (kind === 'video') {
      // 播放回退链：直连原始字节流（Range 流式，需同源鉴权；iframe 拿不到
      // dashboard JWT 时会 401）→ base64 桥接播放。沙箱 iframe（sandbox 无
      // allow-same-origin，不透明源）里直接 fetch /api/file 会被 CORS 拦截，
      // 媒体字节统一走 bridge；file_token 仅作 8ms 的元数据探测（编码/大小）。
      const rawUrl = `/api/plug/${PLUGIN_NAME}/media/${mediaId}`;
      const BASE64_MAX_BYTES = 16 * 1024 * 1024;
      const knownSize = Number(element.dataset.size || 0);
      const playBlob = (blobUrl) => {
        state.mediaObjectUrls.push(blobUrl);
        element.classList.remove('loading');
        element.src = blobUrl;
        // 第一次点击 play 时 src 还是 401 直连源，会被 error 打断；
        // 回退源就绪后自动续播，让“一次点击”就进入播放状态。
        const attempt = element.play();
        if (attempt && typeof attempt.catch === 'function') {
          attempt.catch(() => { /* 自动播放被拦截时停在首帧，用户再点播放 */ });
        }
      };
      const showPlaybackError = (hint) => {
        if (element.dataset.failed === '1') return;
        element.dataset.failed = '1';
        element.classList.remove('loading');
        element.classList.add('failed');
        element.insertAdjacentHTML('afterend',
          `<div class="media-error">video 无法播放（${hint}），请用下载按钮保存后本地观看</div>`);
      };
      const canPlayHevc = () => {
        const probe = document.createElement('video');
        return probe.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') !== ''
          || probe.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"') !== '';
      };
      if (knownSize > BASE64_MAX_BYTES) {
        showPlaybackError(`视频过大（${formatBytes(knownSize)}），无法内联播放`);
        return;
      }
      element.addEventListener('error', () => {
        if (element.dataset.fallback === '1') {
          // base64 兜底也解不出来：编码不受浏览器支持
          showPlaybackError('浏览器可能不支持该视频编码');
          return;
        }
        element.dataset.fallback = '1';
        element.classList.add('loading');
        let blocked = false;
        apiGet(`media/${mediaId}`, { as: 'file_token' })
          .then((response) => unwrap(response))
          .then((data) => {
            if (data && data.codec === 'hevc' && !canPlayHevc()) {
              blocked = true;
              showPlaybackError('视频为 HEVC 编码，当前浏览器不支持');
            } else if (Number((data && data.size) || 0) > BASE64_MAX_BYTES) {
              blocked = true;
              showPlaybackError(`视频过大（${formatBytes((data && data.size) || 0)}），无法内联播放`);
            }
          })
          .catch((error) => {
            console.error('[chat_memory] video 元数据探测失败：', error);
          })
          .then(() => {
            if (blocked) return;
            return apiGet(`media/${mediaId}`, { as: 'base64' })
              .then((response) => {
                const data = unwrap(response);
                playBlob(base64ToObjectUrl(data.data, data.mime));
              })
              .catch((error) => {
                const message = (error && error.message) ? error.message : '';
                showPlaybackError(message.includes('过大') ? '媒体过大，无法内联播放' : '加载失败');
              });
          });
      });
      element.src = rawUrl;
      return;
    }
    apiGet(`media/${mediaId}`, { as: 'base64' })
      .then((response) => {
        const data = unwrap(response);
        const url = base64ToObjectUrl(data.data, data.mime);
        state.mediaObjectUrls.push(url);
        if (kind === 'voice') {
          element.src = url;
        }
      })
      .catch((error) => {
        const message = (error && error.message) ? error.message : '';
        const hint = message.includes('过大') ? '（媒体过大，仅可下载）' : '（归档可能已清理，仍可尝试下载）';
        element.classList.add('failed');
        element.insertAdjacentHTML('afterend', `<div class="media-error">${kind} 加载失败${hint}</div>`);
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

function hydrateInlineThumbs(root, urlStore, allowFullView) {
  // ⟦CM_IMG:<id>⟧ → 内联缩略图：先替换文本标记，再逐个经 thumb 接口加载。
  // urlStore 由调用方提供：列表用 listMediaUrls（重渲染前撤销），详情用
  // mediaObjectUrls（closeDetail 撤销），避免交叉撤销导致列表坏图/泄漏。
  // allowFullView：详情内允许双击切原图；列表缩略图只作预览，不允许查看原图。
  root.querySelectorAll('.record-content, .detail-message').forEach((container) => {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) {
      if (/⟦CM_IMG:[0-9a-f]{32}⟧/.test(walker.currentNode.textContent)) {
        textNodes.push(walker.currentNode);
      }
    }
    textNodes.forEach((node) => {
      const fragment = document.createDocumentFragment();
      const text = node.textContent;
      const pattern = /⟦CM_IMG:([0-9a-f]{32})⟧/g;
      let last = 0;
      let match;
      while ((match = pattern.exec(text)) !== null) {
        if (match.index > last) {
          fragment.appendChild(document.createTextNode(text.slice(last, match.index)));
        }
        const img = document.createElement('img');
        img.className = 'media-thumb-inline';
        img.alt = '历史图片';
        img.dataset.inlineImg = match[1];
        fragment.appendChild(img);
        last = match.index + match[0].length;
      }
      if (last < text.length) {
        fragment.appendChild(document.createTextNode(text.slice(last)));
      }
      node.replaceWith(fragment);
    });
    container.querySelectorAll('img[data-inline-img]').forEach((img) => {
      apiGet(`media/${img.dataset.inlineImg}`, { as: 'thumb' })
        .then((response) => {
          const data = unwrap(response);
          const url = base64ToObjectUrl(data.data, data.mime);
          urlStore.push(url);
          img.src = url;
          if (allowFullView) {
            img.title = '双击查看原图';
            bindImageFullView(img, img.dataset.inlineImg, url);
          } else {
            img.classList.add('static');
          }
        })
        .catch(() => {
          img.alt = '图片加载失败';
          img.classList.add('failed');
        });
    });
  });
}

function bindImageFullView(img, mediaId, thumbUrl) {
  let fullUrl = null;
  const dropFullUrl = () => {
    if (!fullUrl) return;
    // 只在这里撤销：同时从详情资源表移除，避免 closeDetail 二次撤销
    try { URL.revokeObjectURL(fullUrl); } catch (error) { /* ignore */ }
    state.mediaObjectUrls = state.mediaObjectUrls.filter((url) => url !== fullUrl);
    fullUrl = null;
  };
  img.addEventListener('dblclick', () => {
    if (fullUrl) {
      img.src = thumbUrl;
      img.classList.add('thumb');
      img.classList.remove('full');
      dropFullUrl();
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
      .catch(() => {
        img.classList.remove('loading');
        img.title = '原图加载失败（可能过大或编码不支持）';
      });
  });
}

// 删除模式图标：times=软删除单条，backup=恢复
const TIMES_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
const BACKUP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12a8 8 0 1 0 2.6-5.9"/><path d="M4 4v5h5"/></svg>';

function deletedStateChip(record) {
  // 软删除 chip：删除后在该条下方展示
  if (!record || !record.deleted) return '';
  return '<div class="record-deleted-chip"><span class="chip-deleted">deleted</span></div>';
}

function modeButtonHtml(rowId, deleted, target = 'record') {
  // 删除模式下的单条操作图标：未删除=times（软删除），已删除=backup（恢复）
  if (!state.deleteMode) return '';
  const action = deleted ? 'restore' : 'soft-delete';
  const icon = deleted ? BACKUP_ICON : TIMES_ICON;
  const what = target === 'tool' ? '工具调用' : '记录';
  const label = `${deleted ? '恢复' : '软删除'}此${what}`;
  return `<button type="button" class="record-mode-button ${action}" data-mode-action="${action}" data-mode-id="${rowId}" data-mode-target="${target}" title="${label}" aria-label="${label}">${icon}</button>`;
}

function recordModeButton(record) {
  return modeButtonHtml(record.record_id, record.deleted);
}

function applyDeletedState(recordId, deleted) {
  // 原地更新该条：chip 出现/消失 + 图标在 times ↔ backup 之间切换。
  // 不整表重查——删除后仍要能看到 chip 与 backup 图标（下次查询才按筛选收敛）。
  const card = document.querySelector(`article[data-card-id="${recordId}"]`);
  if (!card) return;
  const main = card.querySelector('.record-main');
  const existing = main ? main.querySelector('.record-deleted-chip') : null;
  if (deleted && main && !existing) {
    const chip = document.createElement('div');
    chip.className = 'record-deleted-chip';
    chip.innerHTML = '<span class="chip-deleted">deleted</span>';
    const meta = main.querySelector('.record-meta');
    main.insertBefore(chip, meta || null);
  } else if (!deleted && existing) {
    existing.remove();
  }
  const button = card.querySelector('[data-mode-action]');
  if (button) {
    button.outerHTML = modeButtonHtml(recordId, deleted);
  }
  bindModeButtons(card);
}

const VOICE_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>';

function stopVoicePlaying() {
  // 停止列表语音气泡播放并回收资源（列表重渲染前必须调用）
  if (state.voiceAudio) {
    state.voiceAudio.pause();
    try { state.voiceAudio.src = ''; } catch (error) { /* ignore */ }
    state.voiceAudio = null;
  }
  state.voicePlayingId = null;
  state.voiceUrls.forEach((url) => {
    try { URL.revokeObjectURL(url); } catch (error) { /* ignore */ }
  });
  state.voiceUrls = [];
  document.querySelectorAll('[data-voice-id]').forEach((bubble) => {
    bubble.classList.remove('playing');
    bubble.dataset.playing = '';
    bubble.dataset.url = '';
  });
}

function bindVoiceBubbles(root) {
  // QQ 风格语音气泡：点击即播（file_token → blob → Audio）。
  // voice 一定是独立消息，正文位置直接渲染气泡，无需考虑混合消息。
  root.querySelectorAll('[data-voice-id]').forEach((bubble) => {
    bubble.addEventListener('click', () => {
      const voiceId = bubble.dataset.voiceId;
      if (bubble.dataset.playing === '1') {
        stopVoicePlaying();
        return;
      }
      stopVoicePlaying();
      bubble.classList.add('playing');
      bubble.dataset.playing = '1';
      state.voicePlayingId = voiceId;
      const playUrl = (url) => {
        const audio = new Audio(url);
        state.voiceAudio = audio;
        audio.onended = () => stopVoicePlaying();
        audio.onerror = () => {
          stopVoicePlaying();
          showToast('语音播放失败', 'error');
        };
        audio.play().catch(() => {
          stopVoicePlaying();
          showToast('语音播放失败', 'error');
        });
      };
      if (bubble.dataset.url) {
        playUrl(bubble.dataset.url);
        return;
      }
      // 沙箱 iframe（sandbox 无 allow-same-origin，不透明源）里直接 fetch
      // /api/file 会被 CORS 拦截；语音文件小，字节统一走 bridge 的 base64 通道。
      apiGet(`media/${voiceId}`, { as: 'base64' })
        .then((response) => unwrap(response))
        .then((data) => {
          if (!data || !data.data) throw new Error('语音数据缺失');
          const url = base64ToObjectUrl(data.data, data.mime);
          state.voiceUrls.push(url);
          bubble.dataset.url = url;
          playUrl(url);
        })
        .catch((error) => {
          console.error('[chat_memory] voice 加载失败：', error);
          stopVoicePlaying();
          showToast('语音加载失败', 'error');
        });
    });
  });
}

const PERSONA_EMPTY = '__empty__';

function personaFilterValue() {
  const value = $('#filter-persona').value;
  return value === PERSONA_EMPTY ? PERSONA_EMPTY : value;
}

// 工具页的 persona 下拉与记录页同构：空串=all（不发送）、哨兵=「未标记 persona」。
function toolPersonaValue() {
  const value = $('#tool-filter-persona').value;
  if (!value) return undefined;          // all：不带该键
  return value === PERSONA_EMPTY ? PERSONA_EMPTY : value;
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
  const toolDeleted = Number(tools.deleted_calls || 0);
  const toolLabel = tools.present
    ? (toolDeleted > 0 ? `工具调用（已删 ${toolDeleted}）` : '工具调用')
    : '工具调用（无表）';
  const toolValue = tools.present ? tools.total_calls : '—';
  const mediaLabel = media.present ? '媒体归档' : '媒体归档（无表）';
  const mediaValue = media.present ? media.total_files : '—';
  const deletedCount = Number(summary.deleted_records || 0);
  $('#metrics-grid').innerHTML = [
    metricCard(icons.records, '', summary.total_records, '归档记录'),
    metricCard(icons.source, 'teal', summary.total_umos, 'UMO 来源'),
    metricCard(icons.conversation, '', summary.total_conversations, 'Conversation'),
    metricCard(icons.users, 'amber', summary.total_users, '独立用户'),
    metricCard(icons.tools, '', toolValue, toolLabel),
    metricCard(icons.media, 'teal', mediaValue, mediaLabel),
    // 软删除计数只在存在时展示（明细见记录查询的「删除状态=已删除」）
    deletedCount > 0
      ? metricCard(icons.records, 'amber', deletedCount, '已删除（软删）')
      : '',
  ].join('');

  renderActivity(data.daily || []);
  renderDistribution(data.roles || {}, data.statuses || {}, tools.tool_names || {});
  renderDatabaseDetails(data.database || {});
  setDatabaseState(data.database);
}

function renderActivity(daily) {
  const chart = $('#activity-chart');
  if (!daily.length) {
    // 空图必须能区分"最近 30 天确实没数据"和"统计坏了"：把库里实际的最新/最旧时间写出来
    const summary = (state.overview && state.overview.summary) || {};
    const newest = summary.newest_at || '—';
    const oldest = summary.oldest_at || '—';
    chart.innerHTML =
      '<div class="empty-state compact">近 30 天没有记录' +
      `<p>库里最新一条：${escapeHtml(newest)} · 最旧：${escapeHtml(oldest)}</p></div>`;
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
  stopVoicePlaying();
  state.listMediaUrls.forEach((url) => {
    try { URL.revokeObjectURL(url); } catch (error) { /* ignore */ }
  });
  state.listMediaUrls = [];
  state.requestToken += 1;
  state.facetRequestToken += 1;
  state.toolRequestToken += 1;
  // 群名是"按库现查"的：换库后不清会继续显示上一个库的群名（同一群号在别的库里可能不存在）
  state.groupNames = {};
  state.groupNamesLoaded = false;
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
  $('#filter-chat-type').value = '';
  $('#filter-session').value = '';
  $('#filter-sender').value = '';
  $('#filter-kind').value = '';
  $('#filter-event-type').value = '';
  $('#filter-persona').value = '';
  $('#filter-persona-role').value = '';
  $('#filter-persona-status').value = '__all__';
  $('#filter-paired-only').value = '';
  $('#filter-deleted').value = 'visible';
  $('#tool-filter-deleted').value = 'visible';
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
    // 数据源可能变成/变回插件实时库：刷新 write_enabled 并退出删除模式，
    // 否则会出现"图标还在但点必 403"或"已可写却被前端拦住"
    setDeleteMode(false);
    try {
      state.about = unwrap(await apiGet('about'));
      if (state.about.write_enabled === false) {
        $('#mode-toggle').title = '当前数据源不是插件实时数据库，软删除/恢复已禁用';
      } else {
        $('#mode-toggle').title = '点击进入删除模式：可对单条记录做软删除/恢复（仅插件实时库开放）';
      }
    } catch (error) {
      state.about = null;
    }
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
  // 现在只有「内容类型」需要候选；其余是手填或固定选项
  $('#filter-kind').innerHTML = facetOptions({
    allValue: '', allLabel: 'all', items: data.kinds || [],
    current: $('#filter-kind').value,
    valueOf: (item) => item.value,
    labelOf: (item) => `${item.value} · ${formatNumber(item.count)}`,
    missingLabel: (value) => value,
  });
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
  // 只有这一组条件：聊天类型 / QQ号 / 发言者 / 内容类型 / 事件子类 / 时间 / 删除状态
  return {
    keyword: $('#filter-keyword').value.trim(),
    chat_type: $('#filter-chat-type').value,
    session_id: $('#filter-session').value.trim(),
    sender: $('#filter-sender').value.trim(),
    content_kind: $('#filter-kind').value,
    event_type: $('#filter-event-type').value,
    since: $('#filter-since').value,
    until: $('#filter-until').value,
    deleted_state: $('#filter-deleted').value,
    // 高级模式必须选中 persona 才带这些参数；"未标记"用哨兵值原样发给后端
    persona_id: (state.advancedMode && personaFilterValue()) || undefined,
    advanced: state.advancedMode && Boolean(personaFilterValue()),
    persona_role: (state.advancedMode && $('#filter-persona-role').value) || undefined,
    persona_llm_status: (state.advancedMode && $('#filter-persona-status').value) || undefined,
    paired_only: Boolean(state.advancedMode && $('#filter-paired-only').value),
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

function speakerAvatarHtml(record, sender) {
  // 头像直接用 QQ 现成接口（按 QQ 号），不落库、不经插件；取不到就退回首字母
  const senderId = String(record.sender_id || record.user_id || '').trim();
  const initial = escapeHtml((String(sender || '').trim() || '?').slice(0, 1));
  const title = escapeHtml(sender);
  if (!/^[0-9]{5,12}$/.test(senderId)) {
    return `<div class="speaker-avatar" title="${title}"><span class="avatar-initial">${initial}</span></div>`;
  }
  const url = `https://q1.qlogo.cn/g?b=qq&nk=${senderId}&s=100`;
  return `<div class="speaker-avatar" title="${title}">`
    + `<img src="${url}" alt="" loading="lazy" referrerpolicy="no-referrer"`
    + ` onerror="this.style.display='none';this.nextElementSibling.style.display='grid'">`
    + `<span class="avatar-initial" style="display:none">${initial}</span></div>`;
}

async function hydrateGroupNames(root) {
  // 群名不落库：一次请求拿全量映射（插件侧 get_group_list + 6 小时缓存），
  // 页面内缓存住，chip 上补成 群聊·群号（群名）
  const chips = Array.from(root.querySelectorAll('[data-group-id]'));
  if (!chips.length) return;
  if (!state.groupNamesLoaded) {
    state.groupNamesLoaded = true;
    try {
      const data = unwrap(await apiGet('group-names'));
      state.groupNames = (data && data.groups) || {};
    } catch (error) {
      // 失败要回滚标记：否则一次网络抖动会让本页后续所有列表都不再补群名。
      state.groupNames = {};
      state.groupNamesLoaded = false;
    }
  }
  chips.forEach((chip) => {
    const name = state.groupNames[chip.dataset.groupId];
    if (name && !chip.dataset.named) {
      chip.dataset.named = '1';
      chip.textContent = `${chip.dataset.label}（${name}）`;
      chip.title = `${chip.dataset.label} · ${name}`;
    }
  });
}

// 高级模式：同一轮的消息共用一个配对号（列表里第几个轮次），user/assistant 一致
function pairIndexOf(note) {
  const turn = String((note && note.turn_id) || '');
  if (!turn) return 0;
  if (!state.pairIndex.has(turn)) state.pairIndex.set(turn, state.pairIndex.size + 1);
  return state.pairIndex.get(turn);
}

// 高级只读模式的状态配色：成功=青、主动=琥珀、等待=蓝、孤儿/单边=红、空=none
const STATUS_CLASS = {
  llm_success: 'status-success',
  proactive: 'status-proactive',
  llm_pending: 'status-pending',
  orphan: 'status-orphan',
};

function advancedBadges(record) {
  // 高级只读模式：这条消息在所选 persona 下的逻辑轮次与状态（数据来自参与行）
  if (!state.advancedMode) return [];
  const kinds = Array.isArray(record.content_kind) ? record.content_kind : [];
  if (kinds.includes('event') || kinds.includes('system_event')) {
    // 事件不是"某一轮的提问或回答"：角色/配对/LLM 状态对它都没有意义。
    // 是谁触发的看头像+昵称就够了，这里不再加任何 chip。
    return [];
  }
  const note = record.advanced;
  if (!note) return [];
  const turn = String(note.turn_id || '');
  const pairNo = note.paired ? pairIndexOf(note) : 0;
  // 状态取"这一轮回复的状态"：本条若是 user 行，它自己的 llm_status 本来就是空的
  const status = note.turn_llm_status || note.llm_status || '';
  const sendStatus = note.turn_send_status || note.send_status || '';
  const role = String(note.role || '');
  return [
    note.paired
      ? pairNo
        ? `<span class="badge ok" title="轮次 ${escapeHtml(turn)}（第 ${pairNo} 对）">已配对#${pairNo}</span>`
        : ''
      : '<span class="badge warn" title="这一轮没有 assistant 行（或不是轮次消息）">未配对</span>',
    role
      ? `<span class="badge ${role === 'assistant' ? 'kind' : 'role-user'}">${escapeHtml(role)}</span>`
      : '',
    `<span class="badge ${STATUS_CLASS[status] || 'status-none'}">${escapeHtml(status || 'no_llm')}</span>`,
    sendStatus
      ? `<span class="badge ${sendStatus === 'send_attempted' ? 'send-attempted' : 'send-prepared'}">${escapeHtml(sendStatus)}</span>`
      : '',
  ].filter(Boolean);
}

function renderRecords(items, keyword) {
  stopVoicePlaying();
  state.listMediaUrls.forEach((url) => {
    try { URL.revokeObjectURL(url); } catch (error) { /* ignore */ }
  });
  state.listMediaUrls = [];
  state.pairIndex = new Map();
  if (!items.length) {
    $('#records-list').innerHTML = '<div class="empty-state"><strong>没有匹配记录</strong><p>尝试减少筛选条件或扩大时间范围</p></div>';
    return;
  }
  $('#records-list').innerHTML = items.map((record) => {
    // 不区分"自己发的/别人发的"：role 在合并后不可靠，界面上一律同等对待
    const sender = record.sender_nickname || record.user_id || record.sender_id || '未知成员';
    const kinds = Array.isArray(record.content_kind) ? record.content_kind : [];
    const chatType = CHAT_TYPE_LABELS[String(record.message_type || '')] || '';
    const sessionId = String(record.session_id || '').trim();
    // 会话类型和会话号绑在一起显示（群聊·群号 / 私聊·对方号）：单看"群聊"没有信息量
    const chatLabel = sessionId ? `${chatType}·${sessionId}` : chatType;
    const chatChip = chatType
      ? `<span class="badge"${sessionId && chatType === '群聊' ? ` data-group-id="${escapeHtml(sessionId)}" data-label="${escapeHtml(chatLabel)}"` : ''} title="${escapeHtml(`${chatType} ${sessionId || '（无会话号）'}`)}">${escapeHtml(chatLabel)}</span>`
      : '';
    // chip 只放「消息表自己的东西」：内容类型、事件子类、会话、撤回状态。
    // llm_status / persona / umo / cid 属于参与行（哪个实例看到的、归哪一轮），
    // 合并存储之后同一条消息它们的值各不相同，展示出来只会误导。
    const badges = [
      ...kinds.map((kind) => `<span class="badge kind">${escapeHtml(kind)}</span>`),
      ...subtypeBadges(record),
      chatChip,
      record.recalled ? '<span class="badge recalled">已撤回</span>' : '',
      ...advancedBadges(record),
    ].filter(Boolean).join('');
    const replyView = record.reply_view;
    const replyBlock = replyView
      ? `<div class="reply-quote" title="回复关系（resolution=${escapeHtml(replyView.resolution)}）">↩ 回复 <strong>${escapeHtml(replyView.target)}</strong>${replyView.text ? `：${escapeHtml(replyView.text)}` : ''}</div>`
      : '';
    // 事件正文展示与上下文一致的自然语言句子（后端已组装，见 _ui_event_sentence）；
    // 纯语音消息：正文位置直接渲染 QQ 风格语音气泡（voice 一定是独立消息）。
    const voiceEntry = Array.isArray(record.media_view)
      ? record.media_view.find((item) => item && item.kind === 'voice')
      : null;
    const isPureVoice = kinds.length === 1 && kinds[0] === 'voice';
    let contentBlock;
    if (isPureVoice && voiceEntry && voiceEntry.archived && voiceEntry.media_id) {
      contentBlock = `<div class="record-content"><span class="voice-bubble" data-voice-id="${escapeHtml(voiceEntry.media_id)}">${VOICE_ICON}<span class="voice-bars" aria-hidden="true"><span></span><span></span><span></span></span><span class="voice-label">语音</span></span></div>`;
    } else if (isPureVoice) {
      contentBlock = '<div class="record-content"><span class="voice-bubble disabled">' + VOICE_ICON + '<span class="voice-label">语音（未归档）</span></span></div>';
    } else {
      contentBlock = `<div class="record-content">${highlight(record.content || '(空消息)', keyword)}${record.content_truncated ? '\n… 列表预览已截断，打开详情查看完整内容' : ''}</div>`;
    }
    return `<article class="record-card" data-card-id="${record.record_id}">
      ${speakerAvatarHtml(record, sender)}
      <div class="record-main">
        <div class="record-header"><span class="record-sender" title="${escapeHtml(sender)}">${escapeHtml(sender)}</span><span class="record-time">${escapeHtml(record.created_at || record.created_at_utc || '—')}</span></div>
        ${replyBlock}
        ${contentBlock}
        ${deletedStateChip(record)}
        <div class="record-meta">${badges}</div>
      </div>
      <div class="record-actions">
        ${recordModeButton(record)}
        <button class="record-detail-button" data-record-id="${record.record_id}">详情 ›</button>
      </div>
    </article>`;
  }).join('');
  hydrateInlineThumbs($('#records-list'), state.listMediaUrls, false);
  hydrateGroupNames($('#records-list'));
  bindVoiceBubbles($('#records-list'));
  document.querySelectorAll('[data-record-id]').forEach((button) => {
    button.addEventListener('click', () => openDetail(button.dataset.recordId));
  });
  bindModeButtons($('#records-list'));
}

function bindModeButtons(root) {
  // 删除模式的单条软删除 / 恢复：查询台唯一的写操作（后端仅对插件实时库开放）
  root.querySelectorAll('[data-mode-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const recordId = button.dataset.modeId;
      const action = button.dataset.modeAction;
      const target = button.dataset.modeTarget || 'record';
      button.disabled = true;
      apiPost(action === 'restore' ? 'restore' : 'soft-delete', {
        id: recordId,
        target,
      })
        .then((response) => {
          const result = unwrap(response) || {};
          const deleted = Boolean(result.deleted);
          const what = target === 'tool' ? '工具调用' : '记录';
          // 提示里的编号也用列表位置编号（真实 id 只在详情括号里出现）
          showToast(deleted ? `已软删除${what}` : `已恢复${what}`, 'ok');
          if (target === 'tool') {
            // 工具卡片没有独立详情页：原地更新该卡片（chip + 删除时间 + 图标）
            applyToolDeletedState(recordId, deleted, result.deleted_at || '');
          } else {
            applyDeletedState(recordId, deleted);
            const modal = $('#detail-modal');
            if (modal && !modal.classList.contains('hidden')) {
              openDetail(recordId);
            }
          }
          lockSnapshot().then(() => loadDashboardData());
        })
        .catch((error) => {
          button.disabled = false;
          showToast(`${action === 'restore' ? '恢复' : '删除'}失败：${error.message}`, 'error');
        });
    });
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
  // 详情也有竞态：慢响应可能在关闭/换记录之后到达，用 token 丢弃过期结果
  const token = ++state.detailRequestToken;
  modal.classList.remove('hidden');
  $('#detail-content').innerHTML = '<div class="empty-state compact"><span class="loader"></span></div>';
  try {
    const data = unwrap(await apiGet('record', { id }));
    if (token !== state.detailRequestToken) return;
    state.detailRecordId = Number(id);
    renderDetail(data.record);
    setDatabaseState(data.database);
  } catch (error) {
    if (token !== state.detailRequestToken) return;
    $('#detail-content').innerHTML = `<div class="empty-state compact"><strong>详情加载失败</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function renderDetail(record) {
  // 标题不再带编号：数据库 id 已在下面的字段表里（record_id），列表也不再显示它
  $('#detail-title').textContent = '记录详情';
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
      <div class="detail-item"><div class="detail-key">${escapeHtml(mention.all ? '全体成员' : (mention.nickname || '未知成员'))}</div><div class="detail-value">${escapeHtml(mention.all ? '@全体' : `@${mention.nickname || '未知成员'}`)}</div></div>`).join('')}</div></section>` : '',
  ].filter(Boolean).join('');
  const kindBadges = (Array.isArray(record.content_kind) ? record.content_kind : [])
    .map((kind) => `<span class="badge kind">${escapeHtml(kind)}</span>`);
  const detailBadges = [
    ...kindBadges,
    ...subtypeBadges(record),
    record.recalled ? '<span class="badge recalled">已撤回</span>' : '',
  ].join('');
  // 软删除状态块：chip + 删除时间（详情页展示删除时间）；删除模式下附 times/backup 图标
  const deleteBlock = record.deleted
    ? `<div class="detail-deleted"><span class="chip-deleted">deleted</span><span class="detail-deleted-time">删除时间：${escapeHtml(record.deleted_at || '（未知）')}</span></div>`
    : '';
  $('#detail-content').innerHTML = `
    <div class="detail-message">${escapeHtml(record.content || '(空消息)')}</div>
    ${detailBadges ? `<div class="record-meta">${detailBadges}</div>` : ''}
    ${deleteBlock}
    ${relationBlocks}
    <section class="detail-section"><h3>字段</h3><div class="detail-grid">${fields.map(([key, value]) => `
      <div class="detail-item"><div class="detail-key">${escapeHtml(key)}</div><div class="detail-value">${escapeHtml(Array.isArray(value) ? value.join(', ') : value ?? 'null')}</div></div>`).join('')}</div></section>
    <section class="detail-section"><h3>原始 content_template</h3><pre class="detail-json">${escapeHtml(record.content_template || '')}</pre></section>
    <section class="detail-section"><h3>relation_data</h3><pre class="detail-json">${escapeHtml(relation)}</pre></section>`;
  // 删除模式：详情标题右侧出现 times/backup 图标
  const modeSlot = $('#detail-mode-slot');
  if (modeSlot) modeSlot.innerHTML = recordModeButton(record);
  attachDetailMedia($('#detail-content'));
  bindModeButtons($('#detail-content'));
  bindModeButtons($('#detail-title-row'));
  hydrateInlineThumbs($('#detail-content'), state.mediaObjectUrls, true);
}

async function loadPersonaOptions() {
  if (state.personaOptions.length) return;
  try {
    const data = unwrap(await apiGet('personas'));
    state.personaOptions = (data && data.personas) || [];
  } catch (error) {
    state.personaOptions = [];
  }
  const select = $('#filter-persona');
  const current = select.value;
  select.innerHTML = ['<option value="">请选择 persona</option>']
    .concat(state.personaOptions.map((item) => {
      const label = item.value ? `${item.value}（${formatNumber(item.count)} 条）` : '未标记 persona';
      // 空串在这里会被 `value || undefined` 吞掉，所以"未标记"用显式哨兵值
      const optionValue = item.value || PERSONA_EMPTY;
      return `<option value="${escapeHtml(optionValue)}">${escapeHtml(label)}</option>`;
    }))
    .join('');
  select.value = current;
}

function setAdvancedMode(enabled) {
  state.advancedMode = Boolean(enabled);
  $('#advanced-toggle').classList.toggle('active', state.advancedMode);
  $('#advanced-filters').classList.toggle('hidden', !state.advancedMode);
  if (state.advancedMode && state.deleteMode) setDeleteMode(false);
  if (state.advancedMode) {
    showToast('高级只读模式：先选 persona；「配对」可选 all 或仅看配对');
    loadPersonaOptions();
  }
}

async function toggleAdvancedMode() {
  setAdvancedMode(!state.advancedMode);
  await loadFacets();
  runQuery(1);
}

function setDeleteMode(enabled) {
  // 只读模式 ↔ 删除模式：进入后每条记录（列表与详情）出现单条软删除/恢复图标
  state.deleteMode = Boolean(enabled);
  if (state.deleteMode && state.advancedMode) setAdvancedMode(false);
  const pill = $('#mode-toggle');
  pill.classList.toggle('active', state.deleteMode);
  $('#mode-toggle-label').textContent = state.deleteMode ? '删除模式' : '只读模式';
  if (state.deleteMode) {
    showToast('删除模式：点每条记录后面的图标可软删除单条（backup 图标恢复）');
  }
  // 重绘当前列表（记录 + 工具调用两份）与（若打开着）详情，让图标立即出现/消失。
  // 工具列表必须一起重绘：图标是渲染期插值的，漏了就表现为"工具条目没有删除图标"。
  if (state.lastQuery) runQuery(state.page);
  if (state.lastToolQuery) runToolQuery(state.toolPage);
  const modal = $('#detail-modal');
  if (modal && !modal.classList.contains('hidden') && state.detailRecordId) {
    openDetail(state.detailRecordId);
  }
}

function toggleDeleteMode() {
  if (!state.deleteMode && state.about && state.about.write_enabled === false) {
    showToast('当前数据源不是插件实时数据库，删除/恢复已禁用', 'error');
    return;
  }
  setDeleteMode(!state.deleteMode);
}

function collectToolQuery(page = state.toolPage) {
  return {
    tool_name: $('#tool-filter-name').value.trim(),
    chat_type: $('#tool-filter-chat-type').value,
    session_id: $('#tool-filter-session').value.trim(),
    platform_id: $('#tool-filter-platform').value.trim(),
    persona_id: toolPersonaValue(),
    since: $('#tool-filter-since').value,
    until: $('#tool-filter-until').value,
    deleted_state: $('#tool-filter-deleted').value,
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

function toolDeletedBlock(record) {
  // 工具调用的软删除标记：chip + 删除时间（工具卡片没有独立详情页）
  if (!record || !record.deleted) return '';
  return `<div class="record-deleted-chip"><span class="chip-deleted">deleted</span><span class="detail-deleted-time">删除时间：${escapeHtml(record.deleted_at || '（未知）')}</span></div>`;
}

function applyToolDeletedState(toolId, deleted, deletedAt) {
  // 原地更新该工具卡片：chip/删除时间出现或消失 + 图标 times ↔ backup
  // 同时更新缓存的原始对象：详情抽屉直接复用它渲染，只改 DOM 会让详情里的
  // 删除状态停在旧值，与卡片上的 chip 自相矛盾。
  const cached = state.toolItems && state.toolItems.get(Number(toolId));
  if (cached) {
    cached.deleted = Boolean(deleted);
    cached.deleted_at = deleted ? (deletedAt || '') : '';
  }
  const card = document.querySelector(`article[data-tool-card-id="${toolId}"]`);
  if (!card) return;
  const head = card.querySelector('.tool-card-head');
  const existing = card.querySelector('.record-deleted-chip');
  if (deleted && !existing) {
    const block = document.createElement('div');
    block.className = 'record-deleted-chip';
    block.innerHTML = `<span class="chip-deleted">deleted</span><span class="detail-deleted-time">删除时间：${escapeHtml(deletedAt || '（未知）')}</span>`;
    card.insertBefore(block, head ? head.nextSibling : card.firstChild);
  } else if (!deleted && existing) {
    existing.remove();
  }
  const button = card.querySelector('[data-mode-action]');
  if (button) button.outerHTML = modeButtonHtml(toolId, deleted, 'tool');
  bindModeButtons(card);
}

// 工具记录的会话标识：从 umo 拆 平台:类型:会话号，只显示"群聊·群号"这类人话
// 只有"一轮调了多次"才值得标位置；单次调用什么都不显示。
// 轮次标识直接用 turn_id 的前 8 位（uuid 唯一、跨页稳定），不另做编号。
function toolTurnShort(record) {
  return String(record.turn_id || '').slice(0, 8);
}

function toolStepLabel(record) {
  const total = Number(record.turn_total) || 0;
  const index = Number(record.call_index) || 1;
  if (total <= 1) return '';
  const short = toolTurnShort(record);
  return short ? `轮 ${short} · 第 ${index} 次调用` : `第 ${index} 次调用`;
}

// 调用者头像：BOT 自己的 QQ（self_id）现查头像，拿不到退首字母
function toolCallerAvatar(record) {
  const who = String(record.self_id || '').trim();
  const label = String(record.platform_id || '').trim();
  if (!who && !label) return '';
  const initial = escapeHtml(label.slice(0, 1) || '?');
  if (!/^[0-9]{5,12}$/.test(who)) {
    return `<div class="speaker-avatar small" title="${escapeHtml(label)}"><span class="avatar-initial">${initial}</span></div>`;
  }
  const url = `https://q1.qlogo.cn/g?b=qq&nk=${who}&s=100`;
  return `<div class="speaker-avatar small" title="${escapeHtml(label)}">`
    + `<img src="${url}" alt="" loading="lazy" referrerpolicy="no-referrer"`
    + ` onerror="this.style.display='none';this.nextElementSibling.style.display='grid'">`
    + `<span class="avatar-initial" style="display:none">${initial}</span></div>`;
}

function toolSessionChip(record) {
  const parts = String(record.umo || '').split(':');
  const kind = parts.length >= 2 ? parts[1] : '';
  const session = parts.length >= 3 ? parts[2] : '';
  const label = CHAT_TYPE_LABELS[kind];
  if (!label || !session) return '';
  const text = `${label}·${session}`;
  const groupAttr = label === '群聊'
    ? ` data-group-id="${escapeHtml(session)}" data-label="${escapeHtml(text)}"`
    : '';
  return `<span class="badge"${groupAttr} title="会话">${escapeHtml(text)}</span>`;
}

async function loadToolPersonaOptions() {
  const select = $('#tool-filter-persona');
  if (select.dataset.loaded === '1') return;
  await loadPersonaOptions();
  const current = select.value;
  // 「未标记 persona」的 option value 是空串，与「all」同值：直接用 value 会让它被
  // 前端当成"没选"（后端归一成 all），选了等于没筛。给空值加哨兵，与记录页同一口径。
  select.innerHTML = ['<option value="">all</option>']
    .concat(state.personaOptions.map((item) => {
      const label = item.value ? `${item.value}（${formatNumber(item.count)} 条）` : '未标记 persona';
      const value = item.value || PERSONA_EMPTY;
      return `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
    }))
    .join('');
  select.value = current;
  select.dataset.loaded = '1';
}

function renderToolRecords(items) {
  // 详情按钮只靠 tool_id 找记录：当前页的原始对象存一份，避免再发一次请求
  state.toolItems = new Map(items.map((item) => [Number(item.tool_id), item]));
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
    return `<article class="tool-card" data-tool-card-id="${record.tool_id}">
      <div class="tool-card-head">
        <span class="badge tool-name">${escapeHtml(record.tool_name || '(empty)')}</span>
        <span class="tool-card-time">${escapeHtml(record.created_at || record.created_at_utc || '—')}</span>
        <span class="tool-card-mode">
          ${modeButtonHtml(record.tool_id, record.deleted, 'tool')}
          <button class="record-detail-button" data-tool-detail-id="${record.tool_id}">详情 ›</button>
        </span>
      </div>
      ${toolDeletedBlock(record)}
      <div class="record-meta tool-card-chips">
        ${toolCallerAvatar(record)}
        ${toolStepLabel(record) ? `<span class="badge step" title="这一轮共 ${record.turn_total} 次工具调用">${escapeHtml(toolStepLabel(record))}</span>` : ''}
        ${record.platform_id ? `<span class="badge tool-bot" title="发起调用的 BOT">${escapeHtml(record.platform_id)}</span>` : ''}
        ${record.persona_id ? `<span class="badge tool-persona" title="这一轮的人设">${escapeHtml(record.persona_id)}</span>` : ''}
        ${toolSessionChip(record)}
      </div>
      <div class="tool-block"><div class="tool-block-title">参数</div><pre class="tool-json">${escapeHtml(argsPreview)}</pre></div>
      <div class="tool-block"><div class="tool-block-title">返回</div><pre class="tool-json">${escapeHtml(record.tool_result || '(空)')}</pre></div>
    </article>`;
  }).join('');
  bindModeButtons($('#tools-list'));
  $('#tools-list').querySelectorAll('[data-tool-detail-id]').forEach((button) => {
    button.addEventListener('click', () => {
      const record = (state.toolItems || new Map()).get(Number(button.dataset.toolDetailId));
      if (record) openToolDetail(record);
    });
  });
  hydrateGroupNames($('#tools-list'));
}

// 工具调用详情：技术值（轮次/UMO/CID/记录 id）都在这里，卡片上不再出现
function openToolDetail(record) {
  const modal = $('#detail-modal');
  const stepLabel = toolStepLabel(record);
  const step = stepLabel || `第 ${Number(record.call_index) || 1} 次（本轮仅此一次）`;
  const fields = [
    ['工具', record.tool_name || '(empty)'],
    ['BOT', record.platform_id || '（未知）'],
    ['人设', record.persona_id || '（未标记）'],
    ['BOT 号码 self_id', record.self_id || '（未知）'],
    ['这一步', step],
    ['时间', record.created_at || record.created_at_utc || '—'],
    ['轮次 turn_id', record.turn_id || '（无）'],
    ['会话 UMO', record.umo],
    ['Conversation ID', record.conversation_id],
    ['记录 ID', String(record.tool_id)],
    ['删除状态', record.deleted ? `已删除（${record.deleted_at || '未知时间'}）` : '未删除'],
  ];
  $('#detail-title').textContent = '工具调用详情';
  const modeSlot = $('#detail-mode-slot');
  if (modeSlot) modeSlot.innerHTML = '';
  $('#detail-content').innerHTML = `
    <div class="record-meta tool-card-chips">
      <span class="badge tool-name">${escapeHtml(record.tool_name || '(empty)')}</span>
      <span class="badge step">${escapeHtml(step)}</span>
      ${toolSessionChip(record)}
    </div>
    ${toolDeletedBlock(record)}
    <section class="detail-section"><h3>参数</h3><pre class="detail-json">${escapeHtml(record.tool_args || '(空)')}</pre></section>
    <section class="detail-section"><h3>返回</h3><pre class="detail-json">${escapeHtml(record.tool_result || '(空)')}</pre></section>
    <section class="detail-section"><h3>字段</h3><div class="detail-grid">${fields
      .filter(([, value]) => value === null || typeof value !== 'object')
      .map(([key, value]) => `
      <div class="detail-item"><div class="detail-key">${escapeHtml(key)}</div><div class="detail-value">${escapeHtml(value)}</div></div>`).join('')}</div></section>`;
  modal.classList.remove('hidden');
  hydrateGroupNames($('#detail-content'));
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
  $('#tool-filter-persona').value = '';
  $('#tool-filter-page-size').value = '50';
  runToolQuery(1);
}

function closeDetail() {
  state.detailRequestToken += 1;  // 丢弃仍在途的详情响应
  state.detailRecordId = null;
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
  // 高级只读模式是"消息 + 轮次"的视角：只有记录查询页需要它，
  // 数据概览（统计口径）与工具调用页都没有这条查询路径 → 那两页不显示入口
  const advancedPill = $('#advanced-toggle');
  if (advancedPill) {
    advancedPill.classList.toggle('hidden', target !== 'query-section');
  }
  if (target === 'tools-section') {
    loadToolPersonaOptions().catch(() => {});
    if (!state.lastToolQuery) runToolQuery(1);
  }
}

function resetFilters() {
  $('#query-form').reset();
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

async function exportSnapshot() {
  // SQLite backup 一致快照：服务端后台生成，源库读写不阻塞，无需排队。
  const button = $('#export-button');
  button.disabled = true;
  const original = button.textContent;
  button.textContent = '导出中…';
  try {
    const info = unwrap(await apiPost('export', {}));
    if (bridge && typeof bridge.download === 'function') {
      await bridge.download(`export/${info.file_name}`, {}, info.file_name);
    } else {
      const response = await fetch(`/api/plug/${PLUGIN_NAME}/export/${info.file_name}`);
      if (!response.ok) throw new Error(`下载失败：HTTP ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = info.file_name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    }
    showToast(`已导出：${info.file_name}（${formatBytes(info.size_bytes)}）`);
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = original;
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
  ['#tool-filter-chat-type', '#tool-filter-persona', '#tool-filter-deleted',
    '#tool-filter-page-size'].forEach((selector) => {
    $(selector).addEventListener('change', () => runToolQuery(1));
  });
  $('#reset-tool-filters').addEventListener('click', resetToolFilters);
  $('#tool-page-prev').addEventListener('click', () => state.toolPage > 1 && runToolQuery(state.toolPage - 1));
  $('#tool-page-next').addEventListener('click', () => state.toolPage < state.toolPageCount && runToolQuery(state.toolPage + 1));
  $('#detail-close').addEventListener('click', closeDetail);
  $('#detail-modal').addEventListener('click', (event) => { if (event.target.id === 'detail-modal') closeDetail(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDetail(); });
  $('#theme-toggle').addEventListener('click', toggleTheme);
  $('#reload-button').addEventListener('click', initialize);
  $('#export-button').addEventListener('click', exportSnapshot);
  $('#mode-toggle').addEventListener('click', toggleDeleteMode);
  $('#advanced-toggle').addEventListener('click', () => {
    toggleAdvancedMode().catch((error) => showToast(error.message, 'error'));
  });
  $('#filter-persona').addEventListener('change', () => runQuery(1));
  $('#filter-persona-role').addEventListener('change', () => runQuery(1));
  $('#filter-persona-status').addEventListener('change', () => runQuery(1));
  $('#filter-paired-only').addEventListener('change', () => runQuery(1));
  $('#database-settings-form').addEventListener('submit', saveDatabaseSettings);
  $('#restore-database-path').addEventListener('click', restoreDefaultDatabasePath);
  FACET_SELECTORS.forEach((selector) => $(selector).addEventListener('change', async () => {
    // 下拉改动既要刷候选（计数随筛选变化），也要重查列表——只刷候选会让结果停在上一次
    try {
      await loadFacets();
      runQuery(1);
    } catch (error) {
      showToast(error.message, 'error');
    }
  }));
  $('#filter-page-size').addEventListener('change', () => runQuery(1));
}

async function lockSnapshot() {
  // 进页面/点刷新时锁定当前库状态：之后翻页、筛选都读同一份快照，
  // 不会因为群里在聊天而反复重建（重建一次约 0.6 秒）。
  try {
    return unwrap(await apiPost('snapshot', {}));
  } catch (error) {
    return null;      // 锁不上就退回按需重建，功能不受影响
  }
}

async function initialize() {
  try {
    if (bridge && typeof bridge.ready === 'function') await bridge.ready();
    const [aboutResponse, settingsResponse] = await Promise.all([
      apiGet('about'), apiGet('settings'),
    ]);
    state.about = unwrap(aboutResponse);
    if (state.about.write_enabled === false) {
      // 数据源不是插件实时库：删除模式不可用，明确写进 title 而不是让人点了才知道
      $('#mode-toggle').title = '当前数据源不是插件实时数据库，软删除/恢复已禁用';
    }
    renderSettings(unwrap(settingsResponse));
    $('#version-label').textContent = `${state.about.name} · v${state.about.version}`;
    $('#filter-page-size').value = String(state.about.default_page_size || 50);
    setSettingsMessage('路径设置保存在 AstrBot 插件存储中。');
    await lockSnapshot();
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
