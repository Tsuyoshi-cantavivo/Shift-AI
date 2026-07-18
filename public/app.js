/* ============================================================
   ShiftAI — AIシフト自動作成 SaaS
   フロントエンドSPA (Vanilla JS + Chart.js)
   Design System: Deep Navy × Indigo × AI Green
   ============================================================ */

const API = '/api';
let authToken = localStorage.getItem('shift_token') || null;
let currentUser = null;
let currentRole = null;
let currentScreen = null;
let chartInstances = {};
const appState = { period: null, businessHours: null, patterns: null }; // 全画面で共有する期間状態・営業時間・パターン

// ============================================================
// グローバルエラー捕捉：同期エラー・Promise未捕捉rejectの両方を
// toast + console に詳細（ファイル:行:列）表示し、原因特定を容易にする。
// ============================================================
function _formatErr(prefix, msg, file, line, col) {
  const f = (file || '').split('/').pop();
  return `${prefix}: ${msg}${f ? ` (${f}:${line || '?'}${col ? ':' + col : ''})` : ''}`;
}
window?.addEventListener('error', (e) => {
  const m = _formatErr('JS Error', e.message, e.filename, e.lineno, e.colno);
  console.error('[ShiftAI]', m, e.error || '');
  if (window.__toastReady) window.__toast(m, 'error');
});
window?.addEventListener('unhandledrejection', (e) => {
  const reason = e.reason;
  const msg = reason && reason.message ? reason.message : String(reason);
  const line = reason && reason.stack ? (reason.stack.split('\n')[1] || '') : '';
  const m = _formatErr('Promise', msg, line, '', '');
  console.error('[ShiftAI]', m, reason || '');
  if (window.__toastReady) window.__toast(m, 'error');
});

/* ============================================================
   Utilities
   ============================================================ */
async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (authToken) headers['Authorization'] = 'Bearer ' + authToken;
  const res = await fetch(API + path, { ...options, headers });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    if (res.status === 401) logoutLocal();
    throw new Error(data.error || ('HTTP ' + res.status));
  }
  return data;
}

function logoutLocal() {
  authToken = null; currentUser = null; currentRole = null;
  localStorage.removeItem('shift_token');
  // ★ ログアウト時にセッション間で共有されるグローバル状態をクリア
  // （前ユーザーのチャット履歴・カレンダー・キャッシュが残らないように）
  window._miniChat = null;
  window._shopChat = null;
  window._shiftCalCtrl = null;
  window._nextPeriod = null;
  appState.period = null;
  appState.businessHours = null;
  appState.patterns = null;
  wishState = {};
  document.getElementById('loginView')?.classList.remove('d-none');
  document.getElementById('appView')?.classList.add('d-none');
}

const WD = ['日', '月', '火', '水', '木', '金', '土'];
function wdName(d) { return WD[new Date(d + 'T00:00:00').getDay()]; }
function hm(iso) { return iso ? iso.slice(11, 16) : '--:--'; }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function slotClass(iso) { const h = parseInt(iso.slice(11, 13)); if (h < 12) return 'morning'; if (h < 16) return 'noon'; return 'evening'; }
function yen(n) { return '¥' + (n || 0).toLocaleString(); }
function buzz(ms = 8) { try { navigator.vibrate?.(ms); } catch (e) {} }

function todayStr() { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function plusMonths(n) { const d = new Date(); d.setMonth(d.getMonth() + n); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function isPC() { return window.matchMedia('(min-width: 992px)').matches; }

/* Toast */
function toast(msg, type = 'info') {
  const wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const icon = type === 'success' ? 'bi-check-circle-fill' : type === 'error' ? 'bi-x-circle-fill' : 'bi-info-circle-fill';
  el.innerHTML = `<i class="bi ${icon}"></i> ${esc(msg)}`;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(10px)'; setTimeout(() => el.remove(), 300); }, 3000);
}
// グローバルエラーハンドラから toast を呼ぶための公開参照
window.__toast = toast;
window.__toastReady = true;

/* Loading */
function setLoading(on, label) {
  const ex = document.getElementById('loadingOverlay');
  if (ex) ex.remove();
  if (!on) return;
  const el = document.createElement('div');
  el.id = 'loadingOverlay'; el.className = 'loading-overlay';
  el.innerHTML = label
    ? `<div class="text-center"><div class="ai-thinking mb-2"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div><div class="text-secondary small">${esc(label)}</div></div>`
    : '<div class="spinner-border" role="status"></div>';
  document.body.appendChild(el);
}

/* ============================================================
   Component Builders
   ============================================================ */
function card(body, extraClass = '') {
  return `<div class="app-card ${extraClass}"><div class="card-body">${body}</div></div>`;
}

function kpiCard(icon, label, value, sub, variant) {
  return `<div class="kpi-card kpi-${variant}">
    <div class="kpi-icon"><i class="bi ${icon}"></i></div>
    <div class="kpi-label">${label}</div>
    <div class="kpi-value num">${value}</div>
    <div class="kpi-sub">${sub || ''}</div>
  </div>`;
}

function pageHead(title, icon, sub) {
  return `<div class="page-head"><h4><i class="bi ${icon}"></i> ${esc(title)}</h4>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>`;
}

function sectionTitle(icon, title, extra = '') {
  return `<div class="section-title"><i class="bi ${icon}"></i> ${esc(title)} ${extra}</div>`;
}

function emptyState(icon, msg) {
  return `<div class="empty-state"><i class="bi ${icon}"></i><div>${esc(msg)}</div></div>`;
}

function badge(text, variant = 'muted') {
  return `<span class="badge-soft ${variant}">${esc(text)}</span>`;
}

/* Modal */
function openModal(title, bodyHtml, onSave, opts = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'modal-overlay';
  const saveLabel = opts.saveLabel || '保存';
  wrap.innerHTML = `
    <div class="modal-box" style="${opts.width ? 'max-width:' + opts.width + 'px' : ''}">
      <div class="modal-header">
        <div class="modal-title">${title}</div>
        <button class="modal-close" data-x><i class="bi bi-x-lg"></i></button>
      </div>
      <div class="modal-body">${bodyHtml}</div>
      <div class="modal-footer">
        <button class="btn btn-light" data-x>キャンセル</button>
        ${onSave ? `<button class="btn btn-primary" data-save>${saveLabel}</button>` : ''}
      </div>
    </div>`;
  document.body.appendChild(wrap);
  const close = () => wrap.remove();
  wrap.querySelectorAll('[data-x]').forEach((b) => b?.addEventListener('click', close));
  wrap?.addEventListener('click', (e) => { if (e.target === wrap) close(); });
  if (onSave) wrap.querySelector('[data-save]')?.addEventListener('click', () => onSave(wrap, close));
  return wrap;
}

/* ============================================================
   Login / Init
   ============================================================ */
function showLogin() {
  document.getElementById('loginView')?.classList.remove('d-none');
  document.getElementById('appView')?.classList.add('d-none');
}
function showApp() {
  document.getElementById('loginView')?.classList.add('d-none');
  document.getElementById('appView')?.classList.remove('d-none');
  renderNav();
  // 店舗の場合は期間・営業時間を事前取得してから画面へ
  if (currentRole === 'shop') {
    Promise.all([ensurePeriod(), ensureBusinessHours()]).then(() => navigateTo(defaultScreen()));
  } else {
    navigateTo(defaultScreen());
  }
}
function defaultScreen() {
  if (currentRole === 'shop') return 'dashboard';
  if (currentRole === 'staff') return 'staffDashboard';
  if (currentRole === 'admin') return 'adminHome';
  return 'dashboard';
}

document.getElementById('loginBtn')?.addEventListener('click', async () => {
  const id = document.getElementById('loginId').value.trim();
  const pw = document.getElementById('loginPassword').value;
  const errEl = document.getElementById('loginError');
  errEl.textContent = ''; setLoading(true);
  try {
    const data = await api('/login', { method: 'POST', body: JSON.stringify({ id, password: pw }) });
    authToken = data.token; currentUser = data.user; currentRole = data.role;
    // ★ ログイン時に前ユーザーの状態を確実にクリア（ログアウト漏れ対策の二重 defence）
    window._miniChat = null;
    window._shopChat = null;
    window._shiftCalCtrl = null;
    localStorage.setItem('shift_token', authToken);
    showApp();
  } catch (e) { errEl.textContent = e.message; }
  finally { setLoading(false); }
});
document.getElementById('loginPassword')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') document.getElementById('loginBtn').click(); });
document.getElementById('logoutBtn')?.addEventListener('click', async () => { try { await api('/logout', { method: 'POST' }); } catch {} logoutLocal(); });
document.getElementById('notifBtn')?.addEventListener('click', () => openNotifications());

/* ============================================================
   Theme (dark/light) toggle
   ============================================================ */
function currentTheme() { return document.documentElement.getAttribute('data-theme') || 'dark'; }
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
  try { localStorage.setItem('shiftai_theme', t); } catch (e) {}
  const icon = document.querySelector('#themeToggleBtn i');
  if (icon) icon.className = (t === 'light') ? 'bi bi-sun-fill' : 'bi bi-moon-stars-fill';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', t === 'light' ? '#F1F5F9' : '#0F172A');
}
applyTheme(currentTheme()); // アイコンとmetaを現在テーマに同期
document.getElementById('themeToggleBtn')?.addEventListener('click', () => {
  applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
});
document.getElementById('menuToggle')?.addEventListener('click', () => {
  document.getElementById('sideNav')?.classList.toggle('open');
  document.getElementById('sideOverlay')?.classList.toggle('d-none');
});
document.getElementById('sideOverlay')?.addEventListener('click', () => {
  document.getElementById('sideNav')?.classList.remove('open');
  document.getElementById('sideOverlay')?.classList.add('d-none');
});

(async function bootstrap() {
  if (authToken) {
    try {
      const data = await api('/me'); currentUser = data.user; currentRole = data.role;
      // ★ 自動ログイン時も前セッションの状態をクリア
      window._miniChat = null;
      window._shopChat = null;
      window._shiftCalCtrl = null;
      showApp();
    }
    catch { logoutLocal(); }
  }
})();

/* ============================================================
   Notifications
   ============================================================ */
async function refreshNotifBadge() {
  if (!currentRole) return;
  try {
    const d = await api(`/${currentRole}/notifications`);
    const badge = document.getElementById('notifBadge');
    const btn = document.getElementById('notifBtn');
    if (btn) btn.classList.remove('d-none');
    if (d.unread > 0) { if (badge) { badge.textContent = d.unread; badge.classList.remove('d-none'); } }
    else { if (badge) badge.classList.add('d-none'); }
    // サイドバーの通知バッジも更新
    const sideBadge = document.getElementById('sideNotifBadge');
    if (sideBadge) {
      if (d.unread > 0) { sideBadge.textContent = d.unread; sideBadge.style.display = ''; }
      else { sideBadge.style.display = 'none'; }
    }
    // 希望休管理のバッジ
    if (currentRole === 'shop') {
      try {
        const shifts = await api(`/shop/shifts?start=${todayStr().slice(0,8)+'01'}&end=${plusMonths(2)}`);
        const reqCount = (shifts.shifts || []).filter((s) => s.status === 'requested').length;
        const reqBadge = document.getElementById('sideReqBadge');
        if (reqBadge) {
          if (reqCount > 0) { reqBadge.textContent = reqCount; reqBadge.style.display = ''; }
          else { reqBadge.style.display = 'none'; }
        }
      } catch {}
    }
  } catch {}
}
function openNotifications() {
  api(`/${currentRole}/notifications`).then((d) => {
    const renderList = (notifs) => notifs.length ? notifs.map((n) => `
      <div class="notif-item ${n.is_read ? '' : 'unread'}">
        <div class="nt-title">${esc(n.title)}</div>
        <div class="nt-body">${esc(n.body || '')}</div>
        <div class="nt-time">${esc((n.created_at || '').replace('T', ' ').slice(0, 16))}</div>
      </div>`).join('') : '<div class="text-muted small">通知はありません</div>';
    const w = openModal('<i class="bi bi-bell"></i> 通知', renderList(d.notifications) + (d.notifications.length ? '<button class="btn btn-light w-full mt-3" id="readAllBtn">すべて既読にする</button>' : ''), null);
    if (d.notifications.length) {
      w.querySelector('#readAllBtn')?.addEventListener('click', async () => {
        await api(`/${currentRole}/notifications/read-all`, { method: 'PUT' });
        // モーダル内のリストを既読状態で再描画
        const updated = d.notifications.map((n) => ({ ...n, is_read: 1 }));
        w.querySelector('.modal-body').innerHTML = renderList(updated) + '<div class="small text-success mt-2"><i class="bi bi-check-circle"></i> すべて既読にしました</div>';
        w.querySelector('#readAllBtn')?.remove();
        refreshNotifBadge();
      });
    }
  });
}

/* ============================================================
   Navigation
   ============================================================ */
const NAV_DEFS = {
  shop: [
    { key: 'dashboard', icon: 'bi-grid-1x2', label: 'ダッシュボード', mobile: true },
    { key: 'shifts', icon: 'bi-calendar3', label: 'シフト', mobile: true },
    { key: 'aiGenerate', icon: 'bi-stars', label: 'AIシフト作成', mobile: true, ai: true },
    { key: 'staffs', icon: 'bi-people', label: 'スタッフ管理', mobile: true },
    { key: 'requests', icon: 'bi-inbox', label: '希望休管理' },
    { key: 'analytics', icon: 'bi-graph-up-arrow', label: '人件費分析' },
    { key: 'notifications', icon: 'bi-bell', label: '通知' },
    { key: 'settings', icon: 'bi-gear', label: '設定', mobile: true },
  ],
  staff: [
    { key: 'staffDashboard', icon: 'bi-house-door', label: 'ホーム', mobile: true },
    { key: 'myshift', icon: 'bi-calendar-check', label: 'マイシフト', mobile: true },
    { key: 'request', icon: 'bi-pencil-square', label: '希望提出', mobile: true },
    { key: 'staffSettings', icon: 'bi-person-gear', label: '設定', mobile: true },
  ],
  admin: [
    { key: 'adminHome', icon: 'bi-house-door', label: 'ホーム', mobile: true },
    { key: 'adminShops', icon: 'bi-shop', label: '店舗', mobile: true },
  ],
};

function renderNav() {
  const defs = NAV_DEFS[currentRole] || [];
  // Sidebar (PC)
  const side = document.getElementById('sideNav');
  side.innerHTML = `
    <div class="side-section-label">メインメニュー</div>
    ${defs.map((it) => `
      <button class="side-item" data-screen="${it.key}">
        <div class="side-item-icon"><i class="bi ${it.icon}"></i></div>
        <span>${it.label}</span>
        ${it.key === 'notifications' ? '<span class="side-item-badge" id="sideNotifBadge" style="display:none">0</span>' : ''}
        ${it.key === 'requests' ? '<span class="side-item-badge" id="sideReqBadge" style="display:none">0</span>' : ''}
      </button>`).join('')}
    <div class="side-footer"><i class="bi bi-shield-check"></i> ShiftAI v2.0</div>`;
  side.querySelectorAll('.side-item').forEach((b) => b?.addEventListener('click', () => {
    navigateTo(b.dataset.screen);
    if (!isPC()) { side.classList.remove('open'); document.getElementById('sideOverlay')?.classList.add('d-none'); }
  }));
  // Bottom nav (mobile)
  const mobileDefs = defs.filter((d) => d.mobile);
  const bn = document.getElementById('bottomNav');
  bn.innerHTML = mobileDefs.map((it) => `
    <button class="bn-item" data-screen="${it.key}">
      <i class="bi ${it.icon}"></i><span>${it.label.replace('シフト作成', 'AI作成').replace('ダッシュボード', 'ホーム')}</span>
    </button>`).join('');
  bn.querySelectorAll('.bn-item').forEach((b) => b?.addEventListener('click', () => navigateTo(b.dataset.screen)));
}

function setActiveNav() {
  document.querySelectorAll('.side-item, .bn-item').forEach((b) => b.classList.toggle('active', b.dataset.screen === currentScreen));
  const defs = NAV_DEFS[currentRole] || [];
  const label = defs.find((i) => i.key === currentScreen)?.label || 'ShiftAI';
  document.getElementById('headerTitle').textContent = label;
}

function navigateTo(screen) {
  // Destroy charts on navigation
  Object.values(chartInstances).forEach((c) => { try { c.destroy(); } catch {} });
  chartInstances = {};
  // Close all open modals
  document.querySelectorAll('.modal-overlay').forEach((m) => m.remove());
  currentScreen = screen;
  setActiveNav();
  const content = document.getElementById('content');
  content.innerHTML = '';
  content.className = 'app-content fade-in';
  const fn = SCREENS[screen];
  if (fn) fn(content); else content.innerHTML = emptyState('bi-exclamation-circle', '画面が見つかりません');
  refreshNotifBadge();
}

/* 全画面共有の期間を取得（キャッシュ付き） */
async function ensurePeriod() {
  if (appState.period) return appState.period;
  try {
    appState.period = await api('/shop/periods/next');
    window._nextPeriod = appState.period; // 後方互換
    return appState.period;
  } catch { return { start_date: '', end_date: '', deadline: '' }; }
}

/* 店舗の営業時間をパターン（shift_patterns）の最小開始/最大終了から算出してキャッシュ。
   タイムライン表示で「日によって時間軸が変わる」のを防ぎ、営業時間全体を固定表示する。 */
async function ensureBusinessHours() {
  if (appState.businessHours && appState.patterns) return appState.businessHours;
  const fallback = { start: 9, end: 22 };
  if (currentRole !== 'shop' && currentRole !== 'staff') return fallback;
  try {
    const d = await api('/shop/patterns');
    const pats = d.patterns || [];
    appState.patterns = pats;
    if (!pats.length) { appState.businessHours = fallback; return fallback; }
    let start = 24, end = 0;
    pats.forEach((p) => {
      const sh = +(p.start_time || '').slice(0, 2);
      const eh = +(p.end_time || '').slice(0, 2) + (+(p.end_time || '').slice(3, 5) > 0 ? 1 : 0);
      if (!isNaN(sh)) start = Math.min(start, sh);
      if (!isNaN(eh)) end = Math.max(end, eh);
    });
    if (start >= end) { appState.businessHours = fallback; return fallback; }
    appState.businessHours = { start, end };
    return appState.businessHours;
  } catch { return fallback; }
}

/* 時間帯別不足計算（タイムライン・印刷・不足通知で共通利用）
   戻り値: [{ hour, required, placed, gap }] — gap>0 の時間帯が不足
*/
function _computeHourlyGaps(shifts, dayStr) {
  const pats = appState.patterns;
  if (!pats || !pats.length) return [];
  const wd = new Date(dayStr + 'T00:00:00').getDay();
  const bh = appState.businessHours || { start: 9, end: 22 };
  // 各パターンから曜日別必要人数を取得
  const hourReq = {}; // hour → required
  pats.forEach((p) => {
    const ps = +(p.start_time || '').slice(0, 2);
    const pe = +(p.end_time || '').slice(0, 2);
    const wr = (p.weekday_required || {});
    const req = wr[String(wd)] != null ? +wr[String(wd)] : (p.required_staff || 0);
    if (req <= 0) return;
    for (let h = ps; h < pe; h++) {
      hourReq[h] = Math.max(hourReq[h] || 0, req);
    }
  });
  // confirmed シフトで各時間帯の配置人数をカウント
  const hourPlaced = {};
  (shifts || []).forEach((s) => {
    if (s.status !== 'confirmed' && s.status !== 'modifying') return;
    const sh = +(s.start_datetime || '').slice(11, 13);
    const eh = +(s.end_datetime || '').slice(11, 13) + (+(s.end_datetime || '').slice(14, 16) > 0 ? 1 : 0);
    for (let h = sh; h < eh; h++) {
      hourPlaced[h] = (hourPlaced[h] || 0) + 1;
    }
  });
  // 不足時間帯を返す
  const result = [];
  for (let h = bh.start; h < bh.end; h++) {
    const req = hourReq[h] || 0;
    if (req <= 0) continue;
    const placed = hourPlaced[h] || 0;
    const gap = req - placed;
    if (gap > 0) {
      result.push({ hour: h, required: req, placed, gap });
    }
  }
  return result;
}

/* 不足時間帯を連続区間にマージ（"17:00〜21:00 あと2名"のように表示するため） */
function _mergeHourlyGaps(gaps) {
  if (!gaps.length) return [];
  const merged = [];
  let cur = { start: gaps[0].hour, end: gaps[0].hour + 1, gap: gaps[0].gap };
  for (let i = 1; i < gaps.length; i++) {
    const g = gaps[i];
    if (g.hour === cur.end && g.gap === cur.gap) {
      cur.end = g.hour + 1;
    } else {
      merged.push(cur);
      cur = { start: g.hour, end: g.hour + 1, gap: g.gap };
    }
  }
  merged.push(cur);
  return merged;
}

/* ============================================================
   Shared: Calendar
   ============================================================ */
function createCalendar(mountEl, opts) {
  const today = new Date();
  let initY = today.getFullYear(), initM = today.getMonth();
  if (opts?.initial) { const d0 = new Date(opts.initial + 'T00:00:00'); if (!isNaN(d0)) { initY = d0.getFullYear(); initM = d0.getMonth(); } }
  let state = { y: initY, m: initM, selectedDay: null, shifts: [] };
  let lastTap = 0;

  async function refresh() {
    setLoading(true);
    try {
      const from = `${state.y}-${String(state.m + 1).padStart(2, '0')}-01`;
      const to = `${state.y}-${String(state.m + 1).padStart(2, '0')}-31`;
      state.shifts = await opts.loader(from, to);
      draw();
    } catch (e) { mountEl.innerHTML = `<div class="text-danger">${esc(e.message)}</div>`; }
    finally { setLoading(false); }
  }

  function byDay() {
    const m = {};
    state.shifts.forEach((s) => { const d = s.start_datetime.slice(0, 10); (m[d] = m[d] || []).push(s); });
    return m;
  }

  function draw() {
    const bd = byDay();
    const startWd = new Date(state.y, state.m, 1).getDay();
    const dim = new Date(state.y, state.m + 1, 0).getDate();
    const todayStr = today.toISOString().slice(0, 10);
    let cells = '';
    for (let i = 0; i < startWd; i++) cells += '<div class="cal-cell empty"></div>';
    for (let d = 1; d <= dim; d++) {
      const ds = `${state.y}-${String(state.m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      const list = bd[ds] || [];
      const wd = new Date(ds + 'T00:00:00').getDay();
      const cls = ['cal-cell'];
      if (ds === todayStr) cls.push('today');
      if (ds === state.selectedDay) cls.push('selected');
      const dowCls = wd === 0 ? 'sun' : (wd === 6 ? 'sat' : '');
      const chips = list.slice(0, 3).map((s) => {
        // confirmed は実線、requested（調整待ち）は点線で区別（混在表示の誤認防止）
        const dashed = s.status === 'requested' ? ' chip-pending' : '';
        return `<div class="chip ${slotClass(s.start_datetime)}${dashed}" title="${s.status === 'requested' ? '調整待ち' : '確定'}">${hm(s.start_datetime)}-${hm(s.end_datetime)}</div>`;
      }).join('');
      // 調整待ちが混在する場合は警告アイコンを右上に表示
      const pendingCnt = list.filter((s) => s.status === 'requested').length;
      const confirmedCnt = list.length - pendingCnt;
      const badge = pendingCnt > 0 ? `<span class="cal-pending-badge" title="確定${confirmedCnt}件 / 調整待ち${pendingCnt}件">!</span>` : '';
      const extra = list.length > 3 ? `<div class="chip count">+${list.length - 3}</div>` : '';
      cells += `<div class="${cls.join(' ')}" data-day="${ds}"><div class="cal-date ${dowCls}">${d}${badge}</div><div class="cal-chips">${chips}${extra}</div></div>`;
    }
    mountEl.innerHTML = `
      <div class="cal-hint no-print">日付をダブルタップでシフト表を表示</div>
      <div class="cal-toolbar">
        <button class="cal-nav-btn" id="calPrev"><i class="bi bi-chevron-left"></i></button>
        <div class="cal-title num">${state.y}年 ${state.m + 1}月</div>
        <button class="cal-nav-btn" id="calNext"><i class="bi bi-chevron-right"></i></button>
      </div>
      <div class="cal-weekdays"><div class="sun">日</div><div>月</div><div>火</div><div>水</div><div>木</div><div>金</div><div class="sat">土</div></div>
      <div class="cal-grid">${cells}</div>
      <div class="day-detail" id="dayDetail"></div>`;
    mountEl.querySelector('#calPrev')?.addEventListener('click', () => { state.m--; if (state.m < 0) { state.m = 11; state.y--; } refresh(); });
    mountEl.querySelector('#calNext')?.addEventListener('click', () => { state.m++; if (state.m > 11) { state.m = 0; state.y++; } refresh(); });
    mountEl.querySelectorAll('.cal-cell[data-day]').forEach((c) => c?.addEventListener('click', () => {
      const now = Date.now();
      state.selectedDay = c.dataset.day; draw(); drawDetail();
      if (now - lastTap < 350) { openDayTimeline(c.dataset.day, state.shifts, opts.editable, opts.onChange); lastTap = 0; }
      else lastTap = now;
    }));
    if (state.selectedDay) drawDetail();
  }

  function drawDetail() {
    const box = mountEl.querySelector('#dayDetail');
    const list = (byDay()[state.selectedDay] || []).slice().sort((a, b) => a.start_datetime.localeCompare(b.start_datetime));
    if (!list.length) { box.innerHTML = `<div class="day-detail-header"><i class="bi bi-calendar-x"></i> ${esc(state.selectedDay)}（${wdName(state.selectedDay)}）</div>${emptyState('bi-cup-hot', 'この日にシフトはありません')}`; return; }
    box.innerHTML = `<div class="day-detail-header"><i class="bi bi-calendar-week"></i> ${esc(state.selectedDay)}（${wdName(state.selectedDay)}） — ${list.length}件</div>` + list.map((s) => shiftDetailHtml(s, opts.editable)).join('');
    if (opts.editable) box.querySelectorAll('.edit-shift').forEach((b, i) => b?.addEventListener('click', () => showEditModal(list[i])));
  }

  refresh();
  return { goToMonth(y, m) { state.y = y; state.m = m; state.selectedDay = null; return refresh(); }, refresh };
}

function shiftDetailHtml(s, editable) {
  const sc = slotClass(s.start_datetime);
  const statusBadge = s.status === 'confirmed' ? badge('確定', 'success') : s.status === 'requested' ? badge('調整待ち', 'warning') : badge('調整中', 'info');
  const edit = editable ? `<button class="btn btn-sm btn-light edit-shift"><i class="bi bi-pencil"></i></button>` : '';
  return `<div class="shift-line">
    <div><span class="dot ${sc}"></span><span class="time">${hm(s.start_datetime)} - ${hm(s.end_datetime)}</span>${s.break_time_minutes ? `<span class="who">・休憩${s.break_time_minutes}分</span>` : ''} ${statusBadge}</div>
    <div class="flex items-center gap-2"><span class="who">${esc(s.staff_name || '')}</span>${edit}</div>
  </div>`;
}

/* ============================================================
   Print / PDF (1日1ページ・タイムライン形式) — 印刷時にのみ表示されるビューを構築
   ============================================================ */
window?.addEventListener('afterprint', () => {
  const pv = document.getElementById('printView');
  if (pv) pv.innerHTML = '';
});

function _tlTimeMin(iso) {
  return +iso.slice(11, 13) * 60 + +iso.slice(14, 16);
}

function buildPrintTimelineHtml(list) {
  // list: その日の confirmed シフト群。タイムライン（矢印バー）形式で返す。
  const order = []; const staffMap = {};
  list.forEach((s) => {
    if (!staffMap[s.staff_id]) {
      staffMap[s.staff_id] = { name: s.staff_name || ('#' + s.staff_id), shifts: [] };
      order.push(s.staff_id);
    }
    staffMap[s.staff_id].shifts.push(s);
  });
  // 時間軸は「営業時間」をベースにし、シフトがはみ出す場合のみ拡張（全日で統一）。
  const bh = appState.businessHours || { start: 9, end: 22 };
  let minH = bh.start, maxH = bh.end;
  list.forEach((s) => {
    minH = Math.min(minH, +s.start_datetime.slice(11, 13));
    const eH = +s.end_datetime.slice(11, 13) + (+(s.end_datetime.slice(14, 16)) > 0 ? 1 : 0);
    maxH = Math.max(maxH, eH);
  });
  minH = Math.max(0, Math.floor(minH));
  maxH = Math.min(24, Math.ceil(maxH));
  if (maxH <= minH) maxH = minH + 1;
  const rangeMin = minH * 60, rangeLen = (maxH - minH) * 60;

  const hours = [];
  for (let h = minH; h <= maxH; h++) hours.push(`<div class="tl-hour">${h}</div>`);

  const rows = order.map((sid) => {
    const st = staffMap[sid];
    const bars = st.shifts.map((s) => {
      const sMin = _tlTimeMin(s.start_datetime);
      let eMin = _tlTimeMin(s.end_datetime);
      if (eMin <= sMin) eMin = sMin + 60;
      const left = ((sMin - rangeMin) / rangeLen) * 100;
      const width = Math.max(4, ((eMin - sMin) / rangeLen) * 100);
      const lbl = width > 14 ? `${hm(s.start_datetime)}-${hm(s.end_datetime)}` : `${hm(s.start_datetime)}`;
      return `<div class="tl-bar ${slotClass(s.start_datetime)}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">${lbl}</div>`;
    }).join('');
    return `<div class="tl-row"><div class="tl-name">${esc(st.name)}</div><div class="tl-track">${bars}</div></div>`;
  }).join('');

  // 時間帯別不足バー（印刷用）
  const dayStr = list.length ? list[0].start_datetime.slice(0, 10) : '';
  const gaps = dayStr ? _computeHourlyGaps(list, dayStr) : [];
  let gapRowHtml = '';
  if (gaps.length) {
    const merged = _mergeHourlyGaps(gaps);
    const gapBars = merged.map((g) => {
      const left = ((g.start * 60 - rangeMin) / rangeLen) * 100;
      const width = Math.max(4, ((g.end - g.start) * 60 / rangeLen) * 100);
      return `<div class="tl-gap-bar" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">↓${g.gap}名不足</div>`;
    }).join('');
    gapRowHtml = `<div class="tl-row tl-gap-row"><div class="tl-name tl-gap-name">不足</div><div class="tl-track">${gapBars}</div></div>`;
  }

  // シフト0件でも営業時間の空き状況が分かるよう、時間軸だけ表示して「シフトなし」を添える
  if (!list.length) {
    return `<div class="tl-wrap">
      <div class="tl-axis-row"><div class="tl-name"></div><div class="tl-axis">${hours.join('')}</div></div>
      <div class="print-empty">この日はシフトがありません（営業時間 ${minH}時〜${maxH}時は全枠空き）</div>
    </div>`;
  }
  return `<div class="tl-wrap">
    <div class="tl-axis-row"><div class="tl-name"></div><div class="tl-axis">${hours.join('')}</div></div>
    ${rows}
    ${gapRowHtml}
  </div>
  <div class="tl-legend">
    <span><i style="background:#F59E0B"></i>朝</span>
    <span><i style="background:#10B981"></i>昼</span>
    <span><i style="background:#6366F1"></i>夜</span>
    <span><i style="background:#EF4444"></i>不足</span>
  </div>`;
}

async function openPrintView(start, end) {
  if (!start || !end) { toast('期間を指定してください'); return; }
  setLoading(true);
  try {
    const shiftsD = await api(`/shop/shifts?start=${start}&end=${end}`);
    const shifts = (shiftsD.shifts || [])
      .filter((s) => s.status === 'confirmed')
      .sort((a, b) => (a.start_datetime || '').localeCompare(b.start_datetime || ''));
    const byDay = {};
    shifts.forEach((s) => {
      const day = (s.start_datetime || '').slice(0, 10);
      if (!day) return;
      (byDay[day] = byDay[day] || []).push(s);
    });
    // 期間内の全日（シフトが無い日も「この日にシフトはありません」ページとして出力）
    // ※ toISOString() はUTC変換でタイムゾーンのズレが出るため、ローカル日付で文字列化
    const fmtDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const days = [];
    {
      const cur = new Date(start + 'T00:00:00');
      const endD = new Date(end + 'T00:00:00');
      while (cur <= endD) {
        days.push(fmtDay(cur));
        cur.setDate(cur.getDate() + 1);
      }
    }
    if (!days.length) { setLoading(false); toast('期間が無効です', 'error'); return; }

    const shopName = (currentUser && currentUser.shop_name) || 'ShiftAI';
    const wdArr = ['日', '月', '火', '水', '木', '金', '土'];

    const pagesHtml = days.map((day) => {
      const list = byDay[day] || [];
      const wd = new Date(day + 'T00:00:00').getDay();
      const timeline = buildPrintTimelineHtml(list);
      return `<section class="print-page">
        <div class="print-page-header">
          <h2>${day}（${wdArr[wd]}）</h2>
          <div class="print-shop">${esc(shopName)}</div>
        </div>
        ${timeline}
        <div class="print-footer">発行日: ${new Date().toLocaleString('ja-JP', { hour12: false })} · ShiftAI</div>
      </section>`;
    }).join('');

    const pv = document.getElementById('printView');
    pv.innerHTML = pagesHtml;
    setLoading(false);
    // レンダリングを1フレーム待ってから印刷ダイアログを開く
    requestAnimationFrame(() => requestAnimationFrame(() => window.print()));
  } catch (e) {
    setLoading(false);
    toast('印刷ビューの生成に失敗: ' + e.message, 'error');
  }
}

function openDayTimeline(date, allShifts, editable, onChange) {
  buzz(12);
  const list = (allShifts || []).filter((s) => s.start_datetime.slice(0, 10) === date).sort((a, b) => a.start_datetime.localeCompare(b.start_datetime));
  const order = []; const staffMap = {};
  list.forEach((s) => { if (!staffMap[s.staff_id]) { staffMap[s.staff_id] = { name: s.staff_name || ('#' + s.staff_id), shifts: [] }; order.push(s.staff_id); } staffMap[s.staff_id].shifts.push(s); });
  // 時間軸は「営業時間」をベースにし、シフトが営業時間外にはみ出す場合のみ拡張。
  // これにより「シフトが無い時間帯が消える」「日によって軸が変わる」を防ぐ。
  const bh = appState.businessHours || { start: 9, end: 22 };
  let minH = bh.start, maxH = bh.end;
  list.forEach((s) => {
    minH = Math.min(minH, +s.start_datetime.slice(11, 13));
    const eH = +s.end_datetime.slice(11, 13) + (+(s.end_datetime.slice(14,16)) > 0 ? 1 : 0);
    maxH = Math.max(maxH, eH);
  });
  minH = Math.max(0, Math.floor(minH)); maxH = Math.min(24, Math.ceil(maxH));
  if (maxH <= minH) maxH = minH + 1;
  const rangeMin = minH * 60, rangeLen = (maxH - minH) * 60;
  const hours = []; for (let h = minH; h <= maxH; h++) hours.push(`<div class="tl-hour">${h}</div>`);
  const rows = order.map((sid) => {
    const st = staffMap[sid];
    const bars = st.shifts.map((s) => {
      const sMin = +s.start_datetime.slice(11, 13) * 60 + +s.start_datetime.slice(14, 16);
      let eMin = +s.end_datetime.slice(11, 13) * 60 + +s.end_datetime.slice(14, 16);
      if (eMin <= sMin) eMin = sMin + 60;
      const left = ((sMin - rangeMin) / rangeLen) * 100, width = Math.max(3, ((eMin - sMin) / rangeLen) * 100);
      const lbl = width > 12 ? `${hm(s.start_datetime)}-${hm(s.end_datetime)}` : '';
      return `<div class="tl-bar ${slotClass(s.start_datetime)}" data-id="${s.id}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">${lbl}</div>`;
    }).join('');
    return `<div class="tl-row" data-staff-id="${sid}" data-staff-name="${esc(st.name)}"><div class="tl-name">${esc(st.name)}</div><div class="tl-track" data-staff-id="${sid}" title="${editable ? '空き部分をクリックで追加' : ''}">${bars}</div></div>`;
  }).join('');

  // 時間帯別不足バー（赤で視覚化）
  const gaps = _computeHourlyGaps(list, date);
  let gapRow = '';
  if (gaps.length) {
    const merged = _mergeHourlyGaps(gaps);
    const gapBars = merged.map((g) => {
      const left = ((g.start * 60 - rangeMin) / rangeLen) * 100;
      const width = Math.max(4, ((g.end - g.start) * 60 / rangeLen) * 100);
      return `<div class="tl-gap-bar" data-start="${g.start}" data-end="${g.end}" data-gap="${g.gap}" title="${editable ? 'クリックして配置' : ''}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">↓${g.gap}名不足</div>`;
    }).join('');
    gapRow = `<div class="tl-row tl-gap-row"><div class="tl-name tl-gap-name">不足</div><div class="tl-track">${gapBars}</div></div>`;
  }

  const body = !list.length ? emptyState('bi-cup-hot', 'この日にシフトはありません') :
    `<div class="tl-wrap"><div class="tl-axis-row"><div class="tl-name"></div><div class="tl-axis">${hours.join('')}</div></div>${rows}${gapRow}</div>
     <div class="tl-legend"><span><i style="background:#F59E0B"></i>朝</span><span><i style="background:#10B981"></i>昼</span><span><i style="background:#6366F1"></i>夜</span><span><i style="background:#EF4444"></i>不足</span>${editable ? '<span><i class="bi bi-hand-index" style="font-style:normal;font-size:.7rem"></i>空きをクリックで追加</span>' : ''}<span>バーをタップで${editable ? '編集' : '詳細'}</span></div>`;
  const w = openModal(`<i class="bi bi-diagram-3"></i> ${esc(date)}（${wdName(date)}）のシフト表`, body, null);
  w.querySelectorAll('.tl-bar').forEach((bar) => bar?.addEventListener('click', (ev) => {
    ev.stopPropagation();
    buzz(10);
    w.querySelectorAll('.tl-bar').forEach((b) => b.classList.remove('selected'));
    bar.classList.add('selected');
    const s = list.find((x) => String(x.id) === bar.dataset.id);
    if (editable && s) showEditModal(s);
    else if (onChange && s) onChange(s);
  }));
  // 空き部分クリック → そのスタッフ＋クリック位置の時間帯で追加
  if (editable) {
    w.querySelectorAll('.tl-track').forEach((track) => {
      track?.addEventListener('click', (e) => {
        if (e.target.closest('.tl-bar') || e.target.closest('.tl-gap-bar')) return; // バー/不足バーのクリックは別処理
        const staffId = track.dataset.staffId;
        const staffName = track.closest('.tl-row').dataset.staffName;
        // クリックX座標から時間を計算
        const rect = track.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        const clickMin = rangeMin + ratio * rangeLen;
        const startHour = Math.max(minH, Math.min(maxH - 1, Math.floor(clickMin / 60)));
        const endHour = Math.min(maxH, startHour + 4); // デフォルト4h
        const fmtH = (h) => `${String(h).padStart(2, '0')}:00`;
        buzz(10);
        const addW = openModal(`<i class="bi bi-plus-lg"></i> シフト追加 — ${esc(staffName)} ${date}`,
          `<div class="row">
             <div class="col-6"><label class="form-label" for="qStart">開始</label><input type="time" id="qStart" class="form-control" value="${fmtH(startHour)}"></div>
             <div class="col-6"><label class="form-label" for="qEnd">終了</label><input type="time" id="qEnd" class="form-control" value="${fmtH(endHour)}"></div>
           </div>
           <div class="small text-secondary mt-2">時間を調整して「保存」を押してください。上限人数を超える場合は自動調整します。</div>`,
          async (w2, close) => {
            const st = w2.querySelector('#qStart').value;
            const en = w2.querySelector('#qEnd').value;
            if (!st || !en) { toast('時間を入力してください', 'error'); return; }
            try {
              const r = await api('/shop/shifts', { method: 'POST', body: JSON.stringify({
                staff_id: +staffId,
                start_datetime: `${date}T${st}:00`,
                end_datetime: `${date}T${en}:00`,
                auto_adjust: true,
              })});
              close();
              if (r.adjustments && r.adjustments.length) {
                toast(`追加しました（${r.adjustments.length}件自動調整）`, 'success');
              } else {
                toast('追加しました', 'success');
              }
              // タイムラインモーダルを閉じて再描画
              w.remove();
              // シフトを再取得して再描画
              const sd = await api(`/shop/shifts?start=${date}&end=${date}`);
              openDayTimeline(date, sd.shifts, editable, onChange);
            } catch (err) { toast(err.message, 'error'); }
          });
        addW.querySelector('[data-save]').textContent = '保存';
      });
    });
  }
  // 赤い不足バーをクリック → スタッフを選んで配置（1名ずつ）
  if (editable) {
    w.querySelectorAll('.tl-gap-bar').forEach((bar) => {
      bar?.addEventListener('click', async (e) => {
        e.stopPropagation();
        const startH = +bar.dataset.start;
        const endH = +bar.dataset.end;
        const gap = +bar.dataset.gap;
        const fmtH = (h) => `${String(h).padStart(2, '0')}:00`;
        buzz(10);
        // スタッフリストを取得
        let opts = '';
        try {
          const sd = await api('/shop/staffs');
          const active = (sd.staffs || []).filter((s) => !s.is_resigned);
          opts = active.map((s) => `<option value="${s.id}">${esc(s.name)}（${s.role === 'employee' ? '社員' : 'バイト'}）</option>`).join('');
        } catch (err) { toast('スタッフ一覧の取得に失敗', 'error'); return; }
        const addW = openModal(`<i class="bi bi-person-plus"></i> 不足枠に配置 — ${date} ${fmtH(startH)}〜${fmtH(endH)}`,
          `<div class="alert alert-warning py-2 mb-3"><i class="bi bi-exclamation-triangle"></i> この時間帯は<strong>${gap}名</strong>不足中。1名ずつ追加できます。</div>
           <label class="form-label" for="gapStaff">スタッフを選択</label>
           <select id="gapStaff" class="form-select mb-2">${opts}</select>
           <div class="row">
             <div class="col-6"><label class="form-label" for="gapStart">開始</label><input type="time" id="gapStart" class="form-control" value="${fmtH(startH)}"></div>
             <div class="col-6"><label class="form-label" for="gapEnd">終了</label><input type="time" id="gapEnd" class="form-control" value="${fmtH(endH)}"></div>
           </div>
           <div class="small text-secondary mt-2">残り${gap - 1}名の不足がある場合は、追加後に再度クリックしてください。</div>`,
          async (w2, close) => {
            const staffId = +w2.querySelector('#gapStaff').value;
            const st = w2.querySelector('#gapStart').value;
            const en = w2.querySelector('#gapEnd').value;
            try {
              const r = await api('/shop/shifts', { method: 'POST', body: JSON.stringify({
                staff_id: staffId, start_datetime: `${date}T${st}:00`, end_datetime: `${date}T${en}:00`, auto_adjust: true,
              })});
              close();
              if (r.adjustments && r.adjustments.length) {
                toast(`配置しました（${r.adjustments.length}件自動調整）`, 'success');
              } else {
                toast('配置しました', 'success');
              }
              w.remove();
              const sd2 = await api(`/shop/shifts?start=${date}&end=${date}`);
              openDayTimeline(date, sd2.shifts, editable, onChange);
            } catch (err) { toast(err.message, 'error'); }
          });
        addW.querySelector('[data-save]').textContent = '配置';
      });
    });
  }
  return w;
}

function showEditModal(s) {
  if (!s) { toast('シフト情報が取得できません', 'error'); return; }
  const toLocal = (iso) => (iso || '').slice(0, 16);
  const w = openModal(`<i class="bi bi-pencil-square"></i> シフト編集${s.staff_name ? ' — ' + esc(s.staff_name) : ''}`,
    `<label class="form-label" for="mStart">開始</label><input type="datetime-local"  id="mStart" class="form-control mb-2" value="${toLocal(s.start_datetime)}">
     <label class="form-label" for="mEnd">終了</label><input type="datetime-local"  id="mEnd" class="form-control mb-3" value="${toLocal(s.end_datetime)}">
     <label class="form-label" for="mStatus">ステータス</label><select id="mStatus" class="form-select mb-3">
       <option value="confirmed" ${s.status === 'confirmed' ? 'selected' : ''}>確定</option>
       <option value="modifying" ${s.status === 'modifying' ? 'selected' : ''}>調整中</option>
       <option value="requested" ${s.status === 'requested' ? 'selected' : ''}>調整待ち</option></select>
     <button id="mDelete" class="btn btn-outline-danger w-full"><i class="bi bi-trash"></i> 削除</button>`,
    async (w2, close) => {
      const payload = {
        staff_id: s.staff_id,
        start_datetime: w2.querySelector('#mStart').value + ':00',
        end_datetime: w2.querySelector('#mEnd').value + ':00',
        status: w2.querySelector('#mStatus').value,
        // 保存時に常にauto_adjust=trueで送信（1往復で完了）。
        // cap内なら調整なし、cap超過/同日重複なら自動調整（社員優先短縮/統合）。
        auto_adjust: true,
      };
      try {
        const r = await api(`/shop/shifts/${s.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        close();
        if (r.adjustments && r.adjustments.length) {
          toast(`保存しました（${r.adjustments.length}件自動調整）`, 'success');
          r.adjustments.forEach((a, i) => setTimeout(() => toast(a.message, 'info'), (i + 1) * 800));
        } else {
          toast('保存しました', 'success');
        }
        navigateTo('shifts');
      } catch (e) {
        toast(e.message, 'error');
      }
    });
  if (!w) return;
  // 予防的 null チェック（モーダル生成失敗や #mDelete 欠落でアプリ全体が落とさないよう保護）
  w.querySelector('#mDelete')?.addEventListener('click', async () => {
    if (!confirm('削除しますか？')) return;
    try { await api(`/shop/shifts/${s.id}`, { method: 'DELETE' }); w.remove(); toast('削除しました', 'success'); navigateTo('shifts'); } catch (e) { toast(e.message, 'error'); }
  });
}

/* ============================================================
   Change Requests (modal)
   ============================================================ */
async function openChangeRequests() {
  setLoading(true);
  try {
    const d = await api('/shop/change-requests');
    const pend = d.change_requests.filter((r) => r.status === 'pending');
    const done = d.change_requests.filter((r) => r.status !== 'pending');
    const typeName = { change: '時間変更', cancel: '休み', add: '追加' };
    const row = (r) => `<div class="list-row"><div>
      ${badge(typeName[r.request_type], r.request_type === 'cancel' ? 'warning' : 'info')}
      <strong>${esc(r.staff_name)}</strong>
      <div class="small text-secondary">${r.desired_start ? esc(r.desired_start.slice(5, 16)) + '〜' + esc((r.desired_end || '').slice(11, 16)) : '－'} ${r.reason ? '・' + esc(r.reason) : ''}</div>
      ${badge(r.status === 'approved' ? '承認済' : r.status === 'rejected' ? '却下' : '承認待ち', r.status === 'approved' ? 'success' : r.status === 'rejected' ? 'warning' : 'muted')}
      </div>${r.status === 'pending' ? `<div class="flex gap-1"><button class="btn btn-sm btn-primary" data-app="${r.id}">承認</button><button class="btn btn-sm btn-light" data-rej="${r.id}">却下</button></div>` : ''}</div>`;
    const w = openModal(`<i class="bi bi-clipboard-check"></i> 変更申請 (${pend.length}件保留)`,
      (pend.length ? '<div class="small text-secondary mb-2">承認待ち</div>' + pend.map(row).join('') : '<div class="small text-secondary">承認待ちの申請はありません</div>') +
      (done.length ? '<div class="small text-secondary mt-3 mb-2">処理済</div>' + done.slice(0, 8).map(row).join('') : ''), null);
    w.querySelectorAll('[data-app]').forEach((b) => b?.addEventListener('click', async () => {
      if (!confirm('承認してシフトへ反映しますか？')) return;
      await api(`/shop/change-requests/${b.dataset.app}`, { method: 'PUT', body: JSON.stringify({ action: 'approve' }) });
      w.remove(); toast('承認しました', 'success'); openChangeRequests(); refreshNotifBadge();
    }));
    w.querySelectorAll('[data-rej]').forEach((b) => b?.addEventListener('click', async () => {
      await api(`/shop/change-requests/${b.dataset.rej}`, { method: 'PUT', body: JSON.stringify({ action: 'reject' }) });
      w.remove(); toast('却下しました', 'info'); openChangeRequests();
    }));
  } catch (e) { toast(e.message, 'error'); }
  finally { setLoading(false); }
}

async function loadShortage(box, start, end) {
  if (!start || !end) { box.innerHTML = '<div class="text-muted small">期間を指定してください</div>'; return; }
  try {
    // 時間帯単位の不足を計算（「夜(17:00)」のような区分単位ではなく）
    await ensureBusinessHours();
    const sd = await api(`/shop/shifts?start=${start}&end=${end}`);
    const allShifts = sd.shifts || [];
    const byDay = {};
    allShifts.forEach((s) => {
      const day = s.start_datetime.slice(0, 10);
      (byDay[day] = byDay[day] || []).push(s);
    });
    const chips = [];
    Object.keys(byDay).sort().forEach((day) => {
      const gaps = _computeHourlyGaps(byDay[day], day);
      if (!gaps.length) return;
      const merged = _mergeHourlyGaps(gaps);
      merged.forEach((g) => {
        const sH = String(g.start).padStart(2, '0');
        const eH = String(g.end).padStart(2, '0');
        chips.push(`<span class="shortage-chip"><i class="bi bi-exclamation-triangle"></i> ${day.slice(5)} ${sH}:00〜${eH}:00 <strong>あと${g.gap}名</strong></span>`);
      });
    });
    // シフトが無い日は全時間帯不足として表示
    if (appState.patterns) {
      const days = [];
      const cur = new Date(start + 'T00:00:00');
      const endD = new Date(end + 'T00:00:00');
      while (cur <= endD) {
        const ds = cur.toISOString().slice(0, 10);
        if (!byDay[ds]) days.push(ds);
        cur.setDate(cur.getDate() + 1);
      }
      days.forEach((day) => {
        const gaps = _computeHourlyGaps([], day);
        const merged = _mergeHourlyGaps(gaps);
        merged.forEach((g) => {
          const sH = String(g.start).padStart(2, '0');
          const eH = String(g.end).padStart(2, '0');
          chips.push(`<span class="shortage-chip"><i class="bi bi-exclamation-triangle"></i> ${day.slice(5)} ${sH}:00〜${eH}:00 <strong>あと${g.gap}名</strong></span>`);
        });
      });
    }
    if (!chips.length) {
      box.innerHTML = '<div class="shortage-none"><i class="bi bi-check-circle"></i> 不足なし — 全時間帯充足</div>';
    } else {
      box.innerHTML = chips.join('');
    }
  } catch (e) { box.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`; }
}

/* ============================================================
   SCREENS
   ============================================================ */
const SCREENS = {};

/* ---------- Dashboard ---------- */
SCREENS.dashboard = async function (el) {
  el.innerHTML = pageHead('ダッシュボード', 'bi-grid-1x2', currentUser.shop_name) +
    `<div class="kpi-grid" id="kpiGrid"><div class="skeleton" style="height:110px;border-radius:16px"></div><div class="skeleton" style="height:110px;border-radius:16px"></div><div class="skeleton" style="height:110px;border-radius:16px"></div><div class="skeleton" style="height:110px;border-radius:16px"></div></div>
    <div class="dash-grid">
      <div id="dashLeft"></div>
      <div id="dashRight"></div>
    </div>`;

  try {
    const d = await api('/shop/dashboard');
    // KPIs
    document.getElementById('kpiGrid').innerHTML =
      kpiCard('bi-people-fill', '稼働スタッフ', d.staff_count, `社員${d.employee_count} / バイト${d.part_time_count}`, 'indigo') +
      kpiCard('bi-calendar-check', '今日の出勤', d.today_attendance + '名', d.today_shortage ? `${d.today_shortage}枠不足` : '充足', d.today_shortage ? 'amber' : 'green') +
      kpiCard('bi-cash-stack', '今月の人件費', '¥' + (d.month_cost / 1000).toFixed(0) + 'K', `${d.month_hours}h`, 'indigo') +
      kpiCard('bi-inbox', '承認待ち', d.pending_approvals + d.pending_requests, '申請・希望', (d.pending_approvals + d.pending_requests) > 0 ? 'red' : 'green');

    // Left: charts
    const leftBox = document.getElementById('dashLeft');
    leftBox.innerHTML =
      card(sectionTitle('bi-bar-chart', '今日の時間帯別人数') + `<div class="chart-box"><canvas id="todayChart"></canvas></div>`) +
      card(sectionTitle('bi-graph-up', '人件費推移（直近30日）') + `<div class="chart-box"><canvas id="costChart"></canvas></div>`);

    // Today hourly chart
    const todayHours = d.today_hourly.length ? d.today_hourly : [];
    const hours = todayHours.map((h) => h.hour + ':00');
    const counts = todayHours.map((h) => h.count);
    chartInstances.today = new Chart(document.getElementById('todayChart'), {
      type: 'bar',
      data: { labels: hours.length ? hours : ['データなし'], datasets: [{ label: '人数', data: counts.length ? counts : [0], backgroundColor: 'rgba(99,102,241,.6)', borderRadius: 6 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { color: '#64748B' }, grid: { color: 'rgba(148,163,184,.1)' } }, x: { ticks: { color: '#64748B' }, grid: { display: false } } } }
    });

    // Cost chart
    const costData = d.daily_cost_series || [];
    chartInstances.cost = new Chart(document.getElementById('costChart'), {
      type: 'line',
      data: { labels: costData.map((c) => c.date.slice(5)), datasets: [{ label: '人件費(円)', data: costData.map((c) => c.cost), borderColor: '#6366F1', backgroundColor: 'rgba(99,102,241,.1)', fill: true, tension: .3, pointRadius: 0 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { color: '#64748B', callback: (v) => '¥' + (v / 1000) + 'K' }, grid: { color: 'rgba(148,163,184,.1)' } }, x: { ticks: { color: '#64748B', maxTicksLimit: 8 }, grid: { display: false } } } }
    });

    // Right: AI suggestion + notifications + quick actions
    const rightBox = document.getElementById('dashRight');
    let aiAdvice = 'シフトデータを分析中...';
    try { const rev = await api('/shop/ai/review', { method: 'POST', body: JSON.stringify({ start: todayStr().slice(0, 8) + '01', end: todayStr().slice(0, 8) + '31' }) }); aiAdvice = rev.advice; } catch {}
    rightBox.innerHTML =
      card(sectionTitle('bi-stars', 'AIからの提案', badge('AI', 'ai')) + `<div class="reason-text" style="font-size:.88rem;line-height:1.7;white-space:pre-wrap">${esc(aiAdvice)}</div>`) +
      card(sectionTitle('bi-lightning', 'クイック操作') +
        `<button class="btn btn-ai w-full mb-2" id="qGen"><i class="bi bi-stars"></i> AIでシフト作成</button>
         <button class="btn btn-light w-full mb-2" id="qShifts"><i class="bi bi-calendar3"></i> シフト画面へ</button>
         <button class="btn btn-light w-full" id="qCreq"><i class="bi bi-clipboard-check"></i> 変更申請を確認</button>`) +
      card(sectionTitle('bi-bell', '最近の通知') + `<div id="dashNotif"><div class="text-secondary small">読み込み中...</div></div>`);

    document.getElementById('qGen')?.addEventListener('click', () => navigateTo('aiGenerate'));
    document.getElementById('qShifts')?.addEventListener('click', () => navigateTo('shifts'));
    document.getElementById('qCreq')?.addEventListener('click', () => openChangeRequests());

    // Notifications
    try {
      const n = await api('/shop/notifications');
      document.getElementById('dashNotif').innerHTML = n.notifications.length ? n.notifications.slice(0, 4).map((x) => `<div class="notif-item ${x.is_read ? '' : 'unread'}"><div class="nt-title">${esc(x.title)}</div><div class="nt-body">${esc(x.body || '')}</div></div>`).join('') : '<div class="small text-secondary">通知はありません</div>';
    } catch {}
  } catch (e) {
    el.innerHTML = card(`<div class="text-danger">${esc(e.message)}</div>`);
  }
};

/* ---------- AI Shift Generator + Chat (中心機能) ---------- */
let aiTab = 'generate';
SCREENS.aiGenerate = async function (el) {
  const p = appState.period || await ensurePeriod();
  el.innerHTML = pageHead('AI', 'bi-stars', 'シフト自動作成とAIアシスタント') +
    `<div class="tabs no-print">
      <button class="tab ${aiTab==='generate'?'active':''}" data-tab="generate"><i class="bi bi-magic"></i> シフト作成</button>
      <button class="tab ${aiTab==='chat'?'active':''}" data-tab="chat"><i class="bi bi-chat-dots"></i> AIアシスタント</button>
    </div>
    <div id="aiTabBody"></div>`;
  const renderAiTab = () => {
    el.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === aiTab));
    if (aiTab === 'generate') renderGenerateTab(el.querySelector('#aiTabBody'), p);
    else renderShopChatTab(el.querySelector('#aiTabBody'));
  };
  el.querySelectorAll('.tab').forEach((t) => t?.addEventListener('click', () => { aiTab = t.dataset.tab; renderAiTab(); }));
  renderAiTab();
};

function renderGenerateTab(body, p) {
  body.innerHTML =
    card(sectionTitle('bi-calendar-range', '作成期間') +
      `<div class="row">
        <div class="col-6"><label class="form-label" for="genStart">開始日</label><input type="date"  id="genStart" class="form-control" value="${p.start_date}"></div>
        <div class="col-6"><label class="form-label" for="genEnd">終了日</label><input type="date"  id="genEnd" class="form-control" value="${p.end_date}"></div>
      </div>`) +
    `<div id="genConditions"></div>` +
    card(`<div class="text-center" style="padding:8px 0">
        <button class="btn btn-ai btn-lg" style="min-width:280px;font-size:1.1rem" id="genBtn">
          <i class="bi bi-stars"></i> AIでシフト作成
        </button>
        <div class="small text-muted mt-2">希望休・勤務条件・必要人数を考慮して最適化します</div>
      </div>`) +
    `<div id="genResult"></div>`;

  // Load conditions summary
  api('/shop/staffs').then(async (staffsD) => {
    const [patsD, settingsD] = await Promise.all([api('/shop/patterns'), api('/shop/settings')]);
    const active = (staffsD.staffs || []).filter((s) => !s.is_resigned);
    const s = settingsD.settings || {};
    document.getElementById('genConditions').innerHTML =
      card(sectionTitle('bi-clipboard-data', 'AIに考慮させる条件') +
        `<div class="gen-condition"><span class="gen-condition-label">稼働スタッフ</span><span class="gen-condition-value">${active.length}名</span></div>
         <div class="gen-condition"><span class="gen-condition-label">　社員 / アルバイト</span><span class="gen-condition-value">${active.filter((x) => x.role === 'employee').length}名 / ${active.filter((x) => x.role === 'part_time').length}名</span></div>
         <div class="gen-condition"><span class="gen-condition-label">1日最低勤務時間</span><span class="gen-condition-value">${s.min_daily_hours || 4}時間</span></div>
         <div class="gen-condition"><span class="gen-condition-label">最大連勤（推奨）</span><span class="gen-condition-value">${s.max_consecutive_days || 6}日</span></div>
         <div class="gen-condition"><span class="gen-condition-label">深夜割増率</span><span class="gen-condition-value">${s.night_premium_rate || 1.25}倍</span></div>
         <div class="gen-condition"><span class="gen-condition-label">営業時間</span><span class="gen-condition-value">${esc(s.business_hours || '未設定')}</span></div>
         <div class="gen-condition"><span class="gen-condition-label">シフト時間帯</span><span class="gen-condition-value">${(patsD.patterns || []).length}枠</span></div>`);
  }).catch(() => {});

  document.getElementById('genBtn')?.addEventListener('click', () => runGenerate());
}

/* ---------- 店舗用AIチャット画面 ---------- */
function renderShopChatTab(body) {
  if (!window._shopChat) window._shopChat = [];
  body.innerHTML = card(
    `<div class="chat-card">
      <div class="chat-messages" id="shopChatMsgs"></div>
      <div class="chat-suggestions" id="shopChatSug"></div>
      <div class="chat-input-row">
        <textarea class="form-control chat-input" id="shopChatInput" rows="1" placeholder="シフトについて質問してください..."></textarea>
        <button class="btn btn-ai chat-send" id="shopChatSend"><i class="bi bi-send-fill"></i></button>
      </div>
    </div>`);
  const renderMsgs = () => {
    const box = document.getElementById('shopChatMsgs');
    if (!window._shopChat.length) {
      window._shopChat.push({ role: 'assistant', content: `${currentUser.shop_name}のシフト管理AIアシスタントです。\n不足状況・人件費・連勤・スタッフ配置など、何でもお気軽にどうぞ。` });
    }
    box.innerHTML = window._shopChat.map((m) => {
      if (m.content === '__thinking__') {
        return `<div class="chat-bubble chat-bubble-ai"><div class="chat-ai-avatar"><i class="bi bi-stars"></i></div><div class="chat-ai-text"><div class="ai-thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div></div></div>`;
      }
      return m.role === 'user'
        ? `<div class="chat-bubble chat-bubble-user">${esc(m.content)}</div>`
        : `<div class="chat-bubble chat-bubble-ai"><div class="chat-ai-avatar"><i class="bi bi-stars"></i></div><div class="chat-ai-text">${esc(m.content)}</div></div>`;
    }).join('');
    box.scrollTop = box.scrollHeight;
  };
  const renderSug = (suggestions) => {
    const items = suggestions || ['今月のシフト状況は？', '不足している時間帯は？', '人件費はいくら？', '連勤の偏りは？'];
    document.getElementById('shopChatSug').innerHTML = items.map((s) => `<button class="chat-suggest-chip" data-sug="${esc(s)}">${esc(s)}</button>`).join('');
    document.querySelectorAll('#shopChatSug [data-sug]').forEach((b) => b?.addEventListener('click', () => { document.getElementById('shopChatInput').value = b.dataset.sug; sendShopChat(); }));
  };
  async function sendShopChat() {
    const inp = document.getElementById('shopChatInput');
    const msg = (inp.value || '').trim(); if (!msg) return;
    inp.value = ''; inp.style.height = 'auto';
    window._shopChat.push({ role: 'user', content: msg });
    window._shopChat.push({ role: 'assistant', content: '__thinking__' });
    renderMsgs();
    document.getElementById('shopChatSug').innerHTML = '';
    try {
      const history = window._shopChat.filter((h) => h.content !== '__thinking__').slice(-11, -1);
      const d = await api('/shop/ai/chat', { method: 'POST', body: JSON.stringify({ message: msg, history }) });
      window._shopChat[window._shopChat.length - 1] = { role: 'assistant', content: d.reply };
      renderMsgs();
      if (d.suggestions && d.suggestions.length) renderSug(d.suggestions);
    } catch (e) {
      window._shopChat[window._shopChat.length - 1] = { role: 'assistant', content: 'エラーが発生しました。もう一度お試しください。' };
      renderMsgs();
    }
  }
  const input = document.getElementById('shopChatInput');
  input?.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendShopChat(); } });
  input?.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 120) + 'px'; });
  document.getElementById('shopChatSend')?.addEventListener('click', sendShopChat);
  renderMsgs();
  renderSug();
}

async function runGenerate() {
  const start = document.getElementById('genStart').value;
  const end = document.getElementById('genEnd').value;
  if (!start || !end) { toast('期間を指定してください', 'error'); return; }
  const resultBox = document.getElementById('genResult');

  // Step animation
  const steps = [
    { title: 'スタッフ希望を分析中', desc: '希望休・NG曜日・希望時間帯を確認', icon: 'bi-people' },
    { title: '固定シフトを配置', desc: '契約済みの固定勤務を最優先で配置', icon: 'bi-calendar-check' },
    { title: '希望シフトを組み込み', desc: '上限人数を守りながら希望を反映', icon: 'bi-pencil-square' },
    { title: '社員で不足を補填', desc: '空き時間帯を社員が柔軟にカバー', icon: 'bi-robot' },
    { title: '労務条件を最終チェック', desc: '連勤・月間上限・休憩を検証', icon: 'bi-shield-check' },
  ];
  resultBox.innerHTML = card(sectionTitle('bi-cpu', 'AI生成中') +
    `<div class="gen-steps" id="genSteps">${steps.map((s, i) => `
      <div class="gen-step" data-step="${i}" style="animation-delay:${i * 100}ms">
        <div class="gen-step-icon"><i class="bi ${s.icon}"></i></div>
        <div class="gen-step-text"><div class="gen-step-title">${s.title}</div><div class="gen-step-desc">${s.desc}</div></div>
      </div>`).join('')}</div>
      <div class="progress-bar mt-3"><div class="progress-bar-fill" id="genProgress" style="width:0%"></div></div>`);
  // Animate steps
  for (let i = 0; i < steps.length; i++) {
    await new Promise((r) => setTimeout(r, 350));
    const stepEl = document.querySelector(`.gen-step[data-step="${i}"]`);
    if (stepEl) stepEl.classList.add('active');
    document.getElementById('genProgress').style.width = `${((i + 1) / steps.length) * 80}%`;
  }

  // Run actual generation (dry run)
  try {
    const prev = await api('/shop/shifts/auto', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end, dry_run: true }) });
    document.getElementById('genProgress').style.width = '100%';
    // Mark all steps done
    document.querySelectorAll('.gen-step').forEach((s) => { s.classList.remove('active'); s.classList.add('done'); });

    // Show preview + explanations
    const names = await api('/shop/staffs').then((sd) => { const m = {}; sd.staffs.forEach((s) => m[s.id] = s.name); return m; });
    const mins = prev.minutes_by_staff || {};
    const topList = Object.entries(mins).sort((a, b) => b[1] - a[1]).slice(0, 10)
      .map(([id, m]) => `<div class="preview-pill">${esc(names[id] || ('#' + id))}<br><b class="num">${(m / 60).toFixed(1)}h</b></div>`).join('');

    const explanations = (prev.explanations || []).map((e) => `
      <div class="explanation-item">
        <div class="ei-icon ${e.type}"><i class="bi ${e.icon}"></i></div>
        <div class="ei-text"><strong>${esc(e.title)}</strong><br><span class="text-secondary">${esc(e.detail)}</span></div>
      </div>`).join('');

    resultBox.innerHTML = card(
      sectionTitle('bi-eye', 'プレビュー', badge(`${prev.confirmed_count}件確定`, 'success')) +
      `<div class="kpi-grid mb-3" style="grid-template-columns:repeat(3,1fr)">
        <div class="kpi-card kpi-green"><div class="kpi-label">確定予定</div><div class="kpi-value num">${prev.confirmed_count}</div></div>
        <div class="kpi-card kpi-amber"><div class="kpi-label">調整待ち</div><div class="kpi-value num">${prev.pending_count}</div></div>
        <div class="kpi-card kpi-red"><div class="kpi-label">不足枠</div><div class="kpi-value num">${(prev.shortage || []).length}</div></div>
      </div>`) +
    card(sectionTitle('bi-lightbulb', 'AIの判断理由', badge('Explainable AI', 'ai')) +
      `<div class="explanation-list">${explanations}</div>`) +
    card(sectionTitle('bi-people', 'スタッフ別 想定労働時間') + `<div class="preview-grid">${topList || '<span class="small text-secondary">なし</span>'}</div>`) +
    card(
      `<div class="text-center">
        <button class="btn btn-primary btn-lg" style="min-width:260px" id="confirmGen"><i class="bi bi-check-lg"></i> この内容で確定</button>
        <div class="small text-secondary mt-2">※確定すると期間内の「確定シフト」を上書きします</div>
      </div>`);

    document.getElementById('confirmGen')?.addEventListener('click', async () => {
      setLoading(true, 'シフトを確定中...');
      try {
        const d = await api('/shop/shifts/auto', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end }) });
        setLoading(false);
        toast(`${d.confirmed_count}件のシフトを確定しました`, 'success');
        navigateTo('shifts');
      } catch (e) { setLoading(false); toast(e.message, 'error'); }
    });
  } catch (e) {
    resultBox.innerHTML = card(`<div class="text-danger">${esc(e.message)}</div>`);
  }
}

/* ---------- Shifts (Calendar + Summary) ---------- */
SCREENS.shifts = function (el) {
  const p = appState.period || { start_date: '', end_date: '' };
  el.innerHTML = pageHead('シフト管理', 'bi-calendar3') +
    card(sectionTitle('bi-magic', '自動作成・手動操作') +
      `<div class="row mb-2">
        <div class="col-6 col-sm-5"><label class="form-label" for="sStart">開始</label><input type="date" id="sStart" class="form-control" value="${p.start_date}"></div>
        <div class="col-6 col-sm-5"><label class="form-label" for="sEnd">終了</label><input type="date" id="sEnd" class="form-control" value="${p.end_date}"></div>
        <div class="col-12 col-sm-2 mt-2 mt-sm-0"><label class="form-label d-none d-sm-block">&nbsp;</label><button class="btn btn-ai w-full" id="autoGen" title="AI自動作成"><i class="bi bi-stars"></i> AI生成</button></div>
      </div>
      <div class="flex gap-2 flex-wrap">
        <button class="btn btn-light flex-grow" id="addShiftBtn"><i class="bi bi-plus-lg"></i> 手動追加</button>
        <button class="btn btn-light flex-grow" id="copyBtn"><i class="bi bi-files"></i> コピー</button>
        <button class="btn btn-light" id="printBtn"><i class="bi bi-printer"></i></button>
        <button class="btn btn-ai" id="autoConfirmBtn" title="調整待ち（requested）のシフトを自動調整で一括確定"><i class="bi bi-check2-all"></i> 一括確定</button>
      </div>
      <div id="genResult" class="mt-2"></div>`) +
    card(sectionTitle('bi-calendar3', '確定シフトカレンダー') + `<div id="calMount"></div>`) +
    card(sectionTitle('bi-exclamation-octagon', '不足コマ') + `<div id="shortageBox"><div class="text-secondary small">読み込み中...</div></div><button class="btn btn-light w-full mt-2" id="openCreq2"><i class="bi bi-clipboard-check"></i> 変更申請を承認/却下</button>`) +
    card(sectionTitle('bi-bar-chart', '労働時間・給与集計') + `<div id="summaryBox"><div class="text-secondary small">読み込み中...</div></div>`);

  const sStartEl = document.getElementById('sStart');
  const sEndEl = document.getElementById('sEnd');
  const cur = () => ({ start: sStartEl ? sStartEl.value : '', end: sEndEl ? sEndEl.value : '' });
  async function loadSummary() {
    const { start, end } = cur();
    const box = document.getElementById('summaryBox');
    if (!start || !end) { box.innerHTML = '<div class="text-muted small">期間を指定してください</div>'; return; }
    try {
      const d = await api(`/shop/summary?start=${start}&end=${end}`);
      if (!d.staff.length) { box.innerHTML = '<div class="text-muted small">確定シフトがありません</div>'; return; }
      box.innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>氏名</th><th>日</th><th class="t-num">確定</th><th class="t-num">見込</th><th class="t-num">深夜</th><th class="t-num">給与</th></tr></thead>
        <tbody>${d.staff.map((s) => `<tr><td><div class="staff-cell"><span class="staff-name">${esc(s.name)}</span><span class="staff-sub">${s.role === 'employee' ? '社員' : 'バイト'}</span></div></td><td>${s.days}</td><td class="t-num num">${s.confirmed_hours}h</td><td class="t-num num">${s.projected_hours}h</td><td class="t-num num">${s.night_hours}h</td><td class="t-num num">${yen(s.pay)}</td></tr>`).join('')}
        <tr style="font-weight:800;color:var(--indigo-l)"><td>合計</td><td></td><td class="t-num num">${d.total_hours}h</td><td class="t-num num">${d.total_projected_hours}h</td><td></td><td class="t-num num">${yen(d.total_pay)}</td></tr>
        </tbody></table></div>`;
    } catch (e) { box.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`; }
  }
  async function refreshShortage() {
    const { start, end } = cur();
    const box = document.getElementById('shortageBox');
    if (!start || !end) { box.innerHTML = '<div class="text-muted small">期間を指定してください</div>'; return; }
    await loadShortage(box, start, end);
  }
  loadSummary();
  refreshShortage();

  // AI生成ボタン: 入力期間で直接プレビュー→確定（遷移しない）
  // ※ 各ボタンは ?. で保護（HTML描画不良時にアプリ全体が停止するのを防ぐ）
  document.getElementById('autoGen')?.addEventListener('click', () => runShiftGenInline(cur, loadSummary, refreshShortage));
  document.getElementById('addShiftBtn')?.addEventListener('click', () => openAddShiftModal());
  document.getElementById('copyBtn')?.addEventListener('click', () => {
    api('/shop/periods').then((d) => {
      const past = d.periods.filter((p) => p.end_date < cur().start).sort((a, b) => b.end_date.localeCompare(a.end_date))[0];
      const defFrom = past ? past.start_date : '', defTo = past ? past.end_date : '';
      const m = openModal('<i class="bi bi-files"></i> 前回シフトをコピー',
        `<p class="small text-muted">過去期間の確定シフトを、現在の期間へ日付をずらして複製します。</p>
         <div class="row"><div class="col-6"><label class="form-label" for="cpFrom">コピー元 開始</label><input type="date"  id="cpFrom" class="form-control" value="${defFrom}"></div>
         <div class="col-6"><label class="form-label" for="cpFromEnd">コピー元 終了</label><input type="date"  id="cpFromEnd" class="form-control" value="${defTo}"></div></div>
         <label class="form-label mt-2">貼り付け先 開始</label><input type="date" id="cpTo" class="form-control" value="${cur().start}">
         <div class="small text-muted mt-1" id="cpPreview"></div>`,
        async (w, close) => {
          try {
            const r = await api('/shop/shifts/copy', { method: 'POST', body: JSON.stringify({ from_start: w.querySelector('#cpFrom').value, from_end: w.querySelector('#cpFromEnd').value, to_start: w.querySelector('#cpTo').value }) });
            close(); toast(`${r.copied}件コピーしました`, 'success'); navigateTo('shifts');
          } catch (e) { toast(e.message, 'error'); }
        });
      // コピー先終了日の自動計算プレビュー
      const updatePreview = () => {
        const fs = m.querySelector('#cpFrom').value, fe = m.querySelector('#cpFromEnd').value, ts = m.querySelector('#cpTo').value;
        if (fs && fe && ts) {
          const days = (new Date(fe) - new Date(fs)) / 86400000;
          const te = new Date(new Date(ts).getTime() + days * 86400000).toISOString().slice(0, 10);
          m.querySelector('#cpPreview').textContent = `貼り付け先終了日（自動）: ${te}`;
        }
      };
      ['#cpFrom', '#cpFromEnd', '#cpTo'].forEach((id) => m.querySelector(id)?.addEventListener('change', updatePreview));
      updatePreview();
    });
  });
  document.getElementById('printBtn')?.addEventListener('click', () => {
    const { start, end } = cur();
    openPrintView(start, end);
  });
  document.getElementById('openCreq2')?.addEventListener('click', () => openChangeRequests());

  // 調整待ち（requested）を一括で自動調整して確定
  document.getElementById('autoConfirmBtn')?.addEventListener('click', async () => {
    const { start, end } = cur();
    if (!start || !end) { toast('期間を指定してください', 'error'); return; }
    if (!confirm(`${start} 〜 ${end} の調整待ち（requested）シフトを自動調整で一括確定しますか？\n・同日重複の希望は既存シフトと統合\n・上限人数超過は他スタッフ（社員優先）のシフトを短縮`)) return;
    setLoading(true, '自動調整で確定中...');
    try {
      const r = await api('/shop/shifts/auto-confirm', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end }) });
      setLoading(false);
      const msg = `${r.total}件中: 確定${r.confirmed}件 / 統合${r.merged}件 / スキップ${r.skipped}件`;
      toast(msg, r.skipped > 0 ? 'info' : 'success');
      // 調整内容の詳細を順次表示
      if (r.adjustments && r.adjustments.length) {
        r.adjustments.slice(0, 5).forEach((a, i) => setTimeout(() => toast(a.message, 'info'), (i + 1) * 700));
      }
      loadSummary(); refreshShortage();
      if (window._shiftCalCtrl) window._shiftCalCtrl.refresh();
      refreshNotifBadge();
    } catch (e) { setLoading(false); toast('一括確定に失敗: ' + e.message, 'error'); }
  });

  const calCtrl = createCalendar(document.getElementById('calMount'), {
    initial: p.start_date,
    loader: (from, to) => api(`/shop/shifts?start=${from}&end=${to}`).then((d) => d.shifts),
    editable: true,
  });
  window._shiftCalCtrl = calCtrl;
};

/* AI生成: シフト画面内で直接プレビュー→確定（遷移しない） */
async function runShiftGenInline(cur, loadSummary, refreshShortage) {
  const { start, end } = cur();
  if (!start || !end) { toast('期間を指定してください', 'error'); return; }
  setLoading(true, 'AI がシフトを生成中...');
  const genResult = document.getElementById('genResult');
  try {
    const prev = await api('/shop/shifts/auto', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end, dry_run: true }) });
    setLoading(false);
    const explanations = (prev.explanations || []).map((e) => `
      <div class="explanation-item">
        <div class="ei-icon ${e.type}"><i class="bi ${e.icon}"></i></div>
        <div class="ei-text"><strong>${esc(e.title)}</strong><br><span class="text-muted">${esc(e.detail)}</span></div>
      </div>`).join('');
    const warnBox = (prev.warnings && prev.warnings.length)
      ? `<div class="alert alert-warning py-2 mb-2"><i class="bi bi-exclamation-triangle"></i> ${prev.warnings.map((w) => esc(w.message)).join('<br>')}</div>` : '';
    const w = openModal(`<i class="bi bi-stars"></i> AI生成プレビュー（${start} 〜 ${end}）`,
      `${warnBox}
       <div class="row g-2 mb-3">
         <div class="col-4"><div class="kpi-card kpi-green" style="margin:0;padding:12px"><div class="kpi-label">確定予定</div><div class="kpi-value num">${prev.confirmed_count}</div></div></div>
         <div class="col-4"><div class="kpi-card kpi-amber" style="margin:0;padding:12px"><div class="kpi-label">調整待ち</div><div class="kpi-value num">${prev.pending_count}</div></div></div>
         <div class="col-4"><div class="kpi-card kpi-red" style="margin:0;padding:12px"><div class="kpi-label">不足枠</div><div class="kpi-value num">${(prev.shortage || []).length}</div></div></div>
       </div>
       ${explanations ? `<div class="small fw-bold text-muted mb-2"><i class="bi bi-lightbulb"></i> AIの判断理由</div><div class="explanation-list mb-3">${explanations}</div>` : ''}
       <div class="small text-muted">※確定すると期間内の「確定シフト」を上書きします。</div>`,
      async (w2, close) => {
        setLoading(true, 'シフトを確定中...');
        try {
          const d = await api('/shop/shifts/auto', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end }) });
          setLoading(false);
          close();
          toast(`${d.confirmed_count}件のシフトを確定しました`, 'success');
          // カレンダーを作成月へジャンプ
          try { const d0 = new Date(start + 'T00:00:00'); if (window._shiftCalCtrl) window._shiftCalCtrl.goToMonth(d0.getFullYear(), d0.getMonth()); } catch {}
          loadSummary(); refreshShortage(); refreshNotifBadge();
        } catch (e) { setLoading(false); toast(e.message, 'error'); }
      });
    w.querySelector('[data-save]').textContent = 'この内容で確定';
  } catch (e) { setLoading(false); genResult.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`; }
}

function openAddShiftModal() {
  const p = appState.period || { start_date: todayStr() };
  const defDate = p.start_date || todayStr();
  api('/shop/staffs').then((sd) => {
    const active = (sd.staffs || []).filter((s) => !s.is_resigned);
    const opts = active.map((s) => `<option value="${s.id}">${esc(s.name)}（${s.role === 'employee' ? '社員' : 'バイト'}）</option>`).join('');
    openModal('<i class="bi bi-plus-lg"></i> 手動シフト追加',
      `<label class="form-label" for="adStaff">スタッフ</label><select id="adStaff" class="form-select mb-2">${opts}</select>
       <div class="row">
         <div class="col-6"><label class="form-label" for="adStart">開始</label><input type="datetime-local"  id="adStart" class="form-control mb-2" value="${defDate}T09:00"></div>
         <div class="col-6"><label class="form-label" for="adEnd">終了</label><input type="datetime-local"  id="adEnd" class="form-control mb-2" value="${defDate}T18:00"></div>
       </div>
       <label class="form-label" for="adStatus">ステータス</label><select id="adStatus" class="form-select"><option value="confirmed">確定</option><option value="modifying">調整中</option></select>
       <div class="small text-muted mt-2">休憩は労基法で自動計算・必要人数を超える配置は警告します</div>`,
      async (w, close) => {
        const startVal = w.querySelector('#adStart').value;
        const endVal = w.querySelector('#adEnd').value;
        if (!startVal || !endVal) { toast('開始・終了を入力してください', 'error'); return; }
        const payload = { staff_id: +w.querySelector('#adStaff').value, start_datetime: startVal + ':00', end_datetime: endVal + ':00', status: w.querySelector('#adStatus').value };
        try {
          await api('/shop/shifts', { method: 'POST', body: JSON.stringify(payload) });
          close(); toast('追加しました', 'success'); navigateTo('shifts');
        } catch (e) {
          if (e.message.includes('必要人数') && confirm(e.message + '\n\nそれでも配置しますか？')) {
            try { await api('/shop/shifts', { method: 'POST', body: JSON.stringify({ ...payload, force: true }) }); close(); toast('追加しました', 'success'); navigateTo('shifts'); } catch (e2) { toast(e2.message, 'error'); }
          } else { toast(e.message, 'error'); }
        }
      });
  });
}

/* ---------- Staff Management ---------- */
SCREENS.staffs = async function (el) {
  el.innerHTML = pageHead('スタッフ管理', 'bi-people') +
    card(`<div class="flex justify-between items-center mb-3">${sectionTitle('bi-people', 'スタッフ一覧')}<button class="btn btn-primary btn-sm" id="addStaffBtn"><i class="bi bi-person-plus"></i> 追加</button></div><div id="staffList"></div>`);
  document.getElementById('addStaffBtn')?.addEventListener('click', () => showStaffForm());
  await loadStaffList();
};
async function loadStaffList() {
  try {
    const data = await api('/shop/staffs');
    const list = document.getElementById('staffList');
    if (!data.staffs.length) { list.innerHTML = emptyState('bi-people', 'スタッフがいません'); return; }
    list.innerHTML = data.staffs.map((s) => `
      <div class="list-row">
        <div class="flex items-center gap-2">
          <span class="dot ${s.role === 'employee' ? 'evening' : 'noon'}"></span>
          <div>
            <strong>${esc(s.name)}</strong> <span class="text-secondary">${esc(s.staff_code)}</span>${s.is_resigned ? badge('退職', 'warning') : ''}
            <div class="small text-secondary">${s.role === 'employee' ? '社員' : 'アルバイト'} ・ 時給${s.hourly_wage}円 ・ 月${s.min_hours_per_month}-${s.max_hours_per_month}h</div>
          </div>
        </div>
        <div class="flex gap-1">
          <button class="btn btn-sm btn-light" data-fix="${s.id}" data-name="${esc(s.name)}" title="固定シフト"><i class="bi bi-calendar-week"></i></button>
          <button class="btn btn-sm btn-light" data-edit="${s.id}"><i class="bi bi-pencil"></i></button>
        </div>
      </div>`).join('');
    list.querySelectorAll('[data-edit]').forEach((b) => b?.addEventListener('click', () => showStaffForm(data.staffs.find((x) => x.id == b.dataset.edit))));
    list.querySelectorAll('[data-fix]').forEach((b) => b?.addEventListener('click', () => showFixedShiftModal(+b.dataset.fix, b.dataset.name)));
  } catch (e) { document.getElementById('staffList').innerHTML = `<div class="text-danger">${esc(e.message)}</div>`; }
}
function showStaffForm(s) {
  const isEdit = !!s;
  openModal(`<i class="bi bi-person-plus"></i> ${isEdit ? 'スタッフ編集' : 'スタッフ追加'}`,
    `<div class="row">
      <div class="col-6"><label class="form-label" for="f_code">コード</label><input id="f_code" class="form-control" value="${s ? esc(s.staff_code) : ''}" ${isEdit ? 'disabled' : ''}></div>
      <div class="col-6"><label class="form-label" for="f_name">氏名</label><input id="f_name" class="form-control" value="${s ? esc(s.name) : ''}"></div>
    </div>
    <label class="form-label mt-2">ロール</label><select id="f_role" class="form-select"><option value="part_time" ${s && s.role === 'part_time' ? 'selected' : ''}>アルバイト</option><option value="employee" ${s && s.role === 'employee' ? 'selected' : ''}>社員</option></select>
    <div class="row mt-2">
      <div class="col-4"><label class="form-label" for="f_wage">時給</label><input id="f_wage" type="number" class="form-control" value="${s ? s.hourly_wage : 1100}"></div>
      <div class="col-4"><label class="form-label" for="f_min">最低h</label><input id="f_min" type="number" class="form-control" value="${s ? s.min_hours_per_month : 0}"></div>
      <div class="col-4"><label class="form-label" for="f_max">上限h</label><input id="f_max" type="number" class="form-control" value="${s ? s.max_hours_per_month : 160}"></div>
    </div>
    <label class="form-label mt-2">ステータス</label><select id="f_resign" class="form-select"><option value="0" ${!s || !s.is_resigned ? 'selected' : ''}>在籍</option><option value="1" ${s && s.is_resigned ? 'selected' : ''}>退職</option></select>
    <label class="form-label mt-2">パスワード ${isEdit ? '（変更時のみ・8文字以上）' : '（8文字以上・英数字）'}</label><input id="f_pw" class="form-control" placeholder="${isEdit ? '空欄で変更なし' : 'パスワード'}">`,
    async (w, close) => {
      const g = (id) => w.querySelector(id).value;
      try {
        if (isEdit) {
          await api(`/shop/staffs/${s.id}`, { method: 'PUT', body: JSON.stringify({ name: g('#f_name'), hourly_wage: +g('#f_wage'), min_hours_per_month: +g('#f_min'), max_hours_per_month: +g('#f_max'), is_resigned: !!+g('#f_resign'), password: g('#f_pw') || undefined }) });
        } else {
          await api('/shop/staffs', { method: 'POST', body: JSON.stringify({ staff_code: g('#f_code'), name: g('#f_name'), password: g('#f_pw'), role: g('#f_role'), hourly_wage: +g('#f_wage'), min_hours_per_month: +g('#f_min'), max_hours_per_month: +g('#f_max') }) });
        }
        close(); toast('保存しました', 'success'); navigateTo('staffs');
      } catch (e) { toast(e.message, 'error'); }
    });
}
function showFixedShiftModal(staffId, staffName) {
  api('/shop/fixed-shifts').then((d) => {
    let mine = d.fixed_shifts.filter((f) => f.staff_id === staffId);
    const render = (w) => {
      w.querySelector('#fxList').innerHTML = mine.length ? mine.map((f) => `
        <div class="list-row"><div>${badge(WD[f.weekday] + '曜', 'info')} ${esc(f.start_time)} - ${esc(f.end_time)}</div>
        <div class="flex gap-1">
          <button class="btn btn-sm btn-light" data-edit="${f.id}" data-wd="${f.weekday}" data-st="${f.start_time}" data-et="${f.end_time}"><i class="bi bi-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger" data-del="${f.id}"><i class="bi bi-x"></i></button>
        </div></div>`).join('') : '<div class="small text-secondary">固定シフト未設定</div>';
      w.querySelectorAll('[data-del]').forEach((b) => b?.addEventListener('click', async () => { await api(`/shop/fixed-shifts/${b.dataset.del}`, { method: 'DELETE' }); mine = mine.filter((m) => m.id != b.dataset.del); render(w); }));
      w.querySelectorAll('[data-edit]').forEach((b) => {
        b?.addEventListener('click', () => openModal('<i class="bi bi-pencil"></i> 固定シフト編集',
          `<label class="form-label" for="eWd">曜日</label><select id="eWd" class="form-select mb-2">${WD.map((n, i) => `<option value="${i}" ${i == b.dataset.wd ? 'selected' : ''}>${n}曜</option>`).join('')}</select>
           <div class="row"><div class="col-6"><label class="form-label" for="eSt">開始</label><input id="eSt" class="form-control" value="${b.dataset.st}"></div><div class="col-6"><label class="form-label" for="eEt">終了</label><input id="eEt" class="form-control" value="${b.dataset.et}"></div></div>`,
          async (w2, close2) => {
            try { await api(`/shop/fixed-shifts/${b.dataset.edit}`, { method: 'PUT', body: JSON.stringify({ weekday: +w2.querySelector('#eWd').value, start_time: w2.querySelector('#eSt').value, end_time: w2.querySelector('#eEt').value }) });
              const m = mine.find((x) => x.id == b.dataset.edit); if (m) { m.weekday = +w2.querySelector('#eWd').value; m.start_time = w2.querySelector('#eSt').value; m.end_time = w2.querySelector('#eEt').value; }
              close2(); render(w);
            } catch (e) { toast(e.message, 'error'); }
          }));
      });
    };
    const w = openModal(`<i class="bi bi-calendar-week"></i> 固定シフト — ${esc(staffName)}`,
      `<div id="fxList" class="mb-3"></div>
       <div class="row"><div class="col-4"><label class="form-label" for="fxWd">曜日</label><select id="fxWd" class="form-select">${WD.map((n, i) => `<option value="${i}">${n}曜</option>`).join('')}</select></div>
       <div class="col-4"><label class="form-label" for="fxSt">開始</label><input id="fxSt" class="form-control" value="09:00"></div>
       <div class="col-4"><label class="form-label" for="fxEt">終了</label><input id="fxEt" class="form-control" value="18:00"></div></div>`,
      async (w2, close) => {
        try { const r = await api('/shop/fixed-shifts', { method: 'POST', body: JSON.stringify({ staff_id: staffId, weekday: +w2.querySelector('#fxWd').value, start_time: w2.querySelector('#fxSt').value, end_time: w2.querySelector('#fxEt').value }) });
          mine.push({ id: r.id, staff_id: staffId, weekday: +w2.querySelector('#fxWd').value, start_time: w2.querySelector('#fxSt').value, end_time: w2.querySelector('#fxEt').value });
          render(w2);
        } catch (e) { toast(e.message, 'error'); }
      });
    render(w);
  });
}

/* ---------- Requests (希望休管理) ---------- */
SCREENS.requests = async function (el) {
  el.innerHTML = pageHead('希望休管理', 'bi-inbox', 'スタッフからの希望シフト一覧') + card(`<div id="reqList"><div class="text-secondary small">読み込み中...</div></div>`);
  try {
    const d = await api(`/shop/shifts?start=${todayStr().slice(0,8)+'01'}&end=${plusMonths(1)}`);
    const reqs = (d.shifts || []).filter((s) => s.status === 'requested');
    const box = document.getElementById('reqList');
    if (!reqs.length) { box.innerHTML = emptyState('bi-inbox', '希望シフトはありません'); return; }
    box.innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>スタッフ</th><th>日付</th><th>時間</th><th>種別</th><th></th></tr></thead><tbody>
      ${reqs.sort((a,b)=>a.start_datetime.localeCompare(b.start_datetime)).map((s) => `
        <tr>
          <td>${esc(s.staff_name)}</td>
          <td class="num">${esc(s.start_datetime.slice(0,10))} (${wdName(s.start_datetime.slice(0,10))})</td>
          <td class="num">${s.availability ? badge({any:'いつでも',morning:'早番',evening:'遅番'}[s.availability]||'柔軟','info') : hm(s.start_datetime)+'-'+hm(s.end_datetime)}</td>
          <td>${badge('希望', 'warning')}</td>
          <td><button class="btn btn-sm btn-outline-danger" data-del="${s.id}"><i class="bi bi-x"></i></button></td>
        </tr>`).join('')}</tbody></table></div>`;
    box.querySelectorAll('[data-del]').forEach((b) => b?.addEventListener('click', async () => {
      if (!confirm('この希望を削除しますか？')) return;
      try { await api(`/shop/shifts/${b.dataset.del}`, { method: 'DELETE' }); toast('削除しました', 'success'); navigateTo('requests'); } catch (e) { toast(e.message, 'error'); }
    }));
  } catch (e) { document.getElementById('reqList').innerHTML = `<div class="text-danger">${esc(e.message)}</div>`; }
};

/* ---------- Analytics (人件費分析) ---------- */
SCREENS.analytics = async function (el) {
  el.innerHTML = pageHead('人件費分析', 'bi-graph-up-arrow') +
    `<div class="kpi-grid" id="anaKpi"></div>
    <div class="dash-grid">
      <div id="anaLeft"></div>
      <div id="anaRight"></div>
    </div>`;
  try {
    const start = todayStr().slice(0, 8) + '01';
    const end = todayStr().slice(0, 8) + '31';
    const [sum, d] = await Promise.all([api(`/shop/summary?start=${start}&end=${end}`), api('/shop/dashboard')]);
    document.getElementById('anaKpi').innerHTML =
      kpiCard('bi-cash-stack', '今月の人件費', '¥' + (d.month_cost / 10000).toFixed(1) + '万', `${d.month_hours}h`, 'indigo') +
      kpiCard('bi-clock', '総労働時間', d.month_hours + 'h', `スタッフ${d.staff_count}名`, 'green') +
      kpiCard('bi-triangle-exclamation', '不足枠', d.shortage_total, '月間', d.shortage_total ? 'red' : 'green') +
      kpiCard('bi-people', '1人あたり', (d.month_hours / Math.max(d.staff_count, 1)).toFixed(0) + 'h', '平均', 'amber');

    // Cost chart
    document.getElementById('anaLeft').innerHTML = card(sectionTitle('bi-graph-up', '日別人件費') + `<div class="chart-box"><canvas id="anaCost"></canvas></div>`);
    const costData = d.daily_cost_series || [];
    chartInstances.anaCost = new Chart(document.getElementById('anaCost'), {
      type: 'bar',
      data: { labels: costData.map((c) => c.date.slice(5)), datasets: [{ label: '人件費', data: costData.map((c) => c.cost), backgroundColor: 'rgba(99,102,241,.6)', borderRadius: 4 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { color: '#64748B', callback: (v) => '¥' + (v / 1000) + 'K' }, grid: { color: 'rgba(148,163,184,.1)' } }, x: { ticks: { color: '#64748B', maxTicksLimit: 10 }, grid: { display: false } } } }
    });

    // Staff distribution
    const staffData = (sum.staff || []).slice().sort((a, b) => b.projected_hours - a.projected_hours).slice(0, 8);
    document.getElementById('anaRight').innerHTML = card(sectionTitle('bi-bar-chart', 'スタッフ別労働時間') +
      `<div class="chart-box"><canvas id="anaStaff"></canvas></div>`);
    chartInstances.anaStaff = new Chart(document.getElementById('anaStaff'), {
      type: 'bar',
      data: { labels: staffData.map((s) => s.name), datasets: [{ label: '時間', data: staffData.map((s) => s.projected_hours), backgroundColor: staffData.map((s) => s.role === 'employee' ? 'rgba(16,185,129,.6)' : 'rgba(99,102,241,.6)'), borderRadius: 4 }] },
      options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { color: '#64748B' }, grid: { display: false } }, x: { ticks: { color: '#64748B' }, grid: { color: 'rgba(148,163,184,.1)' } } } }
    });

    // AI advice
    let advice = '分析中...';
    try { const rev = await api('/shop/ai/review', { method: 'POST', body: JSON.stringify({ start, end }) }); advice = rev.advice; } catch {}
    document.getElementById('anaRight').innerHTML += card(sectionTitle('bi-stars', 'AI改善提案', badge('AI', 'ai')) + `<div style="font-size:.88rem;line-height:1.7;white-space:pre-wrap">${esc(advice)}</div>`);
  } catch (e) { el.innerHTML += card(`<div class="text-danger">${esc(e.message)}</div>`); }
};

/* ---------- Notifications ---------- */
SCREENS.notifications = async function (el) {
  el.innerHTML = pageHead('通知', 'bi-bell') + card(`<div id="notifList"><div class="text-muted small">読み込み中...</div></div><button class="btn btn-light w-full mt-3 d-none" id="readAll">すべて既読にする</button>`);
  const loadNotifs = async () => {
    try {
      const d = await api('/shop/notifications');
      const list = document.getElementById('notifList');
      list.innerHTML = d.notifications.length ? d.notifications.map((n) => `
        <div class="notif-item ${n.is_read ? '' : 'unread'}"><div class="nt-title">${esc(n.title)}</div><div class="nt-body">${esc(n.body || '')}</div><div class="nt-time">${esc((n.created_at || '').replace('T', ' ').slice(0, 16))}</div></div>`).join('')
        : emptyState('bi-bell', '通知はありません');
      const readBtn = document.getElementById('readAll');
      if (d.unread > 0) { readBtn.classList.remove('d-none'); } else { readBtn.classList.add('d-none'); }
    } catch (e) { document.getElementById('notifList').innerHTML = `<div class="text-danger">${esc(e.message)}</div>`; }
  };
  await loadNotifs();
  document.getElementById('readAll')?.addEventListener('click', async () => {
    await api('/shop/notifications/read-all', { method: 'PUT' });
    toast('既読にしました', 'success');
    await loadNotifs(); refreshNotifBadge();
  });
};

/* ---------- Settings ---------- */
let settingsTab = 'shift';
SCREENS.settings = function (el) {
  el.innerHTML = pageHead('設定', 'bi-gear') +
    `<div class="tabs no-print">
      <button class="tab ${settingsTab==='shift'?'active':''}" data-tab="shift">シフト設定</button>
      <button class="tab ${settingsTab==='shop'?'active':''}" data-tab="shop">店舗情報</button>
      <button class="tab ${settingsTab==='periods'?'active':''}" data-tab="periods">募集期間</button>
      <button class="tab ${settingsTab==='password'?'active':''}" data-tab="password">パスワード</button>
    </div><div id="settingsBody"></div>`;
  el.querySelectorAll('.tab').forEach((t) => t?.addEventListener('click', () => { settingsTab = t.dataset.tab; el.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === t)); renderSettingsTab(el.querySelector('#settingsBody')); }));
  renderSettingsTab(el.querySelector('#settingsBody'));
};
function renderSettingsTab(body) {
  ({ shift: renderShiftMatrixTab, shop: renderShopTab, periods: renderPeriodsTab, password: renderPasswordTab }[settingsTab])(body);
}

/* --- シフト設定（マトリクス） --- */
function renderShiftMatrixTab(body) {
  body.innerHTML = card(
    sectionTitle('bi-grid-3x3-gap', 'シフト設定', `<span class="small text-secondary">— 各時間帯の必要人数を曜日ごとに設定</span>`) +
    `<p class="small text-secondary mb-3">空欄のマスは<strong>基本</strong>の人数が適用されます。<strong>0</strong>を入れるとその曜日は募集しません。</p>
    <div id="matrixWrap"></div>
    <button class="btn btn-primary mt-3" id="addPat"><i class="bi bi-plus-lg"></i> 時間帯を追加</button>`);
  loadMatrix(body);
  body.querySelector('#addPat')?.addEventListener('click', () => openPatternModal(null, () => loadMatrix(body)));
}
async function loadMatrix(body) {
  const wrap = body.querySelector('#matrixWrap');
  try {
    const d = await api('/shop/patterns');
    if (!d.patterns.length) { wrap.innerHTML = emptyState('bi-grid-3x3-gap', '時間帯がありません。「時間帯を追加」で作成してください'); return; }
    wrap.innerHTML = `<div class="matrix-wrap"><table class="matrix-table">
      <thead><tr>
        <th style="text-align:left;padding-left:14px">時間帯</th>
        <th>基本</th>
        <th class="sun">日</th><th>月</th><th>火</th><th>水</th><th>木</th><th>金</th><th class="sat">土</th>
        <th></th>
      </tr></thead>
      <tbody>${d.patterns.map((p) => {
        const wr = p.weekday_required || {};
        return `<tr data-pid="${p.id}">
          <td><div class="matrix-pat-name">${esc(p.pattern_name)}</div><div class="matrix-pat-time">${esc(p.start_time)} - ${esc(p.end_time)}</div></td>
          <td><input type="number" class="matrix-input matrix-default" data-pid="${p.id}" value="${p.required_staff}" min="0" title="基本必要人数"></td>
          ${[0,1,2,3,4,5,6].map((w) => {
            const val = wr[String(w)];
            const has = val !== undefined && val !== null;
            return `<td><input type="number" class="matrix-input matrix-wd ${has?'has-override':''}" data-pid="${p.id}" data-wd="${w}" value="${has?val:''}" placeholder="${p.required_staff}" min="0"></td>`;
          }).join('')}
          <td><div class="matrix-row-actions">
            <button data-edit="${p.id}" data-n="${esc(p.pattern_name)}" data-st="${p.start_time}" data-et="${p.end_time}" data-req="${p.required_staff}" title="編集"><i class="bi bi-pencil"></i></button>
            <button data-del="${p.id}" title="削除"><i class="bi bi-trash"></i></button>
          </div></td>
        </tr>`;
      }).join('')}</tbody>
    </table></div>
    <div class="flex gap-2 mt-3">
      <button class="btn btn-primary" id="saveMatrix"><i class="bi bi-check-lg"></i> 保存</button>
      <span class="small text-secondary flex items-center">※変更後「保存」を押してください。青い数字は曜日別オーバーライドです。</span>
    </div>`;
    // Edit buttons
    wrap.querySelectorAll('[data-edit]').forEach((b) => b?.addEventListener('click', () => openPatternModal(b.dataset, () => loadMatrix(body))));
    // Delete buttons
    wrap.querySelectorAll('[data-del]').forEach((b) => b?.addEventListener('click', async () => {
      if (!confirm('この時間帯を削除しますか？曜日別設定も削除されます。')) return;
      await api(`/shop/patterns/${b.dataset.del}`, { method: 'DELETE' });
      toast('削除しました', 'success'); loadMatrix(body);
    }));
    // Save
    body.querySelector('#saveMatrix')?.addEventListener('click', async () => {
      try {
        const rows = wrap.querySelectorAll('tbody tr');
        for (const tr of rows) {
          const pid = tr.dataset.pid;
          const defVal = +tr.querySelector('.matrix-default').value;
          const name = tr.querySelector('.matrix-pat-name').textContent;
          const time = tr.querySelector('.matrix-pat-time').textContent;
          const [st, et] = time.split(' - ');
          // Update pattern default
          await api(`/shop/patterns/${pid}`, { method: 'PUT', body: JSON.stringify({ pattern_name: name, start_time: st, end_time: et, required_staff: defVal }) });
          // Collect weekday overrides
          const wr = {};
          tr.querySelectorAll('.matrix-wd').forEach((inp) => { const v = inp.value.trim(); if (v !== '') wr[inp.dataset.wd] = parseInt(v, 10); });
          await api(`/shop/patterns/${pid}/weekday-required`, { method: 'PUT', body: JSON.stringify({ weekday_required: wr }) });
        }
        toast('保存しました', 'success'); loadMatrix(body);
      } catch (e) { toast(e.message, 'error'); }
    });
  } catch (e) { wrap.innerHTML = `<div class="text-danger">${esc(e.message)}</div>`; }
}
function openPatternModal(data, onDone) {
  const isEdit = !!data;
  openModal(`<i class="bi bi-clock-history"></i> ${isEdit ? '時間帯の編集' : '新しい時間帯'}`,
    `<label class="form-label" for="pName">時間帯名</label><input id="pName" class="form-control mb-2" value="${data?.n || ''}" placeholder="例: 夜">
     <div class="row"><div class="col-6"><label class="form-label" for="pSt">開始</label><input id="pSt" class="form-control" value="${data?.st || '17:00'}"></div>
     <div class="col-6"><label class="form-label" for="pEt">終了</label><input id="pEt" class="form-control" value="${data?.et || '22:00'}"></div></div>
     <label class="form-label mt-2">基本必要人数</label><input id="pReq" type="number" class="form-control" value="${data?.req || 2}">
     <div class="small text-secondary mt-2">作成後、マトリクスで曜日別の人数を設定できます。</div>`,
    async (w, close) => {
      try {
        if (isEdit) {
          await api(`/shop/patterns/${data.edit}`, { method: 'PUT', body: JSON.stringify({ pattern_name: w.querySelector('#pName').value, start_time: w.querySelector('#pSt').value, end_time: w.querySelector('#pEt').value, required_staff: +w.querySelector('#pReq').value }) });
        } else {
          await api('/shop/patterns', { method: 'POST', body: JSON.stringify({ pattern_name: w.querySelector('#pName').value, start_time: w.querySelector('#pSt').value, end_time: w.querySelector('#pEt').value, required_staff: +w.querySelector('#pReq').value }) });
        }
        close(); toast('保存しました', 'success'); onDone?.();
      } catch (e) { toast(e.message, 'error'); }
    });
}

function renderShopTab(body) {
  body.innerHTML = card('<div class="text-secondary small">読み込み中...</div>');
  api('/shop/settings').then((d) => {
    const s = d.settings || {};
    body.innerHTML = card(sectionTitle('bi-shop', '店舗情報') +
      `<label class="form-label" for="setShopName">店舗名</label><input id="setShopName" class="form-control mb-2" value="${esc(d.shop_name)}">
       <label class="form-label" for="setShopCode">店舗コード</label><input id="setShopCode" class="form-control mb-3" value="${esc(d.shop_code)}" disabled>
       <hr style="border-color:var(--line);margin:16px 0">
       ${sectionTitle('bi-gear', '運用設定')}
       <div class="row">
         <div class="col-6"><label class="form-label" for="setWage">デフォルト時給(円)</label><input id="setWage" type="number" class="form-control" value="${s.default_hourly_wage ?? 1000}"></div>
         <div class="col-6"><label class="form-label" for="setMinDaily">1日最低勤務(h)</label><input id="setMinDaily" type="number" class="form-control" value="${s.min_daily_hours ?? 4}"></div>
         <div class="col-6"><label class="form-label" for="setMaxDaily">1日最大勤務(h)</label><input id="setMaxDaily" type="number" class="form-control" value="${s.max_daily_hours ?? 9}"></div>
         <div class="col-6"><label class="form-label" for="setMaxConsec">最大連勤（推奨）</label><input id="setMaxConsec" type="number" class="form-control" value="${s.max_consecutive_days ?? 6}"></div>
         <div class="col-6"><label class="form-label" for="setNightRate">深夜割増率</label><input id="setNightRate" type="number" step="0.05" class="form-control" value="${s.night_premium_rate ?? 1.25}"></div>
         <div class="col-6"><label class="form-label" for="setTransport">1日交通費(円)</label><input id="setTransport" type="number" class="form-control" value="${s.transport_per_day ?? 0}"></div>
         <div class="col-6"><label class="form-label" for="setBiz">営業時間</label><input id="setBiz" class="form-control" value="${esc(s.business_hours || '')}" placeholder="9:00-22:00"></div>
         <div class="col-6"><label class="form-label" for="setPeriodMode">デフォルト期間</label><select id="setPeriodMode" class="form-select"><option value="half" ${(s.period_mode || 'half') === 'half' ? 'selected' : ''}>半月ごと</option><option value="month" ${s.period_mode === 'month' ? 'selected' : ''}>1ヶ月ごと</option></select></div>
       </div>
       <button class="btn btn-primary btn-lg w-full mt-3" id="saveSettings">保存</button>
       <div id="setMsg" class="mt-2 small"></div>`);
    body.querySelector('#saveSettings')?.addEventListener('click', async () => {
      try {
        await api('/shop/settings', { method: 'PUT', body: JSON.stringify({
          shop_name: body.querySelector('#setShopName').value,
          settings: {
            default_hourly_wage: +body.querySelector('#setWage').value, min_daily_hours: +body.querySelector('#setMinDaily').value,
            max_daily_hours: +body.querySelector('#setMaxDaily').value, max_consecutive_days: +body.querySelector('#setMaxConsec').value,
            night_premium_rate: +body.querySelector('#setNightRate').value, transport_per_day: +body.querySelector('#setTransport').value,
            business_hours: body.querySelector('#setBiz').value, period_mode: body.querySelector('#setPeriodMode').value } }) });
        toast('保存しました', 'success'); currentUser.shop_name = body.querySelector('#setShopName').value;
      } catch (e) { toast(e.message, 'error'); }
    });
  });
}

function renderPeriodsTab(body) {
  body.innerHTML = card(`<div class="flex justify-between items-center mb-3">${sectionTitle('bi-calendar-range', '募集期間')}<button class="btn btn-primary btn-sm" id="addPer"><i class="bi bi-plus-lg"></i></button></div><div id="perList"></div>`);
  const load = async () => {
    const d = await api('/shop/periods');
    document.getElementById('perList').innerHTML = d.periods.length ? d.periods.map((p) => `
      <div class="list-row"><div><strong class="num">${esc(p.start_date)} 〜 ${esc(p.end_date)}</strong> ${p.is_active ? badge('受付中', 'success') : badge('終了', 'muted')}<div class="small text-secondary">締切 ${esc(p.deadline)}</div></div>
        <div class="flex gap-1"><button class="btn btn-sm btn-light" data-toggle="${p.id}" data-active="${p.is_active}">${p.is_active ? '終了' : '再開'}</button><button class="btn btn-sm btn-outline-danger" data-pdel="${p.id}"><i class="bi bi-trash"></i></button></div></div>`).join('')
      : emptyState('bi-calendar-range', '募集期間がありません');
    document.getElementById('perList').querySelectorAll('[data-toggle]').forEach((b) => b?.addEventListener('click', async () => { await api(`/shop/periods/${b.dataset.toggle}`, { method: 'PUT', body: JSON.stringify({ is_active: b.dataset.active !== '1' }) }); load(); }));
    document.getElementById('perList').querySelectorAll('[data-pdel]').forEach((b) => b?.addEventListener('click', async () => { if (confirm('削除しますか？')) { await api(`/shop/periods/${b.dataset.pdel}`, { method: 'DELETE' }); load(); } }));
  };
  load();
  document.getElementById('addPer')?.addEventListener('click', async () => {
    let np = window._nextPeriod; if (!np) { try { np = await api('/shop/periods/next'); } catch { np = { start_date: '', end_date: '', deadline: '' }; } }
    openModal('<i class="bi bi-plus-lg"></i> 募集期間追加',
      `<div class="row"><div class="col-6"><label class="form-label" for="peStart">開始</label><input type="date"  id="peStart" class="form-control" value="${np.start_date}"></div><div class="col-6"><label class="form-label" for="peEnd">終了</label><input type="date"  id="peEnd" class="form-control" value="${np.end_date}"></div></div>
       <label class="form-label mt-2">締切</label><input type="date" id="peDeadline" class="form-control" value="${np.deadline}">`,
      async (w, close) => { try { await api('/shop/periods', { method: 'POST', body: JSON.stringify({ start_date: w.querySelector('#peStart').value, end_date: w.querySelector('#peEnd').value, deadline: w.querySelector('#peDeadline').value }) }); close(); toast('追加しました', 'success'); load(); } catch (e) { toast(e.message, 'error'); } });
  });
}

function renderPasswordTab(body) {
  body.innerHTML = card(sectionTitle('bi-key', 'パスワード変更') +
    `<label class="form-label" for="pwCur">現在のパスワード</label><input type="password"  id="pwCur" class="form-control mb-2">
     <label class="form-label" for="pwNew">新しいパスワード（8文字以上・英数字）</label><input type="password"  id="pwNew" class="form-control mb-2">
     <label class="form-label" for="pwConf">新しいパスワード（確認）</label><input type="password"  id="pwConf" class="form-control mb-3">
     <button class="btn btn-primary btn-lg w-full" id="pwBtn">変更</button>`);
  body.querySelector('#pwBtn')?.addEventListener('click', async () => {
    if (body.querySelector('#pwNew').value !== body.querySelector('#pwConf').value) { toast('確認用が一致しません', 'error'); return; }
    try { await api('/shop/password', { method: 'PUT', body: JSON.stringify({ current_password: body.querySelector('#pwCur').value, new_password: body.querySelector('#pwNew').value }) }); toast('変更しました', 'success'); }
    catch (e) { toast(e.message, 'error'); }
  });
}

/* ============================================================
   Staff Screens
   ============================================================ */
function openChangeRequestModal(s) {
  const sl = (iso) => (iso || '').slice(0, 16);
  const w = openModal('<i class="bi bi-pencil"></i> シフト変更申請',
    `<div class="small text-secondary mb-2">対象: ${esc(s.start_datetime.slice(0, 16))} 〜 ${esc(s.end_datetime.slice(11, 16))}</div>
     <label class="form-label" for="crType">申請種別</label><select id="crType" class="form-select mb-3"><option value="change">時間変更</option><option value="cancel">休みにする</option></select>
     <div id="crTime"><label class="form-label" for="crStart">希望時間</label><div class="row mb-2"><div class="col-6"><input type="datetime-local" id="crStart" class="form-control" value="${sl(s.start_datetime)}"></div><div class="col-6"><input type="datetime-local" id="crEnd" class="form-control" value="${sl(s.end_datetime)}"></div></div></div>
     <label class="form-label" for="crReason">理由</label><input id="crReason" class="form-control mb-2" placeholder="例: 用事のため変更希望">
     <div class="small text-secondary">※店長の承認後にシフトへ反映されます</div>`,
    async (w2, close) => {
      try {
        await api('/staff/change-requests', { method: 'POST', body: JSON.stringify({ shift_id: s.id, request_type: w2.querySelector('#crType').value, desired_start: w2.querySelector('#crStart').value + ':00', desired_end: w2.querySelector('#crEnd').value + ':00', reason: w2.querySelector('#crReason').value }) });
        close(); toast('申請を送信しました', 'success'); refreshNotifBadge();
      } catch (e) { toast(e.message, 'error'); }
    });
  const t = w.querySelector('#crType'); const timeBox = w.querySelector('#crTime');
  t?.addEventListener('change', () => { timeBox.style.display = t.value === 'cancel' ? 'none' : 'block'; });
}

SCREENS.staffDashboard = async function (el) {
  // 募集期間を取得してバナー表示
  let periodBanner = '';
  try {
    const periods = await api('/staff/periods');
    const ap = (periods.periods || []).filter((p) => p.is_active).sort((a, b) => b.end_date.localeCompare(a.end_date))[0];
    if (ap) {
      periodBanner = `<div class="kpi-card kpi-indigo mb-3"><div class="kpi-label"><i class="bi bi-megaphone"></i> シフト希望受付中</div><div class="kpi-value num" style="font-size:1.05rem">${ap.start_date} 〜 ${ap.end_date}</div><div class="kpi-sub">締切: ${ap.deadline}</div><button class="btn btn-primary btn-sm mt-2" id="goRequest"><i class="bi bi-pencil-square"></i> 希望を提出する</button></div>`;
    }
  } catch {}

  el.innerHTML = pageHead('ホーム', 'bi-house-door', currentUser.name + 'さん') + periodBanner +
    card(sectionTitle('bi-calendar-check', '次のシフト') + `<div id="nextBox"><div class="text-muted small">読み込み中...</div></div>`) +
    card(sectionTitle('bi-stars', 'AIアシスタント', badge('AI', 'ai')) +
      `<div id="miniChat" style="max-height:300px;overflow-y:auto"></div>
       <div class="chat-input-row mt-2" style="border:none;padding:0">
         <input type="text" id="miniChatInput" class="form-control chat-input" placeholder="例: 次のシフトは？ / 月5万円稼ぐには？">
         <button class="btn btn-ai chat-send" id="miniChatSend"><i class="bi bi-send-fill"></i></button>
       </div>
       <div class="chat-suggestions mt-2" style="border:none;padding:0" id="miniChatSug"></div>`) +
    card(sectionTitle('bi-bell', 'お知らせ') + `<div id="notifBox"><div class="text-muted small">読み込み中...</div></div>`) +
    card(sectionTitle('bi-clock-history', '申請ステータス') + `<div id="creqBox"><div class="text-muted small">読み込み中...</div></div>`);

  // 募集期間バナーのボタン
  const goReq = document.getElementById('goRequest');
  if (goReq) goReq?.addEventListener('click', () => navigateTo('request'));

  try {
    const d = await api('/staff/dashboard');
    const ns = d.next_shift;
    document.getElementById('nextBox').innerHTML = ns
      ? `<div class="kpi-card kpi-indigo" style="margin:0"><div class="kpi-label">次回</div><div class="kpi-value num">${esc(ns.start_datetime.slice(5, 10))} ${hm(ns.start_datetime)}〜${hm(ns.end_datetime)}</div></div>`
      : '<div class="text-muted small">確定している今後のシフトはありません</div>';
  } catch {}
  try { const n = await api('/staff/notifications'); document.getElementById('notifBox').innerHTML = n.notifications.length ? n.notifications.slice(0, 5).map((x) => `<div class="notif-item ${x.is_read?'':'unread'}"><div class="nt-title">${esc(x.title)}</div><div class="nt-body">${esc(x.body||'')}</div></div>`).join('') : '<div class="small text-muted">通知はありません</div>'; } catch {}
  try {
    const c = await api('/staff/change-requests');
    document.getElementById('creqBox').innerHTML = c.change_requests.length ? c.change_requests.slice(0, 8).map((r) => {
      const tn = { change: '時間変更', cancel: '休み', add: '追加' }[r.request_type];
      const st = { approved: ['success','承認済'], rejected: ['warning','却下'], pending: ['muted','承認待ち'] }[r.status];
      return `<div class="list-row"><div>${badge(tn, 'info')} ${r.desired_start ? '<span class="small text-muted">'+esc(r.desired_start.slice(5,16))+'</span>' : ''}<div class="small text-muted">${esc(r.reason||'')}</div></div>${badge(st[1], st[0])}</div>`;
    }).join('') : '<div class="small text-muted">申請履歴はありません</div>';
  } catch {}
  // Mini chat（__thinking__バグ修正：タイピングインジケーターを使用）
  if (!window._miniChat) window._miniChat = [];
  if (!window._miniChat.length) {
    window._miniChat.push({ role: 'assistant', content: `${currentUser.name}さん、こんにちは。シフトについて何でもお聞きください。` });
  }
  const renderMini = () => {
    document.getElementById('miniChat').innerHTML = window._miniChat.slice(-6).map((m) => {
      if (m.content === '__thinking__') {
        return `<div class="chat-bubble chat-bubble-ai"><div class="chat-ai-avatar"><i class="bi bi-stars"></i></div><div class="chat-ai-text"><div class="ai-thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div></div></div>`;
      }
      if (m.role === 'user') {
        return `<div class="chat-bubble chat-bubble-user">${esc(m.content)}</div>`;
      }
      // AI未接続（unavailable）は警告スタイルで「ルールベース」と誤認させない
      if (m.source === 'unavailable') {
        return `<div class="chat-bubble chat-bubble-warn"><div class="chat-ai-avatar"><i class="bi bi-exclamation-triangle"></i></div><div class="chat-ai-text">${esc(m.content)}</div></div>`;
      }
      return `<div class="chat-bubble chat-bubble-ai"><div class="chat-ai-avatar"><i class="bi bi-stars"></i></div><div class="chat-ai-text">${esc(m.content)}</div></div>`;
    }).join('');
    document.getElementById('miniChat').scrollTop = 9999;
  };
  const renderSug = () => {
    document.getElementById('miniChatSug').innerHTML = ['次のシフトは？','月5万円稼ぐには？','シフトの変更は？'].map((s) => `<button class="chat-suggest-chip" data-sug="${esc(s)}">${esc(s)}</button>`).join('');
    document.querySelectorAll('#miniChatSug [data-sug]').forEach((b) => b?.addEventListener('click', () => { document.getElementById('miniChatInput').value = b.dataset.sug; sendMini(); }));
  };
  async function sendMini() {
    const inp = document.getElementById('miniChatInput');
    const msg = (inp.value || '').trim(); if (!msg) return;
    inp.value = '';
    window._miniChat.push({ role: 'user', content: msg });
    window._miniChat.push({ role: 'assistant', content: '__thinking__' });
    renderMini();
    try {
      const history = window._miniChat.filter((h) => h.content !== '__thinking__').slice(-11, -1);
      const d = await api('/staff/ai/chat', { method: 'POST', body: JSON.stringify({ message: msg, history }) });
      // source: 'llm' | 'unavailable' — 未接続時は警告スタイルで表示
      window._miniChat[window._miniChat.length - 1] = { role: 'assistant', content: d.reply, source: d.source };
    } catch (e) {
      window._miniChat[window._miniChat.length - 1] = { role: 'assistant', content: 'エラーが発生しました。もう一度お試しください。' };
    }
    renderMini();
  }
  document.getElementById('miniChatSend')?.addEventListener('click', sendMini);
  document.getElementById('miniChatInput')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendMini(); });
  renderMini(); renderSug();
};

SCREENS.myshift = function (el) {
  el.innerHTML = pageHead('マイシフト', 'bi-calendar-check') +
    card(`<div class="flex justify-between items-center mb-2">${sectionTitle('bi-calendar-check', 'マイシフト')}<button class="btn btn-light btn-sm" id="icsBtn"><i class="bi bi-calendar-plus"></i> カレンダー同期</button></div>
      <div id="mySummary" class="mb-2"></div><div id="staffCalMount"></div>
      <div class="small text-secondary mt-2">日付をダブルタップでシフト表表示・バーをタップで変更申請</div>`);
  const now = new Date();
  const s = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-01`;
  const e = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-31`;
  api(`/staff/summary?start=${s}&end=${e}`).then((d) => {
    document.getElementById('mySummary').innerHTML = d.staff.length ? `<div class="flex gap-2 flex-wrap">${badge(d.staff[0].days+'日', 'info')} ${badge('確定'+d.staff[0].confirmed_hours+'h', 'muted')} <span class="stat-pill" style="color:var(--success)"><i class="bi bi-cash"></i> ${yen(d.staff[0].pay)}</span></div>` : '<div class="small text-secondary">確定シフトがまだありません</div>';
  }).catch(() => {});
  createCalendar(document.getElementById('staffCalMount'), {
    loader: (from, to) => api(`/staff/shifts?start=${from}&end=${to}`).then((d) => d.shifts),
    editable: false, onChange: (sh) => openChangeRequestModal(sh),
  });
  document.getElementById('icsBtn')?.addEventListener('click', () => {
    const url = `${location.origin}${API}/staff/shifts/ics?t=${authToken}`;
    const m = openModal('<i class="bi bi-calendar-plus"></i> カレンダー同期 (iCal)',
      `<p class="small text-secondary">以下のURLをGoogleカレンダー等の「他のカレンダー追加 → URLで追加」へ設定すると、自分の確定シフトが自動同期されます。</p>
       <textarea class="form-control" rows="3" readonly>${esc(url)}</textarea>
       <button class="btn btn-primary w-full mt-2" id="copyIcs"><i class="bi bi-clipboard"></i> URLをコピー</button>
       <a class="btn btn-light w-full mt-2" href="${esc(url)}" download="my_shift.ics"><i class="bi bi-download"></i> .icsファイルをダウンロード</a>`, null);
    m.querySelector('#copyIcs')?.addEventListener('click', () => navigator.clipboard.writeText(url).then(() => toast('コピーしました', 'success')));
  });
};

let wishState = {}; let wishMonth = null; let wishPeriod = null;
SCREENS.request = async function (el) {
  // 募集期間に基づいてカレンダーの初期月を設定
  try {
    const periods = await api('/staff/periods');
    wishPeriod = (periods.periods || []).filter((p) => p.is_active).sort((a, b) => b.end_date.localeCompare(a.end_date))[0] || null;
  } catch { wishPeriod = null; }
  // カレンダーの初期表示月を募集期間の開始月に合わせる
  if (wishPeriod && wishPeriod.start_date) {
    const d0 = new Date(wishPeriod.start_date + 'T00:00:00');
    wishMonth = { y: d0.getFullYear(), m: d0.getMonth() };
  } else {
    const today = new Date();
    wishMonth = { y: today.getFullYear(), m: today.getMonth() };
  }

  const periodBanner = wishPeriod
    ? `<div class="kpi-card kpi-indigo" style="margin-bottom:12px"><div class="kpi-label">募集期間</div><div class="kpi-value num" style="font-size:1.1rem">${wishPeriod.start_date} 〜 ${wishPeriod.end_date}</div><div class="kpi-sub">締切: ${wishPeriod.deadline}</div></div>`
    : `<div class="kpi-card kpi-red" style="margin-bottom:12px"><div class="kpi-label"><i class="bi bi-exclamation-triangle"></i> 募集期間外</div><div class="kpi-sub">現在シフト希望を提出できる期間ではありません。店長にお問い合わせください。</div></div>`;

  el.innerHTML = pageHead('シフト希望入力', 'bi-pencil-square') + periodBanner +
    card(sectionTitle('bi-stars', 'AIで希望を作成', badge('AI', 'ai')) +
      `<p class="small text-muted">「8万円稼ぎたい、水曜NG、夕方多め」等を入力</p>
       <textarea id="aiText" class="form-control mb-2" rows="2" placeholder="例: 今月は8万円稼ぎたい。火・木の夕方で、日曜はNG。"></textarea>
       <button class="btn btn-ai w-full" id="aiParseBtn" ${wishPeriod ? '' : 'disabled'}><i class="bi bi-stars"></i> AIで解析</button>
       <div id="aiResult" class="mt-2"></div>`) +
    card(`<div class="cal-toolbar"><button class="cal-nav-btn" id="wPrev"><i class="bi bi-chevron-left"></i></button><div class="cal-title num" id="wTitle"></div><button class="cal-nav-btn" id="wNext"><i class="bi bi-chevron-right"></i></button></div>
      <div class="cal-weekdays"><div class="sun">日</div><div>月</div><div>火</div><div>水</div><div>木</div><div>金</div><div class="sat">土</div></div>
      <div id="wishGrid" class="wish-cal"></div>
      <div class="small text-muted mt-2">日付をタップして希望を選択。募集期間内の日付のみ選択できます。</div>
      <button class="btn btn-primary btn-lg w-full mt-3" id="submitWish" ${wishPeriod ? '' : 'disabled'}><i class="bi bi-send"></i> 希望を提出</button>
      <div id="wishResult" class="mt-2"></div>
      <hr style="border-color:var(--line);margin:16px 0">
      ${sectionTitle('bi-clock-history', '提出済みの希望（調整待ち）')}<div id="myReqs"></div>`);
  function drawWish() {
    document.getElementById('wTitle').textContent = `${wishMonth.y}年 ${wishMonth.m + 1}月`;
    const first = new Date(wishMonth.y, wishMonth.m, 1); const startWd = first.getDay();
    const dim = new Date(wishMonth.y, wishMonth.m + 1, 0).getDate();
    const label = { any: 'いつでも', morning: '早番', evening: '遅番', time: '時間', rest: '休み' };
    const inPeriod = (ds) => wishPeriod && ds >= wishPeriod.start_date && ds <= wishPeriod.end_date;
    let cells = '';
    for (let i = 0; i < startWd; i++) cells += '<div class="wish-cell empty"></div>';
    for (let d = 1; d <= dim; d++) {
      const ds = `${wishMonth.y}-${String(wishMonth.m+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
      const w = wishState[ds]; const wd = new Date(ds + 'T00:00:00').getDay();
      const cls = wd === 0 ? 'sun' : (wd === 6 ? 'sat' : '');
      const allowed = inPeriod(ds);
      const cellCls = allowed ? 'wish-cell' : 'wish-cell disabled';
      const mark = w ? `<div class="wmark ${w.type === 'time' ? 'time' : w.type}">${label[w.type]}</div>` : '';
      cells += `<div class="${cellCls}" data-day="${ds}" data-allowed="${allowed ? 1 : 0}"><div class="wd ${cls}">${d}</div>${mark}</div>`;
    }
    document.getElementById('wishGrid').innerHTML = cells;
    document.getElementById('wishGrid').querySelectorAll('.wish-cell[data-day]').forEach((c) => {
      if (c.dataset.allowed === '0') {
        c?.addEventListener('click', () => toast('この日は募集期間外です', 'error'));
      } else {
        c?.addEventListener('click', () => openWishPicker(c.dataset.day));
      }
    });
  }
  function openWishPicker(day) {
    const w = openModal(`${day}（${wdName(day)}）の希望`, `
      <div class="flex flex-wrap gap-1">
        <button class="btn btn-light flex-grow" data-t="rest">休み</button>
        <button class="btn btn-light flex-grow" data-t="any">いつでも可</button>
        <button class="btn btn-light flex-grow" data-t="morning">早番</button>
        <button class="btn btn-light flex-grow" data-t="evening">遅番</button>
      </div>
      <div class="mt-2"><label class="form-label" for="wpStart">時間指定</label><div class="row"><div class="col-6"><input type="time" id="wpStart" class="form-control" value="17:00"></div><div class="col-6"><input type="time" id="wpEnd" class="form-control" value="22:00"></div></div>
      <button class="btn btn-primary w-full mt-2" data-t="time">この時間で設定</button></div>`, null);
    w.querySelectorAll('[data-t]').forEach((b) => b?.addEventListener('click', () => {
      const t = b.dataset.t;
      if (t === 'time') { const st = w.querySelector('#wpStart').value, en = w.querySelector('#wpEnd').value; wishState[day] = { type: 'time', start: `${day}T${st}:00`, end: `${day}T${en}:00` }; }
      else wishState[day] = { type: t };
      buzz(10); w.remove(); drawWish();
    }));
  }
  document.getElementById('wPrev')?.addEventListener('click', () => { wishMonth.m--; if (wishMonth.m < 0) { wishMonth.m = 11; wishMonth.y--; } drawWish(); });
  document.getElementById('wNext')?.addEventListener('click', () => { wishMonth.m++; if (wishMonth.m > 11) { wishMonth.m = 0; wishMonth.y++; } drawWish(); });
  function fillWishesFromAI(d) {
    const ng = new Set(d.ng_weekdays || []); const isTime = d.preferred_slot === 'time' && d.preferred_start && d.preferred_end;
    const pref = isTime ? 'time' : (d.preferred_slot === 'morning' ? 'morning' : d.preferred_slot === 'evening' ? 'evening' : 'any');
    const need = d.need_days || 0; const dim = new Date(wishMonth.y, wishMonth.m + 1, 0).getDate(); let filled = 0;
    const inPeriod = (ds) => wishPeriod && ds >= wishPeriod.start_date && ds <= wishPeriod.end_date;
    // HH:MM → HH:MM:00 に正規化（サーバーが %H:%M:%S パースのため）
    const padTime = (t) => /^\d{1,2}:\d{2}$/.test(t || '') ? t + ':00' : t;
    for (let day = 1; day <= dim; day++) {
      const ds = `${wishMonth.y}-${String(wishMonth.m+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
      const wd = new Date(ds + 'T00:00:00').getDay();
      if (!inPeriod(ds)) continue; // 募集期間外はスキップ
      if (ng.has(wd)) { wishState[ds] = { type: 'rest' }; continue; }
      if (need && filled >= need) { wishState[ds] = { type: 'rest' }; continue; }
      wishState[ds] = isTime ? { type: 'time', start: `${ds}T${padTime(d.preferred_start)}`, end: `${ds}T${padTime(d.preferred_end)}` } : { type: pref }; filled++;
    }
    drawWish();
  }
  document.getElementById('aiParseBtn')?.addEventListener('click', async () => {
    const text = document.getElementById('aiText').value.trim(); if (!text) { toast('文章を入力'); return; }
    const box = document.getElementById('aiResult'); box.innerHTML = '<div class="text-secondary small">解析中...</div>'; setLoading(true);
    try {
      const d = await api('/staff/ai/parse', { method: 'POST', body: JSON.stringify({ text }) });
      const ng = (d.ng_weekdays || []).map((x) => WD[x]).join('・');
      const slotTxt = d.preferred_slot === 'time' ? `${d.preferred_start}-${d.preferred_end}` : (d.preferred_slot === 'morning' ? '朝' : d.preferred_slot === 'evening' ? '夜' : '指定なし');
      fillWishesFromAI(d);
      box.innerHTML = `<div class="ai-card p-3"><div class="flex gap-2 flex-wrap mb-2">${badge(d.source === 'llm' ? 'AI(API)' : 'ルールベース', d.source === 'llm' ? 'success' : 'warning')}
        ${d.target_income ? `<span class="stat-pill">目標 ${yen(d.target_income)}</span>` : ''}
        ${d.need_hours ? `<span class="stat-pill">必要 ${d.need_hours}h</span>` : ''}
        ${ng ? `<span class="stat-pill">NG ${ng}</span>` : ''}
        <span class="stat-pill">希望時間帯 ${slotTxt}</span></div>
        <div style="font-size:.88rem;line-height:1.7;white-space:pre-wrap">${esc(d.reason)}</div></div>`;
    } catch (e) { box.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`; }
    finally { setLoading(false); }
  });
  document.getElementById('submitWish')?.addEventListener('click', async () => {
    const shifts = [];
    // 秒なし "YYYY-MM-DDTHH:MM" → "YYYY-MM-DDTHH:MM:00" に正規化（サーバーパース対応）
    const normDt = (dt) => /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(dt || '') ? dt + ':00' : dt;
    Object.entries(wishState).forEach(([day, w]) => {
      if (w.type === 'rest') return;
      if (w.type === 'time') shifts.push({ start_datetime: normDt(w.start), end_datetime: normDt(w.end) });
      else shifts.push({ start_datetime: `${day}T09:00:00`, availability: w.type });
    });
    if (!shifts.length) { toast('希望を選択してください', 'error'); return; }
    try {
      const d = await api('/staff/requests', { method: 'POST', body: JSON.stringify({ shifts }) });
      document.getElementById('wishResult').innerHTML = `<div class="alert alert-success py-2">${d.submitted}件の希望を提出しました</div>`;
      wishState = {}; drawWish(); loadMyReqs();
    } catch (e) { document.getElementById('wishResult').innerHTML = `<div class="alert alert-danger py-2">${esc(e.message)}</div>`; }
  });
  const loadMyReqs = async () => {
    try {
      const d = await api('/staff/requests');
      document.getElementById('myReqs').innerHTML = d.requests.length ? d.requests.map((r) => `<div class="list-row"><div><strong class="num">${esc(r.start_datetime.slice(5,10))} ${hm(r.start_datetime)}-${hm(r.end_datetime)}</strong> ${badge('調整待ち','warning')}</div><button class="btn btn-sm btn-outline-danger" data-cancel="${r.id}"><i class="bi bi-x"></i></button></div>`).join('') : '<div class="small text-secondary">提出済みの希望はありません</div>';
      document.getElementById('myReqs').querySelectorAll('[data-cancel]').forEach((b) => b?.addEventListener('click', async () => { await api(`/staff/requests/${b.dataset.cancel}`, { method: 'DELETE' }); loadMyReqs(); }));
    } catch {}
  };
  drawWish(); loadMyReqs();
};

SCREENS.staffSettings = function (el) {
  el.innerHTML = pageHead('アカウント設定', 'bi-person-gear') +
    card(sectionTitle('bi-key', 'パスワード変更') +
      `<label class="form-label" for="pwCur">現在のパスワード</label><input type="password"  id="pwCur" class="form-control mb-2">
       <label class="form-label" for="pwNew">新しいパスワード（8文字以上・英数字）</label><input type="password"  id="pwNew" class="form-control mb-2">
       <label class="form-label" for="pwConf">新しいパスワード（確認）</label><input type="password"  id="pwConf" class="form-control mb-3">
       <button class="btn btn-primary btn-lg w-full" id="pwBtn">変更</button>`);
  el.querySelector('#pwBtn')?.addEventListener('click', async () => {
    if (el.querySelector('#pwNew').value !== el.querySelector('#pwConf').value) { toast('確認用が一致しません', 'error'); return; }
    try { await api('/staff/password', { method: 'PUT', body: JSON.stringify({ current_password: el.querySelector('#pwCur').value, new_password: el.querySelector('#pwNew').value }) }); toast('変更しました', 'success'); }
    catch (e) { toast(e.message, 'error'); }
  });
};

/* ============================================================
   Admin Screens
   ============================================================ */
SCREENS.adminHome = function (el) {
  el.innerHTML = pageHead('システム管理者', 'bi-shield-lock', currentUser.name) +
    card(`<button class="btn btn-primary btn-lg w-full" id="goShops"><i class="bi bi-shop"></i> 店舗一覧へ</button>`);
  document.getElementById('goShops')?.addEventListener('click', () => navigateTo('adminShops'));
};
SCREENS.adminShops = async function (el) {
  el.innerHTML = pageHead('店舗一覧', 'bi-shop') +
    card(`<div class="flex justify-between items-center mb-3">${sectionTitle('bi-shop', '店舗一覧')}<button class="btn btn-primary btn-sm" id="addShopBtn"><i class="bi bi-plus-lg"></i></button></div><div id="shopList"></div>`);
  const load = async () => {
    const d = await api('/admin/shops');
    document.getElementById('shopList').innerHTML = d.shops.length ? (await Promise.all(d.shops.map(async (s) => {
      let st = { staff_count: '-', confirmed_count: '-' };
      try { st = await api(`/admin/shops/stats/${s.id}`); } catch {}
      return `<div class="list-row" style="cursor:pointer" data-detail="${s.id}"><div><strong>${esc(s.shop_name)}</strong> <span class="text-secondary">${esc(s.shop_code)}</span> ${badge(s.is_active ? '有効' : '無効', s.is_active ? 'success' : 'warning')}<div class="small text-secondary">スタッフ${st.staff_count}名 / 確定${st.confirmed_count}件</div></div><button class="btn btn-sm btn-light" data-toggle="${s.id}" data-active="${s.is_active}">${s.is_active ? '無効化' : '有効化'}</button></div>`;
    }))).join('') : emptyState('bi-shop', '店舗がありません');
    document.getElementById('shopList').querySelectorAll('[data-detail]').forEach((b) => b?.addEventListener('click', (ev) => { if (ev.target.closest('[data-toggle]')) return; window._adminShopId = +b.dataset.detail; navigateTo('adminShopDetail'); }));
    document.getElementById('shopList').querySelectorAll('[data-toggle]').forEach((b) => b?.addEventListener('click', async (ev) => { ev.stopPropagation(); await api(`/admin/shops/${b.dataset.toggle}`, { method: 'PUT', body: JSON.stringify({ is_active: b.dataset.active !== '1', shop_name: '' }) }); load(); }));
  };
  load();
  document.getElementById('addShopBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-plus-lg"></i> 店舗追加',
      `<label class="form-label" for="shCode">店舗コード</label><input id="shCode" class="form-control mb-2"><label class="form-label" for="shName">店舗名</label><input id="shName" class="form-control mb-2"><label class="form-label" for="shPw">パスワード</label><input id="shPw" class="form-control">`,
      async (w, close) => { try { await api('/admin/shops', { method: 'POST', body: JSON.stringify({ shop_code: w.querySelector('#shCode').value, shop_name: w.querySelector('#shName').value, password: w.querySelector('#shPw').value }) }); close(); toast('追加しました', 'success'); load(); } catch (e) { toast(e.message, 'error'); } }));
};
SCREENS.adminShopDetail = async function (el) {
  const sid = window._adminShopId;
  const shop = (await api('/admin/shops')).shops.find((x) => x.id === sid) || { shop_name: '店舗#' + sid, shop_code: '' };
  el.innerHTML = pageHead(shop.shop_name, 'bi-shop', shop.shop_code) +
    card(`<button class="btn btn-sm btn-light mb-2" id="backBtn"><i class="bi bi-arrow-left"></i> 戻る</button>
      <div class="row mb-3"><div class="col-5"><label class="form-label" for="dStart">開始</label><input type="date"  id="dStart" class="form-control"></div><div class="col-5"><label class="form-label" for="dEnd">終了</label><input type="date"  id="dEnd" class="form-control"></div><div class="col-2 flex items-end"><button class="btn btn-primary w-full" id="loadBtn">表示</button></div></div>
      <div id="detailBody"><div class="text-secondary small">期間を指定してください</div></div>`);
  document.getElementById('backBtn')?.addEventListener('click', () => navigateTo('adminShops'));
  document.getElementById('loadBtn')?.addEventListener('click', () => loadDetail());
  api(`/admin/shops/${sid}/periods/next`).then((p) => { dStart.value = p.start_date; dEnd.value = p.end_date; loadDetail(); }).catch(() => {});
  async function loadDetail() {
    const start = dStart.value, end = dEnd.value; if (!start || !end) return;
    const body = document.getElementById('detailBody'); body.innerHTML = '<div class="text-secondary small">読み込み中...</div>';
    try {
      const [sum, st] = await Promise.all([api(`/admin/shops/summary/${sid}?start=${start}&end=${end}`), api(`/admin/shops/staffs/${sid}`)]);
      const tbl = sum.staff.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>氏名</th><th>日</th><th class="t-num">確定</th><th class="t-num">給与</th></tr></thead><tbody>${sum.staff.map((s) => `<tr><td>${esc(s.name)}</td><td>${s.days}</td><td class="t-num num">${s.confirmed_hours}h</td><td class="t-num num">${yen(s.pay)}</td></tr>`).join('')}<tr style="font-weight:800;color:var(--indigo-l)"><td>合計</td><td></td><td class="t-num num">${sum.total_hours}h</td><td class="t-num num">${yen(sum.total_pay)}</td></tr></tbody></table></div>` : '<div class="small text-secondary">確定シフトなし</div>';
      const slist = st.staffs.map((s) => `<div class="list-row"><div class="staff-cell"><span class="staff-name">${esc(s.name)}</span><span class="staff-sub">${esc(s.staff_code)} ・ ${s.role === 'employee' ? '社員' : 'バイト'}${s.is_resigned ? ' ・ 退職' : ''}</span></div></div>`).join('');
      body.innerHTML = sectionTitle('bi-people', `スタッフ（${st.staffs.length}名）`) + slist + `<hr style="border-color:var(--line);margin:16px 0">` + sectionTitle('bi-bar-chart', `集計（${start}〜${end}）`) + tbl;
    } catch (e) { body.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`; }
  }
};
