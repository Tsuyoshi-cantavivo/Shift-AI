"""tests/run_tests.py - シフトエンジンの包括テスト（バグ検出強化版）。

実行: ./.venv/bin/python tests/run_tests.py

構成:
  [正常系] T1-T5 基本不変量
    T1: 同一スタッフの1日シフトは1つまで（中抜け/重複禁止）
    T2: 同一スタッフのシフト時間重複なし
    T3: 各(日,パターン)の配置が required を超えない（過剰配置禁止）
    T4: shortage リストが未カバー枠を正確に反映する
    T5: 休憩時間が労基法に合致（6h超→45分, 8h超→60分）
  [正常系] T6 固定縛りすぎ検出
    固定シフトがある場合でも、固定の無い日は未カバー枠へ社員を自動配置する
  [正常系] バイト希望経路
    Step2a: 時間指定希望の配置 / Step2b: 柔軟希望(availability)の配置
  [バグ検出] BUG#1 月間上限チェック欠落
    Cloudflare版は Step3 で minutesByStaff+work > 上限 をスキップ、Flask版は無チェック。
  [バグ検出] BUG#2 カバレッジ集計のoverlap過大評価
    固定9-18が夜17-22と1時間重なるだけで「カバー扱い」→ 未カバーが不足に現れない。
  [バグ検出] BUG#3 compute_shortage のキー名不整合
    auto_generate の出力(start/end) を compute_shortage(start_datetime/end_datetime) へ渡すと KeyError。

※ バグ検出テストは「正しい挙動」を主張するため、現状コードのバグにより
   現在は FAIL/ERROR になる＝バグ存在の確認。修正後に PASS に転じる。
"""
import os
import sys
import sqlite3
import hashlib
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import db as _db
import shift_engine
from utils import shift_covers_pattern, minutes_between, compute_break_minutes

BASE = os.path.join(os.path.dirname(__file__), "..")
SCHEMA = os.path.join(BASE, "schema.sql")
WD = {"月": 1, "火": 2, "水": 3, "木": 4, "金": 5, "土": 6, "日": 0}


# ---------------- テスト用ヘルパ ----------------
def h(p):
    return hashlib.pbkdf2_hmac("sha256", p.encode(), b"s", 50000, 32).hex()


def use_db(path):
    """テストごとに独立した一時DBへ切替え、スキーマを初期化する。"""
    if os.path.exists(path):
        os.remove(path)
    _db.DB_PATH = path
    _db.init_schema(SCHEMA)
    return sqlite3.connect(path)


def mkshop(c, code="S1", settings='{"min_daily_hours":4,"max_consecutive_days":6}'):
    c.execute(
        "INSERT INTO shops (shop_code,shop_name,password_hash,settings) VALUES (?,?,?,?)",
        (code, code, h("x"), settings),
    )
    return c.lastrowid


def mkstaff(c, shop, code, name, role="part_time", maxh=120, minh=0, wage=1100):
    c.execute(
        "INSERT INTO staffs (shop_id,staff_code,password_hash,name,role,hourly_wage,"
        "min_hours_per_month,max_hours_per_month) VALUES (?,?,?,?,?,?,?,?)",
        (shop, code, h("x"), name, role, wage, minh, maxh),
    )
    return c.lastrowid


def mkfixed(c, staff, weekday, st, en):
    c.execute(
        "INSERT INTO fixed_shifts (staff_id,weekday,start_time,end_time) VALUES (?,?,?,?)",
        (staff, weekday, st, en),
    )


def mkpat(c, shop, name, st, en, req):
    c.execute(
        "INSERT INTO shift_patterns (shop_id,pattern_name,start_time,end_time,required_staff) "
        "VALUES (?,?,?,?,?)",
        (shop, name, st, en, req),
    )


def mkreq(c, shop, staff, day, st, en, availability=None):
    sd, ed = f"{day}T{st}:00", f"{day}T{en}:00"
    if availability:
        c.execute(
            "INSERT INTO shifts (shop_id,staff_id,start_datetime,end_datetime,status,availability) "
            "VALUES (?,?,?,?,?,?)",
            (shop, staff, sd, ed, "requested", availability),
        )
    else:
        c.execute(
            "INSERT INTO shifts (shop_id,staff_id,start_datetime,end_datetime,status) "
            "VALUES (?,?,?,?,?)",
            (shop, staff, sd, ed, "requested"),
        )


def by_day(shifts):
    d = defaultdict(list)
    for s in shifts:
        d[s["start"][:10]].append(s)
    return d


# ---------------- テストランナ ----------------
results = []


def run(name, category, fn):
    try:
        fn()
        results.append((category, name, "PASS", ""))
        print(f"[PASS] {name}")
    except AssertionError as e:
        results.append((category, name, "FAIL", str(e)))
        print(f"[FAIL] {name}  <- {e}")
    except Exception as e:  # noqa: BLE001
        results.append((category, name, "ERROR", f"{type(e).__name__}: {e}"))
        print(f"[ERR ] {name}  <- {type(e).__name__}: {e}")


# ============================================================
# [正常系] T1-T5: 基本不変量
# ============================================================
def test_basic_invariants():
    conn = use_db("/tmp/t_basic.db")
    c = conn.cursor()
    shop = mkshop(c)
    e1 = mkstaff(c, shop, "E1", "社員A", "employee", maxh=200)
    e2 = mkstaff(c, shop, "E2", "社員B", "employee", maxh=200)
    p1 = mkstaff(c, shop, "P1", "バイトC", "part_time", maxh=120)
    for w in range(1, 6):  # 社員Aは月-金 9-18固定
        mkfixed(c, e1, w, "09:00", "18:00")
    mkpat(c, shop, "朝", "09:00", "13:00", 1)
    mkpat(c, shop, "昼", "13:00", "17:00", 1)
    mkpat(c, shop, "夜", "17:00", "22:00", 3)
    conn.commit()
    conn.close()

    res = shift_engine.auto_generate(
        shop, {"min_daily_hours": 4, "max_consecutive_days": 6}, "2026-08-01", "2026-08-07"
    )
    shifts = res["confirmed"]
    name = {e1: "社員A", e2: "社員B", p1: "バイトC"}
    days = by_day(shifts)

    # T1: 同一スタッフの1日シフトは1つまで
    for day, lst in days.items():
        per_staff = defaultdict(int)
        for s in lst:
            per_staff[s["staff_id"]] += 1
        for sid, n in per_staff.items():
            assert n <= 1, f"T1 {day} {name.get(sid, sid)} が{n}シフト(中抜け/重複)"

    # T2: 同一スタッフのシフト時間重複なし
    for day, lst in days.items():
        per_staff = defaultdict(list)
        for s in lst:
            per_staff[s["staff_id"]].append(s)
        for sid, sl in per_staff.items():
            sl.sort(key=lambda x: x["start"])
            for i in range(len(sl) - 1):
                assert sl[i]["end"] <= sl[i + 1]["start"], (
                    f"T2 {day} {name.get(sid, sid)} が時間重複 "
                    f"{sl[i]['start'][11:16]}-{sl[i]['end'][11:16]} / {sl[i+1]['start'][11:16]}-{sl[i+1]['end'][11:16]}"
                )

    # T3: 各(日,パターン)の配置が required を超えない
    pats = _db.query_all("SELECT * FROM shift_patterns WHERE shop_id=?", (shop,))
    for day in days:
        for p in pats:
            placed = sum(
                1 for s in shifts
                if shift_covers_pattern(s["start"], s["end"], day, p["start_time"], p["end_time"])
            )
            assert placed <= (p["required_staff"] or 1), (
                f"T3 {day} {p['pattern_name']} 過剰配置 {placed}/{p['required_staff']}"
            )

    # T4: shortage リストが未カバー枠を正確に反映
    #   平日夜は required3 に対し実質2(固定overlap1+自動1)のため shortage が出るはず
    weekday_night = [
        s for s in res["shortage"]
        if s["pattern"] == "夜" and s["date"] not in ("2026-08-01", "2026-08-02")
    ]
    assert len(weekday_night) >= 1, (
        f"T4 平日夜の不足がshortageに反映されていない: {res['shortage']}"
    )

    # T5: 休憩時間が労基法に合致
    for s in shifts:
        work = minutes_between(s["start"], s["end"])
        expected = compute_break_minutes(work)
        assert s["break"] == expected, (
            f"T5 {s['start'][:10]} {name.get(s['staff_id'], s['staff_id'])} "
            f"休憩{s['break']}分!=期待{expected}分(勤務{work}分)"
        )


# ============================================================
# [正常系] T6: 固定縛りすぎ検出
#   固定シフトがあるスタッフでも、固定の無い日(土日)の未カバー枠へ自動配置される。
# ============================================================
def test_t6_fixed_coverage():
    conn = use_db("/tmp/t_t6.db")
    c = conn.cursor()
    shop = mkshop(c)
    e1 = mkstaff(c, shop, "E1", "社員A", "employee", maxh=200)
    for w in range(1, 6):  # 月-金 9-18固定（土日は固定なし）
        mkfixed(c, e1, w, "09:00", "18:00")
    mkpat(c, shop, "夜", "17:00", "22:00", 1)
    conn.commit()
    conn.close()

    res = shift_engine.auto_generate(
        shop, {"min_daily_hours": 4, "max_consecutive_days": 99}, "2026-08-01", "2026-08-07"
    )
    shifts = res["confirmed"]
    # 土日(08-01土, 08-02日)は固定が無い → 夜へ社員が自動配置されるべき
    for weekend in ("2026-08-01", "2026-08-02"):
        placed = [
            s for s in shifts if s["start"][:10] == weekend and s["staff_id"] == e1
        ]
        assert len(placed) == 1, f"T6 {weekend} 未カバー枠へ社員が未配置: {placed}"
        assert placed[0]["reason"] == "不足補填（社員自動配置）", (
            f"T6 {weekend} 配置理由が想定外: {placed[0]['reason']}"
        )


# ============================================================
# [正常系] バイト希望経路: Step2a(時間指定) / Step2b(柔軟希望)
# ============================================================
def test_parttimer_requests():
    conn = use_db("/tmp/t_pt.db")
    c = conn.cursor()
    shop = mkshop(c)
    p1 = mkstaff(c, shop, "P1", "バイトA", "part_time", maxh=120)
    mkpat(c, shop, "朝", "09:00", "13:00", 1)
    mkreq(c, shop, p1, "2026-08-01", "09:00", "13:00")  # Step2a: 時間指定
    mkreq(c, shop, p1, "2026-08-02", "09:00", "13:00", availability="morning")  # Step2b: 柔軟
    conn.commit()
    conn.close()

    res = shift_engine.auto_generate(
        shop, {"min_daily_hours": 4, "max_consecutive_days": 99}, "2026-08-01", "2026-08-02"
    )
    shifts = res["confirmed"]
    sat = [s for s in shifts if s["start"][:10] == "2026-08-01" and s["staff_id"] == p1]
    sun = [s for s in shifts if s["start"][:10] == "2026-08-02" and s["staff_id"] == p1]
    assert len(sat) == 1 and sat[0]["reason"] == "希望シフト", (
        f"Step2a 時間指定希望が配置されていない: {sat}"
    )
    assert len(sun) == 1 and "柔軟希望" in sun[0]["reason"], (
        f"Step2b 柔軟希望が配置されていない: {sun}"
    )


# ============================================================
# [バグ検出] BUG#1: Step3 の月間上限チェック欠落
#   Cloudflare版は minutesByStaff+work > 上限 で配置をスキップするが、
#   Flask版はチェック無し（事後警告のみ）。上限を無視して自動配置される。
#   正しい挙動(上限遵守)を主張 -> 現状は FAIL = バグ存在。
# ============================================================
def test_bug1_monthly_cap():
    conn = use_db("/tmp/t_bug1.db")
    c = conn.cursor()
    shop = mkshop(c)
    e1 = mkstaff(c, shop, "E1", "社員A", "employee", maxh=5)  # 月間上限5h(超低)
    mkpat(c, shop, "朝", "09:00", "13:00", 1)  # 1シフト4h
    conn.commit()
    conn.close()

    res = shift_engine.auto_generate(
        shop, {"min_daily_hours": 4, "max_consecutive_days": 99}, "2026-08-01", "2026-08-07"
    )
    total_min = res["minutes_by_staff"][e1]
    cap_min = 5 * 60
    assert total_min <= cap_min, (
        f"月間上限{cap_min}分({cap_min/60}h)を超えて配置: "
        f"{total_min}分({round(total_min/60,1)}h) / 配置数={len(res['confirmed'])}"
    )


# ============================================================
# [バグ検出] BUG#2: カバレッジ集計の overlap 過大評価
#   shift_covers_pattern(時間重なり) で配置数を集計するため、
#   固定9-18が夜17-22と1時間(17-18)重なるだけで「夜カバー」とカウントする。
#   実質カバーしていないのに shortage に現れない。
#   正しい挙動(実質未カバーは不足報告)を主張 -> 現状は FAIL = バグ存在。
# ============================================================
def test_bug2_coverage_overlap():
    conn = use_db("/tmp/t_bug2.db")
    c = conn.cursor()
    shop = mkshop(c)
    e1 = mkstaff(c, shop, "E1", "社員A", "employee", maxh=200)
    mkfixed(c, e1, WD["月"], "09:00", "18:00")  # 月曜9-18固定
    mkpat(c, shop, "朝", "09:00", "13:00", 1)   # 9-18が正当にカバー(9-13 ⊂ 9-18)
    mkpat(c, shop, "夜", "17:00", "22:00", 1)   # 9-18は17-18のみ重なる -> 実質未カバー
    conn.commit()
    conn.close()

    res = shift_engine.auto_generate(
        shop, {"min_daily_hours": 4, "max_consecutive_days": 99}, "2026-08-03", "2026-08-03"
    )
    night_short = [
        s for s in res["shortage"]
        if s["date"] == "2026-08-03" and s["pattern"] == "夜"
    ]
    assert len(night_short) == 1 and night_short[0]["shortage"] >= 1, (
        f"固定9-18が夜17-22をカバー扱いし未カバーが隠れた: shortage={res['shortage']}"
    )


# ============================================================
# [バグ検出] BUG#3: compute_shortage のキー名不整合
#   compute_shortage は s["start_datetime"]/s["end_datetime"] を参照するが、
#   auto_generate の戻り値(confirmed/pending)は "start"/"end" キー。
#   auto_generate の結果を compute_shortage に渡すと KeyError。
#   正しい挙動(同一エンジン内で結果を再利用できる)を主張 -> 現状は ERROR = バグ存在。
# ============================================================
def test_bug3_compute_shortage_keys():
    conn = use_db("/tmp/t_bug3.db")
    c = conn.cursor()
    shop = mkshop(c)
    e1 = mkstaff(c, shop, "E1", "社員A", "employee", maxh=200)
    mkpat(c, shop, "朝", "09:00", "13:00", 1)
    conn.commit()
    conn.close()

    res = shift_engine.auto_generate(
        shop, {"min_daily_hours": 4, "max_consecutive_days": 99}, "2026-08-01", "2026-08-01"
    )
    pats = _db.query_all("SELECT * FROM shift_patterns WHERE shop_id=?", (shop,))
    # auto_generate の confirmed を compute_shortage に渡せるか
    short = shift_engine.compute_shortage(
        res["confirmed"], pats, "2026-08-01", "2026-08-01"
    )
    internal = [{"date": s["date"], "pattern": s["pattern"]} for s in res["shortage"]]
    recomputed = [{"date": s["date"], "pattern": s["pattern"]} for s in short]
    assert recomputed == internal, (
        f"compute_shortageの結果がエンジン内部と不一致: internal={internal} recomputed={recomputed}"
    )


if __name__ == "__main__":
    print("=" * 72)
    print("シフトエンジン including テスト（バグ検出強化版）")
    print("=" * 72)
    run("T1-T5 基本不変量", "正常系", test_basic_invariants)
    run("T6 固定縛りすぎ検出", "正常系", test_t6_fixed_coverage)
    run("バイト希望経路(Step2a/2b)", "正常系", test_parttimer_requests)
    run("BUG#1 月間上限チェック", "バグ検出", test_bug1_monthly_cap)
    run("BUG#2 coverage overlap過大評価", "バグ検出", test_bug2_coverage_overlap)
    run("BUG#3 compute_shortageキー不整合", "バグ検出", test_bug3_compute_shortage_keys)

    print("=" * 72)
    cats = defaultdict(lambda: {"PASS": 0, "FAIL": 0, "ERROR": 0})
    for cat, _name, status, _msg in results:
        cats[cat][status] += 1
    for cat in ("正常系", "バグ検出"):
        s = cats[cat]
        print(f"  {cat}: PASS={s['PASS']} FAIL={s['FAIL']} ERROR={s['ERROR']}")
    print("-" * 72)

    normal_issues = [r for r in results if r[0] == "正常系" and r[2] != "PASS"]
    bug_hits = [r for r in results if r[0] == "バグ検出" and r[2] != "PASS"]
    bug_fixed = [r for r in results if r[0] == "バグ検出" and r[2] == "PASS"]

    if normal_issues:
        print(f"!! 正常系テストが{len(normal_issues)}件失敗（想定外の不具合）:")
        for _cat, n, st, msg in normal_issues:
            print(f"   - {n} [{st}] {msg}")
    else:
        print(">> 正常系: 全件 PASS")

    if bug_hits:
        print(f">> バグ検出: {len(bug_hits)}件が FAIL/ERROR = バグの存在を確認:")
        for _cat, n, st, msg in bug_hits:
            print(f"   - {n} [{st}] {msg}")
    if bug_fixed:
        print(f">> バグ検出: {len(bug_fixed)}件が PASS（既に修正済み）:")
        for _cat, n, _st, _msg in bug_fixed:
            print(f"   - {n}")
    print("=" * 72)
