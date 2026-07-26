/**
 * e2e/wish_text_import.spec.js — 希望テキスト取り込み機能（貼付 → AI解析 → カレンダー
 * プレビュー → 一括登録）の e2e テスト。
 *
 * 設計書: docs/superpowers/specs/2026-07-26-wish-text-import-design.md
 * 対象実装: src/app.py の /api/shop/wishes/parse・/api/shop/wishes/bulk、
 *           public/app.js の openWishImportModal() と _wti* 系関数群。
 *
 * この機能の譲れない原則: 「プレビューの内容と実際に登録される内容が一致していなければ
 * ならない」。そのため多くのテストは page.route() で /wishes/parse をスタブしつつ、
 * /wishes/bulk へ実際に送られたリクエストボディを傍受して assert する（トーストの
 * 申告値ではなく、ネットワーク上の実データを検証する）。
 *
 * LLM には依存しない。/wishes/parse は基本的に全テストでスタブする
 * （実サーバの LLM_API_KEY の有無や AI の気まぐれで揺れないようにするため）。
 * /wishes/bulk は「実サーバに通しつつボディを傍受する」テストと「完全にスタブする」
 * テストの両方がある（後者は成功/失敗パターンを確定的に再現するため）。
 */
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, loginAsStaffApi, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const STAFF_PW = 'Stf1234a';

// メインの店舗。ほとんどのテストがここを共有する（希望取り込みのUI挙動テストなので
// 店舗そのものの状態は問わない。募集期間は年間を通してカバーしておき、既存希望の
// フィクスチャ作成（/api/staff/requests 経由）で個々のテストが期間を気にしなくて済む
// ようにする）。
const SHOP = {
  shopCode: `WTI_${RUN_ID}`,
  shopName: '希望取込テスト店',
  managerCode: `wmgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

// 項目13（募集期間外の警告）専用の店舗。MAIN の年間募集期間があると「期間外」を
// 作れないため、意図的に狭い募集期間だけを持つ店舗を分離する。
const SHOP_OUT = {
  shopCode: `WTO_${RUN_ID}`,
  shopName: '希望取込期間外テスト店',
  managerCode: `omgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

let shopHdr;
let shopOutHdr;

/** 店舗スタッフを作成し staff_id を返す。 */
async function createStaff(request, hdr, code, name, role = 'employee') {
  const res = await request.post('/api/shop/staffs', {
    data: { staff_code: code, name, password: STAFF_PW, role },
    headers: hdr,
  });
  const body = await res.json();
  return body.id;
}

/** スタッフ本人として /api/staff/requests を叩き、wish_history に「既存の希望」を
 *  作る（希望表管理画面・希望取り込みの「既存あり」印はここを見る）。 */
async function submitExistingWish(request, shop, staffCode, { start, end, availability }) {
  const token = await loginAsStaffApi(request, { shopCode: shop.shopCode, staffCode, password: STAFF_PW });
  await request.post('/api/staff/requests', {
    data: { shifts: [{ start_datetime: start, end_datetime: end, ...(availability ? { availability } : {}) }] },
    headers: { Authorization: `Bearer ${token}` },
  });
}

/** /api/shop/wishes/parse をスタブする。保存は一切しないAPIなので、レスポンスの
 *  内容を完全にこちらで決め打ちできる（LLMのAPIキー有無・気まぐれに左右されない）。 */
function stubParse(page, response) {
  return page.route(
    (url) => url.pathname.endsWith('/api/shop/wishes/parse'),
    (route) => route.fulfill({ json: response }),
  );
}

/** 希望表管理画面を開き、「テキストから取り込む」でモーダルを開く（ステップ1）。 */
async function openImportModal(page) {
  await page.click('button[data-screen="requests"]');
  await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
  await page.click('#reqImportBtn');
  await page.waitForSelector('#wtiText', { timeout: 10000 });
}

/** ステップ1で対象月・テキストを入れて「解析する」を押す（entries=0 なら
 *  ステップ2に進まずステップ1に留まるので、遷移待ちは呼び出し側の責務）。 */
async function clickParse(page, { yearMonth = '2026-08', text = 'テスト用の貼り付けテキスト（内容は /parse スタブ側で無視される）' } = {}) {
  await page.selectOption('#wtiMonth', yearMonth);
  await page.fill('#wtiText', text);
  await page.click('#wtiParseBtn');
}

/** clickParse に加え、ステップ2（カレンダー確認画面）への遷移を待つ。 */
async function parseAndWaitStep2(page, opts = {}) {
  await clickParse(page, opts);
  await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
}

/** 「合計 N件を登録する」ボタンのラベルから N を取り出す。 */
function submitTotal(text) {
  const m = /合計\s*(\d+)\s*件を登録する/.exec(text || '');
  return m ? Number(m[1]) : null;
}

/** 375px 等の狭い画面でレイアウトが画面外にはみ出していないかの簡易チェック。 */
async function assertNoHorizontalOverflow(page, label) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, `${label}: 横スクロールが発生しています（overflow=${overflow}px）`).toBeLessThanOrEqual(4);
}

/** 詳細モーダル（#wtiDetailErr を含む方）だけを閉じる。ステップ2側の [data-x] と
 *  区別するため、#wtiDetailErr を子に持つ .modal-overlay に絞る。 */
async function closeDetailModal(page) {
  await page.locator('.modal-overlay').filter({ has: page.locator('#wtiDetailErr') })
    .locator('[data-x]').first().click();
}

test.describe('希望テキスト取り込み（wish text import）', () => {
  test.beforeAll(async ({ request }) => {
    fs.mkdirSync(path.resolve(__dirname, '..', 'screenshots'), { recursive: true });
    await ensureShop(request, SHOP);
    await ensureShop(request, SHOP_OUT);

    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    shopHdr = { Authorization: `Bearer ${(await loginRes.json()).token}` };

    const loginResOut = await request.post('/api/login', {
      data: { shop_code: SHOP_OUT.shopCode, user_code: SHOP_OUT.managerCode, password: SHOP_OUT.managerPassword },
    });
    shopOutHdr = { Authorization: `Bearer ${(await loginResOut.json()).token}` };

    // MAIN_SHOP: 既存希望フィクスチャ（/api/staff/requests）作成用に年間の募集期間を
    // 用意しておく。締切は十分未来（このテストでは締切検証そのものは対象外）。
    await request.post('/api/shop/periods', {
      data: { start_date: '2026-01-01', end_date: '2026-12-31', deadline: '2099-12-31' },
      headers: shopHdr,
    });
  });

  // ==========================================================
  // 基本フロー（task-6-brief Step1）
  // LLM に依存しないよう /wishes/parse はスタブし、解析結果の中身ではなく
  // 「登録後に希望表管理のカードが増える」ことで検証する。
  // ==========================================================
  test('基本フロー: 貼付→解析→カレンダー反映→登録→希望表管理にカードが出る', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'BASE1', '取込太郎');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [{ staff_id: sid, staff_hint: null, dates: ['2026-08-02'], availability: 'rest', start: null, end: null, raw: '8/2は休みたいです' }],
      unparsed: [], source: 'llm',
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    // カレンダーに反映されていること
    await expect(page.locator('.wish-cell[data-day="2026-08-02"] .wmark')).toBeVisible();

    // 登録（/wishes/bulk は実サーバへ。ここは統合的な成功パスを見る）
    await page.click('#wtiSubmitBtn');
    await page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 10000 });

    // 希望表管理にカードが増えていること（トーストではなく実データで確認）
    await page.waitForSelector('.req-staff-card', { timeout: 10000 });
    await expect(page.locator('.req-staff-card', { hasText: '取込太郎' })).toBeVisible({ timeout: 10000 });
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目1: 同日競合の可視化（Critical 修正の受け入れテスト）
  // ==========================================================
  test('同一(スタッフ,日付)の競合はカレンダーに「競合N」として出て、詳細で両方/三方を確認・解消できる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'CNF1', '競合花子');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [
        { staff_id: sid, dates: ['2026-08-03'], availability: 'rest', start: null, end: null, raw: '8/3は休みたいです' },
        { staff_id: sid, dates: ['2026-08-03'], availability: 'time', start: '17:00', end: '22:00', raw: '8/3は17時から22時も入れます' },
        { staff_id: sid, dates: ['2026-08-05'], availability: 'rest', start: null, end: null, raw: '8/5休みますA' },
        { staff_id: sid, dates: ['2026-08-05'], availability: 'any', start: null, end: null, raw: '8/5いつでも大丈夫ですB' },
        { staff_id: sid, dates: ['2026-08-05'], availability: 'time', start: '09:00', end: '17:00', raw: '8/5は9時から17時ですC' },
      ],
      unparsed: [], source: 'llm',
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    // 08-03は2件競合、08-05は3件競合として表示される（片方が黙って消えない）
    await expect(page.locator('.wish-cell[data-day="2026-08-03"] .wmark')).toHaveText('競合2');
    await expect(page.locator('.wish-cell[data-day="2026-08-05"] .wmark')).toHaveText('競合3');

    // 上部の警告がスタッフ名と両日の日付を挙げる
    const warn = page.locator('text=同じ日に複数の読み取りがあります');
    await expect(warn).toBeVisible();
    const warnText = await warn.textContent();
    expect(warnText).toContain('競合花子');
    expect(warnText).toContain('08-03');
    expect(warnText).toContain('08-05');

    // 詳細モーダルは両方を独立した select で列挙する
    await page.click('.wish-cell[data-day="2026-08-03"]');
    await expect(page.locator('.wti-detail-entry')).toHaveCount(2);
    const detailText03 = await page.locator('.modal-overlay').filter({ has: page.locator('#wtiDetailErr') }).textContent();
    expect(detailText03).toContain('8/3は休みたいです');
    expect(detailText03).toContain('8/3は17時から22時も入れます');

    // 片方を削除すると通常のマーク1件に戻り、08-03についての警告は消える
    await page.locator('[data-del-idx]').first().click();
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    const mark03 = await page.locator('.wish-cell[data-day="2026-08-03"] .wmark').textContent();
    expect(mark03).not.toContain('競合');
    const warnAfter03 = await page.locator('text=同じ日に複数の読み取りがあります').textContent();
    expect(warnAfter03).not.toContain('08-03');
    expect(warnAfter03).toContain('08-05'); // 08-05はまだ未解決

    // 3件競合（08-05）も同様に、独立select×3 → 1件ずつ削除して解消できる
    await page.click('.wish-cell[data-day="2026-08-05"]');
    await expect(page.locator('.wti-detail-entry')).toHaveCount(3);
    await page.locator('[data-del-idx]').first().click();
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    await expect(page.locator('.wish-cell[data-day="2026-08-05"] .wmark')).toHaveText('競合2');
    await page.click('.wish-cell[data-day="2026-08-05"]');
    await expect(page.locator('.wti-detail-entry')).toHaveCount(2);
    await page.locator('[data-del-idx]').first().click();
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    const mark05 = await page.locator('.wish-cell[data-day="2026-08-05"] .wmark').textContent();
    expect(mark05).not.toContain('競合');
    await expect(page.locator('text=同じ日に複数の読み取りがあります')).toHaveCount(0);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目3: 同日競合が残っている限り送信をブロックする
  // ==========================================================
  test('同日競合が未解決だと /wishes/bulk は呼ばれず、解消すると送信できる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'BLK1', 'ブロック次郎');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [
        { staff_id: sid, dates: ['2026-08-06'], availability: 'rest', start: null, end: null, raw: '8/6休みA' },
        { staff_id: sid, dates: ['2026-08-06'], availability: 'any', start: null, end: null, raw: '8/6いつでもB' },
      ],
      unparsed: [], source: 'llm',
    });
    let bulkCalls = 0;
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      bulkCalls++;
      const body = route.request().postDataJSON();
      await route.fulfill({ json: { ok: true, created: body.wishes.length, skipped: 0, message: 'ok' } });
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    // 競合が残ったまま登録しようとする → ブロック
    await page.click('#wtiSubmitBtn');
    await expect(page.locator('.toast')).toContainText('同じ日に複数の読み取り', { timeout: 3000 });
    expect(bulkCalls).toBe(0);
    await expect(page.locator('#wtiSubmitBtn')).toBeVisible(); // モーダルは閉じていない

    // 片方を削除して解消 → 送信できる
    await page.click('.wish-cell[data-day="2026-08-06"]');
    await page.locator('[data-del-idx]').first().click();
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    await page.click('#wtiSubmitBtn');
    await expect.poll(() => bulkCalls, { timeout: 5000 }).toBeGreaterThan(0);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目2 + 項目4: プレビューの合計件数 = 実際にPOSTされた件数（2リクエスト合計）
  //   + overwrite の分離送信（チェックした日だけがoverwrite:true側／未チェックは混入しない）
  //   + date は常に開始日（日またぎ 22:00〜翌05:00 でも date は開始日のまま）
  // /wishes/bulk は実サーバへ通しつつ、リクエストボディを傍受して assert する。
  // ==========================================================
  test('プレビュー合計と実POST件数が一致し、overwriteは選んだ日だけ分離され、dateは常に開始日', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'OVR1', '上書き三郎');
    // 既存希望を2日分作る（08-14と08-15）。08-14だけ上書きし、08-15はしない。
    await submitExistingWish(request, SHOP, 'OVR1', { start: '2026-08-14T00:00:00', end: '2026-08-14T23:59:59', availability: 'rest' });
    await submitExistingWish(request, SHOP, 'OVR1', { start: '2026-08-15T09:00:00', end: '2026-08-15T20:00:00', availability: 'any' });

    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [
        { staff_id: sid, dates: ['2026-08-14'], availability: 'any', start: null, end: null, raw: '8/14はいつでも大丈夫です' },     // 既存あり・上書きする
        { staff_id: sid, dates: ['2026-08-15'], availability: 'rest', start: null, end: null, raw: '8/15は休みます' },              // 既存あり・上書きしない
        { staff_id: sid, dates: ['2026-08-16'], availability: 'time', start: '22:00', end: '05:00', raw: '8/16の22時から翌5時まで入れます' }, // 日またぎ・既存なし
        { staff_id: sid, dates: ['2026-08-17'], availability: 'rest', start: null, end: null, raw: '8/17は休みます' },              // 既存なし
      ],
      unparsed: [], source: 'llm',
    });
    const bulkRequests = [];
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      bulkRequests.push(route.request().postDataJSON());
      await route.continue();
    });

    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    // 08-14: 上書きする
    await page.click('.wish-cell[data-day="2026-08-14"]');
    await page.check('#wtiDetailOverwrite');
    await page.locator('[data-save]').click();
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });

    // 08-15: 既存はあるがチェックしない（既定オフのまま保存し、明示的に未チェックを確認）
    await page.click('.wish-cell[data-day="2026-08-15"]');
    await expect(page.locator('#wtiDetailOverwrite')).not.toBeChecked();
    await page.locator('[data-save]').click();
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });

    const btnText = await page.locator('#wtiSubmitBtn').textContent();
    const previewTotal = submitTotal(btnText);
    expect(previewTotal).toBe(4);

    await page.click('#wtiSubmitBtn');
    await page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 10000 });

    expect(bulkRequests.length).toBe(2);
    const overwriteReq = bulkRequests.find((r) => r.overwrite === true);
    const normalReq = bulkRequests.find((r) => r.overwrite !== true);
    expect(overwriteReq).toBeTruthy();
    expect(normalReq).toBeTruthy();

    const overwriteDates = overwriteReq.wishes.map((w) => w.date);
    const normalDates = normalReq.wishes.map((w) => w.date);
    // (a) 上書きにチェックした日だけが overwrite:true 側に入る
    expect(overwriteDates).toEqual(['2026-08-14']);
    // (b) チェックしていない日（08-15、既存あり）は overwrite:true 側に混入しない
    expect(overwriteDates).not.toContain('2026-08-15');
    expect(normalDates.sort()).toEqual(['2026-08-15', '2026-08-16', '2026-08-17']);

    // (c) 日またぎ（22:00〜翌05:00）でも date は常に開始日
    const overnight = normalReq.wishes.find((w) => w.start === '22:00');
    expect(overnight).toBeTruthy();
    expect(overnight.date).toBe('2026-08-16');

    // ★プレビューの合計 N と、実際にPOSTされた件数（2リクエスト合計）が一致する
    const actualTotal = overwriteReq.wishes.length + normalReq.wishes.length;
    expect(actualTotal).toBe(previewTotal);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目5: XSS 回帰
  // ==========================================================
  test('rawとunparsedと時刻inputのvalueがすべてエスケープされ、スクリプトが実行されない', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'XSS1', 'XSS花子');
    const maliciousRaw = '<img src=x onerror="window.__xssRaw=true">';
    const maliciousUnassigned = '<img src=x onerror="window.__xssUnassigned=true">';
    const maliciousUnparsed = '<img src=x onerror="window.__xssUnparsed=true">';
    const maliciousStart = '05:00"><script>window.__xssTime=true</script><input value="';

    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [
        { staff_id: sid, dates: ['2026-08-19'], availability: 'time', start: maliciousStart, end: '10:00', raw: maliciousRaw },
        { staff_hint: '謎の人', staff_id: null, dates: ['2026-08-20'], availability: 'rest', start: null, end: null, raw: maliciousUnassigned },
      ],
      unparsed: [maliciousUnparsed], source: 'llm',
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    // 何も実行されていない（onerrorが発火していない）
    expect(await page.evaluate(() => window.__xssRaw)).toBeFalsy();
    expect(await page.evaluate(() => window.__xssUnassigned)).toBeFalsy();
    expect(await page.evaluate(() => window.__xssUnparsed)).toBeFalsy();

    // 未割り当てのraw・unparsedが文字列としてレンダリングされている
    const unassignedText = await page.locator('.wti-unassigned-row .wti-raw-quote').textContent();
    expect(unassignedText).toContain('<img src=x onerror=');
    const unparsedText = await page.locator('.wti-unparsed li').textContent();
    expect(unparsedText).toContain('<img src=x onerror=');

    // 詳細モーダルのraw、および時刻inputのvalue属性
    await page.click('.wish-cell[data-day="2026-08-19"]');
    const detailRaw = await page.locator('.wti-detail-entry .wti-raw-quote').textContent();
    expect(detailRaw).toContain('<img src=x onerror=');
    const startVal = await page.locator('#wtiDetailStart-0').getAttribute('value');
    expect(startVal).toBe(maliciousStart); // 属性の外に抜け出していない（そのまま1つの属性値として保持）
    expect(await page.evaluate(() => window.__xssTime)).toBeFalsy();
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目6: 部分失敗の3ケース
  //   1回目=新規分success/上書き分fail、逆、両方fail
  // ==========================================================
  async function setupPartialFailureCase(page, request, staffCode) {
    const sid = await createStaff(request, shopHdr, staffCode, `部分失敗_${staffCode}`);
    await submitExistingWish(request, SHOP, staffCode, { start: '2026-08-21T00:00:00', end: '2026-08-21T23:59:59', availability: 'rest' });
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [
        { staff_id: sid, dates: ['2026-08-21'], availability: 'any', start: null, end: null, raw: '8/21はいつでも大丈夫（既存あり→上書き）' },
        { staff_id: sid, dates: ['2026-08-22'], availability: 'rest', start: null, end: null, raw: '8/22は休みます（新規）' },
      ],
      unparsed: [], source: 'llm',
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });
    await page.click('.wish-cell[data-day="2026-08-21"]');
    await page.check('#wtiDetailOverwrite');
    await page.locator('[data-save]').click();
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
  }

  test('部分失敗A: 新規分success・上書き分fail → 成功件数が残り、再送は失敗分のみ', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await setupPartialFailureCase(page, request, 'PF1A');
    const calls = [];
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      const body = route.request().postDataJSON();
      calls.push(body);
      if (body.overwrite === true) {
        await route.fulfill({ status: 500, json: { error: '模擬エラー(上書き分)' } });
      } else {
        await route.fulfill({ json: { ok: true, created: body.wishes.length, skipped: 0, message: `${body.wishes.length}件の希望を登録しました` } });
      }
    });

    await page.click('#wtiSubmitBtn');
    // msgBoxのテキストを同期点にしない（既知のバグ: 後述のsoft assertで検出する）。
    // 代わりにネットワーク往復の完了をリクエスト数で待つ。
    await expect.poll(() => calls.length, { timeout: 5000 }).toBe(2);
    await expect(page.locator('#wtiSubmitBtn')).toBeVisible(); // モーダルは閉じない
    // ★既知の不具合: _wtiSubmit が部分失敗メッセージを #wtiSubmitMsg にセットした直後、
    // 同期的に _wtiRenderStep2(wrap, state) を呼んでいる（app.js の該当行）。この再描画が
    // #wtiSubmitMsg を中身なしの新しいdivに置き換えてしまうため、成功件数を含む
    // メッセージは実際には画面に残らない（3秒で消えるトーストしか見えない）。
    // 要件6「成功した件数が画面に残ること」を満たしていないため、ここは意図的に
    // soft assertion にして失敗を可視化しつつ、以降の（正しく動く）再送検証は継続する。
    const msg1 = await page.locator('#wtiSubmitMsg').textContent();
    expect.soft(msg1, '既知のバグ: _wtiRenderStep2 の再描画で部分失敗メッセージが消える').toContain('登録しました');

    // 再送: 成功済み（08-22）は含まれず、失敗分（08-21）だけが送られる
    // （state.items からの除去自体は正しく行われている。合計ボタンの表示件数が
    // 1件に減っていることでも裏付けられる＝メッセージ表示のバグとは独立した経路）
    await expect(page.locator('#wtiSubmitBtn')).toContainText('合計 1件を登録する');
    await page.click('#wtiSubmitBtn');
    await expect.poll(() => calls.length, { timeout: 5000 }).toBe(3);
    const retry = calls[2];
    expect(retry.overwrite).toBe(true);
    expect(retry.wishes.map((w) => w.date)).toEqual(['2026-08-21']);
    expect(errors).toEqual([]);
  });

  test('部分失敗B: 新規分fail・上書き分success → 成功件数が残り、再送は失敗分のみ', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await setupPartialFailureCase(page, request, 'PF1B');
    const calls = [];
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      const body = route.request().postDataJSON();
      calls.push(body);
      if (body.overwrite === true) {
        await route.fulfill({ json: { ok: true, created: body.wishes.length, skipped: 0, message: `${body.wishes.length}件の希望を登録しました` } });
      } else {
        await route.fulfill({ status: 500, json: { error: '模擬エラー(新規分)' } });
      }
    });

    await page.click('#wtiSubmitBtn');
    await expect.poll(() => calls.length, { timeout: 5000 }).toBe(2);
    await expect(page.locator('#wtiSubmitBtn')).toBeVisible();
    // ★既知の不具合（部分失敗Aと同一原因）: 成功件数を含むメッセージが
    // _wtiRenderStep2 の再描画で消えてしまう。soft assertionで可視化する。
    const msg1 = await page.locator('#wtiSubmitMsg').textContent();
    expect.soft(msg1, '既知のバグ: _wtiRenderStep2 の再描画で部分失敗メッセージが消える').toContain('登録しました');

    await expect(page.locator('#wtiSubmitBtn')).toContainText('合計 1件を登録する');
    await page.click('#wtiSubmitBtn');
    await expect.poll(() => calls.length, { timeout: 5000 }).toBe(3);
    const retry = calls[2];
    expect(retry.overwrite).toBe(false);
    expect(retry.wishes.map((w) => w.date)).toEqual(['2026-08-22']);
    expect(errors).toEqual([]);
  });

  test('部分失敗C: 両方fail → 成功を名乗らず、モーダルは閉じず、再送も両方送られる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await setupPartialFailureCase(page, request, 'PF1C');
    const calls = [];
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      const body = route.request().postDataJSON();
      calls.push(body);
      await route.fulfill({ status: 500, json: { error: '模擬エラー' } });
    });

    await page.click('#wtiSubmitBtn');
    await expect.poll(() => calls.length, { timeout: 5000 }).toBe(2);
    await expect(page.locator('#wtiSubmitBtn')).toBeVisible();
    const msg1 = await page.locator('#wtiSubmitMsg').textContent();
    expect(msg1).not.toContain('登録しました'); // 何も成功していないのに成功を名乗らない（これは成立する＝空文字列）
    // ★既知の不具合（部分失敗A/Bと同一原因）: 「失敗」の説明メッセージ自体も
    // _wtiRenderStep2 の再描画で消えてしまい、ユーザーには何も残らない
    // （3秒で消えるトーストだけが頼り）。soft assertionで可視化する。
    expect.soft(msg1, '既知のバグ: _wtiRenderStep2 の再描画で失敗メッセージが消える').toContain('失敗');

    // 何も成功していないので、再送でも両方のグループがそのまま送られる
    await page.click('#wtiSubmitBtn');
    await expect.poll(() => calls.length, { timeout: 5000 }).toBe(4);
    const retryDates = calls.slice(2).flatMap((c) => c.wishes.map((w) => w.date)).sort();
    expect(retryDates).toEqual(['2026-08-21', '2026-08-22']);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目7: unparsed の可視性
  // ==========================================================
  test('unparsedがステップ2画面に表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'UNP1', '未解析一郎');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [{ staff_id: sid, dates: ['2026-08-08'], availability: 'rest', start: null, end: null, raw: '8/8は休みます' }],
      unparsed: ['あいまいで読み取れない一文です'], source: 'llm',
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    const unparsedBox = page.locator('.wti-unparsed');
    await expect(unparsedBox).toBeVisible();
    const text = await unparsedBox.textContent();
    expect(text).toContain('あいまいで読み取れない一文です');
    expect(text).toContain('1件');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目7(entries0件) + 項目8: fallbackバナー
  // entries が0件（解析が最も失敗しているケース）でも unparsed と fallback注記が
  // 画面に出ることを、実サーバの LLM_API_KEY に依存しない形（スタブ）で確認する。
  // ==========================================================
  test('entriesが0件でもunparsedとfallbackバナーが表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [], unparsed: ['謎の文章その1です'], source: 'fallback',
    });
    await openImportModal(page);
    await clickParse(page, { yearMonth: '2026-08' });
    await page.waitForSelector('text=読み取れませんでした', { timeout: 10000 });

    const msgText = await page.locator('#wtiParseMsg').textContent();
    expect(msgText).toContain('簡易解析で読み取りました');
    expect(msgText).toContain('読み取れませんでした');
    expect(msgText).toContain('謎の文章その1です');
    // ステップ2には進んでいない（#wtiSubmitBtn は存在しない）
    await expect(page.locator('#wtiSubmitBtn')).toHaveCount(0);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目9: 時刻未入力のゲート（二重）
  // ==========================================================
  test('時刻未入力: 詳細モーダルで保存ブロック、登録ボタンでもPOSTされずブロック', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'TIM1', '時刻未入力五郎');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [{ staff_id: sid, dates: ['2026-08-07'], availability: 'time', start: null, end: null, raw: '時間がよくわからない文です' }],
      unparsed: [], source: 'llm',
    });
    let bulkCalls = 0;
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      bulkCalls++;
      await route.fulfill({ json: { ok: true, created: 1, skipped: 0, message: 'ok' } });
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    // 詳細モーダル: time選択のまま時刻を空で保存 → モーダルは閉じずインラインエラー
    await page.click('.wish-cell[data-day="2026-08-07"]');
    await page.locator('[data-save]').click();
    await expect(page.locator('#wtiDetailErr')).toContainText('開始・終了の両方を入力', { timeout: 3000 });
    await expect(page.locator('[data-save]')).toBeVisible(); // 詳細モーダルはまだ開いている

    await closeDetailModal(page);
    await page.waitForSelector('#wtiDetailErr', { state: 'detached', timeout: 5000 });

    // その状態（時刻未入力のまま）で登録しようとすると POST されず、日付を挙げたエラーが出る
    await page.click('#wtiSubmitBtn');
    await expect(page.locator('.toast')).toContainText('08-07', { timeout: 3000 });
    expect(bulkCalls).toBe(0);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目10: 既存希望のレンジ（月またぎ）
  // ==========================================================
  test('月またぎの取り込みでは/shop/wishesが対象月+跨いだ日付までのレンジで要求され、9月の既存希望に印が出る', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'RNG1', '月またぎ六郎');
    await submitExistingWish(request, SHOP, 'RNG1', { start: '2026-09-02T09:00:00', end: '2026-09-02T18:00:00', availability: 'any' });

    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    let wishesUrl = null;
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes'), async (route) => {
      wishesUrl = route.request().url();
      await route.continue();
    });
    await stubParse(page, {
      entries: [{ staff_id: sid, dates: ['2026-08-31', '2026-09-02'], availability: 'rest', start: null, end: null, raw: '月末とその後は休みます' }],
      unparsed: [], source: 'llm',
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    expect(wishesUrl).toBeTruthy();
    expect(wishesUrl).toContain('start=2026-08-01');
    expect(wishesUrl).toContain('end=2026-09-02');

    // カレンダーを9月へ送る
    await page.click('#wtiCalNext');
    await page.waitForTimeout(300);
    const cell = page.locator('.wish-cell[data-day="2026-09-02"]');
    await expect(cell.locator('.wmark')).toBeVisible();
    await expect(cell.locator('.wti-existing-flag')).toBeVisible();

    // クリックすると上書きチェックボックスが出る
    await cell.click();
    await expect(page.locator('#wtiDetailOverwrite')).toBeVisible();
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目11: 未割り当ての振り分け
  // ==========================================================
  test('未割り当ては振り分けないまま登録すると対象外になる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'UNA1', '割当済み七子');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [
        { staff_id: sid, dates: ['2026-08-23'], availability: 'rest', start: null, end: null, raw: '8/23は休みます' },
        { staff_hint: '不明さん', staff_id: null, dates: ['2026-08-24'], availability: 'rest', start: null, end: null, raw: '8/24は不明さんが休みます' },
      ],
      unparsed: [], source: 'llm',
    });
    const bulkRequests = [];
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      const body = route.request().postDataJSON();
      bulkRequests.push(body);
      await route.fulfill({ json: { ok: true, created: body.wishes.length, skipped: 0, message: 'ok' } });
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    const btnText = await page.locator('#wtiSubmitBtn').textContent();
    expect(submitTotal(btnText)).toBe(1); // 未割り当ての1件は数えない

    await page.click('#wtiSubmitBtn');
    await page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 10000 });
    expect(bulkRequests.length).toBe(1);
    expect(bulkRequests[0].wishes.length).toBe(1);
    expect(bulkRequests[0].wishes[0].date).toBe('2026-08-23');
    expect(errors).toEqual([]);
  });

  test('未割り当てをスタッフに振り分けるとカレンダーに移る', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'UNA2', '割当済み八子');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [
        { staff_id: sid, dates: ['2026-08-27'], availability: 'rest', start: null, end: null, raw: '8/27は休みます' },
        { staff_hint: '不明さん2', staff_id: null, dates: ['2026-08-28'], availability: 'rest', start: null, end: null, raw: '8/28は不明さん2が休みます' },
      ],
      unparsed: [], source: 'llm',
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    await expect(page.locator('.wti-unassigned-row')).toHaveCount(1);
    await page.locator('.wti-unassigned-select').selectOption(String(sid));
    await page.waitForTimeout(300);

    await expect(page.locator('.wti-unassigned-row')).toHaveCount(0);
    await expect(page.locator('.wish-cell[data-day="2026-08-28"] .wmark')).toBeVisible();
    const btnText = await page.locator('#wtiSubmitBtn').textContent();
    expect(submitTotal(btnText)).toBe(2);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目12: created:0 の扱い
  // ==========================================================
  test('created:0/skipped:Nは成功ではなく警告として表示され、モーダルは閉じない', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'ZERO1', '全滅九子');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [{ staff_id: sid, dates: ['2026-08-25'], availability: 'rest', start: null, end: null, raw: '8/25は休みます' }],
      unparsed: [], source: 'llm',
    });
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      await route.fulfill({ json: { ok: true, created: 0, skipped: 2, message: '0件の希望を登録しました（2件は重複または不正のためスキップ）' } });
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    await page.click('#wtiSubmitBtn');
    await expect(page.locator('#wtiSubmitMsg')).toContainText('登録できる項目がありませんでした', { timeout: 5000 });
    const msg = await page.locator('#wtiSubmitMsg').textContent();
    expect(msg).toContain('2件');
    await expect(page.locator('#wtiSubmitBtn')).toBeVisible(); // モーダルは閉じない
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目13: 募集期間外の警告（ブロックはしない）
  // ==========================================================
  test('募集期間外の日付は警告が出るが、登録ボタンは有効なまま', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopOutHdr, 'OUT1', '期間外十子');
    await request.post('/api/shop/periods', {
      data: { start_date: '2026-08-01', end_date: '2026-08-15', deadline: '2099-12-31' },
      headers: shopOutHdr,
    });
    await loginAsManager(page, { shopCode: SHOP_OUT.shopCode, managerCode: SHOP_OUT.managerCode, password: SHOP_OUT.managerPassword });
    await stubParse(page, {
      entries: [{ staff_id: sid, dates: ['2026-08-20'], availability: 'rest', start: null, end: null, raw: '8/20は休みます（募集期間外）' }],
      unparsed: [], source: 'llm',
    });
    await openImportModal(page);
    await parseAndWaitStep2(page, { yearMonth: '2026-08' });

    const warnText = await page.locator('#wtiPeriodWarn').textContent();
    expect(warnText).toContain('募集期間外');
    expect(warnText).toContain('08-20');
    await expect(page.locator('#wtiSubmitBtn')).toBeEnabled();
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // 項目14: 375px とダークテーマ（既存の *_visual.spec.js に倣い、スクリーンショットを
  // 撮って人の目で確認できるようにしつつ、横スクロールが出ていないかを機械的にも確認する）
  // ==========================================================
  test('375px・ダークテーマでステップ1/ステップ2/詳細モーダルが崩れない', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, 'VIS1', '見た目十一子');
    await page.setViewportSize({ width: 375, height: 812 });
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await stubParse(page, {
      entries: [{ staff_id: sid, dates: ['2026-08-26'], availability: 'time', start: '10:00', end: '18:00', raw: '8/26は10時から18時希望です' }],
      unparsed: [], source: 'llm',
    });

    async function runPass(themeLabel) {
      // 375px ではサイドナビが折りたたまれているのでハンバーガーを開く
      await page.click('#menuToggle');
      await page.click('button[data-screen="requests"]');
      await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
      await page.click('#reqImportBtn');
      await page.waitForSelector('#wtiText', { timeout: 10000 });
      await page.screenshot({ path: `screenshots/wti-step1-375-${themeLabel}.png` });
      await assertNoHorizontalOverflow(page, `step1(${themeLabel})`);

      await parseAndWaitStep2(page, { yearMonth: '2026-08' });
      await page.screenshot({ path: `screenshots/wti-step2-375-${themeLabel}.png` });
      await assertNoHorizontalOverflow(page, `step2(${themeLabel})`);

      await page.click('.wish-cell[data-day="2026-08-26"]');
      // #wtiDetailErr は通常空（0サイズ）で "visible" 判定にならないため、
      // 実際に見える .wti-detail-entry の描画を待つ。
      await page.waitForSelector('.wti-detail-entry', { timeout: 5000 });
      await page.screenshot({ path: `screenshots/wti-detail-375-${themeLabel}.png` });
      await assertNoHorizontalOverflow(page, `detail(${themeLabel})`);

      await closeDetailModal(page);
      await page.waitForSelector('.wti-detail-entry', { state: 'detached', timeout: 5000 });
      // ステップ2のモーダルも閉じる（キャンセル）
      await page.locator('.modal-overlay').filter({ has: page.locator('#wtiSubmitBtn') }).locator('[data-x]').first().click();
      await page.waitForSelector('#wtiSubmitBtn', { state: 'detached', timeout: 5000 });
    }

    await runPass('light');

    // ダークテーマへ切り替え（モーダルが閉じている状態で。ヘッダーのトグルはモーダル
    // オーバーレイに隠れないので操作できるが、念のためモーダルを閉じてから切り替える）
    await page.click('#themeToggleBtn');
    await page.waitForTimeout(300);
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');

    await runPass('dark');
    expect(errors).toEqual([]);
  });
});
