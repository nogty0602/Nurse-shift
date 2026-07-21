"""勤務表の手直し指示（日本語テキスト）を、solve() が使える形に変換する。

対応する書き方の例:
  Cの5連勤後に2連休を入れて
  Jの準夜が続かないように
  Aの夜勤を月8回までにして
  Hの休みを12日以上にして
  Bの18日を休みにして / Bの3日を日勤に
1行に1つの指示を書く。
"""
import re

SYMBOL_WORDS = {
    "休み": "×", "休": "×", "公休": "×", "オフ": "×",
    "年休": "年", "有休": "年", "有給": "年",
    "日勤": "ー", "準夜": "▲", "深夜": "●", "出張": "出",
    "外来": "G/-", "委員会": "ーイ", "研修": "研",
}


def parse_adjustments(text, known_names):
    """複数行のテキストを調整指示のリストに変換。(指示リスト, 解釈できなかった行)"""
    rules, unknown = [], []
    if not text:
        return rules, unknown

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # スタッフ名を特定（長い名前を優先して照合）
        name = None
        for n in sorted(known_names, key=len, reverse=True):
            if re.search(rf"(^|[^A-Za-z0-9]){re.escape(n)}([^A-Za-z0-9]|$)", line):
                name = n
                break
        if name is None:
            unknown.append(raw)
            continue

        soft = ("なるべく" in line or "できれば" in line or "優先" in line)
        hard = not soft

        # 1) 特定日の勤務を固定  例) Bの18日を休みに / Bの3日を日勤に
        m = re.search(r"(\d{1,2})\s*日", line)
        sym = None
        for word, s in SYMBOL_WORDS.items():
            if word in line:
                sym = s
                break
        if m and sym and ("連勤" not in line) and ("以上" not in line) and ("まで" not in line):
            rules.append({"staff": name, "rule": "fix",
                          "day": int(m.group(1)), "symbol": sym, "hard": True})
            continue

        # 2) 連勤後の2連休  例) Cの5連勤後に2連休を入れて
        if "連勤" in line and ("2連休" in line or "２連休" in line or "連休" in line):
            mrun = re.search(r"(\d)\s*連勤", line)
            run = int(mrun.group(1)) if mrun else 5
            rules.append({"staff": name, "rule": "rest2_after_run",
                          "run": run, "hard": hard})
            continue

        # 3) 準夜の連続・集中を抑える  例) Jの準夜が続かないように
        if "準夜" in line and ("続" in line or "偏" in line or "ばかり" in line or "連続" in line):
            mlim = re.search(r"(\d)\s*回", line)
            lim = int(mlim.group(1)) if mlim else 2
            rules.append({"staff": name, "rule": "no_eve_run",
                          "limit": lim, "hard": hard})
            continue

        # 4) 夜勤の回数上限  例) Aの夜勤を月8回までに
        if "夜勤" in line and ("まで" in line or "以下" in line or "上限" in line):
            mcap = re.search(r"(\d{1,2})\s*回", line)
            if mcap:
                rules.append({"staff": name, "rule": "max_nights",
                              "cap": int(mcap.group(1)), "hard": hard})
                continue

        # 5) 休日数の下限  例) Hの休みを12日以上に
        if ("休" in line) and ("以上" in line or "確保" in line or "増やし" in line):
            mday = re.search(r"(\d{1,2})\s*日", line)
            if mday:
                rules.append({"staff": name, "rule": "min_rest",
                              "days": int(mday.group(1)), "hard": hard})
                continue

        unknown.append(raw)
    return rules, unknown
