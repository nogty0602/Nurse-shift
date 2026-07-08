"""看護師シフト自動作成 — Streamlit アプリ.

使い方:
    pip install streamlit ortools openpyxl pandas
    streamlit run app.py

希望届(.xlsx。『希望届』シート＋『詳細設定』シートを含むもの)をアップロードして生成します。
"""
import io
import tempfile
import os
import pandas as pd
import streamlit as st

from solve import solve
from export import export
from shift_core import (STATE_SYMBOL, OFF, LEAVE, DAY, EVE, NIGHT, OFFSITE, GAI, DAYNIGHT)

# 記号 → セル背景色
CELL_COLOR = {
    "●": "#1F4E78", "ー●": "#8DB4E2", "▲": "#E8A33D", "ー": "#E2EFDA",
    "P": "#E2EFDA", "×": "#D9D9D9", "年": "#FFF2CC", "出": "#DDEBF7",
    "外": "#D6BFA8", "G/-": "#D6BFA8", "-/G": "#D6BFA8", "": "#FFFFFF",
}
WHITE_TEXT = {"●", "ー●"}

st.set_page_config(page_title="看護師シフト自動作成", layout="wide")
st.title("看護師シフト自動作成")
st.caption("希望届(Excel)をアップロードして、三交代シフトを自動生成します。")

with st.sidebar:
    st.header("設定")
    up = st.file_uploader("希望届 (.xlsx)", type=["xlsx"])
    holidays_txt = st.text_input("祝日(日にちをカンマ区切り)", value="11",
                                 help="例: 11 は8/11(山の日)。複数は 11,15 のように。")
    time_limit = st.slider("計算時間の上限(秒)", 15, 180, 60, step=15)
    run = st.button("シフトを生成", type="primary", use_container_width=True)


def disp_symbol(r, n, d):
    """(スタッフ, 日) の表示記号を返す。"""
    stt = r["assign"].get((n, d), OFF)
    if stt == GAI:
        return r["gairai_cells"].get((n, d), "外")
    if stt == DAY:
        staff = {s["name"]: s for s in r["data"]["staff"]}
        if staff[n].get("tanshuku"):
            return "P"
    return STATE_SYMBOL.get(stt, "")


def build_grid(r):
    days = r["data"]["days"]; dow = r["data"]["dow"]; names = r["names"]; lvl = r["lvl"]
    staff = {s["name"]: s for s in r["data"]["staff"]}
    cols = [f"{d}\n{dow[d]}" for d in days]
    idx = [f"{n} (Lv{lvl[n]}/{staff[n].get('team') or '-'})" for n in names]
    data = [[disp_symbol(r, n, d) for d in days] for n in names]
    return pd.DataFrame(data, index=idx, columns=cols)


def style_grid(df):
    def color(v):
        bg = CELL_COLOR.get(v, "#FFFFFF")
        fg = "white" if v in WHITE_TEXT else "black"
        return f"background-color: {bg}; color: {fg}; text-align: center;"
    return df.style.applymap(color)


if run:
    if up is None:
        st.warning("先に希望届ファイルをアップロードしてください。")
        st.stop()
    try:
        holidays = {int(x) for x in holidays_txt.replace("，", ",").split(",") if x.strip().isdigit()}
    except ValueError:
        holidays = set()

    tmp_in = os.path.join(tempfile.gettempdir(), "wish_upload.xlsx")
    with open(tmp_in, "wb") as f:
        f.write(up.getbuffer())

    with st.spinner("シフトを計算中…"):
        r = solve(tmp_in, holidays, time_limit=time_limit)

    if not r["assign"]:
        st.error(f"解が見つかりませんでした (status: {r['status']})。制約が厳しすぎる可能性があります。")
        st.stop()

    st.success(f"生成完了 (status: {r['status']})")

    df = build_grid(r)
    st.subheader("勤務表")
    st.dataframe(style_grid(df), use_container_width=True, height=560)

    # 凡例
    st.markdown(
        "**凡例**  ● 深夜 / ー● 日勤深夜 / ▲ 準夜 / ー 日勤 / P 時短日勤 / "
        "G/-・-/G 外来(0.5) / × 休 / 年 年休 / 出 出張"
    )

    # 日別サマリー
    days = r["data"]["days"]; dow = r["data"]["dow"]; names = r["names"]
    staff = {s["name"]: s for s in r["data"]["staff"]}
    def count(d, states):
        return sum(1 for n in names if r["assign"].get((n, d)) in states
                   and staff[n].get("emp") != "師長")
    summ = pd.DataFrame({
        "日勤(実働)": [count(d, {DAY, DAYNIGHT}) + 0 for d in days],
        "準夜": [sum(1 for n in names if r["assign"].get((n, d)) == EVE) for d in days],
        "深夜": [sum(1 for n in names if r["assign"].get((n, d)) in {NIGHT, DAYNIGHT}) for d in days],
        "外来": [sum(1 for n in names if r["assign"].get((n, d)) == GAI) for d in days],
    }, index=[f"{d}({dow[d]})" for d in days]).T
    st.subheader("日別人数")
    st.dataframe(summ, use_container_width=True)

    # 警告
    if r["warnings"]:
        with st.expander(f"警告 ({len(r['warnings'])}件)"):
            for w in r["warnings"]:
                st.write("・" + w)

    # ダウンロード(整形済みExcel)
    tmp_out = os.path.join(tempfile.gettempdir(), "schedule_out.xlsx")
    export(tmp_in, holidays, tmp_out)
    with open(tmp_out, "rb") as f:
        st.download_button("勤務表(Excel)をダウンロード", f.read(),
                           file_name="勤務表.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
else:
    st.info("左のサイドバーで希望届をアップロードし、「シフトを生成」を押してください。")
