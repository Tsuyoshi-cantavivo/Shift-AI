"""name_match.py - 希望表に書かれた名前をスタッフに突き合わせる。

【なぜ必要か】
  従来は by_name の完全一致のみで、「田中さん」「タナカ」のような表記ゆれが
  すべて未割り当てになっていた。画像から読み取る場合は誤認識も加わるため、
  候補を出して人に選ばせる仕組みが要る。

【自動確定しない原則】
  best_exact は「正規化して完全一致がちょうど1件」のときだけ ID を返す。
  同姓同名が2人いる場合は None を返して人に選ばせる。
  既存の _extract_staff_hint（src/ai.py:1087）が守っている
  「候補が複数なら名簿順で決めない」原則を引き継ぐ。

外部依存なし（標準ライブラリのみ）。
"""
import unicodedata

# 敬称。長いものから順に剥がす（「ちゃん」を「ん」より先に消す）
_HONORIFICS = ("ちゃん", "さん", "サン", "くん", "クン", "君", "様", "さま", "氏")

# 除去する記号・空白。全角スペースは NFKC で半角になる
_STRIP_CHARS = " \t　・､、,。.／/\\-ー―‐_（）()「」『』【】[]{}〈〉"


def _kata(s):
    """ひらがなをカタカナに寄せる（読み表記のゆれを吸収する）。"""
    out = []
    for ch in s:
        code = ord(ch)
        # ひらがな（U+3041-U+3096）をカタカナ（U+30A1-U+30F6）へ
        if 0x3041 <= code <= 0x3096:
            out.append(chr(code + 0x60))
        else:
            out.append(ch)
    return "".join(out)


def normalize_name(s):
    """名前を比較用に正規化する。

    NFKC（全角→半角・互換文字の統一）→ 敬称除去 → 記号/空白除去
    → ひらがな→カタカナ → casefold の順。
    敬称は記号除去より先に剥がす（「田中 さん」のような分かち書きに対応するため
    空白除去の後にもう一度剥がす）。
    """
    if not s or not isinstance(s, str):
        return ""
    t = unicodedata.normalize("NFKC", s)
    t = _strip_honorifics(t)
    t = "".join(ch for ch in t if ch not in _STRIP_CHARS)
    t = _strip_honorifics(t)  # 「田中 さん」→空白除去後に再度剥がす
    t = _kata(t)
    return t.casefold()


def _strip_honorifics(t):
    changed = True
    while changed:
        changed = False
        for h in _HONORIFICS:
            if len(t) > len(h) and t.endswith(h):
                t = t[: -len(h)]
                changed = True
    return t


def _levenshtein(a, b):
    """編集距離。名前は短いので素朴な DP で十分（外部依存を増やさない）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _split_candidates(n):
    """姓・名の切り出し候補。区切りが無い日本語名は前半/後半で割る。

    「田中太郎」(4文字) → ["田中", "太郎"] のような分割を試す。
    厳密な姓名分解はできないので、部分一致の判定材料として使う。
    """
    out = set()
    if len(n) >= 2:
        for cut in range(1, len(n)):
            out.add(n[:cut])
            out.add(n[cut:])
    return out


def best_exact(hint, staffs):
    """正規化して完全一致がちょうど1件のときだけ staff_id を返す。

    0件・2件以上（同姓同名）は None。自動確定してよいのはこのケースだけ。
    """
    h = normalize_name(hint)
    if not h:
        return None
    hits = [s for s in (staffs or []) if normalize_name(s.get("name")) == h]
    return hits[0]["id"] if len(hits) == 1 else None


def match_staff(hint, staffs):
    """候補をスコア降順で返す。0.6 未満は含めない。

    戻り値: [{"staff_id", "name", "score", "reason"}]
    """
    h = normalize_name(hint)
    if not h:
        return []
    out = []
    for s in (staffs or []):
        n = normalize_name(s.get("name"))
        if not n:
            continue
        score, reason = _score(h, n)
        if score >= 0.6:
            out.append({"staff_id": s["id"], "name": s.get("name"),
                        "score": round(score, 3), "reason": reason})
    out.sort(key=lambda c: (-c["score"], c["staff_id"]))
    return out


def _score(h, n):
    """正規化済みの2つの名前からスコアと理由を返す。"""
    if h == n:
        return 1.0, "名前が一致"
    parts = _split_candidates(n)
    if h in parts:
        return 0.85, "姓または名が一致"
    if n.startswith(h) or n.endswith(h):
        return 0.75, "名前の一部が一致"
    if h in n:
        return 0.7, "名前に含まれる"
    dist = _levenshtein(h, n)
    longest = max(len(h), len(n))
    sim = 1.0 - (dist / longest) if longest else 0.0
    if sim >= 0.6:
        return sim, "よく似た名前"
    return 0.0, ""
