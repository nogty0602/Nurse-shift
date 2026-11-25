"""看護師シフト自動作成 — 対話版

希望届を読み込んだあとは、画面下の入力欄に日本語で条件を書くだけ。
基本ルール（希望休厳守・連勤・夜勤の並びなど）は固定で、
月ごとに変わる条件だけを会話で足していく。
"""
import os
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import re
import sys
import json
import pickle
import calendar
import datetime
import tempfile
import subprocess

import pandas as pd
import streamlit as st
from openpyxl import load_workbook

from settings_io import (settings_to_rows, write_settings_sheet,
                         save_settings_file, load_settings_file)
from shift_core import (parse_settings, STATE_SYMBOL, OFF, LEAVE, DAY, EVE,
                        NIGHT, DAYNIGHT, GAI)
from condition_parse import (parse_conditions, apply_conditions,
                             suggest_phrasing)
from feasibility import preflight, shortfall_advice

try:
    import jpholiday
    HAS_JP = True
except ImportError:
    HAS_JP = False

st.set_page_config(page_title="看護師シフト自動作成", layout="wide")

CELL_COLOR = {
    "●": "#8DB4E2", "▲": "#B7DDE8", "ー": "#FFFFFF", "P": "#FFF2CC",
    "×": "#E2E2E2", "年": "#D9D2E9", "出": "#D6E4BC",
    "外": "#D6BFA8", "G/-": "#D6BFA8", "-/G": "#D6BFA8",
    "ーイ": "#FDE9D9", "研": "#F4E1F0", "ケ/-": "#F4E1F0", "-/2": "#F4E1F0",
    "": "#FFFFFF",
}
WHITE_TEXT = {"●"}

EXAMPLES = [
    "水曜の日勤は9人",
    "18日と19日の深夜は4人",
    "Kの夜勤は6回まで",
    "5日と12日に応援2人",
    "休みは11日以上",
    "Tは土日だけ準夜",
    "Bは深夜の前は必ず休み",
    "Aの5日は休みにして",
]


def _full(fn):
    """横幅いっぱいの指定（Streamlitの新旧どちらでも動くように）。"""
    try:
        import inspect
        if "width" in inspect.signature(fn).parameters:
            return {"width": "stretch"}
    except (TypeError, ValueError):
        pass
    return {"use_container_width": True}

# ------------------------------------------------------------------ state
ss = st.session_state
ss.setdefault("conditions", [])
ss.setdefault("messages", [])
ss.setdefault("result", None)
ss.setdefault("greeted_for", None)


def say(role, text):
    ss.messages.append({"role": role, "text": text})


def auto_holidays(year, month):
    if not HAS_JP:
        return []
    n = calendar.monthrange(year, month)[1]
    return [d for d in range(1, n + 1)
            if jpholiday.is_holiday(datetime.date(year, month, d))]


def parse_year_month(wb):
    try:
        ws = wb["希望届"] if "希望届" in wb.sheetnames else wb.active
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", str(ws.cell(1, 1).value or ""))
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    t = datetime.date.today()
    return t.year, t.month


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("入力")
    up = st.file_uploader("希望届 (.xlsx)　※必須", type=["xlsx"])
    prev_up = st.file_uploader("前月の勤務表 (.xlsx・任意)", type=["xlsx"],
                               help="月をまたぐ連勤や夜勤明けを正しく判定します。")
    set_up = st.file_uploader("前回の設定ファイル (.xlsx・任意)", type=["xlsx"],
                              help="前回と同じ条件から始めたいときに読み込みます。")
    time_limit = st.slider("計算時間の上限（秒）", 30, 360, 180, step=30,
                           help="条件が厳しい月は長め（240〜360秒）にしてください。")

if up is None:
    st.title("看護師シフト自動作成")
    st.info("左のサイドバーから **希望届（.xlsx）** を読み込んでください。\n\n"
            "読み込んだあとは、画面下の入力欄に日本語で条件を書くだけでシフトを作成します。")
    st.markdown("#### 入力のしかた（例）")
    st.code("\n".join(EXAMPLES), language=None)
    st.stop()

# ------------------------------------------------------------------ 読み込み
tmp_in = os.path.join(tempfile.gettempdir(), "wish_upload.xlsx")
with open(tmp_in, "wb") as f:
    f.write(up.getbuffer())
wb = load_workbook(tmp_in)

staff_names = set()
if "希望届" in wb.sheetnames:
    ws = wb["希望届"]
    for r in range(4, ws.max_row + 1):
        v = ws.cell(r, 1).value
        if v not in (None, ""):
            staff_names.add(str(v).strip())

year, month = parse_year_month(wb)
ndays = calendar.monthrange(year, month)[1]
base_holidays = set(auto_holidays(year, month))

# 前回の設定ファイルがあればそれを土台にする
if set_up is not None:
    sp = os.path.join(tempfile.gettempdir(), "settings_upload.xlsx")
    with open(sp, "wb") as f:
        f.write(set_up.getbuffer())
    try:
        base_rows = load_settings_file(sp, staff_names)
    except Exception:
        base_rows = settings_to_rows(parse_settings(wb, staff_names))
else:
    base_rows = settings_to_rows(parse_settings(wb, staff_names))

# 最初のあいさつ（ファイルが変わったら作り直す）
key = (up.name, year, month, len(staff_names))
if ss.greeted_for != key:
    ss.greeted_for = key
    ss.messages = []
    ss.result = None
    hol_txt = ("、".join(f"{d}日" for d in sorted(base_holidays))
               if base_holidays else "なし")
    say("assistant",
        f"**{year}年{month}月**の希望届を読み込みました（スタッフ {len(staff_names)}名）。\n\n"
        f"祝日は自動判定で **{hol_txt}** です。\n\n"
        "この月だけの条件があれば、下の入力欄に書いてください。"
        "なければ、そのまま「この条件でシフトを作成」を押せます。")

# ------------------------------------------------------------------ 画面
st.title("看護師シフト自動作成")

with st.expander("いつも守るルール（変更不要）", expanded=False):
    st.markdown(
        "- 希望休（×・年休）は必ず守ります\n"
        "- 休み希望の前日は準夜なし／翌日は深夜なし\n"
        "- 連勤は5日まで。5連勤の後は2連休\n"
        "- 夜勤は2回続けて組む（●● ▲▲ ●▲／▲●は不可）\n"
        "- 夜勤明けの翌日は日勤に入れない\n"
        "- 日勤の連続は4回まで\n"
        "- 外来は1日1名・0.5人換算・祝日はなし\n"
        "- 準夜と深夜は、どちらも可能な人には両方を配分\n"
        "- リーダー・サポート・チームの人数バランス")

# 条件を設定表に反映（事前チェックと生成の両方で使う）
rows = {k: [list(r) for r in v] for k, v in base_rows.items()}
rows, adjust, holidays = apply_conditions(rows, ss.conditions, base_holidays)

# 作成前に「どう組んでも足りない日」を調べる
pre_issues = []
try:
    pre_path = os.path.join(tempfile.gettempdir(), "preflight.xlsx")
    wb_pre = load_workbook(tmp_in)
    write_settings_sheet(wb_pre, rows)
    wb_pre.save(pre_path)
    pre_issues, n_staff = preflight(pre_path, holidays)
except Exception:
    pre_issues = []

col_chat, col_cond = st.columns([3, 2])

with col_cond:
    if pre_issues:
        st.warning(
            "**人手が足りない見込みの日があります**\n\n"
            + "\n".join(f"- {it['day']}日（{it['dow']}）"
                        f"あと{it['short']:g}名"
                        for it in pre_issues)
            + "\n\n希望休と必要人数から計算したものです。"
            "応援を手配するか、その日の必要人数を下げてください。")
        st.caption("そのまま入力欄に貼れます ↓")
        st.code("\n".join(shortfall_advice(pre_issues)), language=None)

    st.subheader(f"この月の条件（{len(ss.conditions)}件）")
    if not ss.conditions:
        st.caption("まだありません。下の入力欄から追加できます。")
    for i, c in enumerate(list(ss.conditions)):
        c1, c2 = st.columns([8, 1])
        c1.markdown(f"・{c['label']}")
        if c2.button("✕", key=f"del{i}", help="この条件を削除"):
            ss.conditions.pop(i)
            st.rerun()
    if ss.conditions and st.button("すべて削除", **_full(st.button)):
        ss.conditions = []
        st.rerun()

    with st.expander("いま効いている設定を確認する"):
        VIEW = [("headcount", "必要人数"), ("support", "応援"),
                ("night_cap", "夜勤上限"), ("rest", "休日数"),
                ("cond", "個人の勤務条件"), ("gairai", "外来"),
                ("pre_rest", "深夜の前は休み"), ("no_dn", "日勤→深夜 不可")]
        from settings_io import TABLE_DEFS
        for key, nm in VIEW:
            body = [r for r in rows.get(key, []) if any(str(c).strip() for c in r)]
            if not body:
                continue
            cols = TABLE_DEFS[key][2]
            df = pd.DataFrame([list(r) + [""] * (len(cols) - len(r)) for r in body],
                              columns=cols)
            st.caption(nm)
            st.dataframe(df, hide_index=True, **_full(st.dataframe))

    st.divider()
    run = st.button("この条件でシフトを作成", type="primary",
                    **_full(st.button))

with col_chat:
    for m in ss.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["text"])

prompt = st.chat_input("条件を入力（例：水曜の日勤は9人 / Kの夜勤は6回まで）")
if prompt:
    say("user", prompt)
    conds, unknown = parse_conditions(prompt, staff_names, ndays)
    lines = []
    if conds:
        ss.conditions.extend(conds)
        lines.append("次のように受け取りました。")
        lines += [f"- {c['label']}" for c in conds]
    if unknown:
        lines.append("")
        lines.append("**次の部分は読み取れませんでした。**")
        for u in unknown:
            lines.append(f"- 「{u}」")
            lines.append(f"  → {suggest_phrasing(u, staff_names)}")
    say("assistant", "\n".join(lines))
    st.rerun()

# ------------------------------------------------------------------ 生成
if run:
    write_settings_sheet(wb, rows)
    wb.save(tmp_in)

    tmp = tempfile.gettempdir()
    out_xlsx = os.path.join(tmp, "schedule_out.xlsx")
    result_pkl = os.path.join(tmp, "result.pkl")
    prev_path = "-"
    if prev_up is not None:
        prev_path = os.path.join(tmp, "prev_schedule.xlsx")
        with open(prev_path, "wb") as f:
            f.write(prev_up.getbuffer())
    adj_path = "-"
    if adjust:
        adj_path = os.path.join(tmp, "adjust.json")
        with open(adj_path, "w", encoding="utf-8") as f:
            json.dump(adjust, f, ensure_ascii=False)

    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_solver.py")
    env = dict(os.environ)
    env["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
    hol_str = ",".join(str(d) for d in sorted(holidays))

    with st.spinner("シフトを作成しています…（最大 %d 秒）" % time_limit):
        proc = subprocess.run(
            [sys.executable, worker, tmp_in, hol_str, str(time_limit),
             out_xlsx, result_pkl, prev_path, adj_path],
            env=env, capture_output=True, text=True,
            timeout=int(time_limit) + 300)

    if proc.returncode != 0:
        say("assistant", "計算が途中で止まってしまいました。"
                         "計算時間を短くするか、条件を減らしてお試しください。")
        ss.result = None
    else:
        with open(result_pkl, "rb") as f:
            p = pickle.load(f)
        if not p.get("has_output"):
            say("assistant",
                "**条件が厳しすぎて、すべてを満たす勤務表が作れませんでした。**\n\n"
                "次のどれかをお試しください。\n"
                "- 人数の条件をゆるめる（例：水曜の日勤は8人）\n"
                "- 休みの日数を減らす（例：休みは10日以上）\n"
                "- 応援の人数を増やす（例：5日に応援2人）\n"
                "- 右の条件一覧から、いくつか削除する")
            ss.result = None
        else:
            with open(out_xlsx, "rb") as f:
                sched_bytes = f.read()
            set_out = os.path.join(tmp, "settings_out.xlsx")
            save_settings_file(rows, set_out)
            with open(set_out, "rb") as f:
                set_bytes = f.read()
            ss.result = {"p": p, "sched": sched_bytes, "settings": set_bytes,
                         "ym": (year, month)}
            say("assistant", "勤務表ができました。下に表示しています。")
    st.rerun()

# ------------------------------------------------------------------ 結果
if ss.result:
    p = ss.result["p"]
    days, dow = p["days"], p["dow"]
    names, lvl, staff = p["names"], p["lvl"], p["staff"]
    A, GC = p["assign"], p["gairai"]
    sup = p.get("support", {})
    req = p.get("req", {})

    def disp(n, d):
        stt = A.get(f"{n}|{d}", OFF)
        if stt == GAI:
            return GC.get(f"{n}|{d}", "外")
        if stt == DAY and staff[n].get("tanshuku"):
            return "P"
        return STATE_SYMBOL.get(stt, "")

    st.divider()
    st.subheader("できあがった勤務表")

    # --- 気になる点（不足の明示）---
    issues = []
    short_days = {}
    half = {"ー": 2, "ー●": 2, "P": 2, "-/2": 2, "G/-": 1, "-/G": 1, "外": 1,
            "ーイ": 1, "ケ/-": 1, "研": 0}
    for d in days:
        syms = [disp(n, d) for n in names if staff[n].get("emp") != "師長"]
        day_fte = sum(half.get(s, 0) for s in syms) / 2 + sup.get(d, {}).get("day", 0)
        eve = sum(1 for s in syms if s == "▲") + sup.get(d, {}).get("eve", 0)
        nig = sum(1 for s in syms if s in ("●", "ー●")) + sup.get(d, {}).get("nig", 0)
        r = req.get(d)
        if not r:
            continue
        lack = 0.0
        if day_fte < r["day"][0] - 1e-6:
            issues.append(f"{d}日（{dow[d]}）日勤が {day_fte:g}人（必要 {r['day'][0]:g}人）")
            lack += r["day"][0] - day_fte
        if eve < r["eve"][0] - 1e-6:
            issues.append(f"{d}日（{dow[d]}）準夜が {eve:g}人（必要 {r['eve'][0]:g}人）")
            lack += r["eve"][0] - eve
        if nig < r["nig"][0] - 1e-6:
            issues.append(f"{d}日（{dow[d]}）深夜が {nig:g}人（必要 {r['nig'][0]:g}人）")
            lack += r["nig"][0] - nig
        if lack > 0:
            short_days[d] = max(1, int(lack + 0.49))

    if issues:
        st.error("**人数が足りていない日があります**\n\n"
                 + "\n".join(f"- {s}" for s in issues)
                 + "\n\n応援を足すか、その日の必要人数を下げると解消できます。")
        sug = sorted({f"{d}日に応援{c}人" for d, c in short_days.items()})
        if sug:
            st.caption("そのまま入力欄に貼れます ↓")
            st.code("\n".join(sug), language=None)
    else:
        st.success("必要人数はすべての日で満たしています。")

    grid = pd.DataFrame(
        [[disp(n, d) for d in days] for n in names],
        index=[f"{n} (Lv{lvl[n]}/{staff[n].get('team') or '-'})" for n in names],
        columns=[f"{d}\n{dow[d]}" for d in days])

    def color(v):
        bg = CELL_COLOR.get(v, "#FFFFFF")
        fg = "white" if v in WHITE_TEXT else "black"
        return f"background-color: {bg}; color: {fg}; text-align:center;"

    styler = grid.style
    styler = styler.map(color) if hasattr(styler, "map") else styler.applymap(color)
    st.dataframe(styler, height=560, **_full(st.dataframe))
    st.caption("● 深夜 / ▲ 準夜 / ー 日勤 / P 時短 / G/-・-/G 外来 / ーイ 委員会 / "
               "研・ケ/-・-/2 研修 / × 休 / 年 年休 / 出 出張")

    if p.get("warnings"):
        with st.expander(f"注記・警告（{len(p['warnings'])}件）"):
            for w in p["warnings"]:
                st.write("・" + w)

    y, m = ss.result["ym"]
    MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    c1, c2 = st.columns(2)
    c1.download_button("勤務表(Excel)をダウンロード", ss.result["sched"],
                       file_name=f"勤務表_{y}年{m:02d}月.xlsx", mime=MIME,
                       key="dl_s", **_full(st.download_button))
    c2.download_button("この条件を保存（次回用）", ss.result["settings"],
                       file_name=f"設定_{y}年{m:02d}月.xlsx", mime=MIME,
                       key="dl_c", **_full(st.download_button))
    st.caption("直したいところがあれば、下の入力欄に書いてもう一度作成できます。"
               "（例：「Cは5連勤後に2連休」「12日に応援1人」）")
