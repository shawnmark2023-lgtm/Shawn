/* CCTR Atelier Pro Desktop Studio · Modular State & Event Delegation */
'use strict';

const Store = {
  bridge: null,
  view: 'home',
  step: 0,
  isLoggedIn: false,
  sessionAccount: null,
  practices: [],
  selectedIds: new Set(),
  pendingFilterPracticeId: '',
  exportFmt: 'pdf',
  exportVariant: 'withAnswersRed',
  kernels: { preferred: 'auto', labels: [], items: {} },
  savedAccounts: [],
  tasks: [],
  logs: [],
  historyFilter: 'all',
  historyQuery: '',
  logLevelFilter: 'all',
  activeTaskId: null,
  lastTasksSig: '',
  lastLogsSig: '',
  aiPresets: [],
  aiCliAgents: [],
  aiMode: 'web',
  aiEditMode: false,
  aiWorkspace: { title: '考点智能融合复习讲义', source_label: '', questionCount: 0, questions: [], notes: [], topics: [] },
  activeAiTopicIdx: 0,
  aiExportFiles: [],
};

const VIEW_META = {
  home: { title: '首页概览', sub: '工作台总览 · 题库抓取与 AI 考点融合' },
  workspace: { title: '题库与制版', sub: '多平台题库抓取 · A4 排版与格式互转' },
  plugins: { title: 'AI 考点工坊', sub: '课堂板书/讲义融合 · 知识点+接题重构' },
  history: { title: '成品归档库', sub: '全部导出文档管理 · 检索与一键直达 AI 工坊' },
  logs: { title: '运行诊断与日志', sub: '完整性校验 · 图片预内联与内核调度记录' },
};

const FMT_NAMES = {
  pdf: 'A4 PDF',
  zip: '分类 ZIP',
  img: 'PNG 图包',
  xlsx: 'Excel 刷题表',
  anki: 'Anki 记忆卡',
  md: 'Markdown 讲义',
  json: '结构化 JSON',
};

const STATUS_LABELS = { success: '完成', processing: '制版中', waiting: '队列中', failed: '失败' };

const qs = id => document.getElementById(id);

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}

function showToast(msg) {
  const t = qs('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}

function shortPathLabel(p) {
  if (!p) return 'Documents/CCTRExport';
  const parts = String(p).replace(/\\/g, '/').split('/').filter(Boolean);
  return parts.length >= 2 ? parts.slice(-2).join('/') : p;
}

function applyTheme(dark) {
  document.documentElement.classList.toggle('dark', !!dark);
  const btn = qs('themeBtn');
  if (btn) {
    btn.innerHTML = dark
      ? '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/></svg>'
      : '<svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  }
}

function toggleTheme() {
  const dark = !document.documentElement.classList.contains('dark');
  applyTheme(dark);
  if (Store.bridge) Store.bridge.setDarkMode(dark);
}

function switchView(view) {
  if (!VIEW_META[view]) view = 'home';
  Store.view = view;
  document.querySelectorAll('.nav-item[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  qs('topbarTitle').textContent = VIEW_META[view].title;
  qs('topbarSub').textContent = VIEW_META[view].sub;
}
window.switchView = switchView;

function toggleDrawer(show) {
  qs('drawerMask').classList.toggle('show', !!show);
  qs('activityDrawer').classList.toggle('show', !!show);
}

function openKernelModal() {
  renderKernelModalList();
  qs('kernelModal').classList.add('show');
}

function closeKernelModal() {
  qs('kernelModal').classList.remove('show');
}

/* ── Global Drag & Drop + Screenshot Paste ───────────────── */
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => {
  e.preventDefault();
  window._setDragOverlay(false);
});
window._setDragOverlay = function (active) {
  const c = qs('dragCurtain');
  if (c) c.classList.toggle('show', !!active);
  const dz = qs('aiDropZone');
  if (dz) dz.classList.toggle('drag-over', !!active);
};
window._resolveDropTarget = function (x, y) {
  const el = document.elementFromPoint(x, y);
  const zoneEl = el && el.closest ? el.closest('[data-drop-zone]') : null;
  if (zoneEl && zoneEl.dataset && zoneEl.dataset.dropZone) return zoneEl.dataset.dropZone;
  return Store.view === 'workspace' ? 'workspace' : 'auto';
};

document.addEventListener('paste', e => {
  if (Store.view !== 'plugins' || !Store.bridge || !Store.bridge.pasteAiClipboardImage) return;
  const items = (e.clipboardData && e.clipboardData.items) || [];
  const activeTag = ((document.activeElement && document.activeElement.tagName) || '').toUpperCase();
  const hasImg = Array.from(items).some(it => it.type && it.type.startsWith('image/'));
  if ((activeTag === 'TEXTAREA' || activeTag === 'INPUT') && !hasImg) return;
  for (const it of items) {
    if (it.type && it.type.startsWith('image/')) {
      const file = it.getAsFile();
      if (!file) continue;
      const reader = new FileReader();
      reader.onload = ev => {
        Store.bridge.pasteAiClipboardImage('剪贴板截图', String(ev.target.result || ''), resStr => {
          const r = JSON.parse(resStr);
          showToast(r.ok ? r.message : (r.error || '粘贴失败'));
        });
      };
      reader.readAsDataURL(file);
      e.preventDefault();
      break;
    }
  }
});

/* ── Workspace Navigation & Auth ────────────────────────── */
function tryGoStep(step) {
  if (step > 0 && !Store.isLoggedIn) return;
  if (step === 2 && Store.selectedIds.size === 0) return;
  goStep(step);
}

function goStep(step) {
  Store.step = step;
  qs('paneStep0').style.display = step === 0 ? 'flex' : 'none';
  qs('paneStep1').style.display = step === 1 ? 'flex' : 'none';
  qs('paneStep2').style.display = step === 2 ? 'flex' : 'none';
  [0, 1, 2].forEach(i => {
    const chip = qs('stepChip' + i);
    chip.classList.toggle('active', i === step);
    chip.classList.toggle('done', i < step || (i === 0 && Store.isLoggedIn && step > 0));
    if (i === 1) chip.disabled = !Store.isLoggedIn;
    if (i === 2) chip.disabled = !Store.isLoggedIn || Store.selectedIds.size === 0;
  });
  if (step === 2) {
    qs('studioScopeBadge').textContent = `已选 ${Store.selectedIds.size} 项练习`;
    loadFiltersIfSingle();
    updatePreviewCard();
  }
}

function selectSourceTab(src) {
  document.querySelectorAll('.source-tab').forEach(b => b.classList.toggle('active', b.dataset.src === src));
  qs('srcPaneCctr').style.display = src === 'cctr' ? 'block' : 'none';
  qs('srcPaneChaoxing').style.display = src === 'chaoxing' ? 'block' : 'none';
  qs('srcPaneYuketang').style.display = src === 'yuketang' ? 'block' : 'none';
  qs('srcPaneLocal').style.display = src === 'local' ? 'block' : 'none';
}

function refreshCaptcha() {
  if (!Store.bridge) return;
  qs('ocrStatusTag').textContent = '识别中…';
  qs('ocrStatusTag').className = 'ocr-note wait';
  qs('captchaPlaceholder').style.display = 'inline';
  qs('captchaImg').style.display = 'none';
  Store.bridge.fetchCaptcha();
}

function submitLogin() {
  if (!Store.bridge) return;
  const school = qs('schoolInput').value.trim();
  const user = qs('userInput').value.trim();
  const pass = qs('passInput').value;
  const captcha = qs('captchaInput').value.trim();
  const err = qs('loginErr');
  err.classList.remove('show');
  if (!school || !user || !pass) {
    err.textContent = '请填写学校代码、学号与密码';
    err.classList.add('show');
    return;
  }
  const btn = qs('loginBtn');
  btn.disabled = true;
  btn.textContent = '正在校验会话并拉取目录…';
  Store.sessionAccount = { school, username: user, password: pass };
  Store.bridge.login(school, user, pass, captcha, true);
}

function signOut() {
  if (Store.bridge) Store.bridge.resetSession();
  Store.isLoggedIn = false;
  Store.sessionAccount = null;
  Store.practices = [];
  Store.selectedIds.clear();
  Store.activeTaskId = null;
  qs('resultBanner').classList.remove('show', 'ok', 'err');
  qs('sessionPill').classList.remove('show');
  qs('switchAccBtn').style.display = 'none';
  qs('passInput').value = '';
  qs('captchaInput').value = '';
  goStep(0);
  refreshCaptcha();
  showToast('已安全断开当前账号');
}

function renderSavedAccounts() {
  const renderHtml = (!Store.savedAccounts || !Store.savedAccounts.length)
    ? '<div class="quick-empty">成功登录后自动记住学校代码与学号，下次点击即可填入。</div>'
    : '<div class="acc-list">' + Store.savedAccounts.map((acc, i) => `
      <div class="acc-chip">
        <button type="button" class="acc-fill" data-action="fill-account" data-idx="${i}">
          <span class="acc-school">${escapeHtml(acc.school)}</span>
          <span class="acc-user">${escapeHtml(acc.username)}</span>
        </button>
        <button type="button" class="acc-del" title="移除记忆账号" data-action="remove-account" data-idx="${i}">×</button>
      </div>
    `).join('') + '</div>';
  ['savedAccountsBox', 'homeSavedAccounts'].forEach(id => {
    const el = qs(id);
    if (el) el.innerHTML = renderHtml;
  });
}

function renderQuickExports() {
  const ready = Store.tasks.filter(t => t.status === 'success' && t.file_path).slice(0, 5);
  const html = !ready.length
    ? '<div class="quick-empty">暂无最近导出成稿。</div>'
    : '<div class="quick-list">' + ready.map(t => `
      <div class="quick-row">
        <div class="quick-main">
          <div class="quick-name" title="${escapeHtml(t.file_name || t.title)}">${escapeHtml(t.file_name || t.title)}</div>
          <div class="quick-meta">${escapeHtml((t.create_time || '').slice(5, 16))}${t.file_size ? ' · ' + escapeHtml(t.file_size) : ''}</div>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0;">
          <button type="button" class="btn btn-carbon" style="height:27px;padding:0 10px;font-size:11.5px;" data-action="open-task-file" data-tid="${t.id}">打开</button>
          <button type="button" class="btn btn-outline" style="height:27px;padding:0 9px;font-size:11.5px;" data-action="open-task-dir" data-tid="${t.id}">目录</button>
          ${t.has_snapshot ? `<button type="button" class="btn btn-outline" style="height:27px;padding:0 9px;font-size:11.5px;" data-action="send-task-ai" data-tid="${t.id}">AI 融合</button>` : ''}
        </div>
      </div>
    `).join('') + '</div>';
  ['quickExportsBox', 'homeRecentExports'].forEach(id => {
    const el = qs(id);
    if (el) el.innerHTML = html;
  });
}

function renderPracticeGrid() {
  const kw = (qs('courseSearch').value || '').trim().toLowerCase();
  const list = Store.practices.filter(p => !kw ||
    (p.lesson_name || '').toLowerCase().includes(kw) ||
    (p.practice_name || '').toLowerCase().includes(kw) ||
    (p.teacher_name || '').toLowerCase().includes(kw)
  );
  const grid = qs('practiceGrid');
  if (!list.length) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1;">没有匹配的课程练习</div>';
  } else {
    grid.innerHTML = list.map(p => {
      const sel = Store.selectedIds.has(String(p.id));
      const cnt = p.question_count != null ? `${p.question_count} 题` : '待核';
      return `
        <div class="course-card ${sel ? 'selected' : ''}" data-action="toggle-practice" data-pid="${escapeHtml(p.id)}">
          <div class="sq-check"><svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg></div>
          <div class="course-body">
            <div class="course-top">
              <div class="course-title">${escapeHtml(p.lesson_name || p.practice_name)}</div>
              <span class="q-tag">${cnt}</span>
            </div>
            <div class="course-sub">${escapeHtml(p.practice_name)}</div>
            ${p.teacher_name ? `<div class="course-foot">教师 · ${escapeHtml(p.teacher_name)}</div>` : ''}
          </div>
        </div>
      `;
    }).join('');
  }
  updateSelectedSummary();
}

function updateSelectedSummary() {
  const chosen = Store.practices.filter(p => Store.selectedIds.has(String(p.id)));
  const totalQ = chosen.reduce((acc, p) => acc + (Number(p.question_count) || 0), 0);
  qs('selectedSummary').textContent = chosen.length ? `已选 ${chosen.length} 项练习 · 约 ${totalQ} 道题` : '请选择至少一项课程练习';
  qs('toStep2Btn').disabled = chosen.length === 0;
  qs('stepChip2').disabled = chosen.length === 0;
}

function selectAllPractices(flag) {
  const kw = (qs('courseSearch').value || '').trim().toLowerCase();
  Store.practices.filter(p => !kw ||
    (p.lesson_name || '').toLowerCase().includes(kw) ||
    (p.practice_name || '').toLowerCase().includes(kw) ||
    (p.teacher_name || '').toLowerCase().includes(kw)
  ).forEach(p => {
    if (flag) Store.selectedIds.add(String(p.id));
    else Store.selectedIds.delete(String(p.id));
  });
  renderPracticeGrid();
}

function setFormat(fmt) {
  Store.exportFmt = fmt;
  document.querySelectorAll('.brick[data-fmt]').forEach(c => c.classList.toggle('active', c.dataset.fmt === fmt));
}

function setVariant(v) {
  Store.exportVariant = v;
  document.querySelectorAll('.seg-btn[data-var]').forEach(p => p.classList.toggle('active', p.dataset.var === v));
  updatePreviewCard();
}

function updatePreviewCard() {
  const isRed = Store.exportVariant === 'withAnswersRed';
  qs('prevOptB').classList.toggle('red', isRed);
  qs('prevAnsTag').classList.toggle('red', isRed);
  qs('prevAnswerBlock').style.display = Store.exportVariant === 'withoutAnswers' ? 'none' : 'block';
  const on = qs('smartPagination') ? qs('smartPagination').checked : true;
  qs('studioPagSpec').textContent = on ? '已开启 · 小题整题不跨页' : '已关闭 · 紧凑连续分页';
}

function loadFiltersIfSingle() {
  const wrap = qs('singleFiltersWrap');
  const cSel = qs('chapterSel');
  const qSel = qs('qtypeSel');
  cSel.innerHTML = '<option value="">全部章节</option>';
  qSel.innerHTML = '<option value="">全部题型</option>';
  Store.pendingFilterPracticeId = '';
  if (Store.selectedIds.size !== 1 || !Store.bridge) {
    wrap.style.display = 'none';
    return;
  }
  const pid = Array.from(Store.selectedIds)[0];
  const item = Store.practices.find(p => String(p.id) === pid);
  if (!item) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  Store.pendingFilterPracticeId = String(item.id);
  Store.bridge.loadFilters(item.json);
}

function submitExport() {
  if (!Store.bridge) return;
  const chosen = Store.practices.filter(p => Store.selectedIds.has(String(p.id)));
  if (!chosen.length) return showToast('请先选择练习');
  const chVal = qs('chapterSel').value;
  const qtVal = qs('qtypeSel').value;
  Store.bridge.submitExport(JSON.stringify({
    format: Store.exportFmt,
    variant: Store.exportVariant,
    practicedOnly: qs('practicedOnly').checked,
    smartPagination: qs('smartPagination').checked,
    practices: chosen.map(p => p.json),
    chapters: chVal ? [JSON.parse(chVal)] : [],
    questionTypes: qtVal ? [JSON.parse(qtVal)] : [],
  }), resStr => {
    const res = JSON.parse(resStr);
    if (!res.ok) return showToast(res.error || '提交失败');
    Store.activeTaskId = res.taskId;
    renderResultBanner();
    showToast(`任务 #${res.taskId} 已开始处理`);
  });
}

function renderResultBanner() {
  const banner = qs('resultBanner');
  if (!Store.activeTaskId) { banner.classList.remove('show'); return; }
  const t = Store.tasks.find(x => x.id === Store.activeTaskId);
  if (!t) return;
  banner.classList.add('show');
  banner.classList.remove('ok', 'err');
  if (t.status === 'success') {
    banner.classList.add('ok');
    qs('resultTitle').textContent = `完成导出：${t.file_name || t.title}`;
    qs('resultSub').textContent = `${t.file_size ? t.file_size + ' · ' : ''}当前仍保持已选择状态，可直接切换载体或版式继续生成。`;
    qs('resultActions').innerHTML = `
      <button class="btn btn-carbon" data-action="open-task-file" data-tid="${t.id}">打开成稿</button>
      <button class="btn btn-outline" data-action="open-task-dir" data-tid="${t.id}">定位目录</button>
      ${t.has_snapshot ? `<button class="btn btn-outline" data-action="send-task-ai" data-tid="${t.id}">AI 考点融合 →</button>` : ''}
    `;
  } else if (t.status === 'failed') {
    banner.classList.add('err');
    qs('resultTitle').textContent = `任务 #${t.id} 导出失败`;
    qs('resultSub').textContent = '请查看运行诊断日志排查详细报错，或切换渲染内核后重试。';
    qs('resultActions').innerHTML = '<button class="btn btn-outline" data-view-jump="logs">查看诊断日志</button>';
  } else {
    qs('resultTitle').textContent = `正在后台制版：${t.title}`;
    qs('resultSub').textContent = '正在核对题目总数、预内联图片并执行版面校验…';
    qs('resultActions').innerHTML = '<button class="btn btn-outline" data-action="open-drawer">查看进度</button>';
  }
}

/* ── Archive & Logs UI ──────────────────────────────────── */
function renderTasksUI() {
  const busy = Store.tasks.filter(t => t.status === 'processing' || t.status === 'waiting').length;
  const okCount = Store.tasks.filter(t => t.status === 'success').length;
  const badge = qs('navTaskBadge');
  badge.style.display = Store.tasks.length ? 'inline-block' : 'none';
  badge.textContent = String(Store.tasks.length);
  if (qs('homeTaskCount')) qs('homeTaskCount').textContent = String(okCount);

  const pill = qs('activityPill');
  pill.classList.toggle('busy', busy > 0);
  if (busy > 0) {
    qs('activityDot').className = 'pulse';
    qs('activityLabel').textContent = `制版中 (${busy})`;
  } else if (Store.tasks.length) {
    qs('activityDot').className = 'dot';
    qs('activityDot').style.color = Store.tasks[0].status === 'success' ? 'var(--jade)' : 'var(--vermilion)';
    qs('activityLabel').textContent = Store.tasks[0].file_name || Store.tasks[0].title;
  } else {
    qs('activityDot').className = 'dot';
    qs('activityDot').style.color = 'var(--ink-3)';
    qs('activityLabel').textContent = '空闲';
  }

  renderQuickExports();
  refreshAiTaskPicker();
  renderHistoryFiltered();
  renderDrawerTasks();
}

function renderHistoryFiltered() {
  const kw = Store.historyQuery.trim().toLowerCase();
  const fmt = Store.historyFilter;
  const filtered = Store.tasks.filter(t => {
    if (fmt !== 'all' && t.export_type !== fmt) return false;
    if (!kw) return true;
    const hay = `${t.title} ${t.file_name} ${t.file_path} ${t.export_type}`.toLowerCase();
    return hay.includes(kw);
  });

  qs('historySummary').textContent = Store.tasks.length
    ? `显示 ${filtered.length} 条 / 共 ${Store.tasks.length} 条归档记录`
    : '暂无归档记录';

  const histBox = qs('historyList');
  if (!filtered.length) {
    histBox.innerHTML = Store.tasks.length
      ? '<div class="empty-state">没有匹配的归档记录，请尝试切换筛选条件。</div>'
      : '<div class="empty-state">尚无导出成稿记录，在「题库与制版」提交导出后将在此归档。</div>';
    return;
  }

  histBox.innerHTML = filtered.map(t => `
    <div class="ledger-row">
      <div style="min-width:0;flex:1;">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span class="ledger-title">${escapeHtml(t.file_name || t.title)}</span>
          <span class="status-tag ${t.status}">${STATUS_LABELS[t.status] || t.status}</span>
          <span class="mono" style="font-size:11px;color:var(--ink-3);">${FMT_NAMES[t.export_type] || t.export_type}</span>
        </div>
        <div class="ledger-meta">
          ${escapeHtml(t.create_time)}${t.file_size ? ' · ' + escapeHtml(t.file_size) : ''}${t.file_path ? ' · ' + escapeHtml(t.file_path) : ''}
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;">
        ${t.status === 'success' ? `
          <button class="btn btn-carbon" style="height:31px;padding:0 12px;font-size:12px;" data-action="open-task-file" data-tid="${t.id}">打开成稿</button>
          <button class="btn btn-outline" style="height:31px;padding:0 11px;font-size:12px;" data-action="open-task-dir" data-tid="${t.id}">定位目录</button>
          ${t.has_snapshot ? `<button class="btn btn-outline" style="height:31px;padding:0 11px;font-size:12px;" data-action="send-task-ai" data-tid="${t.id}">AI 考点融合</button>` : ''}
        ` : ''}
        ${t.status === 'failed' ? `<button class="btn btn-outline" style="height:31px;padding:0 12px;font-size:12px;" data-action="retry-task" data-tid="${t.id}">重试</button>` : ''}
        <button class="btn btn-danger-text" data-action="delete-task" data-tid="${t.id}">移除</button>
      </div>
    </div>
  `).join('');
}

function renderDrawerTasks() {
  const dBox = qs('drawerTasks');
  dBox.innerHTML = Store.tasks.slice(0, 6).map(t => `
    <div style="padding:10px 12px;border:1px solid var(--line);border-radius:6px;margin-bottom:8px;background:var(--sheet-soft);">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">
        <span style="font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(t.title)}</span>
        <span class="status-tag ${t.status}">${STATUS_LABELS[t.status] || t.status}</span>
      </div>
      <div class="mono" style="font-size:10.5px;color:var(--ink-3);margin-top:4px;">${escapeHtml(t.create_time)}${t.file_size ? ' · ' + escapeHtml(t.file_size) : ''}</div>
    </div>
  `).join('') || '<div style="font-size:12px;color:var(--ink-3);">暂无最近任务</div>';
}

function renderLogsUI() {
  const lvl = Store.logLevelFilter;
  const fullList = lvl === 'all' ? Store.logs : Store.logs.filter(l => l.level === lvl);
  const toHtml = list => list.map(l => {
    const ts = (l.create_time || '').slice(11, 19);
    return `<div class="log-line ${escapeHtml(l.level)}"><span class="log-ts">${escapeHtml(ts)}</span><span class="log-id">#${l.task_id}</span><span class="log-msg">${escapeHtml(l.message)}</span></div>`;
  }).join('') || '<div style="color:var(--ink-3);">暂无匹配日志</div>';

  [
    ['fullLogBox', fullList],
    ['drawerLogBox', Store.logs.slice(-40)],
  ].forEach(([boxId, list]) => {
    const el = qs(boxId);
    if (!el) return;
    const prevTop = el.scrollTop;
    const wasAtBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;
    el.innerHTML = toHtml(list);
    el.scrollTop = wasAtBottom ? el.scrollHeight : prevTop;
  });
}

/* ── Kernel & Folder Helpers ────────────────────────────── */
function applyExportDirUI(dir) {
  qs('exportDirLabel').textContent = shortPathLabel(dir);
  qs('studioOutDir').textContent = dir || 'Documents/CCTRExport';
  qs('studioOutDir').title = dir || '';
}

function applyKernelState(k) {
  Store.kernels = k || Store.kernels;
  const pref = Store.kernels.preferred || 'auto';
  const item = (Store.kernels.items || {})[pref] || (Store.kernels.items || {}).auto || {};
  qs('kernelDockLabel').textContent = item.name ? `${item.name}${item.version ? ' ' + item.version.split('.')[0] : ''}` : '自动优选';
  qs('kernelDockDot').style.color = item.installed ? 'var(--jade)' : 'var(--amber)';
  renderKernelModalList();
}

function renderKernelModalList() {
  const list = qs('kernelList');
  if (!list) return;
  const labels = Store.kernels.labels || [];
  const items = Store.kernels.items || {};
  const pref = Store.kernels.preferred || 'auto';
  list.innerHTML = labels.map(l => {
    const info = items[l.key] || {};
    const statusText = info.installed
      ? `<span class="mono" style="color:var(--jade);font-weight:600;">就绪${info.version ? ' · ' + escapeHtml(info.version) : ''}</span>`
      : `<span class="mono" style="color:var(--amber);font-weight:600;">未安装${info.can_download ? '（可下载）' : ''}</span>`;
    return `
      <div class="kernel-item ${pref === l.key ? 'active' : ''}" data-action="select-kernel" data-kind="${escapeHtml(l.key)}">
        <div>
          <div style="font-weight:600;font-size:12.5px;">${escapeHtml(l.label)}</div>
          <div class="mono" style="font-size:11px;color:var(--ink-3);margin-top:2px;">${escapeHtml(info.path || info.source || '')}</div>
        </div>
        <div style="font-size:11.5px;white-space:nowrap;">${statusText}</div>
      </div>
    `;
  }).join('');
}

/* ── AI Knowledge Studio ────────────────────────────────── */
function initAiPluginUI(state) {
  Store.aiPresets = state.aiPresets || [];
  Store.aiCliAgents = state.aiCliAgents || [];
  const sel = qs('aiPresetSel');
  if (sel && Store.aiPresets.length) {
    sel.innerHTML = Store.aiPresets.map(p => `<option value="${escapeHtml(p.key)}">${escapeHtml(p.label)}</option>`).join('');
  }
  const cfg = state.aiConfig || {};
  if (cfg.preset && sel) sel.value = cfg.preset;
  if (qs('aiBaseUrl')) qs('aiBaseUrl').value = cfg.base_url || 'https://dashscope.aliyuncs.com/compatible-mode/v1';
  if (qs('aiApiKey')) qs('aiApiKey').value = cfg.api_key || '';
  if (qs('aiModelName')) qs('aiModelName').value = cfg.model || 'qwen-vl-max-latest';
  if (qs('aiBatchSize')) qs('aiBatchSize').value = cfg.batch_size || 24;
  if (qs('aiCustomPrompt')) qs('aiCustomPrompt').value = cfg.custom_prompt || '';
  if (qs('aiCliCurrentLabel')) {
    qs('aiCliCurrentLabel').textContent = cfg.provider === 'ollama'
      ? `Ollama: ${cfg.model || 'qwen2.5vl:7b'}`
      : (cfg.provider === 'cli' ? `CLI: ${cfg.base_url || 'codebuddy'}` : '待选择本机 CLI 或 Ollama');
  }
  renderCliAgentPills();
  if (state.aiWorkspace) {
    Store.aiWorkspace = state.aiWorkspace;
    renderAiWorkspace();
  }
}

function collectAiProfile() {
  const key = (qs('aiPresetSel') && qs('aiPresetSel').value) || 'qwen_api';
  const preset = Store.aiPresets.find(p => p.key === key) || {};
  return {
    preset: key,
    provider: preset.provider || (key === 'ollama' ? 'ollama' : (key === 'workbuddy_cli' ? 'cli' : 'openai')),
    base_url: qs('aiBaseUrl').value.trim(),
    api_key: qs('aiApiKey').value.trim(),
    model: qs('aiModelName').value.trim(),
    batch_size: Number(qs('aiBatchSize').value) || 24,
    custom_prompt: qs('aiCustomPrompt').value.trim(),
  };
}

function saveAiConfigDraft() {
  if (Store.bridge && Store.bridge.saveAiPluginConfig) {
    Store.bridge.saveAiPluginConfig(JSON.stringify(collectAiProfile()));
  }
}

function switchAiMode(mode) {
  Store.aiMode = mode;
  document.querySelectorAll('.ai-mode-card').forEach(el => el.classList.toggle('active', el.dataset.mode === mode));
  qs('aiModePaneWeb').style.display = mode === 'web' ? 'block' : 'none';
  qs('aiModePaneOffline').style.display = mode === 'offline' ? 'block' : 'none';
  qs('aiModePaneCli').style.display = mode === 'cli' ? 'block' : 'none';
  qs('aiModePaneApi').style.display = mode === 'api' ? 'block' : 'none';
}

function renderCliAgentPills() {
  const box = qs('aiCliDetectedList');
  if (!box) return;
  if (!Store.aiCliAgents.length) {
    box.innerHTML = '<span style="font-size:11.5px;color:var(--ink-3);">未检测到本机 CLI（可点击右侧探测 Ollama 或使用「免 API 千问/WorkBuddy」）</span>';
    return;
  }
  box.innerHTML = Store.aiCliAgents.map((a, idx) =>
    `<button type="button" class="ollama-pill" data-action="select-cli-agent" data-idx="${idx}">${escapeHtml(a.label)}</button>`
  ).join('');
}

function selectOllamaModel(modelName) {
  if (qs('aiPresetSel')) qs('aiPresetSel').value = 'ollama';
  const curBase = qs('aiBaseUrl').value.trim();
  if (!curBase || !curBase.includes('11434')) {
    qs('aiBaseUrl').value = 'http://127.0.0.1:11434';
  }
  qs('aiModelName').value = modelName;
  qs('aiCliCurrentLabel').textContent = `Ollama: ${modelName}`;
  saveAiConfigDraft();
  showToast(`已切换为本机 Ollama 模型：${modelName}`);
}

function refreshAiTaskPicker() {
  const sel = qs('aiTaskPickSel');
  if (!sel) return;
  const curVal = sel.value;
  const snaps = Store.tasks.filter(t => t.status === 'success' && t.has_snapshot);
  sel.innerHTML = '<option value="">切换历史导出任务…</option>' + snaps.map(t =>
    `<option value="${t.id}">#${t.id} ${escapeHtml(t.title)} (${t.question_count || '?'}题)</option>`
  ).join('');
  if (curVal) sel.value = curVal;
}

function sendTaskToAiPlugin(taskId) {
  if (!Store.bridge || !Store.bridge.loadAiFromTask) return;
  Store.bridge.loadAiFromTask(taskId, resStr => {
    const res = JSON.parse(resStr);
    if (!res.ok) return showToast(res.error || '加载失败');
    switchView('plugins');
    showToast(`已载入「${res.title}」共 ${res.count} 题到 AI 考点工坊`);
  });
}

function startAiFusion(mode) {
  if (!Store.bridge || !Store.bridge.runAiFusion) return;
  if (!Store.aiWorkspace.questionCount) return showToast('请先载入题库数据源');
  if (qs('aiRunBtn')) qs('aiRunBtn').disabled = true;
  if (qs('aiOfflineBtn')) qs('aiOfflineBtn').disabled = true;
  if (qs('aiCliRunBtn')) qs('aiCliRunBtn').disabled = true;
  qs('aiProgTrack').classList.add('show');
  qs('aiProgFill').style.width = '8%';
  qs('aiProgMsg').textContent = mode === 'offline' ? '正在执行离线章节/笔记关键词归类…' : '正在提交 AI 智能考点分类任务…';
  Store.bridge.runAiFusion(JSON.stringify({
    mode,
    title: qs('aiWorkspaceTitle').value.trim() || '考点智能融合',
    profile: collectAiProfile(),
  }));
}

function renderAiWorkspace() {
  const ws = Store.aiWorkspace;
  const tCount = (ws.topics || []).length;
  qs('aiQCountBadge').textContent = `${ws.questionCount || 0} 题`;
  qs('aiNoteCountBadge').textContent = `${(ws.notes || []).length} 份资料`;
  if (qs('homeAiTopicStat')) qs('homeAiTopicStat').textContent = `${tCount} 考点`;
  qs('aiWorkspaceTitle').value = ws.title || '考点智能融合复习讲义';
  qs('aiQSourceInfo').textContent = ws.questionCount
    ? `✓ 当前题库：《${ws.title}》（${ws.questionCount} 道完整题目 · 来源：${ws.source_label || '本地'}）`
    : '尚未加载题库：可直接拖入题库文件，或点击上方「一键载入最近导出题库」。';

  const nBox = qs('aiNotesList');
  nBox.innerHTML = (ws.notes || []).map((n, idx) => `
    <div class="ai-note-item">
      ${n.image_data_uri
        ? `<img class="ai-note-thumb" src="${escapeHtml(n.image_data_uri)}" alt="${escapeHtml(n.title)}">`
        : `<div class="ai-note-thumb">${escapeHtml((n.source_type || 'TXT').toUpperCase())}</div>`}
      <div style="min-width:0;flex:1;">
        <div style="font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(n.title)}</div>
        <div style="font-size:11px;color:var(--ink-2);margin-top:2px;">
          ${n.ocr_summary ? `<span style="color:var(--jade);font-weight:600;">✓ 已提炼 · ${escapeHtml(n.suggested_topic || '考点')}</span>` : escapeHtml((n.raw_preview || '图片板书').slice(0, 40))}
        </div>
        <div style="display:flex;gap:8px;margin-top:4px;">
          <button type="button" class="desk-link" style="font-size:11px;" data-action="analyze-note" data-idx="${idx}">识别提炼</button>
          <button type="button" class="desk-link" style="font-size:11px;color:var(--vermilion);" data-action="remove-note" data-idx="${idx}">移除</button>
        </div>
      </div>
    </div>
  `).join('');

  const topics = ws.topics || [];
  qs('aiEmptyTree').style.display = topics.length ? 'none' : 'block';
  qs('aiTreeGrid').style.display = topics.length ? 'grid' : 'none';
  if (!topics.length) return;
  if (Store.activeAiTopicIdx >= topics.length) Store.activeAiTopicIdx = 0;

  renderAiTopicListOnly();
  renderTopicInspector();
}

function renderAiTopicListOnly() {
  const topics = Store.aiWorkspace.topics || [];
  qs('aiTopicList').innerHTML = topics.map((tp, idx) => `
    <div class="ai-topic-node ${idx === Store.activeAiTopicIdx ? 'active' : ''}" data-action="select-topic" data-idx="${idx}">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
        <span style="font-weight:600;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${idx + 1}. ${escapeHtml(tp.name)}</span>
        <span class="imp-badge ${escapeHtml(tp.importance)}">${escapeHtml(tp.importance)}</span>
      </div>
      <div class="mono" style="font-size:11px;color:var(--ink-3);margin-top:4px;">${(tp.question_ids || []).length} 道配套真题</div>
    </div>
  `).join('');
}

function renderTopicInspector() {
  const box = qs('aiTopicInspector');
  const topics = Store.aiWorkspace.topics || [];
  const tp = topics[Store.activeAiTopicIdx];
  if (!tp) { box.innerHTML = ''; return; }
  const qById = Object.fromEntries((Store.aiWorkspace.questions || []).map(q => [q.id, q]));
  const topicOpts = topics.map((t, i) => `<option value="${i}" ${i === Store.activeAiTopicIdx ? 'selected' : ''}>${escapeHtml(t.name)}</option>`).join('');

  const qsHtml = (tp.question_ids || []).map((qid, i) => {
    const q = qById[qid];
    if (!q) return '';
    return `
      <div class="ai-q-row">
        <div style="min-width:0;flex:1;">
          <div style="font-size:11.5px;color:var(--ink-3);margin-bottom:2px;">#${i + 1} · ${escapeHtml(q.question_type)} · ${escapeHtml(q.id)}</div>
          <div style="font-size:12.5px;">${escapeHtml(q.stem_preview)}</div>
          ${q.answer_preview ? `<div style="font-size:11.5px;color:var(--jade);margin-top:3px;">答：${escapeHtml(q.answer_preview)}</div>` : ''}
        </div>
        <select class="select" data-move-qid="${escapeHtml(qid)}" style="width:130px;height:28px;font-size:11px;padding:0 6px;flex-shrink:0;" title="移动到其他考点">
          ${topicOpts}
        </select>
      </div>
    `;
  }).join('') || '<div style="font-size:12px;color:var(--ink-3);padding:8px 0;">该考点下暂无题目，可从其他考点调入。</div>';

  const summaryBlock = Store.aiEditMode
    ? `<label class="sec-label" style="margin-top:4px;">融合笔记精要</label>
       <textarea class="ai-textarea" data-topic-field="summary_md" rows="4">${escapeHtml(tp.summary_md)}</textarea>
       <label class="sec-label" style="margin-top:8px;">核心公式与结论（每行一条）</label>
       <textarea class="ai-textarea" data-topic-field="formulas" rows="2">${escapeHtml((tp.formulas || []).join('\n'))}</textarea>`
    : `<div class="handout-box">${escapeHtml(tp.summary_md || '暂无笔记精要，点击右上方「✎ 编辑精要/公式」可随时补充。')}${(tp.formulas || []).length ? `<div style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--line);color:var(--jade);font-weight:600;">核心公式：${(tp.formulas || []).map(escapeHtml).join('  ·  ')}</div>` : ''}</div>`;

  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;">
      <div style="display:flex;gap:8px;align-items:center;flex:1;min-width:200px;">
        <input class="input" data-topic-field="name" value="${escapeHtml(tp.name)}" style="font-weight:700;height:34px;">
        <select class="select" data-topic-field="importance" style="width:110px;height:34px;font-size:12px;">
          <option value="核心高频" ${tp.importance === '核心高频' ? 'selected' : ''}>核心高频</option>
          <option value="重要掌握" ${tp.importance === '重要掌握' ? 'selected' : ''}>重要掌握</option>
          <option value="基础了解" ${tp.importance === '基础了解' ? 'selected' : ''}>基础了解</option>
        </select>
      </div>
      <button type="button" class="btn btn-outline" style="height:30px;padding:0 10px;font-size:11.5px;" data-action="toggle-topic-edit">${Store.aiEditMode ? '完成编辑 ✓' : '✎ 编辑精要/公式'}</button>
    </div>
    ${summaryBlock}
    <label class="sec-label" style="margin-top:10px;">紧接配套真题（${(tp.question_ids || []).length} 题 · 支持右上角下拉调换考点）</label>
    <div>${qsHtml}</div>
  `;
}

function syncTopicsToPython() {
  if (Store.bridge && Store.bridge.syncAiTopics) {
    Store.bridge.syncAiTopics(JSON.stringify({
      title: qs('aiWorkspaceTitle') ? qs('aiWorkspaceTitle').value.trim() : Store.aiWorkspace.title,
      topics: Store.aiWorkspace.topics || [],
    }));
  }
}

/* ── Global Delegated Click / Change Listeners ─────────── */
function bindGlobalUIEvents() {
  document.addEventListener('click', e => {
    const viewBtn = e.target.closest('[data-view]');
    if (viewBtn) {
      switchView(viewBtn.dataset.view);
      return;
    }
    const jumpBtn = e.target.closest('[data-view-jump]');
    if (jumpBtn) {
      switchView(jumpBtn.dataset.viewJump);
      if (jumpBtn.dataset.stepJump !== undefined) goStep(Number(jumpBtn.dataset.stepJump));
      return;
    }
    const stepJump = e.target.closest('[data-step-jump]');
    if (stepJump) {
      goStep(Number(stepJump.dataset.stepJump));
      return;
    }
    const tryStep = e.target.closest('[data-try-step]');
    if (tryStep) {
      tryGoStep(Number(tryStep.dataset.tryStep));
      return;
    }
    const srcBtn = e.target.closest('.source-tab[data-src]');
    if (srcBtn) {
      selectSourceTab(srcBtn.dataset.src);
      return;
    }
    const platBtn = e.target.closest('[data-open-platform]');
    if (platBtn && Store.bridge) {
      Store.bridge.openPlatformBrowser(platBtn.dataset.openPlatform || 'chaoxing');
      return;
    }
    const fmtBtn = e.target.closest('.brick[data-fmt]');
    if (fmtBtn) {
      setFormat(fmtBtn.dataset.fmt);
      return;
    }
    const varBtn = e.target.closest('.seg-btn[data-var]');
    if (varBtn) {
      setVariant(varBtn.dataset.var);
      return;
    }
    const modeBtn = e.target.closest('.ai-mode-card[data-mode]');
    if (modeBtn) {
      switchAiMode(modeBtn.dataset.mode);
      return;
    }
    const wbBtn = e.target.closest('[data-workbench]');
    if (wbBtn) {
      if (!Store.aiWorkspace.questionCount) return showToast('请先在第 ① 步导入或载入题库');
      Store.bridge && Store.bridge.openAiWebWorkbench(wbBtn.dataset.workbench || 'qianwen');
      showToast('已生成整理指令并打开内置助手窗口');
      return;
    }
    const hFmtBtn = e.target.closest('#historyFilterPills .f-pill[data-hfmt]');
    if (hFmtBtn) {
      Store.historyFilter = hFmtBtn.dataset.hfmt;
      document.querySelectorAll('#historyFilterPills .f-pill').forEach(b => b.classList.toggle('active', b === hFmtBtn));
      renderHistoryFiltered();
      return;
    }
    const lLvlBtn = e.target.closest('#logFilterPills .f-pill[data-llvl]');
    if (lLvlBtn) {
      Store.logLevelFilter = lLvlBtn.dataset.llvl;
      document.querySelectorAll('#logFilterPills .f-pill').forEach(b => b.classList.toggle('active', b === lLvlBtn));
      renderLogsUI();
      return;
    }
    if (e.target === qs('kernelModal')) {
      closeKernelModal();
      return;
    }

    const actEl = e.target.closest('[data-action]');
    if (!actEl) return;
    const act = actEl.dataset.action;

    switch (act) {
      case 'win-min': Store.bridge && Store.bridge.windowMinimize(); break;
      case 'win-max': Store.bridge && Store.bridge.windowToggleMaximize(); break;
      case 'win-close': Store.bridge && Store.bridge.windowClose(); break;
      case 'toggle-theme': toggleTheme(); break;
      case 'sign-out': signOut(); break;
      case 'open-drawer': toggleDrawer(true); break;
      case 'close-drawer': toggleDrawer(false); break;
      case 'drawer-to-logs': toggleDrawer(false); switchView('logs'); break;
      case 'open-export-dir': e.stopPropagation(); Store.bridge && Store.bridge.openExportDir(); break;
      case 'choose-dir':
        Store.bridge && Store.bridge.chooseExportDir(resStr => {
          const r = JSON.parse(resStr);
          if (r.ok && r.exportDir) { applyExportDirUI(r.exportDir); showToast('已更新默认输出目录'); }
        });
        break;
      case 'open-kernel-modal': openKernelModal(); break;
      case 'close-kernel-modal': closeKernelModal(); break;
      case 'detect-kernels':
        Store.bridge && Store.bridge.detectKernels(resStr => { applyKernelState(JSON.parse(resStr)); showToast('内核检测完成'); });
        break;
      case 'download-kernel':
        if (Store.bridge) {
          qs('dlKernelBtn').disabled = true;
          qs('kernelProgTrack').classList.add('show');
          Store.bridge.downloadKernel(Store.kernels.preferred || 'chromium');
        }
        break;
      case 'select-kernel':
        Store.bridge && Store.bridge.selectKernel(actEl.dataset.kind, resStr => {
          applyKernelState(JSON.parse(resStr));
          showToast('已切换渲染内核策略');
        });
        break;
      case 'toggle-password': {
        const inp = qs('passInput');
        inp.type = inp.type === 'password' ? 'text' : 'password';
        break;
      }
      case 'refresh-captcha': refreshCaptcha(); break;
      case 'submit-login': submitLogin(); break;
      case 'open-yuketang':
        Store.bridge && Store.bridge.openPlatformBrowser(qs('yktEditionSel').value || 'yuketang');
        break;
      case 'import-local-files': Store.bridge && Store.bridge.importLocalFiles(); break;
      case 'home-quick-drop':
        switchView('plugins');
        Store.bridge && Store.bridge.importAiAnyFiles();
        break;
      case 'fill-account': {
        const acc = Store.savedAccounts[Number(actEl.dataset.idx)];
        if (acc) {
          qs('schoolInput').value = acc.school || '';
          qs('userInput').value = acc.username || '';
          switchView('workspace');
          selectSourceTab('cctr');
          goStep(0);
          qs('passInput').focus();
          showToast(`已填入 ${acc.school} · ${acc.username}`);
        }
        break;
      }
      case 'remove-account': {
        const acc = Store.savedAccounts[Number(actEl.dataset.idx)];
        if (acc && Store.bridge && Store.bridge.removeSavedAccount) {
          Store.bridge.removeSavedAccount(acc.school, acc.username, raw => {
            Store.savedAccounts = JSON.parse(raw).savedAccounts || [];
            renderSavedAccounts();
          });
        }
        break;
      }
      case 'toggle-practice': {
        const pid = String(actEl.dataset.pid || '');
        if (Store.selectedIds.has(pid)) Store.selectedIds.delete(pid);
        else Store.selectedIds.add(pid);
        renderPracticeGrid();
        break;
      }
      case 'select-all-practices': selectAllPractices(true); break;
      case 'clear-all-practices': selectAllPractices(false); break;
      case 'refresh-practices':
        if (!Store.sessionAccount || !Store.bridge) {
          showToast('当前为本地/网页导入题库，可通过「+ 导入文件」或「+ 网页补抓」添加');
        } else {
          showToast('正在刷新课程目录…');
          Store.bridge.login(Store.sessionAccount.school, Store.sessionAccount.username, Store.sessionAccount.password, '', true);
        }
        break;
      case 'submit-export': submitExport(); break;
      case 'open-task-file': Store.bridge && Store.bridge.openTaskFile(Number(actEl.dataset.tid)); break;
      case 'open-task-dir': Store.bridge && Store.bridge.openTaskDir(Number(actEl.dataset.tid)); break;
      case 'send-task-ai': sendTaskToAiPlugin(Number(actEl.dataset.tid)); break;
      case 'retry-task': Store.bridge && Store.bridge.runTask(Number(actEl.dataset.tid)); break;
      case 'delete-task': Store.bridge && Store.bridge.deleteTask(Number(actEl.dataset.tid)); break;
      case 'copy-logs': {
        const txt = Store.logs.map(l => `[${(l.create_time || '').slice(11, 19)}] #${l.task_id} [${l.level}] ${l.message}`).join('\n');
        navigator.clipboard.writeText(txt).then(() => showToast('已复制诊断日志'));
        break;
      }
      case 'clear-logs': Store.bridge && Store.bridge.clearLogs(); showToast('已清空日志'); break;
      case 'import-ai-any': Store.bridge && Store.bridge.importAiAnyFiles(); break;
      case 'import-ai-qfiles': Store.bridge && Store.bridge.importAiQuestionFiles(); break;
      case 'import-ai-notefiles': Store.bridge && Store.bridge.importAiNoteFiles(); break;
      case 'clear-ai-ws':
        Store.bridge && Store.bridge.clearAiWorkspace('all', () => showToast('已清空当前 AI 工坊工作区'));
        break;
      case 'load-latest-snapshot': {
        const snaps = Store.tasks.filter(t => t.status === 'success' && t.has_snapshot);
        if (!snaps.length) showToast('暂无刚导出的任务快照，可直接把题库文件拖进上方虚线框');
        else sendTaskToAiPlugin(snaps[0].id);
        break;
      }
      case 'sync-practices-to-ai': {
        const chosen = Store.practices.filter(p => Store.selectedIds.has(String(p.id)));
        if (!chosen.length) showToast('「题库与制版」中尚未勾选任何练习');
        else {
          qs('aiProgTrack').classList.add('show');
          qs('aiProgMsg').textContent = '正在同步工作台所选练习题目…';
          Store.bridge && Store.bridge.loadAiFromPractices(JSON.stringify(chosen.map(p => p.json)));
        }
        break;
      }
      case 'toggle-manual-note':
        qs('manualNoteWrap').style.display = qs('manualNoteWrap').style.display === 'none' ? 'block' : 'none';
        break;
      case 'cancel-manual-note': qs('manualNoteWrap').style.display = 'none'; break;
      case 'submit-manual-note':
        Store.bridge && Store.bridge.addAiManualNote(qs('manualNoteTitle').value.trim(), qs('manualNoteBody').value.trim(), resStr => {
          const r = JSON.parse(resStr);
          if (!r.ok) return showToast(r.error || '添加失败');
          qs('manualNoteTitle').value = '';
          qs('manualNoteBody').value = '';
          qs('manualNoteWrap').style.display = 'none';
          showToast('已加入课堂笔记');
        });
        break;
      case 'analyze-note': {
        const n = (Store.aiWorkspace.notes || [])[Number(actEl.dataset.idx)];
        if (n && Store.bridge) {
          qs('aiProgTrack').classList.add('show');
          qs('aiProgMsg').textContent = '正在调用大模型识别该笔记/板书…';
          Store.bridge.analyzeSingleAiNote(n.id, JSON.stringify(collectAiProfile()));
        }
        break;
      }
      case 'remove-note': {
        const n = (Store.aiWorkspace.notes || [])[Number(actEl.dataset.idx)];
        if (n && Store.bridge) Store.bridge.removeAiNote(n.id);
        break;
      }
      case 'prepare-workbuddy':
        if (!Store.aiWorkspace.questionCount) return showToast('请先载入题库');
        Store.bridge && Store.bridge.prepareWorkBuddyHandoff(resStr => {
          const r = JSON.parse(resStr);
          if (!r.ok) return showToast(r.error || '生成任务包失败');
          showToast(r.launchedGui ? '已打开 WorkBuddy 与任务包目录' : '已打开任务包目录（指令已复制到剪贴板）');
        });
        break;
      case 'toggle-paste-reply':
        qs('aiReplyPasteWrap').style.display = qs('aiReplyPasteWrap').style.display === 'none' ? 'block' : 'none';
        break;
      case 'apply-clipboard-reply':
        Store.bridge && Store.bridge.applyExternalAiReply('', resStr => {
          const r = JSON.parse(resStr);
          showToast(r.ok ? r.message : (r.error || '剪贴板中未检测到有效 AI 回复'));
        });
        break;
      case 'apply-pasted-reply':
        Store.bridge && Store.bridge.applyExternalAiReply(qs('aiReplyPasteArea').value.trim(), resStr => {
          const r = JSON.parse(resStr);
          if (r.ok) qs('aiReplyPasteWrap').style.display = 'none';
          showToast(r.ok ? r.message : (r.error || '解析失败'));
        });
        break;
      case 'probe-ollama':
        if (Store.bridge) {
          qs('ollamaDetectedList').innerHTML = '<span style="font-size:11px;color:var(--ink-3);">正在扫描本机 Ollama…</span>';
          const b = qs('aiBaseUrl').value.includes('11434') ? qs('aiBaseUrl').value.trim() : 'http://127.0.0.1:11434';
          Store.bridge.probeOllamaModels(b);
        }
        break;
      case 'select-cli-agent': {
        const ag = Store.aiCliAgents[Number(actEl.dataset.idx)];
        if (!ag) break;
        if (ag.kind === 'gui') {
          qs('[data-action="prepare-workbuddy"]').click();
        } else {
          if (qs('aiPresetSel')) qs('aiPresetSel').value = 'workbuddy_cli';
          qs('aiBaseUrl').value = ag.cmd;
          qs('aiModelName').value = 'auto';
          qs('aiCliCurrentLabel').textContent = `CLI: ${ag.cmd}`;
          saveAiConfigDraft();
          showToast(`已选中本机智能体 CLI：${ag.cmd}`);
        }
        break;
      }
      case 'select-ollama-model': selectOllamaModel(actEl.dataset.model || ''); break;
      case 'run-offline-fusion': startAiFusion('offline'); break;
      case 'run-ai-fusion': startAiFusion('ai'); break;
      case 'run-local-agent-fusion': {
        const prof = collectAiProfile();
        if (prof.provider !== 'ollama' && prof.provider !== 'cli') {
          if (qs('aiPresetSel')) qs('aiPresetSel').value = 'workbuddy_cli';
          const p = Store.aiPresets.find(x => x.key === 'workbuddy_cli');
          if (p) { qs('aiBaseUrl').value = p.base_url; qs('aiModelName').value = p.model; }
        }
        startAiFusion('ai');
        break;
      }
      case 'test-ai-conn':
        if (Store.bridge) {
          qs('aiConnStatus').textContent = '连接中…';
          Store.bridge.testAiModel(JSON.stringify(collectAiProfile()));
        }
        break;
      case 'select-topic':
        Store.activeAiTopicIdx = Number(actEl.dataset.idx);
        Store.aiEditMode = false;
        renderAiTopicListOnly();
        renderTopicInspector();
        break;
      case 'toggle-topic-edit':
        Store.aiEditMode = !Store.aiEditMode;
        renderTopicInspector();
        break;
      case 'add-custom-topic':
        Store.aiWorkspace.topics = Store.aiWorkspace.topics || [];
        Store.aiWorkspace.topics.push({
          id: `tp_custom_${Date.now()}`,
          name: `新增自定义考点 ${Store.aiWorkspace.topics.length + 1}`,
          importance: '重要掌握',
          summary_md: '',
          formulas: [],
          note_ids: [],
          question_ids: [],
        });
        Store.activeAiTopicIdx = Store.aiWorkspace.topics.length - 1;
        Store.aiEditMode = true;
        syncTopicsToPython();
        renderAiWorkspace();
        break;
      case 'merge-selected-topic': {
        const topics = Store.aiWorkspace.topics || [];
        if (topics.length < 2) return showToast('至少需要 2 个考点才可合并');
        const cur = topics[Store.activeAiTopicIdx];
        const targetIdx = Store.activeAiTopicIdx === 0 ? 1 : 0;
        const target = topics[targetIdx];
        target.question_ids = Array.from(new Set([...(target.question_ids || []), ...(cur.question_ids || [])]));
        target.note_ids = Array.from(new Set([...(target.note_ids || []), ...(cur.note_ids || [])]));
        target.formulas = Array.from(new Set([...(target.formulas || []), ...(cur.formulas || [])]));
        if (cur.summary_md) target.summary_md = [target.summary_md, cur.summary_md].filter(Boolean).join('\n\n');
        topics.splice(Store.activeAiTopicIdx, 1);
        Store.activeAiTopicIdx = Math.min(targetIdx, topics.length - 1);
        syncTopicsToPython();
        renderAiWorkspace();
        showToast(`已合并到「${target.name}」`);
        break;
      }
      case 'export-ai-bundle': {
        if (!Store.bridge) break;
        const outputs = Array.from(document.querySelectorAll('.ai-out-chk:checked')).map(el => el.value);
        if (!outputs.length) return showToast('请至少勾选一种要导出的成品格式');
        qs('aiExportBtn').disabled = true;
        qs('aiExportBtn').textContent = '正在制版生成融合成品…';
        qs('aiExportResults').innerHTML = '';
        Store.bridge.exportAiBundle(JSON.stringify({
          title: qs('aiWorkspaceTitle').value.trim() || Store.aiWorkspace.title,
          topics: Store.aiWorkspace.topics || [],
          outputs,
          layoutMode: qs('aiLayoutSel').value || 'inline',
          includeNotes: qs('aiIncNotes').checked,
          includeQuestions: qs('aiIncQs').checked,
          includeNoteImages: qs('aiIncImgs').checked,
          variant: qs('aiVariantSel').value,
          smartPagination: true,
        }));
        break;
      }
      case 'open-export-result': {
        const f = Store.aiExportFiles[Number(actEl.dataset.idx)];
        if (f && Store.bridge) Store.bridge.openLocalPath(f.path);
        break;
      }
      case 'reveal-export-result': {
        const f = Store.aiExportFiles[Number(actEl.dataset.idx)];
        if (f && Store.bridge) Store.bridge.revealLocalPath(f.path);
        break;
      }
    }
  });

  document.addEventListener('change', e => {
    const t = e.target;
    if (t.id === 'smartPagination') updatePreviewCard();
    else if (t.id === 'aiPresetSel') {
      const p = Store.aiPresets.find(x => x.key === t.value);
      if (p) { qs('aiBaseUrl').value = p.base_url || ''; qs('aiModelName').value = p.model || ''; }
      saveAiConfigDraft();
    } else if (['aiBaseUrl', 'aiApiKey', 'aiModelName', 'aiBatchSize', 'aiCustomPrompt'].includes(t.id)) {
      saveAiConfigDraft();
    } else if (t.id === 'aiTaskPickSel' && t.value) {
      sendTaskToAiPlugin(Number(t.value));
    } else if (t.id === 'aiWorkspaceTitle') {
      Store.aiWorkspace.title = t.value.trim() || Store.aiWorkspace.title;
      syncTopicsToPython();
    } else if (t.dataset && t.dataset.topicField) {
      const tp = (Store.aiWorkspace.topics || [])[Store.activeAiTopicIdx];
      if (!tp) return;
      const field = t.dataset.topicField;
      tp[field] = field === 'formulas' ? t.value.split('\n').map(x => x.trim()).filter(Boolean) : t.value;
      syncTopicsToPython();
      renderAiTopicListOnly();
    } else if (t.dataset && t.dataset.moveQid) {
      const qid = t.dataset.moveQid;
      const targetIdx = Number(t.value);
      const topics = Store.aiWorkspace.topics || [];
      if (targetIdx !== Store.activeAiTopicIdx && topics[targetIdx]) {
        topics.forEach(tp => { tp.question_ids = (tp.question_ids || []).filter(id => id !== qid); });
        topics[targetIdx].question_ids.push(qid);
        syncTopicsToPython();
        renderAiWorkspace();
        showToast(`已将题目调入「${topics[targetIdx].name}」`);
      }
    }
  });

  document.addEventListener('input', e => {
    if (e.target.id === 'courseSearch') renderPracticeGrid();
    else if (e.target.id === 'historySearchInput') {
      Store.historyQuery = e.target.value || '';
      renderHistoryFiltered();
    }
  });

  qs('topbar').addEventListener('mousedown', e => {
    if (e.button !== 0 || !Store.bridge) return;
    if (e.target.closest('button, input, select')) return;
    Store.bridge.windowStartDrag();
  });
  qs('topbar').addEventListener('dblclick', e => {
    if (e.target.closest('button, input, select') || !Store.bridge) return;
    Store.bridge.windowToggleMaximize();
  });
  document.querySelectorAll('.resize-edge[data-resize]').forEach(el => {
    el.addEventListener('mousedown', e => {
      if (e.button !== 0 || !Store.bridge) return;
      e.preventDefault();
      Store.bridge.windowStartResize(el.dataset.resize);
    });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { toggleDrawer(false); closeKernelModal(); }
    if (e.key === 'Enter' && Store.view === 'workspace' && Store.step === 0 && qs('srcPaneCctr').style.display !== 'none') submitLogin();
  });
}

/* ── Qt WebChannel Bridge Initialization ────────────────── */
function renderStatePayload(data) {
  Store.tasks = data.tasks || [];
  Store.logs = data.logs || [];
  const tasksSig = JSON.stringify(Store.tasks);
  if (tasksSig !== Store.lastTasksSig) {
    Store.lastTasksSig = tasksSig;
    renderTasksUI();
    renderResultBanner();
  }
  const logsSig = `${Store.logs.length}:${Store.logs.length ? Store.logs[Store.logs.length - 1].id : 0}`;
  if (logsSig !== Store.lastLogsSig) {
    Store.lastLogsSig = logsSig;
    renderLogsUI();
  }
}

function initBridge() {
  bindGlobalUIEvents();
  if (typeof QWebChannel === 'undefined' || !window.qt) return;
  new QWebChannel(qt.webChannelTransport, channel => {
    const b = channel.objects.cctrBridge;
    Store.bridge = b;

    b.getInitialState(raw => {
      const s = JSON.parse(raw);
      if (s.version && qs('appVersionTag')) qs('appVersionTag').textContent = `PRO STUDIO · V${s.version}`;
      applyTheme(!!s.darkMode);
      applyExportDirUI(s.exportDir || '');
      Store.savedAccounts = s.savedAccounts || [];
      renderSavedAccounts();
      applyKernelState(s.kernels || {});
      renderStatePayload(s);
      initAiPluginUI(s);
      if (qs('homeOcrState')) qs('homeOcrState').textContent = s.ocrAvailable ? 'OCR 就绪' : '仅手动';
      qs('ocrStatusTag').textContent = s.ocrAvailable ? 'OCR 模型就绪' : '手动识别';
      qs('ocrStatusTag').className = 'ocr-note' + (s.ocrAvailable ? '' : ' wait');
      refreshCaptcha();
    });

    b.captchaReady.connect(raw => {
      const res = JSON.parse(raw);
      qs('captchaPlaceholder').style.display = 'none';
      if (!res.ok) { qs('ocrStatusTag').textContent = '获取失败'; return; }
      qs('captchaImg').src = res.imageDataUri;
      qs('captchaImg').style.display = 'block';
      if (res.code) {
        qs('captchaInput').value = res.code;
        qs('ocrStatusTag').textContent = '已自动识别';
        qs('ocrStatusTag').className = 'ocr-note';
      } else {
        qs('ocrStatusTag').textContent = res.ocrAvailable ? '识别待补充' : '请手动输入';
        qs('ocrStatusTag').className = 'ocr-note wait';
      }
    });

    b.loginDone.connect(raw => {
      const btn = qs('loginBtn');
      btn.disabled = false;
      btn.textContent = '连接并载入题库';
      const res = JSON.parse(raw);
      if (!res.ok) {
        qs('loginErr').textContent = res.error || '登录失败';
        qs('loginErr').classList.add('show');
        if ((res.error || '').includes('验证码')) refreshCaptcha();
        return;
      }
      if (Array.isArray(res.savedAccounts)) {
        Store.savedAccounts = res.savedAccounts;
        renderSavedAccounts();
      }
      Store.isLoggedIn = true;
      Store.practices = res.practices || [];
      Store.selectedIds.clear();
      qs('sessionPill').classList.add('show');
      qs('sessionLabel').textContent = `${Store.sessionAccount.school} · ${Store.sessionAccount.username}`;
      qs('switchAccBtn').style.display = 'inline-flex';
      switchView('workspace');
      renderPracticeGrid();
      goStep(1);
      showToast(`已载入 ${Store.practices.length} 项练习`);
    });

    if (b.externalBanksLoaded) {
      b.externalBanksLoaded.connect(raw => {
        const res = JSON.parse(raw);
        if (!res.ok || !Array.isArray(res.practices) || !res.practices.length) {
          const errMsg = (Array.isArray(res.errors) && res.errors[0]) || res.error || '';
          if (errMsg) showToast(errMsg);
          return;
        }
        const existingIds = new Set(Store.practices.map(p => String(p.id)));
        res.practices.forEach(p => {
          const sid = String(p.id);
          if (!existingIds.has(sid)) { Store.practices.push(p); existingIds.add(sid); }
          Store.selectedIds.add(sid);
        });
        Store.isLoggedIn = true;
        qs('sessionPill').classList.add('show');
        qs('sessionLabel').textContent = `${res.sourceLabel || '多平台题库'} · ${Store.practices.length} 份`;
        qs('switchAccBtn').style.display = 'inline-flex';
        switchView('workspace');
        renderPracticeGrid();
        goStep(1);
        showToast(`已导入 ${res.practices.length} 份题库（合计 ${Store.practices.length} 项）`);
      });
    }

    b.filtersReady.connect(raw => {
      const res = JSON.parse(raw);
      if (res.practiceId && Store.pendingFilterPracticeId && res.practiceId !== Store.pendingFilterPracticeId) return;
      const cSel = qs('chapterSel');
      const qSel = qs('qtypeSel');
      cSel.innerHTML = '<option value="">全部章节</option>';
      qSel.innerHTML = '<option value="">全部题型</option>';
      (res.chapters || []).forEach(c => {
        const opt = document.createElement('option');
        opt.value = JSON.stringify(c);
        opt.textContent = c.name || '未命名章节';
        cSel.appendChild(opt);
      });
      (res.question_types || []).forEach(q => {
        const opt = document.createElement('option');
        opt.value = JSON.stringify(q);
        opt.textContent = q.name || '未命名题型';
        qSel.appendChild(opt);
      });
    });

    b.stateChanged.connect(raw => renderStatePayload(JSON.parse(raw)));

    b.kernelProgress.connect((pct, msg) => {
      qs('kernelProgFill').style.width = `${pct}%`;
      qs('kernelProgMsg').textContent = msg;
    });
    b.kernelDone.connect(raw => {
      qs('dlKernelBtn').disabled = false;
      const res = JSON.parse(raw);
      if (res.kernels) applyKernelState(res.kernels);
      qs('kernelProgMsg').textContent = res.ok ? res.message : (res.error || '安装失败');
      if (res.ok) showToast(res.message);
    });

    if (b.windowStateChanged) {
      b.windowStateChanged.connect(isMax => {
        document.body.classList.toggle('maximized', !!isMax);
        const btn = qs('maxBtn');
        if (!btn) return;
        btn.title = isMax ? '向下还原' : '最大化';
        btn.innerHTML = isMax
          ? '<svg viewBox="0 0 16 16"><rect x="3" y="5" width="8" height="8" rx="0.8"/><path d="M5.5 3h7.5v7.5"/></svg>'
          : '<svg viewBox="0 0 16 16"><rect x="3.5" y="3.5" width="9" height="9" rx="0.8"/></svg>';
      });
    }

    if (b.aiStateUpdated) {
      b.aiStateUpdated.connect(raw => { Store.aiWorkspace = JSON.parse(raw); renderAiWorkspace(); });
    }
    if (b.aiProbeDone) {
      b.aiProbeDone.connect(raw => {
        const res = JSON.parse(raw);
        const box = qs('ollamaDetectedList');
        if (!res.ok) {
          box.innerHTML = `<span style="font-size:11px;color:var(--vermilion);">${escapeHtml(res.error || '未检测到本机 Ollama')}</span>`;
          return;
        }
        if (!res.models || !res.models.length) {
          box.innerHTML = '<span style="font-size:11px;color:var(--ink-3);">Ollama 已运行，但暂未拉取模型</span>';
          return;
        }
        box.innerHTML = res.models.map(m =>
          `<button type="button" class="ollama-pill" data-action="select-ollama-model" data-model="${escapeHtml(m.name)}">${escapeHtml(m.name)}${m.vision ? ' · 视觉' : ''}</button>`
        ).join('');
      });
    }
    if (b.aiTestDone) {
      b.aiTestDone.connect(raw => {
        const res = JSON.parse(raw);
        qs('aiConnStatus').textContent = res.ok ? '✓ 已连通' : '✗ 异常';
        showToast(res.ok ? `模型连通成功：${res.reply || 'OK'}` : `模型连接失败：${res.error}`);
      });
    }
    if (b.aiProgress) {
      b.aiProgress.connect((pct, msg) => {
        qs('aiProgTrack').classList.add('show');
        qs('aiProgFill').style.width = `${pct}%`;
        qs('aiProgMsg').textContent = msg;
      });
    }
    if (b.aiFusionDone) {
      b.aiFusionDone.connect(raw => {
        if (qs('aiRunBtn')) qs('aiRunBtn').disabled = false;
        if (qs('aiOfflineBtn')) qs('aiOfflineBtn').disabled = false;
        if (qs('aiCliRunBtn')) qs('aiCliRunBtn').disabled = false;
        const res = JSON.parse(raw);
        if (!res.ok) {
          qs('aiProgMsg').textContent = `✗ ${res.error || '失败'}`;
          showToast(res.error || '操作失败');
          return;
        }
        qs('aiProgFill').style.width = '100%';
        qs('aiProgMsg').textContent = res.message || '完成';
        showToast(res.message || '完成');
      });
    }
    if (b.aiExportDone) {
      b.aiExportDone.connect(raw => {
        qs('aiExportBtn').disabled = false;
        qs('aiExportBtn').textContent = '一键导出已选全部成品 →';
        const res = JSON.parse(raw);
        const box = qs('aiExportResults');
        if (!res.ok) { showToast(res.error || '导出失败'); return; }
        Store.aiExportFiles = res.files || [];
        showToast(`已生成 ${Store.aiExportFiles.length} 份融合复习成品！`);
        box.innerHTML = Store.aiExportFiles.map((f, idx) =>
          `<div style="display:inline-flex;gap:5px;align-items:center;border:1px solid var(--jade-line);background:var(--jade-soft);padding:5px 9px;border-radius:6px;font-size:12px;">
            <span style="color:var(--jade);font-weight:600;">${escapeHtml(f.name)}</span>
            <button type="button" class="btn btn-carbon" style="height:24px;padding:0 8px;font-size:11px;" data-action="open-export-result" data-idx="${idx}">打开</button>
            <button type="button" class="btn btn-outline" style="height:24px;padding:0 7px;font-size:11px;" data-action="reveal-export-result" data-idx="${idx}">定位</button>
          </div>`
        ).join('');
      });
    }
  });
}

initBridge();
