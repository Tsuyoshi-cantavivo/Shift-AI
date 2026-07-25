"""tests/test_wish_honored_over_min_daily.py

回帰: 明示的な時間指定希望が min_daily 未満でも尊重され、
その時間帯に社員が上乗せ配置（過剰配置）されないこと。

再現していたバグ:
  必要人数1、パターン 04:00-07:00。バイトが 04:00-06:00(2h) を希望。
  min_daily=4h のため希望が「調整待ち」に落ち、社員が 04:00-07:00 全体を
  埋める → 希望(04-06)と社員(04-07)が重なり過剰配置になっていた。
期待:
  希望 04:00-06:00 が placed され、社員は残りの 06:00-07:00 のみを埋める。
  どのスロットも配置人数が required(=1) を超えない。
"""
import shift_engine
from helpers import insert_shop, insert_staff, insert_pattern, insert_wish

DAY = "2026-08-01"
SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6, "default_hourly_wage": 1100}


def _slot_coverage(confirmed, gran=30):
    """confirmed 配置から (day, slot_min)->人数 を作る。"""
    cov = {}
    for c in confirmed:
        for sl in shift_engine._shift_slots(c["start"], c["end"], gran):
            key = (c["start"][:10], sl)
            cov[key] = cov.get(key, 0) + 1
    return cov


def test_short_timed_wish_is_placed_not_pending():
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "通", "04:00", "07:00", 1)  # 必要1
    pt = insert_staff(shop_id, "P1", "バイト太郎", "part_time", 1100)
    insert_staff(shop_id, "E1", "社員花子", "employee", 2000, 0, 200)
    insert_wish(shop_id, pt, DAY, "04:00", "06:00")  # 2h < min_daily(4h)

    res = shift_engine.auto_generate(shop_id, SETTINGS, DAY, DAY)

    # 希望(04:00-06:00)が placed されている（pending に落ちていない）
    placed_pt = [c for c in res["confirmed"]
                 if c["staff_id"] == pt and c["start"].endswith("T04:00:00")
                 and c["end"].endswith("T06:00:00")]
    assert placed_pt, f"希望が配置されていない: confirmed={res['confirmed']} pending={res['pending']}"


def test_no_over_staffing_over_wished_band():
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "通", "04:00", "07:00", 1)
    pt = insert_staff(shop_id, "P1", "バイト太郎", "part_time", 1100)
    insert_staff(shop_id, "E1", "社員花子", "employee", 2000, 0, 200)
    insert_wish(shop_id, pt, DAY, "04:00", "06:00")

    res = shift_engine.auto_generate(shop_id, SETTINGS, DAY, DAY)

    cov = _slot_coverage(res["confirmed"])
    over = {k: v for k, v in cov.items() if v > 1}
    assert not over, f"必要人数1を超える過剰配置がある: {over} / confirmed={res['confirmed']}"
