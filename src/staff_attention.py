"""staff_attention.py - スタッフの働き方の変化を検出する。

【なぜ必要か】
  ダッシュボードのAI分析は、その期間のシフトだけを見て偏りや連勤を指摘する。
  見ているのはスナップショットで、過去と比べた変化は見ていない。そのため
  「いつも入っていた人が最近少ない」という、人が離れていく前の変化に気づけない。

【この関数がしないこと】
  原因や状態（離職の意思・体調・人間関係）は判定しない。勤務データから
  分かるのは「働き方が変わった」という事実だけで、そこに解釈を足すと
  外れた決めつけを店長に渡すことになる。返すのは数値と種別だけにする。

【なぜ純関数として切り出すか】
  判定は「何日から何日までを、どう数えて、どのしきい値で比べるか」の塊。
  DBアクセスやLLMと混ざると、しきい値を変えたときの影響が読めなくなる。

外部依存なし（標準ライブラリのみ）。
"""
from datetime import date, timedelta

RECENT_DAYS = 30           # 直近としてみる日数
BASE_DAYS = 60             # 比較の基準に使う、その前の日数
MIN_BASE_ATTENDANCE = 4    # 基準期間の30日あたり出勤日数がこれ未満の人は対象外
DROP_RATIO = 0.6           # 直近が基準のこの割合を下回ったら「減った」
MIN_RECENT_REQUESTS = 3    # 直近30日の変更・取消がこれ未満なら対象外
REQUEST_SPIKE_RATIO = 2.0  # 直近が基準のこの倍数以上なら「増えた」

# 基準期間は直近期間の何倍か（30日あたりへ換算するのに使う）
_BASE_SCALE = BASE_DAYS / RECENT_DAYS


def _in_range(day, lo, hi):
    """lo <= day <= hi（すべて "YYYY-MM-DD" の文字列比較で足りる）。"""
    return lo <= day <= hi


def find_attention(staff_rows, shift_rows, request_rows, today):
    """気にかけたいスタッフを、変化の大きい順に返す。

    staff_rows:   [{"id", "name", "is_resigned"}]
    shift_rows:   [{"staff_id", "start_datetime"}]（確定シフトのみ）
    request_rows: [{"staff_id", "created_at"}]（変更・取消の申請）
    today:        "YYYY-MM-DD"

    戻り値: [{"staff_id", "name", "reasons", "score"}]
      reasons: [{"type": "attendance_drop"|"request_spike", "recent": int, "base": float}]
    """
    t = date.fromisoformat(today)
    recent_lo = (t - timedelta(days=RECENT_DAYS - 1)).isoformat()
    recent_hi = today
    base_lo = (t - timedelta(days=RECENT_DAYS + BASE_DAYS - 1)).isoformat()
    base_hi = (t - timedelta(days=RECENT_DAYS)).isoformat()

    # スタッフごとに「出勤した日の集合」と「申請日の件数」を期間別に集める。
    # 出勤は日の集合にする（同じ日に2本入っていても1日）。
    recent_days = {}
    base_days = {}
    for sh in shift_rows:
        day = (sh.get("start_datetime") or "")[:10]
        sid = sh.get("staff_id")
        if not day or sid is None:
            continue
        if _in_range(day, recent_lo, recent_hi):
            recent_days.setdefault(sid, set()).add(day)
        elif _in_range(day, base_lo, base_hi):
            base_days.setdefault(sid, set()).add(day)

    recent_reqs = {}
    base_reqs = {}
    for rq in request_rows:
        day = (rq.get("created_at") or "")[:10]
        sid = rq.get("staff_id")
        if not day or sid is None:
            continue
        if _in_range(day, recent_lo, recent_hi):
            recent_reqs[sid] = recent_reqs.get(sid, 0) + 1
        elif _in_range(day, base_lo, base_hi):
            base_reqs[sid] = base_reqs.get(sid, 0) + 1

    out = []
    for s in staff_rows:
        if s.get("is_resigned"):
            continue
        sid = s.get("id")
        base_att_raw = len(base_days.get(sid, ()))
        # 基準期間に1日も出ていない人は、比べる過去がない（入ったばかり）
        if base_att_raw == 0:
            continue
        base_att = base_att_raw / _BASE_SCALE       # 30日あたりへ換算
        recent_att = len(recent_days.get(sid, ()))

        reasons = []
        score = 0.0
        if base_att >= MIN_BASE_ATTENDANCE and recent_att < base_att * DROP_RATIO:
            reasons.append({"type": "attendance_drop",
                            "recent": recent_att, "base": round(base_att, 1)})
            score = max(score, 1 - recent_att / base_att)

        r_recent = recent_reqs.get(sid, 0)
        r_base = base_reqs.get(sid, 0) / _BASE_SCALE
        if r_recent >= MIN_RECENT_REQUESTS and (r_base == 0 or r_recent >= r_base * REQUEST_SPIKE_RATIO):
            reasons.append({"type": "request_spike",
                            "recent": r_recent, "base": round(r_base, 1)})
            # 0除算を避けつつ 0〜1 に収める（基準0件のときは分母を最小件数にする）
            score = max(score, min(1.0, r_recent / max(r_base, MIN_RECENT_REQUESTS) / REQUEST_SPIKE_RATIO))

        if reasons:
            out.append({"staff_id": sid, "name": s.get("name"),
                        "reasons": reasons, "score": round(score, 3)})

    # 変化の大きい順。同点は staff_id 昇順（実行のたびに順番が入れ替わらない）
    out.sort(key=lambda x: (-x["score"], x["staff_id"]))
    return out
