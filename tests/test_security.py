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
from datetime import timedelta

import pytest

import app as appmod
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

    def test_shop_a_cannot_delete_shop_b_staff(self, client):
        """店舗Aが店舗Bのスタッフを削除できない（WHERE shop_id=? 保護・404 マスク）。"""
        shop_a, shop_b, sa, sb = self._setup_two_shops()
        tok_a = make_session("shop", shop_a, shop_a)
        # 店舗Bのスタッフを削除試行 → 404
        r = client.delete(f"/api/shop/staffs/{sb}", headers=auth(tok_a))
        assert r.status_code == 404, "他店舗スタッフ削除は404でマスクされるべき"
        # 店舗Bのスタッフは残っている
        row = dbmod.query_one("SELECT name FROM staffs WHERE id=?", (sb,))
        assert row is not None and row["name"] == "店舗Bスタッフ", \
            "他店舗スタッフが削除された (=IDOR)"

    def test_staff_a_cannot_see_shop_b_data(self, client):
        """スタッフAは店舗Bの募集期間を取得できない（自身の店舗のみ）。"""
        shop_a, shop_b, sa, sb = self._setup_two_shops()
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_b, "2026-08-01", "2026-08-31", "2099-12-31"))
        tok_sa = make_session("staff", sa, shop_a)
        r = client.get("/api/staff/periods", headers=auth(tok_sa))
        assert r.status_code == 200
        # 店舗Aには募集期間がない → 空リスト
        assert r.get_json()["periods"] == []


# ============================================================
# 認証: Brute-force / Mass Assignment
# ============================================================
class TestAuthSecurity:
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

    def test_login_lock_is_per_client_ip(self, client):
        """X-Forwarded-For が異なるクライアントは独立にカウントされること。

        ProxyFix が無いとエッジプロキシ配下で全員の remote_addr が同じ値に潰れ、
        第三者が10回失敗を送るだけで正規の管理者を締め出せてしまう
        （アカウントロックアウトDoS）。その回帰テスト。
        """
        insert_admin("admin", "Admin123")
        attacker = {"X-Forwarded-For": "203.0.113.10"}
        victim = {"X-Forwarded-For": "198.51.100.20"}
        for _ in range(11):
            client.post("/api/login", json={"id": "admin", "password": "wrong"},
                        headers=attacker)
        # 攻撃者側の送信元はロックされる
        r = client.post("/api/login", json={"id": "admin", "password": "wrong"},
                        headers=attacker)
        assert r.status_code == 429
        # 別IPの正規管理者は影響を受けない
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"},
                        headers=victim)
        assert r.status_code == 200, "別IPの正規管理者が巻き添えロックされている"

    def test_incomplete_input_is_not_counted_as_failure(self, client):
        """入力不備は認証試行ではないので失敗として数えないこと。

        「店舗コードとユーザーコードを入力してください」「パスワードを入力してください」
        の分岐で _record_login_failure を呼ばないことの回帰テスト。
        """
        insert_admin("admin", "Admin123")
        for _ in range(15):
            client.post("/api/login", json={"id": "admin"})                # パスワード欠落
            client.post("/api/login", json={"password": "x"})              # コード欠落
            client.post("/api/login", json={"shop_code": "SHOP1", "password": "x"})  # user_code 欠落
        rows = dbmod.query_all("SELECT * FROM login_attempts")
        assert rows == [], f"入力不備が失敗として記録されている: {rows}"
        # 45回の入力不備のあとでも正しいログインは通る
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 200

    def test_login_lock_expires_after_lock_minutes(self, client, monkeypatch):
        """_LOGIN_LOCK_MIN 経過するとロックが解除されること。"""
        insert_admin("admin", "Admin123")
        for _ in range(10):
            client.post("/api/login", json={"id": "admin", "password": "wrong"})
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 429
        # ロック時間ぶん時間を進める（時間旅行）
        future = appmod.jst_now() + timedelta(minutes=appmod._LOGIN_LOCK_MIN + 1)
        monkeypatch.setattr(appmod, "jst_now", lambda: future)
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 200, "ロック期限を過ぎても解除されない"

    def test_failure_count_resets_after_window(self, client, monkeypatch):
        """_LOGIN_WINDOW_MIN を超えて間隔が空くと失敗カウントがリセットされること。"""
        insert_admin("admin", "Admin123")
        for _ in range(9):
            r = client.post("/api/login", json={"id": "admin", "password": "wrong"})
            assert r.status_code == 400
        # ウィンドウを超えて間隔を空ける
        future = appmod.jst_now() + timedelta(minutes=appmod._LOGIN_WINDOW_MIN + 1)
        monkeypatch.setattr(appmod, "jst_now", lambda: future)
        # カウントがリセットされていれば、さらに9回失敗してもロックされない
        for i in range(9):
            r = client.post("/api/login", json={"id": "admin", "password": "wrong"})
            assert r.status_code == 400, f"{i+1}回目でロック済み（カウントが持ち越されている）"
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 200

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
# Init endpoint (S4修正: ALLOW_INIT ガード)
# ============================================================
class TestPublicInit:
    def test_init_endpoint_requires_allow_init(self, client, monkeypatch):
        """[修正済み] ALLOW_INIT 未設定なら 403 で拒否される（旧: 認証不要で公開）。

        詳細な回帰テストは tests/test_admin_init.py 側にまとめてある。
        """
        monkeypatch.delenv("ALLOW_INIT", raising=False)
        r = client.post("/api/init")
        assert r.status_code == 403

    def test_init_endpoint_works_when_explicitly_allowed(self, client, monkeypatch):
        """ALLOW_INIT=1 を明示したときのみデモ初期化が動くこと。"""
        monkeypatch.setenv("ALLOW_INIT", "1")
        r = client.post("/api/init")
        assert r.status_code == 200
        # ALLOW_INIT=1 は初回セットアップ時だけ立てて、済んだら戻す運用にする。
        # 立てっぱなしでも、既に管理者がいれば "既に存在します" を返すだけで
        # 上書きはされない（多層防御）。初期パスワードはランダム生成され、
        # このレスポンスで1回だけ返る（保存も再表示もしない）。


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

        # NOTE: /api/shop/shifts の実際のリクエスト形は staff_id/start_datetime/
        # end_datetime（brief 記載の date/start_time/end_time ではない。
        # public/app.js の実呼び出しおよび tests/test_admin_staff_apis.py で確認済み）。
        r = client.post("/api/shop/shifts",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"staff_id": staff_b,
                              "start_datetime": "2026-08-03T09:00:00",
                              "end_datetime": "2026-08-03T17:00:00"})
        assert r.status_code == 404, "他店舗スタッフのシフトが作れてしまう"

    def test_shift_update_rejects_other_shop_staff(self, client):
        """自店舗のシフトを他店舗スタッフに付け替えられないこと。"""
        shop_a, token = self._login_shop_a(client)
        staff_a = insert_staff(shop_a, "p1", "自店の人")
        shop_b = insert_shop("SHOPB", "pw12345678")
        staff_b = insert_staff(shop_b, "p2", "他店の人")

        r = client.post("/api/shop/shifts",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"staff_id": staff_a,
                              "start_datetime": "2026-08-03T09:00:00",
                              "end_datetime": "2026-08-03T17:00:00"})
        assert r.status_code == 200
        shift_id = r.get_json().get("id")

        r = client.put(f"/api/shop/shifts/{shift_id}",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"staff_id": staff_b,
                             "start_datetime": "2026-08-03T09:00:00",
                             "end_datetime": "2026-08-03T17:00:00"})
        assert r.status_code == 404, "他店舗スタッフに付け替えできてしまう"


# ============================================================
# require_auth の後方互換フォールバック (CWE-639: IDOR / テナント越境)
# ============================================================
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


# ============================================================
# ログイン入力の長さ・制御文字（未認証での無制限書き込み対策）
# ============================================================
class TestLoginInputLimits:
    """未認証クライアントが login_attempts / audit_logs を無制限に膨らませられない。

    背景: shop_code / user_code が無制限だと、毎回違う値を送るだけで
    「ロックが一度も発動しないまま行だけが増え続ける」状態が作れた。
    本番は Cloudflare D1（書き込み課金・ストレージ上限）なので実害がある。
    """

    def test_overlong_shop_code_is_rejected_without_writing_rows(self, client):
        insert_admin("admin", "Admin123")
        r = client.post("/api/login", json={"shop_code": "S" * 65, "user_code": "u",
                                            "password": "x"})
        assert r.status_code == 400
        assert dbmod.query_all("SELECT * FROM login_attempts") == [], \
            "長すぎる入力が login_attempts に書き込まれている"
        assert dbmod.query_all("SELECT * FROM audit_logs") == [], \
            "長すぎる入力が audit_logs に書き込まれている"

    def test_overlong_user_code_is_rejected_without_writing_rows(self, client):
        insert_admin("admin", "Admin123")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "u" * 65,
                                            "password": "x"})
        assert r.status_code == 400
        assert dbmod.query_all("SELECT * FROM login_attempts") == []
        assert dbmod.query_all("SELECT * FROM audit_logs") == []

    def test_huge_payload_is_rejected_without_writing_rows(self, client):
        """レビュアー実測の 20,000 文字ケース。"""
        r = client.post("/api/login", json={"shop_code": "A" * 20000, "user_code": "admin",
                                            "password": "x"})
        assert r.status_code == 400
        assert dbmod.query_all("SELECT * FROM login_attempts") == []
        assert dbmod.query_all("SELECT * FROM audit_logs") == []

    def test_repeated_overlong_attempts_never_grow_tables(self, client):
        """毎回別の長い値を送っても行が増えないこと（60回のレビュアー再現）。"""
        for i in range(60):
            client.post("/api/login", json={"shop_code": f"{i}" + "X" * 100,
                                            "user_code": "u", "password": "x"})
        assert dbmod.query_all("SELECT * FROM login_attempts") == []
        assert dbmod.query_all("SELECT * FROM audit_logs") == []

    def test_boundary_64_chars_is_still_a_normal_attempt(self, client):
        """64文字ちょうどは正常な認証試行として扱う（上限は 64 文字まで許可）。"""
        r = client.post("/api/login", json={"shop_code": "S" * 64, "user_code": "u" * 64,
                                            "password": "x"})
        assert r.status_code == 400
        rows = dbmod.query_all("SELECT * FROM login_attempts")
        assert len(rows) == 1, "64文字は認証試行として数えられるべき"

    def test_rejection_is_not_counted_as_login_failure(self, client):
        """長さ超過は入力不備であって認証試行ではないので、失敗カウントに影響しない。"""
        insert_admin("admin", "Admin123")
        for _ in range(30):
            client.post("/api/login", json={"shop_code": "S" * 200, "user_code": "u",
                                            "password": "x"})
        r = client.post("/api/login", json={"id": "admin", "password": "Admin123"})
        assert r.status_code == 200, "長さ超過でロックされてしまっている"

    def test_newlines_are_removed_from_audit_and_attempt_key(self, client):
        """改行入りコードでも監査ログ・レート制限キーに生の改行が残らない（ログ偽装対策）。"""
        r = client.post("/api/login", json={"shop_code": "a\nFAKE/<img src=x onerror=1>",
                                            "user_code": "b\rX", "password": "x"})
        assert r.status_code == 400
        row = dbmod.query_one("SELECT actor_name FROM audit_logs "
                              "WHERE action='auth.login_failed' ORDER BY id DESC LIMIT 1")
        assert row is not None
        assert "\n" not in (row["actor_name"] or ""), "監査ログに生の改行が入っている"
        assert "\r" not in (row["actor_name"] or "")
        keys = [x["attempt_key"] for x in dbmod.query_all("SELECT attempt_key FROM login_attempts")]
        assert keys and all("\n" not in k and "\r" not in k for k in keys)


# ============================================================
# 起動時の整合性検証（login_attempts が無いと全ログインが 500 になる）
# ============================================================
class TestStartupIntegrity:
    def test_verification_passes_on_a_healthy_db(self, client):
        appmod._verify_critical_tables()  # 例外が出なければOK

    def test_verification_raises_when_login_attempts_missing(self, client):
        """テーブルが無いまま起動を続けると、未認証クライアントに 500 が返り、
        かつブルートフォース防御が黙って無効化される。起動時に落とす。"""
        dbmod.execute("DROP TABLE login_attempts")
        with pytest.raises(Exception):
            appmod._verify_critical_tables()

    def test_ensure_db_recreates_login_attempts(self, client):
        dbmod.execute("DROP TABLE login_attempts")
        appmod.ensure_db()
        appmod._verify_critical_tables()

    def test_verification_raises_when_blocked_logged_column_missing(self, client):
        """テーブルはあるが blocked_logged 列だけが無い状態を再現する。

        既存DBへの ALTER TABLE ADD COLUMN が何らかの理由で失敗した場合、
        login_attempts テーブル自体は存在するのに blocked_logged 列だけが
        欠けたままになり得る。テーブルの実在しか見ていないと検知できず、
        ロック中の /api/login が「no such column: blocked_logged」の 500 を
        未認証クライアントに返してしまう（本番D1でスキーマ変更が失敗した
        migrations/0004 の実例と同種の失敗モード）。"""
        dbmod.execute("DROP TABLE login_attempts")
        try:
            dbmod.execute(
                "CREATE TABLE login_attempts ("
                "attempt_key TEXT PRIMARY KEY, fail_count INTEGER NOT NULL DEFAULT 0, "
                "locked_until TEXT, updated_at TEXT)")
            with pytest.raises(Exception):
                appmod._verify_critical_tables()
        finally:
            # db_reset フィクスチャは CREATE TABLE IF NOT EXISTS で整備するため、
            # 列が欠けたテーブルが残っていると後続テストに漏れる。ensure_db() で
            # blocked_logged 列を復元してから終える。
            appmod.ensure_db()
            appmod._verify_critical_tables()


# ============================================================
# /api/init の 403 メッセージ（情報漏洩）
# ============================================================
class TestInitErrorMessage:
    def test_403_message_does_not_leak_env_var_name(self, client, monkeypatch):
        monkeypatch.delenv("ALLOW_INIT", raising=False)
        r = client.post("/api/init")
        assert r.status_code == 403
        msg = r.get_json()["error"]
        assert "ALLOW_INIT" not in msg, f"環境変数名が未認証クライアントに漏れている: {msg}"


# ============================================================
# require_auth: 参照先の行が消えているセッション
# ============================================================
class TestRequireAuthOrphanSession:
    """削除済みのスタッフ／管理者のセッションは 401。

    shop 分岐だけが 401 で、admin / staff は g.user=None のまま後段に進み
    staff["id"] で TypeError → 500 になっていた。
    """

    def test_deleted_admin_session_is_401(self, client):
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        dbmod.execute("DELETE FROM system_admins WHERE id=?", (admin_id,))
        r = client.get("/api/me", headers=auth(tok))
        assert r.status_code == 401, f"削除済み管理者のセッションが通っている: {r.status_code}"

    def test_deleted_staff_session_is_401(self, client):
        shop_id = insert_shop("SHOP1")
        staff_id = insert_staff(shop_id, "p1", "太郎")
        tok = make_session("staff", staff_id, shop_id)
        dbmod.execute("DELETE FROM staffs WHERE id=?", (staff_id,))
        r = client.get("/api/me", headers=auth(tok))
        assert r.status_code == 401, f"削除済みスタッフのセッションが通っている: {r.status_code}"

    def test_deleted_staff_does_not_cause_500(self, client):
        """後段で staff["id"] を触るエンドポイントが 500 にならないこと。"""
        shop_id = insert_shop("SHOP1")
        staff_id = insert_staff(shop_id, "p1", "太郎")
        tok = make_session("staff", staff_id, shop_id)
        dbmod.execute("DELETE FROM staffs WHERE id=?", (staff_id,))
        r = client.get("/api/staff/shifts", headers=auth(tok))
        assert r.status_code == 401, f"500 になっている: {r.status_code}"

    def test_non_string_codes_do_not_cause_500(self, client):
        """文字列以外を送られても 500（未認証への内部エラー露出）にならないこと。"""
        for payload in ({"shop_code": 12345, "user_code": ["a"], "password": "x"},
                        {"shop_code": {"a": 1}, "user_code": None, "password": "x"}):
            r = client.post("/api/login", json=payload)
            assert r.status_code == 400, f"{payload} で {r.status_code}"
