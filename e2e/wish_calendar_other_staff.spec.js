/**
 * e2e/wish_calendar_other_staff.spec.js — 取り込み確認画面のカレンダーで、
 * 「いま表示していないスタッフの希望がある日」と「未割り当ての日」を
 * 取りこぼさないことを固定する。
 *
 * 背景（実測した不具合）:
 *   カレンダーは state.calStaffId の1人分しか描かないため、他のスタッフの
 *   希望がある日が「何も無い日」と同じ .disabled になり、押しても開かなかった。
 *   希望表を写真で撮ると複数人が1枚に写るので、店長から見ると
 *   「一部の日付しか変更できない」状態になる。
 *
 * 実サーバの解析には依存させない（/wishes/parse-image は page.route でスタブ）。
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const FIXTURE_PNG = path.resolve(__dirname, 'fixtures', 'wti-test.png');

const SHOP = {
  shopCode: `WCO_${RUN_ID}`,
  shopName: 'カレンダー確認テスト店',
  managerCode: `wcomgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

let shopHdr;
const ids = {};

/** 田中(8/3)・佐藤(8/5)・未割り当て(8/7) を含む読み取り結果を返す。 */
function stubThreeDays(page) {
  return page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse-image'), (route) =>
    route.fulfill({
      json: {
        entries: [
          { staff_id: ids.a, staff_hint: '田中', dates: ['2026-08-03'], availability: 'rest', start: null, end: null, raw: '8/3 田中 休み', raw_verified: true },
          { staff_id: ids.b, staff_hint: '佐藤', dates: ['2026-08-05'], availability: 'rest', start: null, end: null, raw: '8/5 佐藤 休み', raw_verified: true },
          { staff_id: null, staff_hint: 'ヤマダ', dates: ['2026-08-07'], availability: 'rest', start: null, end: null, raw: '8/7 ヤマダ 休み', raw_verified: true },
        ],
        unparsed: [], source: 'llm',
        ocr_text: '8/3 田中 休み\n8/5 佐藤 休み\n8/7 ヤマダ 休み',
        name_candidates: {},
      },
    }));
}

async function openStep2(page) {
  await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
  await page.click('button[data-screen="requests"]');
  await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
  await page.click('#reqImportBtn');
  await page.waitForSelector('#wtiImageDrop', { timeout: 10000 });
  await page.selectOption('#wtiMonth', '2026-08');
  await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
  await stubThreeDays(page);
  await page.click('#wtiParseBtn');
  await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
}

test.describe('確認画面カレンダー: 表示中でないスタッフの希望', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
    const res = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    shopHdr = { Authorization: `Bearer ${(await res.json()).token}` };
    for (const [key, name] of Object.entries({ a: '田中太郎', b: '佐藤花子', c: '鈴木一郎' })) {
      const r = await request.post('/api/shop/staffs', {
        data: { staff_code: `WCO${key}_${RUN_ID}`, name, password: 'Stf1234a', role: 'part_time' },
        headers: shopHdr,
      });
      ids[key] = (await r.json()).id;
    }
  });

  // ==========================================================
  // ケース1: 他スタッフの希望がある日は「空の日」扱いにしない
  // ==========================================================
  test('他のスタッフの希望がある日は disabled にならず、印が出る', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await openStep2(page);

    // 表示中は田中太郎（entries の先頭）。8/5 は佐藤花子の希望。
    const other = page.locator('.wish-cell[data-day="2026-08-05"]');
    await expect(other).not.toHaveClass(/disabled/);
    // 誰の希望なのかが分かる印が出ていること（押す前に判断できる必要がある）
    await expect(other.locator('.wti-other-staff-mark')).toBeVisible();
    await expect(other.locator('.wti-other-staff-mark')).toHaveAttribute('title', /佐藤花子/);

    // 対照: 本当に何も無い日は今までどおり disabled のまま
    await expect(page.locator('.wish-cell[data-day="2026-08-20"]')).toHaveClass(/disabled/);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース2: 他スタッフの日を押しても、表示中のスタッフは勝手に変わらない
  //
  // 実測した不具合: 日付を押すと、選択中のスタッフが黙って別人に差し替わって
  // いた。セルの見た目は通常の日とほぼ同じ（小さな人アイコンだけ）なので、
  // 表示中の人の希望を直そうとして押したつもりが、カレンダーごと別人に
  // 移動してしまう。誰の希望を触るかは店長が選ぶことであって、
  // クリックの副作用で決まってよいものではない。
  // ==========================================================
  test('他のスタッフの日を押しても表示中スタッフは切り替わらない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await openStep2(page);

    await expect(page.locator('#wtiCalStaff')).toHaveValue(String(ids.a));
    await page.locator('.wish-cell[data-day="2026-08-05"]').click();
    await page.waitForTimeout(500);

    // 勝手に切り替わらない
    await expect(page.locator('#wtiCalStaff')).toHaveValue(String(ids.a));
    // 代わりに「この日は誰の希望か」を選ぶ画面が出て、名前と件数が分かる
    const picker = page.locator('.modal-overlay').last();
    await expect(picker).toContainText('佐藤花子');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース2b: 選べば、その人の読み取りを（表示は変えずに）直せる
  // ==========================================================
  test('選んだ人の読み取りを、表示スタッフを変えずに開いて直せる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await openStep2(page);

    await page.locator('.wish-cell[data-day="2026-08-05"]').click();
    await page.waitForTimeout(400);
    await page.locator('[data-pick-staff]').first().click();
    await page.waitForTimeout(400);

    // 佐藤の読み取り内容が編集できる状態で出ている
    const detail = page.locator('.modal-overlay').last();
    await expect(detail).toContainText('8/5 佐藤 休み');
    // 誰の希望を触っているかがモーダルに書かれている（取り違え防止）
    await expect(detail).toContainText('佐藤花子');
    // それでも表示中スタッフは田中のまま
    await expect(page.locator('#wtiCalStaff')).toHaveValue(String(ids.a));
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース2c: 同じ日に複数人いても、全員に手が届く
  //
  // 従来は others[0] の1人だけを開いており、残りは名前が出るだけで
  // 開く手段が無かった。
  // ==========================================================
  test('同じ日に複数人の希望があれば全員が選べる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse-image'), (route) =>
      route.fulfill({
        json: {
          entries: [
            { staff_id: ids.a, staff_hint: '田中', dates: ['2026-08-03'], availability: 'rest', start: null, end: null, raw: '8/3 田中 休み', raw_verified: true },
            { staff_id: ids.b, staff_hint: '佐藤', dates: ['2026-08-09'], availability: 'rest', start: null, end: null, raw: '8/9 佐藤 休み', raw_verified: true },
            { staff_id: ids.c, staff_hint: '鈴木', dates: ['2026-08-09'], availability: 'any', start: null, end: null, raw: '8/9 鈴木 いつでも', raw_verified: true },
          ],
          unparsed: [], source: 'llm',
          ocr_text: '8/3 田中 休み\n8/9 佐藤 休み\n8/9 鈴木 いつでも',
          name_candidates: {},
        },
      }));
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await page.click('button[data-screen="requests"]');
    await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
    await page.click('#reqImportBtn');
    await page.waitForSelector('#wtiImageDrop', { timeout: 10000 });
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });

    await page.locator('.wish-cell[data-day="2026-08-09"]').click();
    await page.waitForTimeout(400);
    const picker = page.locator('.modal-overlay').last();
    await expect(picker.locator('[data-pick-staff]')).toHaveCount(2);
    await expect(picker).toContainText('佐藤花子');
    await expect(picker).toContainText('鈴木一郎');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース3: 未割り当ての日も取りこぼさない
  // ==========================================================
  test('未割り当ての希望がある日にも印が出て、押すと未割り当て欄が示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await openStep2(page);

    const cell = page.locator('.wish-cell[data-day="2026-08-07"]');
    await expect(cell).not.toHaveClass(/disabled/);
    await expect(cell.locator('.wti-unassigned-mark')).toBeVisible();

    await cell.click();
    await page.waitForTimeout(400);
    // 未割り当て欄が強調される（どこを操作すればよいかが分かる）
    await expect(page.locator('#wtiUnassigned')).toHaveClass(/wti-highlight/);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース4: 表示中スタッフの日は今までどおり開く（退行防止）
  // ==========================================================
  test('表示中スタッフの日はこれまでどおり詳細が開く', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await openStep2(page);

    await page.locator('.wish-cell[data-day="2026-08-03"]').click();
    await page.waitForTimeout(400);
    await expect(page.locator('.modal-overlay').last()).toContainText('8/3 田中 休み');
    expect(errors).toEqual([]);
  });
});
