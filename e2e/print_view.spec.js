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
const { ensureShop, loginAsManager } = require('./helpers');

const SHOP = {
  shopCode: 'PRINT1',
  shopName: '印刷テスト店',
  managerCode: 'mgr1',
  managerPassword: 'mgr1pass',
  managerName: '印刷店長',
};

test.beforeEach(async ({ page, request }) => {
  await ensureShop(request, SHOP);
  // window.print はダイアログを開いてテストを止めるので無害化する。
  // ただし beforeprint / afterprint は自前で dispatch して挙動を見る。
  await page.addInitScript(() => { window.print = () => {}; });
  await loginAsManager(page, {
    shopCode: SHOP.shopCode,
    managerCode: SHOP.managerCode,
    password: SHOP.managerPassword,
  });
  await page.click('.side-item[data-screen="shifts"]');
  await page.waitForSelector('#printBtn');
});

async function openPrint(page) {
  // 期間を1日に絞る（シフトが無くても print-page は日数分生成される）
  await page.fill('#sStart', '2026-08-03');
  await page.fill('#sEnd', '2026-08-03');
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
  // 画面用の .tl-row は min-width:480px を持つ。用紙幅がそれを下回ると、
  // .tl-wrap の overflow-x:auto があふれた分を切り捨てる（印刷では横方向に
  // ページ分割されないため、そのまま消える）。emulateMedia だけではレイアウト
  // 幅が実ビューポート(1280px)のままで 480px の制約に届かないので、
  // ビューポート自体を縮めて狭い用紙を再現する。
  await page.setViewportSize({ width: 400, height: 800 });
  await page.emulateMedia({ media: 'print' });

  const overflow = await page.evaluate(() => {
    const wrap = document.querySelector('#printView .tl-wrap');
    if (!wrap) return null;
    return { scrollWidth: wrap.scrollWidth, clientWidth: wrap.clientWidth };
  });
  expect(overflow).not.toBeNull();
  // 中身が枠に収まっていること(切り捨てが起きていない)
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);

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
