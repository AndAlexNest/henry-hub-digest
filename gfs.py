import logging
import requests

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


def fetch_gfs_forecast():
    """GFS-прогноз на 10 дней по городам США (Open-Meteo)."""
    city_rows = []
    total_hdd = 0.0
    total_cdd = 0.0
    total_weight = sum(c[3] for c in US_CITIES)

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

        avg_t = sum(good) / max(1, len(good))
        city_rows.append({
            "name": name,
            "avg_temp_f": round(avg_t, 1),
            "hdd10": int(round(hdd)),
            "cdd10": int(round(cdd)),
        })

    if not city_rows:
        return None

    result = {
        "cities": city_rows,
        "hdd10": round(total_hdd, 1),
        "cdd10": round(total_cdd, 1),
        "model": "GFS (Open-Meteo)",
    }
    logger.info(f"GFS: HDD10={result['hdd10']}, CDD10={result['cdd10']}")
    return result


def gfs_weather_assessment(gfs):
    """Качественная оценка влияния погоды на спрос на газ."""
    hdd = gfs["hdd10"]
    cdd = gfs["cdd10"]

    if hdd > 100:
        return "Холодный фон: высокий спрос на отопление, поддержка цен на газ (бычий фактор)."
    if hdd > 50:
        return "Умеренно прохладно: отопительный спрос выше нормы, умеренно бычий фактор."
    if cdd > 100:
        return "Жара: высокий спрос на охлаждение и электрогенерацию, бычий фактор для газа."
    if cdd > 50:
        return "Тепло: спрос на охлаждение выше нормы, умеренно бычий фактор."
    return "Мягкая погода: спрос минимален, медвежий фактор для цен на газ."