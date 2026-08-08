import os
import re
import json
import time
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
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

3. Дать итоговый вывод по новости: bullish (бычий) / bearish (медвежий) / neutral (нейтральный).
4. Написать краткий аналитический комментарий на русском языке.

Формат ответа СТРОГО в JSON без markdown-обёртки:
{
  "is_relevant": true,
  "impact": "bullish / bearish / neutral",
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
  "comment_ru": "Аналитический комментарий на русском, 3-5 предложений о влиянии на Henry Hub"
}

Если новость НЕ относится к природному газу, Henry Hub или перечисленным факторам — верни:
{"is_relevant": false, "impact": "neutral", "scores": {"production": 0, "storage": 0, "demand": 0, "alternatives": 0, "weather": 0, "geopolitics": 0, "oil": 0}, "overall_score": 0, "comment_ru": ""}
"""


def _parse_json_response(text: str):
    """Пытается извлечь JSON из ответа LLM."""
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


def _call_yandexgpt(api_key: str, folder_id: str, user_prompt: str, 
                     model: str = "yandexgpt-lite") -> str:
    """Вызывает YandexGPT API для анализа одной новости."""
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
    alternatives = data.get("result", {}).get("alternatives", [])
    
    if not alternatives:
        return ""

    return alternatives[0].get("message", {}).get("text", "")


def analyze_news(items: list, yandexgpt_cfg: dict) -> list:
    """Анализирует новости через YandexGPT API."""
    # Читаем ключи из переменных окружения
    api_key = os.getenv(yandexgpt_cfg.get("api_key_env", ""), "")
    folder_id = os.getenv(yandexgpt_cfg.get("folder_id_env", ""), "")
    
    if not api_key or not folder_id or not items:
        logger.warning("YandexGPT not configured or no items to analyze")
        return items

    model = yandexgpt_cfg.get("model", "yandexgpt-lite")
    max_analyze = yandexgpt_cfg.get("max_news_to_analyze", 15)

    analyzed_items = []
    items_to_analyze = items[:max_analyze]

    for i, item in enumerate(items_to_analyze, 1):
        logger.info(f"[{i}/{len(items_to_analyze)}] Analyzing: {item['title'][:50]}...")

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
                logger.warning(f"  ⚠ Could not parse LLM response")
                continue

            if not parsed.get("is_relevant", False):
                logger.info(f"  ⚠ Skipped as irrelevant")
                continue

            scores = parsed.get("scores", {})
            overall = sum(int(v) for v in scores.values() if isinstance(v, (int, float)))

            item["analysis"] = {
                "impact": parsed.get("impact", "neutral"),
                "scores": scores,
                "overall_score": overall,
                "comment_ru": parsed.get("comment_ru", ""),
            }

            analyzed_items.append(item)
            logger.info(f"  ✓ {parsed.get('impact', 'neutral')} (score: {overall:+d})")

            time.sleep(0.5)

        except requests.exceptions.HTTPError as e:
            logger.error(f"  ✗ YandexGPT API error: {e}")
            if e.response is not None:
                logger.error(f"    Response: {e.response.text[:500]}")
        except Exception as e:
            logger.error(f"  ✗ Error: {e}")
            time.sleep(2)

    remaining_items = items[max_analyze:]
    analyzed_items.extend(remaining_items)

    analyzed_items.sort(
        key=lambda x: abs(x.get("analysis", {}).get("overall_score", 0)),
        reverse=True,
    )

    return analyzed_items