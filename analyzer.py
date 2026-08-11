import os
import re
import json
import time
import logging
import requests

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — аналитик сырьевых рынков, эксперт по ценам на природный газ Henry Hub в США.

Твоя задача:
1. Проанализировать новость и оценить, как она может повлиять на цену Henry Hub.
2. Оценить влияние по каждому из факторов ниже, поставив оценку от -3 до +3:
   -3 = сильное давление вниз (медвежий фактор)
    0 = нейтрально
   +3 = сильное давление вверх (бычий фактор)

Факторы (обязательно для каждого):
- production (Добыча): изменение объёма добычи
- storage (Запасы): уровень и динамика запасов в хранилищах
- demand (Спрос): спрос со стороны ТЭС, промышленности, LNG-экспорта
- alternatives (Альтернативы): влияние ВИЭ, угля, атомной энергии
- weather (Погода): влияние температуры, сезона
- geopolitics (Геополитика): конфликты, санкции, экспортные ограничения
- oil (Нефть): влияние цен на нефть и связанных нефтепродуктов

3. Дать итоговый вывод: bullish / bearish / neutral.
4. Написать краткий аналитический комментарий на русском языке.

Формат ответа СТРОГО в JSON без markdown-обёртки:
{
  "is_relevant": true,
  "impact": "bullish",
  "scores": {
    "production": 0,
    "storage": 0,
    "demand": 0,
    "alternatives": 0,
    "weather": 0,
    "geopolitics": 0,
    "oil": 0
  },
  "overall_score": 0,
  "comment_ru": "Комментарий на русском, 3-5 предложений"
}

Если новость НЕ относится к природному газу США — верни:
{"is_relevant": false, "impact": "neutral", "scores": {"production": 0, "storage": 0, "demand": 0, "alternatives": 0, "weather": 0, "geopolitics": 0, "oil": 0}, "overall_score": 0, "comment_ru": ""}
"""


def _parse_json_response(text):
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
    return None


def _call_yandexgpt(api_key, folder_id, user_prompt, model):
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {api_key}",
        "x-folder-id": folder_id,
    }
    payload = {
        "modelUri": f"gpt://{folder_id}/{model}/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0.3,
            "maxTokens": 1000,
        },
        "messages": [
            {"role": "system", "text": SYSTEM_PROMPT},
            {"role": "user", "text": user_prompt},
        ],
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()

    data = resp.json()
    alts = data.get("result", {}).get("alternatives", [])
    if not alts:
        return ""
    return alts[0].get("message", {}).get("text", "")


def analyze_news(items, yandexgpt_cfg):
    api_key = os.getenv(yandexgpt_cfg.get("api_key_env", ""), "")
    folder_id = os.getenv(yandexgpt_cfg.get("folder_id_env", ""), "")

    if not api_key or not folder_id or not items:
        logger.warning("YandexGPT not configured or no items")
        return items

    model = yandexgpt_cfg.get("model", "yandexgpt-lite")
    max_analyze = yandexgpt_cfg.get("max_news_to_analyze", 15)

    analyzed_items = []
    to_analyze = items[:max_analyze]

    for i, item in enumerate(to_analyze, 1):
        title = item["title"][:50]
        logger.info(f"[{i}/{len(to_analyze)}] Analyzing: {title}...")

        user_prompt = f"""Проанализируй новость:

Источник: {item['source']}
Дата: {item['published']}
Заголовок: {item['title']}
Описание: {item['summary']}
URL: {item['link']}

Верни JSON согласно инструкциям."""

        try:
            text = _call_yandexgpt(api_key, folder_id, user_prompt, model)
            parsed = _parse_json_response(text)

            if not parsed:
                logger.warning("  Could not parse LLM response")
                continue

            if not parsed.get("is_relevant", False):
                logger.info("  Skipped as irrelevant")
                continue

            scores = parsed.get("scores", {})
            overall = 0
            for v in scores.values():
                if isinstance(v, (int, float)):
                    overall += int(v)

            item["analysis"] = {
                "impact": parsed.get("impact", "neutral"),
                "scores": scores,
                "overall_score": overall,
                "comment_ru": parsed.get("comment_ru", ""),
            }
            analyzed_items.append(item)
            logger.info(f"  {parsed.get('impact')} (score: {overall:+d})")
            time.sleep(0.5)

        except requests.exceptions.HTTPError as e:
            logger.error(f"  YandexGPT API error: {e}")
            if e.response is not None:
                logger.error(f"    {e.response.text[:300]}")
        except Exception as e:
            logger.error(f"  Error: {e}")
            time.sleep(2)

    analyzed_items.extend(items[max_analyze:])
    return analyzed_items


def make_overall_conclusion(top_items, gfs, weather_note, yandexgpt_cfg):
    """Общий вывод по рынку на основе топ-новостей и прогноза GFS."""
    api_key = os.getenv(yandexgpt_cfg.get("api_key_env", ""), "")
    folder_id = os.getenv(yandexgpt_cfg.get("folder_id_env", ""), "")
    if not api_key or not folder_id:
        return None

    news_block = ""
    for i, it in enumerate(top_items, 1):
        a = it.get("analysis", {})
        news_block += f"{i}. {it['title']}\n"
        news_block += f"   Направление: {a.get('impact', 'neutral')}, "
        news_block += f"оценка: {a.get('overall_score', 0)}\n"
        news_block += f"   {a.get('comment_ru', '')}\n\n"

    if not news_block:
        news_block = "(значимых новостей нет)\n"

    if gfs:
        gfs_block = f"HDD (10 дн): {gfs['hdd10']}, CDD (10 дн): {gfs['cdd10']}"
    else:
        gfs_block = "(нет данных прогноза)"

    prompt = (
        "Ты — аналитик рынка природного газа США (Henry Hub).\n\n"
        f"Ключевые новости дня:\n{news_block}\n"
        f"Погодный прогноз GFS: {gfs_block}\n"
        f"Оценка погоды: {weather_note}\n\n"
        "Задача: напиши ОБЩИЙ ВЫВОД по рынку на русском языке, 4-6 предложений. "
        "Укажи общее направление (bullish/bearish/neutral), ключевые драйверы "
        "и влияние погоды. Пиши связным текстом, без списков.\n"
        'Верни JSON строго в формате: '
        '{"conclusion_ru": "...", "market_bias": "bullish"}'
    )

    try:
        model = yandexgpt_cfg.get("model", "yandexgpt-lite")
        text = _call_yandexgpt(api_key, folder_id, prompt, model)
        parsed = _parse_json_response(text)
        if parsed:
            return parsed
    except Exception as e:
        logger.error(f"Overall conclusion error: {e}")
    return None

def make_trade_ideas(top_items, gfs_ind, price, eia_text, yandexgpt_cfg):
    """Внутридневные рекомендации на основе AI-анализа."""
    api_key = os.getenv(yandexgpt_cfg.get("api_key_env", ""), "")
    folder_id = os.getenv(yandexgpt_cfg.get("folder_id_env", ""), "")
    if not api_key or not folder_id:
        return None

    news_block = ""
    for i, it in enumerate(top_items, 1):
        a = it.get("analysis", {})
        news_block += f"{i}. {it['title']} "
        news_block += f"[{a.get('impact')}, {a.get('overall_score', 0)}]\n"

    price_line = ""
    if price:
        price_line = (f"Текущая цена фьючерса NG: {price['last']} "
                      f"USD/MMBtu, изменение за день {price['chg']:+.3f}.\n")

    gfs_line = ""
    if gfs_ind:
        gfs_line = f"Индикатор GFS: {gfs_ind['text']}\n"

    eia_line = ""
    if eia_text:
        eia_line = f"Консенсус по отчёту EIA: {eia_text}\n"

    prompt = (
        "Ты — внутридневной трейдер фьючерсами на природный газ "
        "(Henry Hub, NG).\n"
        f"Топ новостей:\n{news_block}\n"
        f"{price_line}{gfs_line}{eia_line}\n"
        "Сформируй 1-2 рекомендации для внутридневной торговли "
        "(горизонт — до конца торговой сессии).\n"
        "Для каждой: направление (long / short / вне рынка), "
        "обоснование на русском (1-2 предложения), ориентировочные "
        "уровни входа/цели/стопа относительно текущей цены.\n"
        "Если картина неясная — рекомендуй 'вне рынка'.\n"
        'Верни JSON строго в формате: '
        '{"ideas": [{"direction": "long", '
        '"rationale_ru": "...", '
        '"levels_ru": "вход 2.85, цель 2.95, стоп 2.78"}], '
        '"risk_note_ru": "..."}'
    )

    try:
        model = yandexgpt_cfg.get("model", "yandexgpt-lite")
        text = _call_yandexgpt(api_key, folder_id, prompt, model)
        parsed = _parse_json_response(text)
        if parsed:
            return parsed
    except Exception as e:
        logger.error(f"Trade ideas error: {e}")
    return None

def make_eia_consensus(previews, gfs_ind, yandexgpt_cfg):
    """Консенсус-прогноз аналитиков по отчёту EIA Storage."""
    api_key = os.getenv(yandexgpt_cfg.get("api_key_env", ""), "")
    folder_id = os.getenv(yandexgpt_cfg.get("folder_id_env", ""), "")
    if not api_key or not folder_id:
        return None

    if previews:
        prev_block = "\n".join(previews[:6])
        src_note = "Опирайся на найденные превью аналитиков:"
    else:
        prev_block = "(превью аналитиков не найдено)"
        src_note = ("Превью не найдены. Дай оценку AI на основе "
                    "сезонности и погоды:")

    gfs_line = ""
    if gfs_ind:
        gfs_line = f"Погода по GFS: {gfs_ind['text']}\n"

    prompt = (
        "Ты — аналитик рынка природного газа США.\n"
        f"{src_note}\n{prev_block}\n\n"
        f"{gfs_line}"
        "Сформулируй консенсус-прогноз аналитиков к ближайшему отчёту "
        "EIA Natural Gas Storage Report: ожидаемое изменение запасов "
        "за неделю (Bcf, со знаком), диапазон оценок, сравнение с "
        "прошлой неделей и 5-летним средним (если данные есть). "
        "Если данных нет — дай обоснованную оценку AI. "
        "3-4 предложения на русском.\n"
        'Верни JSON строго в формате: {"consensus_ru": "..."}'
    )

    try:
        model = yandexgpt_cfg.get("model", "yandexgpt-lite")
        text = _call_yandexgpt(api_key, folder_id, prompt, model)
        parsed = _parse_json_response(text)
        if parsed:
            return parsed.get("consensus_ru")
    except Exception as e:
        logger.error(f"EIA consensus error: {e}")
    return None