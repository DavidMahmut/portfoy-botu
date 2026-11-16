"""Rule-based reading of headlines (no AI): what kind of news is moving this stock, and which way does it point?

Used by the x10 paper account. Each headline that is really about the company is matched against keyword patterns for
a few kinds of news ("catalysts"). The result is deliberately simple and repeatable: the same headlines always give the
same answer, and the matched headline is kept so the reader can check the bot's reading.

  kind      what it means for the bot
  hard      company-specific news with real information (fraud claim, investigation, short-seller report, guidance cut,
            failed trial; guidance raise, approval, big contract)
  soft      weaker or routine news (analyst rating, executive change, buyback)
  rumor     unconfirmed reports and crowd-driven moves; direction is taken from the price
  pinned    the company is being bought: the price sticks to the offer, there is nothing to trade

Limits: keywords cannot read irony, context or numbers ("beats estimates but guides lower" matches both sides; then
the side that agrees with the price move wins and the reading is marked as mixed). Law-firm "shareholder alert"
press releases are ignored: they follow every big fall and say nothing new.
"""
import re
from datetime import datetime, timedelta, timezone

# (key, Turkish label, direction, kind, weight, pattern)
RULES = [
    ("fraud", "Dolandırıcılık / muhasebe iddiası", -1, "hard", 5,
     r"\bfraud|accounting (irregularit|scandal|probe|issue|error|problem)|restat(e|es|ed|ing|ement)|misstat|whistleblower"
     r"|embezzl|ponzi|cooked? the books|material weakness|falsif|fabricat|auditor (resign|quit|step)|delay(s|ed)? (its )?"
     r"(annual|quarterly|10-k|10-q) (report|filing)|unable to file|internal (investigation|probe|review) (into|of|finds)"),
    ("probe", "Soruşturma / dava", -1, "hard", 4,
     r"\b(sec|doj|ftc|fbi|cfpb|justice department|regulators?|attorney general|prosecutors?|watchdog|lawmakers|senate|"
     r"congress|european commission)\b.{0,50}\b(investigat|probe|subpoena|charge|sue|sues|suing|lawsuit|inquiry|raid|"
     r"fine|fines|fined|penalt|accus)|subpoena|under investigation|indict|criminal (probe|charge|investigation)"
     r"|antitrust (suit|case|probe|lawsuit)|(faces|facing|hit with) .{0,25}(lawsuit|probe|investigation|charges)"
     r"|(investigation|probe) into"),
    ("shortreport", "Açığa satış raporu", -1, "hard", 5,
     r"short[- ]seller|short report|hindenburg|muddy waters|citron|spruce point|grizzly research|culper|fuzzy panda"
     r"|viceroy|kerrisdale|gotham city research|wolfpack research|iceberg research|scorpion capital|bleecker street"
     r"|jehoshaphat|blue orca|morpheus research|takes? (a )?short position|discloses? short"),
    ("guidecut", "Beklenti düşürüldü / kötü bilanço", -1, "hard", 4,
     r"(cuts?|cutting|lowers?|lowered|slash(es|ed)?|reduc(es|ed)|trims?|trimmed|withdraws?|withdrew|suspends?|pulls?) "
     r".{0,35}(guidance|outlook|forecasts?|view|targets?)|guidance (cut|below|disappoint|miss)|(weak|soft|disappointing|"
     r"downbeat|gloomy|cautious|lackluster|tepid) (guidance|outlook|forecast|results|quarter|earnings|sales)"
     r"|profit warning|warns? (of|on|that)|(miss|misses|missed|missing) .{0,25}(estimates|expectations|views|forecasts|"
     r"targets)|(earnings|revenue|sales|results|profit) (miss|fall short|fell short|disappoint)|falls? short of"
     r"|(below|short of) (wall street|analyst|consensus|expectations|estimates)|slowing (growth|demand|sales)"),
    ("clinicalfail", "Klinik deneme başarısız / FDA reddi", -1, "hard", 5,
     r"(fails?|failed|misses|missed|did not meet|didn.t meet|does not meet|not meet) .{0,45}(trial|study|endpoint|phase)"
     r"|(trial|study) (fail|halt|miss|stopp|discontinu)|clinical hold|complete response letter|\bcrl\b"
     r"|fda (reject|declin|refus|panel votes? against)|discontinu(es|ed|ing) .{0,35}(trial|study|program|development)"),
    ("distress", "Finansal sıkıntı / sulandırma", -1, "hard", 4,
     r"bankrupt|chapter 11|going concern|defaults? on|defaulted|delist|(public|secondary|stock|share|equity|follow-on) "
     r"offering|dilut|at-the-market|convertible (notes?|offering|debt)|private placement|liquidity (crunch|crisis)"
     r"|debt restructuring|covenant (breach|waiver)|prices? .{0,25}offering|registered direct"),
    ("execexit", "Yönetici ayrılığı", -1, "soft", 2,
     r"(ceo|cfo|chief executive|chief financial officer|chairman|founder|president) .{0,45}(resign|steps? down|stepping"
     r" down|depart|oust|fired|leaves?|leaving|exit|to leave|terminated)|(resign|oust|fires?|fired) .{0,30}(ceo|cfo|chief"
     r" executive|chairman)|abrupt(ly)? (departure|exit|resign)|sudden (departure|exit|resignation)"),
    ("downgrade", "Not düşürme", -1, "soft", 2,
     r"downgrad|cuts? (rating|price target|target|pt)\b|lowers? (rating|price target|target)|to (sell|underweight|"
     r"underperform|reduce)\b|price target (cut|lowered|slashed)|bearish (call|note)"),
    ("recall", "Ürün sorunu / veri ihlali", -1, "soft", 2,
     r"\brecall|data breach|cyber ?attack|\bhack(ed|ers)?\b|outage|safety (probe|issue|concern|defect)|ransomware"
     r"|grounded|(plane|train) crash|explosion at|fire at"),
    ("guideraise", "Beklenti yükseltildi / güçlü bilanço", 1, "hard", 4,
     r"(raises?|raised|raising|lifts?|lifted|boosts?|boosted|hikes?|hiked|increases?|increased|ups) .{0,35}(guidance|"
     r"outlook|forecasts?|view|targets?)|guidance (raise|above|beat|tops)|(strong|upbeat|bullish|robust|stellar|"
     r"better-than-expected|blowout|solid) (guidance|outlook|forecast|results|earnings|quarter|sales|demand)"
     r"|(beats?|beat|tops?|topped|crush(es|ed)?|smash(es|ed)?|exceed(s|ed)?|surpass(es|ed)?) .{0,30}(estimates|"
     r"expectations|views|forecasts|targets)|(earnings|revenue|sales|results|profit) (beat|top|surge|jump|soar)"
     r"|record (revenue|quarter|profit|earnings|sales|backlog|bookings)|(above|ahead of) (wall street|analyst|consensus|"
     r"expectations|estimates)"),
    ("approval", "FDA onayı / başarılı deneme", 1, "hard", 5,
     r"fda (approv|clear|grant|nod)|approval (from|for|of)|(wins?|won|gets?|got|receives?|received|secures?) .{0,25}"
     r"approval|(meets?|met|hits?|achieves?|achieved) .{0,35}(primary |main |key )?(endpoint|goal)|positive .{0,30}(data|"
     r"results|trial|study|readout|topline|top-line)|breakthrough (therapy|designation)|successful (trial|study)"
     r"|priority review|statistically significant"),
    ("deal", "Büyük sözleşme / ortaklık", 1, "hard", 3,
     r"(wins?|won|awarded|secures?|secured|lands?|landed|signs?|signed|inks?|inked|bags?) .{0,40}(contract|deal|order|"
     r"agreement|partnership|award)|\$ ?\d+(\.\d+)? ?(billion|bn|b|million|m)\b.{0,40}(contract|deal|order|investment|"
     r"award)|partnership with|partners with|strategic (investment|partnership|collaboration|alliance)|to supply|supply "
     r"(deal|agreement)|(invests?|investing|to invest|takes? (a )?stake) .{0,30}(in|billion|million)"),
    ("buyback", "Geri alım / temettü", 1, "soft", 2,
     r"buy ?backs?|buy(s|ing)? back|share repurchase|repurchase (program|plan|authorization)|special dividend|(raises?|hikes?|boosts?|"
     r"increases?) .{0,20}dividend|stock split"),
    ("upgrade", "Not yükseltme", 1, "soft", 2,
     r"upgrad|raises? (rating|price target|target|pt)\b|price target (raised|hike|boost|lifted)|(raised|raises|lifted|lifts|"
     r"moved|moves|boosted|boosts) .{0,40}\bto (buy|overweight|outperform|strong buy)\b|initiat(es|ed) .{0,30}(buy|overweight|outperform)|bullish (call|note)|top pick"),
    ("index", "Endekse giriş", 1, "soft", 3,
     r"(join|joins|joining|added to|inclusion in|to be added to|will enter|enters?|replace .{0,25} in) .{0,25}(s&p 500|"
     r"nasdaq[- ]100|dow jones|russell (1000|2000)|s&p midcap|s&p smallcap)"),
    ("takeover", "Satın alma (fiyat teklife sabitlenir)", 1, "pinned", 6,
     r"to be acquired|agrees? to be (bought|acquired|taken private)|(to acquire|acquires|acquiring|acquisition of|to buy|"
     r"buying|buys) .{0,60}(for|in) (about |roughly |nearly |approximately |a )?\$ ?\d|buyout (deal|offer|agreement)"
     r"|take[- ]private (deal|offer|agreement)|takeover (bid|offer|deal)|merger agreement|definitive (merger |"
     r"acquisition )?agreement|agrees? to (merge|sell itself|be sold)|all-cash (deal|offer|transaction)|tender offer"
     r"|go(ing)? private in"
     # an offer on the table (not yet agreed) pins the price just the same: "Quebecor Makes Bid for Telus"
     r"|(makes?|made|launch(es|ed)?|submits?|submitted|unveils?|tables?) (a |an |its )?(\$ ?[\d.,]+ ?(billion|bn|million|mn)? )?"
     r"((all-cash|cash|hostile|unsolicited|rival|takeover|buyout|formal|binding|improved|sweetened) )*(bid|offer|proposal) "
     r"(for|to (buy(?! ?back)|acquire|take over))|\b(bid|offer) to (buy(?! ?back)|acquire|take over)"
     r"|\$ ?[\d.,]+ ?(billion|bn) (takeover|buyout) (bid|offer)"),
    ("rumor", "Söylenti / spekülasyon", 0, "rumor", 2,
     r"reportedly|rumou?r|\bsaid to\b|sources? (say|said|familiar|tell|told)|people familiar|in (advanced |early |"
     r"preliminary )?talks|explor(es|ing) (a |its )?(sale|options|strategic|potential)|(consider|weigh|mull)(s|ing) (a |an )?"
     r"(sale|bid|offer|takeover|merger|breakup|spin)|takeover (interest|speculation|chatter|target|approach)|speculation"
     r"|(bloomberg|wsj|reuters|ft|cnbc|the information) (reports?|says)|unconfirmed|report says"),
    ("squeeze", "Kısa sıkışması / meme hareketi", 0, "rumor", 2,
     r"short squeeze|meme stock|retail (frenzy|traders|investors|crowd)|reddit|wallstreetbets|roaring kitty|gamma squeeze"
     r"|social media (buzz|frenzy|hype)|goes viral|most active stocks?"),
]
COMPILED = [(k, lab, d, kind, w, re.compile(rx, re.I)) for k, lab, d, kind, w, rx in RULES]
LABELS = {k: lab for k, lab, *_ in RULES}
KINDS = {k: kind for k, _, _, kind, *_ in RULES}
DIRS = {k: d for k, _, d, *_ in RULES}
KIND_TR = {"hard": "sert", "soft": "zayıf", "rumor": "söylenti", "pinned": "satın alma"}

# law-firm solicitations and other noise that follows every big move
NOISE = re.compile(
    r"shareholder (alert|notice|investigation|update|reminder|action)|investor (alert|notice|reminder|deadline)|law (firm|offices?)"
    r"|rosen law|pomerantz|levi & korsinsky|bragar eagel|faruqi|glancy prongay|kessler topaz|robbins (geller|llp)|"
    r"bernstein liebhard|hagens berman|the schall|kirby mcinerney|scott\+scott|johnson fistel|kahn swick|halper sadeh|"
    r"class action (deadline|reminder|notice|lawsuit filed|filed|alert)|investors? (who|with) (lost|losses|suffered)|"
    r"(deadline|lead plaintiff) (alert|reminder|approaching|deadline)|encourages? (investors|shareholders)|securities "
    r"(fraud )?class action|recover (your |their )?losses|contact the firm|investigat(es|ing) (claims|potential claims) on behalf",
    re.I)
GENERIC_FIRST = {"american", "first", "united", "national", "general", "global", "international", "new", "the", "us",
                 "u.s.", "advanced", "applied", "digital", "western", "southern", "northern", "eastern", "pacific",
                 "atlantic", "great", "royal", "energy", "capital", "bank", "banco", "china", "america", "universal",
                 "standard", "liberty", "federal", "community", "world", "sun", "blue", "green", "red", "gold", "silver"}
SUFFIX = re.compile(r"[,.]?\s+(incorporated|inc|corp|corporation|company|co|ltd|limited|plc|holdings?|group|n\.?v|s\.?a|"
                    r"ag|se|lp|l\.p|trust|class [a-c]|common stock|ordinary shares|adr|ads)\.?\s*$", re.I)


def name_keys(ticker, name):
    """Words that show a headline is about this company: the ticker (3+ letters) and the first distinctive word(s)."""
    nm = (name or "").strip()
    for _ in range(4):
        nm = SUFFIX.sub("", nm).strip().rstrip(",.")
    words = [w for w in re.split(r"[\s\-/&]+", nm) if w]
    keys = []
    if words:
        first = re.sub(r"[^A-Za-z0-9.']", "", words[0])
        if len(first) >= 4 and first.lower() not in GENERIC_FIRST:
            keys.append(first.lower())
        elif len(words) >= 2:
            keys.append((first + " " + re.sub(r"[^A-Za-z0-9.']", "", words[1])).lower())
        elif first:
            keys.append(first.lower())
    return keys


def is_about(ticker, keys, text):
    t = ticker.replace("-", ".")
    if len(t) >= 3 and re.search(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", text):
        return True
    if len(t) <= 2 and re.search(r"\((NYSE|NASDAQ|Nasdaq|NYSEAMERICAN)?:? ?" + re.escape(t) + r"\)", text):
        return True
    low = text.lower()
    return any(re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", low) for k in keys)


def _when(n):
    at = n.get("at")
    if isinstance(at, datetime):
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def classify(ticker, name, items, move_pct, now, max_age_h=40):
    """items: [{title, summary, publisher, url, at}]. move_pct: today's change in %.
    Returns {cat, label, kind, dir, mixed, score, headline, matches, relevant, read, text}; cat None = no usable news."""
    keys = name_keys(ticker, name)
    cutoff = now - timedelta(hours=max_age_h)
    relevant, hits = [], {}
    for n in items or []:
        title = (n.get("title") or "").strip()
        if not title:
            continue
        when = _when(n)
        if when is not None and when < cutoff:
            continue
        body = title + ". " + (n.get("summary") or "")
        if NOISE.search(body):
            continue
        if not is_about(ticker, keys, title) and not is_about(ticker, keys, n.get("summary") or ""):
            continue
        relevant.append(n)
        in_title = title.lower()
        for key, lab, d, kind, w, rx in COMPILED:
            m = rx.search(title) or rx.search(body)
            if not m:
                continue
            weight = w * (1.0 if rx.search(in_title) else 0.6)          # a match in the headline itself counts more
            h = hits.setdefault(key, {"cat": key, "score": 0.0, "n": 0, "item": n, "when": when})
            h["score"] += weight
            h["n"] += 1
            if when and (h["when"] is None or when > h["when"]):
                h["item"], h["when"] = n, when
    base = {"cat": None, "label": None, "kind": None, "dir": 0, "mixed": False, "score": 0.0, "headline": None,
            "matches": [], "relevant": len(relevant), "read": len(items or [])}
    if not hits:
        base["text"] = ("Şirketle ilgili güncel haber bulunamadı." if not relevant else
                        f"{len(relevant)} güncel haber var ama bilinen bir haber türüne uymuyor.")
        if relevant:
            n = relevant[0]
            base["headline"] = {"title": n.get("title"), "url": n.get("url"), "publisher": n.get("publisher")}
        return base
    sign = 1 if move_pct > 0 else -1 if move_pct < 0 else 0
    ranked = sorted(hits.values(), key=lambda h: -h["score"])
    pinned = next((h for h in ranked if KINDS[h["cat"]] == "pinned"), None)
    agree = [h for h in ranked if DIRS[h["cat"]] == sign and KINDS[h["cat"]] != "pinned"]
    against = [h for h in ranked if DIRS[h["cat"]] == -sign and DIRS[h["cat"]] != 0]
    neutral = [h for h in ranked if DIRS[h["cat"]] == 0]
    if pinned and pinned["score"] >= ranked[0]["score"] * 0.8:
        top, mixed = pinned, False
    elif agree:
        top, mixed = agree[0], bool(against)
    elif neutral:
        top, mixed = neutral[0], bool(against)
    else:
        top, mixed = against[0], True          # the news says one thing, the price the other
    cat = top["cat"]
    d = DIRS[cat] if DIRS[cat] != 0 else sign
    n = top["item"]
    head = {"title": n.get("title"), "url": n.get("url"), "publisher": n.get("publisher"),
            "at": top["when"].isoformat() if top["when"] else None}
    conflict = DIRS[cat] != 0 and DIRS[cat] != sign and KINDS[cat] != "pinned"
    text = (f"Haber türü: {LABELS[cat]} ({KIND_TR[KINDS[cat]]}"
            + (", yönü fiyattan alındı" if DIRS[cat] == 0 else ", olumlu" if DIRS[cat] > 0 else ", olumsuz") + ")"
            + (" — ama fiyat ters yönde hareket ediyor" if conflict else "")
            + (" — karışık: ters yönde haber de var" if mixed and not conflict else "")
            + f". “{head['title']}”" + (f" ({head['publisher']})" if head.get("publisher") else "") + ".")
    return {"cat": cat, "label": LABELS[cat], "kind": KINDS[cat], "dir": d, "mixed": mixed, "conflict": conflict,
            "score": round(top["score"], 1), "headline": head,
            "matches": [{"cat": h["cat"], "label": LABELS[h["cat"]], "score": round(h["score"], 1), "n": h["n"]} for h in ranked],
            "relevant": len(relevant), "read": len(items or []), "text": text}
