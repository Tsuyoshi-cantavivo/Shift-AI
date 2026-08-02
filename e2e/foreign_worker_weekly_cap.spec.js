/**
 * e2e/foreign_worker_weekly_cap.spec.js — 外国籍アルバイトの週28時間上限を
 * 手動でシフト追加するときの確認ダイアログと再送。
 *
 * 計画: docs/superpowers/plans/2026-08-02-foreign-worker-weekly-cap.md (Task 6)
 * 対象実装: public/app.js の api()（エラーに data を添える）と
 *           saveShiftWithWeeklyCapConfirm()、およびその5箇所の呼び出し。
 *
 * 方針:
 *   POST /api/shop/shifts は page.route でスタブし、1回目は 400 +
 *   weekly_cap_exceeded、2回目は 200 を返す。実サーバの週計算には依存させない
 *   （判定そのものは tests/test_foreign_worker_role.py の責務であり、ここで
 *   検証するのはフロントの確認と再送の動線だけ）。
 *
 *   確認は confirm() を使っている（既存の「必要人数超過」確認と同じ作法）。
 *   Playwright では page.on('dialog') で受ける。
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);

const SHOP = {
  shopCode: `FWC_${RUN_ID}`,
  shopName: '外国籍週上限テスト店',
  managerCode: `fwcmgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

const OVER_DETAIL = {
  window_start: '2026-08-08',
  window_end: '2026-08-14',
  minutes: 32 * 60,
  cap_minutes: 28 * 60,
};
const OVER_MESSAGE =
  '2026-08-08〜2026-08-14 の7日間で32時間0分になり、外国籍アルバイトの週28時間の上限を超えます。';

let shopHdr;

test.describe('外国籍アルバイトの週28時間上限（手動追加）', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    const token = (await loginRes.json()).token;
    shopHdr = { Authorization: `Bearer ${token}` };
    // 追加モーダルの <select> に並べるスタッフが1人は要る
    await request.post('/api/shop/staffs', {
      data: {
        staff_code: `FWE_${RUN_ID}`, name: '外国籍テスト',
        password: 'Fwk12345a', role: 'foreign_worker',
      },
      headers: shopHdr,
    });
  });

  /** シフト画面の「手動追加」モーダルを開き、日時を入れる。 */
  async function openAddShiftModal(page) {
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#addShiftBtn', { timeout: 10000 });
    await page.click('#addShiftBtn');
    await page.waitForSelector('#adStaff', { timeout: 10000 });
    await page.fill('#adStart', '2026-08-14T09:00');
    await page.fill('#adEnd', '2026-08-14T18:00');
  }

  /**
   * POST /api/shop/shifts をスタブする。
   * overFirst=true なら1回目だけ 400 + weekly_cap_exceeded を返す。
   * 送られた body を配列で集める（再送の中身を検証するため）。
   */
  async function stubShiftPost(page, { overFirst }) {
    const posted = [];
    await page.route(
      (url) => url.pathname.endsWith('/api/shop/shifts'),
      async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        posted.push(route.request().postDataJSON());
        if (overFirst && posted.length === 1) {
          return route.fulfill({
            status: 400,
            json: { error: OVER_MESSAGE, weekly_cap_exceeded: true, detail: OVER_DETAIL },
          });
        }
        return route.fulfill({ json: { ok: true, id: 12345 } });
      },
    );
    return posted;
  }

  // ==========================================================
  // ケース1: 承諾すると weekly_cap_confirmed を付けて再送される
  // ==========================================================
  test('週28h超過の確認を承諾すると、weekly_cap_confirmed を付けて再送される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    const dialogs = [];
    page.on('dialog', (d) => { dialogs.push(d.message()); d.accept(); });

    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    const posted = await stubShiftPost(page, { overFirst: true });
    await openAddShiftModal(page);
    await page.click('.modal-overlay [data-save]');

    await expect.poll(() => posted.length, { timeout: 10000 }).toBe(2);
    // 1回目に confirmed を付けて送っていないこと。付けて送ると、サーバの検査が
    // 常に素通りして確認そのものが無意味になる。
    expect(posted[0].weekly_cap_confirmed).toBeUndefined();
    // 2回目は承諾フラグ付きで、他の内容は1回目と同じ
    expect(posted[1].weekly_cap_confirmed).toBe(true);
    expect(posted[1].staff_id).toBe(posted[0].staff_id);
    expect(posted[1].start_datetime).toBe(posted[0].start_datetime);
    expect(posted[1].end_datetime).toBe(posted[0].end_datetime);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース2: ダイアログにどの7日間が何時間になるかが出る
  // ==========================================================
  test('確認ダイアログに、超過する7日間と合計時間が示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    const dialogs = [];
    page.on('dialog', (d) => { dialogs.push(d.message()); d.accept(); });

    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubShiftPost(page, { overFirst: true });
    await openAddShiftModal(page);
    await page.click('.modal-overlay [data-save]');

    await expect.poll(() => dialogs.length, { timeout: 10000 }).toBeGreaterThan(0);
    // 店長が判断できる材料（期間と合計時間）が無ければ、確認の意味がない
    expect(dialogs[0]).toContain(OVER_DETAIL.window_start);
    expect(dialogs[0]).toContain(OVER_DETAIL.window_end);
    expect(dialogs[0]).toContain('32時間');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース3: キャンセルすると再送されない
  // ==========================================================
  test('確認をキャンセルすると再送されず、シフトは保存されない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    page.on('dialog', (d) => d.dismiss());

    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    const posted = await stubShiftPost(page, { overFirst: true });
    await openAddShiftModal(page);
    await page.click('.modal-overlay [data-save]');

    // 再送が起きないことを確かめるので、一定時間待ってから件数を見る
    await page.waitForTimeout(1500);
    expect(posted.length).toBe(1);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース4: 上限内ならダイアログを出さない
  // ==========================================================
  test('週28h以内のシフトでは確認ダイアログが出ない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    let dialogCount = 0;
    page.on('dialog', (d) => { dialogCount += 1; d.dismiss(); });

    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    const posted = await stubShiftPost(page, { overFirst: false });
    await openAddShiftModal(page);
    await page.click('.modal-overlay [data-save]');

    await expect.poll(() => posted.length, { timeout: 10000 }).toBe(1);
    await page.waitForTimeout(500);
    expect(dialogCount).toBe(0);
    expect(errors).toEqual([]);
  });
});
