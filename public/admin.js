/* admin.js — システム管理者向け画面（SCREENS.admin*）のみを集めたファイル。

   public/app.js が5000行近くまで肥大化していたため、管理画面（システム管理者
   ロール専用）のコードだけをここに切り出した。

   このプロジェクトはモジュール化していない（bundler も ESM も使わず、
   index.html で <script src="app.js"> を1本読むだけの構成）ため、ここに書く
   関数・変数はすべて window スコープのグローバルになる。app.js 側で定義済みの
   グローバル関数（api / esc / openModal / toast / card / pageHead /
   sectionTitle / badge / emptyState / kpiCard / navToken / isAlive /
   safeSetHTML / navigateTo / roleLabel / roleClass 等）や、app.js で
   `const SCREENS = {}` として初期化済みの SCREENS オブジェクトを、
   そのまま参照・代入して使う。

   読み込み順序が重要: index.html では必ず
     <script src="app.js"></script>
     <script src="admin.js"></script>
   の順（admin.js は app.js の直後）に読み込むこと。逆順にすると SCREENS が
   未定義になりエラーになる。

   なお NAV_DEFS（app.js 側）は全ロール共通の定義であり、admin.js より先に
   評価される必要があるため、ここには移設していない。 */

// 要対応の種別 → 日本語ラベル。badge() の見た目はすべて 'warning'（要確認の意味）に統一。
const ATTENTION_LABELS = {
  deadline_passed: '締切超過・未確定',
  no_manager: '店舗管理者が不在',
  stale_login: '長期未ログイン',
  pending_migration: '未適用のマイグレーション',
};

SCREENS.adminHome = async function (el) {
  const tok = navToken();
  el.innerHTML = pageHead('ダッシュボード', 'bi-speedometer2') +
    '<div id="dashKpi" class="row mb-3"></div><div id="dashAttention"></div><div id="dashAudit"></div>';
  const d = await api('/admin/dashboard');
  if (!isAlive(tok)) return;
  const kpiEl = document.getElementById('dashKpi');
  if (!kpiEl) return;
  const k = d.kpi;
  kpiEl.innerHTML =
    `<div class="col-6 col-lg-3">${kpiCard('bi-shop', '稼働店舗', k.shops_active,
       `停止${k.shops_inactive} / アーカイブ${k.shops_archived}`, 'indigo')}</div>
     <div class="col-6 col-lg-3">${kpiCard('bi-people', '在籍スタッフ', k.staffs_total, '全店合計', 'indigo')}</div>
     <div class="col-6 col-lg-3">${kpiCard('bi-calendar-check', '今月の確定シフト', k.confirmed_this_month, '件', 'green')}</div>
     <div class="col-6 col-lg-3">${kpiCard('bi-exclamation-triangle', '要対応', k.attention_count,
       k.attention_count ? '確認してください' : '問題ありません',
       k.attention_count ? 'red' : 'green')}</div>`;

  document.getElementById('dashAttention').innerHTML =
    card(sectionTitle('bi-exclamation-triangle', '要対応') +
      (d.attention.length ? d.attention.map((a) => `
        <div class="list-row" ${a.shop_id ? `style="cursor:pointer" data-attshop="${a.shop_id}"` : ''}>
          <div>${badge(ATTENTION_LABELS[a.kind] || a.kind, 'warning')}
            <strong class="ms-2">${esc(a.shop_name || '—')}</strong>
            <div class="small text-secondary">${esc(a.detail)}</div></div>
          ${a.shop_id ? '<i class="bi bi-chevron-right text-secondary"></i>' : ''}
        </div>`).join('') : emptyState('bi-check-circle', '対応が必要な項目はありません')));
  document.querySelectorAll('[data-attshop]').forEach((b) => b?.addEventListener('click', () => {
    window._adminShopId = +b.dataset.attshop;
    navigateTo('adminShopDetail');
  }));

  document.getElementById('dashAudit').innerHTML =
    card(sectionTitle('bi-clock-history', '最近の操作',
      '<button class="btn btn-sm btn-light" id="toAuditBtn">監査ログへ</button>') +
      (d.recent_audit.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>日時</th><th>操作者</th><th>操作</th><th>詳細</th></tr></thead><tbody>` +
        d.recent_audit.map((l) => `<tr>
          <td class="small">${esc((l.created_at || '').replace('T', ' '))}</td>
          <td class="small">${esc(l.actor_name || l.actor_role || '—')}</td>
          <td>${badge(auditActionLabel(l.action), 'info')}</td>
          <td class="small">${esc(l.detail || '')}</td></tr>`).join('') +
        '</tbody></table></div>' : emptyState('bi-clock-history', '操作履歴がありません')));
  document.getElementById('toAuditBtn')?.addEventListener('click', () => navigateTo('adminAudit'));
};

// アーカイブ済み店舗は既定で一覧から隠す（運営が普段見るのは稼働中の店舗だけのため）。
// トグルで一時的に表示できる。画面遷移をまたいで状態を覚えておく用途なのでモジュール変数。
let adminShowArchived = false;
SCREENS.adminShops = async function (el) {
  el.innerHTML = pageHead('店舗一覧', 'bi-shop') +
    card(`<div class="flex justify-between items-center mb-3">${sectionTitle('bi-shop', '店舗一覧')}
      <div class="flex gap-2">
        <button class="btn btn-light btn-sm" id="toggleArchivedBtn"><i class="bi bi-archive"></i> ${adminShowArchived ? 'アーカイブ済みを隠す' : 'アーカイブ済みも表示'}</button>
        <button class="btn btn-primary btn-sm" id="addShopBtn"><i class="bi bi-plus-lg"></i></button>
      </div></div><div id="shopList"></div>`);
  const load = async () => {
    const d = await api('/admin/shops' + (adminShowArchived ? '?include_archived=1' : ''));
    document.getElementById('shopList').innerHTML = d.shops.length ? (await Promise.all(d.shops.map(async (s) => {
      let st = { staff_count: '-', confirmed_count: '-' };
      try { st = await api(`/admin/shops/stats/${s.id}`); } catch {}
      // アーカイブ済みは「有効/無効」ではなく「アーカイブ済み」バッジのみ出す。
      // 有効化/無効化トグルは（archive() が is_active=0 に固定するので）
      // アーカイブ中に押しても意味が無く、混乱の元になるため出さない。
      const statusBadge = s.is_archived ? badge('アーカイブ済み', 'warning') : badge(s.is_active ? '有効' : '無効', s.is_active ? 'success' : 'warning');
      const toggleBtn = s.is_archived ? '' : `<button class="btn btn-sm btn-light" data-toggle="${s.id}" data-active="${s.is_active}">${s.is_active ? '無効化' : '有効化'}</button>`;
      return `<div class="list-row" style="cursor:pointer" data-detail="${s.id}"><div><strong>${esc(s.shop_name)}</strong> <span class="text-secondary">${esc(s.shop_code)}</span> ${statusBadge}<div class="small text-secondary">スタッフ${st.staff_count}名 / 確定${st.confirmed_count}件</div></div>${toggleBtn}</div>`;
    }))).join('') : emptyState('bi-shop', '店舗がありません');
    document.getElementById('shopList').querySelectorAll('[data-detail]').forEach((b) => b?.addEventListener('click', (ev) => { if (ev.target.closest('[data-toggle]')) return; window._adminShopId = +b.dataset.detail; navigateTo('adminShopDetail'); }));
    document.getElementById('shopList').querySelectorAll('[data-toggle]').forEach((b) => b?.addEventListener('click', async (ev) => { ev.stopPropagation(); await api(`/admin/shops/${b.dataset.toggle}`, { method: 'PUT', body: JSON.stringify({ is_active: b.dataset.active !== '1' }) }); load(); }));
  };
  load();
  document.getElementById('toggleArchivedBtn')?.addEventListener('click', () => { adminShowArchived = !adminShowArchived; navigateTo('adminShops'); });
  document.getElementById('addShopBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-plus-lg"></i> 店舗追加',
      `<p class="small text-secondary mb-3">店舗情報と、ログイン用の店舗責任者アカウントを同時に作成します。店舗責任者は作成直後から <strong>店舗コード + ユーザーID + パスワード</strong> でログインできます。</p>
       <div class="row"><div class="col-6"><label class="form-label" for="shCode">店舗コード <span class="text-danger">*</span></label><input id="shCode" class="form-control mb-2" placeholder="例: SHOP001"></div><div class="col-6"><label class="form-label" for="shName">店舗名 <span class="text-danger">*</span></label><input id="shName" class="form-control mb-2" placeholder="例: 渋谷店"></div></div>
       <label class="form-label" for="shPw">店舗パスワード <span class="text-danger">*</span></label><input id="shPw" type="password" class="form-control mb-2" placeholder="8文字以上・英数字" autocomplete="new-password">
       <hr style="border-color:var(--rule);margin:14px 0">
       <div class="section-title"><i class="bi bi-person-badge"></i> 店舗責任者アカウント</div>
       <div class="row mt-2"><div class="col-6"><label class="form-label" for="shMgrCode">ユーザーID <span class="text-danger">*</span></label><input id="shMgrCode" class="form-control mb-2" placeholder="例: manager" autocomplete="username"></div><div class="col-6"><label class="form-label" for="shMgrName">氏名 <span class="text-danger">*</span></label><input id="shMgrName" class="form-control mb-2" placeholder="例: 山田太郎"></div></div>
       <label class="form-label" for="shMgrPw">パスワード <span class="text-danger">*</span></label><input id="shMgrPw" type="password" class="form-control" placeholder="8文字以上・英数字" autocomplete="new-password">
       <div class="pw-rules mt-2" id="shPwRules">
         <span class="pw-rule" data-rule="len"><i class="bi bi-circle"></i>8文字以上</span>
         <span class="pw-rule" data-rule="alpha"><i class="bi bi-circle"></i>英字を含む</span>
         <span class="pw-rule" data-rule="digit"><i class="bi bi-circle"></i>数字を含む</span>
       </div>
       <div class="form-error mt-2" id="shFormErr"></div>`,
      async (w, close) => {
        const g = (id) => (w.querySelector(id)?.value || '').trim();
        const errBox = w.querySelector('#shFormErr');
        const showErr = (msg) => { if (errBox) errBox.innerHTML = msg ? `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(msg)}` : ''; };
        showErr('');
        // バリデーション
        if (!g('#shCode')) return showErr('店舗コードを入力してください');
        if (!g('#shName')) return showErr('店舗名を入力してください');
        if (!g('#shMgrCode')) return showErr('店舗責任者のユーザーIDを入力してください');
        if (!g('#shMgrName')) return showErr('店舗責任者の氏名を入力してください');
        const shPw = g('#shPw');
        const mgrPw = g('#shMgrPw');
        const verr1 = validatePassword(shPw);
        if (verr1) return showErr('店舗パスワード: ' + verr1);
        const verr2 = validatePassword(mgrPw);
        if (verr2) return showErr('店舗責任者パスワード: ' + verr2);
        try {
          const result = await api('/admin/shops', { method: 'POST', body: JSON.stringify({
            shop_code: g('#shCode'), shop_name: g('#shName'), password: shPw,
            manager_code: g('#shMgrCode'), manager_password: mgrPw, manager_name: g('#shMgrName'),
          })});
          close();
          toast(`店舗「${g('#shName')}」と店舗責任者「${g('#shMgrName')}」を作成しました`, 'success');
          load();
        } catch (e) { showErr(e.message || '作成に失敗しました'); }
      }, { saveLabel: '店舗を作成' }));
  // リアルタイムパスワード検証（両方のPW入力を監視）
  setTimeout(() => {
    const wrap = document.querySelector('.modal-overlay:last-child');
    if (!wrap) return;
    const pwInputs = wrap.querySelectorAll('#shPw, #shMgrPw');
    const ruleEls = wrap.querySelectorAll('#shPwRules .pw-rule');
    const updateRules = (input) => {
      const v = input?.value || '';
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
    pwInputs.forEach((inp) => inp?.addEventListener('input', () => {
      updateRules(inp);
      wrap.querySelector('#shFormErr').innerHTML = '';
    }));
  }, 50);
};
// 店舗詳細のタブ状態。settingsTab（app.js）と同じ理由でモジュール変数にして
// タブ切替のたびに再生成せず、画面再訪時も直前のタブを覚えておく。
let adminShopTab = 'overview';
// 直前に開いていた店舗のID。設定保存・アーカイブ操作の後は同じ店舗の詳細へ
// navigateTo('adminShopDetail') で戻るため、その場合はタブを維持したい
// （例: 設定タブで保存→設定タブのまま／危険な操作タブでアーカイブ→危険な操作タブのまま）。
// 一方で「別の店舗」を開いたときは前の店舗で見ていたタブ（例: 危険な操作）を
// 引き継ぐべきではない。id が変わったときだけ 'overview' にリセットする。
let adminShopTabForId = null;

SCREENS.adminShopDetail = async function (el) {
  const tok = navToken();
  const sid = window._adminShopId;
  if (adminShopTabForId !== sid) {
    adminShopTab = 'overview';
    adminShopTabForId = sid;
  }
  // include_archived=1 なのは、アーカイブ済み店舗の詳細（解除・完全削除）にも
  // このタブからしか到達できないため（一覧の既定表示では隠れている）。
  const shops = await api('/admin/shops?include_archived=1');
  if (!isAlive(tok)) return;
  const shop = (shops.shops || []).find((s) => s.id === sid);
  if (!shop) { el.innerHTML = emptyState('bi-shop', '店舗が見つかりません'); return; }

  el.innerHTML = pageHead(shop.shop_name || '(名称未設定)', 'bi-shop', shop.shop_code) +
    `<button class="btn btn-sm btn-light mb-3" id="backBtn"><i class="bi bi-arrow-left"></i> 戻る</button>
     <div class="tabs no-print">
       <button class="tab ${adminShopTab === 'overview' ? 'active' : ''}" data-tab="overview">概要</button>
       <button class="tab ${adminShopTab === 'staffs' ? 'active' : ''}" data-tab="staffs">スタッフ</button>
       <button class="tab ${adminShopTab === 'settings' ? 'active' : ''}" data-tab="settings">設定</button>
       <button class="tab ${adminShopTab === 'danger' ? 'active' : ''}" data-tab="danger">危険な操作</button>
     </div><div id="shopTabBody"></div>`;
  document.getElementById('backBtn')?.addEventListener('click', () => navigateTo('adminShops'));
  el.querySelectorAll('.tab').forEach((t) => t?.addEventListener('click', () => {
    adminShopTab = t.dataset.tab;
    el.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === adminShopTab));
    renderAdminShopTab(document.getElementById('shopTabBody'), shop);
  }));
  renderAdminShopTab(document.getElementById('shopTabBody'), shop);
};

// 注意: app.js 側にも同名に見える店舗設定タブの関数 renderShopTab(body) が
// 既に存在する（一般店舗ユーザー向け設定画面の「店舗情報」タブ）。両ファイルは
// モジュール化されておらずグローバル関数として共存するため、同名にすると
// admin.js の読み込み順（app.js の後）で上書きしてしまい、店舗ユーザーの
// 設定画面が壊れる。そのため管理者側は renderAdminShopTab という別名にする。
function renderAdminShopTab(body, shop) {
  ({ overview: renderShopOverviewTab, staffs: renderShopStaffsTab,
     settings: renderShopSettingsTab, danger: renderShopDangerTab }[adminShopTab])(body, shop);
}

/* ---- 概要タブ: 代理閲覧・稼働状態・期間集計 ---- */
async function renderShopOverviewTab(body, shop) {
  const sid = shop.id;
  body.innerHTML =
    card(`<div class="flex gap-2 flex-wrap items-center">
      <button class="btn btn-sm btn-light" id="impersonateBtn"><i class="bi bi-eye"></i> この店舗を代理閲覧</button>
      ${shop.is_archived ? badge('アーカイブ済み', 'warning') : badge(shop.is_active ? '稼働中' : '停止中', shop.is_active ? 'success' : 'warning')}
    </div>`) +
    card(sectionTitle('bi-calendar-range', '期間集計') +
      `<div class="row mb-3">
         <div class="col-5"><label class="form-label" for="sumStart">開始</label><input type="date" id="sumStart" class="form-control"></div>
         <div class="col-5"><label class="form-label" for="sumEnd">終了</label><input type="date" id="sumEnd" class="form-control"></div>
         <div class="col-2 flex items-end"><button class="btn btn-primary w-full" id="sumLoadBtn">表示</button></div>
       </div><div id="sumBody"><div class="text-secondary small">「表示」ボタンを押してください</div></div>`);

  document.getElementById('impersonateBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-eye"></i> 代理閲覧',
      `<p class="mb-2">この店舗の画面を<strong>閲覧のみ</strong>の権限で開きます。</p>
       <p class="small text-secondary mb-0">データの変更はできません。開始と終了は監査ログに記録されます。</p>`,
      async (w, close) => {
        try {
          const d = await api(`/admin/impersonate/${sid}`, { method: 'POST' });
          window._impersonating = { shop_id: d.shop.id, shop_name: d.shop.shop_name };
          close();
          renderImpersonationBar();
          renderNav();
          navigateTo('dashboard');
          toast(`${d.shop.shop_name} を代理閲覧中です`, 'success');
        } catch (e) { toast(e.message, 'error'); }
      },
      { saveLabel: '代理閲覧を開始' }));

  async function loadSummary() {
    const start = document.getElementById('sumStart')?.value;
    const end = document.getElementById('sumEnd')?.value;
    if (!start || !end) return;
    const sumBody = document.getElementById('sumBody');
    if (!sumBody) return;  // 画面遷移済み
    const tok = navToken();
    sumBody.innerHTML = '<div class="text-secondary small">読み込み中...</div>';
    try {
      const sum = await api(`/admin/shops/summary/${sid}?start=${start}&end=${end}`);
      if (!isAlive(tok) || !sumBody.isConnected) return;  // 画面遷移済み
      sumBody.innerHTML = sum.staff.length
        ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>氏名</th><th>日</th><th class="t-num">確定</th><th class="t-num">給与</th></tr></thead><tbody>${sum.staff.map((s) => `<tr><td>${esc(s.name)}</td><td>${s.days}</td><td class="t-num num">${s.confirmed_hours}h</td><td class="t-num num">${yen(s.pay)}</td></tr>`).join('')}<tr style="font-weight:800;color:var(--ink)"><td>合計</td><td></td><td class="t-num num">${sum.total_hours}h</td><td class="t-num num">${yen(sum.total_pay)}</td></tr></tbody></table></div>`
        : '<div class="small text-secondary">確定シフトなし</div>';
    } catch (e) {
      if (!isAlive(tok) || !sumBody.isConnected) return;
      sumBody.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
    }
  }
  document.getElementById('sumLoadBtn')?.addEventListener('click', loadSummary);
  api(`/admin/shops/${sid}/periods/next`).then((p) => {
    const ds = document.getElementById('sumStart'); const de = document.getElementById('sumEnd');
    if (!ds || !de) return;  // 画面遷移済み
    ds.value = p.start_date; de.value = p.end_date; loadSummary();
  }).catch(() => {});
}

/* ---- スタッフタブ: 一覧・検索・追加・編集・ロール変更・PWリセット・旧仕様昇格 ---- */
async function renderShopStaffsTab(body, shop) {
  const sid = shop.id;
  body.innerHTML =
    card(`<div class="flex gap-2 flex-wrap mb-3">
        <button class="btn btn-primary btn-sm" id="addStaffBtn"><i class="bi bi-person-plus"></i> スタッフ追加</button>
        <button class="btn btn-light btn-sm" id="migrateBtn" title="旧仕様店主のPWを引き継いで manager スタッフを作成"><i class="bi bi-arrow-up-circle"></i> 旧仕様から manager 昇格</button>
      </div>
      <div id="staffBody"><div class="text-secondary small">読み込み中...</div></div>`);

  document.getElementById('addStaffBtn')?.addEventListener('click', () => openAdminAddStaffModal(sid, loadStaffs));
  document.getElementById('migrateBtn')?.addEventListener('click', () => openAdminMigrateModal(sid, shop, loadStaffs));

  async function loadStaffs() {
    const staffBody = document.getElementById('staffBody');
    if (!staffBody) return;  // 画面遷移済み → 更新中止
    const tok = navToken();
    staffBody.innerHTML = '<div class="text-secondary small">読み込み中...</div>';
    try {
      const st = await api(`/admin/shops/staffs/${sid}`);
      if (!isAlive(tok) || !staffBody.isConnected) return;  // 画面遷移済み
      const slist = (st.staffs || []).map((s) => `
        <div class="list-row" data-staff-row data-search="${esc((s.name || '') + ' ' + (s.staff_code || '') + ' ' + roleLabel(s.role))}">
          <div class="staff-cell">
            <span class="staff-name">${esc(s.name)}</span>
            <span class="staff-sub">${esc(s.staff_code)} ・ ${roleLabel(s.role)}${s.is_resigned ? ' ・ 退職' : ''}</span>
          </div>
          <div class="flex gap-1">
            <button class="btn btn-sm btn-light" data-staff-edit='${esc(JSON.stringify(s))}' title="編集"><i class="bi bi-pencil"></i></button>
            <button class="btn btn-sm btn-light" data-role-edit="${s.id}" data-name="${esc(s.name)}" data-role="${s.role}" title="ロール変更"><i class="bi bi-shield-lock"></i></button>
            <button class="btn btn-sm btn-light" data-pw-reset="${s.id}" data-name="${esc(s.name)}" title="パスワードリセット"><i class="bi bi-key"></i></button>
          </div>
        </div>`).join('');
      const searchBox = `<input type="search" id="staffSearch" class="form-control mb-2" placeholder="氏名・コード・ロールで絞り込み">`;
      staffBody.innerHTML = sectionTitle('bi-people', `スタッフ（${st.staffs.length}名）`) + searchBox +
        `<div id="staffListBox">${(st.staffs || []).length ? slist : emptyState('bi-people', 'スタッフがいません')}</div>`;
      // スタッフ検索（フロント側フィルタ）
      document.getElementById('staffSearch')?.addEventListener('input', (ev) => {
        const q = ev.target.value.trim().toLowerCase();
        staffBody.querySelectorAll('[data-staff-row]').forEach((row) => {
          row.style.display = (!q || (row.dataset.search || '').toLowerCase().includes(q)) ? '' : 'none';
        });
      });
      // 汎用編集ボタン
      staffBody.querySelectorAll('[data-staff-edit]').forEach((b) => b?.addEventListener('click', () => {
        let s2; try { s2 = JSON.parse(b.dataset.staffEdit); } catch { return; }
        openAdminStaffEditModal(sid, s2, loadStaffs);
      }));
      // ロール変更ボタン
      staffBody.querySelectorAll('[data-role-edit]').forEach((b) => b?.addEventListener('click', () => {
        openAdminRoleModal(sid, +b.dataset.roleEdit, b.dataset.name, b.dataset.role, loadStaffs);
      }));
      // パスワードリセットボタン
      staffBody.querySelectorAll('[data-pw-reset]').forEach((b) => b?.addEventListener('click', () => {
        openAdminPwResetModal(sid, +b.dataset.pwReset, b.dataset.name);
      }));
    } catch (e) {
      if (!isAlive(tok) || !staffBody.isConnected) return;
      staffBody.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
    }
  }
  loadStaffs();
}

/* ---- 設定タブ: 店舗名・店舗コード・シフト設定 ---- */
async function renderShopSettingsTab(body, shop) {
  const s = shop.settings ? (typeof shop.settings === 'string' ? JSON.parse(shop.settings || '{}') : shop.settings) : {};
  // settings は店舗側の PUT /api/shop/settings 経由でも保存され得るため、値が
  // 数値である保証はサーバ側バリデーションに頼れない（防御的にもここで esc() する）。
  // num() 自身が esc() 済みの文字列を返すことで、呼び出し側での貼り忘れを防ぐ。
  const num = (v) => (v === undefined || v === null ? '' : esc(String(v)));
  body.innerHTML =
    card(sectionTitle('bi-shop', '店舗情報') +
      `<label class="form-label" for="stName">店舗名</label>
       <input id="stName" class="form-control mb-2" value="${esc(shop.shop_name || '')}">
       <label class="form-label" for="stCode">店舗コード</label>
       <input id="stCode" class="form-control mb-2" value="${esc(shop.shop_code || '')}">
       <div class="form-error" id="stErr"></div>
       <button class="btn btn-primary btn-sm mt-2" id="stSaveBtn">保存</button>`) +
    card(sectionTitle('bi-sliders', 'シフト設定') +
      `<div class="row">
         <div class="col-6"><label class="form-label" for="cfWage">既定時給</label><input type="number" id="cfWage" class="form-control mb-2" value="${num(s.default_hourly_wage)}"></div>
         <div class="col-6"><label class="form-label" for="cfMaxDaily">1日の上限時間</label><input type="number" id="cfMaxDaily" class="form-control mb-2" value="${num(s.max_daily_hours)}"></div>
         <div class="col-6"><label class="form-label" for="cfMinDaily">1日の下限時間</label><input type="number" id="cfMinDaily" class="form-control mb-2" value="${num(s.min_daily_hours)}"></div>
         <div class="col-6"><label class="form-label" for="cfEmpDaily">社員の1日上限</label><input type="number" id="cfEmpDaily" class="form-control mb-2" value="${num(s.max_employee_daily_hours)}"></div>
         <div class="col-6"><label class="form-label" for="cfConsec">連勤上限（日）</label><input type="number" id="cfConsec" class="form-control mb-2" value="${num(s.max_consecutive_days)}"></div>
         <div class="col-6"><label class="form-label" for="cfTransport">1日あたり交通費</label><input type="number" id="cfTransport" class="form-control mb-2" value="${num(s.transport_per_day)}"></div>
       </div>
       <div class="form-error" id="cfErr"></div>
       <button class="btn btn-primary btn-sm mt-2" id="cfSaveBtn">保存</button>`);

  document.getElementById('stSaveBtn')?.addEventListener('click', async () => {
    const err = document.getElementById('stErr');
    if (err) err.textContent = '';
    try {
      await api(`/admin/shops/${shop.id}`, { method: 'PUT', body: JSON.stringify({
        shop_name: document.getElementById('stName').value.trim(),
        shop_code: document.getElementById('stCode').value.trim() }) });
      toast('保存しました', 'success');
      navigateTo('adminShopDetail');
    } catch (e) { if (err) err.textContent = e.message; }
  });

  document.getElementById('cfSaveBtn')?.addEventListener('click', async () => {
    const err = document.getElementById('cfErr');
    if (err) err.textContent = '';
    const pick = (id) => {
      const v = document.getElementById(id).value.trim();
      return v === '' ? undefined : Number(v);
    };
    const payload = {};
    const map = { cfWage: 'default_hourly_wage', cfMaxDaily: 'max_daily_hours',
                  cfMinDaily: 'min_daily_hours', cfEmpDaily: 'max_employee_daily_hours',
                  cfConsec: 'max_consecutive_days', cfTransport: 'transport_per_day' };
    Object.keys(map).forEach((id) => { const v = pick(id); if (v !== undefined) payload[map[id]] = v; });
    try {
      await api(`/admin/shops/${shop.id}/settings`, { method: 'PUT', body: JSON.stringify(payload) });
      toast('保存しました', 'success');
    } catch (e) { if (err) err.textContent = e.message; }
  });
}

/* ---- 危険な操作タブ: アーカイブ・エクスポート・完全削除 ---- */
function renderShopDangerTab(body, shop) {
  body.innerHTML = card(sectionTitle('bi-exclamation-triangle', '危険な操作') +
    `<div class="list-row">
       <div><strong>${shop.is_archived ? 'アーカイブを解除' : 'アーカイブ'}</strong>
         <div class="small text-secondary">${shop.is_archived
           ? '一覧に再表示します。稼働させるには別途「有効化」が必要です。'
           : '一覧から隠し、ログインを停止します。データは残ります。'}</div></div>
       <button class="btn btn-sm btn-light" id="archBtn">${shop.is_archived ? '解除' : 'アーカイブ'}</button>
     </div>
     <div class="list-row">
       <div><strong>データのエクスポート</strong>
         <div class="small text-secondary">この店舗の全データを JSON でダウンロードします。</div></div>
       <button class="btn btn-sm btn-light" id="expBtn"><i class="bi bi-download"></i> ダウンロード</button>
     </div>
     <div class="list-row">
       <div><strong>完全削除</strong>
         <div class="small text-secondary">${shop.is_archived
           ? 'アーカイブ済みです。エクスポートしてから削除できます。'
           : '先にアーカイブしてください。'}</div></div>
       <button class="btn btn-sm btn-outline-danger" id="delBtn" disabled><i class="bi bi-trash"></i> 完全削除</button>
     </div>`);

  document.getElementById('archBtn')?.addEventListener('click', async () => {
    const path = shop.is_archived ? 'unarchive' : 'archive';
    try {
      await api(`/admin/shops/${shop.id}/${path}`, { method: 'POST' });
      toast(shop.is_archived ? 'アーカイブを解除しました' : 'アーカイブしました', 'success');
      navigateTo('adminShopDetail');
    } catch (e) { toast(e.message, 'error'); }
  });

  document.getElementById('expBtn')?.addEventListener('click', async () => {
    try {
      // api() は JSON を返す前提なので、ファイル取得は fetch を直に使う
      const res = await fetch(`/api/admin/shops/${shop.id}/export`, {
        headers: { Authorization: 'Bearer ' + localStorage.getItem('shift_token') } });
      if (!res.ok) throw new Error('エクスポートに失敗しました');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `shop-${shop.shop_code}.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast('エクスポートしました', 'success');
      // エクスポート済みのときだけ削除を許可する（取り返しのつかない操作の前に
      // 必ずバックアップを取らせる）
      const del = document.getElementById('delBtn');
      if (del && shop.is_archived) del.disabled = false;
    } catch (e) { toast(e.message, 'error'); }
  });

  document.getElementById('delBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-trash text-danger"></i> 店舗の完全削除',
      `<div class="text-center py-2">
         <div class="mb-2"><i class="bi bi-exclamation-triangle-fill text-danger" style="font-size:2.2rem"></i></div>
         <p class="mb-1"><strong>${esc(shop.shop_name || '')}</strong> と、その全スタッフ・シフト・希望を削除します。</p>
         <p class="small text-secondary">この操作は取り消せません。監査ログのみ記録として残ります。</p>
       </div>
       <label class="form-label" for="delCode">確認のため店舗コード <strong>${esc(shop.shop_code)}</strong> を入力してください</label>
       <input id="delCode" class="form-control" autocomplete="off">
       <div class="form-error" id="delErr"></div>`,
      async (w, close) => {
        const err = w.querySelector('#delErr');
        try {
          await api(`/admin/shops/${shop.id}`, { method: 'DELETE',
            body: JSON.stringify({ confirm_code: w.querySelector('#delCode').value.trim() }) });
          close(); toast('削除しました', 'success'); navigateTo('adminShops');
        } catch (e) { if (err) err.textContent = e.message; }
      }, { saveLabel: '完全に削除する', btnClass: 'btn-danger' }));
}

const AUDIT_ACTION_LABELS = {
  'shift.finalize': 'シフト確定',
  'creq.approve': '変更申請 承認',
  'creq.reject': '変更申請 却下',
  'staff.role_change': 'ロール変更',
  'staff.password_reset': 'パスワードリセット',
  'staff.update': 'スタッフ編集',
  'staff.create': 'スタッフ作成',
  'shop.create': '店舗作成',
  'shop.update': '店舗更新',
  'shop.archive': '店舗アーカイブ',
  'shop.unarchive': '店舗復元',
  'shop.export': '店舗エクスポート',
  // 最も重い操作なので絞り込めるようにする。開始と完了で2行出るのは意図的で、
  // 完了が無い＝途中で失敗した、が監査ログだけで判別できる。
  'shop.delete': '店舗完全削除（開始）',
  'shop.delete_done': '店舗完全削除（完了）',
  'auth.login': 'ログイン',
  'auth.login_failed': 'ログイン失敗',
  'auth.login_blocked': 'ログインブロック',
  'auth.logout': 'ログアウト',
  'admin.password_change': '管理者PW変更',
  'admin.impersonate_start': '代理閲覧開始',
  'admin.impersonate_end': '代理閲覧終了',
  'admin.create': '管理者追加',
  'admin.delete': '管理者削除',
  'admin.migrate': 'DBマイグレーション適用',
};
function auditActionLabel(a) { return AUDIT_ACTION_LABELS[a] || a || '—'; }

SCREENS.adminAudit = async function (el) {
  const tok = navToken();
  // include_archived=1: アーカイブ済み店舗の過去ログも店舗フィルタで絞り込めるようにする
  const shopsResp = await api('/admin/shops?include_archived=1');
  if (!isAlive(tok)) return;
  const shops = shopsResp.shops || [];
  el.innerHTML = pageHead('監査ログ', 'bi-clipboard-data', '重要操作の履歴') +
    card(sectionTitle('bi-funnel', 'フィルタ') +
      `<div class="row">
         <div class="col-6 col-lg-3"><label class="form-label" for="auStart">開始日</label><input type="date" id="auStart" class="form-control mb-2"></div>
         <div class="col-6 col-lg-3"><label class="form-label" for="auEnd">終了日</label><input type="date" id="auEnd" class="form-control mb-2"></div>
         <div class="col-6 col-lg-3"><label class="form-label" for="auShop">店舗</label>
           <select id="auShop" class="form-select mb-2"><option value="">すべて</option>
             ${shops.map((s) => `<option value="${s.id}">${esc(s.shop_name || s.shop_code)}</option>`).join('')}
           </select></div>
         <div class="col-6 col-lg-3"><label class="form-label" for="auActor">操作者</label><input id="auActor" class="form-control mb-2" placeholder="氏名の一部"></div>
         <div class="col-6 col-lg-3"><label class="form-label" for="auAction">操作</label>
           <select id="auAction" class="form-select mb-2"><option value="">すべて</option>
             ${Object.keys(AUDIT_ACTION_LABELS).map((a) => `<option value="${esc(a)}">${esc(AUDIT_ACTION_LABELS[a])}</option>`).join('')}
           </select></div>
         <div class="col-12 flex gap-2 items-end">
           <button class="btn btn-primary btn-sm" id="auLoad"><i class="bi bi-search"></i> 表示</button>
           <button class="btn btn-light btn-sm" id="auCsv"><i class="bi bi-download"></i> CSV</button>
         </div>
       </div>`) +
    card('<div id="auBody"><div class="text-secondary small">「表示」ボタンを押してください</div></div>' +
         '<div class="text-center mt-2"><button class="btn btn-light btn-sm d-none" id="auMore">もっと見る</button></div>');

  // ページング中のログを溜め込む配列。「もっと見る」は追記、「表示」は置き換え。
  let rows = [];
  const qs = (beforeId) => {
    const p = new URLSearchParams();
    const v = (id) => document.getElementById(id).value.trim();
    if (v('auStart')) p.set('start', v('auStart'));
    if (v('auEnd')) p.set('end', v('auEnd'));
    if (v('auShop')) p.set('shop', v('auShop'));
    if (v('auActor')) p.set('actor', v('auActor'));
    if (v('auAction')) p.set('action', v('auAction'));
    p.set('limit', '100');
    if (beforeId) p.set('before_id', beforeId);
    return p;
  };

  const render = () => {
    const body = document.getElementById('auBody');
    if (!body) return;
    body.innerHTML = rows.length
      ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>日時</th><th>操作者</th><th>操作</th><th>対象</th><th>詳細</th></tr></thead><tbody>` +
        rows.map((l) => {
          const a = l.action || '';
          // 失敗・却下・削除系は目立たせる（要確認の意味で warning）
          const isWarn = a.indexOf('reject') >= 0 || a.indexOf('fail') >= 0 ||
            a.indexOf('blocked') >= 0 || a.indexOf('delete') >= 0;
          return `<tr>
          <td class="small">${esc((l.created_at || '').replace('T', ' '))}</td>
          <td class="small">${esc(l.actor_name || l.actor_role || '—')}</td>
          <td>${badge(auditActionLabel(a), isWarn ? 'warning' : 'info')}</td>
          <td class="small">${esc(l.target_type || '')}${l.target_id != null ? ' #' + l.target_id : ''}</td>
          <td class="small text-secondary">${esc(l.detail || '')}</td></tr>`;
        }).join('') + '</tbody></table></div>'
      : emptyState('bi-clipboard-data', '該当するログがありません');
  };

  const load = async (beforeId) => {
    const body = document.getElementById('auBody');
    if (!body) return;
    const tok2 = navToken();
    if (!beforeId) body.innerHTML = '<div class="text-secondary small">読み込み中...</div>';
    try {
      const d = await api('/admin/audit-logs?' + qs(beforeId).toString());
      if (!isAlive(tok2)) return;
      rows = beforeId ? rows.concat(d.logs) : d.logs;
      render();
      const more = document.getElementById('auMore');
      if (more) more.classList.toggle('d-none', !d.has_more);
    } catch (e) {
      if (!isAlive(tok2) || !body.isConnected) return;
      body.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
    }
  };

  document.getElementById('auLoad')?.addEventListener('click', () => load());
  document.getElementById('auMore')?.addEventListener('click', () => {
    if (rows.length) load(rows[rows.length - 1].id);
  });
  document.getElementById('auCsv')?.addEventListener('click', async () => {
    try {
      // api() は JSON を返す前提なので、ファイル取得は fetch を直に使う
      const p = qs();
      p.delete('limit');
      const res = await fetch('/api/admin/audit-logs.csv?' + p.toString(), {
        headers: { Authorization: 'Bearer ' + localStorage.getItem('shift_token') } });
      if (!res.ok) throw new Error('ダウンロードに失敗しました');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'audit-logs.csv';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { toast(e.message, 'error'); }
  });
  load();
};

function openAdminStaffEditModal(shopId, s, onDone) {
  const roles = [
    { v: 'manager', label: '店舗管理者（manager）' },
    { v: 'employee', label: '社員（employee）' },
    { v: 'part_time', label: 'アルバイト（part_time）' },
    { v: 'student', label: '学生アルバイト（student）' },
  ];
  openModal(`<i class="bi bi-pencil"></i> スタッフ編集 — ${esc(s.name)}`,
    `<label class="form-label" for="aeName">氏名</label>
     <input id="aeName" class="form-control mb-2" value="${esc(s.name || '')}">
     <label class="form-label" for="aeRole">ロール</label>
     <select id="aeRole" class="form-select mb-2">${roles.map((o) => `<option value="${o.v}" ${o.v === s.role ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select>
     <label class="form-label" for="aeWage">時給</label>
     <input id="aeWage" type="number" class="form-control mb-2" value="${s.hourly_wage != null ? s.hourly_wage : ''}">
     <div class="flex items-center gap-2 mb-1"><input id="aeResigned" type="checkbox" ${s.is_resigned ? 'checked' : ''}><label for="aeResigned" class="form-label mb-0">退職として扱う</label></div>
     <div class="small text-secondary mb-2"><i class="bi bi-info-circle"></i> 学生アルバイトは月80h上限が自動適用されます。ロール変更でセッションは無効化されません（軽微編集用）。</div>
     <div class="form-error mt-1" id="aeErr"></div>`,
    async (w, close) => {
      const errBox = w.querySelector('#aeErr');
      const showErr = (m) => { if (errBox) errBox.innerHTML = m ? `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(m)}` : ''; };
      showErr('');
      const payload = {
        name: w.querySelector('#aeName').value.trim(),
        role: w.querySelector('#aeRole').value,
        hourly_wage: +w.querySelector('#aeWage').value || 0,
        is_resigned: w.querySelector('#aeResigned').checked ? 1 : 0,
      };
      if (!payload.name) { showErr('氏名を入力してください'); return; }
      try {
        await api(`/admin/shops/${shopId}/staffs/${s.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        close();
        toast(`${payload.name} さんの情報を更新しました`, 'success');
        onDone?.();
      } catch (e) { showErr(e.message || '更新に失敗しました'); }
    });
}

function openAdminRoleModal(shopId, staffId, staffName, currentRole, onDone) {
  const opts = [
    { v: 'manager', label: '店舗管理者（manager）— 店舗権限でログイン' },
    { v: 'employee', label: '社員（employee）' },
    { v: 'part_time', label: 'アルバイト（part_time）' },
    { v: 'student', label: '学生アルバイト（student・月80h上限）' },
  ];
  openModal(`<i class="bi bi-shield-lock"></i> ロール変更 — ${esc(staffName)}`,
    `<p class="small text-secondary mb-2">現在のロール: <strong>${roleLabel(currentRole)}</strong></p>
     <label class="form-label">新しいロール</label>
     <select id="admRoleSel" class="form-select">${opts.map((o) => `<option value="${o.v}" ${o.v === currentRole ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select>
     <div class="small text-secondary mt-2"><i class="bi bi-info-circle"></i> 変更すると、このスタッフの既存ログインセッションは無効化されます（再ログインが必要）。</div>
     <div class="form-error mt-2" id="admRoleErr"></div>`,
    async (w, close) => {
      const errBox = w.querySelector('#admRoleErr');
      const showErr = (m) => { if (errBox) errBox.innerHTML = m ? `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(m)}` : ''; };
      showErr('');
      const newRole = w.querySelector('#admRoleSel').value;
      try {
        await api(`/admin/shops/${shopId}/staffs/${staffId}/role`, {
          method: 'PUT', body: JSON.stringify({ role: newRole })
        });
        close();
        toast(`${staffName} さんのロールを ${roleLabel(newRole)} に変更しました`, 'success');
        onDone?.();
      } catch (e) { showErr(e.message || '変更に失敗しました'); }
    });
}

function openAdminPwResetModal(shopId, staffId, staffName) {
  openModal(`<i class="bi bi-key"></i> パスワードリセット — ${esc(staffName)}`,
    `<p class="small text-secondary mb-2">このスタッフのパスワードを新しいものにリセットします。変更後、再ログインが必要になります。</p>
     <label class="form-label">新しいパスワード（8文字以上・英数字）</label>
     <input id="admPwInput" type="password" class="form-control" autocomplete="new-password">
     <div class="pw-rules mt-2" id="admPwRules">
       <span class="pw-rule" data-rule="len"><i class="bi bi-circle"></i>8文字以上</span>
       <span class="pw-rule" data-rule="alpha"><i class="bi bi-circle"></i>英字を含む</span>
       <span class="pw-rule" data-rule="digit"><i class="bi bi-circle"></i>数字を含む</span>
     </div>
     <div class="form-error mt-2" id="admPwErr"></div>`,
    async (w, close) => {
      const errBox = w.querySelector('#admPwErr');
      const showErr = (m) => { if (errBox) errBox.innerHTML = m ? `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(m)}` : ''; };
      showErr('');
      const pw = w.querySelector('#admPwInput').value;
      const verr = validatePassword(pw);
      if (verr) { showErr(verr); return; }
      try {
        await api(`/admin/shops/${shopId}/staffs/${staffId}/password`, {
          method: 'PUT', body: JSON.stringify({ new_password: pw })
        });
        close();
        toast(`${staffName} さんのパスワードをリセットしました`, 'success');
      } catch (e) { showErr(e.message || 'リセットに失敗しました'); }
    });
  // リアルタイムパスワード検証
  setTimeout(() => {
    const wrap = document.querySelector('.modal-overlay:last-child');
    if (!wrap) return;
    const pwInput = wrap.querySelector('#admPwInput');
    const ruleEls = wrap.querySelectorAll('.pw-rule');
    const updateRules = () => {
      const v = pwInput.value || '';
      const checks = { len: v.length >= 8, alpha: /[A-Za-z]/.test(v), digit: /[0-9]/.test(v) };
      ruleEls.forEach((el) => {
        const ok = checks[el.dataset.rule];
        el.classList.toggle('ok', !!ok && v.length > 0);
        el.classList.toggle('ng', !ok && v.length > 0);
        el.querySelector('i').className = ok ? 'bi bi-check-circle-fill' : 'bi bi-x-circle-fill';
      });
    };
    pwInput?.addEventListener('input', () => { updateRules(); wrap.querySelector('#admPwErr').innerHTML = ''; });
  }, 50);
}

function openAdminAddStaffModal(shopId, onDone) {
  openModal(`<i class="bi bi-person-plus"></i> スタッフ追加`,
    `<p class="small text-secondary mb-2">任意のユーザーコードとロールでスタッフを作成します。</p>
     <div class="row">
       <div class="col-6"><label class="form-label" for="admStaffCode">ユーザーコード <span class="text-danger">*</span></label><input id="admStaffCode" class="form-control" placeholder="例: yamada"></div>
       <div class="col-6"><label class="form-label" for="admStaffName">氏名 <span class="text-danger">*</span></label><input id="admStaffName" class="form-control"></div>
     </div>
     <label class="form-label mt-2">ロール</label>
     <select id="admStaffRole" class="form-select">
       <option value="manager">店舗管理者（manager）— 店舗権限でログイン</option>
       <option value="employee" selected>社員（employee）</option>
       <option value="part_time">アルバイト（part_time）</option>
       <option value="student">学生アルバイト（student・月80h上限）</option>
     </select>
     <label class="form-label mt-2">パスワード（8文字以上・英数字）</label>
     <input id="admStaffPw" type="password" class="form-control" autocomplete="new-password">
     <div class="pw-rules mt-2" id="admStaffPwRules">
       <span class="pw-rule" data-rule="len"><i class="bi bi-circle"></i>8文字以上</span>
       <span class="pw-rule" data-rule="alpha"><i class="bi bi-circle"></i>英字を含む</span>
       <span class="pw-rule" data-rule="digit"><i class="bi bi-circle"></i>数字を含む</span>
     </div>
     <div class="form-error mt-2" id="admStaffErr"></div>`,
    async (w, close) => {
      const errBox = w.querySelector('#admStaffErr');
      const showErr = (m) => { if (errBox) errBox.innerHTML = m ? `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(m)}` : ''; };
      showErr('');
      const code = w.querySelector('#admStaffCode').value.trim();
      const name = w.querySelector('#admStaffName').value.trim();
      const role = w.querySelector('#admStaffRole').value;
      const pw = w.querySelector('#admStaffPw').value;
      if (!code) return showErr('ユーザーコードを入力してください');
      if (!name) return showErr('氏名を入力してください');
      const verr = validatePassword(pw);
      if (verr) return showErr(verr);
      try {
        await api(`/admin/shops/${shopId}/staffs`, {
          method: 'POST', body: JSON.stringify({ staff_code: code, name, password: pw, role })
        });
        close();
        toast(`${name} さんを追加しました（${roleLabel(role)}）`, 'success');
        onDone?.();
      } catch (e) { showErr(e.message || '追加に失敗しました'); }
    });
  // リアルタイムパスワード検証
  setTimeout(() => {
    const wrap = document.querySelector('.modal-overlay:last-child');
    if (!wrap) return;
    const pwInput = wrap.querySelector('#admStaffPw');
    const ruleEls = wrap.querySelectorAll('#admStaffPwRules .pw-rule');
    const updateRules = () => {
      const v = pwInput.value || '';
      const checks = { len: v.length >= 8, alpha: /[A-Za-z]/.test(v), digit: /[0-9]/.test(v) };
      ruleEls.forEach((el) => {
        const ok = checks[el.dataset.rule];
        el.classList.toggle('ok', !!ok && v.length > 0);
        el.classList.toggle('ng', !ok && v.length > 0);
        el.querySelector('i').className = ok ? 'bi bi-check-circle-fill' : 'bi bi-x-circle-fill';
      });
    };
    pwInput?.addEventListener('input', () => { updateRules(); wrap.querySelector('#admStaffErr').innerHTML = ''; });
  }, 50);
}

function openAdminMigrateModal(shopId, shop, onDone) {
  openModal(`<i class="bi bi-arrow-up-circle"></i> 旧仕様から manager 昇格 — ${esc(shop.shop_name || '')}`,
    `<p class="small text-secondary mb-2">
       旧仕様（shops テーブルのパスワード直接利用）でログインしていた店主を、
       新仕様の <strong>manager ロール</strong> に昇格させます。<br>
       <strong>パスワードは旧仕様のものを引き継ぎ</strong>ます（同じPWでログイン可）。
     </p>
     <div class="row">
       <div class="col-6"><label class="form-label" for="admMigrateCode">ユーザーコード <span class="text-danger">*</span></label><input id="admMigrateCode" class="form-control" placeholder="例: manager / yamada 等"></div>
       <div class="col-6"><label class="form-label" for="admMigrateName">氏名</label><input id="admMigrateName" class="form-control" placeholder="未入力時は店舗名+店主"></div>
     </div>
     <div class="small text-secondary mt-2"><i class="bi bi-info-circle"></i> 任意のユーザーコードで構いません（'manager' でなくてもOK）。</div>
     <div class="form-error mt-2" id="admMigrateErr"></div>`,
    async (w, close) => {
      const errBox = w.querySelector('#admMigrateErr');
      const showErr = (m) => { if (errBox) errBox.innerHTML = m ? `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(m)}` : ''; };
      showErr('');
      const code = w.querySelector('#admMigrateCode').value.trim();
      const name = w.querySelector('#admMigrateName').value.trim();
      if (!code) return showErr('ユーザーコードを入力してください');
      try {
        const r = await api(`/admin/shops/${shopId}/migrate-legacy-manager`, {
          method: 'POST', body: JSON.stringify({ staff_code: code, name: name || undefined })
        });
        close();
        toast(`昇格しました: ${r.staff_code}（PWは旧仕様のまま）`, 'success');
        onDone?.();
      } catch (e) { showErr(e.message || '昇格に失敗しました'); }
    });
}

/* ---------- システム画面（管理者アカウント / マイグレーション / DB診断） ---------- */
let adminSystemTab = 'admins';

SCREENS.adminSystem = async function (el) {
  el.innerHTML = pageHead('システム', 'bi-gear') +
    `<div class="tabs no-print">
       <button class="tab ${adminSystemTab === 'admins' ? 'active' : ''}" data-tab="admins">管理者アカウント</button>
       <button class="tab ${adminSystemTab === 'migrations' ? 'active' : ''}" data-tab="migrations">マイグレーション</button>
       <button class="tab ${adminSystemTab === 'diagnostic' ? 'active' : ''}" data-tab="diagnostic">DB診断</button>
     </div><div id="sysBody"></div>`;
  el.querySelectorAll('.tab').forEach((t) => t?.addEventListener('click', () => {
    adminSystemTab = t.dataset.tab;
    el.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === adminSystemTab));
    renderAdminSystemTab(document.getElementById('sysBody'));
  }));
  renderAdminSystemTab(document.getElementById('sysBody'));
};

function renderAdminSystemTab(body) {
  ({ admins: renderAdminAccountsTab, migrations: renderMigrationsTab,
     diagnostic: renderDiagnosticTab }[adminSystemTab])(body);
}

async function renderAdminAccountsTab(body) {
  const tok = navToken();
  body.innerHTML = card(sectionTitle('bi-person-badge', '管理者アカウント',
    '<button class="btn btn-primary btn-sm" id="addAdminBtn"><i class="bi bi-plus-lg"></i></button>') +
    '<div id="adminList"></div>') +
    card(sectionTitle('bi-key', '自分のパスワード') +
      '<button class="btn btn-light btn-sm" id="chgPwBtn"><i class="bi bi-key"></i> パスワードを変更</button>');
  const d = await api('/admin/admins');
  if (!isAlive(tok)) return;
  const list = document.getElementById('adminList');
  if (!list) return;
  list.innerHTML = d.admins.length ? d.admins.map((a) => `
    <div class="list-row">
      <div><strong>${esc(a.name || '')}</strong> <span class="text-secondary">${esc(a.admin_id)}</span>
        <div class="small text-secondary">作成 ${esc((a.created_at || '').replace('T', ' '))}</div></div>
      <button class="btn btn-sm btn-outline-danger" data-deladmin="${a.id}" data-name="${esc(a.admin_id)}"><i class="bi bi-trash"></i></button>
    </div>`).join('') : emptyState('bi-person-badge', '管理者がいません');
  list.querySelectorAll('[data-deladmin]').forEach((b) => b?.addEventListener('click', () =>
    openModal('<i class="bi bi-trash text-danger"></i> 管理者の削除',
      `<div class="text-center py-2">
         <div class="mb-2"><i class="bi bi-exclamation-triangle-fill text-danger" style="font-size:2.2rem"></i></div>
         <p class="mb-1"><strong>${esc(b.dataset.name)}</strong> を削除しますか？</p>
         <p class="small text-secondary mb-0">この管理者のセッションは即座に無効になります。</p>
       </div>`,
      async (w, close) => {
        try {
          await api(`/admin/admins/${b.dataset.deladmin}`, { method: 'DELETE' });
          close(); toast('削除しました', 'success'); renderAdminAccountsTab(body);
        } catch (e) { toast(e.message, 'error'); }
      }, { saveLabel: '削除する', btnClass: 'btn-danger' })));

  document.getElementById('addAdminBtn')?.addEventListener('click', () => {
    openModal('<i class="bi bi-plus-lg"></i> 管理者を追加',
      `<label class="form-label" for="naId">管理者ID <span class="text-danger">*</span></label>
       <input id="naId" class="form-control mb-2" placeholder="例: ops2" autocomplete="username">
       <label class="form-label" for="naName">氏名</label>
       <input id="naName" class="form-control mb-2" placeholder="例: 運営 花子">
       <label class="form-label" for="naPw">パスワード <span class="text-danger">*</span></label>
       <input id="naPw" type="password" class="form-control" autocomplete="new-password">
       <div class="pw-rules mt-2" id="naPwRules">
         <span class="pw-rule" data-rule="len"><i class="bi bi-circle"></i>8文字以上</span>
         <span class="pw-rule" data-rule="alpha"><i class="bi bi-circle"></i>英字を含む</span>
         <span class="pw-rule" data-rule="digit"><i class="bi bi-circle"></i>数字を含む</span>
       </div>
       <div class="form-error" id="naErr"></div>`,
      async (w, close) => {
        const err = w.querySelector('#naErr');
        try {
          await api('/admin/admins', { method: 'POST', body: JSON.stringify({
            admin_id: w.querySelector('#naId').value.trim(),
            name: w.querySelector('#naName').value.trim(),
            password: w.querySelector('#naPw').value }) });
          close(); toast('追加しました', 'success'); renderAdminAccountsTab(body);
        } catch (e) { if (err) err.textContent = e.message; }
      }, { saveLabel: '追加する' });
    wirePasswordRuleCheck('naPw', 'naPwRules');
  });

  document.getElementById('chgPwBtn')?.addEventListener('click', () => {
    openModal('<i class="bi bi-key"></i> パスワード変更',
      `<label class="form-label" for="cpCur">現在のパスワード</label>
       <input id="cpCur" type="password" class="form-control mb-2" autocomplete="current-password">
       <label class="form-label" for="cpNew">新しいパスワード</label>
       <input id="cpNew" type="password" class="form-control" autocomplete="new-password">
       <div class="pw-rules mt-2" id="cpPwRules">
         <span class="pw-rule" data-rule="len"><i class="bi bi-circle"></i>8文字以上</span>
         <span class="pw-rule" data-rule="alpha"><i class="bi bi-circle"></i>英字を含む</span>
         <span class="pw-rule" data-rule="digit"><i class="bi bi-circle"></i>数字を含む</span>
       </div>
       <p class="small text-secondary mt-2 mb-0">変更すると、他の端末のログインは無効になります。</p>
       <div class="form-error" id="cpErr"></div>`,
      async (w, close) => {
        const err = w.querySelector('#cpErr');
        try {
          await api('/admin/password', { method: 'PUT', body: JSON.stringify({
            current_password: w.querySelector('#cpCur').value,
            new_password: w.querySelector('#cpNew').value }) });
          close(); toast('パスワードを変更しました', 'success');
        } catch (e) { if (err) err.textContent = e.message; }
      }, { saveLabel: '変更する' });
    wirePasswordRuleCheck('cpNew', 'cpPwRules');
  });
}

// 各所のパスワード入力欄で使っている「8文字以上・英字・数字」のリアルタイム表示を
// この画面の3箇所（管理者追加・パスワード変更）でも同じ見た目に揃えるための共通処理。
function wirePasswordRuleCheck(inputId, rulesId) {
  setTimeout(() => {
    const wrap = document.querySelector('.modal-overlay:last-child');
    if (!wrap) return;
    const pwInput = wrap.querySelector(`#${inputId}`);
    const ruleEls = wrap.querySelectorAll(`#${rulesId} .pw-rule`);
    if (!pwInput || !ruleEls.length) return;
    const updateRules = () => {
      const v = pwInput.value || '';
      const checks = { len: v.length >= 8, alpha: /[A-Za-z]/.test(v), digit: /[0-9]/.test(v) };
      ruleEls.forEach((el) => {
        const ok = checks[el.dataset.rule];
        el.classList.toggle('ok', !!ok && v.length > 0);
        el.classList.toggle('ng', !ok && v.length > 0);
        el.querySelector('i').className = ok ? 'bi bi-check-circle-fill' : 'bi bi-x-circle-fill';
      });
    };
    pwInput.addEventListener('input', updateRules);
  }, 50);
}

async function renderMigrationsTab(body) {
  const tok = navToken();
  body.innerHTML = card(sectionTitle('bi-database-gear', 'マイグレーション') +
    '<div id="migSummary" class="mb-2"></div><div id="migList"></div>' +
    '<button class="btn btn-primary btn-sm mt-3" id="migApplyBtn"><i class="bi bi-play-fill"></i> 未適用を適用</button>');
  const d = await api('/admin/migrations');
  if (!isAlive(tok)) return;
  const summary = document.getElementById('migSummary');
  if (!summary) return;
  summary.innerHTML = d.pending
    ? badge(`未適用 ${d.pending} 件`, 'warning')
    : badge('すべて適用済み', 'success');
  document.getElementById('migList').innerHTML =
    `<div class="table-wrap"><table class="data-table"><thead><tr><th>状態</th><th>ファイル</th><th class="t-num">#</th><th>SQL</th></tr></thead><tbody>` +
    d.migrations.map((m) => `<tr>
      <td>${badge(m.applied ? '適用済み' : '未適用', m.applied ? 'success' : 'warning')}</td>
      <td class="small">${esc(m.filename)}</td>
      <td class="t-num num">${m.stmt_index}</td>
      <td class="small">${esc((m.sql || '').slice(0, 80))}</td>
    </tr>`).join('') + '</tbody></table></div>';

  document.getElementById('migApplyBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-database-gear"></i> マイグレーションの適用',
      `<p class="mb-1">未適用のステートメントを順に実行します。</p>
       <p class="small text-secondary mb-0">失敗した時点で中断します。成功した分は記録されるため、再実行すれば失敗箇所から再開します。</p>`,
      async (w, close) => {
        try {
          const r = await api('/admin/migrations/apply', { method: 'POST' });
          close();
          if (r.failed) {
            toast(`${r.applied.length}件適用後、${r.failed.filename}#${r.failed.stmt_index} で失敗: ${r.failed.error}`, 'error');
          } else {
            toast(`${r.applied.length}件を適用しました`, 'success');
          }
          renderMigrationsTab(body);
        } catch (e) { toast(e.message, 'error'); }
      }, { saveLabel: '適用する' }));
}

async function renderDiagnosticTab(body) {
  const tok = navToken();
  body.innerHTML = card(sectionTitle('bi-clipboard-pulse', 'DB診断') + '<div id="diagBody">読み込み中…</div>');
  const d = await api('/admin/debug/db-schema');
  if (!isAlive(tok)) return;
  const target = document.getElementById('diagBody');
  if (!target) return;
  // キー名は admin_debug_db_schema (src/admin_api.py) の実装に合わせる
  // （supports_student_role / has_shop_holidays_table。student_supported 等ではない）。
  target.innerHTML =
    `<div class="mb-2">${badge(d.supports_student_role ? 'student ロール対応済み' : 'student ロール未対応', d.supports_student_role ? 'success' : 'warning')}
      ${badge(d.has_shop_holidays_table ? 'shop_holidays あり' : 'shop_holidays なし', d.has_shop_holidays_table ? 'success' : 'warning')}</div>
     <details><summary class="small text-secondary">技術詳細</summary><pre class="small" style="white-space:pre-wrap">${esc(JSON.stringify(d, null, 2))}</pre></details>`;
}
