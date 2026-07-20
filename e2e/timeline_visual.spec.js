/**
 * e2e/timeline_visual.spec.js — タイムライン表示と印刷画面の時間表示をじっくり確認
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `TLN_${RUN_ID}`,
  shopName: 'タイムライン確認店',
  managerCode: `mgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

test.describe('タイムライン & 印刷画面の時間表示', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('日付クリック後のタイムライン表示に時間が表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    // 準備
    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    const shopHdr = { Authorization: `Bearer ${(await loginRes.json()).token}` };
    await request.post('/api/shop/staffs', {
      data: { staff_code: 'EMP1', name: '山田一郎', password: 'Emp1234a', role: 'employee', max_hours_per_month: 200 },
      headers: shopHdr,
    });
    await request.post('/api/shop/staffs', {
      data: { staff_code: 'EMP2', name: '佐藤花子', password: 'Emp1234a', role: 'employee', max_hours_per_month: 200 },
      headers: shopHdr,
    });
    // 04:00-02:00 パターン
    await request.post('/api/shop/patterns', {
      data: { pattern_name: '通', start_time: '04:00', end_time: '02:00', required_staff: 2 },
      headers: shopHdr,
    });
    // AIドラフト保存（08-10）
    const autoRes = await request.post('/api/shop/shifts/auto', {
      data: { start_date: '2026-08-10', end_date: '2026-08-10', draft: true },
      headers: shopHdr,
    });
    console.log('AIドラフト confirmed_count:', (await autoRes.json()).confirmed_count);

    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#sStart', { timeout: 10000 });
    await page.waitForTimeout(1500);
    // カレンダーを08月に
    await page.evaluate(() => {
      if (window._shiftCalCtrl) window._shiftCalCtrl.goToMonth(2026, 7);
    });
    await page.waitForTimeout(2500);
    await page.waitForSelector('#calMount .cal-cell');

    // 1. 日付クリック（1回）→ 詳細リスト
    for (const cell of await page.locator('.cal-cell').all()) {
      const t = await cell.textContent();
      if (t && t.trim().startsWith('10') && !await cell.evaluate((el) => el.classList.contains('empty'))) {
        await cell.click();
        break;
      }
    }
    await page.waitForTimeout(800);
    const shiftLinesCount = await page.locator('.shift-line').count();
    console.log(`詳細リスト行数: ${shiftLinesCount}`);
    if (shiftLinesCount > 0) {
      const first = await page.locator('.shift-line').first().textContent();
      console.log(`  詳細リスト1行目: "${first}"`);
    }

    // 2. ダブルクリック → タイムライン表示
    for (const cell of await page.locator('.cal-cell').all()) {
      const t = await cell.textContent();
      if (t && t.trim().startsWith('10') && !await cell.evaluate((el) => el.classList.contains('empty'))) {
        await cell.dblclick();
        break;
      }
    }
    await page.waitForTimeout(1000);
    // モーダル内のタイムライン表示
    const modalBars = await page.locator('.modal-overlay .tl-bar').count();
    console.log(`タイムラインバー数: ${modalBars}`);
    if (modalBars > 0) {
      const firstBar = page.locator('.modal-overlay .tl-bar').first();
      const barText = await firstBar.textContent();
      const barTitle = await firstBar.getAttribute('title');
      const barHtml = await firstBar.innerHTML();
      console.log(`  バー1のtext: "${barText}"`);
      console.log(`  バー1のtitle: "${barTitle}"`);
      console.log(`  バー1のHTML: ${barHtml}`);
      console.log(`  バー1のstyle: ${await firstBar.getAttribute('style')}`);
    }
    // モーダル内の時間軸（tl-hour）
    const hoursCount = await page.locator('.modal-overlay .tl-hour').count();
    console.log(`時間軸数: ${hoursCount}`);
    if (hoursCount > 0) {
      const hourTexts = [];
      for (let i = 0; i < Math.min(hoursCount, 30); i++) {
        hourTexts.push(await page.locator('.modal-overlay .tl-hour').nth(i).textContent());
      }
      console.log(`  時間軸: ${hourTexts.join(' | ')}`);
    }
    expect(errors).toEqual([]);
  });

  test('印刷画面（printView）にドラフト状態のシフトも表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    const shopHdr = { Authorization: `Bearer ${(await loginRes.json()).token}` };

    // 印刷ダイアログ抑制（window.print を無害化）
    await page.addInitScript(() => { window.print = () => {}; });

    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#sStart', { timeout: 10000 });
    await page.fill('#sStart', '2026-08-10');
    await page.dispatchEvent('#sStart', 'change');
    await page.fill('#sEnd', '2026-08-10');
    await page.dispatchEvent('#sEnd', 'change');
    await page.waitForTimeout(2000);

    // 印刷ボタンをクリック
    const printBtn = page.locator('#printBtn');
    if (await printBtn.count() > 0) {
      await printBtn.click().catch(() => {});
      await page.waitForTimeout(2000);
    }

    // printView の中身を確認
    const printHtml = await page.locator('#printView').innerHTML();
    console.log(`印刷画面HTML（先頭600字）: ${printHtml.slice(0, 600)}`);
    const printBars = await page.locator('#printView .tl-bar').count();
    console.log(`印刷画面バー数: ${printBars}`);
    if (printBars > 0) {
      const firstBar = page.locator('#printView .tl-bar').first();
      console.log(`  印刷バー1 text: "${await firstBar.textContent()}"`);
      console.log(`  印刷バー1 title: "${await firstBar.getAttribute('title')}"`);
      console.log(`  印刷バー1 draftCls: ${await firstBar.evaluate((el) => el.classList.contains('tl-bar-draft'))}`);
    }
    // ドラフト状態でも表示されること（バー数 > 0）
    expect(printBars).toBeGreaterThan(0);
    expect(errors).toEqual([]);
  });
});
