"""auth.py - 認証ユーティリティ（パスワードハッシュ・セッショントークン）。"""
import hashlib
import hmac
import secrets

_SALT = b"shift_saas_salt_v1"
_ITER = 50000


def hash_password(password):
    """PBKDF2-HMAC-SHA256 でハッシュ化（16進数文字列）。"""
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), _SALT, _ITER, 32).hex()


def verify_password(password, hash_value):
    """パスワードがハッシュと一致するか検証（定時間比較）。"""
    if not hash_value:
        return False
    try:
        return hmac.compare_digest(hash_password(password), str(hash_value))
    except Exception:
        return False


def gen_token():
    """ランダムなセッショントークンを生成。"""
    return secrets.token_hex(24)


def strip_password(user):
    """ユーザーdictから password_hash を除去（レスポンス用）。"""
    if not user:
        return user
    user = dict(user)
    user.pop("password_hash", None)
    return user
