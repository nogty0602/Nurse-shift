"""第1段階 CP-SAT ソルバー本体."""
from ortools.sat.python import cp_model
from shift_core import (parse, MASTER, OFF, LEAVE, DAY, EVE, NIGHT, OFFSITE,
                        GAI, DAYNIGHT, FIXED, ALLOWED, DAY_REQ, EVE_REQ, NIGHT_REQ, DOW_FRI_MON)

STATES = [OFF, LEAVE, DAY, EVE, NIGHT, OFFSITE, GAI, DAYNIGHT]
WORK = {DAY, EVE, NIGHT, OFFSITE, GAI, DAYNIGHT}
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


def solve(path, holidays, time_limit=60):
    data = parse(path, holidays)
    days, dtype, dow = data["days"], data["dtype"], data["dow"]
    staff = data["staff"]
    names = [s["name"] for s in staff]
    lvl = {s["name"]: s["level"] for s in staff}
    idx = {n: i for i, n in enumerate(names)}
    warnings = []
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
    gairai = data["settings"].get("gairai", [])
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
            # 空欄で、深夜可能かつ日勤可能なら「日勤深夜(ー●)」も選択肢に（Lv2以上）
            if sym == "" and NIGHT in al and DAY in al and s["level"] >= 2:
                al = al | {DAYNIGHT}
            allowed[(n, d)] = al
            if w:
                warnings.append(f"D{d} {w}")
            if n in phase_soft and sym == "●":
                soft_night_wish.append((n, d))
            if n in phase_names and n not in phase_soft and sym == "●":
                honored_wish.add((n, d))       # 希望優先○：フェーズ制約をこの日はかけない

    # 外来割当：担当者を外来日にGAI固定（衝突する固定希望があればスキップ）
    gairai_cells = {}
    for e in gairai:
        n = e.get("staff")
        if not n or n not in names:
            continue
        for d in gairai_match_days(days, dow, e):
            wish = cells_of.get(n, {}).get(d, "")
            if wish in ("×", "●", "▲", "年", "出", "ー●"):
                warnings.append(f"D{d} {n}:外来希望と本人希望({wish})が衝突→本人希望を優先")
                continue
            allowed[(n, d)] = {GAI}
            gairai_cells[(n, d)] = e["symbol"]

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

    def day_req_half(d):
        """日勤の必要人数を半単位(FTE×2)で返す (下限)。"""
        if dtype[d] == "sat": return 16
        if dtype[d] == "sun": return 14
        if dow[d] == "金": return 18          # 9名
        if dow[d] == "火": return 21          # 10.5名(外来0.5含む)
        return 20                             # 10名

    def deep_vars(n, d):
        return [x[(n, d, st)] for st in (NIGHT, DAYNIGHT) if (n, d, st) in x]

    for d in days:
        # --- 日勤(半単位: ー・ー●=2, 外来=1) ---
        lo = day_req_half(d)
        day_terms = []
        for n in names:
            if is_chief[n]:
                continue
            for st, coef in ((DAY, 2), (DAYNIGHT, 2), (GAI, 1)):
                if (n, d, st) in x:
                    day_terms.append(coef * x[(n, d, st)])
        sh = slack(f"dsh{d}", 1000); ov = slack(f"dov{d}", 2)
        mdl.Add(sum(day_terms) + sh >= lo)
        if dtype[d] == "wd" and dow[d] == "火":
            mdl.Add(sum(day_terms) <= 23)     # 火 上限11.5
        mdl.Add(sum(day_terms) - ov <= lo)    # 目標loに寄せる（超過は軽ペナルティ）

        # --- 準夜(EVE) 3名 ---
        ev = [x[(n, d, EVE)] for n in names if (n, d, EVE) in x]
        se = slack(f"esh{d}", 1000)
        mdl.Add(sum(ev) <= 3); mdl.Add(sum(ev) + se >= 3)
        # --- 深夜(deep = ● + ー●) 3名 ---
        deepall = [v for n in names for v in deep_vars(n, d)]
        sn = slack(f"nsh{d}", 1000)
        mdl.Add(sum(deepall) <= 3); mdl.Add(sum(deepall) + sn >= 3)

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

    # 並び順・連勤（日ごと隣接）
    for n in names:
        for i, d in enumerate(days):
            nd = days[i + 1] if i + 1 < len(days) else None
            nd2 = days[i + 2] if i + 2 < len(days) else None
            night_d = has(n, d, NIGHTS)
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
                # ●×● / ▲×● : 夜勤→単休→深夜 禁止
                deep_nd2 = has(n, nd2, {NIGHT, DAYNIGHT})
                if night_d and deep_nd2:
                    rest_nd = has(n, nd, REST)
                    if rest_nd:
                        for a in night_d:
                            for b in rest_nd:
                                for c in deep_nd2:
                                    mdl.Add(a + b + c <= 2)
        # 連勤最大5（6連勤禁止・絶対上限＝ハード制約）
        for i in range(len(days) - 5):
            win = []
            for j in range(6):
                win += has(n, days[i + j], WORK)
            if win:
                mdl.Add(sum(win) <= 5)

        # 日勤深夜(ー●) 個人上限：可＝月2回、不可＝月1回まで（強く penalize）
        dn = [x[(n, d, DAYNIGHT)] for d in days if (n, d, DAYNIGHT) in x]
        if dn:
            if n in no_daynight:
                mdl.Add(sum(dn) <= 1)
                for v in dn:
                    pen.append((300, v))       # ー●不可者はほぼ0に
            else:
                mdl.Add(sum(dn) <= 2)
        # ー●不可者の単独深夜(●)は前日を休みにする
        if n in no_daynight:
            for i, d in enumerate(days):
                if i == 0 or (n, d, NIGHT) not in x:
                    continue
                pv = has(n, days[i - 1], REST)
                if pv:
                    mdl.Add(x[(n, d, NIGHT)] <= sum(pv))
                else:
                    mdl.Add(x[(n, d, NIGHT)] == 0)

    # 日勤深夜(ー●) は1日あたり最大2名（病棟全体）
    for d in days:
        dnd = [x[(n, d, DAYNIGHT)] for n in names if (n, d, DAYNIGHT) in x]
        if dnd:
            mdl.Add(sum(dnd) <= 2)

    # 夜勤回数の均等化（8〜10, 対象=night_cap）
    for n in night_cap:
        nc = mdl.NewIntVar(0, 31, f"nc_{n}")
        terms = []
        for d in days:
            if (n, d, EVE) in x:   terms.append(x[(n, d, EVE)])
            if (n, d, NIGHT) in x: terms.append(x[(n, d, NIGHT)])
            if (n, d, DAYNIGHT) in x: terms.append(x[(n, d, DAYNIGHT)])
        mdl.Add(nc == sum(terms))
        hi = slack(f"nhi_{n}", 20); lo = slack(f"nlo_{n}", 15)
        mdl.Add(nc - hi <= 10); mdl.Add(nc + lo >= 8)

    # 【項目6】休み数基準：週休は月内に消費（各人 休(OFF) ≧ 月の土日祝数 を目安）。
    # 夜勤明けの休みは並び順ルールで自動付与されるため、ここでは週休の下限のみをソフトで確保。
    W = sum(1 for d in days if dtype[d] in ("sat", "sun"))
    for n in names:
        if is_chief[n]:
            continue
        offs = [x[(n, d, OFF)] for d in days if (n, d, OFF) in x]
        if not offs:
            continue
        oc = mdl.NewIntVar(0, 31, f"off_{n}")
        mdl.Add(oc == sum(offs))
        dlo = slack(f"olo_{n}", 8)          # 週休が土日祝数を下回るぶんを軽く penalize
        mdl.Add(oc + dlo >= W)

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
    return dict(status=status, obj=solver.ObjectiveValue() if assign else None,
                assign=assign, data=data, names=names, lvl=lvl,
                warnings=warnings, gairai_cells=gairai_cells)


if __name__ == "__main__":
    r = solve("/home/claude/希望届_2026年08月.xlsx", holidays={11}, time_limit=45)
    print("status:", r["status"], "obj:", r["obj"])
    print("warnings:", r["warnings"][:10])
