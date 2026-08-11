import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

US_CITIES = [
    ("New York", 40.71, -74.01, 8.3),
    ("Los Angeles", 34.05, -118.24, 3.9),
    ("Chicago", 41.88, -87.63, 2.7),
    ("Houston", 29.76, -95.37, 2.3),
    ("Dallas", 32.78, -96.80, 1.3),
    ("Atlanta", 33.75, -84.39, 0.5),
    ("Boston", 42.36, -71.06, 0.7),
    ("Miami", 25.76, -80.19, 0.4),
]

BASE_F = 65.0

# Сезонные нормы США (взвешенно по населению, сумма за 10 дней)
MONTH_NORMS = {
    1: (300, 5), 2: (260, 5), 3: (180, 15), 4: (90, 40),
    5: (25, 90), 6: (5, 140), 7: (0, 170), 8: (0, 160),
    9: (20, 110), 10: (100, 45), 11: (200, 10), 12: (290, 5),
}


def fetch_gfs_forecast():
    """HDD/CDD за 10 дней по последнему прогону GFS."""
    total_hdd = 0.0
    total_cdd = 0.0
    total_weight = sum(c[3] for c in US_CITIES)
    ok_cities = 0

    for name, lat, lon, weight in US_CITIES:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_mean",
            "temperature_unit": "fahrenheit",
            "forecast_days": 10,
            "models": "gfs_seamless",
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            temps = data.get("daily", {}).get("temperature_2m_mean", [])
        except Exception as e:
            logger.warning(f"GFS fetch failed for {name}: {e}")
            continue

        hdd = 0.0
        cdd = 0.0
        good = [t for t in temps if t is not None]
        for t in good:
            hdd += max(0.0, BASE_F - t)
            cdd += max(0.0, t - BASE_F)

        total_hdd += hdd * weight / total_weight
        total_cdd += cdd * weight / total_weight
        ok_cities += 1

    if not ok_cities:
        return None

    return {
        "hdd10": round(total_hdd, 1),
        "cdd10": round(total_cdd, 1),
    }


def gfs_indicator(gfs, price=None):
    """Индикатор влияния последнего прогона GFS на фьючерс NG."""
    month = datetime.now().month
    norm_hdd, norm_cdd = MONTH_NORMS.get(month, (50, 50))

    if norm_cdd >= norm_hdd:
        cur, norm = gfs["cdd10"], norm_cdd
        kind = "CDD (спрос на охлаждение)"
    else:
        cur, norm = gfs["hdd10"], norm_hdd
        kind = "HDD (спрос на отопление)"

    dev = cur - norm
    pct = dev / max(norm, 1) * 100

    if pct > 15:
        score, label = 2, "сильное бычье давление"
    elif pct > 5:
        score, label = 1, "умеренное бычье давление"
    elif pct < -15:
        score, label = -2, "сильное медвежье давление"
    elif pct < -5:
        score, label = -1, "умеренное медвежье давление"
    else:
        score, label = 0, "нейтрально, в рамках сезона"

    text = f"{kind}: {cur} против нормы {norm} ({pct:+.0f}%). "
    text += f"Влияние последнего GFS на NG: {label}."

    if price:
        text += f" Фьючерс NG: {price['last']} USD/MMBtu "
        text += f"({price['chg']:+.3f} за день)."

    return {
        "score": score,
        "label": label,
        "text": text,
        "pct": round(pct, 1),
        "hdd10": gfs["hdd10"],
        "cdd10": gfs["cdd10"],
    }