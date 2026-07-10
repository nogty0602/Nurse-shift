"""別プロセスで解く子スクリプト（Streamlitと分離して ortools を実行）。

呼び出し: python run_solver.py <in.xlsx> <holidays> <time_limit> <out.xlsx> <result.pkl>
  holidays: "11,15" のようなカンマ区切り（空文字可）
結果を result.pkl に保存し、勤務表を out.xlsx に書き出す。
"""
import os
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
import sys
import pickle

from solve import solve
from export import export


def main():
    in_path = sys.argv[1]
    hol = sys.argv[2] if len(sys.argv) > 2 else ""
    holidays = {int(x) for x in hol.split(",") if x.strip().isdigit()}
    time_limit = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    out_xlsx = sys.argv[4]
    result_pkl = sys.argv[5]

    r = solve(in_path, holidays, time_limit=time_limit)
    payload = {"status": r["status"], "warnings": r["warnings"], "has_output": False}
    if r["assign"]:
        staff = {s["name"]: {"team": s.get("team"), "emp": s.get("emp"),
                             "tanshuku": s.get("tanshuku")} for s in r["data"]["staff"]}
        payload.update(
            has_output=True,
            days=r["data"]["days"], dow=r["data"]["dow"],
            names=r["names"], lvl=r["lvl"], staff=staff,
            assign={f"{n}|{d}": st for (n, d), st in r["assign"].items()},
            gairai={f"{n}|{d}": sym for (n, d), sym in r["gairai_cells"].items()})
        export(in_path, holidays, out_xlsx, r=r)
    with open(result_pkl, "wb") as f:
        pickle.dump(payload, f)


if __name__ == "__main__":
    main()
