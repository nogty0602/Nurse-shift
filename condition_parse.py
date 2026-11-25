"""対話で入力された日本語の条件を、シフト作成用の設定に変換する。

使い方:
    conds = parse_conditions("Aの夜勤は6回まで", known_names, ndays=30)
    rows, adjust = apply_conditions(base_rows, conds)

条件は「設定表の行」または「調整指示」のどちらかに変換される。
解釈できた条件には label（確認用の日本語）が付く。
"""
import re
import unicodedata

DOW_CHARS = "月火水木金土日"

# 勤務の呼び名 -> 内部キー
SHIFT_WORDS = [
    ("日勤", "day"), ("ひるま", "day"), ("昼勤", "day"),
    ("準夜", "eve"), ("じゅんや", "eve"),
    ("深夜", "nig"), ("しんや", "nig"),
    ("夜勤", "night_both"),
]

# 勤務記号（固定指示用）
SYMBOL_WORDS = [
    ("公休", "×"), ("休み", "×"), ("休日", "×"), ("オフ", "×"), ("休", "×"),
    ("年休", "年"), ("有休", "年"), ("有給", "年"),
    ("日勤", "ー"), ("準夜", "▲"), ("深夜", "●"), ("出張", "出"),
    ("外来", "G/-"), ("委員会", "ーイ"), ("研修", "研"),
]

KANJI_NUM = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _kanji_to_int(s):
    """一〜三十一 程度の漢数字を整数に。失敗したら None。"""
    s = s.strip()
    if not s:
        return None
    if all(c in KANJI_NUM for c in s):
        if len(s) == 1:
            return KANJI_NUM[s]
        return None
    if "十" in s:
        a, _, b = s.partition("十")
        tens = KANJI_NUM.get(a, 1) if a else 1
        ones = KANJI_NUM.get(b, 0) if b else 0
        if a and a not in KANJI_NUM:
            return None
        if b and b not in KANJI_NUM:
            return None
        return tens * 10 + ones
    return None


def normalize(text):
    """全角英数を半角に、区切り記号をそろえる。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace("～", "〜").replace("－", "-").replace("ー〜", "〜")
    return t.strip()


def _num_before(text, unit_pattern):
    """「6回」「六回」のような 数+単位 をすべて拾う。"""
    out = []
    for m in re.finditer(rf"([0-9]+|[一二三四五六七八九十〇零]+)\s*(?:{unit_pattern})", text):
        raw = m.group(1)
        v = int(raw) if raw.isdigit() else _kanji_to_int(raw)
        if v is not None:
            out.append((v, m.start(), m.end()))
    return out


def _find_names(text, known_names):
    """文中のスタッフ名をすべて拾う（長い名前を優先）。"""
    found = []
    used = [False] * len(text)
    for n in sorted(known_names, key=len, reverse=True):
        for m in re.finditer(re.escape(n), text):
            if any(used[m.start():m.end()]):
                continue
            # 英字1文字の名前は、前後が英字でないときだけ採用（AとBの誤検出防止）
            if re.fullmatch(r"[A-Za-z]+", n):
                before = text[m.start() - 1] if m.start() > 0 else ""
                after = text[m.end()] if m.end() < len(text) else ""
                if re.match(r"[A-Za-z]", before) or re.match(r"[A-Za-z]", after):
                    continue
            found.append((m.start(), n))
            for i in range(m.start(), m.end()):
                used[i] = True
    found.sort()
    return [n for _, n in found]


def _find_days(text, ndays):
    """「5日」「5日と12日」「10〜12日」などから日付の一覧を返す。"""
    days = set()
    # 範囲: 10〜12日 / 10-12日 / 10日から12日
    for m in re.finditer(r"([0-9]+)\s*(?:日)?\s*(?:〜|-|から)\s*([0-9]+)\s*日", text):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= b <= ndays:
            days.update(range(a, b + 1))
    # 単独: 5日 / 五日
    for v, _, _ in _num_before(text, "日"):
        if 1 <= v <= ndays:
            days.add(v)
    # 「5、12、18日」のように単位が最後だけ付く形
    m = re.search(r"((?:[0-9]+\s*[、,/・]\s*)+[0-9]+)\s*日", text)
    if m:
        for part in re.split(r"[、,/・]", m.group(1)):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= ndays:
                days.add(int(part))
    return sorted(days)


def _find_dow_targets(text):
    """曜日・平日・祝・毎日 などの対象を返す。"""
    targets = []
    if re.search(r"毎日|全日|すべての日|全部の日", text):
        return ["毎日"]
    if "平日" in text:
        targets.append("平日")
    if re.search(r"祝日|祝祭日|祝", text):
        targets.append("祝")
    if re.search(r"土日|週末", text):
        targets.extend(["土", "日"])
    for m in re.finditer(r"(?:毎週\s*)?([月火水木金土日])曜", text):
        c = m.group(1)
        if c not in targets:
            targets.append(c)
    return targets


def _find_shift(text):
    """文中の勤務種別を返す（複数可）。"""
    out = []
    for word, key in SHIFT_WORDS:
        if word in text:
            if key == "night_both":
                # 「夜勤」は準夜・深夜の両方。ただし準夜/深夜が明示されていれば無視
                if not any(w in text for w in ("準夜", "深夜")):
                    out.extend(["eve", "nig"])
            elif key not in out:
                out.append(key)
    return out


def _find_symbol(text):
    for word, sym in sorted(SYMBOL_WORDS, key=lambda x: -len(x[0])):
        if word in text:
            return sym, word
    return None, None


def _dow_sequence(text):
    """「金土日月」「土日」のような曜日の並びを抜き出す。"""
    m = re.search(rf"([{DOW_CHARS}]{{2,}})", text)
    if m:
        return m.group(1)
    return None


SHIFT_LABEL = {"day": "日勤", "eve": "準夜", "nig": "深夜"}


# =============================================================
#  個々の解釈ルール。該当すれば条件のリストを返す。
# =============================================================
def _rule_support(t, names, ndays):
    """応援（他部署からの助勤）"""
    if not re.search(r"応援|助勤|ヘルプ|応援者", t):
        return None
    nums = _num_before(t, "人|名")
    cnt = nums[0][0] if nums else 1
    shifts = _find_shift(t) or ["day"]
    key = "day" if "day" in shifts else shifts[0]
    days = _find_days(t, ndays)
    dows = _find_dow_targets(t)
    out = []
    targets = [str(d) for d in days] or dows or ["毎日"]
    for tg in targets:
        out.append({
            "kind": "support", "target": tg, "shift": key, "count": cnt,
            "label": f"{_t_label(tg)}の応援を{SHIFT_LABEL[key]}{_fmt(cnt)}名にする",
        })
    return out


def _rule_holiday(t, names, ndays):
    """祝日の追加・取り消し"""
    if not re.search(r"祝日", t):
        return None
    if not re.search(r"にする|に設定|扱い|追加|です|だ$|削除|外す|取り消", t):
        return None
    days = _find_days(t, ndays)
    if not days:
        return None
    remove = bool(re.search(r"削除|外す|取り消|ではない|じゃない", t))
    return [{
        "kind": "holiday", "days": days, "remove": remove,
        "label": ("、".join(f"{d}日" for d in days)
                  + ("を祝日から外す" if remove else "を祝日として扱う")),
    }]


def _rule_gairai(t, names, ndays):
    """外来の担当者・曜日"""
    if "外来" not in t:
        return None
    if not names:
        return None
    dows = [c for c in _find_dow_targets(t) if c in DOW_CHARS]
    if not dows:
        return None
    ampm = "午後" if "午後" in t else "午前"
    weeks = "毎週"
    m = re.search(r"第\s*([0-9])\s*(?:・|,|と|、)?\s*(?:第\s*([0-9]))?", t)
    if m and "第" in t:
        ws = [m.group(1)]
        if m.group(2):
            ws.append(m.group(2))
        weeks = "・".join(f"第{w}" for w in ws)
    out = []
    for dw in dows:
        out.append({
            "kind": "gairai", "dow": dw, "ampm": ampm, "weeks": weeks,
            "staff": list(names),
            "label": f"{dw}曜({weeks}・{ampm})の外来担当を {('・'.join(names))} にする",
        })
    return out


def _rule_fix(t, names, ndays):
    """特定の人の特定の日を固定する"""
    if not names:
        return None
    if re.search(r"人|名", t):          # 人数指定の文は対象外
        return None
    # 「休みを12日以上」のような個数の話は日付ではない
    if re.search(r"以上|以下|最低|合計|まで|ずつ", t):
        return None
    days = _find_days(t, ndays)
    if not days:
        return None
    sym, word = _find_symbol(t)
    if sym is None:
        return None
    out = []
    for n in names:
        for d in days:
            out.append({
                "kind": "fix", "staff": n, "day": d, "symbol": sym,
                "label": f"{n}さんの{d}日を「{word}」にする",
            })
    return out


def _rule_night_cap(t, names, ndays):
    """1人あたりの夜勤回数の上限"""
    if "夜勤" not in t and "夜勤回数" not in t:
        return None
    nums = _num_before(t, "回")
    if not nums:
        return None
    if not re.search(r"まで|以下|上限|超えない|にして|にする|に$", t):
        return None
    cap = nums[0][0]
    targets = names or ["全員"]
    return [{
        "kind": "night_cap", "target": n, "cap": cap,
        "label": (f"{n}さん" if n != "全員" else "全員")
                 + f"の夜勤を月{cap}回までにする",
    } for n in targets]


def _rule_rest_days(t, names, ndays):
    """1人あたりの休日数"""
    if not re.search(r"休み|休日|公休", t):
        return None
    if re.search(r"連休|前|後|深夜|準夜", t):
        return None
    nums = _num_before(t, "日")
    if not nums:
        return None
    need = nums[0][0]
    include_leave = not re.search(r"年休(?:は|を)?(?:含めない|除く|抜き)", t)
    targets = names or ["全員"]
    return [{
        "kind": "rest", "target": n, "days": need, "include_leave": include_leave,
        "label": (f"{n}さん" if n != "全員" else "全員")
                 + f"の休みを月{need}日以上にする"
                 + ("" if include_leave else "（年休は数えない）"),
    } for n in targets]


def _rule_headcount(t, names, ndays):
    """日ごとの必要人数"""
    shifts = _find_shift(t)
    if not shifts:
        return None
    nums = _num_before(t, "人|名")
    if not nums:
        return None
    if names:                            # 個人の話なら対象外
        return None
    # 範囲 10〜11人
    rng = re.search(r"([0-9]+(?:\.[0-9])?)\s*(?:〜|-|から)\s*([0-9]+(?:\.[0-9])?)\s*(?:人|名)", t)
    half = re.search(r"([0-9]+\.[0-9])\s*(?:人|名)", t)
    if rng:
        lo, hi = float(rng.group(1)), float(rng.group(2))
    else:
        v = float(half.group(1)) if half else float(nums[0][0])
        if re.search(r"以上|最低|下限", t):
            lo, hi = v, None
        elif re.search(r"まで|以下|上限", t):
            lo, hi = None, v
        else:
            lo = hi = v
    days = _find_days(t, ndays)
    dows = _find_dow_targets(t)
    targets = [str(d) for d in days] or dows or ["毎日"]
    out = []
    for tg in targets:
        for sh in shifts:
            out.append({
                "kind": "headcount", "target": tg, "shift": sh, "lo": lo, "hi": hi,
                "label": f"{_t_label(tg)}の{SHIFT_LABEL[sh]}を{_range_label(lo, hi)}にする",
            })
    return out


def _rule_person_cond(t, names, ndays):
    """個人の勤務条件（不可・この曜日だけ・この曜日以外）"""
    if not names:
        return None
    shifts = _find_shift(t)
    if not shifts:
        return None
    if re.search(r"人|名|回|連勤|応援", t):
        return None
    out = []
    if re.search(r"不可|できな|できませ|入れな|入りませ|させな|無理|禁止|外す|なし|ありませ", t):
        for n in names:
            for sh in shifts:
                out.append({
                    "kind": "cond", "staff": n, "shift": sh, "value": "不可",
                    "label": f"{n}さんは{SHIFT_LABEL[sh]}に入らない",
                })
        return out
    seq = _dow_sequence(t)
    if seq and re.search(r"だけ|のみ|限|しか", t):
        for n in names:
            for sh in shifts:
                out.append({
                    "kind": "cond", "staff": n, "shift": sh, "value": seq,
                    "label": f"{n}さんの{SHIFT_LABEL[sh]}は{_dow_label(seq)}のみにする",
                })
        return out
    m = re.search(rf"([{DOW_CHARS}])曜?以外", t)
    if m:
        for n in names:
            for sh in shifts:
                out.append({
                    "kind": "cond", "staff": n, "shift": sh, "value": "除" + m.group(1),
                    "label": f"{n}さんの{SHIFT_LABEL[sh]}は{m.group(1)}曜以外にする",
                })
        return out
    if seq:
        for n in names:
            for sh in shifts:
                out.append({
                    "kind": "cond", "staff": n, "shift": sh, "value": seq,
                    "label": f"{n}さんの{SHIFT_LABEL[sh]}は{_dow_label(seq)}のみにする",
                })
        return out
    # 曜日が無く「深夜だけ」のような形 → その勤務だけ可（他は不可）
    if len(shifts) == 1 and re.search(r"だけ|のみ|しか", t):
        keep = shifts[0]
        others = [k for k in ("day", "eve", "nig") if k != keep]
        if keep != "day":
            others = [k for k in others if k != "day"]   # 日勤は残す
        for n in names:
            for sh in others:
                out.append({
                    "kind": "cond", "staff": n, "shift": sh, "value": "不可",
                    "label": f"{n}さんは{SHIFT_LABEL[sh]}に入らない"
                             f"（{SHIFT_LABEL[keep]}のみ）",
                })
        return out or None
    return None


def _rule_pre_rest(t, names, ndays):
    """深夜の前は必ず休み"""
    if not names:
        return None
    if not re.search(r"深夜の前|深夜前|●の前", t):
        return None
    if not re.search(r"休", t):
        return None
    return [{
        "kind": "pre_rest", "staff": n,
        "label": f"{n}さんは深夜の前日を必ず休みにする",
    } for n in names]


def _rule_no_daynight(t, names, ndays):
    """日勤の直後の深夜（ー●）を不可に"""
    if not names:
        return None
    if not re.search(r"ー●|日勤深夜|日勤の後|日勤のあと|日勤明け", t):
        return None
    return [{
        "kind": "no_dn", "staff": n,
        "label": f"{n}さんは日勤の直後に深夜へ入れない",
    } for n in names]


def _rule_run_rest(t, names, ndays):
    """連勤後の2連休"""
    if not re.search(r"連勤", t):
        return None
    if not re.search(r"連休|休み|2日|二日", t):
        return None
    m = re.search(r"([0-9]+|[一二三四五六七八九十]+)\s*連勤", t)
    run = 5
    if m:
        v = int(m.group(1)) if m.group(1).isdigit() else _kanji_to_int(m.group(1))
        if v:
            run = v
    soft = bool(re.search(r"なるべく|できれば|優先", t))
    targets = names or ["全員"]
    return [{
        "kind": "rest2_after_run", "staff": n, "run": run, "hard": not soft,
        "label": (f"{n}さん" if n != "全員" else "全員")
                 + f"は{run}連勤の後に2連休を入れる" + ("（なるべく）" if soft else ""),
    } for n in targets]


def _rule_eve_run(t, names, ndays):
    """準夜の集中を抑える"""
    if "準夜" not in t:
        return None
    if not re.search(r"続|偏|ばかり|連続|集中", t):
        return None
    nums = _num_before(t, "回")
    lim = nums[0][0] if nums else 2
    soft = bool(re.search(r"なるべく|できれば|優先", t))
    targets = names or ["全員"]
    return [{
        "kind": "no_eve_run", "staff": n, "limit": lim, "hard": not soft,
        "label": (f"{n}さん" if n != "全員" else "全員")
                 + f"の準夜を7日間で{lim}回までにする",
    } for n in targets]


RULES = [
    _rule_support, _rule_holiday, _rule_gairai, _rule_run_rest, _rule_eve_run,
    _rule_pre_rest, _rule_no_daynight, _rule_fix, _rule_night_cap,
    _rule_rest_days, _rule_headcount, _rule_person_cond,
]


def _dow_label(seq):
    return "".join(seq) + "曜" if len(seq) == 1 else "".join(seq) + "曜日"


def _t_label(tg):
    if tg == "毎日":
        return "毎日"
    if tg == "平日":
        return "平日"
    if tg == "祝":
        return "祝日"
    if tg.isdigit():
        return f"{tg}日"
    return f"{tg}曜"


def _fmt(v):
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


def _range_label(lo, hi):
    if lo is not None and hi is not None:
        return f"{_fmt(lo)}人" if lo == hi else f"{_fmt(lo)}〜{_fmt(hi)}人"
    if lo is not None:
        return f"{_fmt(lo)}人以上"
    return f"{_fmt(hi)}人まで"


def parse_conditions(text, known_names, ndays=31):
    """日本語テキスト → (条件リスト, 解釈できなかった行)"""
    conds, unknown = [], []
    for raw in re.split(r"[\n。]", text or ""):
        line = raw.strip()
        if not line:
            continue
        t = normalize(line)
        names = _find_names(t, known_names)
        got = None
        for rule in RULES:
            try:
                got = rule(t, names, ndays)
            except Exception:
                got = None
            if got:
                break
        if got:
            for c in got:
                c["source"] = line
            conds.extend(got)
        else:
            unknown.append(line)
    return conds, unknown


# =============================================================
#  条件 → 設定表 / 調整指示
# =============================================================
def _set_row(rows, key, match_col, match_val, updates):
    """設定表の該当行を更新（無ければ追加）。updates は {列index: 値}。"""
    for r in rows[key]:
        if len(r) > match_col and str(r[match_col]).strip() == str(match_val):
            for ci, v in updates.items():
                while len(r) <= ci:
                    r.append("")
                if v is not None:
                    r[ci] = v
            return r
    width = max([len(x) for x in rows[key]] + [max(updates) + 1, match_col + 1])
    row = [""] * width
    row[match_col] = match_val
    for ci, v in updates.items():
        row[ci] = "" if v is None else v
    rows[key].append(row)
    return row


def apply_conditions(rows, conditions, holidays=None):
    """条件を設定表に反映し、調整指示リストと祝日集合を返す。"""
    adjust = []
    holidays = set(holidays or [])
    for c in conditions:
        k = c["kind"]
        if k == "support":
            col = {"day": 1, "eve": 2, "nig": 3}[c["shift"]]
            _set_row(rows, "support", 0, c["target"], {col: c["count"]})
        elif k == "headcount":
            base = {"day": 1, "eve": 3, "nig": 5}[c["shift"]]
            upd = {}
            if c["lo"] is not None:
                upd[base] = _num(c["lo"])
            if c["hi"] is not None:
                upd[base + 1] = _num(c["hi"])
            tgts = (list(DOW_CHARS) + ["祝"]) if c["target"] == "毎日" else [c["target"]]
            for tg in tgts:
                _set_row(rows, "headcount", 0, tg, dict(upd))
        elif k == "night_cap":
            _set_row(rows, "night_cap", 0, c["target"], {1: c["cap"]})
        elif k == "rest":
            _set_row(rows, "rest", 0, c["target"],
                     {1: c["days"], 2: "○" if c["include_leave"] else ""})
        elif k == "cond":
            col = {"eve": 1, "nig": 2, "day": 3}[c["shift"]]
            _set_row(rows, "cond", 0, c["staff"], {col: c["value"]})
        elif k == "pre_rest":
            if not any(str(r[0]).strip() == c["staff"] for r in rows["pre_rest"] if r):
                rows["pre_rest"].append([c["staff"]])
        elif k == "no_dn":
            if not any(str(r[0]).strip() == c["staff"] for r in rows["no_dn"] if r):
                rows["no_dn"].append([c["staff"]])
        elif k == "gairai":
            _set_row(rows, "gairai", 0, c["dow"],
                     {1: c["ampm"], 2: c["weeks"], 3: "・".join(c["staff"])})
        elif k == "holiday":
            for d in c["days"]:
                if c["remove"]:
                    holidays.discard(d)
                else:
                    holidays.add(d)
        elif k in ("fix", "rest2_after_run", "no_eve_run"):
            a = {"staff": c.get("staff"), "rule": k, "hard": c.get("hard", True)}
            if k == "fix":
                a.update(day=c["day"], symbol=c["symbol"])
            elif k == "rest2_after_run":
                a.update(run=c["run"])
            else:
                a.update(limit=c["limit"])
            if a["staff"] == "全員":
                continue          # 全員向けは固定ルールで既に担保
            adjust.append(a)
    return rows, adjust, holidays


def _num(v):
    f = float(v)
    return int(f) if f == int(f) else f


def suggest_phrasing(line, known_names):
    """解釈できなかった文に対して、書き方の例を返す。"""
    t = normalize(line)
    names = _find_names(t, known_names)
    who = names[0] if names else "Aさん"
    shifts = _find_shift(t)
    if "応援" in t or "助勤" in t:
        return f"「5日に応援2人」「毎週水曜に応援1人」のように書いてください"
    if "夜勤" in t and names:
        return f"「{who}の夜勤は6回まで」のように、回数を入れてください"
    if re.search(r"休み|休日|公休", t):
        if names:
            return f"「{who}の休みは11日以上」または「{who}の5日は休み」のように書いてください"
        return "「休みは11日以上」のように書いてください"
    if shifts and not names:
        sh = SHIFT_LABEL.get(shifts[0], "日勤")
        return f"「水曜の{sh}は9人」「5日の{sh}は10人」のように、人数を入れてください"
    if shifts and names:
        sh = SHIFT_LABEL.get(shifts[0], "日勤")
        return (f"「{who}は{sh}に入れない」「{who}の{sh}は土日だけ」"
                "のように書いてください")
    if names:
        return f"「{who}の5日は休み」「{who}の夜勤は6回まで」のように書いてください"
    return "「水曜の日勤は9人」「5日に応援2人」のように書いてください"
