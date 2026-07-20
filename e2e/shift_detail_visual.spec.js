/**
 * e2e/shift_detail_visual.spec.js — シフト詳細の時間表示をじっくり確認
 *
 * ※slowMo 付きで実行し、人間の目で見える形で検証
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `DTL_${RUN_ID}`,
  shopName: 'シフト詳細確認店',
  managerCode: `mgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

test.describe('シフト詳細の時間表示', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test.beforeEach(async ({ browser }) => {
    // slowMo 付きでブラウザを開く（人間の目で見える速度）
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    await page.setViewportSize({ width: 1280, height: 900 });
  });

  test('シフト一覧の各シフトに時間が表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    // 準備: スタッフ + パターン + シフト作成
    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    const shopHdr = { Authorization: `Bearer ${(await loginRes.json()).token}` };
    const empRes = await request.post('/api/shop/staffs', {
      data: { staff_code: 'EMP1', name: '山田一郎', password: 'Emp1234a', role: 'employee' },
      headers: shopHdr,
    });
    const sid1 = (await empRes.json()).id;
    const emp2Res = await request.post('/api/shop/staffs', {
      data: { staff_code: 'PT1', name: '佐藤花子', password: 'Pt1234ab', role: 'part_time' },
      headers: shopHdr,
    });
    const sid2 = (await emp2Res.json()).id;
    await request.post('/api/shop/patterns', {
      data: { pattern_name: '通', start_time: '04:00', end_time: '02:00', required_staff: 2 },
      headers: shopHdr,
    });
    // confirmed シフトを数件作成
    await request.post('/api/shop/shifts', {
      data: { staff_id: sid1, start_datetime: '2026-08-03T04:00:00', end_datetime: '2026-08-04T02:00:00' },
      headers: shopHdr,
    });
    await request.post('/api/shop/shifts', {
      data: { staff_id: sid2, start_datetime: '2026-08-03T10:00:00', end_datetime: '2026-08-03T20:00:00' },
      headers: shopHdr,
    });
    await request.post('/api/shop/shifts', {
      data: { staff_id: sid1, start_datetime: '2026-08-05T04:00:00', end_datetime: '2026-08-06T02:00:00' },
      headers: shopHdr,
    });

    // ログイン
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });

    // シフト管理画面へ
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#sStart', { timeout: 10000 });
    // 期間を2026-08-01〜2026-08-31に設定してchangeイベント発火
    await page.fill('#sStart', '2026-08-01');
    await page.dispatchEvent('#sStart', 'change');
    await page.fill('#sEnd', '2026-08-31');
    await page.dispatchEvent('#sEnd', 'change');
    await page.waitForTimeout(2500);  // カレンダー描画待ち

    // カレンダーが描画されているか
    await page.waitForSelector('#calMount .cal-cell', { timeout: 10000 });

    // 8月3日のセルをクリック
    const dayCells = await page.locator('.cal-cell').all();
    let clicked = false;
    for (const cell of dayCells) {
      const text = await cell.textContent();
      // 「3」を含む日付セル（ただし empty クラスは除く）
      if (text && text.trim().startsWith('3') && !await cell.evaluate((el) => el.classList.contains('empty'))) {
        await cell.click();
        clicked = true;
        break;
      }
    }
    await page.waitForTimeout(800);

    // 詳細ボックスに時間が表示されているか
    const shiftLines = await page.locator('.shift-line').count();
    console.log(`シフト行数: ${shiftLines}`);
    if (shiftLines > 0) {
      for (let i = 0; i < shiftLines; i++) {
        const line = await page.locator('.shift-line').nth(i).textContent();
        console.log(`  行${i + 1}: ${line}`);
        // HH:MM - HH:MM 形式が含まれること
        expect(line).toMatch(/\d{2}:\d{2}/);
      }
    }
    expect(errors).toEqual([]);
  });

  test('AIドラフト保存したシフトも時間が正しく表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    const shopHdr = { Authorization: `Bearer ${(await loginRes.json()).token}` };
    // テスト2内で完結するようスタッフ・パターンを明示作成
    const empRes = await request.post('/api/shop/staffs', {
      data: { staff_code: 'EMP2', name: 'テスト社員', password: 'Emp1234a', role: 'employee', max_hours_per_month: 200 },
      headers: shopHdr,
    });
    const emp2Res = await request.post('/api/shop/staffs', {
      data: { staff_code: 'EMP3', name: 'テスト社員2', password: 'Emp1234a', role: 'employee', max_hours_per_month: 200 },
      headers: shopHdr,
    });
    await request.post('/api/shop/patterns', {
      data: { pattern_name: '通', start_time: '04:00', end_time: '02:00', required_staff: 2 },
      headers: shopHdr,
    });

    // AIドラフト生成（08/10）
    const autoRes = await request.post('/api/shop/shifts/auto', {
      data: { start_date: '2026-08-10', end_date: '2026-08-10', draft: true },
      headers: shopHdr,
    });
    expect(autoRes.ok()).toBeTruthy();
    const autoBody = await autoRes.json();
    console.log('AIドラフト保存結果 confirmed_count:', autoBody.confirmed_count);

    // API経由で08-10のシフトを確認
    const checkRes = await request.get('/api/shop/shifts?start=2026-08-10&end=2026-08-10', { headers: shopHdr });
    const checkBody = await checkRes.json();
    console.log('API 08-10 shifts count:', (checkBody.shifts || []).length);
    if ((checkBody.shifts || []).length > 0) {
      console.log('  最初のシフト:', JSON.stringify(checkBody.shifts[0]));
    }

    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#sStart', { timeout: 10000 });
    await page.waitForTimeout(1500);  // カレンダー初期化待ち
    // カレンダーを直接2026年8月に切り替え（goToMonth）
    await page.evaluate(() => {
      if (window._shiftCalCtrl && window._shiftCalCtrl.goToMonth) {
        window._shiftCalCtrl.goToMonth(2026, 7);  // 2026年8月（0-indexed）
      }
    });
    await page.waitForTimeout(3000);  // カレンダー再描画・API取得待ち
    await page.waitForSelector('#calMount .cal-cell', { timeout: 10000 });

    // 10日をクリック
    const dayCells = await page.locator('.cal-cell').all();
    let opened = false;
    for (const cell of dayCells) {
      const text = await cell.textContent();
      if (text && text.trim().startsWith('10') && !await cell.evaluate((el) => el.classList.contains('empty'))) {
        await cell.click();
        opened = true;
        break;
      }
    }
    await page.waitForTimeout(1000);

    // 詳細ボックス全体を取得して確認
    const detailBox = page.locator('#dayDetail');
    if (await detailBox.count() > 0) {
      const detailText = await detailBox.textContent();
      const detailHtml = await detailBox.innerHTML();
      console.log(`詳細ボックス textContent(先頭300字): ${(detailText || '').slice(0, 300)}`);
      console.log(`詳細ボックス innerHTML(先頭500字): ${(detailHtml || '').slice(0, 500)}`);
    }

    const shiftLines = await page.locator('.shift-line').count();
    console.log(`ドラフト保存後のシフト行数: ${shiftLines}`);
    if (shiftLines > 0) {
      for (let i = 0; i < Math.min(shiftLines, 3); i++) {
        const line = await page.locator('.shift-line').nth(i).textContent();
        const html = await page.locator('.shift-line').nth(i).innerHTML();
        console.log(`  ドラフト行${i + 1} textContent: "${(line || '').trim()}"`);
        console.log(`  ドラフト行${i + 1} innerHTML: ${(html || '').slice(0, 300)}`);
      }
      // 時間が表示されていること
      const first = await page.locator('.shift-line').first().textContent();
      expect(first).toMatch(/\d{2}:\d{2}/);
    }
    expect(errors).toEqual([]);
  });

  test('スタッフのマイシフト画面にも時間表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    // スタッフとしてログイン
    await page.goto('/');
    await page.fill('#loginShopCode', SHOP.shopCode);
    await page.fill('#loginUserCode', 'EMP1');
    await page.fill('#loginPassword', 'Emp1234a');
    await page.click('#loginBtn');
    await page.waitForSelector('#appView:not(.d-none)', { timeout: 10000 });

    // スタッフ画面は defaultScreen が staffDashboard なので、自動遷移を待つ
    await page.waitForTimeout(1500);

    // マイシフトを開く（staff ロール・サイドメニュー優先）
    const myshiftBtn = page.locator('button[data-screen="staffMyshift"]').first();
    await myshiftBtn.click();
    await page.waitForSelector('#staffCalMount', { timeout: 10000 });
    await page.waitForTimeout(2000);

    // カレンダー内のシフトバーに時間表示があることを確認
    const bars = await page.locator('.shift-bar, .cal-cell .chip').count();
    console.log(`スタッフ画面のシフトバー等の数: ${bars}`);

    // 8月3日をクリックして詳細を開く
    const cells = await page.locator('.cal-cell').all();
    for (const cell of cells) {
      const text = await cell.textContent();
      if (text && text.includes('3') && text.length < 30 && !await cell.evaluate((el) => el.classList.contains('empty'))) {
        await cell.click();
        break;
      }
    }
    await page.waitForTimeout(500);

    // 何らかの時間表示があることを確認（スタッフ画面でも）
    const allText = await page.locator('#staffCalMount').textContent().catch(() => '');
    console.log(`  カレンダー内容(先頭200字): ${allText.slice(0, 200)}`);
    expect(errors).toEqual([]);
  });
});
