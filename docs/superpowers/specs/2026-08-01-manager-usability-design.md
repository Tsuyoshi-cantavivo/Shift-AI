# 店長ユーザビリティ改善 設計書

作成日: 2026-08-01

## 背景

実際の店長に画面を見せたところ「分かりにくい」との評価だった。あわせて4件の要望・不具合報告があった。

1. シフト希望表の**画像**を貼るだけでテキスト化し、そこから希望を読み込みたい
2. 希望表の名前と登録名はほぼ一致しているので、**ある程度予測してマッピング**してほしい
3. **必要人数の設定画面**が使いにくく表示も分かりにくい。シフト表と同じような**バー**で設定したい（数値入力も残す）
4. シフトの**印刷で「横」に変更したら表示がまったくされなかった**

本設計はこの4件と、調査で判明した「分かりにくさ」の根本原因の一部を対象とする。

## 調査で確定した事実（設計の前提）

すべて `file:line` でコード確認済み。

### 印刷

- アプリに縦／横の切替UIは**存在しない**。`@page` はCSS全体で1箇所のみ、`public/style.css:1203` の `@page { size: A4 landscape; margin: 10mm; }`。**既定ですでに横向き**。
- `#printView`（`public/index.html:101`）は印刷ボタン押下時にのみ構築され（`public/app.js:1144`）、`afterprint` で `innerHTML = ''` にされる（`public/app.js:975-978`）。**`beforeprint` による再構築は存在しない**。
- `@media print` が `#appView` を `display: none !important` にする（`public/style.css:1221-1222`）ため、`#printView` が空だと紙面は完全な白紙になる。
- 画面用の `.tl-wrap { overflow-x: auto }`（`public/style.css:859`）と `.tl-axis-row / .tl-row { min-width: 480px }`（`public/style.css:860, 892`）は印刷ブロックで上書きされておらず、印刷時も生きている。
- 唯一の印刷E2E（`e2e/timeline_visual.spec.js:117-161`）は `window.print = () => {}` で潰しており（`:125`）、`afterprint` が発火しないため本バグを構造的に検出できない。
- `tests/test_design_tokens.py:272-273` は `css.split("@media print")[0]` で印刷CSSを検査対象外にしている。`@media print` は2箇所（`style.css:1015` と `1202`）あるため、**1016〜1201行目の画面用CSSまで検査から漏れている**。

### 必要人数

- `shift_patterns`（`schema.sql:45-53`）が時間帯と基本必要人数、`shift_pattern_weekday_required`（`schema.sql:56-71`）が曜日別の上書き。**時間帯（start/end）は全曜日共通**で、曜日ごとに変わるのは人数のみ。日付単位の必要人数を持つテーブルは存在しない。
- `required_staff` は「必要人数（下限）」と「配置上限（cap）」を**兼ねている**。専用の列はない。
- `cap_ok()` は `required <= 0` のスロットを「上限なし」として扱う（`src/shift_engine.py:343`）。
- `POST/PUT /api/shop/patterns` は `required_staff` を `req or 1` で処理するため（`src/app.py:1901, 1914`）、**0 を送ると 1 に戻る**。一方 `PUT .../weekday-required` は 0 を保存できる（`src/app.py:1944-1947`）。
- 現行UIの保存処理は画面の文字列 `"09:00 - 22:00"` を `' - '` で split して復元している（`public/app.js:4141-4145`）。表示形式を変えると保存が壊れる。
- 保存はパターンごとに直列2リクエスト（`public/app.js:4138-4155`）。N件で2N回。
- サーバの `_validate_pattern_hours()`（`src/app.py:1858-1888`）は 9h/13h 超で `warning` を返すが、**フロントは読み捨てている**（`public/app.js:4174`）。
- 流用可能な資産: `.tl-*` 一式（`public/style.css:817-1015`）、座標計算（`public/app.js:1000-1011, 1035-1040`）、`installDraftTimelineDrag`（`public/app.js:1159-1324`）、`ensureBusinessHours()`（`public/app.js:756-787`）、`.tl-hour:last-child` の絶対配置トリック（`public/style.css:874-890`）。

### 希望取込

- `POST /api/shop/wishes/parse`（`src/app.py:3203-3252`）は解析のみでDBに書かない。確定は `POST /api/shop/wishes/bulk`（`src/app.py:3314-3512`）が `shifts` と `wish_history` の**両方**に書く。
- `_post_llm()`（`src/ai.py:75-101`）は `messages` を無検査でJSONシリアライズするだけ。**`content` を配列にすれば vision リクエストがそのまま通る**。`timeout=30` 固定、リトライなし。
- `LLM_MODEL` 既定は `gpt-4o-mini`（`.env.example:39`）＝画像入力対応。
- 依存は `requests` のみ。Pillow / openai SDK は無い（`requirements.txt`）。
- `_sanitize_llm_wish_result()`（`src/ai.py:1394-1463`）が entries/unparsed の契約を守る。**検証で落ちた entry は捨てず必ず unparsed に積む**のが既存の原則。
- `raw_verified`（`src/app.py:3174-3200`）は「貼付テキストに実在する文か」を照合し、AIの創作を検出する安全弁。照合元テキストが前提。
- 画像アップロード実装はプロジェクト内に**皆無**（`input[type=file]` / `FileReader` / `FormData` / `MAX_CONTENT_LENGTH` すべて0件）。
- `api()`（`public/app.js:42-53`）は `Content-Type: application/json` 決め打ち。

### 名前照合

- `by_name.get(hint)` の**完全一致のみ**（`src/app.py:3238, 3251`）。正規化なし。
- `staffs` の名前関連列は `name` の1つだけ（`schema.sql:27-42`）。kana / nickname は無い。
- `_extract_staff_hint()`（`src/ai.py:1067-1087`）は候補が2件以上ヒットしたら `None` を返す。この「名簿順で勝手に決めない」不変量は `tests/test_wish_text_import.py:502` が固定している。

### UX

- 新規店舗は `shift_patterns` も `shift_request_periods` もゼロ件で始まる（`src/admin_api.py:225-244`）。
- 時間帯が0件だと `_computeHourlyGaps()` が `[]` を返し（`public/app.js:798-799`）、`loadShortage()` が**「不足なし — 全時間帯充足」**と表示する（`public/app.js:1783`）。
- 募集期間の日付欄はDBではなく計算値 `calc_next_period()` を返す（`public/app.js:693` → `src/app.py:2009`）ため、募集期間を1件も作っていなくても店長画面は正常に見える。スタッフ側だけ提出ボタンが `disabled` になる（`public/app.js:4483, 4491, 4497`）。
- `POST /shop/shifts/finalize` は期間内の `requested` を**全部** confirmed にする（`src/app.py:2176-2179`）。AIドラフト（`status='requested'` + `reason='AIドラフト...'`、`src/app.py:2069`）だけでなく**スタッフが提出した希望も確定・通知される**。ボタン名は「ドラフトを確定・通知」（`public/app.js:2136`）。
- `openModal()` は `onSave` が無いと `saveLabel` を使わない（`public/app.js:279-282`）ため、閲覧専用モーダルの唯一のボタンが「キャンセル」と表示される（例: `public/app.js:2946`）。

### テスト

- pytest 1109 passed / 1 skipped、**13.3秒**。E2E は Playwright 18 spec、`workers: 1` 直列。
- **CI は存在しない**（`.github/` なし）。
- 必要人数設定のUIテストは0件。印刷テストは実質0件。

## スコープ

決定事項（本設計の確定事項）:

- 画像の取込方法は**貼り付け・ファイル選択/D&D・スマホ撮影の3方式すべて**を実装する
- 取り込んだ画像は**保存しない**
- 名前マッピングは**自動で候補を当て、確認画面で人が承認**する（自動確定はしない）
- 必要人数のバーUIは**曜日タブ＋1日を大きく表示**
- 人数は**バーの上下ドラッグ＋数値欄**の双方向
- 時間帯は**バーの左右ドラッグで伸縮**できるようにする
- 印刷の**縦／横切替をアプリ内に新設**する
- UX横断は**「嘘の表示」と危険な操作**に限定する
- Phase ごとに区切り、各Phase完了時にテスト結果と画面を確認する

スコープ外:

- 用語の全面統一（「不足枠／不足コマ／あと〇名」など）
- AI生成の入口が2つある問題の統合
- 初回セットアップのオンボーディング新設
- ダブルタップ導線の再設計
- 日付単位の必要人数（スキーマ追加が必要）
- 必要人数と配置上限の分離（スキーマ追加が必要）

## Phase 0 — 土台

### 0-1. CI

`.github/workflows/test.yml` を新設する。

- `push` と `pull_request` で起動
- Python 3.10 で `pip install -r requirements.txt` → `pytest tests/ -q`
- `node --check public/app.js` と `node --check public/admin.js`
- pytest は13秒で終わるため、コストは無視できる

E2E（Playwright、約1.5分）はこの段階ではCIに含めない。手動実行のまま。

### 0-2. 印刷CSSをテスト対象に入れる

`tests/test_design_tokens.py:272-273` の `css.split("@media print")[0]` を廃し、画面用CSSと印刷用CSSを正しく分離するヘルパに置き換える。現状 `@media print` が2箇所あるため 1016〜1201行が検査から漏れている点を修正する。

そのうえで印刷CSSに対する新規テストを追加する。

- `@page` ルールが定義されていること
- `.print-view` が `@media print` 内で `display: block` になっていること
- `#appView` を非表示にするルールが存在すること

## Phase 1 — 印刷の修正

### 1-1. 根本原因

`#printView` の生存期間が「印刷ボタンのクリック 〜 最初の `afterprint`」に限定されている。ブラウザの印刷プレビューは向き・用紙・倍率・余白を変更するたびにライブDOMから再レンダリングするため、2回目以降のレンダリングは空のDOMを印刷する。`@media print` が `#appView` を消しているので代替表示もなく、紙面は完全な白紙になる。

同じ欠陥により、以下もすべて白紙になる。

- 印刷プレビューで向きや用紙を変更したとき
- Ctrl+P / ブラウザメニューからの印刷
- 「システムダイアログを使用して印刷」
- 2回目以降の印刷ボタン押下（`afterprint` 後、次のクリックまでの間）

### 1-2. 修正

**印刷データを状態として保持し、`beforeprint` で再構築する。**

- `appState.printPayload = { start, end, html }` に直近の印刷内容を保持する
- `beforeprint` リスナで `#printView` が空なら `printPayload.html` から再構築する
- `afterprint` では `#printView` をクリアしない（画面表示には `.print-view { display: none }`（`public/style.css:1194`）が効いているため、残しても画面には出ない）
- `openPrintView()` は payload を更新してから `window.print()` を呼ぶ

これにより、印刷ボタンを一度でも押していれば、その後の任意の再レンダリング経路で内容が保たれる。

### 1-3. 縦／横切替の新設

印刷ボタンの隣に「縦／横」のトグルを置く。

- 選択値は `localStorage` に保持し、次回も同じ向きで開く
- `@page { size }` を切り替えるため、`<style id="printPageRule">` を `document.head` に持ち、選択に応じて `A4 landscape` / `A4 portrait` を書き込む
- `#printView` に `data-orientation="portrait|landscape"` を付け、CSSで縦向き時のレイアウトを調整する
  - `.tl-name` を狭める（横: 72px → 縦: 56px）
  - 時間軸のフォントを縮める
  - 1ページあたりの日数配分は変えない（1日1ページのまま）

### 1-4. クリップの解消

印刷ブロック（`public/style.css:1250` 付近）に以下を追加する。

- `.print-page .tl-wrap { overflow-x: visible; }`
- `.print-page .tl-axis-row, .print-page .tl-row { min-width: 0; }`

これがないと、縮小率や用紙サイズによって印刷ビューポート幅が 480px を下回った際に帯の右側が切り落とされる。

### 1-5. テスト

先に失敗する再現テストを書く。

- **E2E（新規 `e2e/print_view.spec.js`）**
  - `window.print` を潰さず、`page.emulateMedia({ media: 'print' })` を使う
  - 印刷ボタン → `#printView` に内容がある
  - `beforeprint` を手動 dispatch した後も `#printView` が空でない（**現行実装では失敗する**）
  - `afterprint` を dispatch した後に再度 `beforeprint` を dispatch しても空でない
  - 縦／横トグルで `data-orientation` と `@page` ルールが切り替わる
- **pytest（`tests/test_print_view.py` 新規）**
  - `public/style.css` の印刷ブロックに `overflow-x` と `min-width` の解除が入っていること
  - `public/app.js` に `beforeprint` リスナが存在すること（構造的回帰防止）

## Phase 2 — 必要人数のバーUI

### 2-1. 画面構成

設定 → 「シフト設定」タブを作り直す。

```
 [基本][日][月][火][水][木][金][土]

 土曜日                          ※時間帯の変更は全曜日に反映されます
    9  10  11  12  13  14  15  16  17  18  19  20  21  22
   ┌────────────────────┐  ┌──────────────────┐
 3 │████████ 早番 ███████│  │██████████████████│ 4
 2 │████████████████████│  │██████████████████│
 1 │████████████████████│  │████ 夜番 ████████│
   └────────────────────┘  └──────────────────┘
      [-] 3 人 [+]              [-] 4 人 [+]

   [時間帯を追加]                              [保存]
```

- 「基本」タブは `shift_patterns.required_staff`（曜日上書きが無い曜日に適用される既定値）を編集する
- 日〜土タブは `shift_pattern_weekday_required` を編集する。上書きが無い曜日は基本値をグレーで表示し、編集すると上書きとして確定する
- 「基本に戻す」ボタンで曜日上書きを削除できる

### 2-2. バーの描画と操作

- 時間軸は `ensureBusinessHours()` の `appState.businessHours` を使い、シフト表と同じレンジ（拡張時間モデル 0〜2880分）にする
- `--tl-hours` によるグリッド背景と `.tl-hour:last-child` の絶対配置トリックを移植する。**これを移植しないと目盛りが1時間ズレる**
- **バーの高さ = 必要人数**。1人あたり固定px（14px）。上端のドラッグハンドルを上下に動かして増減
- 左右端のドラッグハンドルで時間帯を伸縮。15分スナップ。`installDraftTimelineDrag` の Pointer Events / `setPointerCapture` / ロングプレス開始（スクロールと共存）/ 範囲クランプ / 最小幅の実装パターンを流用する
- **左右ハンドルは色を変える**（時間の変更＝全曜日に影響することを示す）。時間帯を変更したら「時間帯を変更しました（全曜日に反映されます）」とトーストを出す
- バー直下に `[-] 3 人 [+]` と数値入力欄。バーと双方向に同期する
- バーをタップすると時間帯名の編集ができる（インライン、モーダルは開かない）

### 2-3. 保存

現行の「DOM文字列を再パースして行ごとに直列2リクエスト」を廃止する。

新規エンドポイント `PUT /api/shop/patterns/bulk`:

```json
{
  "patterns": [
    {"id": 1, "pattern_name": "早番", "start_time": "09:00", "end_time": "17:00",
     "required_staff": 2, "weekday_required": {"0": 3, "6": 3}}
  ]
}
```

- 既存の `_validate_pattern_hours()` を各パターンに適用し、`warnings` を配列で返す
- `weekday_required` は既存の「全件DELETE → INSERT」置換方式を踏襲する
- 1件でも検証に失敗したら全体をロールバックし、どのパターンが原因かを返す
- 既存の個別エンドポイント（`POST` / `PUT` / `DELETE` / `weekday-required`）は残す（他から使われている可能性と、テスト `tests/test_app.py:861-918` を壊さないため）

フロントは state オブジェクトから送る。DOM の textContent は読まない。

### 2-4. 併せて直す既存バグ

**バグA: 基本必要人数に 0 を保存できない**

`src/app.py:1901, 1914` の `req or 1` により、0 が 1 に戻る。曜日別（`src/app.py:1944-1947`）は 0 を保存できるため非対称。`validate_numeric_field()` を通した後、`None` と `0` を区別して扱うよう修正する。

**バグB: 必要人数 0 が「上限なし」になる**

`cap_ok()` は `required <= 0` を「上限なし」として扱う（`src/shift_engine.py:343`）。一方 UI の注記は「0＝その曜日は募集しない」（`public/app.js:4092`）。**意味が正反対**。曜日別に 0 を設定できる現状でも踏める。

修正方針: `required == 0` は「配置しない（上限0）」として扱い、「上限なし」は `required` が `None`（＝そのスロットにパターンが1つも無い）でのみ成立させる。`_day_requirements()` はパターンが被らないスロットにキーを作らないため、`req_map` に存在しないスロット＝制約なし、`req_map[slot] == 0` ＝配置禁止、と分離できる。

このバグは配置ロジックの中核に触れるため、`tests/run_tests.py` の不変量テストと `test_over_cap_boundaries.py` / `test_over_cap_finalize.py` を修正前後で必ず通す。

**バグC: 労働時間の警告が捨てられている**

`_validate_pattern_hours()` が返す `warning`（9h超・13h超）をフロントがトーストで表示する。

### 2-5. テスト

- **pytest**: `PUT /api/shop/patterns/bulk` の正常系・部分失敗ロールバック・検証エラー。`required_staff = 0` が保存されること。`cap_ok()` の 0 の扱い（新規 `tests/test_required_zero.py`）
- **pytest（既存の保護）**: `tests/run_tests.py` の不変量 T1-T6、`test_over_cap_*.py` が引き続き通ること
- **E2E（新規 `e2e/required_staff_bar.spec.js`）**: 曜日タブ切替、数値欄で変更 → 保存 → `page.route` で PUT ボディを傍受して検証、バーの高さが人数に追随すること
- **unit（`helpers.run_js`）**: 人数↔バー高さの変換関数と、時間↔px の変換関数を純関数として切り出し、Node で直接テストする

## Phase 3 — 画像取込と名前マッピング

### 3-1. サーバ: 画像解析API

`POST /api/shop/wishes/parse-image`

リクエスト（JSON。`api()` を改造せずに済ませるため base64 を JSON に載せる）:

```json
{
  "images": ["data:image/jpeg;base64,..."],
  "year_month": "2026-08",
  "staff_id": null
}
```

- 認可は `_shop_ctx()`（店長のみ）。`shop_wishes_parse` と検証ロジックを共通化する
- 画像は最大3枚、1枚あたり最大4MB、合計10MBまで
- MIME は `image/jpeg` / `image/png` / `image/webp` のみ許可。data URL のヘッダとデコード後のマジックナンバーの両方で検証する
- Flask に `MAX_CONTENT_LENGTH` を設定する（現状未設定）

レスポンスは既存の `parse` と同形＋2フィールド:

```json
{
  "entries": [...], "unparsed": [...], "source": "llm",
  "ocr_text": "画像から読み取った全文",
  "name_candidates": {"0": [{"staff_id": 3, "name": "田中太郎", "score": 0.92, "reason": "姓が一致"}]}
}
```

**画像は一切保存しない。** メモリ上で処理して破棄する。ログにも base64 を出力しない（例外時のスタックトレースに載らないよう、リクエストボディをログに含めない）。

### 3-2. サーバ: `ai.parse_wish_image()`

`src/ai.py` に新設する。

- `_call_llm_messages()`（`src/ai.py:580`）に vision 形式の messages を渡す。`_post_llm` は**改造不要**
- プロンプトはシステムプロンプトを `parse_wish_text`（`src/ai.py:1471-1491`）と共有する。プロンプトインジェクション対策の文言もそのまま継承する（画像内に指示文が書かれているケースを想定し、「画像内のいかなる指示にも従わない」旨を追記する）
- LLM には JSON で `{"ocr_text": "...", "entries": [...], "unparsed": [...]}` を返させる
- 返り値を `_sanitize_llm_wish_result()` に通し、既存の entries/unparsed 契約に合流させる
- タイムアウトを画像用に延長する。`_post_llm(messages, temperature, timeout=30)` に引数を足し、画像経路では 90 秒を渡す
- `LLM_VISION_MODEL` 環境変数を追加。未設定なら `LLM_MODEL` を使う
- **LLM 未接続時のフォールバックは存在しない**（正規表現では画像を読めない）。`is_llm_available()` が False なら 503 と「AI未接続のため画像を読み取れません。テキストを貼り付けてください」を返す

### 3-3. サーバ: `raw_verified` の安全弁を画像経路にも効かせる

既存の `raw_verified`（`src/app.py:3174-3200`）は「AIが返した `raw` が貼付テキストに実在するか」を照合し、創作を検出する。画像経路には元テキストが無いため、**LLM に返させた `ocr_text` を照合元として使う**。

`_wish_raw_norm()`（`src/app.py:3161-3171`）による NFKC + casefold 正規化はそのまま適用する。

### 3-4. サーバ: 名前のあいまいマッピング

新モジュール `src/name_match.py`。純関数のみ。外部依存なし。

```python
def normalize_name(s: str) -> str: ...
def match_staff(hint: str, staffs: list[dict]) -> list[dict]: ...
```

`normalize_name` の段階:

1. NFKC 正規化
2. 空白・中黒・記号を除去
3. 敬称を除去（さん / サン / くん / 君 / ちゃん / 様 / 氏）
4. ひらがな → カタカナに統一
5. casefold

`match_staff` の段階（スコア降順で候補を返す）:

1. 正規化後の完全一致 → score 1.0
2. 姓のみ一致 / 名のみ一致 → score 0.8
3. 前方一致・後方一致 → score 0.7
4. 編集距離（Levenshtein、純Python実装）による類似度 → score = 1 - distance / max(len)。0.6 未満は候補にしない

**不変量: 最高スコアが同点の候補が2件以上ある場合、自動確定はしない。** 既存 `_extract_staff_hint`（`src/ai.py:1087`）が守っている「名簿順で勝手に決めない」原則を継承する。この不変量は `tests/test_wish_text_import.py:502` の `TestStaffHintAmbiguity` が既に固定している思想。

適用先:

- `POST /api/shop/wishes/parse`（テキスト経路）— `by_name.get(hint)` の完全一致（`src/app.py:3251`）を、`match_staff` の**スコア 1.0（正規化完全一致）のみ自動確定**に置き換える。それ未満は `staff_id = None` のまま `name_candidates` に候補を載せる
- `POST /api/shop/wishes/parse-image`（画像経路）— 同じ

これにより既存のテキスト取込も表記ゆれに強くなる。**自動確定の条件は「正規化して完全一致」に限定**するため、既存テストの期待値は変わらない。

### 3-5. フロント: 画像入力UI

`_wtiRenderStep1()`（`public/app.js:3110-3133`）の `#wtiText` の上に画像ゾーンを追加する。

```
┌─────────────────────────────────────┐
│   📷 希望表の画像をここに貼り付け      │
│                                     │
│   [ファイルを選ぶ]  [写真を撮る]      │
│   ドラッグ＆ドロップも可              │
└─────────────────────────────────────┘
      ↓ 選択後はサムネイル一覧＋削除ボタン

      ─── または ───

  [ テキストを貼り付ける textarea ]
```

3方式すべてを実装する。

- **貼り付け**: モーダルに `paste` リスナ。`e.clipboardData.items` から `type.startsWith('image/')` を拾う
- **ファイル選択 / D&D**: `<input type="file" accept="image/*" multiple>` と、ゾーンへの `dragover` / `drop`
- **撮影**: 別の `<input type="file" accept="image/*" capture="environment">`。スマホでカメラが直接起動する。PCでは非表示（`isPC()` で判定）

**送信前にクライアント側でリサイズする。** canvas で長辺 1600px に縮小し、JPEG 品質 0.85 で再エンコードしてから base64 化する。通信量・LLMトークン・タイムアウトの3つに効く。

`_wtiParse()`（`public/app.js:3135`）を分岐させる。画像があれば `/shop/wishes/parse-image`、無ければ従来の `/shop/wishes/parse`。**ステップ2以降（`_wtiFlatten` / カレンダー確認 / `_wtiSubmit` → `bulk`）は変更しない。**

画像経路のときはステップ2の冒頭に「画像から読み取った文章」を折りたたみで表示し、店長が原文と照合できるようにする（既存設計書 `2026-07-26-wish-text-import-design.md` §6「元の文を必ず見せる」の原則を継承）。

### 3-6. フロント: 名前マッピングの確認UI

`_wtiRenderUnassigned()`（`public/app.js:3530-3542`）を強化する。

現状は素の `<select>` にスタッフ全員が並ぶだけ。これを次のようにする。

```
「田中」さんの希望が未割り当てです
  ● 田中太郎   よく似ています
  ○ 田中花子   よく似ています
  ○ その他から選ぶ ▼
```

- `name_candidates` の候補をスコア降順でラジオとして並べる
- score >= 0.9 は「ほぼ一致」、0.7〜0.9 は「よく似ています」、0.6〜0.7 は「似ているかもしれません」と表示する
- 候補が無い場合は従来の `<select>` にフォールバック
- **候補が同点で複数ある場合は先頭を選択済みにしない**（店長に必ず選ばせる）

### 3-7. テスト

- **pytest（`tests/test_name_match.py` 新規）**: `normalize_name` の各段階、`match_staff` のスコア順、同点2件で自動確定しないこと、既存の完全一致が壊れないこと
- **pytest（`tests/test_wish_image_import.py` 新規）**: `call_llm` / `_call_llm_messages` を monkeypatch し、既存の `_use_llm()` パターン（`tests/test_wish_text_import.py:559-562`）を画像版に拡張。不正JSON・空レスポンス・`ocr_text` 欠落・巨大画像・非画像MIME・LLM未接続時の503
- **pytest**: `MAX_CONTENT_LENGTH` 超過が 413 を返すこと
- **E2E（`e2e/wish_image_import.spec.js` 新規）**: `stubParse` と同型で `/shop/wishes/parse-image` をスタブし、ファイル選択 → ステップ2遷移 → 名前候補の表示 → `bulk` のボディ傍受。**実画像をLLMに投げない**

## Phase 4 — 嘘の表示と危険な操作

範囲を限定する。用語統一・導線再設計・オンボーディング新設は行わない。

### 4-1. 「全時間帯充足」の誤表示

時間帯が0件のとき、緑のチェックマークで「不足なし — 全時間帯充足」と表示される（`public/app.js:1783`）。新規店舗が最初に見る画面がこれ。

`_computeHourlyGaps()` が「時間帯0件」と「本当に不足なし」を区別できるよう、戻り値に状態を持たせる。時間帯0件のときは次を表示する。

> ⚠ 時間帯が未設定です。設定 → シフト設定で登録すると、不足がここに表示されます。 [設定を開く]

同じ判定をダッシュボードのKPI（`public/app.js:1857`）とAI作成画面（`public/app.js:1965-1975`）にも適用する。

### 4-2. 募集期間が未作成であることを店長に見せる

現状、募集期間を1件も作っていなくても店長画面の日付欄は計算値で埋まるため正常に見える。一方スタッフは提出できない。

シフト画面とAI作成画面の冒頭に、募集期間が0件のときだけ警告バーを出す。

> ⚠ 募集期間が未設定のため、スタッフは希望を提出できません。 [募集期間を設定]

判定は既存の募集期間APIを使う。`calc_next_period()` の計算値ではなく、`shift_request_periods` の実レコード件数を見る。

### 4-3. 「ドラフトを確定・通知」の実挙動と説明を一致させる

このボタンは期間内の `requested` を全部 confirmed にするため、AIの下書きだけでなく**スタッフが提出した希望も確定・通知される**（`src/app.py:2176-2179`）。全スタッフに通知が飛ぶ不可逆操作。

**挙動は変えない**（変更は影響範囲が大きく、別途の設計が必要）。代わりに、押す前に何が起きるかを正確に見せる。

- ネイティブ `confirm()`（`public/app.js:2234`）をやめ、確定対象の内訳を出すモーダルにする

  > 以下を確定し、スタッフ全員に通知します。
  > ・AIが作成した下書き: 42件
  > ・スタッフが提出した希望: 8件
  > 合計 50件。この操作は取り消せません。

- 内訳は既存APIから取得できる（`reason` が `AIドラフト` 始まりかで分類）。dry-run 用のクエリを追加するか、画面が既に持っているシフト一覧から集計する
- ボタンのラベルと `title` を実挙動に合わせる

### 4-4. 閲覧専用モーダルのボタンが「キャンセル」

`openModal()` は `onSave` が無いと `saveLabel` を使わない（`public/app.js:279-282`）ため、`saveLabel: '閉じる'` を渡している呼び出し（`public/app.js:2946` ほか）が無視され、読むだけのモーダルの唯一のボタンが「キャンセル」になる。

`onSave` が無いときはキャンセルボタンのラベルに `saveLabel`（既定「閉じる」）を使うよう修正する。影響は全モーダル共通のため、既存の呼び出し40箇所すべてで表示を確認する。

### 4-5. テスト

- **pytest**: 時間帯0件のときの不足判定（`helpers.run_js` で `_computeHourlyGaps` を直接実行）
- **pytest**: 募集期間0件の判定API
- **E2E**: 時間帯0件の新規店舗で「全時間帯充足」が出ないこと、警告が出ること
- **E2E**: 閲覧専用モーダルのボタンが「閉じる」であること

## 実装順序と確認ポイント

| Phase | 内容 | 完了時に見せるもの |
|---|---|---|
| 0 | CI・印刷CSSのテスト土台 | CI の実行結果、pytest 全緑 |
| 1 | 印刷の白紙バグ修正、縦／横切替 | 再現テストが赤→緑になった記録、縦横それぞれの印刷プレビュー画像 |
| 2 | 必要人数バーUI、必要人数0のバグ2件 | 新UIのスクリーンショット、不変量テストの結果 |
| 3 | 画像取込、名前マッピング | 取込フローのスクリーンショット、名前候補の表示 |
| 4 | 嘘の表示と危険な操作 | 修正前後の画面比較 |

各Phaseの完了時点で pytest 全緑と `node --check` を確認する。Phase 2 は配置ロジックに触れるため、`tests/run_tests.py` の不変量テストも必ず通す。

## リスク

| リスク | 対処 |
|---|---|
| Phase 2 の「必要人数0」修正が配置ロジックを壊す | 修正前に `cap_ok` / `_day_requirements` の現挙動を固定するテストを書き、修正後に不変量テスト（`tests/run_tests.py` T1-T6）と `test_over_cap_*.py` を通す |
| バーUIが既存タイムラインと座標計算がズレる | `.tl-hour:last-child` の絶対配置トリックを必ず移植する。px/分の変換関数を純関数として切り出し `helpers.run_js` でテストする |
| 画像OCRの精度が実用に足りない | 確認画面で必ず人が承認する設計のため、誤読は登録前に止まる。`ocr_text` を折りたたみで見せ、原文照合を可能にする |
| vision の応答が遅くタイムアウトする | クライアント側で長辺1600pxに縮小、タイムアウトを90秒に延長 |
| base64 画像が想定外の場所（ログ・監査ログ）に残る | 例外ハンドラでリクエストボディをログに含めない。`audit()` に画像を渡さない |
| Phase 4-3 の内訳集計が重い | 既に画面が持っているシフト一覧から集計し、追加のAPI呼び出しを避ける |
