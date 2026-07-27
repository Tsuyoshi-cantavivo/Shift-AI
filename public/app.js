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
let _navToken = 0;  // 現在画面のトークン（高速遷移で前画面の非同期更新を破棄するため）

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

/* 現在画面が生きているか確認するガード関数。
   高速遷移で前画面の async 処理が DOM を書き換えるのを防ぐために使う。
   token を省略した場合は _navToken（最新）と比較し、移動済みなら false を返す。
   例:
     const tok = navToken();
     const data = await api(...);
     if (!isAlive(tok)) return;  // 既に別画面へ遷移済み → DOM更新中止
     el.innerHTML = ...;
*/
function navToken() { return _navToken; }
function isAlive(token) { return token === _navToken; }

/* 安全な innerHTML setter: 要素が null/undefined または画面遷移済みなら何もしない。
   DOM破棄後の更新を根本防止（"Cannot set properties of null (setting 'innerHTML')" 回避）。 */
function safeSetHTML(el, html) {
  if (!el || !el.isConnected) return false;
  try { el.innerHTML = html; return true; }
  catch (e) { console.warn('[ShiftAI] safeSetHTML failed:', e?.message || e); return false; }
}

/* 安全な querySelector: element が null なら null を返す（オプショナルチェーンの糖衣）。 */
function $q(parent, selector) {
  if (!parent) return null;
  try { return parent.querySelector(selector); } catch { return null; }
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
  // 代理閲覧の状態も前セッションのものなので消す（残すと次ログインで誤表示になる）
  window._impersonating = null;
  renderImpersonationBar();
  document.getElementById('loginView')?.classList.remove('d-none');
  document.getElementById('appView')?.classList.add('d-none');
}

const WD = ['日', '月', '火', '水', '木', '金', '土'];
function wdName(d) { return WD[new Date(d + 'T00:00:00').getDay()]; }
/* ISO datetime から時分を抽出。ゼロ埋め無し "T7:00:00" 等にも対応。
   【背景】過去バージョンでDBに "2026-08-01T7:00:00" のような
   非ゼロ埋め時刻が保存されていた。slice(11,16) だと "7:00:" に化ける
   ため正規化してから抽出する。 */
function hm(iso) {
  if (!iso) return '--:--';
  const t = String(iso).slice(11);
  const m = t.match(/^(\d{1,2}):(\d{2})/);
  if (!m) return '--:--';
  return `${m[1].padStart(2, '0')}:${m[2]}`;
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
/** CSS変数の現在値を読む。テーマ切替後は値が変わるので、描画のたびに呼ぶこと。 */
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** 不透明度を落とした色を作る。Chart.js の塗りに使う。
 *  Chart.js は canvas に描画するため CSS 関数（color-mix 等）を解釈できない。
 *  トークンの hex を読んで rgba() の文字列に変換する。 */
function cssVarAlpha(name, alpha) {
  const hex = cssVar(name).replace('#', '');
  if (hex.length !== 6) return cssVar(name);   // 想定外の形式ならそのまま返す
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}
/** スタッフのロールから配置帯・チップの色クラスを決める。
 *  色は staffs.role を表す。寒色＝常勤（店長・社員）、暖色＝非常勤（パート・学生）。
 *  未知の値や欠損は社員扱いにフォールバックする（色が消えるより誤色のほうが害が小さい）。 */
function roleClass(role) {
  switch (role) {
    case 'manager':   return 'role-manager';
    case 'employee':  return 'role-employee';
    case 'part_time': return 'role-part-time';
    case 'student':   return 'role-student';
    default:          return 'role-employee';
  }
}

/** 配置帯のバッジ・凡例で使う短いロール名。狭いスペースに収めるため。
 *  ヘッダー等で使う正式名称は roleLabel() 側（店舗管理者/社員/アルバイト/学生アルバイト）。 */
function roleBadgeLabel(role) {
  switch (role) {
    case 'manager':   return '店長';
    case 'employee':  return '社員';
    case 'part_time': return 'パート';
    case 'student':   return '学生';
    default:          return '';
  }
}

/** シフトの状態を質感クラスに変換する。色はロールが担うため、状態は模様で表す。
 *  confirmed=ベタ塗り / modifying=斜線 / requested=淡く破線枠 */
function statusClass(status) {
  switch (status) {
    case 'confirmed': return 'tl-st-confirmed';
    case 'modifying': return 'tl-st-modifying';
    case 'requested': return 'tl-st-requested';
    default:          return 'tl-st-confirmed';
  }
}

/** 不足の時間帯を一文にまとめる。色と模様だけに頼らず言葉でも届けるため。
 *  引数は _mergeHourlyGaps() の戻り値 [{start, end, gap}]（start/end は拡張時間の整数。25=翌1時）。
 *  例: 「22:00–翌02:00 が 1名不足」 */
function gapSummaryText(merged) {
  if (!merged.length) return '';
  const t = (h) => (h >= 24 ? `翌${_extHourLabel(h)}:00` : `${_extHourLabel(h)}:00`);
  const head = merged.slice(0, 2)
    .map((g) => `${t(g.start)}–${t(g.end)} が ${g.gap}名不足`)
    .join('、');
  return merged.length > 2 ? `${head}（ほか${merged.length - 2}件）` : head;
}

function yen(n) { return '¥' + (n || 0).toLocaleString(); }
function buzz(ms = 8) { try { navigator.vibrate?.(ms); } catch (e) {} }

function todayStr() { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function plusMonths(n) { const d = new Date(); d.setMonth(d.getMonth() + n); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function isPC() { return window.matchMedia('(min-width: 992px)').matches; }

/* Date をローカル日付の "YYYY-MM-DD" で返す（toISOString は UTC になるので NG）。 */
function _localDateStr(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
/* 拡張時間（0-47）を表示用文字列に。翌日なら "(翌)HH"。 */
function _fmtExtHour(h) {
  return h >= 24 ? `(翌)${String(h - 24).padStart(2, '0')}` : String(h).padStart(2, '0');
}

/* Toast */
function toast(msg, type = 'info') {
  const wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = `toast show ${type}`;
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

/* ロールコード → 日本語表示（manager/employee/part_time/student に対応） */
function roleLabel(role) {
  return role === 'manager' ? '店舗管理者'
    : role === 'employee' ? '社員'
    : role === 'student' ? '学生アルバイト'
    : 'アルバイト';
}

/* 学生アルバイトの月間上限 */
const STUDENT_MAX_HOURS = 80;

/* Modal */
function openModal(title, bodyHtml, onSave, opts = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'modal-overlay';
  const saveLabel = opts.saveLabel || '保存';
  const btnClass = opts.btnClass || 'btn-primary';
  wrap.innerHTML = `
    <div class="modal-box" style="${opts.width ? 'max-width:' + opts.width + 'px' : ''}">
      <div class="modal-header">
        <div class="modal-title">${title}</div>
        <button class="modal-close" data-x><i class="bi bi-x-lg"></i></button>
      </div>
      <div class="modal-body">${bodyHtml}</div>
      <div class="modal-footer">
        <button class="btn btn-light" data-x>キャンセル</button>
        ${onSave ? `<button class="btn ${btnClass}" data-save>${saveLabel}</button>` : ''}
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

// 起動時・ログイン時に /api/me を叩いて正確な権限情報を取得
async function refreshMyStaffInfo() {
  if (!authToken) { window._myStaffInfo = null; return; }
  try {
    const d = await api('/me');
    window._myStaffInfo = d.staff_info || null;
    setActiveNav();
  } catch { window._myStaffInfo = null; }
}

function showApp() {
  document.getElementById('loginView')?.classList.add('d-none');
  document.getElementById('appView')?.classList.remove('d-none');
  renderNav();
  // 自分の権限情報を正確に取得（非同期・画面遷移は待たない）
  refreshMyStaffInfo();
  // 店舗の場合（代理閲覧中も含む）は期間・営業時間を事前取得してから画面へ
  if (effectiveRole() === 'shop') {
    Promise.all([ensurePeriod(), ensureBusinessHours()]).then(() => navigateTo(defaultScreen()));
  } else {
    navigateTo(defaultScreen());
  }
}
function defaultScreen() {
  // 代理閲覧中は currentRole が admin のままでも店舗のダッシュボードを開く
  // （リロード直後にナビだけ店舗用でホームが管理者画面のままになるのを防ぐ）
  const role = effectiveRole();
  if (role === 'shop') return 'dashboard';
  if (role === 'staff') return 'staffDashboard';
  if (role === 'admin') return 'adminHome';
  return 'dashboard';
}

/* ============================================================
   Login (単一フォーム: 店舗コード + ユーザーコード + パスワード)
   - ユーザーコード "admin" → システム管理者
   - ユーザーコード "manager" (role='manager') → 店舗管理者
   - その他 → 一般スタッフ
   ============================================================ */
document.getElementById('loginBtn')?.addEventListener('click', async () => {
  const shopCode = document.getElementById('loginShopCode').value.trim();
  const userCode = document.getElementById('loginUserCode').value.trim();
  const pw = document.getElementById('loginPassword').value;
  const errEl = document.getElementById('loginError');
  errEl.textContent = ''; setLoading(true);
  try {
    if (!shopCode || !userCode || !pw) {
      throw new Error('店舗コード・ユーザーコード・パスワードを入力してください');
    }
    const data = await api('/login', {
      method: 'POST',
      body: JSON.stringify({ shop_code: shopCode, user_code: userCode, password: pw })
    });
    authToken = data.token; currentUser = data.user; currentRole = data.role;
    window._miniChat = null;
    window._shopChat = null;
    window._shiftCalCtrl = null;
    localStorage.setItem('shift_token', authToken);
    showApp();
  } catch (e) { errEl.textContent = e.message; }
  finally { setLoading(false); }
});
['loginShopCode', 'loginUserCode', 'loginPassword'].forEach((id) => {
  document.getElementById(id)?.addEventListener('keydown', (e) => {
    // IME変換中のEnterは確定扱いとして送信しない（念のため）
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) document.getElementById('loginBtn').click();
  });
});
document.getElementById('logoutBtn')?.addEventListener('click', async () => { try { await api('/logout', { method: 'POST' }); } catch {} logoutLocal(); });
document.getElementById('notifBtn')?.addEventListener('click', () => openNotifications());

/* ============================================================
   Theme (dark/light) toggle
   ============================================================ */
function currentTheme() { return document.documentElement.getAttribute('data-theme') || 'light'; }
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
  try { localStorage.setItem('shiftai_theme', t); } catch (e) {}
  const icon = document.querySelector('#themeToggleBtn i');
  if (icon) icon.className = (t === 'light') ? 'bi bi-sun-fill' : 'bi bi-moon-stars-fill';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', t === 'light' ? '#FBFAF6' : '#262624');
}
applyTheme(currentTheme()); // アイコンとmetaを現在テーマに同期
document.getElementById('themeToggleBtn')?.addEventListener('click', () => {
  applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
  // チャートは生成時の色を保持するため、テーマが変わったら現在画面を描き直す。
  // navigateTo() は先頭でチャート破棄（destroy）を行うため、ここでの再破棄は不要。
  if (typeof navigateTo === 'function' && currentScreen) navigateTo(currentScreen);
});
document.getElementById('menuToggle')?.addEventListener('click', () => {
  document.getElementById('sideNav')?.classList.toggle('open');
  document.getElementById('sideOverlay')?.classList.toggle('d-none');
});
document.getElementById('sideOverlay')?.addEventListener('click', () => {
  document.getElementById('sideNav')?.classList.remove('open');
  document.getElementById('sideOverlay')?.classList.add('d-none');
});

/* ============================================================
   代理閲覧（管理者が店舗画面を閲覧のみで開く）
   状態は /api/me の impersonating をそのまま window._impersonating に保持する。
   リロードしても /api/me から復元されるため、ページ内変数だけで完結する。
   ============================================================ */
window._impersonating = null;

/* 代理閲覧中は currentRole が 'admin' のままでも実際には店舗として振る舞う
   （Task 6 の設計：/api/admin/* と /api/me を管理者のまま動かすため）。
   ナビ・デフォルト画面・通知・営業時間取得など「今どの役割として動くか」を
   判定する箇所は currentRole を直接見ず、必ずこちらを使うこと。
   【背景】レビューで、この判定漏れにより営業時間9-22時のダミー値が代理閲覧中の
   ダッシュボードに表示される・通知バッジが常に管理者の空スタブを見てしまう、
   という実害が具体的に確認された。 */
function effectiveRole() {
  return window._impersonating ? 'shop' : currentRole;
}

// バーの実高を監視する ResizeObserver。renderImpersonationBar() を呼ぶたびに
// 必ず破棄してから作り直す（多重登録・古いバーへのゾンビ更新を防ぐため）。
let _impBarObserver = null;

function renderImpersonationBar() {
  const existing = document.getElementById('impersonationBar');
  if (existing) existing.remove();
  if (_impBarObserver) { _impBarObserver.disconnect(); _impBarObserver = null; }
  // 高さオフセットも一旦リセット（非表示時は --header-h と同値に戻す）
  document.documentElement.classList.remove('has-impersonation-bar');
  document.documentElement.style.removeProperty('--imp-bar-h');
  const info = window._impersonating;
  if (!info) return;
  const header = document.querySelector('.app-header');
  if (!header) return;
  const bar = document.createElement('div');
  bar.id = 'impersonationBar';
  bar.className = 'impersonation-bar';
  bar.innerHTML = `<span><i class="bi bi-eye"></i> ${esc(info.shop_name)} を代理閲覧中（閲覧のみ・変更はできません）</span>` +
    `<button class="btn btn-sm btn-light" id="stopImpersonateBtn">管理者に戻る</button>`;
  header.insertAdjacentElement('afterend', bar);
  document.getElementById('stopImpersonateBtn')?.addEventListener('click', stopImpersonation);
  document.documentElement.classList.add('has-impersonation-bar');
  // side-nav / side-overlay がこのバーと重ならないよう、実測した高さで --imp-bar-h を
  // 上書きする。offsetHeight の一回読みだけだと、
  //   ・呼び出し時点で祖先の #appView に d-none が付いていて未レイアウト（0を測ってしまう）
  //   ・ウィンドウ幅が変わってテキストが折り返し、バーの実高が変わる
  // の2ケースで値がずれたまま固定される（レビューで実機確認済み）。ResizeObserver で
  // バー自身の box を監視し、#appView が可視化された瞬間・折り返しで高さが変わった
  // 瞬間の両方で測り直す。
  const applyHeight = () => {
    // 別の renderImpersonationBar() 呼び出しで既にこのバーが破棄されている場合、
    // 遅延実行（rAF）の間に古い bar を測って新しいバーの高さを上書きしてしまう
    // （ゾンビ更新）ことがあるため、DOM に残っているかを確認してから書き込む。
    if (!bar.isConnected) return;
    document.documentElement.style.setProperty('--imp-bar-h', bar.offsetHeight + 'px');
  };
  applyHeight();
  if (typeof ResizeObserver !== 'undefined') {
    // ResizeObserver のコールバック内で同期的にレイアウトへ影響する書き込みを行うと、
    // ブラウザが「ResizeObserver loop completed with undelivered notifications」という
    // 無害だが煩わしい警告を error イベントとして発生させることがある
    // （本アプリはグローバルエラーハンドラで window の error を全てトースト表示するため、
    // このままだと代理閲覧の開始/終了を素早く繰り返しただけでエラートーストが出てしまう）。
    // 次フレームまで書き込みを遅延させるのが定石の回避策。
    _impBarObserver = new ResizeObserver(() => requestAnimationFrame(applyHeight));
    _impBarObserver.observe(bar);
  }
}

async function stopImpersonation() {
  try {
    await api('/admin/impersonate', { method: 'DELETE' });
    window._impersonating = null;
    renderImpersonationBar();
    renderNav();
    navigateTo('adminShops');
    toast('管理者に戻りました', 'success');
  } catch (e) { toast(e.message, 'error'); }
}

(async function bootstrap() {
  if (authToken) {
    try {
      const data = await api('/me'); currentUser = data.user; currentRole = data.role;
      // リロードしても代理閲覧中であることが分かるよう、/api/me の結果から復元する
      window._impersonating = data.impersonating || null;
      renderImpersonationBar();
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
  // 代理閲覧中は 'admin' ではなく 'shop' の通知APIを見る（管理者向けは常に空の
  // スタブを返すため、代理中のままだと店舗の実際の未読件数が拾えない）。
  const role = effectiveRole();
  // 管理者自身（代理閲覧していない状態）の通知ベルには「未読」という概念が
  // 無い。GET /api/admin/notifications は Task 14 で一斉通知の配信履歴
  // {announcements:[...]} を返す実装に変わり、unread を持たなくなったため、
  // d.unread を参照すると常に undefined になる（バッジが出ないだけで実害は
  // 無いが、意図を明示するため早期リターンする）。
  if (role === 'admin') {
    document.getElementById('notifBtn')?.classList.remove('d-none');
    document.getElementById('notifBadge')?.classList.add('d-none');
    const adminSideBadge = document.getElementById('sideNotifBadge');
    if (adminSideBadge) adminSideBadge.style.display = 'none';
    return;
  }
  try {
    const d = await api(`/${role}/notifications`);
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
    if (role === 'shop') {
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
  const role = effectiveRole();
  // 管理者自身（代理閲覧していない状態）のベルは「配信履歴」を出す。
  // GET /api/admin/notifications は {announcements:[...]}（一斉通知の履歴）を
  // 返し、店舗/スタッフ向けの {notifications:[...], unread} とは形が違うため、
  // d.notifications.length に触れると必ず例外になる。ここで個別に分岐して回避する。
  // 既読/未読の概念が無いため「すべて既読にする」ボタンは出さない（配信した
  // 側の履歴を眺めるだけの画面という位置づけ）。
  if (role === 'admin') {
    api('/admin/notifications').then((d) => {
      const items = d.announcements || [];
      const listHtml = items.length ? items.map((a) => `
        <div class="notif-item">
          <div class="nt-title">${esc(a.title || '')}</div>
          <div class="nt-body">配信先 ${a.shops} 店舗 / 個人宛 ${a.recipients || 0} 名</div>
          <div class="nt-time">${esc((a.created_at || '').replace('T', ' '))}</div>
        </div>`).join('') : '<div class="text-muted small">配信履歴はありません</div>';
      openModal('<i class="bi bi-bell"></i> 通知', listHtml, null);
    }).catch(() => {
      openModal('<i class="bi bi-bell"></i> 通知', '<div class="text-muted small">通知はありません</div>', null);
    });
    return;
  }
  api(`/${role}/notifications`).then((d) => {
    const renderList = (notifs) => notifs.length ? notifs.map((n) => `
      <div class="notif-item ${n.is_read ? '' : 'unread'}">
        <div class="nt-title">${esc(n.title)}</div>
        <div class="nt-body">${esc(n.body || '')}</div>
        <div class="nt-time">${esc((n.created_at || '').replace('T', ' ').slice(0, 16))}</div>
      </div>`).join('') : '<div class="text-muted small">通知はありません</div>';
    const w = openModal('<i class="bi bi-bell"></i> 通知', renderList(d.notifications) + (d.notifications.length ? '<button class="btn btn-light w-full mt-3" id="readAllBtn">すべて既読にする</button>' : ''), null);
    if (d.notifications.length) {
      w.querySelector('#readAllBtn')?.addEventListener('click', async () => {
        await api(`/${role}/notifications/read-all`, { method: 'PUT' });
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
    { key: 'myshift', icon: 'bi-calendar2-check', label: 'マイシフト・希望' },
    { key: 'requests', icon: 'bi-inbox', label: '希望表管理' },
    { key: 'analytics', icon: 'bi-graph-up-arrow', label: '人件費分析' },
    { key: 'notifications', icon: 'bi-bell', label: '通知' },
    { key: 'settings', icon: 'bi-gear', label: '設定', mobile: true },
  ],
  staff: [
    { key: 'staffDashboard', icon: 'bi-house-door', label: 'ホーム', mobile: true },
    { key: 'staffMyshift', icon: 'bi-calendar-check', label: 'マイシフト', mobile: true },
    { key: 'request', icon: 'bi-pencil-square', label: '希望提出', mobile: true },
    { key: 'staffSettings', icon: 'bi-person-gear', label: '設定', mobile: true },
  ],
  admin: [
    { key: 'adminHome', icon: 'bi-house-door', label: 'ホーム', mobile: true },
    { key: 'adminShops', icon: 'bi-shop', label: '店舗', mobile: true },
    { key: 'adminAudit', icon: 'bi-clipboard-data', label: '監査ログ', mobile: true },
    { key: 'adminSystem', icon: 'bi-gear', label: 'システム', mobile: true },
  ],
};

function renderNav() {
  // 代理閲覧中は店舗のナビを出す。管理者に戻るのは警告バーのボタンから。
  const defs = NAV_DEFS[effectiveRole()] || [];
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
      <i class="bi ${it.icon}"></i><span>${it.label.replace('AIシフト作成', 'AI作成').replace('ダッシュボード', 'ホーム')}</span>
    </button>`).join('');
  bn.querySelectorAll('.bn-item').forEach((b) => b?.addEventListener('click', () => navigateTo(b.dataset.screen)));
}

function setActiveNav() {
  document.querySelectorAll('.side-item, .bn-item').forEach((b) => b.classList.toggle('active', b.dataset.screen === currentScreen));
  // renderNav() と同じ effectiveRole() を使う（代理閲覧中は店舗画面のキーで画面タイトルを引く）
  const defs = NAV_DEFS[effectiveRole()] || [];
  const label = defs.find((i) => i.key === currentScreen)?.label || 'ShiftAI';
  const titleEl = document.getElementById('headerTitle');
  if (titleEl) {
    // 現在の権限を正確に判定して表示
    let roleDisp = '';
    if (currentRole === 'shop') {
      // /api/me の is_manager フラグで正確に判定（未取得時は shop_name から推定）
      const isMgr = window._myStaffInfo?.is_manager === true;
      const hasStaff = !!window._myStaffInfo;
      if (isMgr) roleDisp = '【店舗管理者】';
      else if (hasStaff) roleDisp = `【店舗(${roleLabel(window._myStaffInfo.role)})】`;
      else roleDisp = '【店舗（旧仕様）】';
    } else if (currentRole === 'admin') {
      roleDisp = '【システム管理者】';
    } else if (currentRole === 'staff') {
      roleDisp = `【スタッフ:${roleLabel(currentUser?.role)}】`;
    }
    titleEl.textContent = label + (roleDisp ? ' ' + roleDisp : '');
  }
}


function navigateTo(screen) {
  // 画面遷移トークンをインクリメント → 前画面の async 処理が isAlive(tok) で自我判断できる
  _navToken++;
  // Destroy charts on navigation
  Object.values(chartInstances).forEach((c) => { try { c.destroy(); } catch {} });
  chartInstances = {};
  // Close all open modals
  document.querySelectorAll('.modal-overlay').forEach((m) => m.remove());
  currentScreen = screen;
  setActiveNav();
  const content = document.getElementById('content');
  if (!content) return;
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

/* ============================================================
   時間処理ヘルパ（日またぎ営業対応）
   ・ensureBusinessHours で end < start の overnight パターンは end += 24 で拡張
   ・extended hour 空間（0-47）で営業時間・シフト時間を扱う
   ・表示は h % 24、日付計算は anchorDate との差分で処理
   ============================================================ */
function _dateDiffDays(fromStr, toStr) {
  // "YYYY-MM-DD" 同士の日数差（to - from）。同じ日なら 0。
  const a = new Date(fromStr + 'T00:00:00');
  const b = new Date(toStr + 'T00:00:00');
  return Math.round((b - a) / (24 * 60 * 60 * 1000));
}

function _extMinFromIso(iso, anchorDate) {
  // iso を anchorDate 基準の「拡張分」に変換。翌日なら +1440。
  // 【時刻パースは正規化して行う】"T7:00:00" のような非ゼロ埋めでもOK
  const t = String(iso || '').slice(11);
  const m = t.match(/^(\d{1,2}):(\d{2})/);
  if (!m) return NaN;  // パース失敗時は NaN で伝播（呼び出し元でisNaN判定）
  const h = +m[1];
  const mn = +m[2];
  const isoDate = (iso || '').slice(0, 10);
  const diff = anchorDate ? _dateDiffDays(anchorDate, isoDate) : 0;
  return (h + diff * 24) * 60 + mn;
}

function _extHourLabel(h) {
  // 拡張時間 → 表示用文字列。25時=翌1時、29時=翌5時
  const hh = h % 24;
  return String(hh).padStart(2, '0');
}

function _extHourToIsoTime(h, m, anchorDate) {
  // 拡張時間(HH+日付) → { date: "YYYY-MM-DD", time: "HH:MM" }
  const dayOffset = Math.floor(h / 24);
  const hh = String(h % 24).padStart(2, '0');
  const mm = String(m || 0).padStart(2, '0');
  let dateStr = anchorDate;
  if (dayOffset > 0) {
    const d = new Date(anchorDate + 'T00:00:00');
    d.setDate(d.getDate() + dayOffset);
    dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  return { date: dateStr, time: `${hh}:${mm}` };
}

/* "H:MM" / "HH:MM" → 時刻数値(時,分)。不正時は NaN。 */
function _parseTimeParts(t) {
  const m = String(t || '').match(/^(\d{1,2}):(\d{2})/);
  if (!m) return [NaN, NaN];
  return [+m[1], +m[2]];
}

/* 店舗の営業時間をパターン（shift_patterns）の最小開始/最大終了から算出してキャッシュ。
   タイムライン表示で「日によって時間軸が変わる」のを防ぎ、営業時間全体を固定表示する。
   【日またぎ対応】end_time <= start_time の overnight パターンは end に +24 する。
   【時刻パース堅牢化】"7:00" のような非ゼロ埋め時刻でも正しく解析する。 */
async function ensureBusinessHours() {
  // 【キャッシュ戦略】毎回 /shop/patterns を取り直して計算する。
  // パターン編集後にキャッシュが古くなり「9-19」等の古い表示に固定される
  // デグレを防ぐため。API は軽量なので毎回呼び出しでも実用上問題ない。
  const fallback = { start: 9, end: 22 };
  // currentRole ではなく effectiveRole() で判定する。代理閲覧中は currentRole が
  // 'admin' のままのため、ここを currentRole のままにすると /shop/patterns を
  // 一切呼ばずに 9-22時のダミー値を返してしまい、代理閲覧中のダッシュボード・
  // シフトカレンダーのタイムライン軸が実際の営業時間からずれる（レビューで実機確認済み）。
  const role = effectiveRole();
  if (role !== 'shop' && role !== 'staff') return fallback;
  try {
    const d = await api('/shop/patterns');
    const pats = d.patterns || [];
    appState.patterns = pats;
    if (!pats.length) { appState.businessHours = fallback; return fallback; }
    let start = 48, end = 0;
    pats.forEach((p) => {
      const [sh] = _parseTimeParts(p.start_time);
      let [peH, peM] = _parseTimeParts(p.end_time);
      if (isNaN(sh) || isNaN(peH)) return;  // 不正時刻は無視
      // overnight (翌日またぎ): end <= start なら翌日扱いで +24
      if (peH < sh || (peH === sh && peM === 0)) peH += 24;
      else if (peM > 0) peH += 1;  // 終了分がある場合は +1 時間切り上げ
      start = Math.min(start, sh);
      end = Math.max(end, peH);
    });
    if (start >= end || isNaN(start) || isNaN(end)) { appState.businessHours = fallback; return fallback; }
    appState.businessHours = { start, end };
    return appState.businessHours;
  } catch { return fallback; }
}

/* 時間帯別不足計算（タイムライン・印刷・不足通知で共通利用）
   戻り値: [{ hour, required, placed, gap }] — gap>0 の時間帯が不足
   【日またぎ対応】hour は拡張時間（0-47）。overnight シフトも正しくカウント。
   【オプション】includeRequested=true のとき、スタッフ希望(requested)も
     カバレッジに含める。AIドラフト(requested, reason='AIドラフト...')は除外。
     タイムライン表示では希望出し済みのスタッフを「配置済み相当」と扱い、
     「希望が出ているのに不足扱い」という誤表示を防ぐ。 */
function _computeHourlyGaps(shifts, dayStr, opts) {
  const includeRequested = !!(opts && opts.includeRequested);
  const pats = appState.patterns;
  if (!pats || !pats.length) return [];
  const wd = new Date(dayStr + 'T00:00:00').getDay();
  const bh = appState.businessHours || { start: 9, end: 22 };
  // 各パターンから曜日別必要人数を取得（overnight は +24 時間拡張）
  const hourReq = {}; // 拡張hour → required
  pats.forEach((p) => {
    const [ps0] = _parseTimeParts(p.start_time);
    let [pe0] = _parseTimeParts(p.end_time);
    if (isNaN(ps0) || isNaN(pe0)) return;
    const ps = ps0;
    let pe = pe0;
    if (pe <= ps) pe += 24;  // overnight
    const wr = (p.weekday_required || {});
    const req = wr[String(wd)] != null ? +wr[String(wd)] : (p.required_staff || 0);
    if (req <= 0) return;
    for (let h = ps; h < pe; h++) {
      hourReq[h] = Math.max(hourReq[h] || 0, req);
    }
  });
  // confirmed シフトで各時間帯の配置人数をカウント（overnight は +24）
  // 【includeRequested】未確定の requested（スタッフ希望・AIドラフト提案の両方）も
  // カバーに含める。ドラフト提案はその日の配置案なので、確定前でも「不足」表示から
  // 除外する（旧: AIドラフトを除外していたため、AIが埋めた枠が確定するまで
  // 「不足1名」と誤表示されていた）。
  const hourPlaced = {};
  (shifts || []).forEach((s) => {
    const isConfirmed = (s.status === 'confirmed' || s.status === 'modifying');
    const isRequestedCoverage = includeRequested && s.status === 'requested';
    if (!isConfirmed && !isRequestedCoverage) return;
    const sMin = _extMinFromIso(s.start_datetime, dayStr);
    const eMin = _extMinFromIso(s.end_datetime, dayStr);
    if (isNaN(sMin) || isNaN(eMin)) return;
    const sH = Math.floor(sMin / 60);
    const eH = Math.ceil(eMin / 60);
    for (let h = sH; h < eH; h++) {
      hourPlaced[h] = (hourPlaced[h] || 0) + 1;
    }
  });
  // 不足時間帯を返す（営業時間内のみ）
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
    const tok = navToken();
    setLoading(true);
    try {
      const from = `${state.y}-${String(state.m + 1).padStart(2, '0')}-01`;
      const to = `${state.y}-${String(state.m + 1).padStart(2, '0')}-31`;
      state.shifts = await opts.loader(from, to);
      // 画面遷移済み or DOM破棄済みなら更新中止
      if (!isAlive(tok) || !mountEl.isConnected) return;
      draw();
    } catch (e) {
      if (!isAlive(tok) || !mountEl.isConnected) return;
      safeSetHTML(mountEl, `<div class="text-danger">${esc(e.message)}</div>`);
    }
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
    const todayStr = _localDateStr(today);
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
        return `<div class="chip ${roleClass(s.staff_role)}${dashed}" title="${s.status === 'requested' ? '調整待ち' : '確定'}">${hm(s.start_datetime)}-${hm(s.end_datetime)}</div>`;
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
  const sc = roleClass(s.staff_role);
  const statusBadge = s.status === 'confirmed' ? badge('確定', 'success') : s.status === 'requested' ? badge('調整待ち', 'warning') : badge('調整中', 'info');
  const edit = editable ? `<button class="btn btn-sm btn-light edit-shift"><i class="bi bi-pencil"></i></button>` : '';
  return `<div class="shift-line">
    <div><span class="dot ${sc}"></span><span class="time">${hm(s.start_datetime)} - ${hm(s.end_datetime)}</span>${s.break_time_minutes ? `<span class="who">・休憩${esc(s.break_time_minutes)}分</span>` : ''} ${statusBadge}</div>
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
  // 後方互換：anchor 無しの日付内ローカル分
  return +iso.slice(11, 13) * 60 + +iso.slice(14, 16);
}

/** 静的タイムライン（矢印バー形式）のHTML文字列を返す純粋関数。
 *  クリック・ドラッグ等のインタラクションは持たない（編集可能な版は openDayTimeline）。
 *  印刷ビュー（openPrintView）とダッシュボード（今日の配置帯）の両方から呼ばれる。
 *  list: 表示対象のシフト群。anchorDate: 拡張時間の基準日（"YYYY-MM-DD"）。指定時は翌日またぎを正しく扱う。 */
function buildStaticTimelineHtml(list, anchorDate) {
  const day = anchorDate || (list.length ? list[0].start_datetime.slice(0, 10) : '');
  const order = []; const staffMap = {};
  list.forEach((s) => {
    if (!staffMap[s.staff_id]) {
      staffMap[s.staff_id] = { name: s.staff_name || ('#' + s.staff_id), role: s.staff_role, shifts: [] };
      order.push(s.staff_id);
    }
    staffMap[s.staff_id].shifts.push(s);
  });
  // 時間軸は「営業時間」をベースにし、シフトがはみ出す場合のみ拡張（全日で統一）。
  const bh = appState.businessHours || { start: 9, end: 22 };
  let minH = bh.start, maxH = bh.end;
  list.forEach((s) => {
    const sMin = _extMinFromIso(s.start_datetime, day);
    const eMin = _extMinFromIso(s.end_datetime, day);
    if (isNaN(sMin) || isNaN(eMin)) return;  // 不正時刻は集計から除外
    minH = Math.min(minH, Math.floor(sMin / 60));
    maxH = Math.max(maxH, Math.ceil(eMin / 60));
  });
  minH = Math.max(0, Math.floor(minH));
  maxH = Math.min(48, Math.ceil(maxH));  // 最大翌日の24時まで
  if (maxH <= minH) maxH = minH + 1;
  const rangeMin = minH * 60, rangeLen = (maxH - minH) * 60;

  const hours = [];
  for (let h = minH; h <= maxH; h++) {
    const lbl = _extHourLabel(h);
    const isNextDay = h >= 24;
    hours.push(`<div class="tl-hour${isNextDay ? ' tl-hour-next' : ''}">${lbl}</div>`);
  }

  // 配置帯のグリッド用: 表示時間数と、24:00 の位置（範囲外なら線を出さない）
  const tlHours = Math.max(1, maxH - minH);
  const dayBreakPct = ((24 * 60 - rangeMin) / rangeLen) * 100;
  const showDayBreak = dayBreakPct > 0 && dayBreakPct < 100;
  const trackVars = `--tl-hours:${tlHours};`
    + (showDayBreak ? `--tl-daybreak:${dayBreakPct.toFixed(2)}%;--tl-daybreak-display:block;` : '');

  const rows = order.map((sid) => {
    const st = staffMap[sid];
    const bars = st.shifts.map((s) => {
      const sMin = _extMinFromIso(s.start_datetime, day);
      let eMin = _extMinFromIso(s.end_datetime, day);
      if (eMin <= sMin) eMin = sMin + 60;
      // 表示範囲 [0%, 100%] にクリップ（前日/翌日へのはみ出し防止）
      const rawLeft = ((sMin - rangeMin) / rangeLen) * 100;
      const rawRight = ((eMin - rangeMin) / rangeLen) * 100;
      const left = Math.max(0, rawLeft);
      const right = Math.min(100, rawRight);
      const width = Math.max(3, right - left);
      const continued = rawLeft < 0;
      const endsOff = rawRight > 100;
      let lbl = '';
      if (width > 14) {
        if (continued && !endsOff) lbl = `→${hm(s.end_datetime)}`;
        else if (!continued && endsOff) lbl = `${hm(s.start_datetime)}→`;
        else if (continued && endsOff) lbl = `→→`;
        else lbl = `${hm(s.start_datetime)}-${hm(s.end_datetime)}`;
      } else if (width > 6) {
        lbl = `${hm(s.start_datetime)}`;
      }
      const contCls = continued ? ' tl-bar-continued' : '';
      const draftCls = (s.status === 'requested' && (s.reason || '').startsWith('AIドラフト')) ? ' tl-bar-draft' : '';
      return `<div class="tl-bar ${roleClass(s.staff_role)} ${statusClass(s.status)}${contCls}${draftCls}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">${lbl}</div>`;
    }).join('');
    return `<div class="tl-row"><div class="tl-name"><span class="tl-name-text">${esc(st.name)}</span><span class="tl-role-badge ${roleClass(st.role)}">${roleBadgeLabel(st.role)}</span></div><div class="tl-track" style="${trackVars}">${bars}</div></div>`;
  }).join('');

  // 時間帯別不足バー（印刷用）— anchorDate (day) を基準に計算
  // 印刷版でもスタッフ希望をカバー扱い（タイムラインと一貫性）
  const gaps = day ? _computeHourlyGaps(list, day, { includeRequested: true }) : [];
  let gapRowHtml = '';
  if (gaps.length) {
    const merged = _mergeHourlyGaps(gaps);
    const gapBars = merged.map((g) => {
      const left = ((g.start * 60 - rangeMin) / rangeLen) * 100;
      const width = Math.max(4, ((g.end - g.start) * 60 / rangeLen) * 100);
      // 表示用ラベル（拡張時間 → 翌日表記）
      const sLbl = g.start >= 24 ? `(翌)${_extHourLabel(g.start)}` : `${_extHourLabel(g.start)}時`;
      const eLbl = g.end >= 24 ? `(翌)${_extHourLabel(g.end)}` : `${_extHourLabel(g.end)}時`;
      return `<div class="tl-gap-bar" title="${sLbl}〜${eLbl} あと${g.gap}名" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">↓${g.gap}名不足</div>`;
    }).join('');
    gapRowHtml = `<div class="tl-row tl-gap-row"><div class="tl-name tl-gap-name">不足</div><div class="tl-track" style="${trackVars}">${gapBars}</div></div>`;
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
    <span><i class="lg-role-manager"></i>店長</span>
    <span><i class="lg-role-employee"></i>社員</span>
    <span><i class="lg-role-part-time"></i>パート</span>
    <span><i class="lg-role-student"></i>学生</span>
    <span><i class="lg-alert"></i>不足</span>
    <span class="tl-legend-note">ベタ塗り＝確定／斜線＝変更中／薄い破線＝申請中</span>
  </div>`;
}

async function openPrintView(start, end) {
  if (!start || !end) { toast('期間を指定してください'); return; }
  setLoading(true);
  try {
    const shiftsD = await api(`/shop/shifts?start=${start}&end=${end}`);
    // confirmed + AIドラフト(requested, reason='AIドラフト...') を含めて表示
    // （ドラフト状態でも確認・印刷できるようにする）
    const shifts = (shiftsD.shifts || [])
      .filter((s) => s.status === 'confirmed' || ((s.status === 'requested') && (s.reason || '').startsWith('AIドラフト')))
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
      const timeline = buildStaticTimelineHtml(list, day);
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

function isAiDraftShift(shift) {
  return shift?.status === 'requested' && String(shift.reason || '').startsWith('AIドラフト');
}

function installDraftTimelineDrag(modal, { date, list, editable, rangeMin, rangeLen }) {
  if (!editable) return;
  const undoButton = modal.querySelector('#tlDraftUndo');
  const rangeMax = rangeMin + rangeLen;
  let lastUndo = null;
  const snap15 = (minute) => Math.round(minute / 15) * 15;
  const toIso = (minute) => {
    const info = _extHourToIsoTime(Math.floor(minute / 60), minute % 60, date);
    return `${info.date}T${info.time}:00`;
  };
  const position = (minute) => ((minute - rangeMin) / rangeLen) * 100;
  const renderBar = (bar, startMinute, endMinute, startIso, endIso) => {
    bar.style.left = `${Math.max(0, position(startMinute)).toFixed(2)}%`;
    bar.style.width = `${Math.max(2, position(endMinute) - position(startMinute)).toFixed(2)}%`;
    bar.title = `${hm(startIso)}-${hm(endIso)}（ドラフト・直接調整可）`;
    const label = bar.querySelector('.tl-bar-label');
    if (label) label.textContent = `${hm(startIso)}-${hm(endIso)}`;
  };
  const showUndo = () => { if (undoButton) undoButton.hidden = !lastUndo; };
  const restoreWithError = (bar, before) => {
    renderBar(bar, before.startMinute, before.endMinute, before.startIso, before.endIso);
    bar.classList.add('tl-bar-save-error');
    window.setTimeout(() => bar.classList.remove('tl-bar-save-error'), 450);
  };
  const saveChange = async (bar, shift, before, next, rememberUndo) => {
    bar.classList.add('tl-bar-saving');
    bar.style.pointerEvents = 'none';
    try {
      const result = await api(`/shop/shifts/${shift.id}/draft-time`, {
        method: 'PATCH',
        body: JSON.stringify({
          start_datetime: next.startIso,
          end_datetime: next.endIso,
          updated_at: shift.updated_at || shift.created_at,
        }),
      });
      Object.assign(shift, result.shift);
      renderBar(bar, next.startMinute, next.endMinute, shift.start_datetime, shift.end_datetime);
      if (rememberUndo) { lastUndo = { bar, shift, before }; showUndo(); }
      toast('ドラフトを保存しました', 'success');
    } catch (error) {
      restoreWithError(bar, before);
      toast(error.message || 'ドラフトの保存に失敗しました', 'error');
    } finally {
      bar.classList.remove('tl-bar-saving');
      bar.style.pointerEvents = '';
    }
  };

  undoButton?.addEventListener('click', async () => {
    if (!lastUndo) return;
    const history = lastUndo;
    lastUndo = null;
    showUndo();
    const currentStart = _extMinFromIso(history.shift.start_datetime, date);
    const currentEnd = _extMinFromIso(history.shift.end_datetime, date);
    await saveChange(history.bar, history.shift, {
      startMinute: currentStart,
      endMinute: currentEnd,
      startIso: history.shift.start_datetime,
      endIso: history.shift.end_datetime,
    }, history.before, false);
  });

  modal.querySelectorAll('.tl-bar[data-draft-editable="true"]').forEach((bar) => {
    const shift = list.find((item) => String(item.id) === bar.dataset.id);
    if (!shift || !isAiDraftShift(shift)) return;
    let active = null;
    let longPressTimer = null;
    let pendingTouch = null;
    const clearPendingTouch = () => {
      if (longPressTimer) window.clearTimeout(longPressTimer);
      longPressTimer = null;
      pendingTouch = null;
    };
    const detachGlobalDragListeners = () => {
      window.removeEventListener('pointermove', updateDrag);
      window.removeEventListener('pointerup', finishDrag);
      window.removeEventListener('pointercancel', cancelDrag);
    };
    const beginDrag = (event, mode) => {
      const startMinute = _extMinFromIso(shift.start_datetime, date);
      const endMinute = _extMinFromIso(shift.end_datetime, date);
      if (isNaN(startMinute) || isNaN(endMinute)) return;
      const rect = bar.parentElement.getBoundingClientRect();
      active = {
        mode,
        pointerId: event.pointerId,
        pointerMinute: snap15(rangeMin + ((event.clientX - rect.left) / rect.width) * rangeLen),
        before: { startMinute, endMinute, startIso: shift.start_datetime, endIso: shift.end_datetime },
        next: null,
      };
      bar.setPointerCapture?.(event.pointerId);
      bar.classList.add('tl-bar-dragging');
      window.addEventListener('pointermove', updateDrag, { passive: false });
      window.addEventListener('pointerup', finishDrag);
      window.addEventListener('pointercancel', cancelDrag);
      event.preventDefault();
    };
    const updateDrag = (event) => {
      if (!active || event.pointerId !== active.pointerId) return;
      const rect = bar.parentElement.getBoundingClientRect();
      const pointerMinute = snap15(rangeMin + ((event.clientX - rect.left) / rect.width) * rangeLen);
      const boundedPointer = Math.max(rangeMin, Math.min(rangeMax, pointerMinute));
      let startMinute = active.before.startMinute;
      let endMinute = active.before.endMinute;
      if (active.mode === 'move') {
        const delta = boundedPointer - active.pointerMinute;
        startMinute += delta;
        endMinute += delta;
        if (startMinute < rangeMin) { endMinute += rangeMin - startMinute; startMinute = rangeMin; }
        if (endMinute > rangeMax) { startMinute -= endMinute - rangeMax; endMinute = rangeMax; }
      } else if (active.mode === 'resize-start') {
        startMinute = Math.max(rangeMin, Math.min(boundedPointer, endMinute - 15));
      } else {
        endMinute = Math.min(rangeMax, Math.max(boundedPointer, startMinute + 15));
      }
      active.next = { startMinute, endMinute, startIso: toIso(startMinute), endIso: toIso(endMinute) };
      renderBar(bar, startMinute, endMinute, active.next.startIso, active.next.endIso);
      event.preventDefault();
    };
    const finishDrag = async (event) => {
      if (!active || event.pointerId !== active.pointerId) return;
      detachGlobalDragListeners();
      clearPendingTouch();
      const state = active;
      active = null;
      bar.classList.remove('tl-bar-dragging');
      bar.dataset.skipClick = 'true';
      window.setTimeout(() => { delete bar.dataset.skipClick; }, 0);
      if (!state.next || (state.next.startIso === state.before.startIso && state.next.endIso === state.before.endIso)) {
        renderBar(bar, state.before.startMinute, state.before.endMinute, state.before.startIso, state.before.endIso);
        return;
      }
      await saveChange(bar, shift, state.before, state.next, true);
      event.preventDefault();
    };
    const cancelDrag = (event) => {
      if (!active || event.pointerId !== active.pointerId) return;
      detachGlobalDragListeners();
      clearPendingTouch();
      const before = active.before;
      active = null;
      bar.classList.remove('tl-bar-dragging');
      restoreWithError(bar, before);
      event.preventDefault();
    };

    bar.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || bar.classList.contains('tl-bar-saving')) return;
      const mode = event.target.closest('.tl-drag-handle-start') ? 'resize-start'
        : event.target.closest('.tl-drag-handle-end') ? 'resize-end' : 'move';
      if (event.pointerType === 'touch') {
        pendingTouch = { x: event.clientX, y: event.clientY };
        longPressTimer = window.setTimeout(() => beginDrag(event, mode), 300);
      } else {
        beginDrag(event, mode);
      }
    });
    bar.addEventListener('pointermove', (event) => {
      if (pendingTouch && !active && Math.hypot(event.clientX - pendingTouch.x, event.clientY - pendingTouch.y) > 8) clearPendingTouch();
    });
    bar.addEventListener('pointerup', finishDrag);
    bar.addEventListener('pointercancel', cancelDrag);
  });
}

function openDayTimeline(date, allShifts, editable, onChange) {
  buzz(12);
  // date を anchor として表示。当日タイムラインには「date で始まるシフト」のみ表示。
  // 【理由】営業日は pattern.start_time（例: 6:00）に始まるので、
  //   前日の overnight シフト（前日6:00〜当日5:00）は前日のタイムラインで見れば十分。
  //   当日のタイムラインに混ぜると左に突き抜けて名前カラムに被る問題があった。
  //   前日シフトは前日詳細画面で確認する設計。
  const list = (allShifts || []).filter((s) => s.start_datetime.slice(0, 10) === date)
    .sort((a, b) => a.start_datetime.localeCompare(b.start_datetime));
  const order = []; const staffMap = {};
  // role も保持する。設計書 §3「色だけに依存しない」に従い、名前の下にロールバッジを併記するため。
  list.forEach((s) => { if (!staffMap[s.staff_id]) { staffMap[s.staff_id] = { name: s.staff_name || ('#' + s.staff_id), role: s.staff_role, shifts: [] }; order.push(s.staff_id); } staffMap[s.staff_id].shifts.push(s); });
  // 時間軸は「営業時間」をベースにし、シフトが営業時間外にはみ出す場合のみ拡張。
  // これにより「シフトが無い時間帯が消える」「日によって軸が変わる」を防ぐ。
  // 【日またぎ】anchor=date で拡張分計算。翌日へ延びるシフトは +1440 分で計算。
  // 【NaN対策】壊れた時刻のシフトは minH/maxH 計算から除外（1件の不良で
  //   全バーが消えるインシデントの再発防止）。
  const bh = appState.businessHours || { start: 9, end: 22 };
  let minH = bh.start, maxH = bh.end;
  // date で始まるシフトで範囲拡張を判定
  list.filter((s) => s.start_datetime.slice(0, 10) === date).forEach((s) => {
    const sMin = _extMinFromIso(s.start_datetime, date);
    const eMin = _extMinFromIso(s.end_datetime, date);
    if (isNaN(sMin) || isNaN(eMin)) return;  // 不正時刻は集計から除外
    minH = Math.min(minH, Math.floor(sMin / 60));
    maxH = Math.max(maxH, Math.ceil(eMin / 60));
  });
  minH = Math.max(0, Math.floor(minH));
  maxH = Math.min(48, Math.ceil(maxH));  // 最大翌日の24時まで
  if (maxH <= minH) maxH = minH + 1;
  const rangeMin = minH * 60, rangeLen = (maxH - minH) * 60;
  const hours = [];
  for (let h = minH; h <= maxH; h++) {
    const lbl = _extHourLabel(h);
    const isNextDay = h >= 24;
    hours.push(`<div class="tl-hour${isNextDay ? ' tl-hour-next' : ''}">${lbl}</div>`);
  }
  // 配置帯のグリッド用: 表示時間数と、24:00 の位置（範囲外なら線を出さない）
  const tlHours = Math.max(1, maxH - minH);
  const dayBreakPct = ((24 * 60 - rangeMin) / rangeLen) * 100;
  const showDayBreak = dayBreakPct > 0 && dayBreakPct < 100;
  const trackVars = `--tl-hours:${tlHours};`
    + (showDayBreak ? `--tl-daybreak:${dayBreakPct.toFixed(2)}%;--tl-daybreak-display:block;` : '');
  const rows = order.map((sid) => {
    const st = staffMap[sid];
    const bars = st.shifts.map((s) => {
      // date を anchor にして拡張分計算。前日から跨ぐシフトは負の left になるので
      // 表示範囲 [0%, 100%] にクリップし、「前日から継続」マークを付ける。
      const sMin = _extMinFromIso(s.start_datetime, date);
      let eMin = _extMinFromIso(s.end_datetime, date);
      // 【NaN対策】時刻パース失敗のシフトは安全なプレースホルダ表示に倒す
      if (isNaN(sMin) || isNaN(eMin)) {
        return `<div class="tl-bar ${roleClass(s.staff_role)} ${statusClass(s.status)}" data-id="${s.id}" title="${hm(s.start_datetime)}-${hm(s.end_datetime)} (時刻不正)" style="left:0%;width:5%">${hm(s.start_datetime)}?</div>`;
      }
      if (eMin <= sMin) eMin = sMin + 60;
      const rawLeft = ((sMin - rangeMin) / rangeLen) * 100;
      const rawRight = ((eMin - rangeMin) / rangeLen) * 100;
      const left = Math.max(0, rawLeft);
      const right = Math.min(100, rawRight);
      const width = Math.max(2, right - left);
      const continued = rawLeft < 0;  // 前日から継続（左がクリップされた）
      const endsOff = rawRight > 100; // 翌日へ延長（右がクリップされた）
      // ラベル: クリップ時は矢印で継続を表現
      let lbl = '';
      if (width > 12) {
        if (continued && !endsOff) lbl = `→${hm(s.end_datetime)}`;
        else if (!continued && endsOff) lbl = `${hm(s.start_datetime)}→`;
        else if (continued && endsOff) lbl = `→→`;
        else lbl = `${hm(s.start_datetime)}-${hm(s.end_datetime)}`;
      }
      const contCls = continued ? ' tl-bar-continued' : '';
      const isDraft = isAiDraftShift(s);
      const draftCls = isDraft ? ' tl-bar-draft' : '';
      const overCap = !!s.over_cap_flag;
      const overCapCls = overCap ? ' tl-bar-overcap' : '';
      const overCapMark = overCap ? '<span class="tl-bar-overcap-mark" aria-hidden="true">⚠️</span>' : '';
      // shifts.note はスタッフが希望提出時に自由入力できる。title 属性に生で
      // 入れると " で属性を抜けられるため、必ず esc() を通す。
      const noteTitle = s.note ? `\nメモ: ${esc(s.note)}` : '';
      const overCapTitle = overCap ? '\n⚠必要人数超過の時間帯を含みます' : '';
      const dragAttrs = editable && isDraft ? ' data-draft-editable="true"' : '';
      const handles = editable && isDraft ? '<span class="tl-drag-handle tl-drag-handle-start" aria-hidden="true"></span><span class="tl-drag-handle tl-drag-handle-end" aria-hidden="true"></span>' : '';
      return `<div class="tl-bar ${roleClass(s.staff_role)} ${statusClass(s.status)}${contCls}${draftCls}${overCapCls}" data-id="${s.id}"${dragAttrs} title="${continued ? '前日から継続: ' : ''}${hm(s.start_datetime)}-${hm(s.end_datetime)}${isDraft ? '（ドラフト・直接調整可）' : ''}${overCapTitle}${noteTitle}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">${handles}${overCapMark}<span class="tl-bar-label">${lbl}</span></div>`;
    }).join('');
    return `<div class="tl-row" data-staff-id="${sid}" data-staff-name="${esc(st.name)}"><div class="tl-name"><span class="tl-name-text">${esc(st.name)}</span><span class="tl-role-badge ${roleClass(st.role)}">${roleBadgeLabel(st.role)}</span></div><div class="tl-track" data-staff-id="${sid}" style="${trackVars}" title="${editable ? '空き部分をクリックで追加' : ''}">${bars}</div></div>`;
  }).join('');

  // 時間帯別不足バー（赤で視覚化）
  // ★ タイムライン表示では「希望を出しているスタッフ」もカバー扱いし、
  //   「4-6に1人いるのに1人不足」のような誤表示を防ぐ
  const gaps = _computeHourlyGaps(list, date, { includeRequested: true });
  let gapRow = '';
  if (gaps.length) {
    const merged = _mergeHourlyGaps(gaps);
    const gapBars = merged.map((g) => {
      const left = ((g.start * 60 - rangeMin) / rangeLen) * 100;
      const width = Math.max(4, ((g.end - g.start) * 60 / rangeLen) * 100);
      const sLbl = g.start >= 24 ? `(翌)${_extHourLabel(g.start)}` : `${_extHourLabel(g.start)}時`;
      const eLbl = g.end >= 24 ? `(翌)${_extHourLabel(g.end)}` : `${_extHourLabel(g.end)}時`;
      return `<div class="tl-gap-bar" data-start="${g.start}" data-end="${g.end}" data-gap="${g.gap}" title="${editable ? 'クリックして配置' : ''} ${sLbl}〜${eLbl}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">↓${g.gap}名不足</div>`;
    }).join('');
    gapRow = `<div class="tl-row tl-gap-row"><div class="tl-name tl-gap-name">不足</div><div class="tl-track" style="${trackVars}">${gapBars}</div></div>`;
  }

  // 【日またぎ/空日対応】シフトが無い日でも営業時間の空タイムライン＋不足バーを表示。
  // emptyState で隠すと「その日の不足が分からない」問題があるため。
  const emptyNotice = !list.length
    ? `<div class="alert alert-info py-2 mb-2 small"><i class="bi bi-info-circle"></i> この日はまだシフトがありません。赤い不足バーをクリックするか、下部の「手動追加」ボタンから登録してください。</div>`
    : '';
  // 編集モードではフッター相当の手動追加ボタンをタイムライン下に置く
  const manualAddBtn = editable
    ? `<button class="btn btn-outline-primary btn-sm mt-2" id="tlManualAdd"><i class="bi bi-plus-lg"></i> 手動追加</button>`
    : '';
  const body =
    `<div class="tl-wrap"><div class="tl-axis-row"><div class="tl-name"></div><div class="tl-axis">${hours.join('')}</div></div>${rows}${gapRow}</div>
     ${emptyNotice}
     <div class="tl-legend"><span><i class="lg-role-manager"></i>店長</span><span><i class="lg-role-employee"></i>社員</span><span><i class="lg-role-part-time"></i>パート</span><span><i class="lg-role-student"></i>学生</span><span><i class="lg-alert"></i>不足</span>${editable ? '<span><i class="bi bi-hand-index" style="font-style:normal;font-size:.7rem"></i>空きをクリックで追加</span>' : ''}${editable && list.some(isAiDraftShift) ? '<span><i class="bi bi-arrows-move" style="font-style:normal;font-size:.7rem"></i>AIドラフトはドラッグで調整</span>' : ''}<span>バーをタップで${editable ? '編集' : '詳細'}</span></div>
     <button class="btn btn-outline-secondary btn-sm mt-2" id="tlDraftUndo" hidden><i class="bi bi-arrow-counterclockwise"></i> 直前の調整を戻す</button>
     ${manualAddBtn}`;
  // PC版は広め(800px)、スマホは画面幅で横スクロール対応
  const modalWidth = window.matchMedia('(min-width: 768px)').matches ? 800 : undefined;
  const w = openModal(`<i class="bi bi-diagram-3"></i> ${esc(date)}（${wdName(date)}）のシフト表`, body, null, { width: modalWidth });
  w.querySelectorAll('.tl-bar').forEach((bar) => bar?.addEventListener('click', (ev) => {
    ev.stopPropagation();
    if (bar.dataset.skipClick === 'true') return;
    buzz(10);
    w.querySelectorAll('.tl-bar').forEach((b) => b.classList.remove('selected'));
    bar.classList.add('selected');
    const s = list.find((x) => String(x.id) === bar.dataset.id);
    if (editable && s) showEditModal(s);
    else if (onChange && s) onChange(s);
  }));
  installDraftTimelineDrag(w, { date, list, editable, rangeMin, rangeLen });
  // 手動追加ボタン → スタッフを選んで時間自由入力で新規シフト
  if (editable) {
    w.querySelector('#tlManualAdd')?.addEventListener('click', async () => {
      buzz(10);
      // スタッフ一覧を取得
      let opts = '';
      try {
        const sd = await api('/shop/staffs');
        const active = (sd.staffs || []).filter((s) => !s.is_resigned);
        opts = active.map((s) => `<option value="${s.id}">${esc(s.name)}（${roleLabel(s.role)}）</option>`).join('');
      } catch (err) { toast('スタッフ一覧の取得に失敗', 'error'); return; }
      // デフォルト時間: 営業開始時刻〜+4h（翌日またぎも考慮）
      const bh = appState.businessHours || { start: 9, end: 22 };
      const sExt = bh.start;
      const eExt = Math.min(bh.end, sExt + 4);
      const sInfo = _extHourToIsoTime(sExt, 0, date);
      const eInfo = _extHourToIsoTime(eExt, 0, date);
      const isOvernight = sInfo.date !== date || eInfo.date !== date;
      const addW = openModal(`<i class="bi bi-plus-lg"></i> シフト追加 — ${date}`,
        `<label class="form-label" for="mStaff">スタッフ</label>
         <select id="mStaff" class="form-select mb-2">${opts}</select>
         <div class="row">
           <div class="col-6"><label class="form-label" for="mStart">開始 (${sInfo.date})</label><input type="time" id="mStart" class="form-control" value="${sInfo.time}"></div>
           <div class="col-6"><label class="form-label" for="mEnd">終了 (${eInfo.date})</label><input type="time" id="mEnd" class="form-control" value="${eInfo.time}"></div>
         </div>
         <div class="small text-secondary mt-2">${isOvernight ? '※翌日またぎのシフトです。' : ''}上限人数を超える場合は自動調整されます。</div>`,
        async (w2, close) => {
          const staffId = +w2.querySelector('#mStaff').value;
          const st = w2.querySelector('#mStart').value;
          const en = w2.querySelector('#mEnd').value;
          if (!st || !en) { toast('時間を入力してください', 'error'); return; }
          try {
            const r = await api('/shop/shifts', { method: 'POST', body: JSON.stringify({
              staff_id: staffId,
              start_datetime: `${sInfo.date}T${st}:00`,
              end_datetime: `${eInfo.date}T${en}:00`,
              auto_adjust: true,
            })});
            close();
            if (r.adjustments && r.adjustments.length) {
              toast(`追加しました（${r.adjustments.length}件自動調整）`, 'success');
            } else {
              toast('追加しました', 'success');
            }
            // タイムラインを再描画（前日〜翌日の範囲で取得してovernightも拾う）
            w.remove();
            const prevDay = new Date(date + 'T00:00:00'); prevDay.setDate(prevDay.getDate() - 1);
            const nextDay = new Date(date + 'T00:00:00'); nextDay.setDate(nextDay.getDate() + 1);
            const sd2 = await api(`/shop/shifts?start=${_localDateStr(prevDay)}&end=${_localDateStr(nextDay)}`);
            openDayTimeline(date, sd2.shifts, editable, onChange);
          } catch (err) { toast(err.message, 'error'); }
        });
      addW.querySelector('[data-save]').textContent = '追加';
    });
  }
  // 空き部分クリック → そのスタッフ＋クリック位置の時間帯で追加
  if (editable) {
    w.querySelectorAll('.tl-track').forEach((track) => {
      track?.addEventListener('click', (e) => {
        if (e.target.closest('.tl-bar') || e.target.closest('.tl-gap-bar')) return; // バー/不足バーのクリックは別処理
        const staffId = track.dataset.staffId;
        const staffName = track.closest('.tl-row').dataset.staffName;
        // クリックX座標から時間を計算（拡張時間 0-47）
        const rect = track.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        const clickMin = rangeMin + ratio * rangeLen;
        const startHour = Math.max(minH, Math.min(maxH - 1, Math.floor(clickMin / 60)));
        const endHour = Math.min(maxH, startHour + 4); // デフォルト4h
        // 拡張時間 → { date, time }。翌日またぎは date に1日加算
        const sInfo = _extHourToIsoTime(startHour, 0, date);
        const eInfo = _extHourToIsoTime(endHour, 0, date);
        const isOvernight = sInfo.date !== date || eInfo.date !== date;
        const datePrefix = isOvernight ? `${sInfo.date} ` : '';
        buzz(10);
        const addW = openModal(`<i class="bi bi-plus-lg"></i> シフト追加 — ${esc(staffName)} ${sInfo.date}`,
          `<div class="row">
             <div class="col-6"><label class="form-label" for="qStart">開始</label><input type="time" id="qStart" class="form-control" value="${sInfo.time}"></div>
             <div class="col-6"><label class="form-label" for="qEnd">終了</label><input type="time" id="qEnd" class="form-control" value="${eInfo.time}"></div>
           </div>
           <div class="small text-secondary mt-2">${isOvernight ? `※翌日またぎのシフトです（開始: ${sInfo.date} / 終了: ${eInfo.date}）。` : ''}時間を調整して「保存」を押してください。上限人数を超える場合は自動調整します。</div>`,
          async (w2, close) => {
            const st = w2.querySelector('#qStart').value;
            const en = w2.querySelector('#qEnd').value;
            if (!st || !en) { toast('時間を入力してください', 'error'); return; }
            try {
              const r = await api('/shop/shifts', { method: 'POST', body: JSON.stringify({
                staff_id: +staffId,
                start_datetime: `${sInfo.date}T${st}:00`,
                end_datetime: `${eInfo.date}T${en}:00`,
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
        const startH = +bar.dataset.start;  // 拡張時間 (0-47)
        const endH = +bar.dataset.end;
        const gap = +bar.dataset.gap;
        // 拡張時間 → 実際の {date, time}
        const sInfo = _extHourToIsoTime(startH, 0, date);
        const eInfo = _extHourToIsoTime(endH, 0, date);
        const isOvernight = sInfo.date !== date || eInfo.date !== date;
        buzz(10);
        // スタッフリストを取得
        let opts = '';
        try {
          const sd = await api('/shop/staffs');
          const active = (sd.staffs || []).filter((s) => !s.is_resigned);
          opts = active.map((s) => `<option value="${s.id}">${esc(s.name)}（${roleLabel(s.role)}）</option>`).join('');
        } catch (err) { toast('スタッフ一覧の取得に失敗', 'error'); return; }
        const addW = openModal(`<i class="bi bi-person-plus"></i> 不足枠に配置 — ${sInfo.date} ${sInfo.time}〜${eInfo.date === sInfo.date ? '' : eInfo.date + ' '}${eInfo.time}`,
          `<div class="alert alert-warning py-2 mb-3"><i class="bi bi-exclamation-triangle"></i> この時間帯は<strong>${gap}名</strong>不足中。1名ずつ追加できます。</div>
           <label class="form-label" for="gapStaff">スタッフを選択</label>
           <select id="gapStaff" class="form-select mb-2">${opts}</select>
           <div class="row">
             <div class="col-6"><label class="form-label" for="gapStart">開始 (${sInfo.date})</label><input type="time" id="gapStart" class="form-control" value="${sInfo.time}"></div>
             <div class="col-6"><label class="form-label" for="gapEnd">終了 (${eInfo.date})</label><input type="time" id="gapEnd" class="form-control" value="${eInfo.time}"></div>
           </div>
           <div class="small text-secondary mt-2">${isOvernight ? '※翌日またぎのシフトです。' : ''}残り${gap - 1}名の不足がある場合は、追加後に再度クリックしてください。</div>`,
          async (w2, close) => {
            const staffId = +w2.querySelector('#gapStaff').value;
            const st = w2.querySelector('#gapStart').value;
            const en = w2.querySelector('#gapEnd').value;
            try {
              const r = await api('/shop/shifts', { method: 'POST', body: JSON.stringify({
                staff_id: staffId,
                start_datetime: `${sInfo.date}T${st}:00`,
                end_datetime: `${eInfo.date}T${en}:00`,
                auto_adjust: true,
              })});
              close();
              if (r.adjustments && r.adjustments.length) {
                toast(`配置しました（${r.adjustments.length}件自動調整）`, 'success');
              } else {
                toast('配置しました', 'success');
              }
              w.remove();
              // 前日・当日・翌日のいずれかを含む範囲で再取得（overnight表示のため）
              const prevDay = new Date(date + 'T00:00:00'); prevDay.setDate(prevDay.getDate() - 1);
              const nextDay = new Date(date + 'T00:00:00'); nextDay.setDate(nextDay.getDate() + 1);
              const fmtD = (d) => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
              const sd2 = await api(`/shop/shifts?start=${fmtD(prevDay)}&end=${fmtD(nextDay)}`);
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
  const overCapBanner = s.over_cap_flag
    ? `<div class="alert alert-warning py-2 mb-2 small"><i class="bi bi-exclamation-triangle-fill"></i> このシフトは必要人数を超過している時間帯を含みます。</div>`
    : '';
  // 確定シフトはロック：時間・取消は「スタッフの変更申請を承認」で反映する方針。
  // ここではメモのみ編集可能にし、時間は読み取り専用で表示する。
  if (s.status === 'confirmed') {
    const wl = openModal(`<i class="bi bi-lock"></i> 確定シフト${s.staff_name ? ' — ' + esc(s.staff_name) : ''}`,
      `${overCapBanner}
       <div class="small text-secondary mb-2">${esc(hm(s.start_datetime))} 〜 ${esc(hm(s.end_datetime))}（確定）</div>
       <div class="alert alert-info py-2 mb-3 small"><i class="bi bi-info-circle"></i> 確定シフトの時間変更・取消は、スタッフからの<strong>変更申請を承認</strong>して反映します（直接編集はできません）。</div>
       <label class="form-label" for="mNote">店長メモ<span class="small text-secondary">（この画面のみ表示）</span></label>
       <textarea id="mNote" class="form-control mb-1" rows="2" placeholder="例: 新人研修のため増員">${esc(s.note || '')}</textarea>`,
      async (w2, close) => {
        const noteVal = w2.querySelector('#mNote').value;
        try {
          await api(`/shop/shifts/${s.id}/note`, { method: 'PATCH', body: JSON.stringify({ note: noteVal }) });
          close();
          toast('メモを保存しました', 'success');
          navigateTo('shifts');
        } catch (e) { toast(e.message, 'error'); }
      });
    if (wl) { const btn = wl.querySelector('[data-save]'); if (btn) btn.textContent = 'メモを保存'; }
    return wl;
  }
  const w = openModal(`<i class="bi bi-pencil-square"></i> シフト編集${s.staff_name ? ' — ' + esc(s.staff_name) : ''}`,
    `${overCapBanner}<label class="form-label" for="mStart">開始</label><input type="datetime-local"  id="mStart" class="form-control mb-2" value="${esc(toLocal(s.start_datetime))}">
     <label class="form-label" for="mEnd">終了</label><input type="datetime-local"  id="mEnd" class="form-control mb-3" value="${esc(toLocal(s.end_datetime))}">
     <label class="form-label" for="mStatus">ステータス</label><select id="mStatus" class="form-select mb-3">
       <option value="confirmed" ${s.status === 'confirmed' ? 'selected' : ''}>確定</option>
       <option value="modifying" ${s.status === 'modifying' ? 'selected' : ''}>調整中</option>
       <option value="requested" ${s.status === 'requested' ? 'selected' : ''}>調整待ち</option></select>
     <label class="form-label" for="mNote">店長メモ<span class="small text-secondary">（この画面のみ表示）</span></label>
     <textarea id="mNote" class="form-control mb-3" rows="2" placeholder="例: 新人研修のため増員">${esc(s.note || '')}</textarea>
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
        // 店長メモを保存（変更があった場合のみ）
        const noteVal = w2.querySelector('#mNote').value;
        if ((noteVal || '') !== (s.note || '')) {
          try { await api(`/shop/shifts/${s.id}/note`, { method: 'PATCH', body: JSON.stringify({ note: noteVal }) }); } catch (_) { /* メモ保存失敗は主保存を妨げない */ }
        }
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
  if (!box || !box.isConnected) return;
  if (!start || !end) { box.innerHTML = '<div class="text-muted small">期間を指定してください</div>'; return; }
  const tok = navToken();
  try {
    // 時間帯単位の不足を計算（「夜(17:00)」のような区分単位ではなく）
    await ensureBusinessHours();
    const sd = await api(`/shop/shifts?start=${start}&end=${end}`);
    if (!isAlive(tok) || !box.isConnected) return;
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
        const sH = _fmtExtHour(g.start);
        const eH = _fmtExtHour(g.end);
        chips.push(`<span class="shortage-chip"><i class="bi bi-exclamation-triangle"></i> ${day.slice(5)} ${sH}:00〜${eH}:00 <strong>あと${g.gap}名</strong></span>`);
      });
    });
    // シフトが無い日は全時間帯不足として表示
    if (appState.patterns) {
      const days = [];
      const cur = new Date(start + 'T00:00:00');
      const endD = new Date(end + 'T00:00:00');
      while (cur <= endD) {
        const ds = _localDateStr(cur);  // toISOString は UTC で日付がズレる
        if (!byDay[ds]) days.push(ds);
        cur.setDate(cur.getDate() + 1);
      }
      days.forEach((day) => {
        const gaps = _computeHourlyGaps([], day);
        const merged = _mergeHourlyGaps(gaps);
        merged.forEach((g) => {
          const sH = _fmtExtHour(g.start);
          const eH = _fmtExtHour(g.end);
          chips.push(`<span class="shortage-chip"><i class="bi bi-exclamation-triangle"></i> ${day.slice(5)} ${sH}:00〜${eH}:00 <strong>あと${g.gap}名</strong></span>`);
        });
      });
    }
    if (!isAlive(tok) || !box.isConnected) return;
    if (!chips.length) {
      box.innerHTML = '<div class="shortage-none"><i class="bi bi-check-circle"></i> 不足なし — 全時間帯充足</div>';
    } else {
      box.innerHTML = chips.join('');
    }
  } catch (e) {
    if (!isAlive(tok) || !box.isConnected) return;
    box.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
  }
}

/* ============================================================
   SCREENS
   ============================================================ */
const SCREENS = {};

/* ---------- Dashboard ---------- */
SCREENS.dashboard = async function (el) {
  const tok = navToken();
  const today = todayStr();
  // 「組む」から「確認する」へ：店長が最初に見るべきは今日の配置（穴のありか）であって
  // 集計値ではない。配置帯を最上部、KPIはその下に小さく敷く。
  el.innerHTML = pageHead('ダッシュボード', 'bi-grid-1x2', currentUser.shop_name) +
    card(sectionTitle('bi-diagram-3', '今日の配置') + `<div id="dashTimeline"><div class="skeleton" style="height:90px;border-radius:10px"></div></div>`) +
    `<div class="kpi-grid" id="kpiGrid"><div class="skeleton" style="height:64px"></div><div class="skeleton" style="height:64px"></div><div class="skeleton" style="height:64px"></div><div class="skeleton" style="height:64px"></div></div>
    <div class="dash-grid">
      <div id="dashLeft"></div>
      <div id="dashRight"></div>
    </div>`;

  // 今日の配置帯（最上部）。/shop/dashboard の today_shifts は staff_role を持たず
  // start/endもHH:MM文字列のため配置帯は描けない。配置帯専用に /shop/shifts を叩く。
  // KPI等の描画を待たせないよう独立させ、失敗しても他のブロックには影響させない。
  (async () => {
    try {
      await ensureBusinessHours();
      const sd = await api(`/shop/shifts?start=${today}&end=${today}`);
      if (!isAlive(tok) || !el.isConnected) return;
      const todayShifts = sd.shifts || [];
      const todayGaps = _mergeHourlyGaps(_computeHourlyGaps(todayShifts, today, { includeRequested: true }));
      const shortageNote = todayGaps.length
        ? `<div class="dash-shortage-note">${esc(gapSummaryText(todayGaps))}</div>`
        : '';
      const dashTimeline = document.getElementById('dashTimeline');
      if (dashTimeline) dashTimeline.innerHTML = shortageNote + buildStaticTimelineHtml(todayShifts, today);
    } catch (e) {
      if (!isAlive(tok) || !el.isConnected) return;
      const dashTimeline = document.getElementById('dashTimeline');
      if (dashTimeline) dashTimeline.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
    }
  })();

  try {
    const d = await api('/shop/dashboard');
    // 画面遷移済み or DOM破棄済みなら更新中止（"Cannot set properties of null" 回避）
    if (!isAlive(tok) || !el.isConnected) return;
    // KPIs
    const kpiGrid = document.getElementById('kpiGrid');
    if (kpiGrid) kpiGrid.innerHTML =
      kpiCard('bi-people-fill', '稼働スタッフ', d.staff_count, `社員${d.employee_count} / バイト${d.part_time_count}`, 'indigo') +
      kpiCard('bi-calendar-check', '今日の出勤', d.today_attendance + '名', d.today_shortage ? `${d.today_shortage}枠不足` : '充足', d.today_shortage ? 'amber' : 'green') +
      kpiCard('bi-cash-stack', '今月の人件費', '¥' + (d.month_cost / 1000).toFixed(0) + 'K', `${d.month_hours}h`, 'indigo') +
      kpiCard('bi-inbox', '承認待ち', d.pending_approvals + d.pending_requests, '申請・希望', (d.pending_approvals + d.pending_requests) > 0 ? 'red' : 'green');

    // Left: charts
    const leftBox = document.getElementById('dashLeft');
    if (leftBox) leftBox.innerHTML =
      card(sectionTitle('bi-bar-chart', '今日の時間帯別人数') + `<div class="chart-box"><canvas id="todayChart"></canvas></div>`) +
      card(sectionTitle('bi-graph-up', '人件費推移（直近30日）') + `<div class="chart-box"><canvas id="costChart"></canvas></div>`);

    // Today hourly chart
    const todayHours = d.today_hourly.length ? d.today_hourly : [];
    const hours = todayHours.map((h) => h.hour + ':00');
    const counts = todayHours.map((h) => h.count);
    const todayCanvas = document.getElementById('todayChart');
    if (todayCanvas) chartInstances.today = new Chart(todayCanvas, {
      type: 'bar',
      data: { labels: hours.length ? hours : ['データなし'], datasets: [{ label: '人数', data: counts.length ? counts : [0], backgroundColor: cssVarAlpha('--role-employee', .9), borderRadius: 6 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { color: cssVar('--ink-3') }, grid: { color: cssVar('--rule') } }, x: { ticks: { color: cssVar('--ink-3') }, grid: { display: false } } } }
    });

    // Cost chart
    const costData = d.daily_cost_series || [];
    const costCanvas = document.getElementById('costChart');
    if (costCanvas) chartInstances.cost = new Chart(costCanvas, {
      type: 'line',
      data: { labels: costData.map((c) => c.date.slice(5)), datasets: [{ label: '人件費(円)', data: costData.map((c) => c.cost), borderColor: cssVar('--info'), backgroundColor: cssVarAlpha('--info', .12), fill: true, tension: .3, pointRadius: 0 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { color: cssVar('--ink-3'), callback: (v) => '¥' + (v / 1000) + 'K' }, grid: { color: cssVar('--rule') } }, x: { ticks: { color: cssVar('--ink-3'), maxTicksLimit: 8 }, grid: { display: false } } } }
    });

    // Right: AI suggestion + notifications + quick actions
    const rightBox = document.getElementById('dashRight');
    let aiAdvice = 'シフトデータを分析中...';
    try { const rev = await api('/shop/ai/review', { method: 'POST', body: JSON.stringify({ start: todayStr().slice(0, 8) + '01', end: todayStr().slice(0, 8) + '31' }) }); aiAdvice = rev.advice; } catch {}
    if (!isAlive(tok) || !el.isConnected) return;
    if (rightBox) rightBox.innerHTML =
      card(sectionTitle('bi-stars', 'AIからの提案', badge('AI', 'ai')) + `<div class="reason-text" style="font-size:.88rem;line-height:1.7;white-space:pre-wrap">${esc(aiAdvice)}</div>`) +
      card(sectionTitle('bi-lightning', 'クイック操作') +
        `<button class="btn btn-ai w-full mb-2" id="qGen"><i class="bi bi-stars"></i> AIでシフト作成</button>
         <button class="btn btn-light w-full mb-2" id="qShifts"><i class="bi bi-calendar3"></i> シフト画面へ</button>
         <button class="btn btn-light w-full" id="qCreq"><i class="bi bi-clipboard-check"></i> 変更申請を確認 <span id="qCreqBadge"></span></button>`) +
      card(sectionTitle('bi-bell', '最近の通知') + `<div id="dashNotif"><div class="text-secondary small">読み込み中...</div></div>`);

    document.getElementById('qGen')?.addEventListener('click', () => navigateTo('aiGenerate'));
    document.getElementById('qShifts')?.addEventListener('click', () => navigateTo('shifts'));
    document.getElementById('qCreq')?.addEventListener('click', () => openChangeRequests());

    // 変更申請の保留件数バッジ
    try {
      const cr = await api('/shop/change-requests');
      if (isAlive(tok) && el.isConnected) {
        const pending = (cr.change_requests || []).filter((x) => x.status === 'pending').length;
        const badgeEl = document.getElementById('qCreqBadge');
        if (badgeEl && pending > 0) badgeEl.innerHTML = badge(`${pending}件保留`, 'warning');
      }
    } catch {}

    // Notifications
    try {
      const n = await api('/shop/notifications');
      if (!isAlive(tok) || !el.isConnected) return;
      const dashNotif = document.getElementById('dashNotif');
      if (dashNotif) dashNotif.innerHTML = n.notifications.length ? n.notifications.slice(0, 4).map((x) => `<div class="notif-item ${x.is_read ? '' : 'unread'}"><div class="nt-title">${esc(x.title)}</div><div class="nt-body">${esc(x.body || '')}</div></div>`).join('') : '<div class="small text-secondary">通知はありません</div>';
    } catch {}
  } catch (e) {
    if (!isAlive(tok) || !el.isConnected) return;
    safeSetHTML(el, card(`<div class="text-danger">${esc(e.message)}</div>`));
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
        <div class="col-6"><label class="form-label" for="genStart">開始日</label><input type="date"  id="genStart" class="form-control" value="${esc(p.start_date)}"></div>
        <div class="col-6"><label class="form-label" for="genEnd">終了日</label><input type="date"  id="genEnd" class="form-control" value="${esc(p.end_date)}"></div>
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
    // s.xxx（下の gen-condition-value）は shops.settings 由来。サーバ側の型検証
    // （utils.validate_known_settings_values）は新規保存にしか効かず、代理閲覧中は
    // このコードが別テナントのデータを管理者のブラウザで描画し得るため、
    // 描画側でも esc() を通す（保存型XSS対策の多層防御）。
    // シフト時間設定から代表的な時間帯を表示（bulk_mode優先、無ければ月-金の平均）
    let shiftHoursLabel = '未設定';
    try {
      const sh = await api('/shop/shift-hours');
      if (sh.bulk_mode) {
        const b = sh.bulk || {};
        shiftHoursLabel = b.is_closed ? '定休（一括）' : `${b.start_time || '?'}-${b.end_time || '?'}`;
      } else {
        const mon = (sh.days || {})['1'] || {};
        shiftHoursLabel = mon.is_closed ? '月曜定休' : `${mon.start_time || '?'}-${mon.end_time || '?'}`;
      }
    } catch {}
    document.getElementById('genConditions').innerHTML =
      card(sectionTitle('bi-clipboard-data', 'AIに考慮させる条件') +
        `<div class="gen-condition"><span class="gen-condition-label">稼働スタッフ</span><span class="gen-condition-value">${active.length}名</span></div>
         <div class="gen-condition"><span class="gen-condition-label">　社員 / アルバイト</span><span class="gen-condition-value">${active.filter((x) => x.role === 'employee').length}名 / ${active.filter((x) => x.role === 'part_time' || x.role === 'student').length}名</span></div>
         <div class="gen-condition"><span class="gen-condition-label">1日最低勤務時間</span><span class="gen-condition-value">${esc(String(s.min_daily_hours || 4))}時間</span></div>
         <div class="gen-condition"><span class="gen-condition-label">最大連勤（推奨）</span><span class="gen-condition-value">${esc(String(s.max_consecutive_days || 6))}日</span></div>
         <div class="gen-condition"><span class="gen-condition-label">深夜割増率</span><span class="gen-condition-value">${esc(String(s.night_premium_rate || 1.25))}倍</span></div>
         <div class="gen-condition"><span class="gen-condition-label">シフト時間（代表）</span><span class="gen-condition-value">${esc(shiftHoursLabel)}</span></div>
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
  input?.addEventListener('keydown', (e) => {
    // IME変換中（isComposing / keyCode 229）のEnterは確定扱いとして送信しない
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); sendShopChat(); }
  });
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
        <div class="kpi-card kpi-red"><div class="kpi-label">不足枠</div><div class="kpi-value num">${prev.shortage_unique_count != null ? prev.shortage_unique_count : (prev.shortage || []).length}</div></div>
      </div>`) +
    card(sectionTitle('bi-lightbulb', 'AIの判断理由', badge('Explainable AI', 'ai')) +
      `<div class="explanation-list">${explanations}</div>`) +
    card(sectionTitle('bi-people', 'スタッフ別 想定労働時間') + `<div class="preview-grid">${topList || '<span class="small text-secondary">なし</span>'}</div>`) +
    card(
      `<div class="gen-actions">
        <button class="btn btn-primary btn-lg" style="min-width:260px" id="saveDraftBtn"><i class="bi bi-pencil-square"></i> ドラフト保存（後で調整）</button>
        <div class="small text-secondary mt-2 mb-3">ドラフト保存後、シフト画面で調整 → 「確定」ボタンでスタッフに通知</div>
      </div>`);

    // ドラフト保存（推奨・デフォルト）
    document.getElementById('saveDraftBtn')?.addEventListener('click', async () => {
      setLoading(true, 'ドラフト保存中...');
      try {
        const d = await api('/shop/shifts/auto', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end, draft: true }) });
        setLoading(false);
        toast(`${d.confirmed_count}件をドラフト保存しました（確定前）`, 'success');
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
        <div class="col-6 col-sm-5"><label class="form-label" for="sStart">開始</label><input type="date" id="sStart" class="form-control" value="${esc(p.start_date)}"></div>
        <div class="col-6 col-sm-5"><label class="form-label" for="sEnd">終了</label><input type="date" id="sEnd" class="form-control" value="${esc(p.end_date)}"></div>
        <div class="col-12 col-sm-2 mt-2 mt-sm-0"><label class="form-label d-none d-sm-block">&nbsp;</label><button class="btn btn-ai w-full" id="autoGen" title="AI自動作成"><i class="bi bi-stars"></i> AI生成</button></div>
      </div>
      <div class="flex gap-2 flex-wrap">
        <button class="btn btn-light flex-grow" id="addShiftBtn"><i class="bi bi-plus-lg"></i> 手動追加</button>
        <button class="btn btn-light flex-grow" id="copyBtn"><i class="bi bi-files"></i> コピー</button>
        <button class="btn btn-light" id="printBtn"><i class="bi bi-printer"></i></button>
        <button class="btn btn-success flex-grow" id="finalizeDraftBtn" title="AIドラフト保存中のシフトを一括確定して通知"><i class="bi bi-megaphone"></i> ドラフトを確定・通知</button>
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
    if (!box) return;
    if (!start || !end) { box.innerHTML = '<div class="text-muted small">期間を指定してください</div>'; return; }
    const tok = navToken();
    try {
      const d = await api(`/shop/summary?start=${start}&end=${end}`);
      if (!isAlive(tok) || !box.isConnected) return;
      if (!d.staff.length) { box.innerHTML = '<div class="text-muted small">確定シフトがありません</div>'; return; }
      box.innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>氏名</th><th>日</th><th class="t-num">確定</th><th class="t-num">見込</th><th class="t-num">深夜</th><th class="t-num">給与</th></tr></thead>
        <tbody>${d.staff.map((s) => `<tr><td><div class="staff-cell"><span class="staff-name">${esc(s.name)}</span><span class="staff-sub">${roleLabel(s.role)}</span></div></td><td>${s.days}</td><td class="t-num num">${s.confirmed_hours}h</td><td class="t-num num">${s.projected_hours}h</td><td class="t-num num">${s.night_hours}h</td><td class="t-num num">${yen(s.pay)}</td></tr>`).join('')}
        <tr style="font-weight:800;color:var(--ink)"><td>合計</td><td></td><td class="t-num num">${d.total_hours}h</td><td class="t-num num">${d.total_projected_hours}h</td><td></td><td class="t-num num">${yen(d.total_pay)}</td></tr>
        </tbody></table></div>`;
    } catch (e) {
      if (!isAlive(tok) || !box.isConnected) return;
      box.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
    }
  }
  async function refreshShortage() {
    const { start, end } = cur();
    const box = document.getElementById('shortageBox');
    if (!start || !end) { box.innerHTML = '<div class="text-muted small">期間を指定してください</div>'; return; }
    await loadShortage(box, start, end);
  }
  loadSummary();
  refreshShortage();

  // 期間変更で自動再描画（カレンダーも同期）
  const onPeriodChange = () => {
    loadSummary();
    refreshShortage();
    // カレンダー表示月も sStart に合わせる
    const s = sStartEl ? sStartEl.value : '';
    if (s && window._shiftCalCtrl && window._shiftCalCtrl.goToMonth) {
      const [yy, mm] = s.split('-').map((x) => +x);
      if (yy && mm) {
        try { window._shiftCalCtrl.goToMonth(yy, mm - 1); } catch {}
      }
    }
  };
  sStartEl?.addEventListener('change', onPeriodChange);
  sEndEl?.addEventListener('change', onPeriodChange);

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
          const te = _localDateStr(new Date(new Date(ts).getTime() + days * 86400000));
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

  // ドラフト保存中のシフトを一括確定して通知
  document.getElementById('finalizeDraftBtn')?.addEventListener('click', async () => {
    const { start, end } = cur();
    if (!start || !end) { toast('期間を指定してください', 'error'); return; }
    if (!confirm(`${start} 〜 ${end} のドラフト保存シフトを確定し、全スタッフに通知しますか？\n\n・シフトが「確定」状態になります\n・スタッフに「シフト確定」通知が届きます`)) return;
    setLoading(true, '確定・通知中...');
    try {
      const r = await api('/shop/shifts/finalize', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end }) });
      setLoading(false);
      const extra = r.over_cap ? `（必要人数超過 ${r.over_cap} 件。⚠️のシフトを確認してください）` : '';
      toast((r.message || `${r.finalized}件を確定しました`) + extra, r.over_cap ? 'warning' : 'success');
      loadSummary();
      refreshShortage();
    } catch (e) { setLoading(false); toast(e.message, 'error'); }
  });

  const calCtrl = createCalendar(document.getElementById('calMount'), {
    initial: p.start_date,
    loader: (from, to) => api(`/shop/shifts?start=${from}&end=${to}`).then((d) => d.shifts),
    editable: true,
  });
  window._shiftCalCtrl = calCtrl;
  // カレンダー初期表示月を画面上部の期間（sStart）に同期
  // ※ 従来は appState.period.start_date だけで初期化されていたため、
  //    ユーザーが期間を変えてもカレンダーが追従せず時間表示が無いように見える問題
  setTimeout(() => {
    const ss = document.getElementById('sStart');
    if (ss && ss.value) {
      const [yy, mm] = ss.value.split('-').map((x) => +x);
      if (yy && mm) {
        try { calCtrl.goToMonth(yy, mm - 1); } catch {}
      }
    }
  }, 200);
};

/* AI生成: シフト画面内で直接プレビュー→ドラフト保存（遷移しない） */
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
          <div class="col-4"><div class="kpi-card kpi-red" style="margin:0;padding:12px"><div class="kpi-label">不足枠</div><div class="kpi-value num">${prev.shortage_unique_count != null ? prev.shortage_unique_count : (prev.shortage || []).length}</div></div></div>
       </div>
       ${explanations ? `<div class="small fw-bold text-muted mb-2"><i class="bi bi-lightbulb"></i> AIの判断理由</div><div class="explanation-list mb-3">${explanations}</div>` : ''}
       <div class="small text-muted">※ドラフトとして保存後、日別シフト表で時間を調整できます。スタッフへの通知は「ドラフトを確定・通知」を押すまで送信されません。</div>`,
      async (w2, close) => {
        setLoading(true, 'ドラフトを保存中...');
        try {
          const d = await api('/shop/shifts/auto', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end, draft: true }) });
          setLoading(false);
          close();
          toast(`${d.confirmed_count}件をドラフト保存しました。日別シフト表で調整できます。`, 'success');
          // カレンダーを作成月へジャンプ
          try { const d0 = new Date(start + 'T00:00:00'); if (window._shiftCalCtrl) window._shiftCalCtrl.goToMonth(d0.getFullYear(), d0.getMonth()); } catch {}
          loadSummary(); refreshShortage(); refreshNotifBadge();
        } catch (e) { setLoading(false); toast(e.message, 'error'); }
      });
    w.querySelector('[data-save]').textContent = 'ドラフトとして保存して調整';
  } catch (e) { setLoading(false); genResult.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`; }
}

function openAddShiftModal() {
  const p = appState.period || { start_date: todayStr() };
  const defDate = p.start_date || todayStr();
  api('/shop/staffs').then((sd) => {
    const active = (sd.staffs || []).filter((s) => !s.is_resigned);
    const opts = active.map((s) => `<option value="${s.id}">${esc(s.name)}（${roleLabel(s.role)}）</option>`).join('');
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
  const tok = navToken();
  try {
    const data = await api('/shop/staffs');
    if (!isAlive(tok)) return;
    const list = document.getElementById('staffList');
    if (!list) return;
    if (!data.staffs.length) { list.innerHTML = emptyState('bi-people', 'スタッフがいません'); return; }
    list.innerHTML = data.staffs.map((s) => `
      <div class="list-row">
        <div class="flex items-center gap-2">
          <span class="dot ${roleClass(s.role)}"></span>
          <div>
            <strong>${esc(s.name)}</strong> <span class="text-secondary">${esc(s.staff_code)}</span>${s.is_resigned ? badge('退職', 'warning') : ''}
            <div class="small text-secondary">${roleLabel(s.role)} ・ 時給${esc(s.hourly_wage)}円 ・ 月${esc(s.min_hours_per_month)}-${esc(s.max_hours_per_month)}h</div>
          </div>
        </div>
        <div class="flex gap-1">
          <button class="btn btn-sm btn-light" data-fix="${s.id}" data-name="${esc(s.name)}" title="固定シフト"><i class="bi bi-calendar-week"></i></button>
          <button class="btn btn-sm btn-light" data-edit="${s.id}" title="編集"><i class="bi bi-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger" data-del="${s.id}" data-name="${esc(s.name)}" title="削除"><i class="bi bi-trash"></i></button>
        </div>
      </div>`).join('');
    list.querySelectorAll('[data-edit]').forEach((b) => b?.addEventListener('click', () => showStaffForm(data.staffs.find((x) => x.id == b.dataset.edit))));
    list.querySelectorAll('[data-fix]').forEach((b) => b?.addEventListener('click', () => showFixedShiftModal(+b.dataset.fix, b.dataset.name)));
    list.querySelectorAll('[data-del]').forEach((b) => b?.addEventListener('click', () => confirmDeleteStaff(+b.dataset.del, b.dataset.name)));
  } catch (e) {
    if (!isAlive(tok)) return;
    const list = document.getElementById('staffList');
    if (list) list.innerHTML = `<div class="text-danger">${esc(e.message)}</div>`;
  }
}
function showStaffForm(s) {
  const isEdit = !!s;
  const isStudent = s && s.role === 'student';
  const wrap = openModal(`<i class="bi bi-person-plus"></i> ${isEdit ? 'スタッフ編集' : 'スタッフ追加'}`,
    `<div class="row">
      <div class="col-6"><label class="form-label" for="f_code">コード</label><input id="f_code" class="form-control" value="${s ? esc(s.staff_code) : ''}" ${isEdit ? 'disabled' : ''}></div>
      <div class="col-6"><label class="form-label" for="f_name">氏名</label><input id="f_name" class="form-control" value="${s ? esc(s.name) : ''}"></div>
    </div>
    <label class="form-label mt-2">ロール</label><select id="f_role" class="form-select"><option value="part_time" ${s && s.role === 'part_time' ? 'selected' : ''}>アルバイト</option><option value="student" ${s && s.role === 'student' ? 'selected' : ''}>学生アルバイト（月${STUDENT_MAX_HOURS}h上限）</option><option value="employee" ${s && s.role === 'employee' ? 'selected' : ''}>社員</option><option value="manager" ${s && s.role === 'manager' ? 'selected' : ''}>店舗管理者（店舗権限）</option></select>
    <div class="row mt-2">
      <div class="col-4"><label class="form-label" for="f_wage">時給</label><input id="f_wage" type="number" class="form-control" value="${esc(s ? s.hourly_wage : 1100)}"></div>
      <div class="col-4"><label class="form-label" for="f_min">最低h</label><input id="f_min" type="number" class="form-control" value="${esc(s ? s.min_hours_per_month : 0)}"></div>
      <div class="col-4"><label class="form-label" for="f_max">上限h ${isStudent ? `<span class="text-danger small">(学生は${STUDENT_MAX_HOURS})</span>` : ''}</label><input id="f_max" type="number" class="form-control" value="${esc(s ? s.max_hours_per_month : 160)}" ${isStudent ? 'max="' + STUDENT_MAX_HOURS + '"' : ''}></div>
    </div>
    <div class="small text-secondary mt-1" id="f_role_hint" style="display:${isStudent ? 'block' : 'none'}"><i class="bi bi-info-circle"></i> 学生アルバイトは月間${STUDENT_MAX_HOURS}時間上限・学生のみのシフトは作成できません。</div>
    <label class="form-label mt-2">ステータス</label><select id="f_resign" class="form-select"><option value="0" ${!s || !s.is_resigned ? 'selected' : ''}>在籍</option><option value="1" ${s && s.is_resigned ? 'selected' : ''}>退職</option></select>
    <label class="form-label mt-2">パスワード ${isEdit ? '（変更時のみ・8文字以上）' : '（8文字以上・英数字）'}</label>
    <input id="f_pw" type="password" class="form-control" placeholder="${isEdit ? '空欄で変更なし' : 'パスワード'}" autocomplete="new-password">
    <div class="pw-rules" id="pwRules">
      <span class="pw-rule" data-rule="len"><i class="bi bi-circle"></i>8文字以上</span>
      <span class="pw-rule" data-rule="alpha"><i class="bi bi-circle"></i>英字を含む</span>
      <span class="pw-rule" data-rule="digit"><i class="bi bi-circle"></i>数字を含む</span>
    </div>
    <div class="form-error" id="f_err"></div>`,
    async (w, close) => {
      const g = (id) => w.querySelector(id).value;
      const errBox = w.querySelector('#f_err');
      const showErr = (msg) => {
        errBox.innerHTML = msg ? `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(msg)}` : '';
        if (msg) w.querySelector('#f_pw').classList.add('is-invalid');
        else w.querySelector('#f_pw').classList.remove('is-invalid');
      };
      // クライアント側事前バリデーション（API通信せず即座に回答）
      const pwVal = g('#f_pw');
      const pwRequired = !isEdit || pwVal.length > 0;
      if (pwRequired) {
        const verr = validatePassword(pwVal);
        if (verr) { showErr(verr); return; }
      }
      // 必須項目
      if (!isEdit && !g('#f_code')) { showErr('コードを入力してください'); return; }
      if (!g('#f_name')) { showErr('氏名を入力してください'); return; }
      // 学生アルバイト: 月80h上限
      const role = g('#f_role');
      const maxH = parseInt(g('#f_max'), 10);
      if (role === 'student' && maxH > STUDENT_MAX_HOURS) {
        showErr(`学生アルバイトの月間上限は${STUDENT_MAX_HOURS}時間です（${maxH}hは設定できません）`);
        return;
      }
      showErr('');
      try {
        if (isEdit) {
          await api(`/shop/staffs/${s.id}`, { method: 'PUT', body: JSON.stringify({ name: g('#f_name'), hourly_wage: +g('#f_wage'), min_hours_per_month: +g('#f_min'), max_hours_per_month: +g('#f_max'), is_resigned: !!+g('#f_resign'), password: g('#f_pw') || undefined }) });
        } else {
          await api('/shop/staffs', { method: 'POST', body: JSON.stringify({ staff_code: g('#f_code'), name: g('#f_name'), password: g('#f_pw'), role: g('#f_role'), hourly_wage: +g('#f_wage'), min_hours_per_month: +g('#f_min'), max_hours_per_month: +g('#f_max') }) });
        }
        close(); toast('保存しました', 'success'); navigateTo('staffs');
      } catch (e) {
        // APIのエラーメッセージ（例: "パスワードは8文字以上で設定してください"）をインライン表示
        showErr(e.message || '保存に失敗しました');
      }
    });
  // ロール変更で「学生」を選択したとき上限のヒントを表示
  const roleSel = wrap.querySelector('#f_role');
  const hintBox = wrap.querySelector('#f_role_hint');
  const maxInput = wrap.querySelector('#f_max');
  const maxLabel = wrap.querySelector('label[for="f_max"]');
  function syncRoleUI() {
    const isStu = roleSel.value === 'student';
    if (hintBox) hintBox.style.display = isStu ? 'block' : 'none';
    if (isStu) {
      maxInput.max = String(STUDENT_MAX_HOURS);
      if (parseInt(maxInput.value, 10) > STUDENT_MAX_HOURS) maxInput.value = String(STUDENT_MAX_HOURS);
      if (maxLabel) maxLabel.innerHTML = `上限h <span class="text-danger small">(学生は${STUDENT_MAX_HOURS})</span>`;
    } else {
      maxInput.removeAttribute('max');
      if (maxLabel) maxLabel.innerHTML = '上限h';
    }
  }
  roleSel?.addEventListener('change', syncRoleUI);
  // 上限h を直接編集した際も学生なら80にクランプ
  maxInput?.addEventListener('input', () => {
    if (roleSel.value === 'student') {
      const v = parseInt(maxInput.value, 10);
      if (!isNaN(v) && v > STUDENT_MAX_HOURS) maxInput.value = String(STUDENT_MAX_HOURS);
    }
  });
  maxInput?.addEventListener('blur', syncRoleUI);
  // リアルタイム検証: 入力ごとにルールの check/cross を切替
  const pwInput = wrap.querySelector('#f_pw');
  const ruleEls = wrap.querySelectorAll('.pw-rule');
  const updateRules = () => {
    const v = pwInput.value || '';
    const checks = {
      len: v.length >= 8,
      alpha: /[A-Za-z]/.test(v),
      digit: /[0-9]/.test(v),
    };
    ruleEls.forEach((el) => {
      const k = el.dataset.rule;
      const ok = checks[k];
      el.classList.toggle('ok', !!ok && v.length > 0);
      el.classList.toggle('ng', !ok && v.length > 0);
      el.querySelector('i').className = ok ? 'bi bi-check-circle-fill' : 'bi bi-x-circle-fill';
    });
  };
  pwInput?.addEventListener('input', () => {
    updateRules();
    wrap.querySelector('#f_err').innerHTML = '';
    pwInput.classList.remove('is-invalid');
  });
  updateRules();
}

/* クライアント側パスワードバリデーション（src/utils.validate_password と同要件） */
function validatePassword(pw) {
  if (!pw || pw.length < 8) return 'パスワードは8文字以上で設定してください';
  if (!/[A-Za-z]/.test(pw)) return 'パスワードに英字を含めてください';
  if (!/[0-9]/.test(pw)) return 'パスワードに数字を含めてください';
  return null;
}
function confirmDeleteStaff(staffId, staffName) {
  openModal(`<i class="bi bi-trash text-danger"></i> スタッフ削除`,
    `<div class="text-center py-2">
      <div class="mb-2"><i class="bi bi-exclamation-triangle-fill text-danger" style="font-size:2.2rem"></i></div>
      <p class="mb-1"><strong>${esc(staffName)}</strong> を削除しますか？</p>
      <p class="small text-secondary mb-0">このスタッフの固定シフト・シフト実績・希望履歴・変更申請・通知も全て削除されます。<br>この操作は取り消せません。退職として残す場合は「編集」からステータスを退職にしてください。</p>
    </div>`,
    async (w, close) => {
      try {
        await api(`/shop/staffs/${staffId}`, { method: 'DELETE' });
        close(); toast('削除しました', 'success'); navigateTo('staffs');
      } catch (e) { toast(e.message, 'error'); close(); }
    },
    { saveLabel: '削除する', btnClass: 'btn-danger' });
}
function showFixedShiftModal(staffId, staffName) {
  api('/shop/fixed-shifts').then((d) => {
    let mine = d.fixed_shifts.filter((f) => f.staff_id === staffId);
    const render = (w) => {
      w.querySelector('#fxList').innerHTML = mine.length ? mine.map((f) => `
        <div class="list-row"><div>${badge(WD[f.weekday] + '曜', 'info')} ${esc(f.start_time)} - ${esc(f.end_time)}</div>
        <div class="flex gap-1">
          <button class="btn btn-sm btn-light" data-edit="${f.id}" data-wd="${esc(f.weekday)}" data-st="${esc(f.start_time)}" data-et="${esc(f.end_time)}"><i class="bi bi-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger" data-del="${f.id}"><i class="bi bi-x"></i></button>
        </div></div>`).join('') : '<div class="small text-secondary">固定シフト未設定</div>';
      w.querySelectorAll('[data-del]').forEach((b) => b?.addEventListener('click', async () => { await api(`/shop/fixed-shifts/${b.dataset.del}`, { method: 'DELETE' }); mine = mine.filter((m) => m.id != b.dataset.del); render(w); }));
      w.querySelectorAll('[data-edit]').forEach((b) => {
        b?.addEventListener('click', () => openModal('<i class="bi bi-pencil"></i> 固定シフト編集',
          `<label class="form-label" for="eWd">曜日</label><select id="eWd" class="form-select mb-2">${WD.map((n, i) => `<option value="${i}" ${i == b.dataset.wd ? 'selected' : ''}>${n}曜</option>`).join('')}</select>
           <div class="row"><div class="col-6"><label class="form-label" for="eSt">開始</label><input id="eSt" class="form-control" value="${esc(b.dataset.st)}"></div><div class="col-6"><label class="form-label" for="eEt">終了</label><input id="eEt" class="form-control" value="${esc(b.dataset.et)}"></div></div>`,
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

/* ---------- MyShift (店舗管理者自身のシフト・希望) ---------- */
SCREENS.myshift = async function (el) {
  const tok = navToken();
  el.innerHTML = pageHead('マイシフト・希望', 'bi-calendar2-check', 'あなた自身のシフトと希望管理') +
    card(`<div id="myInfo"><div class="text-secondary small">読み込み中...</div></div>`) +
    card(sectionTitle('bi-calendar-check', '確定シフト（来月まで）') +
      `<div id="myShifts"><div class="text-secondary small">読み込み中...</div></div>`) +
    card(sectionTitle('bi-pencil-square', '希望の提出', `<button class="btn btn-primary btn-sm ms-2" id="addMyReqBtn"><i class="bi bi-plus-lg"></i> 希望を追加</button>`) +
      `<div id="myReqs"><div class="text-secondary small">読み込み中...</div></div>`) +
    card(sectionTitle('bi-clock-history', '希望履歴（全件）') +
      `<div id="myWishes"><div class="text-secondary small">読み込み中...</div></div>`);
  document.getElementById('addMyReqBtn')?.addEventListener('click', () => openMyReqModal(loadMyData));
  await loadMyData();

  async function loadMyData() {
    if (!isAlive(tok) || !el.isConnected) return;
    let me, shifts, reqs, wishes;
    try {
      [me, shifts, reqs, wishes] = await Promise.all([
        api('/shop/me'),
        api(`/shop/my-shifts?start=${todayStr().slice(0, 8) + '01'}&end=${plusMonths(2)}`),
        api('/shop/my-requests'),
        api('/shop/my-wishes'),
      ]);
    } catch (e) {
      if (!isAlive(tok) || !el.isConnected) return;
      // 権限エラー等の場合は分かりやすい案内を表示
      const msg = e.message || '';
      const infoBox = document.getElementById('myInfo');
      if (infoBox) {
        if (msg.includes('権限') || msg.includes('403')) {
          safeSetHTML(infoBox, `<div class="info-box"><i class="bi bi-exclamation-triangle text-warning"></i> <strong>この機能を利用できません</strong><br>マイシフト・希望は <strong>店舗管理者（manager）アカウント</strong>でログイン中のみ利用できます。<br>現在のログイン権限では利用できない、または旧仕様の店主アカウントの可能性があります。<br><br><span class="small">エラー詳細: ${esc(msg)}</span></div>`);
        } else {
          safeSetHTML(infoBox, `<div class="info-box text-danger"><i class="bi bi-exclamation-circle"></i> ${esc(msg)}</div>`);
        }
      }
      ['myShifts', 'myReqs', 'myWishes'].forEach((id) => {
        const b = document.getElementById(id);
        if (b) safeSetHTML(b, '<div class="text-secondary small">—</div>');
      });
      return;
    }
    if (!isAlive(tok) || !el.isConnected) return;
    // 自身の情報
    const infoBox = document.getElementById('myInfo');
    if (infoBox) {
      if (!me.staff) {
        safeSetHTML(infoBox, `<div class="info-box"><i class="bi bi-info-circle text-primary"></i> <strong>希望提出機能について</strong><br>このアカウントは <strong>旧仕様の店主ログイン</strong>（shops テーブルのパスワード直接利用）のため、希望提出機能は利用できません。<br>新仕様の <strong>manager ロール</strong> でログインすると、シフト希望を出せるようになります（システム管理者にご相談ください）。</div>`);
        ['myShifts', 'myReqs', 'myWishes'].forEach((id) => {
          const b = document.getElementById(id);
          if (b) safeSetHTML(b, '<div class="text-secondary small">このアカウントでは利用できません</div>');
        });
        return;
      }
      safeSetHTML(infoBox, `<div class="my-info-row"><i class="bi bi-person-badge"></i> <strong>${esc(me.staff.name)}</strong> (${esc(me.staff.staff_code)}) ・ ${roleLabel(me.staff.role)} ・ 時給${esc(me.staff.hourly_wage)}円</div>`);
    }
    // 確定シフト（+ AIドラフトも確認用に表示）
    const shiftsBox = document.getElementById('myShifts');
    if (shiftsBox) {
      const list = (shifts.shifts || []).filter((s) =>
        s.status === 'confirmed' ||
        ((s.status === 'requested') && (s.reason || '').startsWith('AIドラフト')));
      if (!list.length) {
        safeSetHTML(shiftsBox, '<div class="text-secondary small">確定シフトはありません</div>');
      } else {
        safeSetHTML(shiftsBox, `<div class="table-wrap"><table class="data-table"><thead><tr><th>日付</th><th>曜日</th><th>時間</th><th>状態</th><th>休憩</th></tr></thead><tbody>${list.map((s) => {
          const d = s.start_datetime.slice(0, 10);
          const isDraft = s.status === 'requested';
          const stateBadge = isDraft ? badge('ドラフト', 'warning') : badge('確定', 'success');
          return `<tr><td class="num">${esc(d)}</td><td>${wdName(d)}</td><td class="num">${hm(s.start_datetime)} - ${hm(s.end_datetime)}</td><td>${stateBadge}</td><td class="num">${(s.break_time_minutes || 0)}分</td></tr>`;
        }).join('')}</tbody></table></div>`);
      }
    }
    // 提出済み希望（pending）
    const reqsBox = document.getElementById('myReqs');
    if (reqsBox) {
      const list = reqs.requests || [];
      if (!list.length) {
        safeSetHTML(reqsBox, '<div class="text-secondary small">提出中の希望はありません。「希望を追加」ボタンから提出できます。</div>');
      } else {
          safeSetHTML(reqsBox, `<div class="list-rows">${list.map((r) => `
            <div class="list-row">
              <div><strong class="num">${esc(r.start_datetime.slice(0, 16).replace('T', ' '))}</strong> 〜 <span class="num">${esc((r.end_datetime || '').slice(11, 16))}</span>
                ${r.availability === 'rest' ? badge('休希望', 'danger') : r.availability ? badge({ any: 'いつでも', morning: '早番', evening: '遅番' }[r.availability] || '柔軟', 'info') : badge('希望', 'warning')}
                <div class="small text-secondary">${esc(r.reason || '')}</div>
              </div>
              <button class="btn btn-sm btn-outline-danger" data-del="${r.id}"><i class="bi bi-x"></i></button>
            </div>`).join('')}</div>`);
        reqsBox.querySelectorAll('[data-del]').forEach((b) => b?.addEventListener('click', async () => {
          if (!confirm('この希望を削除しますか？')) return;
          try {
            await api(`/shop/my-requests/${b.dataset.del}`, { method: 'DELETE' });
            toast('削除しました', 'success');
            loadMyData();
          } catch (e) { toast(e.message, 'error'); }
        }));
      }
    }
    // 希望履歴
    const wishesBox = document.getElementById('myWishes');
    if (wishesBox) {
      const list = wishes.wishes || [];
      if (!list.length) {
        safeSetHTML(wishesBox, '<div class="text-secondary small">希望履歴はありません</div>');
      } else {
        safeSetHTML(wishesBox, `<div class="table-wrap"><table class="data-table"><thead><tr><th>日付</th><th>時間</th><th>種別</th><th>提出日時</th></tr></thead><tbody>${list.slice(0, 30).map((w) => `
          <tr><td class="num">${esc((w.start_datetime || '').slice(0, 10))}</td>
          <td class="num">${hm(w.start_datetime)} - ${hm(w.end_datetime)}</td>
          <td>${w.availability ? badge({ any: 'いつでも', morning: '早番', evening: '遅番' }[w.availability] || '柔軟', 'info') : badge('時間指定', 'muted')}</td>
          <td class="num small">${esc((w.submitted_at || '').replace('T', ' ').slice(0, 16))}</td></tr>`).join('')}</tbody></table></div>`);
      }
    }
  }
};

function openMyReqModal(onDone) {
  const today = todayStr();
  const wrap = openModal('<i class="bi bi-pencil-square"></i> 希望を提出',
    `<p class="small text-secondary mb-2">以下の3パターンから選べます:</p>
     <div class="mb-2">
       <div class="form-check">
         <input class="form-check-input" type="radio" name="myRqType" value="time" checked id="rqTypeTime">
         <label class="form-check-label" for="rqTypeTime"><strong>① 時間指定</strong>（働きたい時間を指定）</label>
       </div>
       <div class="form-check">
         <input class="form-check-input" type="radio" name="myRqType" value="flex" id="rqTypeFlex">
         <label class="form-check-label" for="rqTypeFlex"><strong>② 柔軟希望</strong>（時間は目安・シフト時間内で調整可）</label>
       </div>
       <div class="form-check">
         <input class="form-check-input" type="radio" name="myRqType" value="rest" id="rqTypeRest">
         <label class="form-check-label" for="rqTypeRest"><strong>③ 休希望</strong>（その日は働かない）</label>
       </div>
     </div>
     <div class="row mb-2">
       <div class="col-12"><label class="form-label" for="myRqDate">日付 <span class="text-danger">*</span></label><input type="date" id="myRqDate" class="form-control" value="${today}"></div>
     </div>
     <div id="myRqTimeRow" class="row mb-2">
       <div class="col-6"><label class="form-label" for="myRqSt">開始</label><input type="time" id="myRqSt" class="form-control" value="09:00"></div>
       <div class="col-6"><label class="form-label" for="myRqEt">終了</label><input type="time" id="myRqEt" class="form-control" value="18:00"></div>
     </div>
     <div id="myRqFlexBox" style="display:none">
       <label class="form-label">希望時間帯</label>
       <select id="myRqAvail" class="form-select">
         <option value="any">いつでもOK</option>
         <option value="morning">早番（朝〜昼）</option>
         <option value="evening">遅番（夕方〜夜）</option>
       </select>
     </div>
     <div id="myRqRestNote" style="display:none" class="info-box mt-2">
       <i class="bi bi-info-circle"></i> この日は働かない希望として提出します。AI自動生成でこの希望が優先されます。
     </div>
     <div class="form-error mt-2" id="myRqErr"></div>`,
    async (w, close) => {
      const errBox = w.querySelector('#myRqErr');
      const showErr = (m) => { if (errBox) errBox.innerHTML = m ? `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(m)}` : ''; };
      showErr('');
      const date = w.querySelector('#myRqDate').value;
      if (!date) return showErr('日付を入力してください');
      const type = w.querySelector('input[name="myRqType"]:checked').value;
      const st = w.querySelector('#myRqSt').value;
      const et = w.querySelector('#myRqEt').value;
      let start_iso, end_iso, avail = null, reason = '管理者希望提出';
      if (type === 'rest') {
        start_iso = `${date}T00:00:00`;
        end_iso = `${date}T23:59:59`;
        avail = 'rest';
        reason = '休希望(管理者)';
      } else if (type === 'flex') {
        start_iso = `${date}T${st || '00:00'}:00`;
        end_iso = `${date}T${et || '23:59'}:00`;
        avail = w.querySelector('#myRqAvail').value;
        reason = '柔軟希望(管理者)';
      } else {
        if (!st || !et) return showErr('開始・終了時刻を入力してください');
        start_iso = `${date}T${st}:00`;
        end_iso = `${date}T${et}:00`;
      }
      try {
        const body = { shifts: [{ start_datetime: start_iso, end_datetime: end_iso }] };
        if (avail) body.shifts[0].availability = avail;
        const r = await api('/shop/my-requests', { method: 'POST', body: JSON.stringify(body) });
        close();
        toast(r.message || '提出しました', 'success');
        onDone?.();
      } catch (e) {
        showErr(e.message || '提出に失敗しました');
      }
    });
  // ラジオボタンで表示切り替え
  const radios = wrap.querySelectorAll('input[name="myRqType"]');
  const timeRow = wrap.querySelector('#myRqTimeRow');
  const flexBox = wrap.querySelector('#myRqFlexBox');
  const restNote = wrap.querySelector('#myRqRestNote');
  function update() {
    const v = wrap.querySelector('input[name="myRqType"]:checked').value;
    if (timeRow) timeRow.style.display = (v === 'rest') ? 'none' : 'flex';
    if (flexBox) flexBox.style.display = (v === 'flex') ? 'block' : 'none';
    if (restNote) restNote.style.display = (v === 'rest') ? 'block' : 'none';
  }
  radios.forEach((r) => r?.addEventListener('change', update));
  update();
}

/* ---------- Requests (希望表管理) ---------- */
SCREENS.requests = async function (el) {
  const tok = navToken();
  // 期間を選べるように上部に期間フィルタを追加
  const today = todayStr();
  const defaultStart = today.slice(0, 8) + '01';
  const defaultEnd = plusMonths(2);
  el.innerHTML = pageHead('希望表管理', 'bi-inbox', 'スタッフごとの希望シフト一覧') +
    card(`<div class="row mb-3">
        <div class="col-5"><label class="form-label" for="reqStart">開始</label><input type="date" id="reqStart" class="form-control" value="${defaultStart}"></div>
        <div class="col-5"><label class="form-label" for="reqEnd">終了</label><input type="date" id="reqEnd" class="form-control" value="${defaultEnd}"></div>
        <div class="col-2 flex items-end"><button class="btn btn-primary w-full" id="reqLoadBtn">表示</button></div>
      </div>
      <div class="mb-3"><button class="btn btn-light w-full" id="reqImportBtn"><i class="bi bi-clipboard-plus"></i> テキストから取り込む</button></div>
      <div id="reqList"><div class="text-secondary small">「表示」ボタンを押してください</div></div>`);
  document.getElementById('reqLoadBtn')?.addEventListener('click', () => loadReqList());
  document.getElementById('reqImportBtn')?.addEventListener('click', () => openWishImportModal((range) => {
    // M-9: 取り込んだ日付が現在の一覧フィルタ範囲外だと、登録成功のトーストが出ても
    // 一覧に何も表示されず「失敗した」ように見える。フィルタを取り込んだ範囲まで広げる。
    // range は実際に登録した項目の日付min〜max（対象月ではない。8/31〜9/2のように
    // 対象月をまたぐ取り込みだと対象月だけでは9月分が一覧から漏れるため）。
    if (range && range.start && range.end) {
      const sEl = document.getElementById('reqStart');
      const eEl = document.getElementById('reqEnd');
      if (sEl && (!sEl.value || sEl.value > range.start)) sEl.value = range.start;
      if (eEl && (!eEl.value || eEl.value < range.end)) eEl.value = range.end;
    }
    loadReqList();
  }));
  await loadReqList();

  async function loadReqList() {
    if (!isAlive(tok) || !el.isConnected) return;
    const box = document.getElementById('reqList');
    if (!box) return;
    const startDate = document.getElementById('reqStart')?.value || defaultStart;
    const endDate = document.getElementById('reqEnd')?.value || defaultEnd;
    safeSetHTML(box, '<div class="text-secondary small">読み込み中...</div>');
    try {
      // ★【インシデント対策】希望表は wish_history を見る（シフト確定後も残す）
      // 従来は shifts.requested を見ていたため、AI生成や確定で希望が画面から消えた。
      // wish_history は永久履歴なので、シフトが確定しても希望は残り続ける。
      const [wishesD, shiftsD, staffsD] = await Promise.all([
        api(`/shop/wishes?start=${startDate}&end=${endDate}`),
        api(`/shop/shifts?start=${startDate}&end=${endDate}`),
        api('/shop/staffs'),
      ]);
      if (!isAlive(tok) || !el.isConnected) return;
      // wish_history から希望を取得（note で AIドラフト等を除外することも可能だが、
      // wish_history にはスタッフ希望・管理者希望のみ保存されるのでそのまま使う）
      const reqs = (wishesD.wishes || []).filter((w) => {
        const note = w.note || '';
        // AIドラフト由来の wish_history は存在しないが念のため除外
        return !note.startsWith('AIドラフト') && !note.startsWith('AI生成');
      });
      const staffs = staffsD.staffs || [];
      const staffMap = {};
      staffs.forEach((s) => staffMap[s.id] = s);
      // シフトが確定済みか判定するためのマップ (staff_id, date) → confirmed の有無
      const confirmedKey = new Set();
      (shiftsD.shifts || []).forEach((s) => {
        if (s.status === 'confirmed') {
          confirmedKey.add(`${s.staff_id}|${(s.start_datetime || '').slice(0, 10)}`);
        }
      });
      // staff_id ごとにグループ化。各希望に confirmed フラグを付与
      const byStaff = {};
      reqs.forEach((r) => {
        const sid = r.staff_id;
        if (!byStaff[sid]) byStaff[sid] = [];
        // この希望が確定済みシフトに対応しているか（同スタッフ・同日のconfirmed）
        r._confirmed = confirmedKey.has(`${sid}|${(r.start_datetime || '').slice(0, 10)}`);
        byStaff[sid].push(r);
      });
      // 表示用配列（希望数降順）
      const cards = Object.entries(byStaff).map(([sid, list]) => ({
        staff: staffMap[sid] || { id: +sid, name: '不明#' + sid, staff_code: '?', role: '' },
        list: list.sort((a, b) => (a.start_datetime || '').localeCompare(b.start_datetime || '')),
      })).sort((a, b) => b.list.length - a.list.length);

      if (!cards.length) {
        safeSetHTML(box, '<div class="info-box"><i class="bi bi-info-circle"></i> この期間の希望シフトはありません。スタッフに希望提出を促しましょう。</div>');
        return;
      }
      // サマリ
      const totalReqs = reqs.length;
      const restCount = reqs.filter((r) => r.availability === 'rest').length;
      const flexCount = reqs.filter((r) => r.availability && r.availability !== 'rest').length;
      const timeCount = totalReqs - restCount - flexCount;
      // カードグリッド（スタッフ別）
      safeSetHTML(box,
        `<div class="info-box mb-3">
          <strong>集計:</strong> ${cards.length}名 / 計 ${totalReqs}件
           <span class="badge-soft warning ms-2">時間指定 ${timeCount}</span>
           <span class="badge-soft info">柔軟 ${flexCount}</span>
           <span class="badge-soft danger">休希望 ${restCount}</span>
        </div>
        <div class="req-cards-grid">
          ${cards.map((c) => renderStaffReqCard(c)).join('')}
        </div>`);
      // カードクリックで個人詳細モーダル
      box.querySelectorAll('[data-staff-detail]').forEach((b) => b?.addEventListener('click', () => {
        const sid = +b.dataset.staffDetail;
        const card = cards.find((c) => c.staff.id === sid);
        if (card) openStaffReqDetailModal(card);
      }));
    } catch (e) {
      if (!isAlive(tok) || !el.isConnected) return;
      safeSetHTML(box, `<div class="text-danger">${esc(e.message)}</div>`);
    }
  }
};

function renderStaffReqCard({ staff, list }) {
  const restCount = list.filter((r) => r.availability === 'rest').length;
  const flexCount = list.filter((r) => r.availability && r.availability !== 'rest').length;
  const timeCount = list.length - restCount - flexCount;
  const confirmedCount = list.filter((r) => r._confirmed).length;
  // 最初と最後の日付
  const firstDate = list[0]?.start_datetime?.slice(0, 10) || '';
  const lastDate = list[list.length - 1]?.start_datetime?.slice(0, 10) || '';
  const roleBadge = staff.role === 'manager' ? badge('店長', 'info')
    : staff.role === 'employee' ? badge('社員', 'success')
    : staff.role === 'student' ? badge('学生', 'warning')
    : badge('バイト', 'muted');
  return `<div class="req-staff-card" data-staff-detail="${staff.id}">
    <div class="req-card-header">
      <div class="req-card-name">
        <span class="dot ${roleClass(staff.role)}"></span>
        <strong>${esc(staff.name)}</strong>
        <span class="text-secondary small">${esc(staff.staff_code || '')}</span>
        ${roleBadge}
      </div>
      <div class="req-card-count">
        <span class="num"><strong>${list.length}</strong>件</span>
        <i class="bi bi-chevron-right text-muted"></i>
      </div>
    </div>
    <div class="req-card-badges">
      ${timeCount > 0 ? `<span class="badge-soft warning">時間 ${timeCount}</span>` : ''}
      ${flexCount > 0 ? `<span class="badge-soft info">柔軟 ${flexCount}</span>` : ''}
      ${restCount > 0 ? `<span class="badge-soft danger">休希望 ${restCount}</span>` : ''}
      ${confirmedCount > 0 ? `<span class="badge-soft success">確定済 ${confirmedCount}</span>` : ''}
    </div>
    <div class="req-card-period small text-secondary">
      ${firstDate === lastDate ? esc(firstDate) : esc(firstDate) + ' 〜 ' + esc(lastDate)}
    </div>
  </div>`;
}

function openStaffReqDetailModal({ staff, list }) {
  // カレンダー風リスト表示
  const rows = list.map((r) => {
    const day = (r.start_datetime || '').slice(0, 10);
    const st = (r.start_datetime || '').slice(11, 16);
    const et = (r.end_datetime || '').slice(11, 16);
    let badgeHtml = '';
    let timeText = '';
    if (r.availability === 'rest') {
      badgeHtml = badge('休希望', 'danger');
      timeText = '終日（働かない）';
    } else if (r.availability) {
      const label = { any: 'いつでも', morning: '早番', evening: '遅番' }[r.availability] || '柔軟';
      badgeHtml = badge(label, 'info');
      timeText = `${st} - ${et}（目安）`;
    } else {
      badgeHtml = badge('時間指定', 'warning');
      timeText = `${st} - ${et}`;
    }
    const stateBadge = r._confirmed ? badge('確定済', 'success') : badge('調整待ち', 'warning');
    return `<tr>
      <td class="num">${esc(day)} <span class="text-secondary small">(${wdName(day)})</span></td>
      <td class="num">${esc(timeText)}</td>
      <td>${badgeHtml}</td>
      <td>${stateBadge}</td>
      <td class="small text-secondary">${esc(r.note || r.reason || '')}</td>
    </tr>`;
  }).join('');
  const m = openModal(`<i class="bi bi-person"></i> ${esc(staff.name)} さんの希望表`,
    `<div class="my-info-row mb-2">
       <i class="bi bi-person-badge"></i>
       <strong>${esc(staff.name)}</strong>
       <span class="text-secondary">${esc(staff.staff_code || '')}</span>
        ${staff.role === 'manager' ? badge('店長', 'info') : staff.role === 'employee' ? badge('社員', 'success') : staff.role === 'student' ? badge('学生', 'warning') : badge('バイト', 'muted')}
        <span class="small text-secondary ms-auto">希望 ${list.length} 件</span>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>日付</th><th>時間</th><th>種別</th><th>状態</th><th>メモ</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="5" class="text-secondary small">希望なし</td></tr>'}</tbody>
        </table>
      </div>`,
    null, { saveLabel: '閉じる' });
}

/* ---------- Wish text import (希望テキスト取り込み) ----------
   店長が LINE 等のテキストを貼り付けて、複数スタッフ分の希望を代理入力する。
   流れ: ステップ1(貼付・解析) → ステップ2(月間カレンダーで確認・修正・登録)。
   parse は保存しないので何度でもやり直せる。bulk 登録は日付ごとに展開して送る。
   参考: SCREENS.request のスタッフ希望カレンダー（app.js内 `let wishState`/`wishMonth`
   の少し下）。見た目・操作感（.wish-cal/.wish-cell/.wmark）をそのまま流用している。
   ここでの状態は `wishState`/`wishMonth`（スタッフ本人用）とは別物として state object
   に閉じ込め、干渉しないようにする。 */

/* 対象月セレクトの選択肢: 当月を含む前後（-1〜+2ヶ月）。 */
function _wtiMonthOptions() {
  const opts = [];
  const now = new Date();
  for (let off = -1; off <= 2; off++) {
    const d = new Date(now.getFullYear(), now.getMonth() + off, 1);
    const val = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    opts.push({ value: val, label: `${d.getFullYear()}年${d.getMonth() + 1}月` });
  }
  return opts;
}
function _wtiLastDayOfMonth(yearMonth) {
  const [y, m] = yearMonth.split('-').map(Number);
  return new Date(y, m, 0).getDate();
}

/* 解析結果 entries（dates配列を持つ）を、日付ごとに1件へ展開したフラットな
   作業状態に変換する。カレンダーでの個別編集・削除・スタッフ割り当てを
   単純にするため、以降の画面操作はすべてこのフラット配列を対象に行う。
   entryIdx は「未割り当て」一覧のグルーピング（元の1文をまとめて振り分ける）に使う。 */
function _wtiFlatten(entries) {
  const items = [];
  (entries || []).forEach((e, idx) => {
    (e.dates || []).forEach((date) => {
      items.push({
        uid: `${idx}-${date}-${Math.random().toString(36).slice(2, 7)}`,
        entryIdx: idx,
        staffId: e.staff_id || null,
        staffHint: e.staff_hint || null,
        date,
        availability: e.availability,
        start: e.start || null,
        end: e.end || null,
        raw: e.raw || '',
        // I-3: raw_verified はサーバが「AIの返した raw が貼り付けテキストに実在するか」
        // を検証した結果（無ければ undefined＝旧サーバ・検証未実装として扱う。false の
        // ときだけ警告を出す。true/undefined は警告しない）。
        rawVerified: e.raw_verified,
        overwriteConfirmed: false,
      });
    });
  });
  return items;
}

async function openWishImportModal(onImported) {
  let staffs = [];
  try {
    const sd = await api('/shop/staffs');
    staffs = (sd.staffs || []).filter((s) => !s.is_resigned);
  } catch (e) { toast('スタッフ一覧の取得に失敗しました', 'error'); return; }
  const state = {
    staffs, items: [], unparsed: [], source: null, periods: [],
    // I-4: staff_id|date → 既存希望（配列。中身の availability/時刻を保持する。
    // 従来は Set で「有無」だけ持ち、詳細モーダルで「何が消えるか」を示せなかった）。
    existing: {},
    // I-7: どのスタッフ分の既存希望を取得済みか（差分取得のため）。
    existingFetchedStaffIds: new Set(),
    existingRange: null,
    yearMonth: todayStr().slice(0, 7), explicitStaffId: null, rawText: '',
    calStaffId: null, calMonth: null, onImported,
    // I-2: どのスタッフのカレンダーを一度でも表示したかを記録し、
    // 未確認のまま登録しようとした店長に注意を出すために使う。
    viewedStaffIds: new Set(),
    // fix round 3: #wtiSubmitMsg に書いた登録結果メッセージ（成功件数・失敗内容）を
    // state 側にも持たせる。_wtiRenderStep2 が毎回このHTMLを描くことで、
    // 再描画（成功済み項目の除去を画面に反映するために必須）が直後に走っても
    // メッセージが消えないようにする。
    submitMsg: null,
    // Minor(3399): 部分失敗→再送を繰り返す間、実際に登録できた日付をここへ累積する。
    // succeededItems（1回のsubmit呼び出しに閉じたローカル変数）だけを使うと、
    // 再送のたびに「今回成功した分」しか見えず、一覧側のフィルタ拡張（onImported）が
    // 過去の成功分の日付を取りこぼす。
    succeededDates: [],
  };
  const wrap = openModal('<i class="bi bi-clipboard-plus"></i> テキストから取り込む',
    '<div class="text-secondary small">読み込み中...</div>', null, { width: 640 });
  _wtiRenderStep1(wrap, state);
}

/* I-7: 指定スタッフ分の既存希望（wish_history）をまだ取得していなければ取得し、
   state.existing にマージする。既に取得済みのスタッフは飛ばす（差分取得）ので、
   「未割り当て」から新しいスタッフを振り分けるたびに呼んでも無駄打ちしない。
   I-4: 中身（availability・時刻）まで保持する必要があるため、has()だけで済む Set
   ではなく `staff_id|date` → 希望配列 のオブジェクトに詰める（同日複数件もありうる）。
   staff_id パラメータをサーバが未対応でも、余分なクエリパラメータは無視されるだけ
   なので落ちない（省略時は現在の挙動のまま）。 */
async function _wtiEnsureExistingLoaded(state, staffIds) {
  const missing = [...new Set((staffIds || []).filter((sid) => sid && !state.existingFetchedStaffIds.has(sid)))];
  if (!missing.length || !state.existingRange) return;
  missing.forEach((sid) => state.existingFetchedStaffIds.add(sid));
  const { start, end } = state.existingRange;
  const staffQS = missing.map((sid) => `&staff_id=${sid}`).join('');
  try {
    const d = await api(`/shop/wishes?start=${start}&end=${end}${staffQS}`);
    (d.wishes || []).forEach((w) => {
      const key = `${w.staff_id}|${(w.start_datetime || '').slice(0, 10)}`;
      (state.existing[key] = state.existing[key] || []).push(w);
    });
  } catch (e) {
    // 既存希望の照合ができなくても取り込み自体は続行できるようにする（安全側に倒す：
    // 印・上書き判定が出ないだけで、登録自体は overwrite:false のためサーバ側の
    // 重複スキップに委ねられる。既存踏襲の方針）。
  }
}
function _wtiExistingFor(state, staffId, date) { return state.existing[`${staffId}|${date}`] || []; }
function _wtiHasExisting(state, staffId, date) { return _wtiExistingFor(state, staffId, date).length > 0; }

/* I-4: 既存希望1件を「休み希望 17:00-22:00」のような短い日本語にする。
   N-1修正: サーバのCritical C-1で「時間指定の希望は shifts/wish_history どちらも
   availability=NULL」に統一された（既存の /api/staff/requests・実UIの
   submitWish（public/app.js:4362）が time のとき availability を送らないのと
   揃えるため）。「time」という文字列値は wish_history には存在しないため、
   w.availability === 'time' を条件にすると常に外れ、時間指定の既存希望が
   軒並み「種別不明」になっていた（I-4が守ろうとした「何が消えるか見せる」が
   最も情報価値の高いケースで機能しない状態だった）。availability が無く
   start/end があるものを「時間指定」として扱う。 */
function _wtiExistingLabel(w) {
  const label = { rest: '休み希望', any: 'いつでも可', morning: '早番希望', evening: '遅番希望' };
  const isTimeSpecified = !w.availability && w.start_datetime && w.end_datetime;
  const base = isTimeSpecified ? '時間指定' : (label[w.availability] || w.availability || '種別不明');
  if (isTimeSpecified) {
    const st = (w.start_datetime.split('T')[1] || '').slice(0, 5);
    const et = (w.end_datetime.split('T')[1] || '').slice(0, 5);
    if (st && et) return `${base} ${st}-${et}`;
  }
  return base;
}

/* ★最優先修正: skipped の内訳を人間可読にする。従来は skipped を無条件に
   「重複のため」と断定していたが、実際には他店舗/退職スタッフ・enum外の
   availability・wish_history書き込み失敗によるrollback（データが失われた
   可能性）も同じ skipped に含まれる。skipped_detail（サーバ未対応なら null）
   から内訳を組み立て、rollback の有無は hasRollback で呼び出し側に伝える。
   skipped_detail が無い場合は理由を断定しない表現にフォールバックする。 */
function _wtiSkippedSummary(skipped, skippedDetail) {
  if (!skipped) return { phrase: '', hasRollback: false };
  if (!skippedDetail) return { phrase: `${skipped}件はスキップされました`, hasRollback: false };
  const duplicate = skippedDetail.duplicate || 0;
  const invalid = skippedDetail.invalid || 0;
  const rollback = skippedDetail.rollback || 0;
  const parts = [];
  if (duplicate) parts.push(`重複 ${duplicate}件`);
  if (invalid) parts.push(`不正な入力 ${invalid}件`);
  if (rollback) parts.push(`書き込みに失敗して取り消し ${rollback}件`);
  const accounted = duplicate + invalid + rollback;
  if (accounted < skipped) parts.push(`その他 ${skipped - accounted}件`);
  const phrase = parts.length ? `${skipped}件はスキップ（${parts.join('・')}）` : `${skipped}件はスキップされました`;
  return { phrase, hasRollback: rollback > 0 };
}

/* ステップ1: 貼り付け（対象月・スタッフ・テキスト・解析ボタン） */
function _wtiRenderStep1(wrap, state) {
  const titleEl = wrap.querySelector('.modal-title');
  if (titleEl) titleEl.innerHTML = '<i class="bi bi-clipboard-plus"></i> テキストから取り込む';
  const body = wrap.querySelector('.modal-body');
  if (!body) return;
  const monthOpts = _wtiMonthOptions();
  const staffOptsHtml = state.staffs.map((s) =>
    `<option value="${s.id}"${state.explicitStaffId === s.id ? ' selected' : ''}>${esc(s.name)}（${roleLabel(s.role)}）</option>`).join('');
  safeSetHTML(body, `
    <div class="row mb-2">
      <div class="col-6"><label class="form-label" for="wtiMonth">対象月</label>
        <select id="wtiMonth" class="form-select">${monthOpts.map((o) =>
          `<option value="${o.value}"${o.value === state.yearMonth ? ' selected' : ''}>${o.label}</option>`).join('')}</select>
      </div>
      <div class="col-6"><label class="form-label" for="wtiStaff">スタッフ</label>
        <select id="wtiStaff" class="form-select"><option value="">自動判定</option>${staffOptsHtml}</select>
      </div>
    </div>
    <label class="form-label" for="wtiText">貼り付けるテキスト</label>
    <textarea id="wtiText" class="form-control mb-2" rows="7" placeholder="例：8/3は休みたいです&#10;8/5、8/7、8/9は17時から22時まで入れます">${esc(state.rawText || '')}</textarea>
    <button class="btn btn-primary w-full" id="wtiParseBtn" type="button"><i class="bi bi-magic"></i> 解析する</button>
    <div id="wtiParseMsg" class="mt-2"></div>`);
  wrap.querySelector('#wtiParseBtn')?.addEventListener('click', () => _wtiParse(wrap, state));
}

async function _wtiParse(wrap, state) {
  const text = (wrap.querySelector('#wtiText')?.value || '').trim();
  if (!text) { toast('テキストを入力してください', 'error'); return; }
  state.rawText = text;
  state.yearMonth = wrap.querySelector('#wtiMonth')?.value || state.yearMonth;
  const staffSel = wrap.querySelector('#wtiStaff')?.value || '';
  state.explicitStaffId = staffSel ? +staffSel : null;
  const msgBox = wrap.querySelector('#wtiParseMsg');
  if (msgBox) msgBox.innerHTML = '<div class="text-secondary small">解析中...</div>';
  setLoading(true);
  try {
    const body = { text, year_month: state.yearMonth };
    if (state.explicitStaffId) body.staff_id = state.explicitStaffId;
    const r = await api('/shop/wishes/parse', { method: 'POST', body: JSON.stringify(body) });
    state.source = r.source;
    state.unparsed = r.unparsed || [];
    state.items = _wtiFlatten(r.entries || []);
    state.viewedStaffIds = new Set();
    if (!state.items.length) {
      // I-4: entries が0件（＝解析が最も失敗しているケース）でも、fallback注記と
      // unparsed は情報価値が最大なので必ず表示する（設計書§4「unparsedは捨てない」）。
      const fallbackNote = state.source === 'fallback'
        ? '<div class="alert alert-warning py-2 mb-2"><i class="bi bi-exclamation-triangle"></i> 簡易解析で読み取りました。内容をよく確認してください。</div>' : '';
      if (msgBox) msgBox.innerHTML = fallbackNote +
        '<div class="alert alert-warning py-2 mb-2">日付や希望内容を読み取れませんでした。文面を見直してください。</div>' +
        _wtiUnparsedHtml(state);
      setLoading(false);
      return;
    }
    // I-5: 既存希望の取得範囲は「対象月」固定ではなく、実際に解析で出た日付の
    // min〜maxで取る。カレンダーは月送りで対象月の外（例:8/31〜9/2をまたぐ解析
    // 結果の9月側）にも移動できるため、対象月だけに限定すると、その月の印・
    // 上書きチェックボックスが出ず、サーバ側は黙って重複skipするだけの三重の
    // 齟齬が起きる。対象月レンジは常に含めておく（相対表現の解決基準のため
    // items が万一空でも対象月自体は見えるようにする）。
    const dim = _wtiLastDayOfMonth(state.yearMonth);
    const itemDates = state.items.map((it) => it.date).sort();
    const start = [itemDates[0], `${state.yearMonth}-01`].sort()[0];
    const end = [itemDates[itemDates.length - 1], `${state.yearMonth}-${String(dim).padStart(2, '0')}`].sort().pop();
    state.existingRange = { start, end };
    state.existing = {};
    state.existingFetchedStaffIds = new Set();
    // I-7: /api/shop/wishes は ORDER BY start_datetime DESC LIMIT 500（src/app.py）。
    // レンジを広げるほど切り捨てに当たりやすく、DESC順のため切り捨てられるのは
    // 古い日付側＝対象月そのもの。staff_id で絞ることで、解析結果に出たスタッフ
    // 分だけを取得し、他スタッフの行で LIMIT を消費させない（11名運用で数ヶ月分
    // 溜まると顕在化していた問題）。staff_id 未対応のサーバでも余分なクエリ
    // パラメータは無視されるだけで壊れない（フォールバック不要）。
    const initialStaffIds = [...new Set(state.items.filter((it) => it.staffId).map((it) => it.staffId))];
    const [periodsD] = await Promise.all([
      api('/shop/periods').catch(() => ({ periods: [] })),
      _wtiEnsureExistingLoaded(state, initialStaffIds),
    ]);
    state.periods = periodsD.periods || [];
    const firstAssigned = state.items.find((it) => it.staffId);
    state.calStaffId = firstAssigned ? firstAssigned.staffId : null;
    const [iy, im] = state.yearMonth.split('-').map(Number);
    state.calMonth = { y: iy, m: im - 1 };
    _wtiRenderStep2(wrap, state);
  } catch (e) {
    if (msgBox) msgBox.innerHTML = `<div class="alert alert-danger py-2">${esc(e.message)}</div>`;
  } finally { setLoading(false); }
}

/* 期間外の警告文（対象日付のいずれかが、有効な募集期間のどれにも含まれない場合）。
   募集期間が1つも無い店舗では警告を出さない（未設定でも取り込めるようにする、設計書§3）。 */
function _wtiPeriodWarnHtml(state) {
  const periods = (state.periods || []).filter((p) => p.is_active);
  if (!periods.length) return '';
  const inAny = (d) => periods.some((p) => d >= p.start_date && d <= p.end_date);
  const outDates = [...new Set(state.items.map((it) => it.date))].filter((d) => !inAny(d)).sort();
  if (!outDates.length) return '';
  const sample = outDates.slice(0, 5).map((d) => d.slice(5)).join('、') + (outDates.length > 5 ? ' 他' : '');
  return `<div class="alert alert-warning py-2 mb-2"><i class="bi bi-exclamation-triangle"></i> ${outDates.length}件の日付が募集期間外です（${esc(sample)}）。登録は可能ですが、日付を確認してください。</div>`;
}

/* I-1: 未割り当て・期間外警告と同格の目立ち方にする（従来は灰色小文字で
   埋もれていた）。
   Minor（レビュー指摘）: 件数上限が無く、隣の期間外警告（_wtiPeriodWarnHtml）・
   重複警告（_wtiDuplicateWarnHtml）が5件＋「他」で打ち切っているのと非対称
   だった。サーバ側のC-1修正で unparsed に落ちる頻度が上がった（「土日」「平日」
   等を含む文が丸ごと落ちる）ため、375pxでカレンダーが画面外に押し出される
   実害があった。同じ「5件＋他」の形に揃える。 */
function _wtiUnparsedHtml(state) {
  if (!state.unparsed || !state.unparsed.length) return '';
  const shown = state.unparsed.slice(0, 5);
  const rows = shown.map((u) => `<li>${esc(u)}</li>`).join('');
  const moreNote = state.unparsed.length > 5 ? `<li class="text-secondary">他 ${state.unparsed.length - 5}件</li>` : '';
  return `<div class="wti-unparsed alert alert-warning py-2 mb-2"><i class="bi bi-question-circle"></i> 読み取れなかった文（${state.unparsed.length}件）。必要であれば手入力で補ってください。<ul class="small mb-0 mt-1">${rows}${moreNote}</ul></div>`;
}

/* C-1: 同一(スタッフ,日付)に複数の読み取りが残っている状態を検出する。
   src/ai.py のプロンプトは「内容が違えばentriesを分ける」と明示しており、
   同日に矛盾する2件が返るのは正常系。未割り当ては対象外（staff_id が
   定まって初めて「同じ枠の競合」になるため）。表示にも送信ブロックにも使う
   ため、判定ロジックはここに一本化する。 */
function _wtiFindDuplicateGroups(state) {
  const groups = {};
  state.items.forEach((it) => {
    if (!it.staffId) return;
    const key = `${it.staffId}|${it.date}`;
    (groups[key] = groups[key] || []).push(it);
  });
  const dup = {};
  Object.keys(groups).forEach((k) => { if (groups[k].length > 1) dup[k] = groups[k]; });
  return dup;
}

/* カレンダーは後勝ちで1件しか描けないため、店長が気づけるようここで必ず知らせる。
   期間外警告（_wtiPeriodWarnHtml）と同じく5件＋「他」で打ち切る（375pxでカレンダーが
   画面外に押し出されるのを防ぐ）。 */
function _wtiDuplicateWarnHtml(state) {
  const dupGroups = _wtiFindDuplicateGroups(state);
  const dupKeys = Object.keys(dupGroups);
  if (!dupKeys.length) return '';
  const parts = dupKeys.map((k) => {
    const [sid, date] = k.split('|');
    const s = state.staffs.find((x) => x.id === +sid);
    return `${esc(s ? s.name : '不明')} ${esc(date.slice(5))}`;
  });
  const sample = parts.slice(0, 5).join('、') + (parts.length > 5 ? ' 他' : '');
  return `<div class="alert alert-warning py-2 mb-2"><i class="bi bi-exclamation-triangle"></i> 同じ日に複数の読み取りがあります（${sample}）。日付を開いて、どちらを登録するか確認してください。</div>`;
}

/* I-2: 「1人目だけ見て登録」を防ぐための内訳表示。未確認（一度もカレンダーを
   開いていない）スタッフがいれば個別に示す。 */
function _wtiSummaryInfo(state) {
  const assignedStaffIds = [...new Set(state.items.filter((it) => it.staffId).map((it) => it.staffId))];
  const chips = assignedStaffIds.map((sid) => {
    const s = state.staffs.find((x) => x.id === sid);
    const cnt = state.items.filter((it) => it.staffId === sid).length;
    const unseen = !state.viewedStaffIds.has(sid);
    return badge(`${s ? s.name : '不明#' + sid} ${cnt}件${unseen ? '・未確認' : ''}`, unseen ? 'warning' : 'muted');
  });
  const total = state.items.filter((it) => it.staffId).length;
  return { chipsHtml: chips.join(' '), total };
}

/* H:MM → 「17-22」のような短縮表記（M-1）。分が :00 なら省略する。 */
function _wtiShortTime(t) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(t || '');
  if (!m) return '?';
  const h = String(Number(m[1]));
  return m[2] === '00' ? h : `${h}:${m[2]}`;
}

/* ステップ2: スタッフ切り替え + 月間カレンダー + 未割り当て一覧 + 登録ボタン。
   何かが変わるたびに（スタッフ切替・月送り・編集・削除・割り当て）この関数を
   呼び直して全体を再描画する。件数が小さい（1人1ヶ月分）ため、差分更新より
   単純さを優先した。 */
function _wtiRenderStep2(wrap, state) {
  const titleEl = wrap.querySelector('.modal-title');
  if (titleEl) titleEl.innerHTML = '<i class="bi bi-clipboard-check"></i> 取り込み内容の確認';
  const body = wrap.querySelector('.modal-body');
  if (!body) return;

  const sourceWarn = state.source === 'fallback'
    ? `<div class="alert alert-warning py-2 mb-2"><i class="bi bi-exclamation-triangle"></i> 簡易解析で読み取りました。内容をよく確認してください。</div>`
    : '';

  const assignedStaffIds = [...new Set(state.items.filter((it) => it.staffId).map((it) => it.staffId))];
  if (!state.calStaffId || !assignedStaffIds.includes(state.calStaffId)) {
    state.calStaffId = assignedStaffIds[0] || null;
  }
  // I-2: 表示した瞬間に「確認済み」として記録する（未確認スタッフの注意表示に使う）
  if (state.calStaffId) state.viewedStaffIds.add(state.calStaffId);

  const staffSelectHtml = assignedStaffIds.length
    ? `<select id="wtiCalStaff" class="form-select mb-2">${assignedStaffIds.map((sid) => {
        const s = state.staffs.find((x) => x.id === sid);
        const cnt = state.items.filter((it) => it.staffId === sid).length;
        return `<option value="${sid}"${sid === state.calStaffId ? ' selected' : ''}>${esc(s ? s.name : '不明#' + sid)}（${cnt}件）</option>`;
      }).join('')}</select>`
    : `<div class="info-box mb-2"><i class="bi bi-info-circle"></i> 割り当て済みのスタッフがいません。下の「未割り当て」から振り分けてください。</div>`;

  const summary = _wtiSummaryInfo(state);

  // I-1: unparsed はカレンダーより上（sourceWarn直後）に格上げして配置。
  // C-1: 同一(スタッフ,日付)の競合警告もカレンダーより上で必ず目に入るようにする。
  safeSetHTML(body, `
    ${sourceWarn}
    ${_wtiUnparsedHtml(state)}
    ${_wtiDuplicateWarnHtml(state)}
    ${staffSelectHtml}
    ${assignedStaffIds.length ? `<div class="cal-toolbar">
      <button class="cal-nav-btn" id="wtiCalPrev" type="button"><i class="bi bi-chevron-left"></i></button>
      <div class="cal-title num" id="wtiCalTitle"></div>
      <button class="cal-nav-btn" id="wtiCalNext" type="button"><i class="bi bi-chevron-right"></i></button>
    </div>
    <div class="cal-weekdays"><div class="sun">日</div><div>月</div><div>火</div><div>水</div><div>木</div><div>金</div><div class="sat">土</div></div>
    <div id="wtiCalGrid" class="wish-cal wti-cal"></div>` : ''}
    <div id="wtiPeriodWarn">${_wtiPeriodWarnHtml(state)}</div>
    <div id="wtiUnassigned"></div>
    ${summary.chipsHtml ? `<div class="wti-summary-chips mb-2">${summary.chipsHtml}</div>` : ''}
    <div class="flex gap-2 mt-1">
      <button class="btn btn-light" id="wtiBackBtn" type="button"><i class="bi bi-arrow-left"></i> やり直す</button>
      <button class="btn btn-primary flex-grow" id="wtiSubmitBtn" type="button"><i class="bi bi-check2-circle"></i> 合計 ${summary.total}件を登録する</button>
    </div>
    <div id="wtiSubmitMsg" class="mt-2">${state.submitMsg || ''}</div>`);

  wrap.querySelector('#wtiCalStaff')?.addEventListener('change', (e) => {
    state.calStaffId = +e.target.value;
    const [iy, im] = state.yearMonth.split('-').map(Number);
    state.calMonth = { y: iy, m: im - 1 };
    _wtiRenderStep2(wrap, state);
  });
  wrap.querySelector('#wtiCalPrev')?.addEventListener('click', () => {
    state.calMonth.m--; if (state.calMonth.m < 0) { state.calMonth.m = 11; state.calMonth.y--; }
    _wtiRenderStep2(wrap, state);
  });
  wrap.querySelector('#wtiCalNext')?.addEventListener('click', () => {
    state.calMonth.m++; if (state.calMonth.m > 11) { state.calMonth.m = 0; state.calMonth.y++; }
    _wtiRenderStep2(wrap, state);
  });
  wrap.querySelector('#wtiBackBtn')?.addEventListener('click', () => _wtiRenderStep1(wrap, state));
  wrap.querySelector('#wtiSubmitBtn')?.addEventListener('click', () => _wtiSubmit(wrap, state));

  _wtiRenderCalendar(wrap, state);
  _wtiRenderUnassigned(wrap, state);
}

/* カレンダー本体の描画。SCREENS.request の drawWish() と同じ .wish-cal/.wish-cell/.wmark
   を使い、見た目・操作感を揃える。既存希望がある日には印（アイコン）を付ける。
   C-1: 同一(スタッフ,日付)に複数項目がありうる（src/ai.py のプロンプトが
   「内容が違えばentriesを分ける」よう指示しているため、正常系として起こる）。
   後勝ちで1件だけ描いて矛盾を隠すと、店長が見た画面と送信内容がずれる
   （プレビューは店長が誤りを捕まえる最後の関門）ため、日付ごとに配列で持つ。 */
function _wtiRenderCalendar(wrap, state) {
  const titleEl = wrap.querySelector('#wtiCalTitle');
  const gridEl = wrap.querySelector('#wtiCalGrid');
  if (!gridEl) return;
  if (!state.calStaffId || !state.calMonth) { gridEl.innerHTML = ''; if (titleEl) titleEl.textContent = ''; return; }
  const { y, m } = state.calMonth;
  if (titleEl) titleEl.textContent = `${y}年 ${m + 1}月`;
  const first = new Date(y, m, 1); const startWd = first.getDay();
  const dim = new Date(y, m + 1, 0).getDate();
  const label = { any: '終日', morning: '早', evening: '遅', rest: '休' };
  // Minor: it.availability は _wtiFlatten 経由でサーバ（AI）由来の値を素通しして
  // いるため、enum を矯正しているのは現状サーバ側の1箇所のみ。ここでも既知の値
  // 以外はクラス名として使わない（属性注入経路を1つに依存させない）。
  const knownAvail = { any: 1, morning: 1, evening: 1, rest: 1, time: 1 };
  const byDate = {};
  state.items.filter((it) => it.staffId === state.calStaffId).forEach((it) => {
    (byDate[it.date] = byDate[it.date] || []).push(it);
  });
  let cells = '';
  for (let i = 0; i < startWd; i++) cells += '<div class="wish-cell empty"></div>';
  for (let d = 1; d <= dim; d++) {
    const ds = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    const dayItems = byDate[ds] || [];
    const wd = new Date(ds + 'T00:00:00').getDay();
    const wdCls = wd === 0 ? 'sun' : (wd === 6 ? 'sat' : '');
    const hasExisting = _wtiHasExisting(state, state.calStaffId, ds);
    // I-3: raw が貼り付けテキストに実在しない（AIの要約・創作の疑い）警告。
    // 開かないと気づけないため、詳細モーダルだけでなくカレンダーにも印を付ける。
    const hasUnverified = dayItems.some((it) => it.rawVerified === false);
    let mark = '';
    if (dayItems.length === 1) {
      const it = dayItems[0];
      const availCls = knownAvail[it.availability] ? it.availability : 'time';
      const markText = it.availability === 'time' ? `${_wtiShortTime(it.start)}-${_wtiShortTime(it.end)}` : (label[it.availability] || it.availability);
      mark = `<div class="wmark ${availCls}">${esc(markText)}</div>`;
    } else if (dayItems.length > 1) {
      mark = `<div class="wmark wti-conflict">競合${dayItems.length}</div>`;
    }
    const flag = hasExisting ? '<i class="bi bi-exclamation-circle-fill wti-existing-flag" title="既存の希望があります"></i>' : '';
    const unverifiedFlag = hasUnverified
      ? '<i class="bi bi-patch-exclamation-fill wti-raw-unverified-flag" title="AIが要約・創作した可能性がある文があります"></i>' : '';
    // M-3: 項目のない日は押しても何も起きないので、期間外セルと同じ .disabled で見た目も抑える。
    // ただし既存希望の印（wti-existing-flag）がある日は disabled にしない
    // （disabled の opacity:.35 が、意図的に目立たせたい警告アイコンまで薄めてしまうため）。
    // Minor(3161): 項目が無く既存希望の印だけがある日はクリックしても no-op なので、
    // 目立たせたい（=薄くしない）まま cursor だけ default に戻す（別クラス）。
    let cellCls;
    if (dayItems.length) cellCls = 'wish-cell';
    else if (hasExisting) cellCls = 'wish-cell wti-existing-only';
    else cellCls = 'wish-cell disabled';
    cells += `<div class="${cellCls}" data-day="${ds}"><div class="wd ${wdCls}">${d}</div>${mark}${flag}${unverifiedFlag}</div>`;
  }
  gridEl.innerHTML = cells;
  gridEl.querySelectorAll('.wish-cell[data-day]').forEach((c) => {
    c?.addEventListener('click', () => {
      const has = state.items.some((it) => it.staffId === state.calStaffId && it.date === c.dataset.day);
      if (has) _wtiOpenDetail(wrap, state, c.dataset.day);
    });
  });
}

/* 日付クリックで開く詳細: 読み取り内容・【元の文】の対比表示(必須)・修正・削除。
   既存希望がある日は「上書きする」チェックを出す（既定オフ＝スキップ側）。
   C-1: 同日に複数の読み取りが残っている場合は全件を列挙し、個別に編集・削除
   できるようにする（1件だけ見せて他方を隠すとプレビューと送信内容がずれる）。 */
function _wtiOpenDetail(wrap, state, date) {
  const idxs = [];
  state.items.forEach((it, i) => { if (it.staffId === state.calStaffId && it.date === date) idxs.push(i); });
  if (!idxs.length) return;
  const existingList = _wtiExistingFor(state, state.calStaffId, date);
  const hasExisting = existingList.length > 0;
  const availLabelMap = { rest: '休み希望', any: '終日OK', morning: '早番希望', evening: '遅番希望', time: '時間指定' };
  const overwriteChecked = idxs.some((i) => state.items[i].overwriteConfirmed);
  const entryBlocks = idxs.map((idx, n) => {
    const it = state.items[idx];
    const availLabel = availLabelMap[it.availability] || it.availability;
    // I-3: raw がAIの要約・言い換え・幻覚である可能性（raw_verified===false）を
    // 明示する。これが無いと店長は「捏造された元の文」と照合することになり、
    // カレンダープレビューという最後の関門が無効化される。
    const unverifiedWarn = it.rawVerified === false
      ? `<div class="alert alert-warning py-2 mb-2"><i class="bi bi-exclamation-triangle"></i> ⚠ この文は貼り付けたテキストに見つかりませんでした。AIが要約または創作した可能性があります。</div>`
      : '';
    return `<div class="wti-detail-entry" data-idx="${idx}">
      ${idxs.length > 1 ? `<div class="small text-secondary mb-1">読み取り ${n + 1}/${idxs.length}</div>` : ''}
      <div class="mb-2">${badge('読み取り: ' + availLabel, it.availability === 'rest' ? 'danger' : 'info')}</div>
      <div class="small text-secondary mb-1">元の文</div>
      <div class="wti-raw-quote mb-2">${esc(it.raw || '（元の文なし）')}</div>
      ${unverifiedWarn}
      <label class="form-label" for="wtiDetailAvail-${idx}">内容を修正</label>
      <select id="wtiDetailAvail-${idx}" class="form-select mb-2" data-role="avail">
        <option value="rest"${it.availability === 'rest' ? ' selected' : ''}>休み</option>
        <option value="any"${it.availability === 'any' ? ' selected' : ''}>いつでも可</option>
        <option value="morning"${it.availability === 'morning' ? ' selected' : ''}>早番</option>
        <option value="evening"${it.availability === 'evening' ? ' selected' : ''}>遅番</option>
        <option value="time"${it.availability === 'time' ? ' selected' : ''}>時間指定</option>
      </select>
      <div id="wtiDetailTimeRow-${idx}" class="row mb-2" style="${it.availability === 'time' ? '' : 'display:none'}">
        <div class="col-6"><input type="time" id="wtiDetailStart-${idx}" class="form-control" value="${esc(it.start || '')}"></div>
        <div class="col-6"><input type="time" id="wtiDetailEnd-${idx}" class="form-control" value="${esc(it.end || '')}"></div>
      </div>
      <button type="button" class="btn btn-outline-danger btn-sm" data-del-idx="${idx}"><i class="bi bi-trash"></i> この項目を削除</button>
    </div>`;
  }).join('');
  // I-4: 「既存の希望があります」としか出さないと、店長は何が消えるか分からない
  // まま上書きチェックを入れることになる。上書きすると wish_history の当該
  // (staff_id, date) の行は全件消える（複数行あっても全部）ため、中身を列挙する。
  const existingListHtml = hasExisting
    ? `<div class="small mt-1">現在の登録: ${existingList.map((w) => esc(_wtiExistingLabel(w))).join(' / ')}（${existingList.length}件）</div>` : '';
  const body = `
    <div class="mb-2"><strong class="num">${esc(date)}</strong>（${wdName(date)}）</div>
    <div id="wtiDetailErr"></div>
    ${entryBlocks}
    ${hasExisting ? `<div class="alert alert-warning py-2 mt-2 mb-1"><i class="bi bi-exclamation-triangle"></i> この日は既に希望が登録されています。${existingListHtml}<label class="flex items-center gap-2 mt-1" style="font-weight:400"><input type="checkbox" id="wtiDetailOverwrite"${overwriteChecked ? ' checked' : ''}> 既存を上書きして登録する（上記${existingList.length}件を削除して置き換えます）</label></div>` : ''}`;
  const dm = openModal(`<i class="bi bi-calendar-event"></i> ${esc(date)}の希望`, body, (w2, close) => {
    // I-6: time なのに時刻未入力の項目があれば、保存せず・閉じずにエラーを出す。
    // 同日に複数項目があるときは、どれが問題かを「読み取り n/件数」で示す（Minor指摘対応）
    for (let n = 0; n < idxs.length; n++) {
      const idx = idxs[n];
      const selEl = w2.querySelector(`#wtiDetailAvail-${idx}`);
      if (!selEl) continue;
      if (selEl.value === 'time') {
        const st = w2.querySelector(`#wtiDetailStart-${idx}`)?.value;
        const et = w2.querySelector(`#wtiDetailEnd-${idx}`)?.value;
        if (!st || !et) {
          const errBox = w2.querySelector('#wtiDetailErr');
          const posText = idxs.length > 1 ? `（読み取り ${n + 1}/${idxs.length}）` : '';
          if (errBox) errBox.innerHTML = `<div class="alert alert-danger py-2 mb-2">時間指定の場合は開始・終了の両方を入力してください${posText}。</div>`;
          return;
        }
      }
    }
    idxs.forEach((idx) => {
      const selEl = w2.querySelector(`#wtiDetailAvail-${idx}`);
      if (!selEl) return;
      const it = state.items[idx];
      it.availability = selEl.value;
      if (it.availability === 'time') {
        it.start = w2.querySelector(`#wtiDetailStart-${idx}`)?.value || null;
        it.end = w2.querySelector(`#wtiDetailEnd-${idx}`)?.value || null;
      } else {
        it.start = null; it.end = null;
      }
    });
    if (hasExisting) {
      const confirmed = !!w2.querySelector('#wtiDetailOverwrite')?.checked;
      idxs.forEach((idx) => { if (state.items[idx]) state.items[idx].overwriteConfirmed = confirmed; });
    }
    close();
    _wtiRenderStep2(wrap, state);
  }, { saveLabel: '保存' });
  idxs.forEach((idx) => {
    dm.querySelector(`#wtiDetailAvail-${idx}`)?.addEventListener('change', (e) => {
      const row = dm.querySelector(`#wtiDetailTimeRow-${idx}`);
      if (row) row.style.display = e.target.value === 'time' ? 'flex' : 'none';
    });
  });
  // 削除は即座に確定（複数件を一括で消す操作は想定しないため、1件消したら
  // モーダルを閉じて再描画する。他の項目を消したい場合は開き直せばよい）
  dm.querySelectorAll('[data-del-idx]').forEach((btn) => btn?.addEventListener('click', () => {
    const delIdx = +btn.dataset.delIdx;
    state.items.splice(delIdx, 1);
    dm.remove();
    _wtiRenderStep2(wrap, state);
  }));
}

/* 未割り当て一覧: staff_id が null のエントリを、元の1文（entryIdx）単位でまとめて
   表示する。スタッフを選ぶまでは登録対象に入らない（推測で割り当てない）。 */
function _wtiRenderUnassigned(wrap, state) {
  const box = wrap.querySelector('#wtiUnassigned');
  if (!box) return;
  const groups = {};
  state.items.forEach((it) => { if (!it.staffId) (groups[it.entryIdx] = groups[it.entryIdx] || []).push(it); });
  const entryIdxs = Object.keys(groups);
  if (!entryIdxs.length) { box.innerHTML = ''; return; }
  const label = { rest: '休み', any: 'いつでも可', morning: '早番', evening: '遅番', time: '時間指定' };
  const staffOpts = state.staffs.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
  const rows = entryIdxs.map((eidx) => {
    const grp = groups[eidx];
    const raw = grp[0].raw;
    const datesTxt = grp.map((g) => g.date.slice(5)).join('、');
    const availTxt = label[grp[0].availability] || grp[0].availability;
    const hintTxt = grp[0].staffHint ? `（候補: ${esc(grp[0].staffHint)}）` : '';
    return `<div class="wti-unassigned-row">
      <div class="wti-unassigned-info">
        <div class="small text-secondary">${esc(datesTxt)} ・ ${esc(availTxt)}${hintTxt}</div>
        <div class="wti-raw-quote">${esc(raw)}</div>
      </div>
      <select class="form-select wti-unassigned-select" data-entry="${eidx}">
        <option value="">誰の希望か選ぶ</option>${staffOpts}
      </select>
    </div>`;
  }).join('');
  box.innerHTML = `<div class="alert alert-warning py-2 mb-2"><i class="bi bi-person-exclamation"></i> 未割り当て ${entryIdxs.length}件（スタッフを選ぶまで登録されません）</div>${rows}`;
  box.querySelectorAll('[data-entry]').forEach((sel) => sel?.addEventListener('change', async () => {
    const eidx = sel.dataset.entry;
    const sid = +sel.value || null;
    if (!sid) return;
    state.items.forEach((it) => { if (String(it.entryIdx) === eidx && !it.staffId) it.staffId = sid; });
    state.calStaffId = sid;
    buzz(10);
    // I-7: 未割り当てから新たにスタッフへ振り分けたときは、そのスタッフ分の既存希望が
    // まだ未取得（初回parse時点では staffId が null で対象外だった）なので取得する。
    await _wtiEnsureExistingLoaded(state, [sid]);
    _wtiRenderStep2(wrap, state);
  }));
}

/* 登録: staff_id が付いた項目のみ対象。overwrite は API 全体に一括で効くフラグの
   ため、明示的に「上書きする」を選んだ日だけを分けて別リクエストで送る
   （選んでいない日を巻き込んで消してしまわないようにするため）。
   I-3: 2回に分けて送る以上、片方だけ失敗する部分失敗が起こりうる。その場合
   「成功した分は何件か」を必ず示し、成功済み項目は state.items から取り除いて
   二重送信を防ぐ（uid で照合。配列の index は再描画のたびにずれるため使わない）。 */
async function _wtiSubmit(wrap, state) {
  const msgBox = wrap.querySelector('#wtiSubmitMsg');
  const assignable = state.items.filter((it) => it.staffId);
  if (!assignable.length) { toast('登録できる希望がありません（未割り当てのみです）', 'error'); return; }
  // Important（再レビュー指摘）: 同一(スタッフ,日付)の競合が残ったまま overwrite を
  // チェックすると、1つの overwriteConfirmed が両エントリに共有されて overwriteGroup に
  // まとめて入る。サーバの DELETE は希望1件ごとのループの中で走るため、
  // entry1をINSERT→entry2のDELETEがentry1を消す→entry2をINSERTとなり、
  // 最終的に1行しか残らないのに created は2を返す（店長には「2件登録」と見える）。
  // 競合が残っている限り、どちらのグループにも入れず送信をブロックして
  // 店長に解消（片方を削除）させる方が、送信内容とプレビューの一致を保証できる。
  const dupGroups = _wtiFindDuplicateGroups(state);
  if (Object.keys(dupGroups).length) {
    // Minor(3323): 「どちらか一方に」は2件競合の言い回し。AIは同日3件以上の
    // 競合も返しうる（実際に受け入れテストで確認済み）ため、件数に依らず正しい
    // 表現にする。
    toast('同じ日に複数の読み取りが残っています。日付を開いて、1件だけ残してから登録してください。', 'error');
    return;
  }
  // I-6: time なのに時刻未入力のまま送信されるのを送信直前でも弾く（詳細モーダルの
  // バリデーションをすり抜けるケースは無いはずだが、最終防衛線として置く）
  const badTime = assignable.find((it) => it.availability === 'time' && (!it.start || !it.end));
  if (badTime) {
    toast(`時刻が未入力の項目があります（${badTime.date.slice(5)}）。日付を開いて時刻を入力してください。`, 'error');
    return;
  }
  const toWish = (it) => ({ staff_id: it.staffId, date: it.date, availability: it.availability, start: it.start, end: it.end, raw: it.raw });
  const overwriteGroup = assignable.filter((it) => _wtiHasExisting(state, it.staffId, it.date) && it.overwriteConfirmed);
  const normalGroup = assignable.filter((it) => !(_wtiHasExisting(state, it.staffId, it.date) && it.overwriteConfirmed));
  setLoading(true);
  state.submitMsg = '<div class="text-secondary small">登録中...</div>';
  if (msgBox) msgBox.innerHTML = state.submitMsg;
  let created = 0, skipped = 0;
  // ★最優先修正: skipped_detail（サーバ未対応なら null 扱い）の内訳を合算する。
  // 片方のリクエストだけ detail を返さない（サーバ未対応の過渡期）ケースに備え、
  // skipped>0 なのに detail が無いリクエストが1つでもあれば全体を「内訳不明」
  // 扱いにし、理由を断定した表示をしない（skippedDetailKnown）。
  const skippedDetail = { duplicate: 0, invalid: 0, rollback: 0 };
  let skippedDetailKnown = true;
  const succeededItems = [];
  const errors = [];
  const mergeResult = (r) => {
    created += r.created || 0;
    const sk = r.skipped || 0;
    skipped += sk;
    if (r.skipped_detail) {
      skippedDetail.duplicate += r.skipped_detail.duplicate || 0;
      skippedDetail.invalid += r.skipped_detail.invalid || 0;
      skippedDetail.rollback += r.skipped_detail.rollback || 0;
    } else if (sk > 0) {
      skippedDetailKnown = false;
    }
  };
  if (normalGroup.length) {
    try {
      const r1 = await api('/shop/wishes/bulk', { method: 'POST', body: JSON.stringify({ wishes: normalGroup.map(toWish), overwrite: false }) });
      mergeResult(r1);
      succeededItems.push(...normalGroup);
    } catch (e) { errors.push(`新規分: ${e.message}`); }
  }
  if (overwriteGroup.length) {
    try {
      const r2 = await api('/shop/wishes/bulk', { method: 'POST', body: JSON.stringify({ wishes: overwriteGroup.map(toWish), overwrite: true }) });
      mergeResult(r2);
      succeededItems.push(...overwriteGroup);
    } catch (e) { errors.push(`上書き分: ${e.message}`); }
  }
  setLoading(false);
  const skSummary = _wtiSkippedSummary(skipped, skippedDetailKnown ? skippedDetail : null);
  // N-2（レビュー指摘）: 「HTTPリクエストが成功した（例外を投げなかった）」ことと
  // 「実際に created された」ことは別。succeededItems は前者の単位（グループ全体）
  // でしか分からず、サーバは個々の項目のうちどれが created/skipped/rollback
  // だったかを返さない。以前はここで無条件に state.items から取り除いていたため、
  // created===0（全件スキップ）でもカレンダーの元になる項目が消え、セルをクリック
  // しても無反応になり、再送しようとすると「登録できる希望がありません」という
  // 画面と矛盾するトーストが出た。rollback>0（一部だけ書き込み失敗）でも同様に、
  // 失敗した項目を再送する手段が失われていた。
  // → 以下の各分岐は、state.items から取り除いてよい（＝グループがまるごと
  //   成功したと言える）場合を created>0 かつ rollback が無い場合に限定する
  //   （下の「全件成功」分岐のみが該当。created===0 / rollback>0 の分岐は
  //   state.items に一切触れず、店長がカレンダーを操作し続けられる・
  //   再送できる状態を保つ）。

  if (errors.length) {
    // 部分失敗: 正常系では normalGroup/overwriteGroup の一方しか失敗しえないため、
    // 成功した側のグループは「HTTP成功＝概ね意図通り送れた」とみなして取り除く
    // （失敗した側だけを再送できるようにする。既存のテストが前提にしている挙動）。
    const succeededUids = new Set(succeededItems.map((it) => it.uid));
    state.items = state.items.filter((it) => !succeededUids.has(it.uid));
    state.succeededDates.push(...succeededItems.map((it) => it.date));
    // Minor（レビュー指摘）: successPart にサーバの message をそのまま使うと、
    // その message 自体がスキップ理由（例:「1件は既存の希望と重複のためスキップ」）
    // を含んでいる場合があり、skippedPart（skipped_detailベースの自前の内訳）と
    // 同じ内容が2回・別の言い回しで出てしまう。created件数だけを自前で述べ、
    // スキップの理由説明は skippedPart に一本化する。
    const successPart = created > 0 ? `${created}件を登録しました。` : '';
    const skippedPart = skSummary.phrase ? `${skSummary.phrase}。` : '';
    // fix round 3: #wtiSubmitMsg への直書きは、この2行下の _wtiRenderStep2 が
    // #wtiSubmitMsg ごと再描画するため直後に消えてしまう（e2e が実機で検出）。
    // 再描画自体は succeededItems 除去後の state を画面（カレンダー・合計件数）に
    // 反映するために必須なので削らず、メッセージを state 側に持たせて
    // _wtiRenderStep2 に描かせることで、再描画後も残るようにする。
    state.submitMsg = `<div class="alert alert-danger py-2">${esc(successPart)}${esc(skippedPart)}失敗: ${esc(errors.join(' / '))}</div>`;
    toast(created > 0 ? `一部登録できませんでした（${created}件は登録済み）` : '登録に失敗しました', 'error');
    _wtiRenderStep2(wrap, state);
    return;
  }

  if (created === 0) {
    // M-2: 全件スキップは成功ではない。緑トーストにせず、モーダルも閉じずに内容を
    // 見直せるようにする。★最優先修正: 以前は skipped を無条件に「重複のため」と
    // 決め打ちしていたが、他店舗/退職スタッフ・enum外のavailability・
    // wish_history書き込み失敗によるrollback（データが失われた可能性）も同じ
    // skipped に含まれるため、実際には書き込みエラーでも「重複のため」と表示
    // されてしまっていた。skipped_detail に基づく正確な内訳に直す。
    // N-2: shouldDiscardSucceeded は false（created===0）なので state.items には
    // 触れていない。画面（カレンダー・合計件数）も変わっていないので再描画不要。
    const msg = skSummary.phrase ? `登録できる項目がありませんでした。${skSummary.phrase}` : '登録できる項目がありませんでした';
    // rollback（書き込み失敗による取り消し＝データ消失の可能性）は通常のスキップより
    // 一段強い警告色（alert-danger）で目立たせる。
    state.submitMsg = `<div class="alert ${skSummary.hasRollback ? 'alert-danger' : 'alert-warning'} py-2 mb-0">${esc(msg)}</div>`;
    if (msgBox) msgBox.innerHTML = state.submitMsg;
    toast(msg, skSummary.hasRollback ? 'error' : 'warning');
    return;
  }

  if (skSummary.hasRollback) {
    // N-2: created>0 でも、rollback（書き込み失敗による取り消し）した項目が
    // どれかはサーバから分からない。グループ全体を state.items から取り除くと、
    // 実際には失敗した分を二度と再送できなくなる。最低限の安全策として、ここでも
    // state.items には触れない（黙って閉じないだけでなく、再送できる状態を保つ。
    // 再送すれば、既にcreated済みの分はサーバ側の重複判定で無害にスキップされる）。
    // 一覧側の背景更新は今回createdできたと分かっている分の日付だけを使う
    // （state.succeededDates には積まない＝「確定した」ことにはしない）。
    const optimisticDates = succeededItems.map((it) => it.date).sort();
    const optimisticRange = optimisticDates.length
      ? { start: optimisticDates[0], end: optimisticDates[optimisticDates.length - 1] } : null;
    if (typeof state.onImported === 'function') state.onImported(optimisticRange);
    state.submitMsg = `<div class="alert alert-danger py-2"><i class="bi bi-exclamation-octagon"></i> ${esc(`${created}件を登録しました。${skSummary.phrase}`)}</div>`;
    if (msgBox) msgBox.innerHTML = state.submitMsg;
    toast(`${created}件を登録しましたが、一部は書き込みに失敗して取り消されました`, 'error');
    return;
  }

  // 全件成功（スキップがあっても重複・不正な入力のみ）。created>0 かつ
  // rollback 無しと確認できたのはこの分岐だけなので、ここでのみ
  // succeededItems を state.items から取り除く。
  const succeededUids = new Set(succeededItems.map((it) => it.uid));
  state.items = state.items.filter((it) => !succeededUids.has(it.uid));
  // Minor(3399): 部分失敗→再送を繰り返しても、過去に成功した分の日付を失わないよう
  // state 側に累積する（onImported へ渡す一覧フィルタ拡張レンジの計算に使う）。
  state.succeededDates.push(...succeededItems.map((it) => it.date));
  // M-9（再レビュー指摘）: I-5で月またぎ取り込みが可能になったため、フィルタ拡張は
  // state.yearMonth（対象月）ではなく、実際に登録できた項目の日付min〜maxを渡す
  // （8/31〜9/2のように対象月をまたぐ場合、対象月だけでは9月分の日付が一覧に出ない）。
  const submittedDates = state.succeededDates.slice().sort();
  const range = submittedDates.length
    ? { start: submittedDates[0], end: submittedDates[submittedDates.length - 1] }
    : null;
  if (typeof state.onImported === 'function') state.onImported(range);
  wrap.remove();
  toast(`${created}件を登録しました${skSummary.phrase ? `。${skSummary.phrase}` : ''}`, 'success');
}

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
      data: { labels: costData.map((c) => c.date.slice(5)), datasets: [{ label: '人件費', data: costData.map((c) => c.cost), backgroundColor: cssVarAlpha('--info', .75), borderRadius: 4 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { color: cssVar('--ink-3'), callback: (v) => '¥' + (v / 1000) + 'K' }, grid: { color: cssVar('--rule') } }, x: { ticks: { color: cssVar('--ink-3'), maxTicksLimit: 10 }, grid: { display: false } } } }
    });

    // Staff distribution（配置帯と同じロール色で色分け）
    const staffData = (sum.staff || []).slice().sort((a, b) => b.projected_hours - a.projected_hours).slice(0, 8);
    document.getElementById('anaRight').innerHTML = card(sectionTitle('bi-bar-chart', 'スタッフ別労働時間') +
      `<div class="chart-box"><canvas id="anaStaff"></canvas></div>`);
    chartInstances.anaStaff = new Chart(document.getElementById('anaStaff'), {
      type: 'bar',
      data: { labels: staffData.map((s) => s.name), datasets: [{ label: '時間', data: staffData.map((s) => s.projected_hours), backgroundColor: staffData.map((s) => cssVarAlpha('--' + roleClass(s.role), .75)), borderRadius: 4 }] },
      options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { color: cssVar('--ink-3') }, grid: { display: false } }, x: { ticks: { color: cssVar('--ink-3') }, grid: { color: cssVar('--rule') } } } }
    });

    // AI advice
    let advice = '分析中...';
    try { const rev = await api('/shop/ai/review', { method: 'POST', body: JSON.stringify({ start, end }) }); advice = rev.advice; } catch {}
    document.getElementById('anaRight').insertAdjacentHTML('beforeend', card(sectionTitle('bi-stars', 'AI改善提案', badge('AI', 'ai')) + `<div style="font-size:.88rem;line-height:1.7;white-space:pre-wrap">${esc(advice)}</div>`));
  } catch (e) { el.innerHTML += card(`<div class="text-danger">${esc(e.message)}</div>`); }
};

/* ---------- Notifications ---------- */
SCREENS.notifications = async function (el) {
  el.innerHTML = pageHead('通知', 'bi-bell') + card(`<div id="notifList"><div class="text-muted small">読み込み中...</div></div><button class="btn btn-light w-full mt-3 d-none" id="readAll">すべて既読にする</button>`);
  const tok = navToken();
  const loadNotifs = async () => {
    try {
      const d = await api('/shop/notifications');
      if (!isAlive(tok) || !el.isConnected) return;
      const list = document.getElementById('notifList');
      if (!list) return;
      list.innerHTML = d.notifications.length ? d.notifications.map((n) => `
        <div class="notif-item ${n.is_read ? '' : 'unread'}"><div class="nt-title">${esc(n.title)}</div><div class="nt-body">${esc(n.body || '')}</div><div class="nt-time">${esc((n.created_at || '').replace('T', ' ').slice(0, 16))}</div></div>`).join('')
        : emptyState('bi-bell', '通知はありません');
      const readBtn = document.getElementById('readAll');
      if (readBtn) {
        if (d.unread > 0) { readBtn.classList.remove('d-none'); } else { readBtn.classList.add('d-none'); }
      }
    } catch (e) {
      if (!isAlive(tok) || !el.isConnected) return;
      const list = document.getElementById('notifList');
      if (list) list.innerHTML = `<div class="text-danger">${esc(e.message)}</div>`;
    }
  };
  await loadNotifs();
  document.getElementById('readAll')?.addEventListener('click', async () => {
    await api('/shop/notifications/read-all', { method: 'PUT' });
    toast('既読にしました', 'success');
    if (!isAlive(tok) || !el.isConnected) return;
    await loadNotifs(); refreshNotifBadge();
  });
};

/* ---------- Settings ---------- */
let settingsTab = 'shift';
SCREENS.settings = function (el) {
  el.innerHTML = pageHead('設定', 'bi-gear') +
    `<div class="tabs no-print">
      <button class="tab ${settingsTab==='shift'?'active':''}" data-tab="shift">シフト設定</button>
      <button class="tab ${settingsTab==='shifthours'?'active':''}" data-tab="shifthours">シフト時間設定</button>
      <button class="tab ${settingsTab==='shop'?'active':''}" data-tab="shop">店舗情報</button>
      <button class="tab ${settingsTab==='periods'?'active':''}" data-tab="periods">募集期間</button>
      <button class="tab ${settingsTab==='password'?'active':''}" data-tab="password">パスワード</button>
    </div><div id="settingsBody"></div>`;
  el.querySelectorAll('.tab').forEach((t) => t?.addEventListener('click', () => { settingsTab = t.dataset.tab; el.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === t)); renderSettingsTab(el.querySelector('#settingsBody')); }));
  renderSettingsTab(el.querySelector('#settingsBody'));
};
function renderSettingsTab(body) {
  ({ shift: renderShiftMatrixTab, shifthours: renderShiftHoursTab, shop: renderShopTab, periods: renderPeriodsTab, password: renderPasswordTab }[settingsTab])(body);
}

/* --- シフト時間設定（シフト作成可能時間・曜日別/一括） --- */
const SHIFT_HOUR_DAYS = [
  { key: '1', label: '月曜日', short: '月' },
  { key: '2', label: '火曜日', short: '火' },
  { key: '3', label: '水曜日', short: '水' },
  { key: '4', label: '木曜日', short: '木' },
  { key: '5', label: '金曜日', short: '金' },
  { key: '6', label: '土曜日', short: '土' },
  { key: '0', label: '日曜日', short: '日' },
  { key: 'holiday', label: '祝日', short: '祝' },
];
const DEFAULT_SHIFT_HOURS = {
  bulk_mode: true,
  bulk: { start_time: '09:00', end_time: '22:00', is_closed: false },
  days: {
    '0': { start_time: '09:00', end_time: '22:00', is_closed: false },
    '1': { start_time: '09:00', end_time: '22:00', is_closed: false },
    '2': { start_time: '09:00', end_time: '22:00', is_closed: false },
    '3': { start_time: '09:00', end_time: '22:00', is_closed: false },
    '4': { start_time: '09:00', end_time: '22:00', is_closed: false },
    '5': { start_time: '09:00', end_time: '22:00', is_closed: false },
    '6': { start_time: '09:00', end_time: '22:00', is_closed: false },
    'holiday': { start_time: '09:00', end_time: '22:00', is_closed: false },
  },
};

function renderShiftHoursTab(body) {
  body.innerHTML = card(sectionTitle('bi-clock-history', 'シフト時間設定',
    `<span class="small text-secondary">— シフト作成可能な時間帯を曜日別または一括で設定</span>`) +
    `<div id="shiftHoursWrap"><div class="text-secondary small">読み込み中...</div></div>`);
  loadShiftHours(body);
}

async function loadShiftHours(body) {
  const wrap = body.querySelector('#shiftHoursWrap');
  if (!wrap) return;  // タブ切替で要素が既に無い場合は何もしない（null防范）
  let data;
  try {
    data = await api('/shop/shift-hours');
  } catch (e) {
    wrap.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
    return;
  }
  // デフォルトとマージして補完
  const merged = {
    bulk_mode: data.bulk_mode !== undefined ? !!data.bulk_mode : true,
    bulk: { ...(DEFAULT_SHIFT_HOURS.bulk), ...(data.bulk || {}) },
    days: { ...(DEFAULT_SHIFT_HOURS.days), ...(data.days || {}) },
  };
  SHIFT_HOUR_DAYS.forEach((d) => {
    merged.days[d.key] = { ...(DEFAULT_SHIFT_HOURS.days[d.key]), ...(merged.days[d.key] || {}) };
  });

  wrap.innerHTML = `
    <p class="small text-secondary mb-3">
      <i class="bi bi-info-circle"></i>
      この時間帯は「シフト作成が可能な時間帯」を表します。定休日にチェックを入れた日はシフトが作成されません。
    </p>
    <div class="form-check form-switch mb-3">
      <input class="form-check-input" type="checkbox" id="shBulkMode" ${merged.bulk_mode ? 'checked' : ''}>
      <label class="form-check-label" for="shBulkMode"><strong>一括設定</strong> <span class="small text-secondary">（全曜日・祝日共通の時間帯を指定）</span></label>
    </div>
    <div id="shBulkWrap" style="display:${merged.bulk_mode ? 'block' : 'none'}">
      ${renderShiftHourRow('一括（全曜日・祝日）', 'bulk', merged.bulk, true)}
    </div>
    <div id="shDaysWrap" style="display:${merged.bulk_mode ? 'none' : 'block'}">
      <div class="section-title mb-2"><i class="bi bi-calendar3"></i> 曜日別設定</div>
      ${SHIFT_HOUR_DAYS.map((d) => renderShiftHourRow(d.label, 'day_' + d.key, merged.days[d.key], false)).join('')}
    </div>
    <hr style="border-color:var(--rule);margin:16px 0">
    <div class="section-title mb-2"><i class="bi bi-calendar-x"></i> 祝日・特別休業日</div>
    <p class="small text-secondary mb-2">上記「祝日」設定を適用する日付を登録します。<strong>日本の祝日</strong>は自動取り込み可能です（特別休業日は手動で追加してください）。</p>
    <div class="flex gap-2 mb-2 flex-wrap">
      <button class="btn btn-light" id="shImportJapanese" title="日本の祝日（今年〜3年分）を一括取り込み"><i class="bi bi-flag"></i> 日本の祝日を取り込む</button>
      <button class="btn btn-light" id="shPreviewJapanese" title="取り込まれる祝日を事前確認"><i class="bi bi-eye"></i> 祝日を確認</button>
    </div>
    <div id="shJapanesePreview" class="holiday-preview mb-2" style="display:none"></div>
    <div class="row mb-2">
      <div class="col-8"><input type="date" id="shHolidayDate" class="form-control"></div>
      <div class="col-4"><button class="btn btn-light w-100" id="shAddHoliday"><i class="bi bi-plus-lg"></i> 追加</button></div>
    </div>
    <div class="small text-secondary mb-1">上記「追加」ボタンは特別休業日（店の都合で休む日）の登録用です。日本の祝日は上の「日本の祝日を取り込む」をご利用ください。</div>
    <div id="shHolidayList"></div>
    <div class="flex gap-2 mt-3">
      <button class="btn btn-primary" id="shSave"><i class="bi bi-check-lg"></i> 保存</button>
      <label class="form-check ms-2 flex items-center" style="gap:6px">
        <input type="checkbox" id="shSyncPatterns" class="form-check-input" checked>
        <span class="small">シフトパターン（AI生成で使用）にも反映する <strong class="text-danger">（推奨）</strong></span>
      </label>
    </div>
    <div id="shMsg" class="mt-2 small"></div>`;

  // 一括設定トグル
  const bulkToggle = wrap.querySelector('#shBulkMode');
  bulkToggle?.addEventListener('change', () => {
    const bulkMode = bulkToggle.checked;
    wrap.querySelector('#shBulkWrap').style.display = bulkMode ? 'block' : 'none';
    wrap.querySelector('#shDaysWrap').style.display = bulkMode ? 'none' : 'block';
  });
  // 定休日チェックボックスの挙動（時間入力をグレーアウト）
  wrap.querySelectorAll('.sh-closed').forEach((cb) => {
    cb?.addEventListener('change', () => {
      const row = cb.closest('.sh-row');
      if (!row) return;
      const st = row.querySelector('.sh-start');
      const et = row.querySelector('.sh-end');
      if (cb.checked) {
        if (st) { st.disabled = true; st.classList.add('disabled-input'); }
        if (et) { et.disabled = true; et.classList.add('disabled-input'); }
      } else {
        if (st) { st.disabled = false; st.classList.remove('disabled-input'); }
        if (et) { et.disabled = false; et.classList.remove('disabled-input'); }
      }
    });
  });
  // 初期表示で closed 状態を反映
  wrap.querySelectorAll('.sh-closed').forEach((cb) => {
    if (cb.checked) cb.dispatchEvent(new Event('change'));
  });

  // 祝日リストのロード
  const loadHolidays = async () => {
    try {
      const hd = await api('/shop/holidays');
      const list = wrap.querySelector('#shHolidayList');
      if (!list) return;
      list.innerHTML = (hd.holidays || []).length ? `<div class="holiday-list">${hd.holidays.map((h) => `
        <div class="list-row holiday-row" data-date="${esc(h.holiday_date)}">
          <div><strong>${esc(h.holiday_date)}</strong> ${h.note ? `<span class="text-secondary small">${esc(h.note)}</span>` : ''}</div>
          <button class="btn btn-sm btn-outline-danger" data-del="${esc(h.holiday_date)}"><i class="bi bi-x"></i></button>
        </div>`).join('')}</div>` : '<div class="small text-secondary">祝日は登録されていません</div>';
      list.querySelectorAll('[data-del]').forEach((b) => b?.addEventListener('click', async () => {
        try {
          await api(`/shop/holidays/${encodeURIComponent(b.dataset.del)}`, { method: 'DELETE' });
          toast('祝日を削除しました', 'success');
          loadHolidays();
        } catch (e) { toast(e.message, 'error'); }
      }));
    } catch (e) {
      // 祝日APIが未対応の場合は無害
    }
  };
  loadHolidays();
  wrap.querySelector('#shAddHoliday')?.addEventListener('click', async () => {
    const input = wrap.querySelector('#shHolidayDate');
    if (!input || !input.value) { toast('日付を選択してください', 'error'); return; }
    try {
      await api('/shop/holidays', { method: 'POST', body: JSON.stringify({ holiday_date: input.value }) });
      toast('祝日を追加しました', 'success');
      input.value = '';
      loadHolidays();
    } catch (e) { toast(e.message, 'error'); }
  });

  // 日本の祝日プレビュー
  wrap.querySelector('#shPreviewJapanese')?.addEventListener('click', async () => {
    const previewBox = wrap.querySelector('#shJapanesePreview');
    if (!previewBox) return;
    previewBox.style.display = 'block';
    safeSetHTML(previewBox, '<div class="small text-secondary">計算中...</div>');
    try {
      const d = await api('/shop/holidays/japanese-preview');
      const list = (d.holidays || []);
      if (!list.length) {
        safeSetHTML(previewBox, '<div class="small text-secondary">該当年の祝日がありません</div>');
        return;
      }
      const grouped = {};
      list.forEach((h) => {
        const y = h.date.slice(0, 4);
        (grouped[y] = grouped[y] || []).push(h);
      });
      const html = Object.keys(grouped).map((y) => `
        <div class="holiday-preview-year">
          <div class="small fw-bold mt-1">${y}年（${grouped[y].length}日）</div>
          <div class="holiday-preview-chips">
            ${grouped[y].map((h) => `<span class="holiday-chip" title="${esc(h.name)}">${esc(h.date.slice(5))} ${esc(h.name)}</span>`).join('')}
          </div>
        </div>`).join('');
      safeSetHTML(previewBox, html);
    } catch (e) {
      safeSetHTML(previewBox, `<div class="text-danger small">${esc(e.message)}</div>`);
    }
  });

  // 日本の祝日一括取り込み
  wrap.querySelector('#shImportJapanese')?.addEventListener('click', async () => {
    if (!confirm('日本の祝日（今年〜翌々年）を一括取り込みしますか？\n既存の祝日と重複する日はスキップされます。')) return;
    try {
      const r = await api('/shop/holidays/import-japanese', { method: 'POST', body: JSON.stringify({}) });
      toast(`日本の祝日を ${r.imported} 件取り込みました（${r.skipped} 件スキップ）`, 'success');
      loadHolidays();
      // プレビュー表示があれば隠す
      const previewBox = wrap.querySelector('#shJapanesePreview');
      if (previewBox) previewBox.style.display = 'none';
    } catch (e) { toast(e.message, 'error'); }
  });

  // 保存
  wrap.querySelector('#shSave')?.addEventListener('click', async () => {
    const bulkMode = wrap.querySelector('#shBulkMode').checked;
    const bulk = readShiftHourRow(wrap, 'bulk');
    const days = {};
    SHIFT_HOUR_DAYS.forEach((d) => {
      days[d.key] = readShiftHourRow(wrap, 'day_' + d.key);
    });
    const syncPatterns = !!(wrap.querySelector('#shSyncPatterns')?.checked);
    const payload = { bulk_mode: bulkMode, bulk, days };
    try {
      // sync_patterns はトップレベルで送る（APIが body.get('sync_patterns') で探すため）
      const r = await api('/shop/shift-hours', { method: 'PUT', body: JSON.stringify({ shift_hours: payload, sync_patterns: syncPatterns }) });
      const msg = wrap.querySelector('#shMsg');
      let logHtml = '';
      if (r.sync_log && r.sync_log.length) {
        logHtml = '<div class="text-info"><i class="bi bi-info-circle"></i> ' + r.sync_log.map((s) => esc(s)).join('<br>') + '</div>';
      }
      if (msg) msg.innerHTML = '<span class="text-success"><i class="bi bi-check-circle"></i> 保存しました</span>' + logHtml;
      toast('シフト時間設定を保存しました' + (syncPatterns ? '（パターンにも反映）' : ''), 'success');
    } catch (e) {
      const msg = wrap.querySelector('#shMsg');
      if (msg) msg.innerHTML = `<span class="text-danger"><i class="bi bi-exclamation-triangle"></i> ${esc(e.message)}</span>`;
    }
  });
}

function renderShiftHourRow(label, key, data, isBulk) {
  const closed = !!data.is_closed;
  return `<div class="sh-row ${isBulk ? 'sh-row-bulk' : 'sh-row-day'}">
    <div class="sh-row-label"><strong>${esc(label)}</strong></div>
    <div class="sh-row-controls">
      <label class="form-check sh-closed-label">
        <input type="checkbox" class="sh-closed" data-key="${key}" ${closed ? 'checked' : ''}>
        <span class="small">定休日</span>
      </label>
      <div class="sh-time-inputs">
        <input type="time" class="form-control sh-start" data-key="${key}" value="${esc(data.start_time || '09:00')}" ${closed ? 'disabled' : ''}>
        <span class="sh-time-sep">〜</span>
        <input type="time" class="form-control sh-end" data-key="${key}" value="${esc(data.end_time || '22:00')}" ${closed ? 'disabled' : ''}>
      </div>
    </div>
  </div>`;
}

function readShiftHourRow(wrap, key) {
  const st = wrap.querySelector(`.sh-start[data-key="${key}"]`);
  const et = wrap.querySelector(`.sh-end[data-key="${key}"]`);
  const cb = wrap.querySelector(`.sh-closed[data-key="${key}"]`);
  return {
    start_time: st?.value || '09:00',
    end_time: et?.value || '22:00',
    is_closed: !!(cb?.checked),
  };
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
          <td><input type="number" class="matrix-input matrix-default" data-pid="${p.id}" value="${esc(p.required_staff)}" min="0" title="基本必要人数"></td>
          ${[0,1,2,3,4,5,6].map((w) => {
            const val = wr[String(w)];
            const has = val !== undefined && val !== null;
            return `<td><input type="number" class="matrix-input matrix-wd ${has?'has-override':''}" data-pid="${p.id}" data-wd="${w}" value="${esc(has?val:'')}" placeholder="${esc(p.required_staff)}" min="0"></td>`;
          }).join('')}
          <td><div class="matrix-row-actions">
            <button data-edit="${p.id}" data-n="${esc(p.pattern_name)}" data-st="${esc(p.start_time)}" data-et="${esc(p.end_time)}" data-req="${esc(p.required_staff)}" title="編集"><i class="bi bi-pencil"></i></button>
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
    `<label class="form-label" for="pName">時間帯名</label><input id="pName" class="form-control mb-2" value="${esc(data?.n || '')}" placeholder="例: 夜">
     <div class="row"><div class="col-6"><label class="form-label" for="pSt">開始</label><input id="pSt" class="form-control" value="${esc(data?.st || '17:00')}"></div>
     <div class="col-6"><label class="form-label" for="pEt">終了</label><input id="pEt" class="form-control" value="${esc(data?.et || '22:00')}"></div></div>
     <label class="form-label mt-2">基本必要人数</label><input id="pReq" type="number" class="form-control" value="${esc(data?.req || 2)}">
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
    // 数値項目は shops.settings 由来。サーバ側の型検証は新規保存にしか効かず、
    // 代理閲覧中はこの画面が別テナントのデータを管理者のブラウザで描画し得るため、
    // value 属性に入れる前に必ず esc() する（保存型XSS対策の多層防御。
    // public/admin.js の renderShopSettingsTab の num() と同じパターン）。
    const num = (v, dflt) => esc(String(v ?? dflt));
    body.innerHTML = card(sectionTitle('bi-shop', '店舗情報') +
      `<label class="form-label" for="setShopName">店舗名</label><input id="setShopName" class="form-control mb-2" value="${esc(d.shop_name)}">
       <label class="form-label" for="setShopCode">店舗コード</label><input id="setShopCode" class="form-control mb-3" value="${esc(d.shop_code)}" disabled>
       <hr style="border-color:var(--rule);margin:16px 0">
       ${sectionTitle('bi-gear', '運用設定')}
       <div class="row">
         <div class="col-6"><label class="form-label" for="setWage">デフォルト時給(円)</label><input id="setWage" type="number" class="form-control" value="${num(s.default_hourly_wage, 1000)}"></div>
         <div class="col-6"><label class="form-label" for="setMinDaily">1日最低勤務(h)</label><input id="setMinDaily" type="number" class="form-control" value="${num(s.min_daily_hours, 4)}"></div>
         <div class="col-6"><label class="form-label" for="setMaxDaily">1日最大勤務(h)</label><input id="setMaxDaily" type="number" class="form-control" value="${num(s.max_daily_hours, 9)}"></div>
         <div class="col-6"><label class="form-label" for="setMaxConsec">最大連勤（推奨）</label><input id="setMaxConsec" type="number" class="form-control" value="${num(s.max_consecutive_days, 6)}"></div>
         <div class="col-6"><label class="form-label" for="setNightRate">深夜割増率</label><input id="setNightRate" type="number" step="0.05" class="form-control" value="${num(s.night_premium_rate, 1.25)}"></div>
         <div class="col-6"><label class="form-label" for="setTransport">1日交通費(円)</label><input id="setTransport" type="number" class="form-control" value="${num(s.transport_per_day, 0)}"></div>
         <div class="col-12"><label class="form-label">シフト時間設定</label><div class="info-box"><i class="bi bi-info-circle"></i> シフト作成可能な時間帯は <strong>「シフト時間設定」タブ</strong> で管理しています（曜日別・祝日対応）。</div></div>
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
            period_mode: body.querySelector('#setPeriodMode').value } }) });
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
      `<div class="row"><div class="col-6"><label class="form-label" for="peStart">開始</label><input type="date"  id="peStart" class="form-control" value="${esc(np.start_date)}"></div><div class="col-6"><label class="form-label" for="peEnd">終了</label><input type="date"  id="peEnd" class="form-control" value="${esc(np.end_date)}"></div></div>
       <label class="form-label mt-2">締切</label><input type="date" id="peDeadline" class="form-control" value="${esc(np.deadline)}">`,
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
     <div id="crTime"><label class="form-label" for="crStart">希望時間</label><div class="row mb-2"><div class="col-6"><input type="datetime-local" id="crStart" class="form-control" value="${esc(sl(s.start_datetime))}"></div><div class="col-6"><input type="datetime-local" id="crEnd" class="form-control" value="${esc(sl(s.end_datetime))}"></div></div></div>
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
      periodBanner = `<div class="kpi-card kpi-indigo mb-3"><div class="kpi-label"><i class="bi bi-megaphone"></i> シフト希望受付中</div><div class="kpi-value num" style="font-size:1.05rem">${esc(ap.start_date)} 〜 ${esc(ap.end_date)}</div><div class="kpi-sub">締切: ${esc(ap.deadline)}</div><button class="btn btn-primary btn-sm mt-2" id="goRequest"><i class="bi bi-pencil-square"></i> 希望を提出する</button></div>`;
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
  document.getElementById('miniChatInput')?.addEventListener('keydown', (e) => {
    // IME変換中のEnterは確定扱いとして送信しない
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) sendMini();
  });
  renderMini(); renderSug();
};

SCREENS.staffMyshift = function (el) {
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
    ? `<div class="kpi-card kpi-indigo" style="margin-bottom:12px"><div class="kpi-label">募集期間</div><div class="kpi-value num" style="font-size:1.1rem">${esc(wishPeriod.start_date)} 〜 ${esc(wishPeriod.end_date)}</div><div class="kpi-sub">締切: ${esc(wishPeriod.deadline)}</div></div>`
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
      <hr style="border-color:var(--rule);margin:16px 0">
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
      if (t === 'time') {
        const st = w.querySelector('#wpStart').value, en = w.querySelector('#wpEnd').value;
        // ★【翌日またぎ対応】end <= start の場合は翌日扱い。
        // 従来は同日保存して「9-2」の負の時間長になるインシデントがあった。
        const [sh, sm] = st.split(':').map(Number);
        const [eh, em] = en.split(':').map(Number);
        let endDay = day;
        if (eh < sh || (eh === sh && em <= sm)) {
          // 翌日
          const dd = new Date(day + 'T00:00:00'); dd.setDate(dd.getDate() + 1);
          endDay = `${dd.getFullYear()}-${String(dd.getMonth()+1).padStart(2,'0')}-${String(dd.getDate()).padStart(2,'0')}`;
        }
        wishState[day] = { type: 'time', start: `${day}T${st}:00`, end: `${endDay}T${en}:00` };
      }
      else wishState[day] = { type: t };
      buzz(10); w.remove(); drawWish();
    }));
  }
  document.getElementById('wPrev')?.addEventListener('click', () => { wishMonth.m--; if (wishMonth.m < 0) { wishMonth.m = 11; wishMonth.y--; } drawWish(); });
  document.getElementById('wNext')?.addEventListener('click', () => { wishMonth.m++; if (wishMonth.m > 11) { wishMonth.m = 0; wishMonth.y++; } drawWish(); });
  /* "HH:MM" 2つを比較して end <= start なら end を翌日日付で返す。
     戻り: { endDate: "YYYY-MM-DD" } */
  function _calcOvernightEndDay(day, startTime, endTime) {
    const [sh, sm] = (startTime || '').split(':').map(Number);
    const [eh, em] = (endTime || '').split(':').map(Number);
    if (eh < sh || (eh === sh && em <= sm)) {
      const dd = new Date(day + 'T00:00:00'); dd.setDate(dd.getDate() + 1);
      return `${dd.getFullYear()}-${String(dd.getMonth()+1).padStart(2,'0')}-${String(dd.getDate()).padStart(2,'0')}`;
    }
    return day;
  }
  function fillWishesFromAI(d) {
    const ng = new Set(d.ng_weekdays || []); const isTime = d.preferred_slot === 'time' && d.preferred_start && d.preferred_end;
    const pref = isTime ? 'time' : (d.preferred_slot === 'morning' ? 'morning' : d.preferred_slot === 'evening' ? 'evening' : 'any');
    const need = d.need_days || 0; const dim = new Date(wishMonth.y, wishMonth.m + 1, 0).getDate(); let filled = 0;
    const inPeriod = (ds) => wishPeriod && ds >= wishPeriod.start_date && ds <= wishPeriod.end_date;
    // HH:MM → HH:MM:00 に正規化（サーバーが %H:%M:%S パースのため）
    const padTime = (t) => /^\d{1,2}:\d{2}$/.test(t || '') ? t + ':00' : t;
    // ★【翌日またり】preferred_end <= preferred_start なら各dayごとに end を翌日扱い
    for (let day = 1; day <= dim; day++) {
      const ds = `${wishMonth.y}-${String(wishMonth.m+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
      const wd = new Date(ds + 'T00:00:00').getDay();
      if (!inPeriod(ds)) continue; // 募集期間外はスキップ
      if (ng.has(wd)) { wishState[ds] = { type: 'rest' }; continue; }
      if (need && filled >= need) { wishState[ds] = { type: 'rest' }; continue; }
      if (isTime) {
        const endDay = _calcOvernightEndDay(ds, d.preferred_start, d.preferred_end);
        wishState[ds] = { type: 'time', start: `${ds}T${padTime(d.preferred_start)}`, end: `${endDay}T${padTime(d.preferred_end)}` };
      } else {
        wishState[ds] = { type: pref };
      }
      filled++;
    }
    drawWish();
  }
  document.getElementById('aiParseBtn')?.addEventListener('click', async () => {
    const text = document.getElementById('aiText').value.trim(); if (!text) { toast('文章を入力'); return; }
    const box = document.getElementById('aiResult'); box.innerHTML = '<div class="text-secondary small">解析中...</div>'; setLoading(true);
    try {
      const d = await api('/staff/ai/parse', { method: 'POST', body: JSON.stringify({ text }) });
      const ng = (d.ng_weekdays || []).map((x) => WD[x]).join('・');
      // preferred_start / preferred_end / need_hours は LLM が本人の入力文から
      // 抽出した値で、形式が保証されない。描画前に必ず esc() を通す。
      const slotTxt = d.preferred_slot === 'time' ? `${esc(d.preferred_start)}-${esc(d.preferred_end)}` : (d.preferred_slot === 'morning' ? '朝' : d.preferred_slot === 'evening' ? '夜' : '指定なし');
      fillWishesFromAI(d);
      box.innerHTML = `<div class="ai-card p-3"><div class="flex gap-2 flex-wrap mb-2">${badge(d.source === 'llm' ? 'AI(API)' : 'ルールベース', d.source === 'llm' ? 'success' : 'warning')}
        ${d.target_income ? `<span class="stat-pill">目標 ${yen(d.target_income)}</span>` : ''}
        ${d.need_hours ? `<span class="stat-pill">必要 ${esc(d.need_hours)}h</span>` : ''}
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
