/**
 * e2e/print_view.spec.js — 印刷ビューの回帰テスト。
 *
 * 既存の timeline_visual.spec.js は window.print を潰しているため
 * afterprint が一切発火せず、「2回目の印刷が白紙になる」バグを構造的に
 * 検出できなかった。ここでは print イベントを自前で dispatch して
 * 印刷用DOMの生存を直接検証する。
 *
 * 実行: npx playwright test e2e/print_view.spec.js
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, openShiftMoreActions } = require('./helpers');

const SHOP = {
  shopCode: 'PRINT1',
  shopName: '印刷テスト店',
  managerCode: 'mgr1',
  managerPassword: 'mgr1pass',
  managerName: '印刷店長',
};

const PRINT_DAY = '2026-08-03';

/**
 * 印刷対象日（PRINT_DAY）にシフトを1件用意する。
 *
 * この店舗はシフトが無いと buildStaticTimelineHtml()（public/app.js の
 * !list.length 分岐、1125行目付近）に入り、印刷ページには .tl-axis-row と
 * .print-empty しか出ない。.tl-row（シフト行本体）が1つも存在しないと、
 * 「用紙幅が狭くてもタイムラインが横にはみ出さない」テストが実質的に軸行
 * しか検証できていなかったため、実データを仕込む。
 *
 * beforeEach から毎回呼ばれるが、スタッフ・シフトの作成 API は重複時に
 * 400 を返すだけで副作用は無い。ensureShop と同じ「既にあれば無視」の
 * 作法にそろえ、初回作成分をそのまま使い回す。
 */
async function ensurePrintableShift(request) {
  const loginRes = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const shopHdr = { Authorization: `Bearer ${(await loginRes.json()).token}` };

  let staffId;
  const staffRes = await request.post('/api/shop/staffs', {
    data: { staff_code: 'PRTSTF', name: '印刷確認太郎', password: 'PrintStf1', role: 'employee' },
    headers: shopHdr,
  });
  if (staffRes.ok()) {
    staffId = (await staffRes.json()).id;
  } else {
    // 2回目以降はコード重複で 400 になるので、既存の id を取り直す。
    const list = await (await request.get('/api/shop/staffs', { headers: shopHdr })).json();
    staffId = (list.staffs || []).find((s) => s.staff_code === 'PRTSTF')?.id;
  }

  // 2回目以降は同一スタッフ・同一日への重複配置で overlap 400 になるだけ
  // なので無視してよい（1回目に作った行がそのまま使われる）。
  await request.post('/api/shop/shifts', {
    data: { staff_id: staffId, start_datetime: `${PRINT_DAY}T09:00:00`, end_datetime: `${PRINT_DAY}T17:00:00` },
    headers: shopHdr,
  });
}

test.beforeEach(async ({ page, request }) => {
  await ensureShop(request, SHOP);
  await ensurePrintableShift(request);
  // window.print はダイアログを開いてテストを止めるので無害化する。
  // ただし beforeprint / afterprint は自前で dispatch して挙動を見る。
  await page.addInitScript(() => { window.print = () => {}; });
  await loginAsManager(page, {
    shopCode: SHOP.shopCode,
    managerCode: SHOP.managerCode,
    password: SHOP.managerPassword,
  });
  await page.click('.side-item[data-screen="shifts"]');
  // 印刷・用紙の向きは工程バー化で「その他の操作」に畳んだので、開いてから触る。
  await openShiftMoreActions(page);
});

async function openPrint(page) {
  // 期間を1日に絞る（シフトが無くても print-page は日数分生成される）
  await page.fill('#sStart', PRINT_DAY);
  await page.fill('#sEnd', PRINT_DAY);
  await page.click('#printBtn');
  await page.waitForFunction(() => {
    const pv = document.getElementById('printView');
    return pv && pv.querySelectorAll('.print-page').length > 0;
  }, { timeout: 10000 });
}

test('印刷ボタンで印刷用DOMが組み立てられる', async ({ page }) => {
  await openPrint(page);
  await expect(page.locator('#printView .print-page')).toHaveCount(1);
});

test('afterprint の後もプレビュー再描画で内容が残る', async ({ page }) => {
  await openPrint(page);

  // ブラウザの印刷プレビューは向き・用紙・倍率を変えるたびにライブDOMから
  // 再レンダリングする。afterprint で消してしまうとその瞬間に白紙になる。
  // ただし afterprint → beforeprint の順で dispatch するため、このテスト単体
  // では「afterprint で破棄しない」ことまでは検出できない（beforeprint の
  // 復元経路が直後に効いてしまい、破棄する実装が再導入されても緑になり得る）。
  // 破棄そのものの検出は次の「beforeprint が発火しない再描画でも内容が残る」
  // テストが担う。このテストは beforeprint による復元経路自体を見る。
  await page.evaluate(() => window.dispatchEvent(new Event('afterprint')));
  await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));

  const count = await page.locator('#printView .print-page').count();
  expect(count).toBe(1);
});

test('beforeprint が発火しない再描画でも内容が残る', async ({ page }) => {
  await openPrint(page);

  // beforeprint を伴わない再描画（Chrome のプレビュー再生成など）を模す
  await page.evaluate(() => window.dispatchEvent(new Event('afterprint')));

  const count = await page.locator('#printView .print-page').count();
  expect(count).toBe(1);
});

test('印刷メディアで印刷ビューが表示され画面アプリが隠れる', async ({ page }) => {
  await openPrint(page);
  await page.emulateMedia({ media: 'print' });

  await expect(page.locator('#printView')).toBeVisible();
  await expect(page.locator('#appView')).toBeHidden();

  await page.emulateMedia({ media: 'screen' });
});

test('ログアウトで印刷用DOMが消える（前ユーザーのシフトを残さない）', async ({ page }) => {
  // #printView は afterprint で消えなくなったため、ログアウトを跨いで残ると
  // 共有端末でログイン画面から Ctrl+P したときに前ユーザーのシフトが
  // 印刷されてしまう。ログアウト時に明示的に破棄されることを確認する。
  await openPrint(page);
  await expect(page.locator('#printView .print-page')).toHaveCount(1);

  page.on('dialog', (d) => d.accept());
  await page.click('#logoutBtn');
  await page.waitForSelector('#loginView:not(.d-none)', { timeout: 10000 });

  const html = await page.evaluate(() => document.getElementById('printView').innerHTML);
  expect(html.trim()).toBe('');
});

test('印刷ボタンを押していない状態の Ctrl+P でも白紙にならない', async ({ page }) => {
  // openPrint() を呼ばず、印刷ボタンを一度も押していない状態で beforeprint を
  // 発火させる（Ctrl+P やシステムダイアログからの印刷を模す）。payload が
  // 無いので、白紙の代わりに案内ページが入ることを確認する。
  await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));

  await expect(page.locator('#printView .print-page')).toHaveCount(1);
  await expect(page.getByText('シフト表を印刷するには')).toHaveCount(1);
});

test('用紙幅が狭くてもタイムラインが横にはみ出さない', async ({ page }) => {
  await openPrint(page);
  // 画面用の .tl-row / .tl-axis-row は既定で min-width:480px を持つが、
  // ビューポート400pxではそれより先に @media (max-width: 575px)（style.css
  // 919-921行目付近）が効き、min-width は 420px になる。用紙幅がこれを
  // 下回ると、.tl-wrap の overflow-x:auto があふれた分を切り捨てる（印刷では
  // 横方向にページ分割されないため、そのまま消える）。emulateMedia だけでは
  // レイアウト幅が実ビューポート(1280px)のままで min-width の制約に届かない
  // ので、ビューポート自体を縮めて狭い用紙を再現する。
  //
  // beforeEach で1件シフトを作っているため、.print-empty しか出ない
  // シフト0件の状態と違い、実際のシフト行 .tl-row でも検証できる
  // （軸行 .tl-axis-row だけでは .tl-row 側の min-width 解除漏れを見逃す）。
  await page.setViewportSize({ width: 400, height: 800 });
  await page.emulateMedia({ media: 'print' });

  const overflow = await page.evaluate(() => {
    const wrap = document.querySelector('#printView .tl-wrap');
    const row = document.querySelector('#printView .tl-row');
    if (!wrap || !row) return null;
    return {
      wrapScrollWidth: wrap.scrollWidth,
      wrapClientWidth: wrap.clientWidth,
      rowScrollWidth: row.scrollWidth,
    };
  });
  expect(overflow).not.toBeNull();
  // 中身が枠に収まっていること(切り捨てが起きていない)
  expect(overflow.wrapScrollWidth).toBeLessThanOrEqual(overflow.wrapClientWidth + 1);
  // .tl-row 自身も枠(wrap)の可視幅に収まっていること。
  // .tl-row は自分の内部に overflow を持たないため row.scrollWidth と
  // row.clientWidth を比べても常に一致してしまい、min-width 解除漏れを
  // 検出できない（row が可視幅より広く forced されていても、row 自身の
  // 「中身」はその forced 幅にきっちり収まって見えるため）。row が
  // wrap の可視幅からはみ出していないかを見るため、row.scrollWidth を
  // wrap.clientWidth と比較する。
  expect(overflow.rowScrollWidth).toBeLessThanOrEqual(overflow.wrapClientWidth + 1);

  await page.emulateMedia({ media: 'screen' });
  await page.setViewportSize({ width: 1280, height: 800 });
});

test('案内ページの後に印刷ボタンを押すと本物の印刷内容に差し替わる', async ({ page }) => {
  // 案内ページ（目印 data-print-placeholder 付き）を先に出してから、
  // 印刷ボタンで本物の内容が上書きされ、案内ページが残らないことを確認する。
  await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));
  await expect(page.getByText('シフト表を印刷するには')).toHaveCount(1);

  await openPrint(page);

  await expect(page.getByText('シフト表を印刷するには')).toHaveCount(0);
  await expect(page.locator('#printView .print-page')).toHaveCount(1);
  const placeholderFlag = await page.evaluate(
    () => document.getElementById('printView').dataset.printPlaceholder
  );
  expect(placeholderFlag).toBeUndefined();
});

test('用紙の向きを切り替えられる', async ({ page }) => {
  // 既定は横
  await expect(page.locator('#printOrientLabel')).toHaveText('横');

  await page.click('#printOrientBtn');
  await expect(page.locator('#printOrientLabel')).toHaveText('縦');

  const rule = await page.evaluate(() => {
    const st = document.getElementById('printPageRule');
    return st ? st.textContent : null;
  });
  expect(rule).toContain('A4 portrait');

  const orient = await page.getAttribute('#printView', 'data-orientation');
  expect(orient).toBe('portrait');
});

test('選んだ向きは再読み込み後も保たれる', async ({ page }) => {
  await page.click('#printOrientBtn');
  await expect(page.locator('#printOrientLabel')).toHaveText('縦');

  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="shifts"]');
  await openShiftMoreActions(page);

  await expect(page.locator('#printOrientLabel')).toHaveText('縦');
});

test('localStorage に保存できない環境でも向きを切り替えられる', async ({ page }) => {
  // プライベートモード等で setItem が例外を投げる状況を再現する。
  // 実行中の選択値を localStorage に依存させていると、ここでトグルが無反応になる。
  await page.addInitScript(() => {
    const orig = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      if (k === 'shift_print_orientation') throw new Error('QuotaExceededError');
      return orig.call(this, k, v);
    };
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="shifts"]');
  await openShiftMoreActions(page);

  await expect(page.locator('#printOrientLabel')).toHaveText('横');
  await page.click('#printOrientBtn');
  await expect(page.locator('#printOrientLabel')).toHaveText('縦');

  const rule = await page.evaluate(() => document.getElementById('printPageRule')?.textContent);
  expect(rule).toContain('A4 portrait');
});
