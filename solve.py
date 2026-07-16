"""第1段階 CP-SAT ソルバー本体."""
import os
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
from ortools.sat.python import cp_model
from shift_core import (parse, MASTER, OFF, LEAVE, DAY, EVE, NIGHT, OFFSITE,
                        GAI, DAYNIGHT, TRAIN, TRAIN_HALF, TRAIN_2H, DAYCOUNT_HALF,
                        FIXED, ALLOWED, DAY_REQ, EVE_REQ, NIGHT_REQ, DOW_FRI_MON)

STATES = [OFF, LEAVE, DAY, EVE, NIGHT, OFFSITE, GAI, DAYNIGHT,
          TRAIN, TRAIN_HALF, TRAIN_2H]
WORK = {DAY, EVE, NIGHT, OFFSITE, GAI, DAYNIGHT, TRAIN, TRAIN_HALF, TRAIN_2H}
REST = {OFF, LEAVE}
NIGHTS = {EVE, NIGHT, DAYNIGHT}          # 夜勤（明け・並び対象）


def allowed_on(rule, dow):
    """rule 例: None/''=全曜日可, '不可'=禁止, '金土日月'=その曜日のみ, '除日'=日曜以外."""
    if rule is None:
        return True
    r = str(rule).strip()
    if r == "":
        return True
    if "不可" in r:
        return False
    wd = {ch for ch in r if ch in "月火水木金土日"}
    if "除" in r:
        return dow not in wd
    if wd:
        return dow in wd
    return True


SHIFT_JP = {EVE: "準夜", NIGHT: "深夜", DAY: "日勤"}


def build_allowed(name, dtype, dow, sym, shift_rules, phase_names=frozenset(), chief=False):
    """1セルの許容状態集合と警告を返す."""
    if chief:                                            # 師長: 平日ー / 土日祝×
        return ({OFF} if dtype in ("sat", "sun") else {DAY}), None
    rules = shift_rules.get(name, {})
    base = {OFF}
    if allowed_on(rules.get(DAY), dow):   base.add(DAY)
    if allowed_on(rules.get(EVE), dow):   base.add(EVE)
    if allowed_on(rules.get(NIGHT), dow): base.add(NIGHT)

    if sym in FIXED:
        st = FIXED[sym]
        # フェーズ対象者の深夜希望は「解禁前なら休みへ」＝ソフト（下で極力尊重）
        if name in phase_names and st == NIGHT:
            return ({NIGHT, OFF} if NIGHT in base else {OFF}), None
        if st in (EVE, NIGHT, DAY):
            rule = rules.get(st)
            if rule and "不可" in str(rule):             # 絶対不可は希望でも上書きしない
                return base, f"{name}:{sym} は{SHIFT_JP[st]}不可のため無効化"
            return {st}, None                            # 曜日限定は希望優先で上書き
        return {st}, None                                # ×/年/出
    if sym in ALLOWED:
        allow = base & ALLOWED[sym]
        return (allow or base), None
    return base, None                                    # 空欄


def solve(path, holidays, time_limit=60, prev_path=None):
    data = parse(path, holidays)
    prev = {}
    prev_error = None
    if prev_path:
        from shift_core import parse_prev_schedule
        try:
            prev = parse_prev_schedule(prev_path)
            if not prev:
                prev_error = "前月の勤務表を読み込めませんでした（『勤務表』シート・スタッフ名の列・日付の見出しをご確認ください）。月またぎ判定なしで作成します"
        except Exception as e:          # 読めなくても生成は続行
            prev = {}
            prev_error = f"前月の勤務表の読み込みに失敗しました（{e}）。月またぎ判定なしで作成します"
    days, dtype, dow = data["days"], data["dtype"], data["dow"]
    staff = data["staff"]
    names = [s["name"] for s in staff]
    lvl = {}
    lvl_missing = []
    for s in staff:
        v = s["level"]
        if v is None:
            lvl[s["name"]] = 1          # レベル未入力は暫定で1として扱う
            lvl_missing.append(s["name"])
        else:
            lvl[s["name"]] = v
    idx = {n: i for i, n in enumerate(names)}
    warnings = []
    if prev_error:
        warnings.append(prev_error)
    if lvl_missing:
        warnings.append("レベル未入力のため暫定でLv1として扱いました: " + "、".join(lvl_missing)
                        + "（希望届のレベル欄に数字を入力してください）")
    shift_rules = data["settings"].get("shift_rules", {})
    phase_def = data["settings"].get("phase_def", [])
    lv1_exp = data["settings"].get("lv1_exp", {})
    roster = data["settings"].get("roster", {})
    emp = {s["name"]: (s.get("emp") or "") for s in staff}
    staff_team = {s["name"]: s.get("team") for s in staff}

    # スタッフ属性：チーム/レベルは希望届優先、役割は詳細設定、師長は雇用or役割から
    def A_(n, key):
        if n in roster and roster[n].get(key):
            return roster[n][key]
        return MASTER.get(n, {}).get(key, None if key == "team" else False)

    attr = {}
    for n in names:
        team = staff_team.get(n) or A_(n, "team")
        chief = bool(roster.get(n, {}).get("chief")) or ("師長" in emp[n]) or \
                bool(MASTER.get(n, {}).get("chief"))
        attr[n] = dict(
            team=team,
            chief=chief,
            support_required=bool(A_(n, "support_required")),
            can_support=bool(A_(n, "can_support")),
            no_leader=bool(A_(n, "no_leader")),
        )

    from shift_core import weekday_night_bounds
    bounds = weekday_night_bounds(phase_def) if phase_def else {}
    # フェーズ対象＝経験回数表に載っている人だけ（データ駆動）
    phase_names = set(lv1_exp.keys())
    start_cnt = {n: lv1_exp.get(n, {}).get("start", 0) for n in phase_names}
    wish_prio = {n: lv1_exp.get(n, {}).get("wish_priority", False) for n in phase_names}
    # フェーズ対象者の深夜許容曜日＝いずれかの段階で許可される曜日（タイミングは制約で担保）
    if phase_def:
        wk_allowed = "".join(w for w in "月火水木金土日" if bounds.get(w) is not None)
        for n in phase_names:
            shift_rules.setdefault(n, {})
            shift_rules[n][NIGHT] = wk_allowed if wk_allowed else "不可"
    # ●希望をソフト扱い（解禁前は休みに回す）にする対象＝希望優先でない人
    phase_soft = {n for n in phase_names if not wish_prio[n]}
    no_daynight = data["settings"].get("no_daynight", set())
    pre_rest = data["settings"].get("pre_rest", set())
    gairai = data["settings"].get("gairai", [])
    holidays = data.get("holidays", set())
    cells_of = {s["name"]: s["cells"] for s in staff}

    from shift_core import GAI, DAYNIGHT, gairai_match_days

    # 許容集合
    allowed = {}
    soft_night_wish = []
    honored_wish = set()
    for s in staff:
        n = s["name"]
        for d in days:
            sym = s["cells"].get(d, "")
            al, w = build_allowed(n, dtype[d], dow[d], sym, shift_rules,
                                  frozenset(phase_soft), attr[n]["chief"])
            allowed[(n, d)] = al
            if w:
                warnings.append(f"D{d} {w}")
            if n in phase_soft and sym == "●":
                soft_night_wish.append((n, d))
            if n in phase_names and n not in phase_soft and sym == "●":
                honored_wish.add((n, d))       # 希望優先○：フェーズ制約をこの日はかけない

    # 【固定ルール】希望休(×)の前日は準夜(▲)不可 / 翌日は深夜(●)不可
    # ※本人が書いた ▲/● 希望よりルールを優先し、その日は休みに変更する
    for s in staff:
        n = s["name"]
        if attr[n]["chief"]:
            continue
        for i, d in enumerate(days):
            if s["cells"].get(d, "") != "×":
                continue
            if i > 0:                                    # 前日: 準夜を除外
                pd = days[i - 1]
                if EVE in allowed[(n, pd)]:
                    al = allowed[(n, pd)] - {EVE}
                    if not al:
                        al = {OFF}
                        warnings.append(f"D{pd} {n}:×の前日のため準夜希望を休みに変更")
                    allowed[(n, pd)] = al
            if i + 1 < len(days):                        # 翌日: 深夜を除外
                nd_ = days[i + 1]
                if NIGHT in allowed[(n, nd_)] or DAYNIGHT in allowed[(n, nd_)]:
                    al = allowed[(n, nd_)] - {NIGHT, DAYNIGHT}
                    if not al:
                        al = {OFF}
                        warnings.append(f"D{nd_} {n}:×の翌日のため深夜希望を休みに変更")
                    allowed[(n, nd_)] = al

    # 外来割当：各エントリのプールから1日1名をGAIに（回数はプール内で均等化）
    gairai_cells = {}
    gairai_slots = []       # (pool, days, avail)

    # 本人が希望届に外来記号(G/- , -/G)を記入している日は、その日を外来として確定し
    # その日は他の人を外来に入れない（外来は1日1名）。
    # 記号は詳細設定の時間帯(午前/午後)に合わせて自動補正する。
    sym_by_day = {}                 # day -> 設定上の記号(午前/午後)
    for e in gairai:
        for d in gairai_match_days(days, dow, e):
            if d not in holidays:
                sym_by_day[d] = e["symbol"]

    wish_gairai_days = {}          # day -> name
    for s in staff:
        n = s["name"]
        for d, sym in s["cells"].items():
            if sym in ("G/-", "-/G", "外"):
                correct = sym_by_day.get(d)
                if correct and correct != sym:
                    warnings.append(
                        f"D{d} {n}:外来記号を設定の時間帯に合わせて {sym}→{correct} に補正")
                    sym = correct
                elif sym == "外":
                    sym = "G/-"
                gairai_cells[(n, d)] = sym
                wish_gairai_days[d] = n

    for e in gairai:
        pool = [n for n in (e.get("staff") or []) if n in names]
        if not pool:
            continue
        gdays = [d for d in gairai_match_days(days, dow, e) if d not in holidays]
        avail = {}
        for d in gdays:
            if d in wish_gairai_days:              # 本人記入の外来が既にある日は自動割当しない
                owner = wish_gairai_days[d]
                if owner not in pool:
                    warnings.append(f"D{d} {owner}:希望届に外来記入あり（この日の自動割当はスキップ）")
                avail[d] = []
                continue
            cand = []
            for n in pool:
                wish = cells_of.get(n, {}).get(d, "")
                if wish in ("×", "●", "▲", "年", "出"):
                    continue                       # 本人希望を優先し外来対象外
                allowed[(n, d)] = allowed[(n, d)] | {GAI}
                gairai_cells[(n, d)] = e["symbol"]   # 設定の時間帯(午前/午後)の記号を優先
                cand.append(n)
            avail[d] = cand
        gairai_slots.append((pool, gdays, avail))

    mdl = cp_model.CpModel()
    x = {}
    for n in names:
        for d in days:
            for st in allowed[(n, d)]:
                x[(n, d, st)] = mdl.NewBoolVar(f"x_{n}_{d}_{st}")
            mdl.AddExactlyOne(x[(n, d, st)] for st in allowed[(n, d)])

    def xv(n, d, st):
        return x.get((n, d, st))

    def has(n, d, sts):
        return [x[(n, d, st)] for st in sts if (n, d, st) in x]

    # 集合（すべて詳細設定のスタッフ属性から）
    team = {n: attr[n]["team"] for n in names}
    is_chief = {n: attr[n]["chief"] for n in names}
    # 夜勤可能＝いずれかの日に準夜/深夜が許容されている人（詳細設定に追従）
    night_cap = [n for n in names if not is_chief[n] and
                 any((EVE in allowed[(n, d)]) or (NIGHT in allowed[(n, d)]) for d in days)]
    lv4plus = [n for n in names if lvl[n] >= 4 and not is_chief[n]]
    lv3plus_noT = [n for n in names if lvl[n] >= 3 and not attr[n]["no_leader"]
                   and not is_chief[n]]
    lv1 = [n for n in names if lvl[n] == 1]
    tanshuku = {s["name"] for s in staff if s.get("tanshuku")}
    # 日勤リーダー資格 = Lv4以上・非師長・非時短
    lv4plus = [n for n in lv4plus if n not in tanshuku]

    pen = []  # (weight, var)

    def slack(name, w):
        v = mdl.NewIntVar(0, 99, name); pen.append((w, v)); return v

    from shift_core import resolve_req
    headcount = data["settings"].get("headcount", [])
    holidays = data.get("holidays", set())

    def deep_vars(n, d):
        return [x[(n, d, st)] for st in (NIGHT, DAYNIGHT) if (n, d, st) in x]

    for d in days:
        req = resolve_req(headcount, d, dow[d], dtype[d], d in holidays)
        # --- 日勤(半単位: ー=2, 外来=1) 下限loH〜上限hiH ---
        loH = int(round(req["day"][0] * 2)); hiH = int(round(req["day"][1] * 2))
        day_terms = []
        for n in names:
            if is_chief[n]:
                continue
            for st, coef in DAYCOUNT_HALF.items():
                if coef and (n, d, st) in x:
                    day_terms.append(coef * x[(n, d, st)])
        sh = slack(f"dsh{d}", 3000); ov = slack(f"dov{d}", 60)   # 日勤: 不足は重く、超過も抑制
        mdl.Add(sum(day_terms) + sh >= loH)
        mdl.Add(sum(day_terms) <= hiH)               # 上限（ハード）
        mdl.Add(sum(day_terms) - ov <= loH)          # 目標loに寄せる（超過は軽ペナルティ）

        # --- 準夜(EVE) ---
        e_lo, e_hi = int(req["eve"][0]), int(req["eve"][1])
        ev = [x[(n, d, EVE)] for n in names if (n, d, EVE) in x]
        se = slack(f"esh{d}", 12000)   # 準夜の人数不足は最優先で回避
        mdl.Add(sum(ev) <= e_hi); mdl.Add(sum(ev) + se >= e_lo)
        # --- 深夜(deep = ● + ー●) ---
        n_lo, n_hi = int(req["nig"][0]), int(req["nig"][1])
        deepall = [v for n in names for v in deep_vars(n, d)]
        sn = slack(f"nsh{d}", 12000)   # 深夜の人数不足は最優先で回避
        mdl.Add(sum(deepall) <= n_hi); mdl.Add(sum(deepall) + sn >= n_lo)

        # 準夜・深夜 共通のチーム/Lv1/リーダー
        for label, per in (("e", lambda n: [x[(n, d, EVE)]] if (n, d, EVE) in x else []),
                           ("n", lambda n: deep_vars(n, d))):
            ta = [v for n in names if team[n] == "A" for v in per(n)]
            tb = [v for n in names if team[n] == "B" for v in per(n)]
            mdl.Add(sum(ta) <= 2); mdl.Add(sum(tb) <= 2)
            sa = slack(f"{label}ta{d}", 200); sb = slack(f"{label}tb{d}", 200)
            mdl.Add(sum(ta) + sa >= 1); mdl.Add(sum(tb) + sb >= 1)
            l1 = [v for n in lv1 for v in per(n)]
            if l1:
                mdl.Add(sum(l1) <= 1)
            ll = [v for n in lv3plus_noT for v in per(n)]
            s = slack(f"{label}lsh{d}", 800); mdl.Add(sum(ll) + s >= 1)

        # 日勤リーダー(Lv4以上・非時短)
        dl = [x[(n, d, DAY)] for n in lv4plus if (n, d, DAY) in x] + \
             [x[(n, d, DAYNIGHT)] for n in lv4plus if (n, d, DAYNIGHT) in x]
        sdl = slack(f"dlsh{d}", 800); mdl.Add(sum(dl) + sdl >= 1)

        # 深夜サポート必須（サポート必須の人が深夜(●/ー●)に入る日は同チーム支援者が必要）
        for g in [n for n in names if attr[n]["support_required"]]:
            gv = deep_vars(g, d)
            if not gv:
                continue
            sup = [v for n in names if n != g and team[n] == team[g]
                   and (lvl[n] >= 2 or attr[n]["can_support"]) for v in deep_vars(n, d)]
            if sup:
                mdl.Add(sum(gv) <= sum(sup))
            else:
                for v in gv:
                    mdl.Add(v == 0)
        # 【項目4】日勤：Lv1の人数 ≤ Lv3以上の人数（ペアを組める体制）
        d_l1 = [x[(n, d, DAY)] for n in lv1 if (n, d, DAY) in x]
        d_l3 = [x[(n, d, st)] for n in names for st in (DAY, DAYNIGHT)
                if lvl[n] >= 3 and not is_chief[n] and (n, d, st) in x]
        if d_l1:
            sp = slack(f"pair{d}", 500)
            mdl.Add(sum(d_l1) <= sum(d_l3) + sp)

    # 外来：各外来日は1名（プールから）／回数はプール内で均等化
    for gi, (pool, gdays, avail) in enumerate(gairai_slots):
        auto_days = [d for d in gdays if avail.get(d)]      # 自動割当する日だけ
        for d in auto_days:
            cand = [x[(n, d, GAI)] for n in avail[d] if (n, d, GAI) in x]
            if cand:
                short = slack(f"gsh{gi}_{d}", 900)
                mdl.Add(sum(cand) + short == 1)          # ちょうど1名（埋まらない時のみ緩和）
        base = len(auto_days) // len(pool) if pool else 0
        for n in pool:
            cnt = [x[(n, d, GAI)] for d in auto_days if (n, d, GAI) in x]
            if not cnt:
                continue
            hi = slack(f"ghi{gi}_{n}", 30); lo = slack(f"glo{gi}_{n}", 30)
            mdl.Add(sum(cnt) - hi <= base + 1)           # 最大 ceil
            mdl.Add(sum(cnt) + lo >= base)               # 最小 floor

    # 【固定ルール】外来は病棟全体で 1日1名まで（担当日以外・担当者以外の外来は禁止）
    for d in days:
        allg = [x[(n, d, GAI)] for n in names if (n, d, GAI) in x]
        if allg:
            mdl.Add(sum(allg) <= 1)

    # 夜勤重複回避（詳細設定シートの「同時不可グループ」）
    # 各グループ内の全ペアを対象。厳守=ハード、それ以外=強いソフト(重み300)。
    pairset = {}   # (a,b) -> hard?
    for grp in data["settings"].get("night_no_overlap", []):
        mem = [m for m in grp["members"] if m in names]
        for ai in range(len(mem)):
            for bi in range(ai + 1, len(mem)):
                key = tuple(sorted((mem[ai], mem[bi])))
                pairset[key] = pairset.get(key, False) or grp["hard"]
    # 後方互換：設定が無ければ MASTER の night_conflict を使う
    if not pairset:
        for a in names:
            for b in MASTER[a].get("night_conflict", []):
                if b in names:
                    pairset[tuple(sorted((a, b)))] = False
    for (a, b), hard in pairset.items():
        for d in days:
            na = has(a, d, NIGHTS); nb = has(b, d, NIGHTS)
            if not na or not nb:
                continue
            if hard:
                mdl.Add(sum(na) + sum(nb) <= 1)
            else:
                z = mdl.NewBoolVar(f"conf_{a}{b}_{d}")
                mdl.Add(z >= sum(na) + sum(nb) - 1)
                pen.append((300, z))

    # フェーズ別 夜勤解禁：深夜●は「その日の(開始回数+当月の深夜数)」が曜日の許容段階内のときだけ
    for n in phase_names:
        if not phase_def:
            break
        night_ind = {d: x.get((n, d, NIGHT)) for d in days}
        for i, d in enumerate(days):
            v = night_ind[d]
            if v is None or (n, d) in honored_wish:
                continue
            b = bounds.get(dow[d])
            if b is None:
                continue
            pmin, pmax = b
            prior = start_cnt[n] + sum(night_ind[days[j]] for j in range(i)
                                       if night_ind[days[j]] is not None)
            if pmax is not None:
                mdl.Add(prior <= pmax).OnlyEnforceIf(v)
            if pmin and pmin > 0:
                mdl.Add(prior >= pmin).OnlyEnforceIf(v)

    # フェーズ対象者の深夜希望：解禁済みなら極力尊重（休みに回ると軽いペナルティ）
    for (n, d) in soft_night_wish:
        if (n, d, OFF) in x:
            pen.append((40, x[(n, d, OFF)]))

    # 【月またぎ】前月末の勤務から、月初の連勤・夜勤明け・並びを制約
    if prev:
        matched = [n for n in names if n in prev]
        unmatched = sorted(set(prev) - set(names))
        warnings.append(
            f"前月の勤務表を読み込みました（照合できたスタッフ: {len(matched)}名 / "
            f"前月{len(prev)}名中）")
        if unmatched:
            warnings.append(
                "前月にいて今月の希望届に見つからないスタッフ（月またぎ判定なし）: "
                + "、".join(unmatched)
                + " ※退職・休職なら問題ありません。名前の表記違い（空白・旧姓など）の場合は"
                  "希望届の氏名を前月の勤務表と揃えてください")
        no_prev = sorted(n for n in names if n not in prev and not attr[n]["chief"])
        if no_prev:
            warnings.append(
                "今月の希望届にいて前月の勤務表に見つからないスタッフ（月またぎ判定なし）: "
                + "、".join(no_prev))

        d1 = days[0]
        for n in names:
            if is_chief[n]:
                continue
            seq = prev.get(n)
            if not seq:
                continue
            last = seq[-1]                      # 前月末日
            prev2 = seq[-2] if len(seq) >= 2 else None

            # 1) 夜勤明け：前月末が夜勤(▲/●) → 1日は日勤系(ー/外来/出張/ー●)に入れない
            if last in (EVE, NIGHT):
                for st in (DAY, GAI, OFFSITE, DAYNIGHT):
                    if (n, d1, st) in x:
                        mdl.Add(x[(n, d1, st)] == 0)
            # 2) ▲● 禁止：前月末が準夜 → 1日の深夜は不可
            if last == EVE:
                for st in (NIGHT, DAYNIGHT):
                    if (n, d1, st) in x:
                        mdl.Add(x[(n, d1, st)] == 0)
            # 3) 3連続夜勤の禁止：前月末2日が夜勤 → 1日は夜勤不可
            if last in (EVE, NIGHT) and prev2 in (EVE, NIGHT):
                for st in (EVE, NIGHT, DAYNIGHT):
                    if (n, d1, st) in x:
                        mdl.Add(x[(n, d1, st)] == 0)
            # 4) ー●：前月末が日勤 → 1日の深夜は「日勤深夜」に相当（ー●不可者は禁止）
            if last in (DAY, GAI) and (n in no_daynight or n in pre_rest):
                for st in (NIGHT, DAYNIGHT):
                    if (n, d1, st) in x:
                        mdl.Add(x[(n, d1, st)] == 0)
            # 5) 深夜の前は必ず休み（指定者）：前月末が休みでなければ1日の深夜は不可
            if n in pre_rest and last not in (OFF, LEAVE, NIGHT):
                for st in (NIGHT, DAYNIGHT):
                    if (n, d1, st) in x:
                        mdl.Add(x[(n, d1, st)] == 0)
            # 6) 月またぎの連勤：前月末の連勤数 k → 今月は (5-k) 日で必ず休みが必要
            k = 0
            for s_ in reversed(seq):
                if s_ in WORK:
                    k += 1
                else:
                    break
            if k > 0:
                room = max(0, 5 - k)            # 連勤上限5
                span = days[:room + 1]
                rest_terms = []
                for dd in span:
                    rest_terms += has(n, dd, REST)
                if rest_terms:
                    mdl.Add(sum(rest_terms) >= 1)   # この範囲に必ず休みを1つ
                else:
                    pass
            # 7) 日勤の連続4回：前月末の日勤連続数 j → 今月頭の日勤を制限
            j = 0
            for s_ in reversed(seq):
                if s_ in (DAY, GAI):
                    j += 1
                else:
                    break
            if j > 0:
                room = max(0, 4 - j)
                span = days[:room + 1]
                dterms = []
                for dd in span:
                    dterms += has(n, dd, {DAY, GAI})
                if dterms:
                    sd = slack(f"xday_{n}", 400)
                    mdl.Add(sum(dterms) - sd <= room)
            # 8) 5連勤後の2連休（月またぎ）：前月末が5連勤 → 1日(と2日)は休み
            if k >= 5:
                need = 2 - (0)                  # 前月内で休めていないので今月頭に2連休
                for idx in range(min(2, len(days))):
                    rr = has(n, days[idx], REST)
                    if rr:
                        s2 = slack(f"x52_{n}_{idx}", 900)
                        mdl.Add(sum(rr) + s2 >= 1)

    # 並び順・連勤（日ごと隣接）
    dnpat_by_day = {}     # ●の日 -> その日にー●となる人のz変数
    for n in names:
        if is_chief[n]:                           # 師長は勤務固定のため対象外
            continue
        nb = {}                                   # nb[d]=1 ⇔ その日が夜勤(▲/●)
        for d in days:
            h = has(n, d, NIGHTS)
            v = mdl.NewBoolVar(f"nb_{n}_{d}")
            mdl.Add(v == (sum(h) if h else 0))
            nb[d] = v
        for i, d in enumerate(days):
            nd = days[i + 1] if i + 1 < len(days) else None
            nd2 = days[i + 2] if i + 2 < len(days) else None
            night_d = has(n, d, NIGHTS)
            # 【ルール3】夜勤は連続が基本：孤立した単発夜勤に軽いペナルティ
            prev = nb[days[i - 1]] if i > 0 else 0
            nxt = nb[days[i + 1]] if i + 1 < len(days) else 0
            iso = mdl.NewBoolVar(f"iso_{n}_{d}")
            mdl.Add(iso >= nb[d] - prev - nxt)
            pen.append((8, iso))
            if nd is not None:
                # 夜勤の翌日は 日勤系(ー/ー●/外来/出張) にしない（＝明け休みか夜勤のみ）
                for st in (DAY, DAYNIGHT, GAI, OFFSITE):
                    if (n, nd, st) in x:
                        for v in night_d:
                            mdl.Add(v + x[(n, nd, st)] <= 1)
                # ▲→● 禁止（●▲は可）
                for nst in (NIGHT, DAYNIGHT):
                    if (n, d, EVE) in x and (n, nd, nst) in x:
                        mdl.Add(x[(n, d, EVE)] + x[(n, nd, nst)] <= 1)
            # 3連続夜勤禁止（最大2連続）
            if nd2 is not None:
                trip = has(n, d, NIGHTS) + has(n, nd, NIGHTS) + has(n, nd2, NIGHTS)
                if trip:
                    mdl.Add(sum(trip) <= 2)
                rest_nd = has(n, nd, REST)
                deep_nd2 = has(n, nd2, {NIGHT, DAYNIGHT})
                eve_d = [x[(n, d, EVE)]] if (n, d, EVE) in x else []
                eve_nd2 = [x[(n, nd2, EVE)]] if (n, nd2, EVE) in x else []
                deep_d = has(n, d, {NIGHT, DAYNIGHT})
                if rest_nd:
                    # ▲×● : 準夜→単休→深夜 は禁止（ハード）
                    for a in eve_d:
                        for b in rest_nd:
                            for c in deep_nd2:
                                mdl.Add(a + b + c <= 2)
                    # ●×● / ▲×▲ : なるべく避ける（ソフト）
                    for pair_a, pair_c, tag in ((deep_d, deep_nd2, "dxd"),
                                                (eve_d, eve_nd2, "exe")):
                        if not pair_a or not pair_c:
                            continue
                        z = mdl.NewBoolVar(f"{tag}_{n}_{d}")
                        for a in pair_a:
                            mdl.Add(z >= a + sum(rest_nd) + sum(pair_c) - 2)
                        pen.append((120, z))
        # 連勤最大5（6連勤禁止・絶対上限＝ハード制約）
        for i in range(len(days) - 5):
            win = []
            for j in range(6):
                win += has(n, days[i + j], WORK)
            if win:
                mdl.Add(sum(win) <= 5)

        # 【固定ルール】5連勤の後は2連休（5日連続勤務 → 直後の2日は休み）
        for i in range(len(days) - 6):
            seg = [days[i + j] for j in range(5)]
            w5 = mdl.NewBoolVar(f"w5_{n}_{i}")
            wvars = []
            ok = True
            for d5 in seg:
                h = has(n, d5, WORK)
                if not h:
                    ok = False; break
                wv = mdl.NewBoolVar(f"wv_{n}_{d5}")
                mdl.Add(wv == sum(h))
                wvars.append(wv)
            if not ok:
                continue
            for wv in wvars:
                mdl.Add(w5 <= wv)
            mdl.Add(w5 >= sum(wvars) - 4)
            pen.append((25, w5))          # 5連勤自体を軽く抑制（4連勤までを優先）
            # w5=1 のとき、直後2日は休み（強いソフト）
            for k in (5, 6):
                dnx = days[i + k]
                rr = has(n, dnx, REST)
                if rr:
                    s2 = slack(f"r52_{n}_{i}_{k}", 900)
                    mdl.Add(sum(rr) + s2 >= w5)

        # 【ルール4】日勤(ー・外来)は連続4回まで（5連続禁止）
        for i in range(len(days) - 4):
            dwin = []
            for j in range(5):
                dwin += has(n, days[i + j], {DAY, GAI})
            if dwin:
                sd = slack(f"dayrun_{n}_{i}", 400)
                mdl.Add(sum(dwin) - sd <= 4)

        # 【ルール2】日勤深夜(ー●)＝ ー(d) の翌日 ●(d+1)。回数を制限。
        dnpat = []            # DAY(d) かつ NIGHT(d+1) の指示変数
        for i in range(len(days) - 1):
            d, nd = days[i], days[i + 1]
            if (n, d, DAY) in x and (n, nd, NIGHT) in x:
                z = mdl.NewBoolVar(f"dnp_{n}_{d}")
                mdl.Add(z <= x[(n, d, DAY)]); mdl.Add(z <= x[(n, nd, NIGHT)])
                mdl.Add(z >= x[(n, d, DAY)] + x[(n, nd, NIGHT)] - 1)
                dnpat.append((nd, z))
                dnpat_by_day.setdefault(nd, []).append(z)
        if dnpat:
            zs = [z for _, z in dnpat]
            if n in pre_rest or n in no_daynight:
                mdl.Add(sum(zs) <= 1)                 # ー●不可者：原則0（月1回まで）
                for z in zs:
                    pen.append((300, z))
            else:
                mdl.Add(sum(zs) <= 2)                 # 個人 月2回まで（ハード）

        # 【詳細設定】深夜の前は必ず休み（指定スタッフ・ハード）
        # 連続深夜(●●)は許容し、そのブロックに入る前日が休みであることを要求する
        if n in pre_rest:
            for i, d in enumerate(days):
                if (n, d, NIGHT) not in x or i == 0:
                    continue
                pd = days[i - 1]
                # 前日が「休み」または「深夜(連続)」以外なら、この日の深夜を禁止
                for st in STATES:
                    if st in (OFF, LEAVE, NIGHT, DAYNIGHT):
                        continue
                    if (n, pd, st) in x:
                        mdl.Add(x[(n, pd, st)] + x[(n, d, NIGHT)] <= 1)
            for i, d in enumerate(days):              # ー●（日勤の直後の深夜）は不可
                if (n, d, DAYNIGHT) in x:
                    mdl.Add(x[(n, d, DAYNIGHT)] == 0)

    # 【ルール2】ー●（日勤→翌深夜）は1日あたり最大2名（病棟全体・ハード）
    for nd, zs in dnpat_by_day.items():
        if zs:
            mdl.Add(sum(zs) <= 2)

    # 夜勤回数の均等化＋上限（上限は詳細設定で変更可、既定10）
    ncap = data["settings"].get("night_cap", {})
    for n in night_cap:
        nc = mdl.NewIntVar(0, 31, f"nc_{n}")
        ev_terms, dp_terms = [], []
        for d in days:
            if (n, d, EVE) in x:   ev_terms.append(x[(n, d, EVE)])
            if (n, d, NIGHT) in x: dp_terms.append(x[(n, d, NIGHT)])
            if (n, d, DAYNIGHT) in x: dp_terms.append(x[(n, d, DAYNIGHT)])
        terms = ev_terms + dp_terms
        mdl.Add(nc == sum(terms))
        cap = ncap.get(n, ncap.get("_default", 10))
        mdl.Add(nc <= cap)                       # 上限（ハード）
        lo = slack(f"nlo_{n}", 15)
        mdl.Add(nc + lo >= min(8, cap))          # 下限8の目安（上限が8未満なら緩和）

        # 準夜・深夜の配分：両方に入れる人は、どちらにも入れる（偏りを是正）
        if ev_terms and dp_terms:
            ec = mdl.NewIntVar(0, 31, f"ec_{n}")
            dc = mdl.NewIntVar(0, 31, f"dc_{n}")
            mdl.Add(ec == sum(ev_terms))
            mdl.Add(dc == sum(dp_terms))
            # 片方が0になるのを強く回避（最低でも各2回は入る）
            se = slack(f"emin_{n}", 900); sd = slack(f"dmin_{n}", 900)
            mdl.Add(ec + se >= 2)
            mdl.Add(dc + sd >= 2)
            # 準夜と深夜の差を抑える（偏りにペナルティ）
            gap = mdl.NewIntVar(0, 31, f"gap_{n}")
            mdl.Add(gap >= ec - dc)
            mdl.Add(gap >= dc - ec)
            pen.append((60, gap))

    # 【休日数】詳細設定の「最低休日数」を確保（既定＝月の土日祝数）
    rest_cfg = data["settings"].get("rest_days", {})
    W = sum(1 for d in days if dtype[d] in ("sat", "sun") or d in holidays)
    for n in names:
        if is_chief[n]:
            continue
        cfg = rest_cfg.get(n, rest_cfg.get("_default", {}))
        need = cfg.get("min", W)
        include_leave = cfg.get("include_leave", True)
        rvars = [x[(n, d, OFF)] for d in days if (n, d, OFF) in x]
        if include_leave:                      # 年休も休みに数える
            rvars += [x[(n, d, LEAVE)] for d in days if (n, d, LEAVE) in x]
        if not rvars:
            continue
        oc = mdl.NewIntVar(0, 31, f"off_{n}")
        mdl.Add(oc == sum(rvars))
        dlo = slack(f"olo_{n}", 6000)          # 休日数不足は強く penalize（夜勤人数の次に優先）
        mdl.Add(oc + dlo >= need)

    # 【追加】月に最低2回の2連休（5連休以上を取得している場合は免除）ソフト
    for n in names:
        if is_chief[n]:
            continue
        rb = {}
        for d in days:
            h = has(n, d, REST)
            v = mdl.NewBoolVar(f"rb_{n}_{d}")
            mdl.Add(v == (sum(h) if h else 0))
            rb[d] = v
        heads = []
        for i in range(len(days) - 1):
            d0, d1 = days[i], days[i + 1]
            hd = mdl.NewBoolVar(f"hd_{n}_{d0}")
            mdl.Add(hd <= rb[d0]); mdl.Add(hd <= rb[d1])
            if i > 0:
                mdl.Add(hd <= 1 - rb[days[i - 1]])
                mdl.Add(hd >= rb[d0] + rb[d1] - rb[days[i - 1]] - 1)
            else:
                mdl.Add(hd >= rb[d0] + rb[d1] - 1)
            heads.append(hd)
        run5 = []
        for i in range(len(days) - 4):
            seg = [rb[days[i + j]] for j in range(5)]
            b5 = mdl.NewBoolVar(f"r5_{n}_{i}")
            for a in seg:
                mdl.Add(b5 <= a)
            mdl.Add(b5 >= sum(seg) - 4)
            run5.append(b5)
        big = mdl.NewBoolVar(f"has5_{n}")
        if run5:
            mdl.AddMaxEquality(big, run5)
        else:
            mdl.Add(big == 0)
        short = slack(f"twoff_{n}", 30)
        mdl.Add(sum(heads) + 2 * big + short >= 2)

    mdl.Minimize(sum(w * v for w, v in pen))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8
    st = solver.Solve(mdl)
    status = solver.StatusName(st)

    assign = {}
    if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for n in names:
            for d in days:
                for s2 in allowed[(n, d)]:
                    if solver.Value(x[(n, d, s2)]) == 1:
                        assign[(n, d)] = s2
                        break
        for (n, d) in soft_night_wish:
            if assign.get((n, d)) != NIGHT:
                warnings.append(f"D{d} {n}:●希望はフェーズ未解禁のため休みに変更")
        # 日勤5連続（ルール4超過）を警告（師長は対象外）
        for n in names:
            if is_chief[n]:
                continue
            run = 0
            for d in days:
                run = run + 1 if assign.get((n, d)) in (DAY, GAI) else 0
                if run == 5:
                    warnings.append(f"D{d} {n}:日勤が5連続（4連続上限の超過）")
    return dict(status=status, obj=solver.ObjectiveValue() if assign else None,
                assign=assign, data=data, names=names, lvl=lvl,
                warnings=warnings, gairai_cells=gairai_cells)


if __name__ == "__main__":
    r = solve("/home/claude/希望届_2026年08月.xlsx", holidays={11}, time_limit=45)
    print("status:", r["status"], "obj:", r["obj"])
    print("warnings:", r["warnings"][:10])
