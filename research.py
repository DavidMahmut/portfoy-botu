"""Before the open (run by the half-hourly watch job between 08:30 and 09:29 New York time): the daily 3+3 ranking
(daytrade.research) on pre-market prices, written to picks.json. The bot opens the positions at 09:35 from this file,
or does the research itself at its first run when the file is not today's."""
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import catalyst as CAT
import daytrade as DT
import trader as T

NY = ZoneInfo("America/New_York")


def main():
    import os
    test_at = os.environ.get("RESEARCH_AT")          # a dry run on an earlier day, e.g. 2026-11-25T09:31 (written to picks_test.json)
    now = datetime.fromisoformat(test_at).replace(tzinfo=NY) if test_at else datetime.now(NY)
    d = now.date().isoformat()
    if now.weekday() >= 5 or d in T.HOLIDAYS:
        print("borsa kapalı")
        return 0
    import data
    now_utc = now.astimezone(ZoneInfo("UTC"))
    news = T.Engine._news_items
    picks = DT.research(data, now, T.SANCTIONED_NAMES, lambda t: news(None, t),
                        lambda t, n, items, mv, _now: CAT.classify(t, n, items, mv, now_utc))
    out = DT.PICKS.with_name("picks_test.json") if test_at else DT.PICKS
    out.write_text(json.dumps(picks, ensure_ascii=False, indent=1), encoding="utf-8")
    print("picks", d, "long", [c["t"] for c in picks["long"][:DT.DAY_N]], "short", [c["t"] for c in picks["short"][:DT.DAY_N]],
          "priced", picks["priced"], "of", picks["universe"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
