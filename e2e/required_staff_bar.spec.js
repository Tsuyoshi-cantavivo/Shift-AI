/**
 * e2e/required_staff_bar.spec.js — 必要人数のバーUI。
 *
 * 実行: npx playwright test e2e/required_staff_bar.spec.js
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager } = require('./helpers');

const SHOP = {
  shopCode: 'REQ1',
  shopName: '必要人数テスト店',
  managerCode: 'mgr1',
  managerPassword: 'mgr1pass',
  managerName: '必要人数店長',
};

/** 時間帯パターンを2件仕込む。バーが2本描かれる状態を作る。 */
async function seedPatterns(request, token) {
  for (const p of [
    { pattern_name: '早番', start_time: '09:00', end_time: '17:00', required_staff: 2 },
    { pattern_name: '夜番', start_time: '17:00', end_time: '22:00', required_staff: 3 },
  ]) {
    await request.post('/api/shop/patterns', {
      headers: { Authorization: `Bearer ${token}` },
      data: p,
    });
  }
}

/** SHOP は全テストで shopCode を共有しているため、既存パターンを消してから
 *  仕込まないと「バーが2本」の前提がテスト実行順に依存して崩れる
 *  （前のテストの beforeEach が作った早番/夜番が積み上がり、
 *  data-name="早番" が複数ヒットして strict mode violation になる）。 */
async function clearPatterns(request, token) {
  const d = await (await request.get('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
  })).json();
  for (const p of d.patterns || []) {
    await request.delete(`/api/shop/patterns/${p.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }
}

test.beforeEach(async ({ page, request }) => {
  await ensureShop(request, SHOP);
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await clearPatterns(request, token);
  await seedPatterns(request, token);

  await loginAsManager(page, {
    shopCode: SHOP.shopCode,
    managerCode: SHOP.managerCode,
    password: SHOP.managerPassword,
  });
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');
});

test('時間帯がバーとして描画される', async ({ page }) => {
  await expect(page.locator('.rq-bar')).toHaveCount(2);
  await expect(page.locator('.rq-bar[data-name="早番"]')).toBeVisible();
});

test('バーの高さが必要人数に比例する', async ({ page }) => {
  const heights = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).getBoundingClientRect().height;
    return { asa: get('早番'), yoru: get('夜番') };
  });
  // 早番2人 / 夜番3人 → 夜番のほうが高い
  expect(heights.yoru).toBeGreaterThan(heights.asa);
});

test('曜日タブを切り替えられる', async ({ page }) => {
  await expect(page.locator('#reqBarTabs .rq-tab')).toHaveCount(8);  // 基本 + 日〜土
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  await expect(page.locator('#reqBarTabs .rq-tab[data-wd="6"]')).toHaveClass(/active/);
});

test('曜日別の人数が未設定なら基本値が表示される', async ({ page }) => {
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  const v = await page.inputValue('.rq-count[data-name="早番"]');
  expect(v).toBe('2');
  // 上書きではないことがクラスで分かる
  await expect(page.locator('.rq-bar[data-name="早番"]')).not.toHaveClass(/rq-override/);
});

test('時間帯が0件のとき案内が出る', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  const d = await (await request.get('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
  })).json();
  for (const p of d.patterns) {
    await request.delete(`/api/shop/patterns/${p.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await expect(page.locator('#reqBarEmpty')).toBeVisible();
});
