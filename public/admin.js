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

SCREENS.adminHome = function (el) {
  el.innerHTML = pageHead('システム管理者', 'bi-shield-lock', currentUser.name) +
    card(`<button class="btn btn-primary btn-lg w-full mb-2" id="goShops"><i class="bi bi-shop"></i> 店舗一覧へ</button>
      <button class="btn btn-light btn-lg w-full" id="dbMaintBtn"><i class="bi bi-database-check"></i> データベース状態確認・更新</button>`);
  document.getElementById('goShops')?.addEventListener('click', () => navigateTo('adminShops'));
  document.getElementById('dbMaintBtn')?.addEventListener('click', () => openDbMaintenanceModal());
};

function openDbMaintenanceModal() {
  const w = openModal('<i class="bi bi-database-check"></i> データベース状態確認・更新',
    `<div id="dbStatus"><div class="text-secondary small">確認中...</div></div>`,
    null, { saveLabel: '閉じる' });
  // 状態取得
  api('/admin/debug/db-schema').then((d) => {
    const box = w.querySelector('#dbStatus');
    if (!box) return;
    const student = d.supports_student_role;
    const holidays = d.has_shop_holidays_table;
    const allOk = student && holidays;
    safeSetHTML(box, `
      <div class="${allOk ? 'info-box' : 'info-box'}" style="border-color:${allOk ? 'var(--success)' : 'var(--danger)'}">
        <div class="mb-2"><strong>現在のデータベース状態</strong></div>
        <div>• student ロール対応: ${student ? '<span class="text-success">✓ 対応済み</span>' : '<span class="text-danger">✗ 未対応（要更新）</span>'}</div>
        <div>• shop_holidays テーブル: ${holidays ? '<span class="text-success">✓ 存在</span>' : '<span class="text-danger">✗ 未作成</span>'}</div>
      </div>
      ${!allOk ? `
        <div class="alert alert-warning mt-3">
          <strong>⚠ データベースが古い状態です。</strong><br>
          新機能（学生アルバイト・祝日機能等）が使えません。<br>
          下の「スキーマを最新化」ボタンで修正できます。
        </div>
        <button class="btn btn-primary w-full mt-2" id="runMigrationBtn"><i class="bi bi-arrow-repeat"></i> スキーマを最新化する</button>
        <div id="migrationResult" class="mt-2"></div>
      ` : `
        <div class="alert alert-success mt-3">
          ✓ データベースは最新です。追加の操作は不要です。
        </div>
      `}
      <details class="mt-3">
        <summary class="small text-secondary cursor-pointer">技術詳細</summary>
        <pre class="small mt-2" style="white-space:pre-wrap;word-break:break-all">${esc(d.staffs_schema || '')}</pre>
        <div class="small mt-2">role 分布:</div>
        <pre class="small">${esc(JSON.stringify(d.role_distribution, null, 2))}</pre>
      </details>`);
    const btn = w.querySelector('#runMigrationBtn');
    if (btn) {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.innerHTML = '<i class="bi bi-hourglass-split"></i> 実行中...';
        const resultBox = w.querySelector('#migrationResult');
        try {
          const r = await api('/admin/db/migrate', { method: 'POST', body: JSON.stringify({}) });
          if (resultBox) {
            safeSetHTML(resultBox, `
              <div class="alert alert-success">
                <strong>✓ マイグレーション完了</strong>
                <pre class="small mt-2" style="white-space:pre-wrap">${esc((r.log || []).join('\n'))}</pre>
              </div>`);
          }
          toast('データベースを最新化しました', 'success');
        } catch (e) {
          if (resultBox) {
            safeSetHTML(resultBox, `<div class="alert alert-danger">エラー: ${esc(e.message)}</div>`);
          }
          toast(e.message, 'error');
        }
      });
    }
  }).catch((e) => {
    const box = w.querySelector('#dbStatus');
    if (box) safeSetHTML(box, `<div class="text-danger small">${esc(e.message)}</div>`);
  });
}
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
    document.getElementById('shopList').querySelectorAll('[data-toggle]').forEach((b) => b?.addEventListener('click', async (ev) => { ev.stopPropagation(); await api(`/admin/shops/${b.dataset.toggle}`, { method: 'PUT', body: JSON.stringify({ is_active: b.dataset.active !== '1' }) }); load(); }));
  };
  load();
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
SCREENS.adminShopDetail = async function (el) {
  const sid = window._adminShopId;
  const shop = (await api('/admin/shops')).shops.find((x) => x.id === sid) || { shop_name: '店舗#' + sid, shop_code: '' };
  el.innerHTML = pageHead(shop.shop_name, 'bi-shop', shop.shop_code) +
    card(`<button class="btn btn-sm btn-light mb-2" id="backBtn"><i class="bi bi-arrow-left"></i> 戻る</button>
      <div class="flex gap-2 flex-wrap mb-3">
        <button class="btn btn-primary btn-sm" id="addStaffBtn"><i class="bi bi-person-plus"></i> スタッフ追加</button>
        <button class="btn btn-light btn-sm" id="migrateBtn" title="旧仕様店主のPWを引き継いで manager スタッフを作成"><i class="bi bi-arrow-up-circle"></i> 旧仕様から manager 昇格</button>
        <button class="btn btn-sm btn-light" id="impersonateBtn"><i class="bi bi-eye"></i> この店舗を代理閲覧</button>
      </div>
      <div class="row mb-3"><div class="col-5"><label class="form-label" for="dStart">開始</label><input type="date"  id="dStart" class="form-control"></div><div class="col-5"><label class="form-label" for="dEnd">終了</label><input type="date"  id="dEnd" class="form-control"></div><div class="col-2 flex items-end"><button class="btn btn-primary w-full" id="loadBtn">表示</button></div></div>
      <div id="detailBody"><div class="text-secondary small">期間を指定してください</div></div>`);
  document.getElementById('backBtn')?.addEventListener('click', () => navigateTo('adminShops'));
  document.getElementById('loadBtn')?.addEventListener('click', () => loadDetail());
  document.getElementById('addStaffBtn')?.addEventListener('click', () => openAdminAddStaffModal(sid, loadDetail));
  document.getElementById('migrateBtn')?.addEventListener('click', () => openAdminMigrateModal(sid, shop, loadDetail));
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
  api(`/admin/shops/${sid}/periods/next`).then((p) => {
    const ds = document.getElementById('dStart'); const de = document.getElementById('dEnd');
    if (!ds || !de) return;  // 画面遷移済み
    ds.value = p.start_date; de.value = p.end_date; loadDetail();
  }).catch(() => {});
  async function loadDetail() {
    const start = dStart.value, end = dEnd.value; if (!start || !end) return;
    const body = document.getElementById('detailBody');
    if (!body) return;  // 画面遷移済み → 更新中止
    const tok = navToken();
    body.innerHTML = '<div class="text-secondary small">読み込み中...</div>';
    try {
      const [sum, st] = await Promise.all([api(`/admin/shops/summary/${sid}?start=${start}&end=${end}`), api(`/admin/shops/staffs/${sid}`)]);
      if (!isAlive(tok) || !body.isConnected) return;  // 画面遷移済み
      const tbl = sum.staff.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>氏名</th><th>日</th><th class="t-num">確定</th><th class="t-num">給与</th></tr></thead><tbody>${sum.staff.map((s) => `<tr><td>${esc(s.name)}</td><td>${s.days}</td><td class="t-num num">${s.confirmed_hours}h</td><td class="t-num num">${yen(s.pay)}</td></tr>`).join('')}<tr style="font-weight:800;color:var(--ink)"><td>合計</td><td></td><td class="t-num num">${sum.total_hours}h</td><td class="t-num num">${yen(sum.total_pay)}</td></tr></tbody></table></div>` : '<div class="small text-secondary">確定シフトなし</div>';
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
      body.innerHTML = sectionTitle('bi-people', `スタッフ（${st.staffs.length}名）`) + searchBox + `<div id="staffListBox">${slist}</div>` + `<hr style="border-color:var(--rule);margin:16px 0">` + sectionTitle('bi-bar-chart', `集計（${start}〜${end}）`) + tbl;
      // スタッフ検索（フロント側フィルタ）
      document.getElementById('staffSearch')?.addEventListener('input', (ev) => {
        const q = ev.target.value.trim().toLowerCase();
        body.querySelectorAll('[data-staff-row]').forEach((row) => {
          row.style.display = (!q || (row.dataset.search || '').toLowerCase().includes(q)) ? '' : 'none';
        });
      });
      // 汎用編集ボタン
      body.querySelectorAll('[data-staff-edit]').forEach((b) => b?.addEventListener('click', () => {
        let s2; try { s2 = JSON.parse(b.dataset.staffEdit); } catch { return; }
        openAdminStaffEditModal(sid, s2, loadDetail);
      }));
      // ロール変更ボタン
      body.querySelectorAll('[data-role-edit]').forEach((b) => b?.addEventListener('click', () => {
        openAdminRoleModal(sid, +b.dataset.roleEdit, b.dataset.name, b.dataset.role, loadDetail);
      }));
      // パスワードリセットボタン
      body.querySelectorAll('[data-pw-reset]').forEach((b) => b?.addEventListener('click', () => {
        openAdminPwResetModal(sid, +b.dataset.pwReset, b.dataset.name);
      }));
    } catch (e) {
      if (!isAlive(tok) || !body.isConnected) return;
      body.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
    }
  }
};

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
  'auth.login': 'ログイン',
  'auth.login_failed': 'ログイン失敗',
  'auth.login_blocked': 'ログインブロック',
  'auth.logout': 'ログアウト',
  'admin.password_change': '管理者PW変更',
  'admin.impersonate_start': '代理閲覧開始',
  'admin.impersonate_end': '代理閲覧終了',
  'admin.create': '管理者追加',
  'admin.delete': '管理者削除',
};
function auditActionLabel(a) { return AUDIT_ACTION_LABELS[a] || a || '—'; }

SCREENS.adminAudit = async function (el) {
  el.innerHTML = pageHead('監査ログ', 'bi-clipboard-data', '重要操作の履歴') +
    card(sectionTitle('bi-funnel', 'フィルタ') +
      `<div class="row mb-2">
         <div class="col-6"><label class="form-label" for="auShop">店舗</label><select id="auShop" class="form-select"><option value="">すべて</option></select></div>
         <div class="col-6"><label class="form-label" for="auAction">操作</label><select id="auAction" class="form-select">
           <option value="">すべて</option>${Object.keys(AUDIT_ACTION_LABELS).map((k) => `<option value="${k}">${esc(AUDIT_ACTION_LABELS[k])}</option>`).join('')}
         </select></div>
       </div>
       <button class="btn btn-primary btn-sm" id="auLoad"><i class="bi bi-search"></i> 表示</button>`) +
    card(`<div id="auBody"><div class="text-secondary small">「表示」を押してください</div></div>`);
  // 店舗フィルタの選択肢
  try {
    const d = await api('/admin/shops');
    const sel = document.getElementById('auShop');
    if (sel) sel.innerHTML = '<option value="">すべて</option>' + (d.shops || []).map((s) => `<option value="${s.id}">${esc(s.shop_name)}</option>`).join('');
  } catch {}
  async function load() {
    const body = document.getElementById('auBody');
    if (!body) return;
    const tok = navToken();
    body.innerHTML = '<div class="text-secondary small">読み込み中...</div>';
    const shop = document.getElementById('auShop').value;
    const action = document.getElementById('auAction').value;
    const qs = new URLSearchParams();
    if (shop) qs.set('shop', shop);
    if (action) qs.set('action', action);
    qs.set('limit', '200');
    try {
      const r = await api('/admin/audit-logs?' + qs.toString());
      if (!isAlive(tok) || !body.isConnected) return;
      const logs = r.logs || [];
      if (!logs.length) { body.innerHTML = '<div class="small text-secondary">該当するログはありません</div>'; return; }
      body.innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>日時</th><th>操作者</th><th>操作</th><th>対象</th><th>詳細</th></tr></thead><tbody>${logs.map((l) => `
        <tr>
          <td class="small">${esc((l.created_at || '').replace('T', ' '))}</td>
          <td class="small">${esc(l.actor_name || l.actor_role || '—')}</td>
          <td>${badge(auditActionLabel(l.action), l.action && l.action.indexOf('reject') >= 0 ? 'warning' : 'info')}</td>
          <td class="small">${esc(l.target_type || '')}${l.target_id != null ? ' #' + l.target_id : ''}</td>
          <td class="small text-secondary">${esc(l.detail || '')}</td>
        </tr>`).join('')}</tbody></table></div>`;
    } catch (e) {
      if (!isAlive(tok) || !body.isConnected) return;
      body.innerHTML = `<div class="text-danger small">${esc(e.message)}</div>`;
    }
  }
  document.getElementById('auLoad')?.addEventListener('click', load);
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
