"""tests/helpers.py - テスト用データ作成ヘルパ。

conftest の db_reset フィクスチャと組み合わせて使う。
すべて db モジュル経由で直接行を挿入する。
"""
import json
import secrets
from datetime import timedelta

import db as dbmod
from auth import hash_password
from utils import jst_now


def insert_admin(admin_id="admin", password="admin123", name="システム管理者"):
    return dbmod.execute(
        "INSERT INTO system_admins (admin_id, password_hash, name) VALUES (?,?,?)",
        (admin_id, hash_password(password), name),
    )["last_row_id"]


def insert_shop(code="SHOP1", password="shop123", name="テスト店舗", settings=None):
    return dbmod.execute(
        "INSERT INTO shops (shop_code, shop_name, password_hash, settings) VALUES (?,?,?,?)",
        (code, name, hash_password(password), json.dumps(settings or {}, ensure_ascii=False)),
    )["last_row_id"]


def insert_staff(shop_id, code, name, role="part_time", wage=1100, minh=0, maxh=160, password="pt001pass"):
    return dbmod.execute(
        "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role, hourly_wage, "
        "min_hours_per_month, max_hours_per_month) VALUES (?,?,?,?,?,?,?,?)",
        (shop_id, code, hash_password(password), name, role, wage, minh, maxh),
    )["last_row_id"]


def insert_pattern(shop_id, name, st, en, req):
    return dbmod.execute(
        "INSERT INTO shift_patterns (shop_id, pattern_name, start_time, end_time, required_staff) "
        "VALUES (?,?,?,?,?)",
        (shop_id, name, st, en, req),
    )["last_row_id"]


def insert_fixed(staff_id, weekday, st, en):
    return dbmod.execute(
        "INSERT INTO fixed_shifts (staff_id, weekday, start_time, end_time) VALUES (?,?,?,?)",
        (staff_id, weekday, st, en),
    )["last_row_id"]


def insert_request(shop_id, staff_id, day, st, en, availability=None):
    sd, ed = f"{day}T{st}:00", f"{day}T{en}:00"
    if availability:
        return dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, availability) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, staff_id, sd, ed, "requested", availability),
        )["last_row_id"]
    return dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) VALUES (?,?,?,?,?)",
        (shop_id, staff_id, sd, ed, "requested"),
    )["last_row_id"]


def insert_wish(shop_id, staff_id, day, st, en, availability=None, note="テスト"):
    """wish_history に直接 INSERT（テスト用）。

    エンジンは wish_history を入力ソースとするため、希望をエンジンに
    処理させたい場合は insert_request だけでなく insert_wish も呼ぶこと。
    """
    sd, ed = f"{day}T{st}:00", f"{day}T{en}:00"
    try:
        return dbmod.execute(
            "INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, availability, note) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, staff_id, sd, ed, availability, note),
        )["last_row_id"]
    except Exception:
        return None


def make_session(role, user_id, shop_id=None):
    """セッションを直接登録してトークンを返す（ログイン経由を省略）。"""
    token = "tok_" + secrets.token_hex(12)
    expires = (jst_now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    if shop_id is None:
        dbmod.execute(
            "INSERT INTO sessions (token, role, user_id, shop_id, expires_at) VALUES (?,?,?,NULL,?)",
            (token, role, user_id, expires),
        )
    else:
        dbmod.execute(
            "INSERT INTO sessions (token, role, user_id, shop_id, expires_at) VALUES (?,?,?,?,?)",
            (token, role, user_id, shop_id, expires),
        )
    return token


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- 検証ヘルパ ----------
def count_staff_in_hour(shifts, day, hour):
    """ある日のある「1時間帯」(hour:00〜hour+1:00) に勤務するスタッフ数。"""
    target_start = hour * 60
    target_end = (hour + 1) * 60
    cnt = 0
    for s in shifts:
        sd = s.get("start") or s.get("start_datetime")
        ed = s.get("end") or s.get("end_datetime")
        if not sd or sd[:10] != day:
            continue
        ss = int(sd[11:13]) * 60 + int(sd[14:16])
        ee = int(ed[11:13]) * 60 + int(ed[14:16])
        if ss < target_end and ee > target_start:
            cnt += 1
    return cnt
