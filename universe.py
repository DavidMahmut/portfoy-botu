"""Tradable universe: liquid US stocks and unleveraged ETFs (price >= $5, market cap >= ~$2B)."""

NAMES = {
    # Mega-cap tech / internet
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "AMZN": "Amazon", "GOOGL": "Alphabet",
    "META": "Meta Platforms", "AVGO": "Broadcom", "TSLA": "Tesla", "NFLX": "Netflix", "ORCL": "Oracle",
    "CRM": "Salesforce", "ADBE": "Adobe", "AMD": "Advanced Micro Devices", "MU": "Micron Technology",
    "INTC": "Intel", "QCOM": "Qualcomm", "TSM": "Taiwan Semiconductor", "ASML": "ASML Holding", "ARM": "Arm Holdings",
    # Software / growth
    "PLTR": "Palantir Technologies", "NOW": "ServiceNow", "CRWD": "CrowdStrike", "PANW": "Palo Alto Networks",
    "SNOW": "Snowflake", "SHOP": "Shopify", "UBER": "Uber", "APP": "AppLovin", "DDOG": "Datadog", "NET": "Cloudflare",
    # AI infrastructure / semis equipment / networking
    "ANET": "Arista Networks", "CIEN": "Ciena", "SMCI": "Super Micro Computer", "VRT": "Vertiv", "DELL": "Dell Technologies",
    "MRVL": "Marvell Technology", "LRCX": "Lam Research", "AMAT": "Applied Materials", "KLAC": "KLA",
    # Power
    "VST": "Vistra", "CEG": "Constellation Energy", "NRG": "NRG Energy", "GEV": "GE Vernova",
    # Financials
    "JPM": "JPMorgan Chase", "GS": "Goldman Sachs", "MS": "Morgan Stanley", "V": "Visa", "MA": "Mastercard",
    "COIN": "Coinbase", "HOOD": "Robinhood", "SOFI": "SoFi Technologies", "BX": "Blackstone",
    # Health care
    "LLY": "Eli Lilly", "NVO": "Novo Nordisk", "UNH": "UnitedHealth", "ISRG": "Intuitive Surgical", "VRTX": "Vertex Pharmaceuticals",
    # Energy
    "XOM": "Exxon Mobil", "CVX": "Chevron", "OXY": "Occidental Petroleum", "COP": "ConocoPhillips", "SLB": "SLB", "EOG": "EOG Resources",
    # Industrials / defense
    "CAT": "Caterpillar", "GE": "GE Aerospace", "RTX": "RTX", "LMT": "Lockheed Martin", "NOC": "Northrop Grumman",
    "BA": "Boeing", "DE": "Deere",
    # Consumer
    "COST": "Costco", "WMT": "Walmart", "HD": "Home Depot", "MCD": "McDonald's", "NKE": "Nike", "SBUX": "Starbucks",
    # ETFs (unleveraged)
    "QQQ": "Invesco QQQ (Nasdaq 100)", "SMH": "VanEck Semiconductor ETF", "XLE": "Energy Select Sector SPDR",
    "XOP": "SPDR S&P Oil & Gas Exploration & Production ETF", "XLF": "Financial Select Sector SPDR",
    "XLV": "Health Care Select Sector SPDR", "XLI": "Industrial Select Sector SPDR", "XLU": "Utilities Select Sector SPDR",
    "XLK": "Technology Select Sector SPDR", "XLY": "Consumer Discretionary Select Sector SPDR",
    "XLP": "Consumer Staples Select Sector SPDR", "ITA": "iShares U.S. Aerospace & Defense ETF",
    "GLD": "SPDR Gold Shares", "SLV": "iShares Silver Trust", "IWM": "iShares Russell 2000 ETF",
    "ARKK": "ARK Innovation ETF", "URA": "Global X Uranium ETF", "COPX": "Global X Copper Miners ETF",
    "GDX": "VanEck Gold Miners ETF", "KRE": "SPDR S&P Regional Banking ETF", "IBIT": "iShares Bitcoin Trust",
}

ETFS = {"QQQ", "SMH", "XLE", "XOP", "XLF", "XLV", "XLI", "XLU", "XLK", "XLY", "XLP", "ITA", "GLD", "SLV",
        "IWM", "ARKK", "URA", "COPX", "GDX", "KRE", "IBIT", "SPY", "DIA"}

UNIVERSE = sorted(NAMES.keys())

# Leveraged and inverse ETFs (not ranked; used as index sleeves by market regime).
# ticker -> (underlying ETF, leverage factor; negative = inverse)
LEVERAGED = {
    "TQQQ": ("QQQ", 3), "UPRO": ("SPY", 3), "SOXL": ("SMH", 3),
    "SQQQ": ("QQQ", -3), "SPXU": ("SPY", -3), "SOXS": ("SMH", -3),
}
NAMES.update({
    "TQQQ": "ProShares UltraPro QQQ (3x Nasdaq 100)", "UPRO": "ProShares UltraPro S&P 500 (3x)",
    "SOXL": "Direxion Daily Semiconductor Bull 3x", "SQQQ": "ProShares UltraPro Short QQQ (−3x Nasdaq 100)",
    "SPXU": "ProShares UltraPro Short S&P 500 (−3x)", "SOXS": "Direxion Daily Semiconductor Bear 3x (−3x)",
    "SPY": "SPDR S&P 500 ETF", "DIA": "SPDR Dow Jones ETF",
})
ETFS |= set(LEVERAGED)
BULL_ETF = {u: t for t, (u, f) in LEVERAGED.items() if f > 0}   # QQQ -> TQQQ
BEAR_ETF = {u: t for t, (u, f) in LEVERAGED.items() if f < 0}   # QQQ -> SQQQ


def lev_of(t):
    """Absolute leverage factor of an instrument (1 for ordinary stocks/ETFs)."""
    return abs(LEVERAGED[t][1]) if t in LEVERAGED else 1

# Market indicators shown in the daily report (not traded)
INDICATORS = [
    ("SPY", "S&P 500 (SPY)"), ("QQQ", "Nasdaq 100 (QQQ)"), ("DIA", "Dow Jones (DIA)"), ("IWM", "Russell 2000 (IWM)"),
    ("^VIX", "VIX korku endeksi"), ("CL=F", "WTI petrol"), ("BZ=F", "Brent petrol"), ("^TNX", "ABD 10 yıllık faiz"),
    ("GC=F", "Altın (ons)"),
]


def name_of(t):
    return NAMES.get(t, t)


def kind_of(t):
    if t in LEVERAGED:
        f = LEVERAGED[t][1]
        return ("3x ETF" if f > 0 else "−3x ters ETF")
    return "ETF" if t in ETFS else "Hisse"


def quote_url(t):
    return "https://finance.yahoo.com/quote/" + t.replace("^", "%5E").replace("=", "%3D") + "/"
