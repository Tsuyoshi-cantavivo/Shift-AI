# 店長のみ運用モードとUI再編 設計書

作成日: 2026-08-12

## 背景

店舗管理者だけがアプリを使い、スタッフはログインしない運用パターンがあることが分かった。
この運用では店長が紙やLINEで集めた希望を代理で入力する。あわせて3件の要望が出た。

1. 希望のテキスト・写真の取り込みが**希望表管理からしか呼べない**。シフト作成画面からも入れたい
2. **AIシフト作成画面がシフト作成画面と重複**している。不要
3. **AIアシスタントはダッシュボードに置いた方がよい**

本設計はこの3件と、そこから直接導かれる画面整理を対象とする。

## 調査で確定した事実（設計の前提）

すべて `file:line` でコード確認済み。

### AI画面の重複

- `SCREENS.aiGenerate`（`public/app.js:2125`）は「シフト作成」「AIアシスタント」の2タブ。
- 「シフト作成」タブの `runGenerate()`（`:2255`）と、シフト画面の `AI生成` ボタンが呼ぶ
  `runShiftGenInline()`（`:2485`）は**どちらも `/shop/shifts/auto` を `dry_run` → プレビュー →
  `draft` 保存する同じ処理**。違いはページかモーダルかだけ。
- `aiGenerate` にしか無い情報は「AIに考慮させる条件」カード（`:2179-2187`）のみ。
  稼働スタッフ数・社員/バイト数・1日最低勤務時間・最大連勤・深夜割増率・シフト時間・時間帯枠数。

### 希望取り込み

- `openWishImportModal(onImported)`（`public/app.js:3230`）は独立した関数で、
  呼び出しは希望表管理の1箇所のみ（`:2997`）。`onImported` は任意のコールバック。
- 取り込みの確定は `POST /api/shop/wishes/bulk` が `shifts` と `wish_history` の両方に書く。
- `GET /api/shop/wishes` は末尾 `LIMIT 500`（`src/app.py:3357` のdocstring）。

### ナビゲーションと通知

- `NAV_DEFS.shop`（`public/app.js:658`）は9項目。`renderNav()`（`:683`）と
  `setActiveNav()`（`:712`）が直接この定数を引いている。
- ヘッダーのベル `#notifBtn`（`public/index.html:82`）は既定で `d-none` だが、
  `refreshNotifBadge()` が shop/staff ロールで `d-none` を外す（`public/app.js:587`）。
  クリックで `openNotifications()` が開く（`:435`）。
  **通知をナビから外してもシステム管理者の一斉通知は読める。**
- `#sideNotifBadge` / `#sideReqBadge` はサイドバー項目に埋め込まれるが、
  `refreshNotifBadge()` は両方とも null ガード済み（`:582, :590`）。
- `showApp()`（`public/app.js:376`）は `renderNav()` を**同期で呼んだ後**に
  非同期の `refreshMyStaffInfo()` を投げる。その後 `Promise.all([ensurePeriod(),
  ensureBusinessHours()])` の完了を待って最初の画面へ遷移する（`:381`）。

### 設定

- `SCREENS.settings`（`public/app.js:4391`）は5タブ:
  シフト設定（実体は必要人数バー）/ シフト時間設定 / 店舗情報 / 募集期間 / パスワード。
- `SETTINGS_KEYS`（`src/utils.py:423`）は `shops.settings` の既知キーの frozenset。
  `validate_known_settings_values()`（`:512`）は**既知キーだけを型検証**し、
  未知キーは後方互換のため素通りさせる（`tests/test_admin_staff_apis.py::test_update_shop_name`
  が `{"new_key": 1}` の保存を200で期待しているため）。
  → 検証を効かせるには `SETTINGS_KEYS` への登録が必須。
- `GET /api/me`（`src/app.py:1022`）は shop ロールで `staff_info` と `is_manager` を返す。

### シフト画面

- `SCREENS.shifts`（`public/app.js:2335`）の上部カードは
  期間2つ＋ボタン6つ（AI生成 / 手動追加 / コピー / 印刷 / 用紙の向き / ドラフトを確定・通知）。
- `loadShortage(box, start, end)`（`:1900`）は `/shop/shifts` を取得して不足を計算し、
  戻り値を返さず box に直接描く。時間帯（`shift_patterns`）が0件のときは
  「時間帯が未設定です」の案内を出して抜ける（`:1912-1920`）。
- AIドラフトの識別は `status === 'requested' && (reason||'').startsWith('AIドラフト')`
  （`public/app.js:1147, 1254`、サーバ側は `src/app.py:2309-2315, 2849`）。

### チャット

- `renderShopChatTab(body)`（`public/app.js:2194`）が `/shop/ai/chat` を叩く。
  1通目は固定の挨拶文「…シフト管理AIアシスタントです。何でもお気軽にどうぞ。」（`:2208`）で情報量ゼロ。
- ダッシュボードは別途 `/shop/ai/review` の `advice` を「AIからの提案」カードに出す
  （`public/app.js:2078, 2089`）。失敗時は `'シフトデータを分析中...'` の初期値のまま固まる。
- `.chat-card` は `min-height: 520px`、PC で `600px`（`public/style.css:1058-1059`）。
  `.chat-messages` は `flex:1; overflow-y:auto`（`:1060`）。

### 既存テストへの影響

- `e2e/fast_navigation.spec.js:53` — ナビ項目リストに `'aiGenerate'` を含む。
- `e2e/capture_mobile.spec.js:167` — `goScreen(page, 'aiGenerate')`。説明書用キャプチャ。
- `tests/test_settings_xss.py:130, 138, 197, 606` — 「AIに考慮させる条件」の
  深夜割増率テンプレートと `#genStart` を名指しでXSS検査している。

## スコープ

含める:

- 運用モード（`staff` / `manager_only`）の設定と、それに応じた画面の出し分け
- `aiGenerate` 画面の削除
- シフト画面の上部カードを4工程のステップバーに再編し、希望取り込みを①に置く
- AIアシスタントをダッシュボードの「AIからの提案」カードに統合

含めない（別件）:

- 用語の統一（「ドラフト」「調整中」「requested」「希望」の混在）
- 設定タブ全体の整理（タブ名が中身と合っていない件）
- 初回セットアップの導線

ただし**設定の「募集期間」タブを `manager_only` で隠す**のは、タブ整理ではなく
運用モードから直接導かれる帰結なので本設計に含める。

## 設計

### 1. 運用モード

`shops.settings` の JSON に `operation_mode` を1キー追加する。テーブル追加もマイグレーションもしない。

| 値 | 意味 |
|---|---|
| `"staff"` | 既定。スタッフもログインして希望を出す |
| `"manager_only"` | 店長だけが使う。希望は店長が代理入力する |

- `src/utils.py`: `SETTINGS_KEYS` に `"operation_mode"` を追加。
  `_SETTINGS_OPERATION_MODES = frozenset({"staff", "manager_only"})` を定義し、
  `validate_known_settings_values()` に `period_mode` と同じ形の分岐を足す。
- `src/app.py`: `GET /api/me` の shop 分岐に `operation_mode` を追加する。
  未設定の店舗は `"staff"` を返す。
  設定画面の `/api/shop/settings` ではなく `/api/me` に載せるのは、
  **ナビの描画に必要**で、専用の往復を増やしたくないため。
- 設定UI: 「店舗情報」タブにラジオ2択を追加する。新タブは作らない（タブ整理は範囲外）。
  説明文に、切り替えると何が画面から消えるかを明記する。

**初回描画のちらつき対策**: `showApp()` の `renderNav()` を
`Promise.all([refreshMyStaffInfo(), ensurePeriod(), ensureBusinessHours()])` の後ろへ移す。
3つは並列に走るので実質の待ち時間は増えず、8項目→6項目に縮む瞬間が消える。

### 2. ナビゲーション

`NAV_DEFS.shop` から `aiGenerate` を削除する（両モード共通）。
`navDefsFor(role)` を新設し、`renderNav()` と `setActiveNav()` は定数ではなくこれを引く。
`manager_only` のとき shop の定義から `myshift` と `notifications` を落とす。

| モード | 項目数 | 中身 |
|---|---|---|
| `staff` | 8 | ダッシュボード / シフト / スタッフ管理 / マイシフト・希望 / 希望表管理 / 人件費分析 / 通知 / 設定 |
| `manager_only` | 6 | ダッシュボード / シフト / スタッフ管理 / 希望表管理 / 人件費分析 / 設定 |

`manager_only` で追加で消すもの:

- ダッシュボードの `qCreq`（変更申請を確認）とシフト画面の `openCreq2`。
  `openChangeRequests()` 自体は `staff` モードで使うので残す
- 設定の「募集期間」タブ
- 確定ボタンの文言。「ドラフトを確定・通知」→「ドラフトを確定」、
  confirm から「スタッフに通知が届きます」の行を落とす。
  **サーバの挙動は変えない**（`/shop/shifts/finalize` は通知行を作り続ける。
  読む人がいないだけで無害であり、ここを変えると既存テストに波及する）

削除するコード: `SCREENS.aiGenerate` / `renderGenerateTab` / `runGenerate` / `aiTab`。

### 3. シフト画面の工程バー

上部カードを差し替える。期間入力（`#sStart` / `#sEnd`）と `#genResult` はIDごと据え置き
（`onPeriodChange` とカレンダー同期がこのIDに依存しているため）。

| # | 名前 | 現状表示 | ボタン |
|---|---|---|---|
| 1 | 希望を集める | 「23件・8名分」／0件なら「まだありません」 | 取り込む → `openWishImportModal` |
| 2 | AIで組む | 「未生成」／「ドラフト18件」 | AI生成 → `runShiftGenInline` |
| 3 | 調整する | 「不足4コマ」／「不足なし」／「時間帯が未設定」 | 手動追加 → `openAddShiftModal` |
| 4 | 確定する | 「18件が未確定」／「確定56件」 | 確定 → 既存 `finalizeDraftBtn` の処理を移設 |

現在地は次の順で最初に当たった1つだけ。同時に2つ光らせない。

```
希望0件      → ①
ドラフト>0   → ④
確定0件      → ②
それ以外     → ③
```

完了マーク: ①は希望>0、②は（確定+ドラフト）>0、④は確定>0かつドラフト0。

コピー・印刷・用紙の向きは `<details>`「その他の操作」に畳む。
レイアウトは PC 4列 / タブレット 2列 / スマホ 1列。

`openWishImportModal` の `onImported` には、シフト画面からは
「工程バーと不足コマとカレンダーを再読込する」関数を渡す
（希望表管理から渡している「一覧フィルタを広げる」関数とは別物）。

**「AIに考慮させる条件」の行き先**: `runShiftGenInline` が開くプレビューモーダル内へ
`<details>` で移す。捨てるとAIが何を見て組んだかが画面から消える。
モーダルには既に「AIの判断理由」があり、判断材料は同じ場所にあるのが自然。

データ取得は `loadStepBar()` を新設し `Promise.all([/shop/wishes, /shop/shifts])` の1往復。
不足コマ数は `loadShortage()` が既に計算しているので、同関数が
`{ gapCount, patternsMissing }` を返すように変えて使い回す（二重計算しない）。

### 4. AIアシスタント

ダッシュボード右カラムの「AIからの提案」カードを「AIアシスタント」カードに置き換える。

- `/shop/ai/review` の `advice` を `window._shopChat` の1通目 assistant 発言として seed する。
  既存の固定挨拶文は廃止。開いた瞬間に中身のある話から始まる
- `renderShopChatTab(body)` を `renderShopChat(container, { seed })` に一般化して流用する。
  送信・サジェストチップ・IME変換中のEnter抑止・thinking表示はそのまま
- ダッシュボード用に `.chat-card-compact`（`min-height: 280px; max-height: 420px`）を足す。
  `.chat-messages` は既に内部スクロールするので、カード側の高さだけ抑える
- `/shop/ai/review` が失敗したら seed を諦め、入力欄だけ使える状態にする。
  現状の「シフトデータを分析中...」で固まる挙動をやめる
- 画面を出入りしても `window._shopChat` が残っていれば会話を保持し、review は叩き直さない

クイック操作カードの `qGen` は行き先の `aiGenerate` が消えるので
`navigateTo('shifts')` に付け替え、ラベルを「シフトを作る」にする。

### 5. データフロー

サーバの新規エンドポイントはゼロ。追加は `/api/me` のフィールド1つと settings のキー1つだけ。

```
紙・LINEの希望
 → ① 取り込む   /shop/wishes/parse(-image) → 確認 → /shop/wishes/bulk
 → ② AI生成     /shop/shifts/auto (dry_run) → プレビュー → (draft) 保存
 → ③ 調整       カレンダーでドラッグ / 手動追加
 → ④ 確定       /shop/shifts/finalize
```

### 6. エラー処理

- 工程バーの各カウントは独立して失敗しうる。取得に失敗したセルは現状表示を「—」にし、
  ボタンは押せるままにする（数字が出ないだけで作業は止めない）
- `/shop/wishes` の `LIMIT 500` に達した場合は「500件以上」と表示する。
  黙って少なく見せない
- 時間帯（`shift_patterns`）が0件のとき、③は「不足なし」ではなく「時間帯が未設定」を出す。
  `loadShortage()` が既に持っている判定を使い、緑のチェックで嘘をつかない
- `/shop/ai/review` の失敗はカードごと落とさず、入力欄を残す

## テスト

### 既存テストの修正（3ファイル）

- `e2e/fast_navigation.spec.js:53` — 項目リストから `'aiGenerate'` を外す
- `e2e/capture_mobile.spec.js:167` — `goScreen(page, 'aiGenerate')` を削除し、
  シフト画面の工程バーのキャプチャに差し替える
- `tests/test_settings_xss.py:130, 138, 197, 606` — 検査対象のテンプレートを
  プレビューモーダル内の新しい位置に付け替える。**エスケープの保証は落とさない**

### 追加するテスト

pytest:

- `operation_mode` に `staff` / `manager_only` 以外を保存しようとすると 400
- `GET /api/me` が shop ロールで `operation_mode` を返す
- `operation_mode` 未設定の店舗は `"staff"` が返る

Playwright:

- `manager_only` でナビが6項目になり、`aiGenerate` が存在しない
- シフト画面の①から希望取り込みモーダルが開く
- 工程バーの現在地が、希望0件のとき①、ドラフトありのとき④になる
- ダッシュボードのAIアシスタントカードに1通目の助言が出て、入力欄から送信できる
