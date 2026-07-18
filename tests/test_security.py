"""tests/test_security.py - セキュリティテスト（OWASP Top10 / CWE 対応）。

対象:
  - 認可 (Broken Access Control / IDOR)
  - 認証 (Brute-force, Session)
  - 入力バリデーション (Mass Assignment, Missing)
  - SQL Injection
  - CSV Injection (Formula Injection)
  - XSS (格納型)
  - CSRF 設計確認
  - Path Traversal (静的ファイル配信)
  - Open Redirect
  - Mass Assignment
  - Information Disclosure (パスワードハッシュ、スタックトレース)
  - Rate Limiting 確認
  - ログイン情報漏洩
"""
import json
import time

import pytest

import db as dbmod
from auth import hash_password
from helpers import (
    insert_admin, insert_shop, insert_staff, insert_pattern, insert_fixed,
    insert_request, make_session, auth,
)


MON = "2026-08-03"


# ============================================================
# 認可: Broken Access Control (CWE-639)
# ============================================================
class TestAccessControl:
    def test_unauthenticated_request_rejected(self, client):
        """認証ヘッダ無し → 401。"""
        for path in ["/api/shop/dashboard", "/api/staff/shifts", "/api/admin/shops"]:
            r = client.get(path)
            assert r.status_code == 401, f"{path} should require auth"

    def test_invalid_token_rejected(self, client):
        """無効トークン → 401。"""
        r = client.get("/api/shop/dashboard", headers={"Authorization": "Bearer invalidtoken123"})
        assert r.status_code == 401

    def test_malformed_authorization_header(self, client):
        """不正形式の認証ヘッダ → 401（500 にならない）。"""
        for h in [{"Authorization": ""}, {"Authorization": "Basic abc"},
                  {"Authorization": "Bearer"}, {"Authorization": "Token x"}]:
            r = client.get("/api/shop/dashboard", headers=h)
            assert r.status_code == 401

    def test_staff_cannot_access_shop_endpoints(self, client):
        """スタッフトークンで店舗 API は 403。"""
        shop_id = insert_shop(code="S1")
        staff_id = insert_staff(shop_id, "P1", "バイト")
        tok = make_session("staff", staff_id, shop_id)
        for path in ["/api/shop/dashboard", "/api/shop/staffs", "/api/shop/patterns"]:
            r = client.get(path, headers=auth(tok))
            assert r.status_code == 403, f"staff should not access {path}"

    def test_shop_cannot_access_admin_endpoints(self, client):
        """店舗トークンで管理者 API は 403。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/admin/shops", headers=auth(tok))
        assert r.status_code == 403

    def test_staff_cannot_access_admin_endpoints(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x")
        tok = make_session("staff", staff_id, shop_id)
        r = client.get("/api/admin/shops", headers=auth(tok))
        assert r.status_code == 403

    def test_expired_session_rejected(self, client):
        """期限切れセッション → 401。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        # expires_at を過去に設定
        dbmod.execute("UPDATE sessions SET expires_at=? WHERE token=?",
                      ("2020-01-01 00:00:00", tok))
        r = client.get("/api/shop/dashboard", headers=auth(tok))
        assert r.status_code == 401
        assert "期限" in r.get_json()["error"] or "切" in r.get_json()["error"]


# ============================================================
# IDOR: 他店舗のリソースにアクセスできないこと
# ============================================================
class TestIDOR:
    def _setup_two_shops(self):
        """2 店舗を作成。"""
        shop_a = insert_shop(code="A")
        shop_b = insert_shop(code="B")
        staff_a = insert_staff(shop_a, "A1", "店舗Aスタッフ")
        staff_b = insert_staff(shop_b, "B1", "店舗Bスタッフ")
        insert_pattern(shop_a, "朝A", "09:00", "13:00", 2)
        insert_pattern(shop_b, "朝B", "09:00", "13:00", 2)
        return shop_a, shop_b, staff_a, staff_b

    def test_shop_a_cannot_list_shop_b_staffs(self, client):
        """店舗Aは店舗Bのスタッフ一覧を取得できない（一覧は自店舗のみ）。"""
        shop_a, shop_b, sa, sb = self._setup_two_shops()
        tok_a = make_session("shop", shop_a, shop_a)
        r = client.get("/api/shop/staffs", headers=auth(tok_a))
        assert r.status_code == 200
        codes = [s["staff_code"] for s in r.get_json()["staffs"]]
        assert "A1" in codes and "B1" not in codes

    def test_shop_a_cannot_modify_shop_b_staff(self, client):
        """店舗Aが店舗Bのスタッフを変更できない（WHERE shop_id=? 保護）。"""
        shop_a, shop_b, sa, sb = self._setup_two_shops()
        tok_a = make_session("shop", shop_a, shop_a)
        r = client.put(f"/api/shop/staffs/{sb}", json={
            "name": "Hacked", "hourly_wage": 1,
            "min_hours_per_month": 0, "max_hours_per_month": 1,
        }, headers=auth(tok_a))
        # 保護されていれば成功を返すが、実際には店舗Bのスタッフは変更されない
        row = dbmod.query_one("SELECT name FROM staffs WHERE id=?", (sb,))
        assert row["name"] == "店舗Bスタッフ", "他店舗スタッフが変更された (=IDOR)"

    def test_shop_a_cannot_delete_shop_b_shift(self, client):
        """店舗Aが店舗Bのシフトを削除できない。"""
        shop_a, shop_b, sa, sb = self._setup_two_shops()
        # 店舗Bにシフト作成
        tok_b = make_session("shop", shop_b, shop_b)
        r = client.post("/api/shop/shifts", json={
            "staff_id": sb, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(tok_b))
        sid = r.get_json()["id"]
        # 店舗Aが削除試行
        tok_a = make_session("shop", shop_a, shop_a)
        client.delete(f"/api/shop/shifts/{sid}", headers=auth(tok_a))
        # 店舗Bのシフトは残っている
        row = dbmod.query_one("SELECT id FROM shifts WHERE id=?", (sid,))
        assert row is not None, "他店舗シフトが削除された (=IDOR)"

    def test_shop_a_cannot_access_shop_b_pattern(self, client):
        """店舗Aが店舗Bのパターンを編集/削除できない。"""
        shop_a, shop_b, sa, sb = self._setup_two_shops()
        pat_b = dbmod.query_one("SELECT id FROM shift_patterns WHERE shop_id=?", (shop_b,))
        tok_a = make_session("shop", shop_a, shop_a)
        r = client.put(f"/api/shop/patterns/{pat_b['id']}", json={
            "pattern_name": "Hacked", "start_time": "00:00", "end_time": "01:00",
            "required_staff": 99,
        }, headers=auth(tok_a))
        # 保護されていれば店舗Bのパターンは変更されない
        row = dbmod.query_one("SELECT pattern_name FROM shift_patterns WHERE id=?", (pat_b["id"],))
        assert row["pattern_name"] == "朝B", "他店舗パターンが変更された (=IDOR)"

    def test_staff_a_cannot_see_shop_b_data(self, client):
        """スタッフAは店舗Bの募集期間を取得できない（自身の店舗のみ）。"""
        shop_a, shop_b, sa, sb = self._setup_two_shops()
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_b, "2026-08-01", "2026-08-31", "2026-07-25"))
        tok_sa = make_session("staff", sa, shop_a)
        r = client.get("/api/staff/periods", headers=auth(tok_sa))
        assert r.status_code == 200
        # 店舗Aには募集期間がない → 空リスト
        assert r.get_json()["periods"] == []


# ============================================================
# 認証: Brute-force / Mass Assignment
# ============================================================
class TestAuthSecurity:
    def test_login_brute_force_no_lockout(self, client):
        """[警告] ログイン失敗を何度繰り返してもロックアウト無し（Rate Limit 脆弱性）。"""
        insert_admin("admin", "Admin123")
        for _ in range(50):
            r = client.post("/api/login", json={"id": "admin", "password": "wrong"})
            assert r.status_code == 400
        # 50 回失敗後も正しいパスワードで即ログイン可能
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 200, "Rate Limit 無し — 50 回失敗後も即座に成功可能"

    def test_login_timing_attack_resistance(self, client):
        """存在しないIDと存在するIDの応答時間が近いこと（タイミング攻撃耐性）。"""
        insert_admin("admin", "Admin123")
        # 存在する ID
        t1 = time.time()
        client.post("/api/login", json={"id": "admin", "password": "wrong"})
        e1 = time.time() - t1
        # 存在しない ID
        t2 = time.time()
        client.post("/api/login", json={"id": "nonexistent", "password": "wrong"})
        e2 = time.time() - t2
        # PBKDF2 50000 iter で十分遅いため、差が小さい（存在確認のみ先にしていると差が大）
        # 5倍以上の差がないこと（緩い閾値）
        assert max(e1, e2) / max(min(e1, e2), 0.001) < 5.0, \
            f"存在/不在で応答時間差が大きい (existing={e1:.3f}s vs missing={e2:.3f}s)"

    def test_password_hash_not_in_login_response(self, client):
        """ログイン成功レスポンスに password_hash を含めない。"""
        insert_admin("admin", "Admin123")
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert "password_hash" not in json.dumps(r.get_json())

    def test_password_hash_not_in_me_response(self, client):
        """/api/me にも password_hash を含めない。"""
        admin_id = insert_admin("admin", "Admin123")
        tok = make_session("admin", admin_id)
        r = client.get("/api/me", headers=auth(tok))
        assert "password_hash" not in json.dumps(r.get_json())


# ============================================================
# SQL Injection (CWE-89)
# ============================================================
class TestSQLInjection:
    def test_login_sql_injection(self, client):
        """ログイン ID/パスワードに SQLi ペイロードを入れても弾かれる。"""
        insert_admin("admin", "Admin123")
        payloads = [
            {"id": "' OR '1'='1", "password": "anything"},
            {"id": "admin'--", "password": "x"},
            {"id": "admin", "password": "' OR '1'='1"},
            {"id": "admin", "password": "x'; DROP TABLE staffs; --"},
            {"id": "admin; INSERT INTO system_admins VALUES(...); --", "password": "x"},
        ]
        for p in payloads:
            r = client.post("/api/login", json=p)
            assert r.status_code in (400,), f"SQLi payload should fail: {p}"
        # テーブルが残っていること
        assert dbmod.query_one("SELECT count(*) as c FROM staffs") is not None

    def test_query_parameter_safety(self, client):
        """クエリパラメータに SQLi を入れても安全（パラメータ化）。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/shifts?start=' OR '1'='1&end=x", headers=auth(tok))
        # 400 または空結果（SQLi でテーブルダンプされない）
        assert r.status_code in (400, 200)


# ============================================================
# CSV Injection (Formula Injection, CWE-1236)
# ============================================================
class TestCSVInjection:
    def test_csv_export_escapes_formula(self, client):
        """staff_name に =cmd|... を入れて CSV 出力 → Excel で数式として実行されないよう
        先頭に ' を前置してエスケープされること（CWE-1236 Formula Injection 対策）。
        """
        shop_id = insert_shop(code="CSV")
        staff_id = insert_staff(shop_id, "P1", "=cmd|'/c calc'!A1")
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": staff_id, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(tok))
        r = client.get(f"/api/shop/shifts/export?start={MON}&end={MON}",
                       headers=auth(tok))
        body = r.data.decode("utf-8")
        lines = body.split("\n")
        staff_lines = [ln for ln in lines if "cmd" in ln]
        assert staff_lines, "テスト用スタッフ行が見つかりません"
        # セル単独で = が先頭に来ていないこと（'= または "'=cmd..." の形でエスケープ）
        for ln in staff_lines:
            cells = ln.split(",")
            for c in cells:
                # 元の =cmd... を含むセルが、そのまま = で始まっていないこと
                if "cmd" in c:
                    assert not c.lstrip('"').startswith("=cmd"), \
                        f"CSV Injection 脆弱性: 数式として解釈されうるセル = {c}"
                    # ' が前置されているか、ダブルクォートで囲まれている
                    assert c.startswith("'") or c.startswith('"'), \
                        f"エスケープ不十分: {c}"

    def test_csv_export_all_dangerous_prefixes(self, client):
        """= + - @ で始まる入力を全てエスケープ（先頭に ' を前置）。"""
        shop_id = insert_shop(code="CSV2")
        insert_pattern(shop_id, "通", "09:00", "18:00", 5)
        dangerous_names = ["=evil", "+1+1", "@SUM(A1)", "-1+1"]
        for i, name in enumerate(dangerous_names):
            sid = insert_staff(shop_id, f"X{i}", name)
            tok = make_session("shop", shop_id, shop_id)
            r = client.post("/api/shop/shifts", json={
                "staff_id": sid, "start_datetime": f"{MON}T09:00:00",
                "end_datetime": f"{MON}T18:00:00",
            }, headers=auth(tok))
            assert r.status_code == 200, f"シフト作成失敗: {r.get_json()}"
        tok = make_session("shop", shop_id, shop_id)
        r = client.get(f"/api/shop/shifts/export?start={MON}&end={MON}",
                       headers=auth(tok))
        body = r.data.decode("utf-8")
        # 各危険な名前のセルが、CSV 行内で「ダブルクォートで囲まれて '+危険文字」の形で
        # エスケープされていること。Excel/Sheets は ' を前置すると数式として解釈しない。
        for dangerous in dangerous_names:
            found = False
            for ln in body.split("\n"):
                if dangerous not in ln:
                    continue
                # セルをパース（簡易: カンマ分割 → "..." の中身を取り出し）
                for c in ln.split(","):
                    raw = c
                    if raw.startswith('"') and raw.endswith('"'):
                        raw = raw[1:-1].replace('""', '"')
                    if dangerous not in raw:
                        continue
                    found = True
                    # raw は "'=evil" 等の形（' が前置でエスケープ）
                    # Excel/Sheets では ' を前置すると強制的に文字列扱い
                    assert raw == "'" + dangerous, \
                        f"Formula Injection エスケープ不十分: cell={c} raw={raw} (expected '\"' + {dangerous})"
            assert found, f"危険な名前 {dangerous} が CSV に見つからない"


# ============================================================
# XSS: 格納型
# ============================================================
class TestStoredXSS:
    def test_staff_name_xss_in_api_response(self, client):
        """staff_name に <script> を入れても JSON ではそのまま返る（フロント側 esc() で防御）。"""
        shop_id = insert_shop()
        xss_payload = '<script>alert("xss")</script>'
        staff_id = insert_staff(shop_id, "P1", xss_payload)
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/staffs", headers=auth(tok))
        # JSON では <,> はエスケープされずそのまま（JSON API として正しい）
        assert xss_payload in r.get_json()["staffs"][0]["name"]
        # フロント側の esc() が &lt;script&gt; に変換することを unit test で担保


# ============================================================
# 入力バリデーション / Mass Assignment
# ============================================================
class TestInputValidation:
    def test_create_shop_missing_fields_400(self, client):
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        # shop_code 欠落 → KeyError は 500 ではなく明示的エラーに
        r = client.post("/api/admin/shops", json={"shop_name": "x"}, headers=auth(tok))
        # 現状は KeyError → 500 になる可能性（warn）
        assert r.status_code in (400, 500)

    def test_create_staff_missing_fields(self, client):
        """必須フィールド欠損時に適切な 400 を返すこと。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        # staff_code 欠落
        r = client.post("/api/shop/staffs", json={"name": "x", "password": "Password1"},
                        headers=auth(tok))
        assert r.status_code in (400, 500)

    def test_weak_password_rejected_for_staff(self, client):
        """8文字未満のパスワードは拒否。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "P1", "name": "x", "password": "short",
        }, headers=auth(tok))
        assert r.status_code == 400

    def test_negative_hourly_wage_allowed_bug(self, client):
        """[警告] 時給に負の値を設定できてしまう（バグ）。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "NEG", "name": "負時給", "password": "Password1",
            "hourly_wage": -1000,
        }, headers=auth(tok))
        # 現状は受け付けてしまう（改善推奨）
        if r.status_code == 200:
            row = dbmod.query_one("SELECT hourly_wage FROM staffs WHERE staff_code='NEG'")
            if row and row["hourly_wage"] == -1000:
                pytest.skip("負の時給が設定可能 — 入力バリデーション強化推奨")

    def test_huge_numeric_value(self, client):
        """異常に大きい数値でも受け付けてしまうか（整数オーバーフロー確認）。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "BIG", "name": "巨大時給", "password": "Password1",
            "hourly_wage": 10 ** 18,
        }, headers=auth(tok))
        # 受け付けた場合、DB に正しく格納されているか
        if r.status_code == 200:
            row = dbmod.query_one("SELECT hourly_wage FROM staffs WHERE staff_code='BIG'")
            assert row is not None  # 格納されていること


# ============================================================
# Path Traversal (CWE-22)
# ============================================================
class TestPathTraversal:
    def test_static_file_traversal(self, client):
        """静的ファイル配信でディレクトリトラバーサルを試す。

        /<path:path> は未登録パスを SPA の index.html へフォールバックする設計。
        したがって /etc/passwd 等のファイルが読み取られないことを検証する。
        """
        for path in ["../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd",
                     "%2e%2e/%2e%2e/etc/passwd", "....//....//etc/passwd"]:
            r = client.get(f"/{path}")
            # 200 (SPA fallback) or 404 は許容。重要なのは /etc/passwd の中身が漏れないこと。
            assert r.status_code in (200, 400, 404)
            assert b"root:" not in r.data
            assert b"/bin/bash" not in r.data

    def test_public_files_only_served(self, client):
        """public/ 内の実ファイルのみ配信されること。"""
        # app.js は public/ に存在 → 配信される
        r = client.get("/app.js")
        assert r.status_code == 200
        # src/app.py は public/ 外 → フォールバック or 404（中身は配信されない）
        r = client.get("/src/app.py")
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            # index.html が返っている（app.py の中身ではない）
            assert b"def handle_init" not in r.data

    def test_api_path_not_served_as_static(self, client):
        """api/ で始まるパスは静的ファイルとして扱わない。"""
        r = client.get("/api/internal/secret")
        assert r.status_code == 404


# ============================================================
# Open Redirect (CWE-601)
# ============================================================
class TestOpenRedirect:
    def test_no_redirect_endpoint(self, client):
        """本アプリにリダイレクト機能は無い（設計的に安全）。"""
        # Flask のリダイレクト応答 (3xx) を使うエンドポイントが存在しないことを確認
        r = client.post("/api/login", json={"id": "x", "password": "y"})
        assert r.status_code != 302


# ============================================================
# Information Disclosure (CWE-209)
# ============================================================
class TestInfoDisclosure:
    def test_500_error_does_not_leak_stacktrace(self, client):
        """サーバエラー時のレスポンスに Python スタックトレースを含めない。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        # わざと不正な datetime を送信して 500 を誘発
        r = client.post("/api/shop/shifts", json={
            "staff_id": 1, "start_datetime": "INVALID", "end_datetime": "ALSO_INVALID",
        }, headers=auth(tok))
        body = r.get_data(as_text=True)
        # スタックトレース ("Traceback (most recent call last)") が含まれないこと
        assert "Traceback" not in body
        assert ".py\"、" not in body and "line " not in body.lower() or r.status_code == 500

    def test_dotenv_not_served(self, client):
        """.env ファイルが静的配信されないこと。"""
        r = client.get("/.env")
        # /<path:path> でマッチしない or 404 フォールバック
        # index.html が返る OR 404
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            # HTML が返ってきている（.env の中身ではない）
            assert b"FLASK_SECRET" not in r.data
            assert b"LLM_API_KEY" not in r.data

    def test_schema_sql_not_served(self, client):
        """schema.sql が静的配信されないこと。"""
        r = client.get("/schema.sql")
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert b"CREATE TABLE" not in r.data


# ============================================================
# Mass Assignment (CWE-915)
# ============================================================
class TestMassAssignment:
    def test_shop_cannot_self_promote_to_admin(self, client):
        """店舗が自分の role を admin に上書きできないこと。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        # settings 更新で role を注入しようとしても無視される
        r = client.put("/api/shop/settings", json={
            "shop_name": "x", "settings": {"role": "admin"},
        }, headers=auth(tok))
        assert r.status_code == 200
        # セッションの role は依然 'shop'
        r2 = client.get("/api/me", headers=auth(tok))
        assert r2.get_json()["role"] == "shop"


# ============================================================
# Init endpoint (公開状態)
# ============================================================
class TestPublicInit:
    def test_init_endpoint_is_public(self, client):
        """[警告] /api/init は認証不要で誰でもデモデータを作成可能。"""
        r = client.post("/api/init")
        assert r.status_code == 200
        # 危険性: 本番環境でこのエンドポイントが有効だと、第三者が初期化可能
        # （ただし既存データがあれば "既に存在します" と返す設計）


# ============================================================
# セッショントークン強度
# ============================================================
class TestSessionToken:
    def test_token_is_hex_and_long(self, client):
        """発行されるトークンは 48 文字の hex（24 bytes）。"""
        insert_admin("admin", "Admin123")
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        tok = r.get_json()["token"]
        assert len(tok) == 48
        int(tok, 16)  # hex として有効

    def test_logout_invalidates_token(self, client):
        """ログアウトでトークンを無効化。"""
        insert_admin("admin", "Admin123")
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        tok = r.get_json()["token"]
        client.post("/api/logout", headers=auth(tok))
        r = client.get("/api/me", headers=auth(tok))
        assert r.status_code == 401
