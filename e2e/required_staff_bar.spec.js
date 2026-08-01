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

// レビュー Critical C1: 数値欄の input で行DOMを再構築すると、入力中の
// input 要素自体が破棄されてフォーカスが飛び、2文字目以降が入らなくなる
// （10人以上を打てない）。page.fill() は値を一括設定するため、この
// フォーカス喪失を再現しない。1文字ずつキー入力する pressSequentially で検証する。
test('数値欄に複数桁を1文字ずつ入力できる（10人以上）', async ({ page }) => {
  const inp = page.locator('.rq-count[data-name="早番"]');
  await inp.click();
  await inp.fill('');
  await inp.pressSequentially('12', { delay: 30 });
  await expect(inp).toHaveValue('12');
  // 入力欄の値だけでなく、state とバー側にも反映されていることを見る
  // （レビュー指摘 Minor N3: 表示だけ直って中身が伴っていない可能性）。
  await expect(page.locator('.rq-bar[data-name="早番"] .rq-bar-label')).toContainText('12人');
  // blur しても入力した値のまま（正規化で消えたりしないこと）。
  await inp.blur();
  await expect(inp).toHaveValue('12');
});

// レビュー Important（N1修正の副作用）: 入力中はバー自身の高さしか
// 更新しないため、トラック側の高さ（--rq-lane-h）を再同期しないと、
// 現在の最大人数を超えて入力した瞬間からバーが枠外へ突き抜けたまま
// 固定される（blurがrenderReqBarTrackを呼ばなくなったN1修正の副作用）。
test('人数を増やしてもバーがトラックからはみ出さない', async ({ page }) => {
  // 早番(基本2人)・夜番(基本3人=maxCount)。早番を9人にすると新しい最大人数になる。
  const inp = page.locator('.rq-count[data-name="早番"]');
  await inp.click();
  await inp.fill('');
  await page.keyboard.type('9');
  await page.keyboard.press('Tab');

  const rects = await page.evaluate(() => {
    const t = document.querySelector('#reqBarTrack').getBoundingClientRect();
    const b = document.querySelector('.rq-bar[data-name="早番"]').getBoundingClientRect();
    return { trackTop: t.top, trackBottom: t.bottom, barTop: b.top, barBottom: b.bottom };
  });
  expect(rects.barTop).toBeGreaterThanOrEqual(rects.trackTop - 1);
  expect(rects.barBottom).toBeLessThanOrEqual(rects.trackBottom + 1);
});

// レビュー Important（再々発防止）: syncReqBarTrackHeight は --rq-lane-h だけでなく
// 各バーの bottom（renderReqBarTrack がインラインの静的値として焼き込む）も
// 再計算しないと、重なる時間帯が2件以上あるとき編集中でない側のバーが
// 古いオフセットのまま取り残される。上の「はみ出さない」テストは重ならない
// 仕込み（早番09-17/夜番17-22）で実質1段のため、この問題を検知できない。
// ここでは重なる時間帯を専用に追加して検証する（beforeEach の共通データは変えない）。
test('重なる時間帯で人数を増やしてもバーが縦に重ならない', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await request.post('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
    data: { pattern_name: '中番', start_time: '12:00', end_time: '20:00', required_staff: 2 },
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');
  await expect(page.locator('.rq-bar')).toHaveCount(3);

  // 早番(09-17, 基本2人)を、現在の最大人数（夜番3人）を超える9人まで打つ。
  // Locator.fill/pressSequentially はアクショナビリティ再試行でフォーカス
  // 競合を自己修復することがあるため、click後にtoBeFocusedで前提を固定し、
  // 実際の打鍵は page.keyboard で行う。
  const inp = page.locator('.rq-count[data-name="早番"]');
  await inp.click();
  await expect(inp).toBeFocused();
  await inp.fill('');
  await page.keyboard.type('9');
  await page.keyboard.press('Tab');

  const rects = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).getBoundingClientRect();
    return { asa: get('早番'), naka: get('中番') };
  });
  const overlapsVertically = (a, b) => a.top < b.bottom && b.top < a.bottom;
  // 早番(09-17)と中番(12-20)は時間が重なる → 縦にも重ならないはず。
  // bottom を更新し忘れると、伸びた早番(0〜132px相当)が中番の古いbottom
  // (52px相当)を飲み込み、ここが重なってしまう。
  expect(overlapsVertically(rects.asa, rects.naka)).toBe(false);
});

test('重なる時間帯で人数を減らしても他方のバーがトラックに収まる', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  const headers = { Authorization: `Bearer ${token}` };
  // 早番の基本人数を先に9人にしておく（＝入力ハンドラを経由せず、初期の
  // 全体再描画=renderReqBarTrackの時点でmaxCount=9として全バーのbottomが
  // 一度だけ正しく焼き込まれる状態を作る）。ここでtypeで9まで増やしてから
  // 2まで減らすと、最終maxCountが初期値(3)と偶然一致し、「更新し忘れた
  // 古いbottom」と「本来あるべき値」が同じになって検知できない
  // （実測で確認済み。テスト設計上の罠）。
  const patternsRes = await (await request.get('/api/shop/patterns', { headers })).json();
  const asa = patternsRes.patterns.find((p) => p.pattern_name === '早番');
  await request.put(`/api/shop/patterns/${asa.id}`, {
    headers,
    data: { pattern_name: '早番', start_time: '09:00', end_time: '17:00', required_staff: 9 },
  });
  await request.post('/api/shop/patterns', {
    headers,
    data: { pattern_name: '中番', start_time: '12:00', end_time: '20:00', required_staff: 2 },
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');
  await expect(page.locator('.rq-bar')).toHaveCount(3);

  // ここまでは全体再描画で maxCount=9 として正しく焼き込まれている
  // （中番の bottom もこの時点の laneHeightPx を使って計算済み）。
  // 早番を2人まで減らす、この1アクションだけで再現する。
  const inp = page.locator('.rq-count[data-name="早番"]');
  await inp.click();
  await expect(inp).toBeFocused();
  await inp.fill('');
  await page.keyboard.type('2');
  await page.keyboard.press('Tab');

  const rects = await page.evaluate(() => {
    const t = document.querySelector('#reqBarTrack').getBoundingClientRect();
    const b = document.querySelector('.rq-bar[data-name="中番"]').getBoundingClientRect();
    return { trackTop: t.top, trackBottom: t.bottom, barTop: b.top, barBottom: b.bottom };
  });
  // bottom を更新し忘れると、中番は9人だった頃の高いオフセットのまま残り、
  // トラックが縮んだ分だけ上端からはみ出す。
  expect(rects.barTop).toBeGreaterThanOrEqual(rects.trackTop - 1);
  expect(rects.barBottom).toBeLessThanOrEqual(rects.trackBottom + 1);
});

// レビュー Minor N3: C1 のうち最も危険な経路。空欄のまま離れると
// parseInt('')=NaN → 0（＝Task1の契約で「募集しない」）に誤って
// 保存されかねない。直前の正常値に戻ることを検証する。
test('数値欄を空にしたまま離れると元の値に戻る（0に落ちない）', async ({ page }) => {
  const inp = page.locator('.rq-count[data-name="早番"]');
  await inp.click();
  await inp.fill('');
  await inp.blur();
  await expect(inp).toHaveValue('2');
  await expect(page.locator('.rq-bar[data-name="早番"]')).not.toHaveClass(/rq-zero/);
});

// レビュー Important N1（新規回帰）: blur で #reqBarBody 全体を再描画すると、
// mousedown → blur（再描画で対象ボタンが消える）→ mouseup の順になり、
// 行内ボタンへの1回目のクリックが空振りする。
test('数値欄にフォーカスしてから＋ボタンを押すと人数が増える', async ({ page }) => {
  const row = page.locator('.rq-row').filter({ has: page.locator('.rq-count[data-name="早番"]') });
  const inp = row.locator('.rq-count');
  await inp.click();   // フォーカスするだけで値は変えない
  await row.locator('.rq-step[data-step="1"]').click();
  // 早番の基本値は2人。空振りしていなければ1回のクリックで3人になる。
  await expect(page.locator('.rq-bar[data-name="早番"] .rq-bar-label')).toContainText('3人');
});

// レビュー Important N1: 時間帯Aの欄を打った直後に時間帯Bの欄へ移ってそのまま
// 打つと、blurの全体再描画でフォーカスがbodyに落ちて入力が飲まれる
// （利用者からはC1と同じ症状に見える）。
// 注意: locator.click()/pressSequentially は内部でアクショナビリティの
// 再試行・再フォーカスを行うため、レース1回分のフォーカス消失を
// 自己修復してしまい検知できないことがある（実測済み）。ここでは
// クリック直後に document.activeElement を直接確認し、そのあとは
// フォーカスを取り直さない page.keyboard.type で打鍵することで、
// レビューが実測した「フォーカスがbodyに落ちる」症状そのものを検証する。
test('時間帯Aの欄を打った直後に時間帯Bの欄へ移って打つとBに入る', async ({ page }) => {
  const inpA = page.locator('.rq-count[data-name="早番"]');
  const inpB = page.locator('.rq-count[data-name="夜番"]');
  await inpA.click();
  await inpA.fill('');
  await inpA.pressSequentially('5', { delay: 30 });
  await inpB.click();   // Aからフォーカスが外れ、Bへ移る
  await expect(inpB).toBeFocused();
  // fill() 等の Locator 操作は再フォーカスを含むため使わない。
  // page.keyboard だけで全選択→上書きする。
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.type('7', { delay: 30 });
  await expect(inpB).toHaveValue('7');
});

// レビュー Important I5: 時間帯が重なると不透明なバーが互いを隠して読めなくなる。
// 早番(09:00-17:00)と重なる「中番」(12:00-20:00)を追加し、縦に重ならない
// （別の段に描かれる）ことを実測で確認する。
test('重なる時間帯はバーが縦に重ならない', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await request.post('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
    data: { pattern_name: '中番', start_time: '12:00', end_time: '20:00', required_staff: 2 },
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');
  await expect(page.locator('.rq-bar')).toHaveCount(3);

  const rects = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).getBoundingClientRect();
    return { asa: get('早番'), naka: get('中番') };
  });
  const overlapsVertically = (a, b) => a.top < b.bottom && b.top < a.bottom;
  // 早番(09-17)と中番(12-20)は時間が重なる → 別の段に描かれ、縦にも重ならない
  expect(overlapsVertically(rects.asa, rects.naka)).toBe(false);
});

// レビュー Important I4: 0人は「募集しない」（Task1の契約）。斜線だけでは
// 判別できないため、ラベル自体が「募集しない」に切り替わることを検証する。
test('0人のバーは「募集しない」と表示される', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await request.post('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
    data: { pattern_name: '深夜', start_time: '22:00', end_time: '23:00', required_staff: 0 },
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');

  const bar = page.locator('.rq-bar[data-name="深夜"]');
  await expect(bar).toHaveClass(/rq-zero/);
  await expect(bar.locator('.rq-bar-label')).toContainText('募集しない');
});

// Task4持ち越し: 曜日タブでまだ曜日別上書きが無い（基本値を表示中）欄に
// 入力すると applyReqBarCount が weekday_required に即座に書き込むため、
// state 上はその瞬間から isOverride:true になる。しかしバーの rq-override
// クラスは行DOM生成時にしか出し分けていなかったため、色で上書き中と
// 分からなかった（見た目と state が食い違う）。
test('曜日タブで基本値の欄に入力すると即座にバーが上書き色になる', async ({ page }) => {
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  const inp = page.locator('.rq-count[data-name="早番"]');
  const bar = page.locator('.rq-bar[data-name="早番"]');

  // 入力前は基本値表示（上書きではない）
  await expect(bar).not.toHaveClass(/rq-override/);

  await inp.click();
  await expect(inp).toBeFocused();
  await inp.fill('');
  await page.keyboard.type('5');

  await expect(bar).toHaveClass(/rq-override/);
});

// Task4持ち越し（もう一つ）: 「基本に戻す」ボタン（.rq-reset）も行HTML生成時にしか
// 出し分けていないため、上と同じ操作をしても実際は上書き済みなのにボタンが
// 出てこない。常に描画しておいて hidden を切り替える形にする。
test('曜日タブで基本値の欄に入力すると「基本に戻す」ボタンが即座に出る', async ({ page }) => {
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  const inp = page.locator('.rq-count[data-name="早番"]');
  const pid = await inp.getAttribute('data-pid');
  const reset = page.locator(`.rq-reset[data-pid="${pid}"]`);

  // 入力前はまだ上書きしていないので隠れている
  await expect(reset).toBeHidden();

  await inp.click();
  await expect(inp).toBeFocused();
  await inp.fill('');
  await page.keyboard.type('5');

  await expect(reset).toBeVisible();
});

test('数値欄を変えて保存できる', async ({ page }) => {
  let sent = null;
  await page.route('**/api/shop/patterns/bulk', async (route) => {
    sent = JSON.parse(route.request().postData());
    // レビュー M4: サーバが返す労働時間の警告（9h/13h超）を従来フロントは
    // 読み捨てていた。保存テストの一つにこれを1件混ぜて、toast に実際に
    // 表示されることまで検証する。
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, warnings: [{ pattern_name: '早番', warning: '1シフトが13時間を超えています' }] }),
    });
  });

  await page.fill('.rq-count[data-name="早番"]', '5');
  await page.click('#reqBarSave');
  await expect.poll(() => sent).not.toBeNull();

  const asa = sent.patterns.find((p) => p.pattern_name === '早番');
  expect(asa.required_staff).toBe(5);

  // saveReqBar が (r.warnings || []).forEach(w => toast(`${w.pattern_name}: ${w.warning}`, 'warning'))
  // を呼ぶ経路。モックが常に warnings:[] を返すとこの表示が一度も検証されない
  // （レビュー M4: 「従来フロントが読み捨てていた」まさにその回帰クラス）。
  await expect(page.locator('#toastWrap .toast.warning')).toContainText('早番: 1シフトが13時間を超えています');
});

test('曜日タブで変えた人数は曜日別として送られる', async ({ page, request }) => {
  // レビュー M5: 保存パスが「他曜日の上書きを消さない」ことはコードレビューで
  // 確認済みだが、それを守る自動テストが無かった。ここでは触らない曜日（水曜）
  // に夜番の上書きを先に仕込んでおき、土曜を編集して保存したペイロードに
  // 水曜の上書きがそのまま含まれていることを検証する。
  const loginRes = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await loginRes.json()).token;
  const headers = { Authorization: `Bearer ${token}` };
  const patternsRes = await (await request.get('/api/shop/patterns', { headers })).json();
  const yoru = patternsRes.patterns.find((p) => p.pattern_name === '夜番');
  await request.put(`/api/shop/patterns/${yoru.id}/weekday-required`, {
    headers,
    data: { weekday_required: { '3': 5 } },   // 水曜だけ5人
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');

  let sent = null;
  await page.route('**/api/shop/patterns/bulk', async (route) => {
    sent = JSON.parse(route.request().postData());
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, warnings: [] }) });
  });

  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  await page.fill('.rq-count[data-name="早番"]', '4');
  await page.click('#reqBarSave');
  await expect.poll(() => sent).not.toBeNull();

  const asa = sent.patterns.find((p) => p.pattern_name === '早番');
  expect(asa.required_staff).toBe(2);          // 基本値は変わらない
  expect(asa.weekday_required['6']).toBe(4);   // 土曜だけ4人

  // 一度も触っていない夜番・水曜の上書きが、保存の一括送信で消えていないこと。
  const yoruSent = sent.patterns.find((p) => p.pattern_name === '夜番');
  expect(yoruSent.weekday_required['3']).toBe(5);
});

// レビュー Important I1: タブ切替時の確認ダイアログで「破棄する」を選んでも、
// dirty=false にして再描画するだけでは reqBarState.patterns の編集内容が
// メモリに残ったままになる。後で別の曜日を編集して保存すると、破棄した
// はずの編集も一緒に一括APIへ送られてしまう（メッセージが嘘になる）。
test('曜日タブの確認ダイアログで破棄を了承すると、その曜日の編集は保存に含まれない', async ({ page }) => {
  page.on('dialog', (d) => d.accept());

  let sent = null;
  await page.route('**/api/shop/patterns/bulk', async (route) => {
    sent = JSON.parse(route.request().postData());
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, warnings: [] }) });
  });

  // 土曜タブで早番を4人に変更（このあと破棄するはずの編集）
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  await page.fill('.rq-count[data-name="早番"]', '4');

  // 日曜タブへ移動 → 確認ダイアログで「破棄する」を選ぶ（dialogハンドラでOK）
  await page.click('#reqBarTabs .rq-tab[data-wd="0"]');
  // loadReqBar のフェッチは非同期のため、クリック直後はまだ再描画が終わって
  // いないことがある。日曜は元々上書きが無い曜日なので、基本値(2)に戻る
  // ことを待ってから次の操作をする（fill を即打つと再描画で消される）。
  const inp = page.locator('.rq-count[data-name="早番"]');
  await expect(inp).toHaveValue('2');

  // 日曜側は別の値に変更して保存する
  await page.fill('.rq-count[data-name="早番"]', '7');
  await page.click('#reqBarSave');
  await expect.poll(() => sent).not.toBeNull();

  const asa = sent.patterns.find((p) => p.pattern_name === '早番');
  expect(asa.weekday_required['0']).toBe(7);
  // 破棄したはずの土曜の4人がここに紛れ込んでいないこと。
  expect(asa.weekday_required['6']).toBeUndefined();
});

test('＋ボタンでバーの高さが伸びる', async ({ page }) => {
  const before = await page.evaluate(() =>
    document.querySelector('.rq-bar[data-name="早番"]').getBoundingClientRect().height);
  const pid = await page.getAttribute('.rq-count[data-name="早番"]', 'data-pid');
  await page.click(`.rq-step[data-step="1"][data-pid="${pid}"]`);
  const after = await page.evaluate(() =>
    document.querySelector('.rq-bar[data-name="早番"]').getBoundingClientRect().height);
  expect(after).toBeGreaterThan(before);
});

test('バーを上にドラッグすると人数が増える', async ({ page }) => {
  const box = await page.locator('.rq-bar[data-name="早番"] .rq-drag-top').boundingBox();
  expect(box).not.toBeNull();

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y - 28, { steps: 6 });
  await page.mouse.up();

  const v = await page.inputValue('.rq-count[data-name="早番"]');
  expect(parseInt(v, 10)).toBeGreaterThan(2);
  // レビュー Minor M3: onMove は .rq-count の value を直接書き換えるため、
  // applyReqBarCount（state更新）を呼ばなくても上の inputValue だけを見る
  // アサーションは緑のまま通ってしまい、「ドラッグしても実は保存されない」
  // という致命的な回帰を見逃す。state 由来の描画（refreshReqBarRowVisual
  // が更新するバーラベル）も併せて見て、value と state の両方を守る。
  await expect(page.locator('.rq-bar[data-name="早番"] .rq-bar-label')).toContainText(`${v}人`);
});

test('0人未満にはならない', async ({ page }) => {
  const pid = await page.getAttribute('.rq-count[data-name="早番"]', 'data-pid');
  for (let i = 0; i < 5; i++) await page.click(`.rq-step[data-step="-1"][data-pid="${pid}"]`);
  const v = await page.inputValue('.rq-count[data-name="早番"]');
  expect(parseInt(v, 10)).toBe(0);
});

// Task6: 時間帯そのもの（開始・終了）をバーの左右端ドラッグで15分単位に
// 伸縮できる。上端ハンドル（人数）とは別に .rq-drag-end / .rq-drag-start を持つ。
test('バーの右端をドラッグして時間帯を伸ばせる', async ({ page }) => {
  const before = await page.textContent('.rq-row[data-pid] .rq-row-time');
  const box = await page.locator('.rq-bar[data-name="早番"] .rq-drag-end').boundingBox();
  expect(box).not.toBeNull();

  const trackBox = await page.locator('#reqBarTrack').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  // 右へ 1時間ぶん動かす
  await page.mouse.move(box.x + trackBox.width / 13, box.y + box.height / 2, { steps: 8 });
  await page.mouse.up();

  const after = await page.textContent('.rq-row[data-pid] .rq-row-time');
  expect(after).not.toBe(before);
});

test('時間帯を変えると全曜日に影響する旨が出る', async ({ page }) => {
  await expect(page.locator('.rq-drag-end').first()).toBeVisible();
  const title = await page.getAttribute('.rq-bar[data-name="早番"] .rq-drag-end', 'title');
  expect(title).toContain('全曜日');
});

// レビュー指摘 I4: 早番の終了は元々17:00（分=00）で、00は15の倍数のため、
// 「実は何も更新されていない」壊れ方を `% 15 === 0` だけでは検出できなかった
// （実測確認済み: drag.p.end_time への書き込みを消しても本テストは green のままだった）。
// 実際に値が変わったことも見る。ドラッグ量も、元の `trackBox.width/40` から
// ハンドル半幅を引いた実効移動量がビューポート幅に依存して0になりかねない
// 問題があったため、ハンドル中心からの相対移動量として明示的に30分ぶん動かす。
test('時間帯は15分単位にスナップする', async ({ page }) => {
  const row = page.locator('.rq-row').filter({ has: page.locator('.rq-count[data-name="早番"]') });
  const before = await row.locator('.rq-row-time').textContent();

  const box = await page.locator('.rq-bar[data-name="早番"] .rq-drag-end').boundingBox();
  const trackBox = await page.locator('#reqBarTrack').boundingBox();
  const handleCenterX = box.x + box.width / 2;
  const pxPerMin = trackBox.width / (13 * 60);   // この仕込みは9-22時の13時間軸

  await page.mouse.move(handleCenterX, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(handleCenterX + pxPerMin * 30, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();

  const after = await row.locator('.rq-row-time').textContent();
  expect(after).not.toBe(before);
  const m = after.match(/(\d{2}):(\d{2})\s*〜\s*(\d{2}):(\d{2})/);
  expect(m).not.toBeNull();
  expect(parseInt(m[4], 10) % 15).toBe(0);
});

// レビュー指摘 I1（Spec ❌）: 15分クランプが構造的に死んでおり、開始を跨いで
// 大きくドラッグすると「日またぎ」と誤認して時間帯が反転・膨張してしまう
// バグがあった。夜番(17:00-22:00)の終了ハンドルを自分の開始(17:00)より
// さらに左（軸の左端寄り）まで大きく動かす。ドラッグ中は各ステップで
// 最小幅ガードが働くため、縮む方向には動くが「開始+15分」を下回ることは
// なく、逆転・日またぎ扱いにもならないはず。
test('右端を開始より左へ大きくドラッグしても、時間帯が反転・膨張しない', async ({ page }) => {
  const row = page.locator('.rq-row').filter({ has: page.locator('.rq-count[data-name="夜番"]') });

  const box = await page.locator('.rq-bar[data-name="夜番"] .rq-drag-end').boundingBox();
  const trackBox = await page.locator('#reqBarTrack').boundingBox();
  expect(box).not.toBeNull();

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  // 軸の左端付近（夜番の開始17:00よりずっと左）まで大きく動かす。
  await page.mouse.move(trackBox.x + 2, box.y + box.height / 2, { steps: 10 });
  await page.mouse.up();

  const after = await row.locator('.rq-row-time').textContent();
  const m = after.match(/(\d{2}):(\d{2})\s*〜\s*(\d{2}):(\d{2})/);
  expect(m).not.toBeNull();
  const startMin = parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  let endMin = parseInt(m[3], 10) * 60 + parseInt(m[4], 10);
  if (endMin <= startMin) endMin += 1440;
  // 最小幅15分は下回らない。
  expect(endMin - startMin).toBeGreaterThanOrEqual(15);
  // 開始を跨いだ「日またぎ」誤認（壊れたガードでの実測は16h）は起きない。
  expect(endMin - startMin).toBeLessThan(15 * 60);
  // 開始時刻(17:00)自体は動いていない（このハンドルは終了だけを動かす）。
  expect(`${m[1]}:${m[2]}`).toBe('17:00');
});

// レビュー指摘 I2: ハンドルをバー内側に収めた副作用で、.rq-drag-top/
// .rq-drag-start/.rq-drag-end の3つがDOM順（top→start→end）でヒット優先度が
// 決まり、左右ハンドルが上端ハンドルの実効領域（overflow:hiddenでバー内側の
// 上8pxに切り取られている）を覆ってしまうと、幅24px未満の細いバーで上下
// ドラッグ（人数変更）が一切できなくなる。15分幅のパターンはこのTaskで新たに
// 作れるようになったばかりで、狭い画面ではこれが容易に起こる。
test('細いバー（15分幅など）でも上端ドラッグで人数を変えられる', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await request.post('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
    data: { pattern_name: '深夜', start_time: '22:00', end_time: '22:15', required_staff: 2 },
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');

  // このテストが本当に「細いバー」を検証できているかの前提チェック。
  // ここが崩れるとテストが何も守らなくなる。
  const barWidth = await page.evaluate(() =>
    document.querySelector('.rq-bar[data-name="深夜"]').getBoundingClientRect().width);
  expect(barWidth).toBeLessThan(24);

  const box = await page.locator('.rq-bar[data-name="深夜"] .rq-drag-top').boundingBox();
  expect(box).not.toBeNull();

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y - 28, { steps: 6 });
  await page.mouse.up();

  const v = await page.inputValue('.rq-count[data-name="深夜"]');
  expect(parseInt(v, 10)).toBeGreaterThan(2);
});

// レビュー指摘 I3: 時間帯ドラッグ中は段(data-lane/bottom)を凍結する設計だが
// （installReqBarDrag参照）、pointerup で段が正しく組み直ることを守るテストが
// 無かった（onUp内のrenderReqBarTrack呼び出しを丸ごと消しても全テストが
// green のままだった）。
// 仕込みはbeforeEachの早番(09-17)・夜番(17-22)をそのまま使う。この2つは
// ちょうど17:00で接するだけ（reqBarLanesはend<=startを「空いている」と
// みなす）なのでドラッグ前は同じ段を共有している。
// （最初、別パターン「中番」を追加する設計で試したが、reqBarLanesの貪欲法は
// 段の再利用が時系列で連鎖するため、早番・夜番が隣接しているだけで
// 「早番と重ならない新規パターン」でも早番と別の段に割り当てられてしまい、
// ドラッグ前から段が違う＝再割り当てを1度も経ずにgreenになってしまう
// 落とし穴があった。beforeEachのデータだけで前提が単純に成立するこの形に
// 直した。）
// 早番の終了を17:00→18:00まで伸ばして夜番の領域へ食い込ませ、初めて
// 重なるようにしたうえで、確定後に段が組み直ることを検証する。
test('時間帯ドラッグで重なりが生じると、確定後に段が組み直る', async ({ page }) => {
  // 前提確認: ドラッグ前は早番(09-17)と夜番(17-22)は接するだけで重ならないため
  // 同じ段のはず。
  const lanesBefore = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).dataset.lane;
    return { asa: get('早番'), yoru: get('夜番') };
  });
  expect(lanesBefore.asa).toBe(lanesBefore.yoru);

  const box = await page.locator('.rq-bar[data-name="早番"] .rq-drag-end').boundingBox();
  const trackBox = await page.locator('#reqBarTrack').boundingBox();
  const handleCenterX = box.x + box.width / 2;
  const pxPerMin = trackBox.width / (13 * 60);

  await page.mouse.move(handleCenterX, box.y + box.height / 2);
  await page.mouse.down();
  // 17:00 → 18:00（夜番の領域に1時間食い込む）
  await page.mouse.move(handleCenterX + pxPerMin * 60, box.y + box.height / 2, { steps: 10 });
  await page.mouse.up();

  // ドラッグ中は段を固定しているが、pointerup で再描画され、重なるように
  // なった早番・夜番は別の段に組み直っているはず。
  const rects = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).getBoundingClientRect();
    return { asa: get('早番'), yoru: get('夜番') };
  });
  const overlapsVertically = (a, b) => a.top < b.bottom && b.top < a.bottom;
  expect(overlapsVertically(rects.asa, rects.yoru)).toBe(false);

  const lanesAfter = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).dataset.lane;
    return { asa: get('早番'), yoru: get('夜番') };
  });
  expect(lanesAfter.asa).not.toBe(lanesAfter.yoru);
});

// レビュー指摘 N1: I2修正（左右ハンドルの開始位置を top:8px に固定）の副作用で、
// 0人のバー（reqBarHeightPx(0)=REQ_BAR_MIN_PX=6px）は 8px の起点自体が
// バーの高さを超えるため高さが負→0にクランプされ、左右ハンドルのヒット
// 領域が消えていた（実測: 12×0px）。0人は「その時間帯は募集しない」を
// 明示的にサポートしている状態（rq-zeroクラス。REQ_BAR_MIN_PXのコメントにも
// 「0人のときも掴めるように残す高さ」とある）で、曜日別で0にした曜日タブでは
// その時間帯をまったく変更できなくなる後退だった。
test('0人のバーでも左右ハンドルが掴めて時間帯を変えられる', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  // 早番(09-17)の枠内に収め、軸(9-22時)を変えないようにする。
  await request.post('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
    data: { pattern_name: '休止', start_time: '10:00', end_time: '10:15', required_staff: 0 },
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');

  // 前提確認: このバーが本当に0人（最小高さ＝掴みにくい状態）であることを見る。
  // ここが崩れるとテストが何も守らなくなる。
  const barHeight = await page.evaluate(() =>
    document.querySelector('.rq-bar[data-name="休止"]').getBoundingClientRect().height);
  expect(barHeight).toBeLessThan(8);
  await expect(page.locator('.rq-bar[data-name="休止"]')).toHaveClass(/rq-zero/);

  const row = page.locator('.rq-row').filter({ has: page.locator('.rq-count[data-name="休止"]') });
  const before = await row.locator('.rq-row-time').textContent();

  const box = await page.locator('.rq-bar[data-name="休止"] .rq-drag-end').boundingBox();
  expect(box).not.toBeNull();
  const trackBox = await page.locator('#reqBarTrack').boundingBox();
  const handleCenterX = box.x + box.width / 2;
  const pxPerMin = trackBox.width / (13 * 60);   // この仕込みは9-22時の13時間軸のまま

  await page.mouse.move(handleCenterX, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(handleCenterX + pxPerMin * 30, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();

  const after = await row.locator('.rq-row-time').textContent();
  expect(after).not.toBe(before);
});
