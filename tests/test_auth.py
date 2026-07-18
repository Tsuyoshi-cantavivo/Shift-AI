"""tests/test_auth.py - auth.py ユニットテスト（PBKDF2・セッショントークン）。"""
import auth


class TestPasswordHashing:
    def test_hash_password_deterministic(self):
        """同じパスワードは同じハッシュ（固定ソルト）。"""
        assert auth.hash_password("abc12345") == auth.hash_password("abc12345")

    def test_hash_password_different_inputs(self):
        """異なるパスワードは異なるハッシュ。"""
        assert auth.hash_password("abc12345") != auth.hash_password("abc12346")

    def test_hash_password_format(self):
        """ハッシュは64文字(32 bytes hex)。"""
        h = auth.hash_password("Password1")
        assert len(h) == 64
        int(h, 16)  # hexとして有効

    def test_hash_password_none(self):
        """None は空文字扱い（例外にしない）。"""
        h = auth.hash_password(None)
        assert isinstance(h, str) and len(h) == 64

    def test_hash_password_empty(self):
        h = auth.hash_password("")
        assert len(h) == 64

    def test_hash_password_unicode(self):
        """マルチバイト文字も OK。"""
        h = auth.hash_password("パスワード1234")
        assert len(h) == 64

    def test_hash_password_long(self):
        """10000文字でも OK。"""
        h = auth.hash_password("a" * 10000)
        assert len(h) == 64


class TestVerifyPassword:
    def test_verify_correct_password(self):
        h = auth.hash_password("Secret123")
        assert auth.verify_password("Secret123", h) is True

    def test_verify_wrong_password(self):
        h = auth.hash_password("Secret123")
        assert auth.verify_password("Wrong456", h) is False

    def test_verify_empty_stored_hash(self):
        """保存ハッシュ空 → False。"""
        assert auth.verify_password("anything", "") is False
        assert auth.verify_password("anything", None) is False

    def test_verify_invalid_stored_hash(self):
        """不正フォーマットの保存ハッシュ → 例外を吐かず False。"""
        assert auth.verify_password("x", "not-a-hex") is False

    def test_verify_constant_time_no_exception(self):
        """定時間比較は例外を投げない。"""
        h = auth.hash_password("A" * 8 + "1")
        # 短い入力/長い入力でも例外なく判定
        assert auth.verify_password("", h) is False
        assert auth.verify_password("A" * 10000, h) is False


class TestGenToken:
    def test_gen_token_random(self):
        """毎回異なるトークン。"""
        a, b = auth.gen_token(), auth.gen_token()
        assert a != b

    def test_gen_token_length(self):
        """token_hex(24) → 48文字の hex。"""
        t = auth.gen_token()
        assert len(t) == 48
        int(t, 16)

    def test_gen_token_entropy(self):
        """1000件生成して全てユニーク。"""
        tokens = {auth.gen_token() for _ in range(1000)}
        assert len(tokens) == 1000


class TestStripPassword:
    def test_strip_removes_hash(self):
        u = {"id": 1, "name": "x", "password_hash": "secret"}
        out = auth.strip_password(u)
        assert "password_hash" not in out
        assert out["id"] == 1

    def test_strip_does_not_mutate_input(self):
        u = {"id": 1, "password_hash": "secret"}
        auth.strip_password(u)
        assert u["password_hash"] == "secret"  # 元は変更されない

    def test_strip_none(self):
        assert auth.strip_password(None) is None

    def test_strip_no_hash_key(self):
        u = {"id": 1, "name": "x"}
        out = auth.strip_password(u)
        assert out == {"id": 1, "name": "x"}
