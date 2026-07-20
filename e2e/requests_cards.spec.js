/**
 * e2e/requests_cards.spec.js — 希望表管理画面のカードUIをブラウザでテスト
 *
 * 要件:
 * 1. 名称が「希望表管理」に変わっていること
 * 2. スタッフごとにカード表示されること
 * 3. カードクリックで個人の希望表モーダルが開くこと
 * 4. 休希望/柔軟希望/時間指定が視覚的に区別できること
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `REQ_${RUN_ID}`,
  shopName: '希望表テスト店',
  managerCode: `mgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

test.describe('希望表管理（カードUI）', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('サイドメニューの表示が「希望表管理」に変更されている', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // サイドメニューに「希望表管理」がある
    await page.waitForSelector('button[data-screen="requests"]');
    const label = await page.locator('button[data-screen="requests"]').textContent();
    expect(label).toContain('希望表管理');
    // 念のため「希望休管理」が残っていないことも確認
    expect(label).not.toContain('希望休管理');
    expect(errors).toEqual([]);
  });

  test('カードUI: スタッフごとにカード表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    // 事前準備: スタッフと希望をAPI経由で作成
    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    const shopToken = (await loginRes.json()).token;
    const shopHdr = { Authorization: `Bearer ${shopToken}` };
    // スタッフ作成
    const staff1 = await request.post('/api/shop/staffs', {
      data: { staff_code: 'EMP1', name: '山田一郎', password: 'Emp1234a', role: 'employee' },
      headers: shopHdr,
    });
    const staff2 = await request.post('/api/shop/staffs', {
      data: { staff_code: 'PT1', name: '佐藤花子', password: 'Pt1234ab', role: 'part_time' },
      headers: shopHdr,
    });
    const sid1 = (await staff1.json()).id;
    const sid2 = (await staff2.json()).id;
    // 募集期間を作る
    await request.post('/api/shop/periods', {
      data: { start_date: '2026-08-01', end_date: '2026-08-31', deadline: '2099-12-31' },
      headers: shopHdr,
    });
    // 希望を出す（スタッフ1: 時間指定, スタッフ2: 休希望 + 柔軟）
    await request.post('/api/shop/shifts', {
      data: { staff_id: sid1, start_datetime: '2026-08-03T09:00:00', end_datetime: '2026-08-03T18:00:00', status: 'requested', reason: 'スタッフ希望' },
      headers: shopHdr,
    });
    await request.post('/api/shop/shifts', {
      data: { staff_id: sid2, start_datetime: '2026-08-04T00:00:00', end_datetime: '2026-08-04T23:59:59', status: 'requested', availability: 'rest', reason: '休希望' },
      headers: shopHdr,
    });
    await request.post('/api/shop/shifts', {
      data: { staff_id: sid2, start_datetime: '2026-08-05T09:00:00', end_datetime: '2026-08-05T22:00:00', status: 'requested', availability: 'any' },
      headers: shopHdr,
    });

    // ログインして希望表管理を開く
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="requests"]');
    await page.waitForSelector('#reqLoadBtn');
    await page.click('#reqLoadBtn');
    // カードが表示されるまで待つ
    await page.waitForSelector('.req-staff-card', { timeout: 10000 });
    // 2件以上のカードがあること
    const cardCount = await page.locator('.req-staff-card').count();
    expect(cardCount).toBeGreaterThanOrEqual(2);
    // 各カードに名前・件数が含まれる
    const card1Text = await page.locator('.req-staff-card').first().textContent();
    expect(card1Text).toContain('件');
    expect(errors).toEqual([]);
  });

  test('カードクリックで個人の希望表モーダルが開く', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="requests"]');
    await page.waitForSelector('#reqLoadBtn');
    await page.click('#reqLoadBtn');
    await page.waitForSelector('.req-staff-card', { timeout: 10000 });
    // 最初のカードをクリック
    await page.locator('.req-staff-card').first().click();
    // モーダルが開く
    await page.waitForSelector('.modal-overlay', { timeout: 5000 });
    await page.waitForSelector('text=さんの希望表');
    // テーブルが表示される
    await page.waitForSelector('.modal-overlay table.data-table');
    // 日付と時間が表示されている
    const modalText = await page.locator('.modal-overlay').textContent();
    expect(modalText).toContain('2026-08');  // テスト期間内の日付
    expect(errors).toEqual([]);
  });

  test('休希望/柔軟希望/時間指定のバッジが視覚的に区別される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="requests"]');
    await page.waitForSelector('#reqLoadBtn');
    await page.click('#reqLoadBtn');
    await page.waitForSelector('.req-staff-card', { timeout: 10000 });
    // カード内のバッジに「休希望」「柔軟」等が含まれる
    const allCardsText = await page.locator('.req-cards-grid').textContent();
    // テストデータ: スタッフ1=時間指定, スタッフ2=休希望+柔軟
    expect(allCardsText).toContain('休希望');
    expect(allCardsText).toContain('柔軟');  // または「時間」
    expect(errors).toEqual([]);
  });

  test('期間フィルタで切り替えられる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="requests"]');
    await page.waitForSelector('#reqStart');
    // 期間を未来（希望が無い期間）に変更
    await page.fill('#reqStart', '2099-01-01');
    await page.fill('#reqEnd', '2099-12-31');
    await page.click('#reqLoadBtn');
    // 「希望シフトはありません」等のメッセージが出る
    await page.waitForSelector('text=この期間の希望シフトはありません', { timeout: 5000 });
    expect(errors).toEqual([]);
  });
});
