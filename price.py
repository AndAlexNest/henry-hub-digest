import logging
import requests

logger = logging.getLogger(__name__)

URL = "https://query1.finance.yahoo.com/v8/finance/chart/NG=F"


def fetch_ng_price():
    """Текущая цена фьючерса Henry Hub и изменение за день."""
    params = {"range": "5d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(URL, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        res = data["chart"]["result"][0]
        closes = res["indicators"]["quote"][0].get("close", [])
        closes = [c for c in closes if c is not None]
        if not closes:
            return None
        last = closes[-1]
        prev = closes[-2] if len(closes) > 1 else last
        return {
            "last": round(last, 3),
            "prev": round(prev, 3),
            "chg": round(last - prev, 3),
        }
    except Exception as e:
        logger.error(f"NG price fetch error: {e}")
        return None