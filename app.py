"""看護師シフト自動作成 — Streamlit アプリ（詳細設定を画面で編集）.

使い方:
    pip install -r requirements.txt
    streamlit run app.py
"""
import os
import tempfile
import pandas as pd
import streamlit as st
from openpyxl import load_workbook

from solve import solve
from export import export
from settings_io import settings_to_rows, write_settings_sheet, TABLE_DEFS, TABLE_ORDER
from shift_core import (parse_settings, STATE_SYMBOL, OFF, DAY, EVE, NIGHT,
                        DAYNIGHT, GAI)

CELL_COLOR = {
    "●": "#1F4E78", "ー●": "#8DB4E2", "▲": "#E8A33D", "ー": "#E2EFDA",
    "P": "#E2EFDA", "×": "#D9D9D9", "年": "#FFF2CC", "出": "#DDEBF7",
    "外": "#D6BFA8", "G/-": "#D6BFA8", "-/G": "#D6BFA8", "": "#FFFFFF",
}
WHITE_TEXT = {"●", "ー●"}

st.set_page_config(page_title="看護師シフト自動作成", layout="wide")
st.title("看護師シフト自動作成")

with st.sidebar:
    st.header("入力")
    up = st.file_uploader("希望届 (.xlsx)", type=["xlsx"])
    holidays_txt = st.text_input("祝日(日にちをカンマ区切り)", value="11")
    time_limit = st.slider("計算時間の上限(秒)", 15, 180, 60, step=15)
    run = st.button("シフトを生成", type="primary", use_container_width=True)

if up is None:
    st.info("左のサイドバーで希望届(.xlsx)をアップロードしてください。"
            "『希望届』シートを含むファイルなら、詳細設定は下の画面で編集できます。")
    st.stop()

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
init_rows = settings_to_rows(parse_settings(wb, staff_names))

st.subheader("詳細設定")
st.caption("各表を直接編集できます（行の追加・削除も可）。編集後に左の「シフトを生成」を押してください。")

COND_OPTS = ["", "不可", "月火水木金", "金土日月", "土日", "除土", "除日", "除土日"]
COLCONF = {
    "roles": {"サポート": st.column_config.SelectboxColumn(
                  options=["", "サポート必須", "サポート業務可"]),
              "リーダー": st.column_config.SelectboxColumn(options=["可能", "不可"]),
              "師長": st.column_config.SelectboxColumn(options=["", "○"])},
    "cond": {"準夜(▲)": st.column_config.SelectboxColumn(options=COND_OPTS,
                 help="不可＝そのシフト禁止 / 空欄＝制限なし / 曜日＝その曜日のみ・除○＝その曜日以外"),
             "深夜(●)": st.column_config.SelectboxColumn(options=COND_OPTS),
             "日勤(ー)": st.column_config.SelectboxColumn(options=COND_OPTS)},
    "gairai": {"曜日": st.column_config.SelectboxColumn(options=["月", "火", "水", "木", "金"]),
               "時間帯": st.column_config.SelectboxColumn(options=["午前", "午後"]),
               "対象週": st.column_config.TextColumn(help="毎週 / 第2・第4 など")},
    "exp": {"希望優先": st.column_config.SelectboxColumn(options=["", "○"])},
    "overlap": {"モード": st.column_config.SelectboxColumn(options=["", "厳守"])},
}
LABELS = {"roles": "① 役割設定", "overlap": "② 夜勤 同時不可グループ",
          "cond": "③ 個人の勤務条件", "phase": "④ 夜勤フェーズ定義",
          "exp": "⑤ レベル1 深夜経験回数", "gairai": "⑥ 外来割当",
          "no_dn": "⑦ 日勤深夜(ー●)不可", "headcount": "⑧ 必要人数(下限/上限)",
          "night_cap": "⑨ 夜勤上限(1人あたり月)"}

edited = {}
for key in TABLE_ORDER:
    title, note, cols, _ = TABLE_DEFS[key]
    with st.expander(LABELS[key], expanded=(key in ("roles", "gairai", "no_dn"))):
        st.caption(note)
        if key == "cond":
            st.markdown(
                "**選択肢の意味** ＝ 空欄:制限なし / 不可:そのシフト禁止 / "
                "曜日を並べたもの(例 金土日月・土日):**その曜日だけ可** / "
                "『除○』(例 除土・除日):**その曜日以外は可**\n\n"
                "例) 土曜は深夜のみ → 深夜=`除日`, 日勤=`除土`, 準夜=`不可`")
        df = pd.DataFrame(init_rows.get(key, []), columns=cols)
        ed = st.data_editor(df, num_rows="dynamic", use_container_width=True,
                            key=f"ed_{key}", column_config=COLCONF.get(key, {}))
        edited[key] = ed.values.tolist()

if run:
    try:
        holidays = {int(x) for x in holidays_txt.replace("，", ",").split(",") if x.strip().isdigit()}
    except ValueError:
        holidays = set()

    write_settings_sheet(wb, edited)
    wb.save(tmp_in)

    with st.spinner("シフトを計算中…"):
        r = solve(tmp_in, holidays, time_limit=time_limit)

    if not r["assign"]:
        st.error(f"解が見つかりませんでした (status: {r['status']})。制約が厳しすぎる可能性があります。")
        st.stop()
    st.success(f"生成完了 (status: {r['status']})")

    days = r["data"]["days"]; dow = r["data"]["dow"]; names = r["names"]; lvl = r["lvl"]
    staff = {s["name"]: s for s in r["data"]["staff"]}

    def disp(n, d):
        stt = r["assign"].get((n, d), OFF)
        if stt == GAI:
            return r["gairai_cells"].get((n, d), "外")
        if stt == DAY and staff[n].get("tanshuku"):
            return "P"
        return STATE_SYMBOL.get(stt, "")

    grid = pd.DataFrame(
        [[disp(n, d) for d in days] for n in names],
        index=[f"{n} (Lv{lvl[n]}/{staff[n].get('team') or '-'})" for n in names],
        columns=[f"{d}\n{dow[d]}" for d in days])

    def color(v):
        bg = CELL_COLOR.get(v, "#FFFFFF")
        fg = "white" if v in WHITE_TEXT else "black"
        return f"background-color: {bg}; color: {fg}; text-align:center;"

    st.subheader("勤務表")
    styler = grid.style
    styler = styler.map(color) if hasattr(styler, "map") else styler.applymap(color)
    st.dataframe(styler, use_container_width=True, height=560)
    st.markdown("**凡例** ● 深夜 / ー● 日勤深夜 / ▲ 準夜 / ー 日勤 / P 時短 / "
                "G/-・-/G 外来 / × 休 / 年 年休 / 出 出張")

    def cnt(d, states):
        return sum(1 for n in names if r["assign"].get((n, d)) in states
                   and staff[n].get("emp") != "師長")
    summ = pd.DataFrame({
        "日勤(実働)": [cnt(d, {DAY, DAYNIGHT}) for d in days],
        "準夜": [sum(1 for n in names if r["assign"].get((n, d)) == EVE) for d in days],
        "深夜": [sum(1 for n in names if r["assign"].get((n, d)) in {NIGHT, DAYNIGHT}) for d in days],
        "外来": [sum(1 for n in names if r["assign"].get((n, d)) == GAI) for d in days],
    }, index=[f"{d}({dow[d]})" for d in days]).T
    st.subheader("日別人数")
    st.dataframe(summ, use_container_width=True)

    if r["warnings"]:
        with st.expander(f"警告 ({len(r['warnings'])}件)"):
            for w in r["warnings"]:
                st.write("・" + w)

    tmp_out = os.path.join(tempfile.gettempdir(), "schedule_out.xlsx")
    export(tmp_in, holidays, tmp_out)
    with open(tmp_out, "rb") as f:
        st.download_button("勤務表(Excel)をダウンロード", f.read(),
                           file_name="勤務表.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
