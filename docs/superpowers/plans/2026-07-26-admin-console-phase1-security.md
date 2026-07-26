# システム管理者コンソール Phase 1（緊急修正）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 未認証で他店舗のデータを改変できる穴、管理者がパスワードを変更できない状態、店舗名を消すトグルバグを塞ぐ。

**Architecture:** 既存の `src/app.py` 単一ファイル構成のまま、認証ガードの追加・所属検証の追加・入力検証の追加を行う。新規ファイルは作らない（ファイル分割は Phase 2 で行う）。テーブル追加は `login_attempts` 1つだけで、`ensure_db()` の `CREATE TABLE IF NOT EXISTS` パターン（`src/app.py:4097-4106` の `audit_logs` と同じ形）で作る。

**Tech Stack:** Python 3 / Flask / SQLite（本番は Cloudflare D1 REST API）/ pytest / Vanilla JS

**設計書:** `docs/superpowers/specs/2026-07-26-admin-console-design.md` の §5

## Global Constraints

- コメントは日本語で書き、「なぜ」に焦点を当てる（`.opencode/skills/shift-saas-dev/SKILL.md` のコード規約）
- コミットメッセージは `fix:` / `feat:` / `refactor:` プレフィックス + 日本語サマリ
- `try/except` で握り潰さない。ログ出力して `raise` で伝播
- DBアクセスは `query_all` / `query_one` / `execute` / `insert_row` を使う（local/D1 自動切替）
- 他テナントのリソースへのアクセスは 403 ではなく **404** を返す（既存の IDOR マスク方針。`src/app.py:2239` と同じ）
- 入力エラーは `raise ValueError(...)` → 400 JSON、認可エラーは `abort(403)`（`src/app.py:64-78` の errorhandler が JSON 化する）
- テストは `.venv/bin/python -m pytest tests/ -q` で全件が通ること
- 構文チェック: `node --check public/app.js` と `.venv/bin/python -c "import ast; ast.parse(open('src/app.py').read())"`
- 既存の709テストを壊さないこと。壊れた場合、その理由が「意図した仕様変更」であることを説明できない限り実装が誤り

---

### Task 1: 固定シフト API の認証と所属検証（S1）

`/api/shop/fixed-shifts` の POST/PUT/DELETE は `_shop_ctx()` を呼んでおらず、**誰でも未認証で任意店舗の固定シフトを改変できる**。`fixed_shifts` テーブルに `shop_id` 列が無いため、所属は `staffs` 経由の JOIN で検証する。

**Files:**
- Modify: `src/app.py:2365-2383`
- Test: `tests/test_security.py`

**Interfaces:**
- Consumes: `_shop_ctx()`（`src/app.py:1424`）、`query_one`、`abort`
- Produces: なし（既存エンドポイントの修正のみ）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_security.py` の末尾に追記する。ファイル先頭の import に `insert_fixed` が無ければ `from helpers import ...` の行に追加すること。

```python
# ============================================================
# 固定シフト: 未認証アクセス / テナント越境（回帰テスト）
# ============================================================
class TestFixedShiftsAuth:
    def test_fixed_shift_create_requires_auth(self, client):
        """認証ヘッダ無しで固定シフトを作成できないこと。"""
        shop_id = insert_shop("SHOP1")
        staff_id = insert_staff(shop_id, "p1", "太郎")
        r = client.post("/api/shop/fixed-shifts",
                        json={"staff_id": staff_id, "weekday": 1,
                              "start_time": "09:00", "end_time": "17:00"})
        assert r.status_code == 401, "未認証で固定シフトが作成できてしまう"

    def test_fixed_shift_update_requires_auth(self, client):
        shop_id = insert_shop("SHOP1")
        staff_id = insert_staff(shop_id, "p1", "太郎")
        fid = insert_fixed(staff_id, 1, "09:00", "17:00")
        r = client.put(f"/api/shop/fixed-shifts/{fid}",
                       json={"weekday": 2, "start_time": "10:00", "end_time": "18:00"})
        assert r.status_code == 401

    def test_fixed_shift_delete_requires_auth(self, client):
        shop_id = insert_shop("SHOP1")
        staff_id = insert_staff(shop_id, "p1", "太郎")
        fid = insert_fixed(staff_id, 1, "09:00", "17:00")
        r = client.delete(f"/api/shop/fixed-shifts/{fid}")
        assert r.status_code == 401

    def test_fixed_shift_create_rejects_other_shop_staff(self, client):
        """他店舗のスタッフを指定した固定シフトは作成できないこと。"""
        shop_a = insert_shop("SHOPA", "pw12345678")
        insert_staff(shop_a, "mgrA", "店長A", role="manager", password="pw12345678")
        shop_b = insert_shop("SHOPB", "pw12345678")
        staff_b = insert_staff(shop_b, "p1", "他店の人")

        r = client.post("/api/login", json={"shop_code": "SHOPA", "user_code": "mgrA",
                                            "password": "pw12345678"})
        assert r.status_code == 200
        token = r.get_json()["token"]

        r = client.post("/api/shop/fixed-shifts",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"staff_id": staff_b, "weekday": 1,
                              "start_time": "09:00", "end_time": "17:00"})
        assert r.status_code == 404, "他店舗スタッフの固定シフトが作れてしまう"

    def test_fixed_shift_update_rejects_other_shop(self, client):
        shop_a = insert_shop("SHOPA", "pw12345678")
        insert_staff(shop_a, "mgrA", "店長A", role="manager", password="pw12345678")
        shop_b = insert_shop("SHOPB", "pw12345678")
        staff_b = insert_staff(shop_b, "p1", "他店の人")
        fid = insert_fixed(staff_b, 1, "09:00", "17:00")

        r = client.post("/api/login", json={"shop_code": "SHOPA", "user_code": "mgrA",
                                            "password": "pw12345678"})
        token = r.get_json()["token"]

        r = client.put(f"/api/shop/fixed-shifts/{fid}",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"weekday": 2, "start_time": "10:00", "end_time": "18:00"})
        assert r.status_code == 404

    def test_fixed_shift_delete_rejects_other_shop(self, client):
        shop_a = insert_shop("SHOPA", "pw12345678")
        insert_staff(shop_a, "mgrA", "店長A", role="manager", password="pw12345678")
        shop_b = insert_shop("SHOPB", "pw12345678")
        staff_b = insert_staff(shop_b, "p1", "他店の人")
        fid = insert_fixed(staff_b, 1, "09:00", "17:00")

        r = client.post("/api/login", json={"shop_code": "SHOPA", "user_code": "mgrA",
                                            "password": "pw12345678"})
        token = r.get_json()["token"]

        r = client.delete(f"/api/shop/fixed-shifts/{fid}",
                          headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404
        # 他店舗のデータが残っていること
        from db import query_one as qo
        assert qo("SELECT id FROM fixed_shifts WHERE id=?", (fid,)) is not None
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestFixedShiftsAuth -v`
Expected: 6件すべて FAIL（未認証のものは 200 が返り、越境のものも 200 が返る）

- [ ] **Step 3: 実装する**

`src/app.py:2365-2383` を以下に置き換える。

```python
def _assert_fixed_shift_in_shop(fid, shop_id):
    """固定シフトが自店舗スタッフのものであることを検証する。

    fixed_shifts には shop_id 列が無く staff_id しか持たないため、staffs 経由で
    JOIN して所属を判定する。他店舗のものは 404（存在を秘匿する既存方針に合わせる）。
    """
    row = query_one(
        "SELECT fs.id FROM fixed_shifts fs JOIN staffs s ON fs.staff_id=s.id "
        "WHERE fs.id=? AND s.shop_id=?", (fid, shop_id))
    if row is None:
        abort(404, description="固定シフトが見つかりません")


def _assert_staff_in_shop(staff_id, shop_id):
    """staff_id が自店舗に属することを検証する（他店舗・存在しないIDは404）。"""
    row = query_one("SELECT id FROM staffs WHERE id=? AND shop_id=?", (staff_id, shop_id))
    if row is None:
        abort(404, description="スタッフが見つかりません")


@app.post("/api/shop/fixed-shifts")
def shop_fixed_post():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    _assert_staff_in_shop(body["staff_id"], shop_id)
    meta = execute("INSERT INTO fixed_shifts (staff_id, weekday, start_time, end_time) VALUES (?,?,?,?)",
                   (body["staff_id"], body["weekday"], body["start_time"], body["end_time"]))
    return jsonify({"ok": True, "id": meta["last_row_id"]})


@app.put("/api/shop/fixed-shifts/<int:fid>")
def shop_fixed_put(fid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    _assert_fixed_shift_in_shop(fid, shop_id)
    execute("UPDATE fixed_shifts SET weekday=?, start_time=?, end_time=? WHERE id=?",
            (body["weekday"], body["start_time"], body["end_time"], fid))
    return jsonify({"ok": True})


@app.delete("/api/shop/fixed-shifts/<int:fid>")
def shop_fixed_del(fid):
    shop, shop_id, _ = _shop_ctx()
    _assert_fixed_shift_in_shop(fid, shop_id)
    execute("DELETE FROM fixed_shifts WHERE id=?", (fid,))
    return jsonify({"ok": True})
```

`_assert_staff_in_shop` は Task 2 でも使うので、`_shop_ctx()`（`src/app.py:1424`）の直後に置くこと。

- [ ] **Step 4: テストを実行して通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestFixedShiftsAuth -v`
Expected: 6件すべて PASS

- [ ] **Step 5: 既存テストが壊れていないことを確認**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS。もし固定シフト関連の既存テストが 401 で落ちるなら、そのテストが認証ヘッダを付けていない（＝脆弱性に依存していた）ことを意味する。テスト側に認証を追加して直す。

- [ ] **Step 6: コミット**

```bash
git add src/app.py tests/test_security.py
git commit -m "fix(security): 固定シフトAPIの未認証アクセスとテナント越境を修正

POST/PUT/DELETE /api/shop/fixed-shifts が _shop_ctx() を呼んでおらず、
誰でも未認証で任意店舗の固定シフトを改変できる状態だった。認証を追加し、
fixed_shifts に shop_id 列が無いため staffs 経由の JOIN で所属を検証する。"
```

---

### Task 2: シフト作成・更新の staff_id 所属検証（S1'）

`POST/PUT /api/shop/shifts` は `staff_id` をそのまま採用しており、自店舗の `shop_id` を持ちながら他店舗スタッフを指す `shifts` 行を作れる。`/api/shop/wishes/bulk`（`src/app.py:3711-3720`）は同じ検証を既に実装しているので、それに揃える。

**Files:**
- Modify: `src/app.py:2814`（POST の `staff_id` 取得箇所）, `src/app.py:2943`（PUT の `staff_id` 取得箇所）
- Test: `tests/test_security.py`

**Interfaces:**
- Consumes: Task 1 で追加した `_assert_staff_in_shop(staff_id, shop_id)`
- Produces: なし

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_security.py` の `TestFixedShiftsAuth` の下に追記する。

```python
class TestShiftStaffScope:
    def _login_shop_a(self, client):
        shop_a = insert_shop("SHOPA", "pw12345678")
        insert_staff(shop_a, "mgrA", "店長A", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOPA", "user_code": "mgrA",
                                            "password": "pw12345678"})
        assert r.status_code == 200
        return shop_a, r.get_json()["token"]

    def test_shift_create_rejects_other_shop_staff(self, client):
        """他店舗スタッフを指すシフトを作成できないこと。"""
        shop_a, token = self._login_shop_a(client)
        shop_b = insert_shop("SHOPB", "pw12345678")
        staff_b = insert_staff(shop_b, "p1", "他店の人")

        r = client.post("/api/shop/shifts",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"staff_id": staff_b, "date": "2026-08-03",
                              "start_time": "09:00", "end_time": "17:00"})
        assert r.status_code == 404, "他店舗スタッフのシフトが作れてしまう"

    def test_shift_update_rejects_other_shop_staff(self, client):
        """自店舗のシフトを他店舗スタッフに付け替えられないこと。"""
        shop_a, token = self._login_shop_a(client)
        staff_a = insert_staff(shop_a, "p1", "自店の人")
        shop_b = insert_shop("SHOPB", "pw12345678")
        staff_b = insert_staff(shop_b, "p2", "他店の人")

        r = client.post("/api/shop/shifts",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"staff_id": staff_a, "date": "2026-08-03",
                              "start_time": "09:00", "end_time": "17:00"})
        assert r.status_code == 200
        shift_id = r.get_json().get("id")

        r = client.put(f"/api/shop/shifts/{shift_id}",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"staff_id": staff_b, "date": "2026-08-03",
                             "start_time": "09:00", "end_time": "17:00"})
        assert r.status_code == 404, "他店舗スタッフに付け替えできてしまう"
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestShiftStaffScope -v`
Expected: 2件とも FAIL

補足: POST のレスポンスに `id` が含まれない実装の場合、`shift_id` が `None` になり2つ目のテストが別の理由で失敗する。その場合は先に `src/app.py` の POST 実装を読み、レスポンスの実際のキー名に合わせてテストを修正すること（テストを通すために実装のレスポンス形を変えてはいけない）。

- [ ] **Step 3: 実装する**

`src/app.py:2814` 付近、POST のハンドラで `staff_id = body["staff_id"]` としている直後に検証を挿入する。

```python
    staff_id = body["staff_id"]
    # 自店舗の shop_id を持ちながら他店舗スタッフを指す行を作らせない。
    # /api/shop/wishes/bulk (src/app.py:3711-3720) と同じ防御。
    _assert_staff_in_shop(staff_id, shop_id)
```

`src/app.py:2943` 付近、PUT のハンドラで `body.get("staff_id")` を使っている箇所も同様にする。PUT では `staff_id` が省略され得るため、値があるときだけ検証する。

```python
    new_staff_id = body.get("staff_id")
    if new_staff_id is not None:
        _assert_staff_in_shop(new_staff_id, shop_id)
```

実装前に該当箇所の前後20行を読み、変数名（`shop_id` がその関数のスコープに存在するか）を確認すること。

- [ ] **Step 4: テストを実行して通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestShiftStaffScope -v`
Expected: 2件とも PASS

- [ ] **Step 5: 既存テストの確認**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 6: コミット**

```bash
git add src/app.py tests/test_security.py
git commit -m "fix(security): シフト作成・更新で staff_id の所属店舗を検証

自店舗の shop_id を持ちながら他店舗スタッフを指す shifts 行を作れる状態だった。
/api/shop/wishes/bulk が既に行っている検証と同じ形に揃える。"
```

---

### Task 3: require_auth の後方互換フォールバック削除（C）

`src/app.py:111-112` は、セッションの `shop_id` が指す店舗を引けなかったとき `user_id` を `shops.id` とみなす。manager セッションでは `user_id` は `staffs.id` なので、**別テナントに着地し得る**。

旧店主ログインは `_create_session("shop", shop["id"], shop["id"], ...)`（`src/app.py:671`）で `shop_id` を正しく入れているため、このフォールバックは既に不要。

**Files:**
- Modify: `src/app.py:106-113`
- Test: `tests/test_security.py`

**Interfaces:**
- Consumes: なし
- Produces: なし

- [ ] **Step 1: 失敗するテストを書く**

```python
class TestSessionFallback:
    def test_deleted_shop_session_does_not_land_on_other_tenant(self, client):
        """セッションの shop_id が指す店舗が消えたとき、別テナントに着地しないこと。

        manager セッションの user_id は staffs.id。フォールバックが残っていると
        staffs.id と同値の shops.id を持つ無関係な店舗の権限を得てしまう。
        """
        import db as dbmod
        # 店舗A(id=1) を作り、その manager でログイン
        shop_a = insert_shop("SHOPA", "pw12345678")
        insert_staff(shop_a, "mgrA", "店長A", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOPA", "user_code": "mgrA",
                                            "password": "pw12345678"})
        assert r.status_code == 200
        token = r.get_json()["token"]

        # セッションの shop_id を存在しない店舗に differ させ、
        # user_id を「別の実在店舗のid」に書き換える（フォールバックが発火する条件）
        shop_b = insert_shop("SHOPB", "pw12345678")
        dbmod.execute("UPDATE sessions SET shop_id=99999, user_id=? WHERE token=?",
                      (shop_b, token))

        r = client.get("/api/shop/staffs", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code != 200, "存在しない店舗のセッションで別テナントに着地している"
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestSessionFallback -v`
Expected: FAIL（200 が返り、店舗Bのスタッフ一覧が見えてしまう）

- [ ] **Step 3: 実装する**

`src/app.py:106-113` を以下に置き換える。

```python
    elif role == "shop":
        # user_id は従来 shops.id（旧店主）または staffs.id（manager ロール）。
        # shop_id を使って店舗情報を取得する。
        # NOTE: かつて shop が引けないとき user_id を shops.id とみなすフォールバックが
        # あったが、manager セッションでは user_id が staffs.id のため、staffs.id と同値の
        # shops.id を持つ別テナントに着地し得た。旧店主ログインも _create_session で
        # shop_id を正しく入れている（src/app.py:671）ため、フォールバックは削除した。
        user = query_one("SELECT * FROM shops WHERE id=?", (session.get("shop_id"),))
        if user is None:
            abort(401, description="セッションの店舗が見つかりません")
```

- [ ] **Step 4: テストを実行して通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestSessionFallback -v`
Expected: PASS

- [ ] **Step 5: 既存テストの確認**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS。旧店主ログインのテストが落ちる場合、`_create_session` が `shop_id` を入れていないケースが残っている可能性があるので `src/app.py:667-671` を確認する。

- [ ] **Step 6: コミット**

```bash
git add src/app.py tests/test_security.py
git commit -m "fix(security): require_auth の後方互換フォールバックを削除

セッションの shop_id が指す店舗が引けないとき user_id を shops.id とみなす
分岐があり、manager セッション(user_id=staffs.id)で別テナントに着地し得た。
旧店主ログインも shop_id を正しく設定しているため不要。"
```

---

### Task 4: 店舗更新の部分更新化（B1）

店舗一覧の有効/無効トグルが `shop_name: ''` を送信し（`public/app.js:4523`）、サーバが空値ガード無しで UPDATE する（`src/app.py:803`）ため**店舗名が消える**。サーバを部分更新に変え、フロントは `is_active` だけを送るようにする。あわせて店舗名・店舗コードの編集を API として使えるようにする（画面は Phase 2 で作る）。

**Files:**
- Modify: `src/app.py:798-807`
- Modify: `public/app.js:4523`
- Test: `tests/test_admin_shop_update.py`（新規）

**Interfaces:**
- Consumes: `require_auth`, `audit`, `query_one`, `execute`
- Produces: `PUT /api/admin/shops/<sid>` — body の含まれるキーのみ更新。受け付けるキーは `shop_name` / `shop_code` / `is_active`

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_admin_shop_update.py`:

```python
"""管理者による店舗更新（部分更新）のテスト。

背景: 有効/無効トグルが shop_name:'' を送り、店舗名が空文字で潰れる事故があった。
"""
import db as dbmod
from helpers import insert_admin, insert_shop


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    assert r.status_code == 200
    return r.get_json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def test_toggle_active_preserves_shop_name(client):
    """is_active だけを送ったとき、店舗名が変わらないこと。"""
    token = _admin_token(client)
    sid = insert_shop("SHOP1", name="レイクタウン店")

    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token),
                   json={"is_active": False})
    assert r.status_code == 200

    row = dbmod.query_one("SELECT shop_name, is_active FROM shops WHERE id=?", (sid,))
    assert row["shop_name"] == "レイクタウン店", "店舗名が消えている"
    assert row["is_active"] == 0


def test_empty_shop_name_is_rejected(client):
    """空の店舗名は 400 で拒否されること。"""
    token = _admin_token(client)
    sid = insert_shop("SHOP1", name="レイクタウン店")

    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token),
                   json={"shop_name": "   "})
    assert r.status_code == 400
    row = dbmod.query_one("SELECT shop_name FROM shops WHERE id=?", (sid,))
    assert row["shop_name"] == "レイクタウン店"


def test_rename_shop(client):
    """店舗名を変更できること。is_active は変わらないこと。"""
    token = _admin_token(client)
    sid = insert_shop("SHOP1", name="旧名")

    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token),
                   json={"shop_name": "新名"})
    assert r.status_code == 200
    row = dbmod.query_one("SELECT shop_name, is_active FROM shops WHERE id=?", (sid,))
    assert row["shop_name"] == "新名"
    assert row["is_active"] == 1


def test_change_shop_code(client):
    """店舗コードを変更できること。"""
    token = _admin_token(client)
    sid = insert_shop("OLD1", name="店")

    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token),
                   json={"shop_code": "NEW1"})
    assert r.status_code == 200
    row = dbmod.query_one("SELECT shop_code FROM shops WHERE id=?", (sid,))
    assert row["shop_code"] == "NEW1"


def test_duplicate_shop_code_is_rejected(client):
    """既に使われている店舗コードへの変更は 400 で拒否されること。"""
    token = _admin_token(client)
    sid1 = insert_shop("SHOP1", name="店1")
    insert_shop("SHOP2", name="店2")

    r = client.put(f"/api/admin/shops/{sid1}", headers=_hdr(token),
                   json={"shop_code": "SHOP2"})
    assert r.status_code == 400
    row = dbmod.query_one("SELECT shop_code FROM shops WHERE id=?", (sid1,))
    assert row["shop_code"] == "SHOP1"


def test_unknown_shop_returns_404(client):
    token = _admin_token(client)
    r = client.put("/api/admin/shops/99999", headers=_hdr(token),
                   json={"is_active": True})
    assert r.status_code == 404


def test_requires_admin_role(client):
    """shop ロールでは呼べないこと。"""
    from helpers import insert_staff
    sid = insert_shop("SHOP1", "pw12345678", name="店")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                        "password": "pw12345678"})
    token = r.get_json()["token"]
    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token), json={"is_active": False})
    assert r.status_code == 403
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_shop_update.py -v`
Expected: `test_toggle_active_preserves_shop_name` / `test_empty_shop_name_is_rejected` / `test_rename_shop` / `test_change_shop_code` / `test_duplicate_shop_code_is_rejected` / `test_unknown_shop_returns_404` が FAIL

- [ ] **Step 3: 実装する**

`src/app.py:798-807` を以下に置き換える。

```python
@app.put("/api/admin/shops/<int:sid>")
def admin_update_shop(sid):
    require_auth(["admin"])
    body = request.get_json(silent=True) or {}
    shop = query_one("SELECT id, shop_code, shop_name FROM shops WHERE id=?", (sid,))
    if shop is None:
        abort(404, description="店舗が見つかりません")

    # 部分更新。送られてきたキーだけを更新する。
    # NOTE: かつて body.get("shop_name") を無条件で UPDATE していたため、
    # 有効/無効トグル（shop_name を送らない）で店舗名が空文字に潰れる事故があった。
    sets, binds, changed = [], [], []
    if "shop_name" in body:
        name = (body.get("shop_name") or "").strip()
        if not name:
            raise ValueError("店舗名を入力してください")
        sets.append("shop_name=?"); binds.append(name); changed.append("shop_name")
    if "shop_code" in body:
        code = (body.get("shop_code") or "").strip()
        if not code:
            raise ValueError("店舗コードを入力してください")
        dup = query_one("SELECT id FROM shops WHERE shop_code=? AND id<>?", (code, sid))
        if dup:
            raise ValueError("その店舗コードは既に使われています")
        sets.append("shop_code=?"); binds.append(code); changed.append("shop_code")
    if "is_active" in body:
        is_active = 1 if body.get("is_active") else 0
        sets.append("is_active=?"); binds.append(is_active); changed.append(f"is_active={is_active}")

    if not sets:
        raise ValueError("更新する項目がありません")

    binds.append(sid)
    execute(f"UPDATE shops SET {','.join(sets)} WHERE id=?", tuple(binds))
    audit("shop.update", target_type="shop", target_id=sid, shop_id=sid,
          detail=",".join(changed))
    return jsonify({"ok": True})
```

- [ ] **Step 4: テストを実行して通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_admin_shop_update.py -v`
Expected: 全件 PASS

- [ ] **Step 5: フロントのトグルを修正**

`public/app.js:4523` の `JSON.stringify({ is_active: b.dataset.active !== '1', shop_name: '' })` から `shop_name: ''` を削除する。修正後の行:

```js
    document.getElementById('shopList').querySelectorAll('[data-toggle]').forEach((b) => b?.addEventListener('click', async (ev) => { ev.stopPropagation(); await api(`/admin/shops/${b.dataset.toggle}`, { method: 'PUT', body: JSON.stringify({ is_active: b.dataset.active !== '1' }) }); load(); }));
```

- [ ] **Step 6: JS の構文チェックと全テスト**

Run: `node --check public/app.js && .venv/bin/python -m pytest tests/ -q`
Expected: 両方とも成功

- [ ] **Step 7: コミット**

```bash
git add src/app.py public/app.js tests/test_admin_shop_update.py
git commit -m "fix: 店舗の有効/無効トグルで店舗名が消える不具合を修正

フロントが shop_name:'' を送り、サーバが空値ガード無しで UPDATE していた。
サーバを部分更新に変更し、店舗名の空値と店舗コードの重複を拒否する。
あわせて店舗名・店舗コードの変更を API で行えるようにした（画面は Phase 2）。"
```

---

### Task 5: /api/init の既定無効化とランダム初期パスワード（S4）

`POST /api/init` は認証不要で、`system_admins` が空なら誰でも `admin` / `admin123` を作れる。環境変数 `ALLOW_INIT=1` のときのみ許可し、初期パスワードはランダム生成してレスポンスで1回だけ返す。

**Files:**
- Modify: `src/app.py:594-609`
- Modify: `.env.example`
- Test: `tests/test_admin_init.py`（新規）

**Interfaces:**
- Consumes: `query_one`, `execute`, `hash_password`
- Produces: `POST /api/init` — `ALLOW_INIT=1` かつ管理者未登録のときだけ 200 で `{ok, message, logins:{admin:{id, password}}}` を返す。それ以外は 403

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_admin_init.py`:

```python
"""POST /api/init のガードに関するテスト。

背景: 認証不要のまま公開されており、system_admins が空なら誰でも
初期管理者を作れる（DBリセット直後の乗っ取り窓）状態だった。
"""
import os

import db as dbmod
from helpers import insert_admin


def test_init_is_disabled_by_default(client, monkeypatch):
    """ALLOW_INIT が未設定なら 403 で拒否され、管理者が作られないこと。"""
    monkeypatch.delenv("ALLOW_INIT", raising=False)
    r = client.post("/api/init")
    assert r.status_code == 403
    assert dbmod.query_one("SELECT id FROM system_admins LIMIT 1") is None


def test_init_creates_admin_when_allowed(client, monkeypatch):
    """ALLOW_INIT=1 なら管理者を作り、生成パスワードを返すこと。"""
    monkeypatch.setenv("ALLOW_INIT", "1")
    r = client.post("/api/init")
    assert r.status_code == 200
    data = r.get_json()
    pw = data["logins"]["admin"]["password"]
    assert pw and pw != "admin123", "固定パスワードが返っている"
    assert len(pw) >= 12

    # 返ってきたパスワードで実際にログインできること
    r = client.post("/api/login", json={"user_code": "admin", "password": pw})
    assert r.status_code == 200


def test_init_is_noop_when_admin_exists(client, monkeypatch):
    """管理者が既にいる場合は ALLOW_INIT=1 でも作らないこと。"""
    monkeypatch.setenv("ALLOW_INIT", "1")
    insert_admin("admin", "Admin123")
    r = client.post("/api/init")
    assert r.status_code == 200
    assert r.get_json()["logins"] == {}
    rows = dbmod.query_all("SELECT id FROM system_admins")
    assert len(rows) == 1
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_init.py -v`
Expected: `test_init_is_disabled_by_default` と `test_init_creates_admin_when_allowed` が FAIL

- [ ] **Step 3: 実装する**

`src/app.py:594-609` を以下に置き換える。`secrets` は `src/app.py` の import に無ければ追加すること（`import secrets`）。

```python
@app.post("/api/init")
def handle_init():
    """初回セットアップ: 管理者が未登録の場合のみ、初期管理者を作成。

    ※ 認証不要のエンドポイントなので、環境変数 ALLOW_INIT=1 のときだけ有効にする。
       既定で無効なのは、DBリセット直後に第三者が初期管理者を作れてしまうため。
    ※ 初期パスワードはランダム生成し、このレスポンスで1回だけ返す（保存も再表示もしない）。
    """
    if os.getenv("ALLOW_INIT") != "1":
        abort(403, description="初期セットアップは無効です（ALLOW_INIT=1 で有効化してください）")
    msg = {"admin": "", "shop": "", "logins": {}}
    if not query_one("SELECT id FROM system_admins LIMIT 1"):
        initial_pw = secrets.token_urlsafe(12)
        execute("INSERT INTO system_admins (admin_id, password_hash, name) VALUES (?,?,?)",
                ("admin", hash_password(initial_pw), "システム管理者"))
        msg["admin"] = "管理者を作成しました。このパスワードは再表示されません。"
        msg["logins"] = {"admin": {"id": "admin", "password": initial_pw}}
        return jsonify({"ok": True, "message": "初期管理者を作成しました", "details": msg,
                        "logins": msg["logins"]})
    return jsonify({"ok": True, "message": "管理者は既に存在します。ログインしてください。",
                    "details": msg, "logins": {}})
```

- [ ] **Step 4: .env.example に説明を追記**

`.env.example` の末尾に追記する。

```
# 初期セットアップ（POST /api/init）の有効化。
# 管理者アカウントを1つも持たない状態から立ち上げるときだけ 1 にし、
# セットアップ完了後は必ず削除するか 0 に戻すこと。
ALLOW_INIT=0
```

- [ ] **Step 5: テストを実行して通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_admin_init.py -v`
Expected: 3件とも PASS

- [ ] **Step 6: 既存テストの確認**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS。`/api/init` を呼んでいる既存テストや E2E ヘルパ（`e2e/helpers.js` の `ensureAdmin`）が 403 で落ちる可能性がある。落ちた場合は次の Step で直す。

- [ ] **Step 7: E2E のセットアップを確認**

Run: `grep -n "api/init" e2e/*.js tests/*.py`

`/api/init` に依存している箇所があれば、E2E サーバ起動スクリプト `e2e/run_server.sh` に `ALLOW_INIT=1` を追加して対応する（E2E は毎回クリーンなDBで起動するため、初期管理者の作成が必要）。

- [ ] **Step 8: コミット**

```bash
git add src/app.py .env.example tests/test_admin_init.py e2e/run_server.sh
git commit -m "fix(security): /api/init を既定で無効化し初期パスワードをランダム化

認証不要のまま公開されており、system_admins が空なら誰でも admin/admin123 を
作れる状態だった。ALLOW_INIT=1 のときのみ有効にし、初期パスワードは
secrets.token_urlsafe(12) で生成してレスポンスで1回だけ返す。"
```

---

### Task 6: ログイン試行のレート制限（S5）

ログイン失敗のロックアウトが無く、管理者ログインは「コード欄に `admin`」という推測容易なマジックワード。`login_attempts` テーブルで同一キーの失敗を数え、15分間に10回失敗で15分ロックする。

**Files:**
- Create: なし
- Modify: `src/app.py`（`ensure_db()` にテーブル作成、`login()` にレート制限、末尾付近）
- Modify: `schema.sql`（新規環境向け）
- Modify: `tests/conftest.py:30-43`（`_TABLES` に `login_attempts` を追加）
- Modify: `tests/test_security.py:184`（`test_login_brute_force_no_lockout` の書き換え）

**Interfaces:**
- Consumes: `query_one`, `execute`, `jst_now`
- Produces:
  - `_login_attempt_key(shop_code, user_code)` → `str`
  - `_check_login_lock(key)` → `None`（ロック中は `abort(429)`）
  - `_record_login_failure(key)` → `None`
  - `_clear_login_failures(key)` → `None`

- [ ] **Step 1: conftest にテーブルを追加**

`tests/conftest.py:30-43` の `_TABLES` リストの先頭付近（`audit_logs` の隣）に `"login_attempts"` を追加する。

```python
_TABLES = [
    "audit_logs",
    "login_attempts",
    "change_requests",
    ...
]
```

**なぜ必要か:** `db_reset` はテストごとにこのリストのテーブルを DELETE する。`login_attempts` を入れ忘れると、あるテストのログイン失敗が次のテストにロック状態として漏れ、原因の分かりにくい失敗を生む。

- [ ] **Step 2: schema.sql にテーブル定義を追加**

`schema.sql` の末尾（`audit_logs` の定義の後）に追記する。

```sql
-- -----------------------------------------------------------
-- 15. login_attempts: ログイン試行のレート制限
-- attempt_key は "<remote_addr>|<shop_code>|<user_code>"。
-- バックグラウンドジョブが無いため、期限切れ行はログイン処理のついでに掃除する。
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS login_attempts (
  attempt_key  TEXT PRIMARY KEY,
  fail_count   INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  updated_at   TEXT
);
```

- [ ] **Step 3: 失敗するテストを書く**

`tests/test_security.py:184` の `test_login_brute_force_no_lockout` を丸ごと以下に置き換える。旧テストは「ロックアウトが無い」という脆弱性を現状として記録しているので、レート制限を入れると必ず落ちる。

```python
    def test_login_brute_force_locks_out(self, client):
        """10回失敗した時点でロックされ、正しいパスワードでもログインできないこと。"""
        insert_admin("admin", "Admin123")
        for i in range(10):
            r = client.post("/api/login", json={"id": "admin", "password": "wrong"})
            assert r.status_code == 400, f"{i+1}回目が想定外のステータス: {r.status_code}"
        # 11回目はロックされて 429
        r = client.post("/api/login", json={"id": "admin", "password": "wrong"})
        assert r.status_code == 429
        # 正しいパスワードでもロック中は通らない
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 429, "ロック中に正しいパスワードで通ってしまう"

    def test_login_success_clears_failure_count(self, client):
        """失敗のあと成功すると、失敗カウントがリセットされること。"""
        insert_admin("admin", "Admin123")
        for _ in range(5):
            client.post("/api/login", json={"id": "admin", "password": "wrong"})
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 200
        # リセットされているので、さらに5回失敗してもロックされない
        for _ in range(5):
            r = client.post("/api/login", json={"id": "admin", "password": "wrong"})
            assert r.status_code == 400
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 200

    def test_login_lock_is_per_account(self, client):
        """あるアカウントのロックが、別アカウントのログインを妨げないこと。"""
        insert_admin("admin", "Admin123")
        shop_id = insert_shop("SHOP1", "pw12345678")
        insert_staff(shop_id, "mgr", "店長", role="manager", password="pw12345678")
        for _ in range(11):
            client.post("/api/login", json={"id": "admin", "password": "wrong"})
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        assert r.status_code == 200, "別アカウントまで巻き添えでロックされている"
```

`insert_shop` / `insert_staff` が `tests/test_security.py` の import に無ければ追加すること。

- [ ] **Step 4: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestAuthSecurity -v`
Expected: `test_login_brute_force_locks_out` が FAIL（11回目も 400 が返る）

- [ ] **Step 5: ensure_db にテーブル作成を追加**

`src/app.py` の `ensure_db()` 内、`audit_logs` を作っているブロック（`src/app.py:4096-4106`）の直後に追記する。既存の `migrations/*.sql` を適用する仕組みは Phase 2 で導入するため、Phase 1 では `audit_logs` と同じ形で作る。

```python
    # ログイン試行のレート制限テーブル（作成失敗しても業務は止めない）
    try:
        execute(
            "CREATE TABLE IF NOT EXISTS login_attempts ("
            "attempt_key TEXT PRIMARY KEY, fail_count INTEGER NOT NULL DEFAULT 0, "
            "locked_until TEXT, updated_at TEXT)")
    except Exception as e:
        print(f"[ensure_db] WARN: login_attempts setup failed (skipped): {e}", flush=True)
```

- [ ] **Step 6: レート制限ヘルパを実装**

`src/app.py` の `require_auth` の定義の直前（`src/app.py:82` の「認証ヘルパ」コメントの下）に追加する。

```python
# ログイン試行のレート制限
_LOGIN_MAX_FAILS = 10        # この回数失敗したらロック
_LOGIN_WINDOW_MIN = 15       # 失敗カウントを保持する時間（分）
_LOGIN_LOCK_MIN = 15         # ロックする時間（分）


def _login_attempt_key(shop_code, user_code):
    """レート制限のキー。管理者ログインは user_code を 'admin' に正規化する。

    管理者は「店舗コード欄・ユーザーコード欄のどちらかに admin」で試行できる
    （src/app.py の login() 参照）ため、正規化しないと2倍の試行回数を許してしまう。
    """
    ip = request.remote_addr or "-"
    if shop_code == "admin" or user_code == "admin":
        return f"{ip}|admin"
    return f"{ip}|{shop_code}|{user_code}"


def _check_login_lock(key):
    """ロック中なら 429 で中断する。期限切れの行はここで掃除する。"""
    now = jst_now()
    row = query_one("SELECT fail_count, locked_until, updated_at FROM login_attempts WHERE attempt_key=?",
                    (key,))
    if not row:
        return
    locked_until = row.get("locked_until")
    if locked_until:
        try:
            lock_dt = datetime.strptime(locked_until, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            lock_dt = None
        if lock_dt and now < lock_dt:
            abort(429, description="ログイン試行が多すぎます。しばらく待ってからお試しください")
    # ロック期限切れ、または最終試行がウィンドウ外なら行を捨てる
    updated = row.get("updated_at")
    stale = True
    if updated:
        try:
            stale = (now - datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")) > timedelta(minutes=_LOGIN_WINDOW_MIN)
        except (ValueError, TypeError):
            stale = True
    if stale or locked_until:
        execute("DELETE FROM login_attempts WHERE attempt_key=?", (key,))


def _record_login_failure(key):
    """失敗を1回数える。上限に達したらロック時刻を設定する。"""
    now = jst_now()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    row = query_one("SELECT fail_count FROM login_attempts WHERE attempt_key=?", (key,))
    count = (row["fail_count"] if row else 0) + 1
    locked_until = None
    if count >= _LOGIN_MAX_FAILS:
        locked_until = (now + timedelta(minutes=_LOGIN_LOCK_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    if row:
        execute("UPDATE login_attempts SET fail_count=?, locked_until=?, updated_at=? WHERE attempt_key=?",
                (count, locked_until, now_s, key))
    else:
        execute("INSERT INTO login_attempts (attempt_key, fail_count, locked_until, updated_at) VALUES (?,?,?,?)",
                (key, count, locked_until, now_s))


def _clear_login_failures(key):
    """ログイン成功時に失敗カウントを消す。"""
    execute("DELETE FROM login_attempts WHERE attempt_key=?", (key,))
```

`timedelta` が `src/app.py` に import されていることを確認する（`_create_session` が使っているので既にあるはず）。

- [ ] **Step 7: login() に組み込む**

`src/app.py:631` からの `login()` を修正する。`pw` のチェックの直後にキー計算とロック確認を入れ、失敗する各分岐の前に `_record_login_failure(key)` を、成功する各分岐の前に `_clear_login_failures(key)` を入れる。

`raise ValueError` が3箇所（管理者・複合キー・末尾）、`_create_session` の呼び出しが4箇所（管理者・manager・staff・旧店主）あるので、すべてに対応させること。実装後の骨格:

```python
    body = request.get_json(silent=True) or {}
    shop_code = (body.get("shop_code") or body.get("id") or "").strip()
    user_code = (body.get("user_code") or body.get("staff_code") or "").strip()
    pw = body.get("password") or ""
    if not pw:
        raise ValueError("パスワードを入力してください")

    attempt_key = _login_attempt_key(shop_code, user_code)
    _check_login_lock(attempt_key)

    # ---- システム管理者 ("admin" マジックワード) ----
    if user_code == "admin" or shop_code == "admin":
        other = user_code if user_code != "admin" else shop_code
        admin_id_guess = other if other and other != "admin" else "admin"
        admin = query_one("SELECT * FROM system_admins WHERE admin_id=?", (admin_id_guess,))
        if not admin and admin_id_guess != "admin":
            admin = query_one("SELECT * FROM system_admins WHERE admin_id=?", ("admin",))
        if admin and verify_password(pw, admin["password_hash"]):
            _clear_login_failures(attempt_key)
            return jsonify(_create_session("admin", admin["id"], None, admin))
        _record_login_failure(attempt_key)
        raise ValueError("管理者IDまたはパスワードが正しくありません")

    if not shop_code or not user_code:
        raise ValueError("店舗コードとユーザーコードを入力してください")

    staff = query_one(
        "SELECT s.* FROM staffs s JOIN shops sh ON s.shop_id=sh.id "
        "WHERE sh.shop_code=? AND s.staff_code=? AND s.is_resigned=0 AND sh.is_active=1",
        (shop_code, user_code))
    if staff and verify_password(pw, staff["password_hash"]):
        _clear_login_failures(attempt_key)
        if staff["role"] == "manager":
            shop = query_one("SELECT * FROM shops WHERE id=?", (staff["shop_id"],))
            return jsonify(_create_session("shop", staff["id"], staff["shop_id"], shop))
        return jsonify(_create_session("staff", staff["id"], staff["shop_id"], staff))

    if user_code == shop_code:
        shop = query_one("SELECT * FROM shops WHERE shop_code=? AND is_active=1", (shop_code,))
        if shop and verify_password(pw, shop["password_hash"]):
            _clear_login_failures(attempt_key)
            return jsonify(_create_session("shop", shop["id"], shop["id"], shop))

    _record_login_failure(attempt_key)
    raise ValueError("店舗コード・ユーザーコードまたはパスワードが正しくありません")
```

「店舗コードとユーザーコードを入力してください」の分岐では `_record_login_failure` を呼ばない。これは入力不備であって認証試行ではないため。

- [ ] **Step 8: テストを実行して通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestAuthSecurity -v`
Expected: 全件 PASS

- [ ] **Step 9: 既存テストの確認**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS。ログインを何度も繰り返すテストがロックに引っかかる場合は、そのテストが同一キーで10回以上失敗していないか確認する。`db_reset` が `login_attempts` を消すのでテスト間の漏れは起きない。

- [ ] **Step 10: コミット**

```bash
git add src/app.py schema.sql tests/conftest.py tests/test_security.py
git commit -m "feat(security): ログイン試行のレート制限を追加

15分間に10回失敗で15分ロックする。管理者は「どちらかの欄に admin」で
試行できるため、キーを 'admin' に正規化して試行回数が2倍にならないようにした。
バックグラウンドジョブが無いため、期限切れ行はログイン処理のついでに掃除する。

test_login_brute_force_no_lockout は脆弱性を現状として記録していたテストなので、
ロックされることを確認する内容に書き換えた。"
```

---

### Task 7: 認証まわりの監査ログ追加（5.9 のログイン系）

ログイン成功/失敗とログアウトが監査ログに残っていない。`audit()` は `g.role` / `g.user` から actor を解決する（`src/app.py:128-145`）が、ログイン失敗時は認証コンテキストが無いため、その場合の記録方法を用意する。

**Files:**
- Modify: `src/app.py:128-145`（`audit()` に明示的な actor 指定を追加）
- Modify: `src/app.py`（`login()` / `logout()` に `audit()` 呼び出し）
- Modify: `public/app.js:4664-4674`（`AUDIT_ACTION_LABELS` に日本語ラベル追加）
- Test: `tests/test_audit_log.py`

**Interfaces:**
- Consumes: `audit()`
- Produces: `audit(action, ..., actor_role=None, actor_name=None)` — 明示指定があれば `g` より優先する

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_audit_log.py` の末尾に追記する。

```python
def test_login_success_is_audited(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    assert r.status_code == 200
    row = dbmod.query_one("SELECT action, actor_role, actor_name FROM audit_logs "
                          "WHERE action='auth.login' ORDER BY id DESC LIMIT 1")
    assert row is not None, "ログイン成功が監査ログに残っていない"
    assert row["actor_role"] == "admin"


def test_login_failure_is_audited_without_password(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "wrongpass"})
    assert r.status_code == 400
    row = dbmod.query_one("SELECT action, actor_role, actor_name, detail FROM audit_logs "
                          "WHERE action='auth.login_failed' ORDER BY id DESC LIMIT 1")
    assert row is not None, "ログイン失敗が監査ログに残っていない"
    assert row["actor_role"] == "anonymous"
    # 入力されたパスワードが記録されていないこと
    joined = f"{row['actor_name']} {row['detail']}"
    assert "wrongpass" not in joined, "パスワードが監査ログに漏れている"


def test_logout_is_audited(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    token = r.get_json()["token"]
    r = client.post("/api/logout", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    row = dbmod.query_one("SELECT action FROM audit_logs WHERE action='auth.logout' "
                          "ORDER BY id DESC LIMIT 1")
    assert row is not None, "ログアウトが監査ログに残っていない"
```

`insert_admin` と `db as dbmod` が import されていることを確認する（既存テストで使っていれば不要）。

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_audit_log.py -v`
Expected: 追加した3件が FAIL

- [ ] **Step 3: audit() に明示的な actor 指定を追加**

`src/app.py:128-145` を以下に置き換える。

```python
def audit(action, target_type=None, target_id=None, shop_id=None, detail=None,
          actor_role=None, actor_id=None, actor_name=None):
    """監査ログを1件記録。失敗しても業務処理を止めない。

    actor は既定で現在の認証コンテキスト(g.role / g.user)から解決する。
    shop の g.user は shops 行なので氏名は shop_name、それ以外は name。

    ログイン失敗のように認証コンテキストが存在しない場面では、
    actor_role / actor_name を明示的に渡す（g より優先される）。
    """
    try:
        role = actor_role if actor_role is not None else getattr(g, "role", None)
        user = getattr(g, "user", None) or {}
        if actor_id is None:
            actor_id = user.get("id")
        if actor_name is None:
            g_role = getattr(g, "role", None)
            actor_name = user.get("shop_name") if g_role == "shop" else user.get("name")
        insert_row("audit_logs", {
            "actor_role": role, "actor_id": actor_id, "actor_name": actor_name,
            "action": action, "target_type": target_type, "target_id": target_id,
            "shop_id": shop_id, "detail": detail,
            "created_at": jst_now().strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        print(f"[audit] WARN: failed to record {action}: {e}", flush=True)
```

- [ ] **Step 4: login() / logout() に記録を追加**

`login()` の各分岐に追加する。**パスワードは絶対に記録しない。**

管理者の成功分岐（`_clear_login_failures(attempt_key)` の直後）:

```python
            audit("auth.login", target_type="system_admin", target_id=admin["id"],
                  actor_role="admin", actor_id=admin["id"], actor_name=admin.get("name"),
                  detail=f"admin_id={admin['admin_id']}")
```

管理者の失敗分岐（`_record_login_failure(attempt_key)` の直後）:

```python
            audit("auth.login_failed", actor_role="anonymous",
                  actor_name=admin_id_guess, detail="管理者ログイン失敗")
```

staff / manager の成功分岐（`_clear_login_failures(attempt_key)` の直後、`if staff["role"] == "manager":` の前）:

```python
        audit("auth.login", target_type="staff", target_id=staff["id"], shop_id=staff["shop_id"],
              actor_role="shop" if staff["role"] == "manager" else "staff",
              actor_id=staff["id"], actor_name=staff.get("name"),
              detail=f"role={staff['role']}")
```

旧店主の成功分岐:

```python
            audit("auth.login", target_type="shop", target_id=shop["id"], shop_id=shop["id"],
                  actor_role="shop", actor_id=shop["id"], actor_name=shop.get("shop_name"),
                  detail="旧仕様の店主ログイン")
```

末尾の失敗分岐（`_record_login_failure(attempt_key)` の直後）:

```python
    audit("auth.login_failed", actor_role="anonymous",
          actor_name=f"{shop_code}/{user_code}", detail="店舗またはスタッフのログイン失敗")
```

`logout()`（`src/app.py:688-694`）を以下に置き換える。トークン削除の**前**に監査するのは、削除後だと誰がログアウトしたか分からなくなるため。

```python
@app.post("/api/logout")
def logout():
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if token:
        session = query_one("SELECT role, user_id, shop_id FROM sessions WHERE token=?", (token,))
        if session:
            audit("auth.logout", shop_id=session.get("shop_id"),
                  actor_role=session["role"], actor_id=session["user_id"])
        execute("DELETE FROM sessions WHERE token=?", (token,))
    return jsonify({"ok": True})
```

- [ ] **Step 5: フロントの日本語ラベルを追加**

`public/app.js:4664-4674` の `AUDIT_ACTION_LABELS` に追記する。

```js
  'auth.login': 'ログイン',
  'auth.login_failed': 'ログイン失敗',
  'auth.logout': 'ログアウト',
```

- [ ] **Step 6: テストを実行して通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_audit_log.py -v`
Expected: 全件 PASS

- [ ] **Step 7: 全テストと構文チェック**

Run: `.venv/bin/python -m pytest tests/ -q && node --check public/app.js`
Expected: 両方とも成功

- [ ] **Step 8: コミット**

```bash
git add src/app.py public/app.js tests/test_audit_log.py
git commit -m "feat(audit): ログイン成功・失敗・ログアウトを監査ログに記録

audit() に actor の明示指定を追加した。ログイン失敗時は認証コンテキストが
無いため g から actor を解決できず、actor_role='anonymous' と入力コードを
明示的に渡す。入力されたパスワードは記録しない。"
```

---

### Task 8: 管理者通知エンドポイントの認証追加（S3）

`/api/admin/notifications` と `/api/admin/notifications/read-all` は `require_auth` を呼んでおらず、未認証で叩ける。現状は固定の空レスポンスを返すだけなので実害は小さいが、Phase 2 で中身を実装した瞬間に認証欠落バグになる。**Phase 1 の時点で塞いでおく。**

**Files:**
- Modify: `src/app.py:1626-1634`
- Test: `tests/test_security.py`

**Interfaces:**
- Consumes: `require_auth`
- Produces: なし（既存エンドポイントの修正のみ）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_security.py` に追記する。

```python
class TestAdminNotificationsAuth:
    def test_admin_notifications_requires_auth(self, client):
        """未認証で管理者通知を読めないこと。"""
        assert client.get("/api/admin/notifications").status_code == 401

    def test_admin_notifications_read_all_requires_auth(self, client):
        assert client.put("/api/admin/notifications/read-all").status_code == 401

    def test_shop_role_cannot_read_admin_notifications(self, client):
        """shop ロールでは403になること。"""
        sid = insert_shop("SHOP1", "pw12345678")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        r = client.get("/api/admin/notifications", headers={"Authorization": f"Bearer {t}"})
        assert r.status_code == 403
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestAdminNotificationsAuth -v`
Expected: 3件とも FAIL（200 が返る）

- [ ] **Step 3: 実装する**

`src/app.py:1626-1634` を以下に置き換える。

```python
@app.get("/api/admin/notifications")
def admin_notifs():
    require_auth(["admin"])
    # システム管理者向け通知は現状なし（空リストを返す）。
    # Phase 2 で一斉通知の配信履歴を返す実装に置き換える。
    return jsonify({"notifications": [], "unread": 0})


@app.put("/api/admin/notifications/read-all")
def admin_notifs_readall():
    require_auth(["admin"])
    return jsonify({"ok": True})
```

- [ ] **Step 4: テストを実行して通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_security.py::TestAdminNotificationsAuth -v`
Expected: 3件とも PASS

- [ ] **Step 5: 全テスト**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 6: コミット**

```bash
git add src/app.py tests/test_security.py
git commit -m "fix(security): 管理者通知エンドポイントの認証欠落を修正

/api/admin/notifications と read-all が require_auth を呼んでおらず
未認証で叩けた。現状は空レスポンスのみで実害は小さいが、実装を足した
時点で認証欠落バグになるため先に塞ぐ。"
```

---

### Task 9: Phase 1 の総仕上げ

**Files:**
- Test: 全体

- [ ] **Step 1: 全ユニットテスト**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS（Phase 1 開始前の709件 + 新規追加分）

- [ ] **Step 2: シフトエンジンの不変量テスト**

Run: `.venv/bin/python tests/run_tests.py`
Expected: PASS

- [ ] **Step 3: 構文チェック**

Run: `node --check public/app.js && .venv/bin/python -c "import ast; ast.parse(open('src/app.py').read())"`
Expected: 両方とも成功（出力なし）

- [ ] **Step 4: E2E テスト**

Run: `npx playwright test`
Expected: 全件 PASS。`/api/init` の無効化で E2E のセットアップが壊れている場合は Task 5 Step 7 の対応が漏れている。

- [ ] **Step 5: 手動確認 — 店舗トグルで店舗名が消えないこと**

```bash
PORT=5555 FLASK_DEBUG=1 .venv/bin/python src/app.py
```

別ターミナルで、管理者としてログインし店舗一覧の有効/無効トグルを押したあと、店舗名が保持されていることを確認する。

```bash
sqlite3 shift.db "SELECT id, shop_code, '['||shop_name||']', is_active FROM shops;"
```

Expected: `shop_name` が空になっていないこと

- [ ] **Step 6: 本番DBの被害確認**

本番（Cloudflare D1）で既にトグルが押されていた場合、店舗名が空になっている可能性がある。確認し、空なら正しい名前に戻す。

```bash
echo "SELECT id, shop_code, shop_name, is_active FROM shops;" | python3 /tmp/d1query.py
```

空文字の店舗があれば、Task 4 で追加した `PUT /api/admin/shops/<sid>` に `{"shop_name": "正しい名前"}` を送って復旧する。

- [ ] **Step 7: 最終コミット**

Phase 1 で未コミットの変更が残っていれば整理してコミットする。

```bash
git status
```

---

### Task 10: 管理者パスワード変更 API（S2）

ブランチ最終レビューで「設計書 §5.3 は S2 を Phase 1 に置いているのに実装計画から欠落
している」と指摘された。S4 で初期パスワードをランダム化したため、変更手段が無いままだと
発行されたランダムパスワードを二度と変更できない。ユーザー判断で Phase 1 に引き上げた。

**UI は作らない**（Phase 2 の `adminSystem` 画面で作る）。API のみ。
管理者アカウントの一覧/追加/削除は Phase 2 Task 5 のまま。

**Files:**
- Modify: `src/app.py`（`admin_notifs_readall` の直後にエンドポイント追加）
- Modify: `public/app.js`（`AUDIT_ACTION_LABELS`）
- Test: `tests/test_admin_accounts.py`（新規。`TestAdminPasswordChange` のみ）

**Interfaces:**
- Produces: `PUT /api/admin/password` body `{current_password, new_password}` → `{"ok": True}`

- [x] **Step 1: 失敗するテストを書く**

`tests/test_admin_accounts.py` に `TestAdminPasswordChange` を置く。Phase 2 Task 5 の
テストコードをそのまま流用し、以下を追加した。

- `test_requires_auth` — 未認証は 401
- `test_change_is_audited` — `admin.password_change` が記録され、そこにパスワードが載らない

- [x] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_accounts.py -q`
Expected: 全件 FAIL（404 が返る）

- [x] **Step 3: 実装する**

`src/app.py` の `admin_notifs_readall`（`/api/admin/notifications/read-all`）の直後に追加。
Phase 2 では `src/admin_api.py` へ切り出す前提なので、他の `/api/admin/*` と同じ場所に置く。

```python
@app.put("/api/admin/password")
def admin_change_password():
    require_auth(["admin"])
    body = request.get_json(silent=True) or {}
    current = body.get("current_password") or ""
    new_pw = body.get("new_password") or ""
    admin_id = (getattr(g, "user", None) or {}).get("id")
    row = query_one("SELECT id, admin_id, password_hash FROM system_admins WHERE id=?",
                    (admin_id,))
    if row is None:
        abort(404, description="管理者が見つかりません")
    if not verify_password(current, row["password_hash"]):
        raise ValueError("現在のパスワードが正しくありません")
    msg = validate_password(new_pw)
    if msg:
        raise ValueError(msg)
    execute("UPDATE system_admins SET password_hash=? WHERE id=?",
            (hash_password(new_pw), admin_id))
    # 他端末のセッションは失効させる。自分の今のセッションだけ残す
    # （変更直後に再ログインを強いられるのは体験が悪いため）。
    token = request.headers.get("Authorization", "")[7:]
    execute("DELETE FROM sessions WHERE role='admin' AND user_id=? AND token<>?",
            (admin_id, token))
    audit("admin.password_change", target_type="system_admin", target_id=admin_id)
    return jsonify({"ok": True})
```

- [x] **Step 4: 監査ログのラベルを追加**

`public/app.js` の `AUDIT_ACTION_LABELS` に `'admin.password_change': '管理者PW変更',` を追加。

- [x] **Step 5: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_admin_accounts.py -q`
Expected: 全件 PASS

- [x] **Step 6: 全テスト・E2E**

Run: `.venv/bin/python -m pytest tests/ -q` / `npx playwright test`
Expected: 既存の件数を下回らないこと

- [x] **Step 7: ドキュメントの整合**

- 設計書 §9: S2 を Phase 2 の項目から Phase 1 側へ移動
- Phase 2 計画 Task 5: パスワード変更部分（Interfaces / テスト / 実装コード / コミットメッセージ）を削除し、
  管理者アカウントの一覧/追加/削除のみにする

---

### Task 11: ブランチ最終レビューの指摘対応

Task 9 の後にブランチ全体のレビューを受け、個別タスクでは見えなかった指摘が出た。
Task 10（S2）以外の対応は以下。詳細は
`.superpowers/sdd/2026-07-26-admin-console-phase1-security/task-10-fix-report.md`。

| 指摘 | 対応 |
|---|---|
| I-1 | `login()` で `shop_code` / `user_code` を 64 文字上限にし、超過は記録せず 400（`_LOGIN_CODE_MAX`） |
| M-2 | `_sanitize_login_code()` で `\r` `\n` を除去してから監査ログ・レート制限キーに載せる |
| I-2 | `_verify_critical_tables()` を追加し、`ensure_db()` の後に必須テーブルの実在を検証。失敗時は起動時に raise |
| M-3 | `/api/init` の 403 メッセージから `ALLOW_INIT` を落とし、運用者向けの案内は print へ |
| M-5 | `require_auth` の admin / staff 分岐も、行が引けなければ 401（shop 分岐と対称に） |
| M-6 | `_check_login_lock` の 429 直前に `auth.login_blocked` を記録。`login_attempts.blocked_logged` でロック期間中1件に制限 |
| M-7 | `logout()` が `_session_actor_name()` で氏名を引いて `actor_name` に渡す |
| M-9 | `tests/test_security.py` の古いコメントを現状に合わせて書き直し |
| M-1 | README の初回セットアップ手順（`ALLOW_INIT` / ランダムパスワード / 後始末）を修正 |

Phase 2 送りにしたもの: I-4（ICS の購読トークン体系）、M-4（`shop_wishes_parse` の 400）、
M-8（`migrations/0005_admin_console.sql`）、`errorhandler(Exception)` の生エラー露出、
`login_attempts` の一括掃除、ProxyFix の信頼プロキシ検証。
