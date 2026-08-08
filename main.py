import os
import re
import ssl
import smtplib
import yaml
import feedparser
import time
import urllib.parse
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
import calendar
import logging
from deep_translator import GoogleTranslator
from duckduckgo_search import DDGS
from duckduckgo_search.exceptions import DuckDuckGoSearchException

from analyzer import analyze_news

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_date(entry):
    for field in ("published_parsed", "updated_parsed"):
        t = entry.get(field)
        if t:
            try:
                timestamp = calendar.timegm(t)
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except Exception:
                pass
    return None


def normalize_url(url):
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return clean_url.rstrip('/').lower()


def matches_us_gas_filter(text, filter_cfg):
    gas_keywords = filter_cfg.get("gas_keywords", [])
    us_markers = filter_cfg.get("us_markers", [])
    text_lower = text.lower()
    has_gas = any(kw.lower() in text_lower for kw in gas_keywords)
    has_us = any(marker.lower() in text_lower for marker in us_markers)
    return has_gas and has_us


def translate_text(text, translator):
    if not text or not text.strip():
        return text
    try:
        return translator.translate(text)
    except Exception as e:
        logger.warning(f"Translation error: {e}")
        return text


def collect_from_rss(config):
    feeds = config.get("rss_feeds", [])
    max_age_hours = config.get("max_age_hours", 24)
    max_items_per_feed = config.get("max_items_per_feed", 30)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    items = []
    for feed_cfg in feeds:
        name = feed_cfg.get("name", "Unknown")
        url = feed_cfg.get("url")
        if not url:
            continue

        logger.info(f"[RSS] Fetching: {name}")
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logger.error(f"Error fetching {name}: {e}")
            continue

        count = 0
        for entry in feed.entries[:max_items_per_feed]:
            title = entry.get("title", "")
            link = entry.get("link") or entry.get("id") or ""
            summary = entry.get("summary", "") or entry.get("description", "")

            if not title or not link:
                continue

            published = parse_date(entry)
            if published and published < cutoff:
                continue

            items.append({
                "source": name,
                "title": title,
                "link": link,
                "published": published,
                "summary": strip_html(summary)[:300],
                "type": "rss",
            })
            count += 1
        logger.info(f"  [RSS] Found {count} raw items in {name}")

    return items


def search_web_news(config):
    queries = config.get("search_queries", [])
    max_results = config.get("max_results_per_query", 15)
    items = []

    if not queries:
        return items

    logger.info("[WEB] Searching DuckDuckGo News...")

    try:
        ddgs = DDGS(headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

        for query in queries:
            logger.info(f"  Query: {query}")
            success = False
            for attempt in range(3):
                try:
                    results = ddgs.news(
                        keywords=query,
                        region="wt-wt",
                        safesearch="off",
                        timelimit="d",
                        max_results=max_results,
                    )
                    for r in results:
                        title = r.get("title", "")
                        link = r.get("url", "")
                        summary = r.get("body", "")
                        source = r.get("source", "Web Search")
                        if not title or not link:
                            continue
                        items.append({
                            "source": source,
                            "title": title,
                            "link": link,
                            "published": datetime.now(timezone.utc),
                            "summary": strip_html(summary)[:300],
                            "type": "web",
                        })
                    logger.info(f"  OK Got {len(results)} results for '{query}'")
                    success = True
                    break
                except DuckDuckGoSearchException as e:
                    if "Ratelimit" in str(e) or "403" in str(e):
                        wait_time = (attempt + 1) * 5
                        logger.warning(f"  Ratelimit attempt {attempt+1}/3, wait {wait_time}s")
                        time.sleep(wait_time)
                    else:
                        logger.warning(f"  DDG error: {e}")
                        break
                except Exception as e:
                    logger.warning(f"  Unexpected error: {e}")
                    break

            if not success:
                logger.info(f"  Skipped query '{query}' due to blocking")
            time.sleep(3)

    except Exception as e:
        logger.error(f"Failed to initialize DDGS: {e}")

    logger.info(f"[WEB] Found {len(items)} items from web search")
    return items


def collect_from_bing_rss(config):
    queries = config.get("bing_rss_queries", [])
    max_age_hours = config.get("max_age_hours", 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items = []

    if not queries:
        return items

    logger.info("[BING] Fetching Bing News RSS...")

    for query in queries:
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://www.bing.com/news/search?q={encoded_query}&format=rss"
        logger.info(f"  Query: {query}")

        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logger.error(f"  Error fetching Bing RSS for '{query}': {e}")
            continue

        count = 0
        for entry in feed.entries[:15]:
            title = entry.get("title", "")
            link = entry.get("link") or entry.get("id") or ""
            summary = entry.get("summary", "") or entry.get("description", "")
            if not title or not link:
                continue
            published = parse_date(entry)
            if published and published < cutoff:
                continue
            items.append({
                "source": "Bing News",
                "title": title,
                "link": link,
                "published": published or datetime.now(timezone.utc),
                "summary": strip_html(summary)[:300],
                "type": "bing",
            })
            count += 1

        logger.info(f"  OK Got {count} items for '{query}'")
        time.sleep(1)

    logger.info(f"[BING] Found {len(items)} items total")
    return items


def collect_and_filter_news(config, translator=None):
    rss_items = collect_from_rss(config)
    bing_items = collect_from_bing_rss(config)
    web_items = search_web_news(config)

    logger.info(f"Raw counts: RSS={len(rss_items)}, Bing={len(bing_items)}, Web={len(web_items)}")

    all_raw_items = rss_items + bing_items + web_items
    seen_urls = set()
    unique_items = []
    filter_cfg = config.get("filter", {})

    for item in all_raw_items:
        norm_url = normalize_url(item["link"])
        if not norm_url or norm_url in seen_urls:
            continue

        full_text = f"{item['title']} {item['summary']}"
        if not matches_us_gas_filter(full_text, filter_cfg):
            continue

        seen_urls.add(norm_url)

        translated_title = item["title"]
        translated_summary = item["summary"]

        if translator:
            translated_title = translate_text(item["title"], translator)
            translated_summary = translate_text(item["summary"], translator)
            time.sleep(0.3)

        pub_str = item["published"].strftime("%d.%m %H:%M") if isinstance(item["published"], datetime) else ""

        unique_items.append({
            "source": item["source"],
            "title": translated_title,
            "original_title": item["title"],
            "link": item["link"],
            "published": pub_str,
            "summary": translated_summary,
            "datetime_obj": item["published"] if isinstance(item["published"], datetime) else datetime.min.replace(tzinfo=timezone.utc),
        })

    unique_items.sort(key=lambda x: x["datetime_obj"], reverse=True)
    return unique_items


def build_email(items, config):
    email_cfg = config["email"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = email_cfg["subject"].format(date=now)

    username = os.getenv(email_cfg.get("username_env", ""), "")

    text_lines = [f"US Gas Digest (AI) - {now}", "=" * 50, ""]

    if not items:
        text_lines.append("Нет релевантных новостей про газ в США за последние 24 часа.")
    else:
        text_lines.append(f"Найдено новостей: {len(items)}")
        text_lines.append("")

        for i, item in enumerate(items, 1):
            text_lines.append(f"{i}. {item['title']}")
            text_lines.append(f"   (Оригинал: {item['original_title']})")
            text_lines.append(f"   Источник: {item['source']} | Дата: {item['published']}")
            text_lines.append(f"   Ссылка: {item['link']}")
            text_lines.append(f"   Кратко: {item['summary']}")

            if "analysis" in item:
                analysis = item["analysis"]
                impact = analysis.get("impact", "neutral").upper()
                score = analysis.get("overall_score", 0)
                comment = analysis.get("comment_ru", "")
                text_lines.append(f"   AI-анализ: {impact} (оценка: {score:+d})")
                if comment:
                    text_lines.append(f"   Комментарий: {comment}")

            text_lines.append("")

    text_body = "\n".join(text_lines)

    html_items = []
    for i, item in enumerate(items, 1):
        analysis_html = ""
        if "analysis" in item:
            analysis = item["analysis"]
            impact = analysis.get("impact", "neutral")
            score = analysis.get("overall_score", 0)
            comment = analysis.get("comment_ru", "")
            scores = analysis.get("scores", {})

            if impact == "bullish":
                badge_color = "#2e7d32"
                badge_text = "БЫЧИЙ"
            elif impact == "bearish":
                badge_color = "#c62828"
                badge_text = "МЕДВЕЖИЙ"
            else:
                badge_color = "#616161"
                badge_text = "НЕЙТРАЛЬНЫЙ"

            factor_labels = {
                "production": "Добыча",
                "storage": "Запасы",
                "demand": "Спрос",
                "alternatives": "Альтернативы",
                "weather": "Погода",
                "geopolitics": "Геополитика",
                "oil": "Нефть",
            }

            score_cells = ""
            for key, label in factor_labels.items():
                v = int(scores.get(key, 0) or 0)
                color = "#2e7d32" if v > 0 else ("#c62828" if v < 0 else "#616161")
                score_cells += (
                    f'<td style="text-align:center;font-size:12px;padding:4px;">'
                    f'<div style="color:#999;font-size:10px;">{label}</div>'
                    f'<div style="color:{color};font-weight:bold;">{v:+d}</div>'
                    f'</td>'
                )

            comment_html = ""
            if comment:
                comment_html = f'<div style="color:#333;font-size:13px;line-height:1.5;margin-top:8px;"><strong>Комментарий:</strong> {comment}</div>'

            analysis_html = f'<div style="background:#f5f5f5;padding:12px;margin-top:10px;border-left:4px solid {badge_color};"><div style="margin-bottom:8px;"><span style="background:{badge_color};color:white;padding:4px 8px;border-radius:4px;font-size:12px;font-weight:bold;">{badge_text}</span><span style="color:#666;font-size:12px;margin-left:8px;">Общая оценка: <strong>{score:+d}</strong></span></div><table style="width:100%;border-collapse:collapse;margin:8px 0;"><tr>{score_cells}</tr></table>{comment_html}</div>'

        html_items.append(f'<div style="margin-bottom: 20px; padding-bottom: 20px; border-bottom: 1px solid #eee;"><h3 style="margin: 0 0 8px 0;"><a href="{item["link"]}" style="color: #1565c0; text-decoration: none;">{i}. {item["title"]}</a></h3><div style="color: #999; font-size: 11px; margin-bottom: 4px;">Оригинал: {item["original_title"]}</div><div style="color: #666; font-size: 12px; margin-bottom: 8px;"><b>{item["source"]}</b> · {item["published"]}</div><div style="color: #333; line-height: 1.5;">{item["summary"]}</div>{analysis_html}</div>')

    items_html = "".join(html_items) if items else "<p>Нет релевантных новостей про газ в США за последние 24 часа.</p>"

    html_body = '<html><head><meta charset="utf-8"></head><body '
    html_body += 'style="font-family: Arial, sans-serif; '
    html_body += 'max-width: 700px; margin: 0 auto; padding: 20px;">'
    html_body += '<h1 style="color: #1565c0;">US Gas Digest (AI)</h1>'
    html_body += '<p style="color: #666;">Отчет за ' + now + '</p>'
    html_body += '<p style="color: #666;">Найдено новостей: <strong>'
    html_body += str(len(items)) + '</strong></p>'
    html_body += items_html + '</body></html>'

    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = email_cfg["to_addr"]
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    return msg


def send_email(msg, config):
    email_cfg = config["email"]
    username = os.getenv(email_cfg.get("username_env", ""), "")
    password = os.getenv(email_cfg.get("password_env", ""), "")

    if not username or not password:
         raise RuntimeError("SMTP credentials missing")

    logger.info(f"Sending email to {email_cfg['to_addr']}...")

    with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(username, password)
        server.send_message(msg)

    logger.info("Email sent successfully!")


def main():
    logger.info("Starting US Gas Digest...")

    config = load_config()

    translator = None
    if config.get("translate", False):
        logger.info("Initializing translator...")
        translator = GoogleTranslator(source="en", target="ru")

    logger.info("Collecting news (RSS + Web)...")
    items = collect_and_filter_news(config, translator)
    logger.info(f"Total unique matching items: {len(items)}")

    yandexgpt_cfg = config.get("yandexgpt", {})
    api_key = os.getenv(yandexgpt_cfg.get("api_key_env", ""), "")
    folder_id = os.getenv(yandexgpt_cfg.get("folder_id_env", ""), "")

    if api_key and folder_id:
        logger.info("Analyzing news with YandexGPT...")
        items = analyze_news(items, yandexgpt_cfg)
        logger.info(f"Analyzed items: {len([i for i in items if 'analysis' in i])}")
    else:
        logger.warning("YandexGPT not configured, skipping AI analysis")

    msg = build_email(items, config)
    send_email(msg, config)

    logger.info("Done!")


if __name__ == "__main__":
    main()
