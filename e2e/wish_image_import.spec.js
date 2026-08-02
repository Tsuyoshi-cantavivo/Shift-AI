/**
 * e2e/wish_image_import.spec.js — 希望表「画像」取り込み（貼り付け・ファイル選択/D&D・
 * 撮影）の e2e テスト。
 *
 * ブリーフ: .superpowers/sdd/2026-08-02-phase3-wish-image-import/task-5-brief.md,
 *           task-6-brief.md
 * 対象実装: public/app.js の _wtiRenderStep1() 内の画像ゾーンと reqImageResize()、
 *           _wtiParse() の /wishes/parse-image 分岐、
 *           _wtiRenderUnassigned() の名前候補（name_candidates）確認UI（Task6）。
 *
 * 既存の e2e/wish_text_import.spec.js と作法を揃える:
 *   - /wishes/parse・/wishes/parse-image は page.route でスタブし、実LLMには依存しない
 *     （実画像をLLMに投げない）。
 *   - ステップ2以降（確認画面・カレンダー・bulk確定）の挙動は wish_text_import 側で
 *     既にカバー済みのため、このファイルではステップ1（画像ゾーン）の挙動と
 *     parse-image への分岐だけを対象にする。
 *   - Task6: name_candidates を使う「未割り当て」候補UIは _wtiRenderUnassigned に
 *     一本化されており、テキスト/画像どちらの経路でも同じ関数が描く（分岐しない）。
 *     brief の Files 指定どおりこのファイル（画像経路）でのみ検証する。
 */
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const STAFF_PW = 'Stf1234a';

const SHOP = {
  shopCode: `WII_${RUN_ID}`,
  shopName: '希望画像取込テスト店',
  managerCode: `wiimgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

const FIXTURE_PNG = path.resolve(__dirname, 'fixtures', 'wti-test.png');

let shopHdr;

/** /api/shop/wishes/parse をスタブする（テキスト経路）。 */
function stubParse(page, response) {
  return page.route(
    (url) => url.pathname.endsWith('/api/shop/wishes/parse'),
    (route) => route.fulfill({ json: response }),
  );
}

/** /api/shop/wishes/parse-image をスタブする（画像経路）。実画像をLLMに投げない。 */
function stubParseImage(page, response) {
  return page.route(
    (url) => url.pathname.endsWith('/api/shop/wishes/parse-image'),
    (route) => route.fulfill({ json: response }),
  );
}

/** 店舗スタッフを作成し staff_id を返す（e2e/wish_text_import.spec.js と同じ形）。 */
async function createStaff(request, hdr, code, name, role = 'employee') {
  const res = await request.post('/api/shop/staffs', {
    data: { staff_code: code, name, password: STAFF_PW, role },
    headers: hdr,
  });
  const body = await res.json();
  return body.id;
}

/** 希望表管理画面を開き、「テキストから取り込む」でモーダルを開く（ステップ1）。 */
async function openImportModal(page) {
  await page.click('button[data-screen="requests"]');
  await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
  await page.click('#reqImportBtn');
  await page.waitForSelector('#wtiImageDrop', { timeout: 10000 });
}

test.describe('希望画像取り込み（wish image import）', () => {
  test.beforeAll(async ({ request }) => {
    fs.mkdirSync(path.resolve(__dirname, '..', 'screenshots'), { recursive: true });
    await ensureShop(request, SHOP);
    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    shopHdr = { Authorization: `Bearer ${(await loginRes.json()).token}` };
    await request.post('/api/shop/periods', {
      data: { start_date: '2026-01-01', end_date: '2026-12-31', deadline: '2099-12-31' },
      headers: shopHdr,
    });
  });

  // ==========================================================
  // ケース1: 画像ゾーンが表示され、ファイルを選ぶとサムネイルが出る
  // ==========================================================
  test('画像ゾーンが表示され、ファイルを選ぶとサムネイルが出る', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);

    await expect(page.locator('#wtiImageDrop')).toBeVisible();
    await expect(page.locator('.wti-image-thumb')).toHaveCount(0);

    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await expect(page.locator('.wti-image-thumb[data-idx="0"]')).toBeVisible();
    await expect(page.locator('.wti-image-thumb')).toHaveCount(1);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース2: 画像を選んだ状態で「解析する」を押すと /wishes/parse-image が
  //          呼ばれる（テキストのみなら /wishes/parse）
  // ==========================================================
  test('画像選択時は /wishes/parse-image が呼ばれ、テキストのみなら /wishes/parse が呼ばれる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });

    let parseImageCalls = 0;
    let parseCalls = 0;
    // '/api/shop/wishes/parse-image' は '/api/shop/wishes/parse' で終わらないため、
    // endsWith だけで両者は排他的に判定できる（例: wish_text_import.spec.js の stubParse と同じ判定式）。
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse'), (route) => {
      parseCalls++;
      return route.fulfill({ json: { entries: [], unparsed: [], source: 'llm' } });
    });
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse-image'), (route) => {
      parseImageCalls++;
      return route.fulfill({ json: { entries: [], unparsed: [], source: 'llm', ocr_text: '' } });
    });

    // まずテキストのみ → /wishes/parse
    await openImportModal(page);
    await page.selectOption('#wtiMonth', '2026-08');
    await page.fill('#wtiText', 'テキストのみのテスト');
    await page.click('#wtiParseBtn');
    await expect.poll(() => parseCalls, { timeout: 5000 }).toBeGreaterThan(0);
    expect(parseImageCalls).toBe(0);

    // 画像を追加 → /wishes/parse-image（テキストが残っていても画像経路優先）
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await expect(page.locator('.wti-image-thumb')).toHaveCount(1);
    await page.click('#wtiParseBtn');
    await expect.poll(() => parseImageCalls, { timeout: 5000 }).toBeGreaterThan(0);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース3: スタブしたレスポンスでステップ2（確認画面）へ進む
  // ==========================================================
  test('画像経路でもスタブしたレスポンスでステップ2（確認画面）へ進み、送信前にJPEGへ再エンコードされている', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);

    // レビュー指摘 I-1: reqImageResizeDims の「計算」を検証するテスト
    // （tests/test_wish_image_resize.py）だけでは、_wtiParse が実際に
    // reqImageResize を呼んで送信しているかは保証できない（呼び出し自体を
    // 消しても純関数テストもE2Eも緑のままだった）。stubParseImage ヘルパは
    // レスポンスを決め打ちするだけでリクエストボディを見ないため、ここでは
    // 生の page.route で実際に送られた postData を捕まえる。フィクスチャは
    // PNG（e2e/fixtures/wti-test.png）なので、images[0] が
    // data:image/jpeg;base64,... で始まっていれば、canvasによる
    // JPEG再エンコード（brief Step4）が実際に走った証拠になる。
    let postedBody = null;
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse-image'), (route) => {
      postedBody = route.request().postDataJSON();
      return route.fulfill({
        json: {
          entries: [{ staff_id: null, staff_hint: null, dates: ['2026-08-10'], availability: 'rest', start: null, end: null, raw: '8/10は休みたいです', raw_verified: true }],
          unparsed: [], source: 'llm', ocr_text: '8/10は休みたいです', name_candidates: {},
        },
      });
    });
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    // 画像経路の目印: OCR全文の折りたたみが出ている（テキスト経路には無い要素）
    await expect(page.locator('.wti-ocr-details')).toBeVisible();

    expect(postedBody).not.toBeNull();
    expect(postedBody.images).toHaveLength(1);
    expect(postedBody.images[0]).toMatch(/^data:image\/jpeg;base64,/);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // レビュー指摘 I-2: 画像経路の警告文言が、テキスト経路の断定文言に
  // 退化していないこと（この Task で追加要件として出された主要件の回帰ガード）。
  // 既存の e2e/wish_text_import.spec.js:886 相当のテストはテキスト経路しか
  // 見ておらず、両経路が同じ文言に揃ってしまう退化を検知できなかった。
  // ==========================================================
  test('画像経路のraw_verified:false警告は、テキスト経路の断定文言に退化していない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    // カレンダーに表示させて詳細モーダルを開くだけなので、実在するスタッフである
    // 必要はない（_wtiRenderCalendar/_wtiOpenDetail は state.staffs に無い
    // staff_id も「不明#id」としてそのまま描く）。API呼び出しを増やさないため、
    // 固定のダミーIDを使う（このファイルの他のテストが作るスタッフと衝突しない
    // よう、実在しなさそうな大きい値にする）。
    const dummyStaffId = 900000001;
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await stubParseImage(page, {
      entries: [{ staff_id: dummyStaffId, staff_hint: null, dates: ['2026-08-14'], availability: 'rest', start: null, end: null, raw: 'このOCR全文には実在しない文', raw_verified: false }],
      unparsed: [], source: 'llm', ocr_text: '無関係な読み取り結果', name_candidates: {},
    });
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });

    await page.click('.wish-cell[data-day="2026-08-14"]');
    const detailText = await page.locator('.wti-detail-entry').textContent();
    // 画像経路の弱い保証を伝える、確認を促す文言を含むこと
    expect(detailText).toContain('元の画像と見比べて確認してください');
    // 退化検知の本体: テキスト経路の断定文言（「AIが要約または創作した可能性が
    // あります」）を含まないこと。片方だけの assert だと「両方混ざった」退化
    // （テキストの文言はそのまま残しつつ画像側の文言も足す）を見逃すため、
    // 含む/含まないの両方を見る。
    expect(detailText).not.toContain('AIが要約または創作した可能性があります');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース4: サムネイルの削除ボタンで取り消せる
  // ==========================================================
  test('サムネイルの削除ボタンで取り消せる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);

    await page.setInputFiles('#wtiImageInput', [FIXTURE_PNG, FIXTURE_PNG]);
    await expect(page.locator('.wti-image-thumb')).toHaveCount(2);

    await page.locator('.wti-image-thumb[data-idx="0"] [data-del-img]').click();
    await expect(page.locator('.wti-image-thumb')).toHaveCount(1);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース5: 4枚目を追加しようとすると弾かれる
  // ==========================================================
  test('4枚目を追加しようとすると弾かれる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);

    await page.setInputFiles('#wtiImageInput', [FIXTURE_PNG, FIXTURE_PNG, FIXTURE_PNG]);
    await expect(page.locator('.wti-image-thumb')).toHaveCount(3);

    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await expect(page.locator('.toast')).toContainText('3枚', { timeout: 3000 });
    await expect(page.locator('.wti-image-thumb')).toHaveCount(3);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // レビュー指摘 I-3: #wtiText への貼り付けは画像を横取りしない。
  // Excel/Slack/LINE 等はクリップボードに text/plain と image/png を同時に
  // 載せることがある。貼り付けリスナは wrap（モーダル全体）に束縛している
  // ため、対策が無いと #wtiText への貼り付けでも画像を拾ってしまい、以後の
  // 「解析する」が貼ったテキストを無告知で無視して画像経路（保証の弱い経路）
  // に切り替わる。生の paste イベントを組み立てて検証する（setInputFiles では
  // 貼り付け経路を通せないため。Playwright の Locator API はクリックやフォーカス
  // の競合を自己修復してしまい、貼り付け固有の分岐を素通りしうる）。
  // ==========================================================
  test('#wtiTextへの貼り付けは画像を拾わない（ドロップゾーンへの貼り付けは従来どおり拾う）', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);

    const pngBase64 = fs.readFileSync(FIXTURE_PNG).toString('base64');

    // text/plain と image/png の両方を積んだ DataTransfer を作り、対象要素へ
    // 生の paste イベントとして dispatch する（ClipboardEvent のコンストラクタに
    // clipboardData を渡す方式はブラウザ間で挙動が割れるため、素の Event に
    // Object.defineProperty で clipboardData を後付けする方式に統一する）。
    const dispatchPaste = async (selector) => {
      await page.locator(selector).evaluate((el, base64) => {
        const byteStr = atob(base64);
        const bytes = new Uint8Array(byteStr.length);
        for (let i = 0; i < byteStr.length; i++) bytes[i] = byteStr.charCodeAt(i);
        const file = new File([bytes], 'clip.png', { type: 'image/png' });
        const dt = new DataTransfer();
        dt.items.add(file);
        dt.items.add('貼り付けたテキストです', 'text/plain');
        const evt = new Event('paste', { bubbles: true, cancelable: true });
        Object.defineProperty(evt, 'clipboardData', { value: dt });
        el.dispatchEvent(evt);
      }, pngBase64);
    };

    // #wtiText への貼り付け: 画像を拾わない（サムネイルが増えない）こと
    await dispatchPaste('#wtiText');
    await page.waitForTimeout(300); // _wtiHandleImageFiles は非同期（FileReader）なので確実に待つ
    await expect(page.locator('.wti-image-thumb')).toHaveCount(0);

    // 対照実験: 同じ paste をドロップゾーンへ送ると従来どおり画像を拾う
    // （このテスト自体が「貼り付けを一律無効化した」壊れ方では通らないことの担保）
    await dispatchPaste('#wtiImageDrop');
    await expect(page.locator('.wti-image-thumb')).toHaveCount(1);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // Task6: 名前候補の確認UI（name_candidates）
  // ==========================================================

  // ケース6: 候補がラジオとして表示され、スコアに応じたラベルが出る
  test('name_candidates がある entry は確度付きラジオとして表示され、スコアに応じたラベルが出る', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sidA = await createStaff(request, shopHdr, `NC1_${RUN_ID}`, '田中太郎');
    const sidB = await createStaff(request, shopHdr, `NC2_${RUN_ID}`, '田中花子');
    const sidC = await createStaff(request, shopHdr, `NC3_${RUN_ID}`, '田仲三郎');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);

    await stubParseImage(page, {
      entries: [{ staff_id: null, staff_hint: '田中', dates: ['2026-08-11'], availability: 'rest', start: null, end: null, raw: '8/11 田中 休み', raw_verified: true }],
      unparsed: [], source: 'llm', ocr_text: '8/11 田中 休み',
      name_candidates: {
        '0': [
          { staff_id: sidA, name: '田中太郎', score: 0.95, reason: '名前が一致' },
          { staff_id: sidB, name: '田中花子', score: 0.72, reason: '姓が一致' },
          { staff_id: sidC, name: '田仲三郎', score: 0.62, reason: '編集距離が近い' },
        ],
      },
    });
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });

    const options = page.locator('.wti-cand-option');
    await expect(options).toHaveCount(3);
    await expect(options.nth(0)).toContainText('田中太郎');
    await expect(options.nth(0)).toContainText('ほぼ一致');
    await expect(options.nth(1)).toContainText('田中花子');
    await expect(options.nth(1)).toContainText('よく似ています');
    await expect(options.nth(2)).toContainText('田仲三郎');
    await expect(options.nth(2)).toContainText('似ているかもしれません');
    await expect(page.locator('.wti-cand-other')).toContainText('その他から選ぶ');
    // どの候補も既定で選択済みになっていないこと（誤配属防止の大前提）
    await expect(page.locator('.wti-cand-radio input:checked')).toHaveCount(0);
    // 候補があるentryでは従来のselectは隠れている（フォールバック用に残るが既定は非表示）
    await expect(page.locator('.wti-unassigned-select')).toBeHidden();
    expect(errors).toEqual([]);
  });

  // ケース7: 同点の候補が複数あっても先頭が選択済みにならない。候補を選ぶと
  //          state.items の staffId が更新され、_wtiEnsureExistingLoaded 経由で
  //          そのスタッフの既存希望が取得され、bulk のペイロードにも載る。
  test('同点の候補は先頭が選択済みにならず、選んだ候補が既存希望取得とbulkに反映される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sidA = await createStaff(request, shopHdr, `NC4_${RUN_ID}`, '鈴木一郎');
    const sidB = await createStaff(request, shopHdr, `NC5_${RUN_ID}`, '鈴木次郎');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);

    await stubParseImage(page, {
      entries: [{ staff_id: null, staff_hint: '鈴木', dates: ['2026-08-12'], availability: 'rest', start: null, end: null, raw: '8/12 鈴木 休み', raw_verified: true }],
      unparsed: [], source: 'llm', ocr_text: '8/12 鈴木 休み',
      name_candidates: {
        '0': [
          { staff_id: sidA, name: '鈴木一郎', score: 0.8, reason: '姓が一致' },
          { staff_id: sidB, name: '鈴木次郎', score: 0.8, reason: '姓が一致' },
        ],
      },
    });
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    await expect(page.locator('.wti-cand-option')).toHaveCount(2);
    await expect(page.locator('.wti-cand-radio input:checked')).toHaveCount(0);

    // 先頭（鈴木一郎）ではなく2番目（鈴木次郎）を選ぶ。誤って先頭が既定選択に
    // なっていた実装なら、この操作をしなくても既に鈴木一郎が選ばれてしまう。
    const existingReq = page.waitForRequest((req) =>
      req.method() === 'GET' && req.url().includes('/api/shop/wishes') && req.url().includes(`staff_id=${sidB}`));
    await page.locator('.wti-cand-option').nth(1).locator('input[type="radio"]').click();
    await existingReq; // _wtiEnsureExistingLoaded の呼び出しが落とされていないことの検証
    await expect(page.locator('.wti-unassigned-row')).toHaveCount(0);

    const bulkRequests = [];
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/bulk'), async (route) => {
      const body = route.request().postDataJSON();
      bulkRequests.push(body);
      await route.fulfill({ json: { ok: true, created: body.wishes.length, skipped: 0, message: 'ok' } });
    });
    await page.click('#wtiSubmitBtn');
    await page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 10000 });
    expect(bulkRequests.length).toBe(1);
    expect(bulkRequests[0].wishes.length).toBe(1);
    expect(bulkRequests[0].wishes[0].staff_id).toBe(sidB);
    expect(errors).toEqual([]);
  });

  // ケース8: 候補が無い entry は従来の <select> にフォールバックする
  test('name_candidates が無い entry は従来のselectにフォールバックする', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    const sid = await createStaff(request, shopHdr, `NC6_${RUN_ID}`, '佐藤四郎');
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);

    await stubParseImage(page, {
      entries: [{ staff_id: null, staff_hint: '不明さん', dates: ['2026-08-13'], availability: 'rest', start: null, end: null, raw: '8/13は不明さんが休みます', raw_verified: true }],
      unparsed: [], source: 'llm', ocr_text: '8/13は不明さんが休みます', name_candidates: {},
    });
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });

    await expect(page.locator('.wti-cand-option')).toHaveCount(0);
    await expect(page.locator('.wti-unassigned-select')).toBeVisible();
    await page.locator('.wti-unassigned-select').selectOption(String(sid));
    await page.waitForTimeout(300);
    await expect(page.locator('.wti-unassigned-row')).toHaveCount(0);
    expect(errors).toEqual([]);
  });
});
