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
