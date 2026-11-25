"""作成前に「人手が足りない日」を見つける。

希望休・出張の数と、その日に必要な人数を突き合わせて、
どう組んでも埋まらない日を先に知らせる（応援の手配に使う）。
"""
from shift_core import parse, resolve_req, resolve_support


def preflight(path, holidays):
    """(不足日のリスト, 全スタッフ数) を返す。
    不足日: dict(day, dow, need, avail, short)
    """
    data = parse(path, set(holidays))
    days, dow, dtype = data["days"], data["dow"], data["dtype"]
    staff = data["staff"]
    settings = data["settings"]
    hc = settings.get("headcount", [])
    sup = settings.get("support", [])

    workers = [s for s in staff if s.get("emp") != "師長"]
    W = len(workers)

    # その日に動けない人（本人の希望休・年休・出張）
    def unavailable(d):
        c = 0
        for s in workers:
            sym = str(s["cells"].get(d, "") or "").strip()
            if sym in ("×", "年", "出"):
                c += 1
        return c

    req = {d: resolve_req(hc, d, dow[d], dtype[d], d in holidays) for d in days}
    spt = {d: resolve_support(sup, d, dow[d], dtype[d], d in holidays) for d in days}

    def night_need(d):
        return req[d]["eve"][0] + req[d]["nig"][0] - spt[d]["eve"] - spt[d]["nig"]

    issues = []
    for i, d in enumerate(days):
        need = (req[d]["day"][0] - spt[d]["day"]) + night_need(d)
        avail = W - unavailable(d)
        # 前日に夜勤だった人のうち、当日の夜勤に回れない人は必ず休みになる
        if i > 0:
            forced = max(0, night_need(days[i - 1]) - night_need(d))
            avail -= forced
        if avail < need - 1e-6:
            issues.append(dict(day=d, dow=dow[d], need=need, avail=avail,
                               short=need - avail))
    return issues, W


def shortfall_advice(issues):
    """不足日から、そのまま入力欄に貼れる条件文を作る。"""
    lines = []
    for it in issues:
        n = it["short"]
        n = int(n) if float(n) == int(n) else round(n + 0.49)
        lines.append(f"{it['day']}日に応援{max(1, int(n))}人")
    return lines
