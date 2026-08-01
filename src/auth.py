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


def dummy_verify(password=""):
    """ユーザーが見つからなかった場合でも、パスワード検証と同じ計算コストを払う。

    【なぜ必要か】
      ユーザーが存在しないときに verify_password を短絡すると、PBKDF2 5万回分
      （手元で約3.6ms、CI の共有ランナーで約17ms）を丸ごと省くことになる。
      存在するユーザーでは数ms、存在しないユーザーでは 0.1ms 程度となり、
      応答時間を測るだけで「そのユーザーコードが実在するか」を判別できてしまう
      （ユーザー列挙攻撃）。ログイン失敗は、ユーザーの有無にかかわらず
      同じだけ時間を使う必要がある。

    verify_password と同じく PBKDF2 を1回だけ実行する（結果は捨てる）。
    ダミーのハッシュ値を保持して比較する形にすると、初回だけ生成で
    もう1回余計に計算することになり、回数が呼び出し順に依存してしまう。
    """
    hash_password(password or "")


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
