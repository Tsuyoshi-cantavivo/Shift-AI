"""ai.py - LLM API 連携ロジック（Flask版・同期）。

LLM設定は **.env（環境変数）でのみ管理**。Web UIからの入力は受け付けない
（APIキーをDBに保存するのはセキュリティリスクが高いため）。
AI未接続時はルールベースに**フォールバックせず**、明示的に「AI未接続」を返す。
（ルールベースをAIと誤認させるのを防ぐ）
OpenAI互換 Chat Completions API に対応（requests 使用）。
"""
import os
import json
import re
import requests

WEEKDAY_MAP = {"日": 0, "月": 1, "火": 2, "水": 3, "木": 4, "金": 5, "土": 6}
WEEKDAY_NAMES = {v: k for k, v in WEEKDAY_MAP.items()}


def _env(key, default=None):
    return os.getenv(key, default)


# -----------------------------------------------------------
# LLM 設定: .env（環境変数）から読み込み。サーバー管理者が設定する。
#   LLM_API_URL (default: OpenAI互換 endpoint)
#   LLM_API_KEY (必須)
#   LLM_MODEL   (default: gpt-4o-mini)
#   LLM_TEMPERATURE (default: 0.4)
# -----------------------------------------------------------
def get_llm_config():
    """LLM設定を {api_url, api_key, model, temperature} で返す。未設定なら None。"""
    api_key = _env("LLM_API_KEY")
    if not api_key:
        return None
    return {
        "api_url": _env("LLM_API_URL", "https://api.openai.com/v1/chat/completions"),
        "api_key": api_key,
        "model": _env("LLM_MODEL", "gpt-4o-mini"),
        "temperature": float(_env("LLM_TEMPERATURE", "0.4")),
    }


def is_llm_available():
    return get_llm_config() is not None


# 直近のLLM呼び出しエラー詳細（APIキー等の機密は除いた、ユーザー表示可能な文字列）
_LAST_LLM_ERROR = None


def get_last_llm_error():
    """最後に失敗したLLM呼び出しのエラー詳細（HTTPステータス+body、または例外名）。
    ユーザー画面に表示して原因調査を助けるため、APIキー等の機密は含めない。
    """
    return _LAST_LLM_ERROR


def _safe_err_body(resp):
    """HTTPエラーレスポンスからユーザー表示可能なエラー詳細を抽出（APIキー等の機密を除く）。"""
    try:
        data = resp.json()
        # OpenAI互換: {"error": {"message": "...", "type": "...", "code": "..."}}
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            e = data["error"]
            parts = []
            for k in ("type", "code", "message"):
                v = e.get(k)
                if v:
                    parts.append(f"{k}={v}")
            return " ".join(parts) if parts else resp.text[:300]
        return resp.text[:300]
    except Exception:
        return resp.text[:300]


def _post_llm(messages, temperature):
    """LLM API を呼ぶ。成功なら (reply, None)、失敗なら (None, err_detail) を返す。

    ※ GPT-5系/o系などの推論モデルは temperature パラメータ非対応（指定すると400エラー）。
       温度設定の有無で2回試さずに済むよう、リクエストから temperature は送らない。
    """
    global _LAST_LLM_ERROR
    cfg = get_llm_config()
    if not cfg:
        _LAST_LLM_ERROR = "LLM_API_KEY が未設定（.env を確認）"
        return None, _LAST_LLM_ERROR
    try:
        resp = requests.post(cfg["api_url"], headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}",
        }, json={"model": cfg["model"], "messages": messages}, timeout=30)
    except Exception as e:
        _LAST_LLM_ERROR = f"接続エラー: {type(e).__name__}: {e}"
        return None, _LAST_LLM_ERROR
    if not resp.ok:
        _LAST_LLM_ERROR = f"HTTP {resp.status_code}: {_safe_err_body(resp)}"
        return None, _LAST_LLM_ERROR
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"], None
    except Exception as e:
        _LAST_LLM_ERROR = f"レスポンス解析エラー: {type(e).__name__}: {e}"
        return None, _LAST_LLM_ERROR


def call_llm(system_prompt, user_prompt, temperature=0.3):
    """LLM を呼び出す。未設定/失敗時は None（詳細は get_last_llm_error() で取得）。"""
    reply, _ = _post_llm(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        temperature)
    return reply


def _ceil_div(a, b):
    return (a + b - 1) // b


def _shift_minutes(s):
    """シフト1件の実働分数を返す（start/end/end_datetime/start_datetime 両対応）。"""
    sd = s.get("start") or s.get("start_datetime") or ""
    ed = s.get("end") or s.get("end_datetime") or ""
    if len(sd) < 16 or len(ed) < 16:
        return 0
    try:
        from datetime import datetime
        a = datetime.fromisoformat(sd.replace("Z", ""))
        b = datetime.fromisoformat(ed.replace("Z", ""))
        return max(0, int((b - a).total_seconds() // 60))
    except Exception:
        return 0


def _slot_times(preferred):
    """希望時間帯から標準的な (開始, 終了, 実働h) を返す。"""
    if preferred == "morning":
        return "09:00", "14:00", 5
    if preferred == "evening":
        return "17:00", "22:00", 5
    return "09:00", "14:00", 5


def _parse_explicit_time_range(text):
    """'13-18', '13時〜18時', '13:00-18:00', '13時から18時', '17時まで' 等の明示時間帯を ("HH:MM","HH:MM") で返す。

    無ければ None。金額表現(8万/80000円)とは区別するため、数値(1-2桁)+時(+区切り)+数値 を要求。
    「N時まで」は (None, "HH:MM") を返す（開始時刻は呼出元で補完）。
    """
    # パターン1: 開始〜終了の明示範囲 (13-18, 13時〜18時, 13:00-18:00 等)
    m = re.search(
        r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*時?\s*(?:〜|～|~|-|—|–|から|以降)\s*(\d{1,2})(?::(\d{2}))?\s*時?(?:まで)?",
        text)
    if m:
        try:
            sh, eh = int(m.group(1)), int(m.group(3))
            sm = int(m.group(2) or 0)
            em = int(m.group(4) or 0)
            if 0 <= sh <= 24 and 0 <= eh <= 24 and (sh, sm) < (eh, em) and eh - sh <= 14:
                return f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"
        except ValueError:
            pass
    # パターン2: 「N時まで」「N時までに」「N:NNまで」(終了時刻のみ)
    m2 = re.search(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*時?\s*(?:まで|までに|迄|まで働きたい|まで入りたい)", text)
    if m2:
        try:
            eh = int(m2.group(1))
            em = int(m2.group(2) or 0)
            if 0 <= eh <= 24:
                return None, f"{eh:02d}:{em:02d}"
        except ValueError:
            pass
    # パターン3: 「N時から」「N時以降」(開始時刻のみ)
    m3 = re.search(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*時?\s*(?:から|以降|以後|から働きたい|から入りたい)", text)
    if m3:
        try:
            sh = int(m3.group(1))
            sm = int(m3.group(2) or 0)
            if 0 <= sh <= 24:
                return f"{sh:02d}:{sm:02d}", None
        except ValueError:
            pass
    return None


def _build_proposed_shifts(need_hours, need_days, ng_weekdays, preferred, pref_start=None, pref_end=None):
    """目標時間を達成する提案シフト配列を構築（NG曜日を除外して必要日数分）。

    戻り値: [{day_index, weekday, start_time, end_time, hours}, ...]
    preferred が "time" のときは pref_start/pref_end を用いる。
    """
    if not need_hours or not need_days:
        return []
    if preferred == "time" and pref_start and pref_end:
        st, en = pref_start, pref_end
        sh = int(st[:2]) * 60 + int(st[3:5])
        eh_ = int(en[:2]) * 60 + int(en[3:5])
        hours_per_day = max(1, round((eh_ - sh) / 60))
    else:
        st, en, hours_per_day = _slot_times(preferred)
    allowed = [d for d in (1, 2, 3, 4, 5, 6) if d not in set(ng_weekdays)]  # 月〜土（日曜除外・NG除外）
    if not allowed:
        allowed = [1]
    shifts = []
    for i in range(need_days):
        wd = allowed[i % len(allowed)]
        shifts.append({
            "day_index": i + 1, "weekday": wd,
            "start_time": st, "end_time": en, "hours": hours_per_day,
        })
    return shifts


# ---------- 機能1: 自然言語による希望シフト解析 ----------
def _parse_request_fallback(text, hourly_wage, period_days=15):
    target = None
    m = re.search(r"(\d+)\s*万\s*円?", text)
    if m:
        target = int(m.group(1)) * 10000
    if target is None:
        m2 = re.search(r"(\d{4,6})\s*円", text)
        if m2:
            target = int(m2.group(1))
    ng_days = set()
    for mm in re.finditer(r"(日|月|火|水|木|金|土)曜[は]?\s*(?:は\s*)?(NG|無理|不可|×|だめ|ダメ|できない|出来ない|厳しい)", text):
        ng_days.add(WEEKDAY_MAP[mm.group(1)])
    # 明示的な時間帯("13-18", "17時まで" 等)を最優先で解析
    pref_start = pref_end = None
    explicit = _parse_explicit_time_range(text)
    if explicit:
        preferred = "time"
        pref_start, pref_end = explicit
        # start または end が None（片方のみ指定）の場合、デフォルトで補完
        if pref_start and not pref_end:
            pref_end = "22:00"  # 開始のみ指定 → 終業22:00まで
        elif pref_end and not pref_start:
            pref_start = "09:00"  # 終了のみ指定 → 営業9:00から
    elif re.search(r"昼|昼間|noon|日中|真昼", text):
        # 「昼間」は朝/夜とは別の時間帯として時間指定扱い
        preferred = "time"
        pref_start, pref_end = "11:00", "16:00"
    elif re.search(r"朝|午前|モーニング|早番", text):
        preferred = "morning"
    elif re.search(r"夕方|夜|pm|午後|evening|night|遅番", text):
        preferred = "evening"
    else:
        preferred = None
    wage = hourly_wage or 1000
    need_hours = _ceil_div(target, wage) if target else None
    # 1日あたりの実働時間で必要日数を試算
    if preferred == "time" and pref_start and pref_end:
        sh = int(pref_start[:2]) * 60 + int(pref_start[3:5])
        eh = int(pref_end[:2]) * 60 + int(pref_end[3:5])
        hpd = max(1, round((eh - sh) / 60))
    else:
        hpd = 5
    need_days = min(period_days, _ceil_div(need_hours, hpd)) if need_hours else 0
    proposed = _build_proposed_shifts(need_hours, need_days, sorted(ng_days), preferred, pref_start, pref_end)
    reasons = []
    if target:
        reasons.append(f"目標金額{target:,}円を時給{wage}円で達成するため、月{need_hours}時間（約{need_days}日）の勤務を提案します。")
    if ng_days:
        reasons.append("ご指定のNG曜日（" + "・".join(WEEKDAY_NAMES[d] for d in sorted(ng_days)) + "曜）は除外しました。")
    if preferred == "time":
        reasons.append(f"ご指定の時間帯（{pref_start}-{pref_end}）のシフトを優先的に配置します。")
    elif preferred == "morning":
        reasons.append("ご希望の「朝」のシフトを優先的に配置します。")
    elif preferred == "evening":
        reasons.append("ご希望の「夕方/夜」のシフトを優先的に配置します。")
    return {"target_income": target, "need_hours": need_hours, "need_days": need_days,
            "ng_weekdays": sorted(ng_days), "preferred_slot": preferred,
            "preferred_start": pref_start, "preferred_end": pref_end,
            "hourly_wage": wage,
            "proposed_shifts": proposed,
            "reason": "\n".join(reasons) if reasons else "ご入力内容から標準的なシフトを提案します。", "source": "rule_based"}


def parse_shift_request(text, hourly_wage, period_days=15):
    wage = hourly_wage or 1000
    system_prompt = (
        "あなたはシフト希望の解析アシスタントです。ユーザーの自然言語入力から次のJSONを厳密に出力してください（他の文章不可）。"
        'スキーマ: {"target_income":数値|null,"need_hours":数値|null,"ng_weekdays":[0-6],'
        '"preferred_slot":"morning"|"evening"|"time"|null,"preferred_start":"HH:MM"|"null","preferred_end":"HH:MM"|"null",'
        '"reason":"提案理由(日本語・丁寧)"}。'
        "weekdaysは0=日,...6=土。need_hoursはtarget_income/時給。具体的な時間帯(例:13-18)があればpreferred_slot='time'としpreferred_start/preferred_endをHH:MMで設定。")
    user_prompt = f'入力文: "{text}"\n想定時給: {wage}円\n対象期間日数: {period_days}日\n上記スキーマのJSONのみを出力してください。'
    result = call_llm(system_prompt, user_prompt, temperature=0.2)
    if result:
        try:
            parsed = json.loads(re.sub(r"```json|```", "", result).strip())
            if parsed.get("target_income") and wage:
                parsed["need_hours"] = _ceil_div(parsed["target_income"], wage)
            # ★ 入力テキストの明示時間帯を最優先で参照(LLMの取りこぼしを防ぐ)
            explicit = _parse_explicit_time_range(text)
            if explicit:
                parsed["preferred_slot"] = "time"
                # explicit は (start, end) だが、片方が None の場合がある（「17時まで」等）
                es, ee = explicit
                parsed["preferred_start"] = es or "09:00"  # 開始未指定 → 営業開始9:00
                parsed["preferred_end"] = ee or "22:00"    # 終了未指定 → 営業終了22:00
            ps = parsed.get("preferred_start")
            pe = parsed.get("preferred_end")
            # preferred_slot="time" で片方だけ指定の場合も補完
            if parsed.get("preferred_slot") == "time":
                if not ps:
                    ps = "09:00"
                if not pe:
                    pe = "22:00"
                parsed["preferred_start"] = ps
                parsed["preferred_end"] = pe
            if parsed.get("preferred_slot") == "time" and ps and pe:
                _sh = int(ps[:2]) * 60 + int(ps[3:5]); _eh = int(pe[:2]) * 60 + int(pe[3:5])
                _hpd = max(1, round((_eh - _sh) / 60))
            else:
                _hpd = 5
            parsed["need_days"] = min(period_days, _ceil_div(parsed.get("need_hours") or 0, _hpd)) if parsed.get("need_hours") else 0
            parsed["hourly_wage"] = wage
            parsed["proposed_shifts"] = _build_proposed_shifts(
                parsed.get("need_hours"), parsed.get("need_days", 0),
                parsed.get("ng_weekdays") or [], parsed.get("preferred_slot"),
                ps if ps != "null" else None, pe if pe != "null" else None)
            parsed["source"] = "llm"
            return parsed
        except Exception:
            pass
    return _parse_request_fallback(text, wage, period_days)


# ---------- 機能2: 欠員ヘルプ要請メッセージ ----------
def _help_fallback(date_label, time_label, shortage, shop_name):
    return "\n".join([
        f"🌟 {shop_name}からのお願い 🌟", "",
        f"{date_label} {time_label}のシフトが、あと{shortage}名不足しています！",
        "皆さんでお力を貸していただけると非常に助かります🙏", "",
        "・無理のない範囲でOKです", "・少しでも入れる方はスタッフアプリからご応募ください", "",
        "ご協力よろしくお願いいたします😊"])


def generate_help_message(date_label, time_label, shortage, shop_name):
    system_prompt = ("あなたは飲食店・小売店のシフト募集文を書くプロです。スタッフに負担を感じさせず"
                     "協力したくなる温かいトーンで、絵文字を適度に使った200文字以内のメッセージを作成してください。")
    user_prompt = (f"店舗名: {shop_name}\n対象日: {date_label}\n時間帯: {time_label}\n不足人数: {shortage}名\n"
                   "スタッフ向けのヘルプ募集メッセージを作成してください。")
    result = call_llm(system_prompt, user_prompt, temperature=0.8)
    return result or _help_fallback(date_label, time_label, shortage, shop_name)


# ---------- 機能3: シフト労務・モチベーション配慮レビュー ----------
def _parse_iso(s):
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def analyze_shift_balance(shifts):
    by_staff = {}
    for s in shifts:
        sid = s["staff_id"]
        by_staff.setdefault(sid, {"staff_id": sid, "name": s.get("staff_name", f"スタッフ{sid}"),
                                  "days": set(), "total_minutes": 0, "dates": []})
        day = s["start_datetime"][:10]
        by_staff[sid]["days"].add(day)
        by_staff[sid]["dates"].append(day)
        work = ((_parse_iso(s["end_datetime"]) - _parse_iso(s["start_datetime"])).total_seconds() // 60
                - (s.get("break_time_minutes") or 0))
        by_staff[sid]["total_minutes"] += max(0, work)
    max_consec = 0; max_consec_name = None
    for info in by_staff.values():
        dates = sorted(info["dates"]); run = 1; best = 1
        for i in range(1, len(dates)):
            prev = _parse_iso(dates[i - 1] + "T00:00:00"); c = _parse_iso(dates[i] + "T00:00:00")
            if (c - prev).days == 1:
                run += 1; best = max(best, run)
            else:
                run = 1
        info["max_consecutive"] = best
        if best > max_consec:
            max_consec = best; max_consec_name = info["name"]
    totals = [i["total_minutes"] for i in by_staff.values()]
    avg = sum(totals) / len(totals) if totals else 0
    std = (sum((t - avg) ** 2 for t in totals) / len(totals)) ** 0.5 if totals else 0
    return {"by_staff": by_staff, "max_consecutive": max_consec, "max_consecutive_staff": max_consec_name,
            "avg_minutes": avg, "std_minutes": std}


def review_shift_balance(shifts):
    m = analyze_shift_balance(shifts)
    by_staff = m["by_staff"]; avg = m["avg_minutes"]; std = m["std_minutes"]
    max_consec = m["max_consecutive"]; max_consec_name = m["max_consecutive_staff"]
    summary = "\n".join(f"・{i['name']}: {len(i['days'])}日 / {round(i['total_minutes']/60)}時間 / 最大連勤{i['max_consecutive']}日"
                        for i in by_staff.values()) or "（シフトデータなし）"
    system_prompt = ("あなたは労務管理とスタッフのモチベーション維持に精通したシフトアドバイザーです。"
                     "連勤過多や労働時間の偏りに配慮した具体的な改善アドバイスを3〜5項目の箇条書きで出力してください。")
    user_prompt = (f"【期間中の確定シフト集計】\n{summary}\n\n平均労働時間: {round(avg/60)}時間\n"
                   f"最大連続勤務: {max_consec}日（{max_consec_name or '-'}）\n労働時間のばらつき(標準偏差): {round(std/60)}時間\n"
                   "労務・モチベーション配慮の観点からアドバイスを箇条書きで出力してください。")
    advice = call_llm(system_prompt, user_prompt, temperature=0.5)
    source = "llm" if advice else "rule_based"
    if not advice:
        tips = []
        if max_consec >= 6:
            tips.append(f"⚠️ 連続勤務が{max_consec}日（{max_consec_name}）と長すぎます。休日を挟む調整を推奨します。")
        if std / 60 >= 15:
            tips.append("📊 スタッフ間の労働時間の偏りが大きいです。稼働の少ないスタッフに振ると定着率向上に繋がります。")
        low = [i["name"] for i in by_staff.values() if i["total_minutes"] < avg * 0.6]
        if low:
            tips.append("👇 " + "・".join(low) + "さんの稼働が相対的に少なめです。希望を確認のうえ追加配置を検討してください。")
        high = [i["name"] for i in by_staff.values() if i["total_minutes"] > avg * 1.4]
        if high:
            tips.append("🔥 " + "・".join(high) + "さんの負担が大きめです。休憩や連勤にご注意ください。")
        if not tips:
            tips.append("✅ 全体的にバランスの取れたシフト構成です。このまま確定して問題ありません。")
        advice = "\n".join(tips)
    staff_summary = [{"staff_id": i["staff_id"], "name": i["name"], "days": len(i["days"]),
                      "hours": round(i["total_minutes"] / 60 * 10) / 10, "max_consecutive": i["max_consecutive"]}
                     for i in by_staff.values()]
    return {"metrics": {"avg_hours": round(avg / 60 * 10) / 10, "stddev_hours": round(std / 60 * 10) / 10,
                        "max_consecutive_days": max_consec, "max_consecutive_staff": max_consec_name,
                        "staff_summary": staff_summary}, "advice": advice, "source": source}


# ===========================================================
# 機能4: 会話型AIチャット（店長アシスタント / スタッフアシスタント）
# ===========================================================
def _summarize_context(ctx):
    """チャットのシステムプロンプト向けに ctx を要約テキスト化（実データを含む）。"""
    if not ctx:
        return ""
    lines = []
    if "shop_name" in ctx:
        lines.append(f"店舗: {ctx.get('shop_name')}")
    if "today" in ctx:
        lines.append(f"今日: {ctx.get('today')}")
    if "staff_name" in ctx:
        lines.append(f"スタッフ: {ctx.get('staff_name')}（{ctx.get('role','')}）")
    if "staff_count" in ctx:
        lines.append(f"稼働スタッフ: {ctx.get('staff_count')}名（社員{ctx.get('employee_count','0')}・バイト{ctx.get('part_time_count','0')}）")
    if "patterns" in ctx and ctx["patterns"]:
        ptxt = " / ".join(f"{p['name']}({p['time']},必要{p['required']}名)" for p in ctx["patterns"])
        lines.append(f"シフト時間帯: {ptxt}")
    if ctx.get("has_weekday_overrides"):
        lines.append("曜日別必要人数設定: あり")
    if "business_hours" in ctx and ctx["business_hours"]:
        lines.append(f"営業時間: {ctx['business_hours']}")
    if "default_wage" in ctx and ctx["default_wage"]:
        lines.append(f"デフォルト時給: {ctx['default_wage']}円")
    if "min_daily_hours" in ctx and ctx["min_daily_hours"]:
        lines.append(f"1日最低勤務: {ctx['min_daily_hours']}h")
    if "max_consecutive_days" in ctx and ctx["max_consecutive_days"]:
        lines.append(f"最大連勤(推奨): {ctx['max_consecutive_days']}日")
    if "hourly_wage" in ctx:
        lines.append(f"時給: {ctx.get('hourly_wage')}円")
    if "upcoming_confirmed" in ctx:
        lines.append(f"今月確定シフト: {ctx.get('upcoming_confirmed',0)}件 / {ctx.get('month_hours',0)}h")
    if "month_cost" in ctx and ctx["month_cost"]:
        lines.append(f"今月人件費: ¥{ctx['month_cost']:,}")
    if "today_attendance" in ctx:
        lines.append(f"今日の出勤: {ctx.get('today_attendance',0)}名")
    if "shortage_count" in ctx:
        lines.append(f"不足枠: {ctx.get('shortage_count',0)}枠")
    if "pending_requests" in ctx:
        lines.append(f"調整待ち希望: {ctx.get('pending_requests',0)}件 / 承認待ち申請: {ctx.get('pending_approvals',0)}件")
    if "active_period" in ctx and ctx["active_period"]:
        ap = ctx["active_period"]
        lines.append(f"募集期間: {ap.get('start','')} 〜 {ap.get('end','')}（締切{ap.get('deadline','')}）")
    sh = ctx.get("staff_hours")
    if sh:
        top = sorted(sh.items(), key=lambda x: -x[1])[:5]
        lines.append("スタッフ別労働時間: " + " / ".join(f"{n} {h:.0f}h" for n, h in top))
    sd = ctx.get("shortage_details")
    if sd:
        items = [f"{d.get('date','')} {d.get('pattern','')} あと{d.get('shortage',1)}名" for d in sd[:5]]
        lines.append("不足詳細: " + " / ".join(items))
    if "upcoming_shifts" in ctx and ctx["upcoming_shifts"]:
        items = ctx["upcoming_shifts"][:5]
        stxt = " / ".join(f"{s['start'][:10]} {s['start'][11:16]}-{s['end'][11:16]}({s['status']})" for s in items)
        lines.append(f"直近の予定: {stxt}")
    return "\n".join(lines)


def chat(message, history, ctx=None):
    """店長向け会話型チャット。

    履歴を保ちつつ、店舗コンテキストに基づいた回答をLLMが推論して返す。
    AI未接続時はルールベースに**フォールバックせず**、明示的に「AI未接続」を返す。
    戻り値: {"reply": str, "source": "llm"|"unavailable", "suggestions": [str]}
    """
    ctx = ctx or {}
    suggestions = _shop_suggestions(message, ctx)
    if not is_llm_available():
        return {
            "reply": (
                "⚠️ AIエンジンが未接続です。\n"
                "システム管理者にお願いし、管理者画面の「AI設定」から OpenAI 互換 API キーを設定してください。\n"
                "（OpenAI / Anthropic / Google Gemini / Groq 等のOpenAI互換APIに対応）"
            ),
            "source": "unavailable",
            "suggestions": suggestions,
        }
    ctx_text = _summarize_context(ctx)
    sys_prompt = (
        "あなたは「ShiftAI」というシフト管理SaaSのAIアシスタントで、日本の飲食店・小売店の店長を支援する。\n"
        "親しみやすく丁寧な日本語で答え、必要に応じて箇条書きや具体例を使う。\n"
        "わからない情報は推測せず、店舗設定画面や実際のシフト表で確認するよう案内する。\n"
        "回答は簡潔に（3〜6文程度）。\n\n"
        "【現在の店舗状況】\n"
        f"{ctx_text}\n\n"
        "【回答のポイント】\n"
        "- 数値（人件費・不足枠・時間数）は必ず上記データに基づき計算・言及する\n"
        "- 抽象的な回答ではなく、この店舗の実データに即して具体的に答える\n"
        "- 時給や目標金額が含まれる質問では、必要時間・日数を計算して示す"
    )
    msgs_for_llm = [{"role": "system", "content": sys_prompt}]
    for h in (history or [])[-10:]:
        r = h.get("role"); c = h.get("content")
        if r in ("user", "assistant") and c:
            msgs_for_llm.append({"role": r, "content": c})
    msgs_for_llm.append({"role": "user", "content": message})

    llm_reply = _call_llm_messages(msgs_for_llm, temperature=0.5)
    if llm_reply:
        return {"reply": llm_reply.strip(), "source": "llm", "suggestions": suggestions}
    err = get_last_llm_error() or "詳細不明"
    return {
        "reply": (
            f"AIエンジンの呼び出しに失敗しました。\n"
            f"エラー詳細: {err}\n\n"
            "サーバー管理者は .env の LLM_API_KEY / LLM_API_URL / LLM_MODEL をご確認ください。"
        ),
        "source": "unavailable",
        "suggestions": suggestions,
    }


def chat_staff(message, history, ctx=None):
    """スタッフ向け会話型チャット（自身のシフト・希望作成の相談など）。

    AI未接続時はルールベースに**フォールバックせず**、明示的に「AI未接続」を返す。
    """
    ctx = ctx or {}
    suggestions = _staff_suggestions(message, ctx)
    if not is_llm_available():
        return {
            "reply": (
                "⚠️ AIエンジンが未接続です。\n"
                "システム管理者に管理者画面の「AI設定」からAPIキーを設定してもらってください。"
            ),
            "source": "unavailable",
            "suggestions": suggestions,
        }
    ctx_text = _summarize_context(ctx)
    sys_prompt = (
        "あなたは「ShiftAI」というシフト管理アプリのAIアシスタントで、アルバイト・社員スタッフを支援する。\n"
        "親しみやすく丁寧な日本語で、自身のシフトの確認や希望提出のコツ、給与の試算などを助ける。\n"
        "回答は簡潔に（3〜5文程度）。\n\n"
        "【スタッフ情報と実データ】\n"
        f"{ctx_text}\n\n"
        "【回答のポイント】\n"
        "- 「月〇〇万円稼ぐには？」「〇〇円欲しい」等には、時給と現在確定シフトから**自ら計算**して必要時間・日数を答える\n"
        "- 「次のシフト」「今月何日」等には、必ず上記実データ（upcoming_shifts等）を参照して答える\n"
        "- 抽象的な一般論ではなく、このスタッフの実データに即して具体化する"
    )
    msgs_for_llm = [{"role": "system", "content": sys_prompt}]
    for h in (history or [])[-10:]:
        r = h.get("role"); c = h.get("content")
        if r in ("user", "assistant") and c:
            msgs_for_llm.append({"role": r, "content": c})
    msgs_for_llm.append({"role": "user", "content": message})

    llm_reply = _call_llm_messages(msgs_for_llm, temperature=0.6)
    if llm_reply:
        return {"reply": llm_reply.strip(), "source": "llm", "suggestions": suggestions}
    err = get_last_llm_error() or "詳細不明"
    return {
        "reply": (
            f"AIエンジンの呼び出しに失敗しました。\n"
            f"エラー詳細: {err}\n\n"
            "サーバー管理者は .env の LLM_API_KEY / LLM_API_URL / LLM_MODEL をご確認ください。"
        ),
        "source": "unavailable",
        "suggestions": suggestions,
    }


def _call_llm_messages(messages, temperature=0.4):
    """OpenAI 互換 Chat API を messages 配列で呼ぶ。未設定/失敗時は None（詳細は get_last_llm_error）。"""
    reply, _ = _post_llm(messages, temperature)
    return reply


def _shop_suggestions(message, ctx):
    """ユーザーの質問と現在の状況に基づく提案チップス。"""
    m = (message or "").lower()
    base = []
    # 状況ベースの提案（優先）
    if ctx and ctx.get("shortage_count", 0) > 0:
        base.append("不足状況を詳しく教えて")
    if ctx and ctx.get("pending_approvals", 0) > 0:
        base.append(f"承認待ち申請は？")
    # 質問ベースの提案
    if any(k in m for k in ["不足", "足り"]):
        base += ["ヘルプ募集メッセージを作って", "どうすれば解消できる？"]
    if any(k in m for k in ["人件費", "コスト"]):
        base += ["スタッフ別の労働時間は？", "コスト削減のコツは？"]
    if any(k in m for k in ["連勤", "偏り"]):
        base += ["労務レビューを実行して", "どのスタッフが多め？"]
    # 汎用提案（常に利用可能）
    base += ["今月のシフト状況は？", "今日の出勤は？", "AIシフト作成の手順は？"]
    # 重複排除して最大4件
    seen = set(); out = []
    for s in base:
        if s not in seen:
            seen.add(s); out.append(s)
        if len(out) >= 4:
            break
    return out


def _staff_suggestions(message, ctx):
    base = []
    msg = message.lower()
    wage = ctx.get("hourly_wage") if ctx else None
    if any(k in msg for k in ["稼ぎ", "給料", "お金", "収入"]):
        base += ["来月は〇〇万円稼ぐには？", "今月の給与予測は？"]
    if any(k in msg for k in ["希望", "シフト希望", "入れたい"]):
        base += ["希望の出し方を教えて", "AIで希望を作って"]
    if any(k in msg for k in ["次", "いつ", "予定"]):
        base += ["次のシフトは？", "今月何日入ってる？"]
    base += ["シフトの変更はどうすればいい？", "AIで希望を作る方法は？"]
    seen = set(); out = []
    for s in base:
        if s not in seen:
            seen.add(s); out.append(s)
        if len(out) >= 4:
            break
    return out


def _shop_rule_based_reply(message, ctx):
    """LLM無し時の店長向けルールベース応答（実データを用いて有用に回答）。"""
    m = (message or "").lower()
    # 会話の流れを考慮: 「それ」「その」「もっと」などの指示詞
    is_followup = any(k in m for k in ["それ", "その", "もっと", "詳しく", "他に", "あと", "続けて"])

    # 不足・時間帯について
    if any(k in m for k in ["不足", "足り", "たりない", "空き", "たりない"]):
        parts = []
        sc = ctx.get("shortage_count", 0)
        if sc > 0:
            parts.append(f"現在 {sc}枠 で人員不足があります。")
            details = ctx.get("shortage_details") or []
            if details:
                items = [f"  ・{d.get('date','')} {d.get('pattern','')} あと{d.get('shortage',1)}名" for d in details[:5]]
                parts.append("\n" + "\n".join(items))
                if len(details) > 5:
                    parts.append(f"\n  ...他 {len(details)-5}枠")
            parts.append("\n\n💡 対策:")
            parts.append("  1. AIシフト作成で再生成する")
            parts.append("  2. ヘルプ募集メッセージをスタッフに送る")
            parts.append("  3. 設定の「シフト設定」で曜日別必要人数を見直す")
        else:
            parts.append("✅ 現在、人員不足はありません。全時間帯で必要人数が確保されています。")
            sh = ctx.get("staff_hours") or {}
            if sh:
                parts.append(f"\nスタッフ{len(sh)}名で {ctx.get('month_hours',0):.0f}時間分のシフトが組まれています。")
        return "\n".join(parts)

    # 人件費・コスト
    if any(k in m for k in ["人件費", "コスト", "給与", "賃金", "お金"]):
        cost = ctx.get("month_cost", 0)
        hours = ctx.get("month_hours", 0)
        sc = ctx.get("staff_count", 1)
        parts = [f"💰 今月の確定シフト: {ctx.get('upcoming_confirmed',0)}件 / {hours:.0f}時間"]
        parts.append(f"   人件費合計: ¥{cost:,}")
        if sc:
            parts.append(f"   スタッフ{sc}名の平均: {hours/max(sc,1):.1f}時間/人")
            parts.append(f"   1人あたりコスト: ¥{cost/max(sc,1):,.0f}")
        sh = ctx.get("staff_hours") or {}
        if sh:
            parts.append("\nスタッフ別（上位3名）:")
            for name, h in sorted(sh.items(), key=lambda x: -x[1])[:3]:
                parts.append(f"  {name}: {h:.0f}h")
        parts.append("\n💡 コスト削減のポイント:")
        parts.append("  ・必要人数の見直し（設定→シフト設定）")
        parts.append("  ・深夜割増の対象時間の確認")
        parts.append("  ・時給の異なるスタッフの配置バランス")
        return "\n".join(parts)

    # 連勤・労務・偏り
    if any(k in m for k in ["連勤", "連続", "疲労", "つかれ", "偏り", "均等", "不公平"]):
        sh = ctx.get("staff_hours") or {}
        if sh:
            vals = list(sh.values()); mx = max(vals); mn = min(vals)
            parts = ["📊 スタッフ別の今月労働時間:"]
            for name, h in sorted(sh.items(), key=lambda x: -x[1]):
                bar = "█" * int(h / 10)
                parts.append(f"  {name}: {h:.0f}h {bar}")
            parts.append(f"\n最多 {mx:.0f}h と 最少 {mn:.0f}h で差は {mx-mn:.0f}h です。")
            if mx - mn > 30:
                parts.append("⚠️ 偏りが大きめです。AIシフト作成時に自動で均等化されますが、固定シフトの見直しもご検討ください。")
            elif mx - mn > 15:
                parts.append("多少の偏りはありますが、概ね良好なバランスです。")
            else:
                parts.append("✅ おおむね均等に配分されています。")
            return "\n".join(parts)
        return "確定シフトがまだないため分析できません。シフト作成後に再度お試しください。"

    # 今日の出勤
    if any(k in m for k in ["今日", "本日", "出勤", "だれ", "誰", "出てる"]):
        names = ctx.get("today_staff_names") or []
        if names:
            return f"📌 今日（{ctx.get('today','')}）の出勤は {len(names)}名です:\n  {' / '.join(names)}\n\nタイムラインで確認するには、シフト画面の日付をダブルタップしてください。"
        return f"📌 今日（{ctx.get('today','')}）は出勤予定のスタッフがいません。"

    # シフト状況・サマリー
    if any(k in m for k in ["状況", "サマリー", "どう", "教えて", "概況", "全体", "まとめ"]) or is_followup:
        parts = [
            f"📊 {ctx.get('shop_name','店舗')}のシフト状況（{ctx.get('today','')}時点）",
            f"  スタッフ: {ctx.get('staff_count',0)}名（社員{ctx.get('employee_count',0)}・バイト{ctx.get('part_time_count',0)}）",
            f"  今月の確定シフト: {ctx.get('upcoming_confirmed',0)}件 / {ctx.get('month_hours',0)}時間",
            f"  人件費: ¥{ctx.get('month_cost',0):,}",
        ]
        sc = ctx.get("shortage_count", 0)
        parts.append(f"  不足枠: {sc}枠" + (" ⚠️ 要対応" if sc else " ✅ 問題なし"))
        pr = ctx.get("pending_requests", 0)
        if pr: parts.append(f"  調整待ち希望: {pr}件")
        pa = ctx.get("pending_approvals", 0)
        if pa: parts.append(f"  承認待ち変更申請: {pa}件")
        ap = ctx.get("active_period")
        if ap: parts.append(f"  募集期間: {ap.get('start','')} 〜 {ap.get('end','')}（締切{ap.get('deadline','')}）")
        parts.append("\n何について詳しく知りたいですか？")
        parts.append("  ・「不足状況」 ・「人件費」 ・「連勤の偏り」 ・「今日の出勤」")
        return "\n".join(parts)

    # 募集期間
    if any(k in m for k in ["募集", "期間", "締切", "いつまで", " deadline"]):
        ap = ctx.get("active_period")
        if ap:
            return (f"📅 現在の募集期間\n  {ap.get('start','')} 〜 {ap.get('end','')}\n  締切: {ap.get('deadline','')}\n\n"
                    "設定の「募集期間」タブから新規作成・編集できます。")
        return "現在アクティブな募集期間はありません。設定の「募集期間」タブから作成してください。"

    # 自動作成方法
    if any(k in m for k in ["自動", "作成", "生成", "aiシフト", "やり方", "手順"]):
        return ("🤖 AIシフト作成の手順:\n"
                "1. 「AI」タブの「シフト作成」を開く\n"
                "2. 作成期間（開始日・終了日）を確認\n"
                "3. 「AIでシフト作成」ボタンを押す\n"
                "4. プレビューで判断理由を確認（希望反映率・不足解消など）\n"
                "5. 問題なければ「確定」\n\n"
                "AIは以下を自動考慮します:\n"
                "  ✓ 希望休・NG曜日\n  ✓ 固定シフト（契約勤務）\n  ✓ 時間帯別必要人数\n"
                "  ✓ 最低勤務時間・月間上限\n  ✓ 深夜割増・休憩時間")

    # 曜日別設定
    if any(k in m for k in ["曜日", "土日", "週末", "平日", "必要人数", "曜日別"]):
        base = "🔧 設定の「シフト設定」タブで、時間帯ごとに曜日別の必要人数を設定できます。\n\n"
        if ctx.get("has_weekday_overrides"):
            base += "現在、曜日別のオーバーライドが設定済みです。\n設定内容は「シフト設定」タブで確認できます（青い数字=オーバーライド）。"
        else:
            base += ("現在は全曜日同じ人数です。\n"
                     "例: 週末だけ人多くしたい → 土日のマスに数字を入れる\n"
                     "例: 平日の昼だけ少なめ → 該当マスを空欄（基本人数が適用）")
        return base

    # ヘルプ募集
    if any(k in m for k in ["ヘルプ", "募集文", "メッセージ", "募集"]):
        sc = ctx.get("shortage_count", 0)
        base = "📢 AI画面のチャットで「ヘルプ募集メッセージを作って」と頼めば、不足日時から募集文を自動生成します。\n"
        if sc:
            base += f"\n現在 {sc}枠の不足があるので、ヘルプ募集をお勧めします。"
        return base

    # 変更申請
    if any(k in m for k in ["変更", "申請", "承認", "却下"]):
        pa = ctx.get("pending_approvals", 0)
        if pa:
            return (f"📋 現在 {pa}件 の変更申請が承認待ちです。\n\n"
                    "シフト画面の「変更申請を承認/却下」ボタンから処理できます。\n"
                    "承認すると自動的にシフトへ反映されます。")
        return "承認待ちの変更申請はありません。"

    # 挨拶
    if any(k in m for k in ["こんにちは", "こんばんは", "おはよう", "はじめまして", "やあ", "ありがとう"]):
        sc = ctx.get("shortage_count", 0)
        greeting = f"{ctx.get('shop_name','店舗')}さん、こんにちは。シフト管理のAIアシスタントです。\n"
        greeting += f"今月は {ctx.get('upcoming_confirmed',0)}件 の確定シフトがあります。"
        if sc:
            greeting += f"\n⚠️ {sc}枠の不足があるので、確認をお勧めします。"
        greeting += "\n\n何について知りたいですか？（不足状況 / 人件費 / 連勤 / 今日の出勤）"
        return greeting

    # スタッフについて
    if any(k in m for k in ["スタッフ", "社員", "バイト", "アルバイト", "人数", "人員"]):
        sh = ctx.get("staff_hours") or {}
        parts = [f"👥 現在 {ctx.get('staff_count',0)}名 が稼働中です（社員{ctx.get('employee_count',0)}名・バイト{ctx.get('part_time_count',0)}名）。"]
        if sh:
            parts.append("\n今月の稼働状況:")
            for name, h in sorted(sh.items(), key=lambda x: -x[1])[:5]:
                parts.append(f"  {name}: {h:.0f}h")
        parts.append("\nスタッフ管理画面で個別の固定シフト・時給・月間上限を設定できます。")
        return "\n".join(parts)

    # 固定シフト
    if any(k in m for k in ["固定", "契約", "ふixed"]):
        return ("📅 固定シフトは「スタッフ管理」のカレンダーアイコンから曜日ごとに設定します。\n"
                "固定シフトは契約勤務として最優先で配置され、上限人数に関わらず必ず入ります。\n"
                "毎週同じ曜日・時間に入るスタッフに適しています。")

    # 希望休・スタッフ希望
    if any(k in m for k in ["希望休", "希望シフト", "スタッフの希望", "りょか"]):
        pr = ctx.get("pending_requests", 0)
        base = "📝 スタッフの希望シフトは「希望休管理」画面で一覧確認できます。\n"
        if pr:
            base += f"\n現在 {pr}件 の希望が調整待ちです。AIシフト作成時に自動的に組み込まれます。"
        return base

    # デフォルト: 状況サマリーを返す
    parts = [
        f"{ctx.get('shop_name','店舗')}の現在の状況:",
        f"  確定シフト {ctx.get('upcoming_confirmed',0)}件 / 人件費 ¥{ctx.get('month_cost',0):,}",
        f"  不足枠 {ctx.get('shortage_count',0)} / 調整待ち希望 {ctx.get('pending_requests',0)}件",
    ]
    parts.append("\n質問例:")
    parts.append("  「不足状況は？」「人件費は？」「今日の出勤は？」「連勤の偏りは？」")
    parts.append("  「AIシフト作成の手順は？」「曜日別設定は？」「募集期間は？」")
    return "\n".join(parts)


def _staff_rule_based_reply(message, ctx):
    """LLM無し時のスタッフ向けルールベース応答。"""
    m = message.lower()
    name = (ctx or {}).get("staff_name") or "そちら"
    wage = (ctx or {}).get("hourly_wage") or 1000
    upcoming = (ctx or {}).get("upcoming_shifts") or []
    confirmed = [s for s in upcoming if s.get("status") == "confirmed"]
    if any(k in m for k in ["こんにちは", "こんばんは", "おはよう", "やあ"]):
        return f"{name}さん、こんにちは。シフトについて何かお手伝いしましょうか？"
    if any(k in m for k in ["次", "いつ", "予定", "シフト"]):
        if confirmed:
            nx = confirmed[0]
            return f"{name}さんの次のシフトは {nx['start'][:10]} {nx['start'][11:16]}〜{nx['end'][11:16]} です。マイシフトタブでも確認できます。"
        return "現在、確定している今後のシフトはありません。希望提出から入力してみましょう。"
    if any(k in m for k in ["稼ぎ", "稼ぐ", "給料", "給与", "お金", "収入", "もうけ", "儲け"]):
        # 金額抽出（「5万」「5万円」「50000円」「5,0000円」等に対応）
        amt = None
        mm = re.search(r"(\d[\d,]*)\s*万[円]?", message)
        if mm:
            amt = int(mm.group(1).replace(",", "")) * 10000
        else:
            mm2 = re.search(r"(\d[\d,]*)\s*円", message)
            if mm2:
                amt = int(mm2.group(1).replace(",", ""))
        if amt:
            hours = _ceil_div(amt, wage)
            days = _ceil_div(hours, 5)
            # 既に今月確定している時間数があれば、追加でどれくらい必要かも提示
            extra = ""
            cur_hours = sum(_shift_minutes(s) for s in confirmed) / 60
            if cur_hours > 0:
                cur_pay = int(cur_hours * wage)
                if cur_pay >= amt:
                    extra = f"\nなお、現在確定しているシフト（約{cur_hours:.0f}h / ¥{cur_pay:,}）で既に目標達成見込みです。"
                else:
                    rem_hours = _ceil_div(amt - cur_pay, wage)
                    rem_days = _ceil_div(rem_hours, 5)
                    extra = f"\n現在確定分（約{cur_hours:.0f}h / ¥{cur_pay:,}）に対し、あと約{rem_hours}時間（1日5h×{rem_days}日）追加が必要です。"
            return (f"💰 目標 {amt:,}円 は、時給{wage}円で 約{hours}時間（1日5h×{days}日）の勤務で達成できます。{extra}\n"
                    "希望提出タブの「AIで希望を作成」に金額を入れると、自動で希望日が提案されます。")
        return f"今月の給与は確定シフトの時間数×時給{wage}円で試算できます。マイシフトタブで確認できます。「月〇〇万円稼ぐには？」のように金額を入れると、必要な勤務時間を計算できます。"
    if any(k in m for k in ["希望", "いれる", "入れたい", "提出"]):
        return ("希望提出タブから、日付をタップして「いつでも可/早番/遅番/時間指定」を選べます。\n"
                "「AIで希望を作成」に『月〇〇万円・火曜NG・夕方』のように書くと自動で入力されます。")
    if any(k in m for k in ["変更", "変え", "休み", "いけない"]):
        return ("確定シフトの変更・休みは、マイシフトタブでシフトバーをタップして「変更申請」から行えます。\n"
                "店長の承認後に反映されます。")
    return f"{name}さん、シフトについて知りたいことを教えてください。例: 「次のシフトは？」「来月5万円稼ぐには？」"
