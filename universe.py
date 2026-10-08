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

# Market indicators shown in the daily report (not traded)
INDICATORS = [
    ("SPY", "S&P 500 (SPY)"), ("QQQ", "Nasdaq 100 (QQQ)"), ("DIA", "Dow Jones (DIA)"), ("IWM", "Russell 2000 (IWM)"),
    ("^VIX", "VIX korku endeksi"), ("CL=F", "WTI petrol"), ("BZ=F", "Brent petrol"), ("^TNX", "ABD 10 yıllık faiz"),
    ("GC=F", "Altın (ons)"),
]


def name_of(t):
    return NAMES.get(t, t)


def kind_of(t):
    return "ETF" if t in ETFS else "Hisse"


def quote_url(t):
    return "https://finance.yahoo.com/quote/" + t.replace("^", "%5E").replace("=", "%3D") + "/"
