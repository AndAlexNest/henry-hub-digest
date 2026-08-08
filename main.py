import os
import re
import ssl
import json
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

from analyzer import analyze_news, make_overall_conclusion
from gfs import fetch_gfs_forecast, gfs_weather_assessment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SEEN_FILE = "seen_urls.json"
MAX_SEEN = 2000
TOP_N = 4


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
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return clean.rstrip("/").lower()


def matches_us_gas_filter(text, filter_cfg):
    gas_kw = filter_cfg.get("gas_keywords", [])
    us_mk = filter_cfg.get("us_markers", [])
    low = text.lower()
    has_gas = any(k.lower() in low for k in gas_kw)
    has_us = any(m.lower() in low for m in us_mk)
    return has_gas and has_us


def translate_text(text, translator):
    if not text or not text.strip():
        return text
    try:
        return translator.translate(text)
    except Exception as e:
        logger.warning(f"Translation error: {e}")
        return text


def load_seen_urls():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception as e:
        logger.warning(f"Could not load seen urls: {e}")
        return set()


def save_seen_urls(seen):
    try:
        data = sorted(seen)
        if len(data) > MAX_SEEN:
            data = data[-MAX_SEEN:]
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        logger.info(f"Saved {len(data)} seen urls")
    except Exception as e:
        logger.warning(f"Could not save seen urls: {e}")


def collect_from_rss(config):
    feeds = config.get("rss_feeds", [])
    max_age = config.get("max_age_hours", 24)
    max_per = config.get("max_items_per_feed", 30)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)

    items = []
    for fc in feeds:
        name = fc.get("name", "Unknown")
        url = fc.get("url")
        if not url:
            continue

        logger.info(f"[RSS] Fetching: {name}")
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logger.error(f"Error fetching {name}: {e}")
            continue

        count = 0
        for entry in feed.entries[:max_per]:
            title = entry.get("title", "")
            link = entry.get("link") or entry.get("id") or ""
            summ = entry.get("summary", "")
            if not summ:
                summ = entry.get("description", "")

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
                "summary": strip_html(summ)[:300],
                "type": "rss",
            })
            count += 1
        logger.info(f"  [RSS] {name}: {count} raw items")

    return items


def search_web_news(config):
    queries = config.get("search_queries", [])
    max_res = config.get("max_results_per_query", 15)
    items = []

    if not queries:
        return items

    logger.info("[WEB] Searching DuckDuckGo News...")

    try:
        ddgs = DDGS(headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8",
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
                        max_results=max_res,
                    )
                    for r in results:
                        title = r.get("title", "")
                        link = r.get("url", "")
                        body = r.get("body", "")
                        src = r.get("source", "Web Search")
                        if not title or not link:
                            continue
                        items.append({
                            "source": src,
                            "title": title,
                            "link": link,
                            "published": datetime.now(timezone.utc),
                            "summary": strip_html(body)[:300],
                            "type": "web",
                        })
                    logger.info(f"  Got {len(results)} results")
                    success = True
                    break
                except DuckDuckGoSearchException as e:
                    if "Ratelimit" in str(e) or "403" in str(e):
                        wait = (attempt + 1) * 5
                        logger.warning(f"  Ratelimit, wait {wait}s")
                        time.sleep(wait)
                    else:
                        logger.warning(f"  DDG error: {e}")
                        break
                except Exception as e:
                    logger.warning(f"  Unexpected error: {e}")
                    break

            if not success:
                logger.info(f"  Skipped query: {query}")
            time.sleep(3)

    except Exception as e:
        logger.error(f"Failed to initialize DDGS: {e}")

    logger.info(f"[WEB] {len(items)} items from web search")
    return items


def collect_from_bing_rss(config):
    queries = config.get("bing_rss_queries", [])
    max_age = config.get("max_age_hours", 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)
    items = []

    if not queries:
        return items

    logger.info("[BING] Fetching Bing News RSS...")

    for query in queries:
        enc = urllib.parse.quote_plus(query)
        url = f"https://www.bing.com/news/search?q={enc}&format=rss"
        logger.info(f"  Query: {query}")

        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logger.error(f"  Bing RSS error: {e}")
            continue

        count = 0
        for entry in feed.entries[:15]:
            title = entry.get("title", "")
            link = entry.get("link") or entry.get("id") or ""
            summ = entry.get("summary", "")
            if not summ:
                summ = entry.get("description", "")
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
                "summary": strip_html(summ)[:300],
                "type": "bing",
            })
            count += 1

        logger.info(f"  Got {count} items")
        time.sleep(1)

    logger.info(f"[BING] {len(items)} items total")
    return items


def collect_and_filter_news(config, translator=None):
    rss_items = collect_from_rss(config)
    bing_items = collect_from_bing_rss(config)
    web_items = search_web_news(config)

    n1, n2, n3 = len(rss_items), len(bing_items), len(web_items)
    logger.info(f"Raw counts: RSS={n1}, Bing={n2}, Web={n3}")

    all_raw = rss_items + bing_items + web_items
    seen = set()
    unique = []
    filter_cfg = config.get("filter", {})

    for item in all_raw:
        nu = normalize_url(item["link"])
        if not nu or nu in seen:
            continue

        full_text = f"{item['title']} {item['summary']}"
        if not matches_us_gas_filter(full_text, filter_cfg):
            continue

        seen.add(nu)

        tr_title = item["title"]
        tr_summary = item["summary"]

        if translator:
            tr_title = translate_text(item["title"], translator)
            tr_summary = translate_text(item["summary"], translator)
            time.sleep(0.3)

        pub = item["published"]
        pub_str = pub.strftime("%d.%m %H:%M") if isinstance(pub, datetime) else ""
        dt = pub if isinstance(pub, datetime) else datetime.min.replace(tzinfo=timezone.utc)

        unique.append({
            "source": item["source"],
            "title": tr_title,
            "original_title": item["title"],
            "link": item["link"],
            "published": pub_str,
            "summary": tr_summary,
            "datetime_obj": dt,
        })

    unique.sort(key=lambda x: x["datetime_obj"], reverse=True)
    return unique


def build_email(items, config, gfs=None, weather_note="", conclusion=None):
    email_cfg = config["email"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = email_cfg["subject"].format(date=now)
    username = os.getenv(email_cfg.get("username_env", ""), "")

    # ---------- ТЕКСТ ----------
    tl = [f"US Gas Digest - {now}", "=" * 50, ""]

    if conclusion:
        tl.append("== ОБЩИЙ ВЫВОД ==")
        tl.append(conclusion.get("conclusion_ru", ""))
        tl.append(f"Направление рынка: {conclusion.get('market_bias', 'neutral')}")
        tl.append("")

    if gfs:
        tl.append("== ПРОГНОЗ GFS (10 дней) ==")
        tl.append(f"HDD: {gfs['hdd10']} | CDD: {gfs['cdd10']}")
        tl.append(weather_note)
        for c in gfs["cities"]:
            row = f"  {c['name']}: {c['avg_temp_f']}F"
            row += f", HDD {c['hdd10']}, CDD {c['cdd10']}"
            tl.append(row)
        tl.append("")

    tl.append(f"== ТОП НОВОСТЕЙ ({len(items)}) ==")
    tl.append("")
    for i, item in enumerate(items, 1):
        tl.append(f"{i}. {item['title']}")
        tl.append(f"   (Оригинал: {item['original_title']})")
        tl.append(f"   Источник: {item['source']} | Дата: {item['published']}")
        tl.append(f"   Ссылка: {item['link']}")
        tl.append(f"   Кратко: {item['summary']}")
        if "analysis" in item:
            a = item["analysis"]
            imp = a.get("impact", "neutral").upper()
            sc = a.get("overall_score", 0)
            tl.append(f"   AI: {imp} (оценка: {sc:+d})")
            if a.get("comment_ru"):
                tl.append(f"   Комментарий: {a['comment_ru']}")
        tl.append("")

    text_body = "\n".join(tl)

    # ---------- HTML ----------
    h = []
    h.append('<html><head><meta charset="utf-8"></head>')
    h.append('<body style="font-family: Arial, sans-serif;')
    h.append('max-width: 700px; margin: 0 auto; padding: 20px;">')
    h.append('<h1 style="color: #1565c0;">US Gas Digest</h1>')
    h.append(f'<p style="color: #666;">Отчет за {now}</p>')

    if conclusion:
        bias = conclusion.get("market_bias", "neutral")
        if bias == "bullish":
            bc, bt = "#2e7d32", "БЫЧИЙ"
        elif bias == "bearish":
            bc, bt = "#c62828", "МЕДВЕЖИЙ"
        else:
            bc, bt = "#616161", "НЕЙТРАЛЬНЫЙ"
        h.append('<div style="background:#e3f2fd;padding:14px;')
        h.append('border-left:4px solid #1565c0;margin:16px 0;">')
        h.append('<h2 style="margin:0 0 8px 0;font-size:16px;">Общий вывод</h2>')
        concl = conclusion.get("conclusion_ru", "")
        h.append(f'<p style="margin:0 0 8px 0;">{concl}</p>')
        h.append(f'<span style="background:{bc};color:white;padding:4px 8px;')
        h.append('border-radius:4px;font-size:12px;font-weight:bold;">')
        h.append(f'{bt}</span></div>')

    if gfs:
        h.append('<div style="background:#f5f5f5;padding:14px;')
        h.append('border-left:4px solid #0288d1;margin:16px 0;">')
        h.append('<h2 style="margin:0 0 8px 0;font-size:16px;">')
        h.append('Обновление прогноза GFS (10 дней)</h2>')
        h.append('<p style="margin:0 0 8px 0;">')
        h.append(f'HDD: <b>{gfs["hdd10"]}</b> | CDD: <b>{gfs["cdd10"]}</b></p>')
        h.append(f'<p style="margin:0 0 8px 0;">{weather_note}</p>')
        h.append('<table style="width:100%;font-size:12px;')
        h.append('border-collapse:collapse;">')
        h.append('<tr style="color:#999;">')
        h.append('<td>Город</td><td>Ср. темп.</td><td>HDD</td><td>CDD</td>')
        h.append('</tr>')
        for c in gfs["cities"]:
            h.append('<tr>')
            h.append(f'<td>{c["name"]}</td>')
            h.append(f'<td>{c["avg_temp_f"]}F</td>')
            h.append(f'<td>{c["hdd10"]}</td>')
            h.append(f'<td>{c["cdd10"]}</td>')
            h.append('</tr>')
        h.append('</table></div>')

    for i, item in enumerate(items, 1):
        h.append('<div style="margin-bottom:20px;padding-bottom:20px;')
        h.append('border-bottom:1px solid #eee;">')
        h.append(f'<h3 style="margin:0 0 8px 0;"><a href="{item["link"]}"')
        h.append('style="color:#1565c0;text-decoration:none;">')
        h.append(f'{i}. {item["title"]}</a></h3>')
        h.append('<div style="color:#999;font-size:11px;margin-bottom:4px;">')
        h.append(f'Оригинал: {item["original_title"]}</div>')
        h.append('<div style="color:#666;font-size:12px;margin-bottom:8px;">')
        h.append(f'<b>{item["source"]}</b> · {item["published"]}</div>')
        h.append('<div style="color:#333;line-height:1.5;">')
        h.append(f'{item["summary"]}</div>')
        if "analysis" in item:
            a = item["analysis"]
            imp = a.get("impact", "neutral")
            sc = a.get("overall_score", 0)
            if imp == "bullish":
                bc, bt = "#2e7d32", "БЫЧИЙ"
            elif imp == "bearish":
                bc, bt = "#c62828", "МЕДВЕЖИЙ"
            else:
                bc, bt = "#616161", "НЕЙТРАЛЬНЫЙ"
            h.append('<div style="background:#f5f5f5;padding:10px;')
            h.append(f'margin-top:10px;border-left:4px solid {bc};">')
            h.append(f'<span style="background:{bc};color:white;')
            h.append('padding:3px 7px;border-radius:4px;')
            h.append(f'font-size:11px;font-weight:bold;">{bt}</span>')
            h.append('<span style="color:#666;font-size:12px;')
            h.append(f'margin-left:8px;">Оценка: <b>{sc:+d}</b></span>')
            if a.get("comment_ru"):
                h.append('<div style="color:#333;font-size:13px;')
                h.append(f'margin-top:8px;">{a["comment_ru"]}</div>')
            h.append('</div>')
        h.append('</div>')

    if not items:
        h.append('<p>Значимых новостей за последние 24 часа нет.</p>')

    h.append('</body></html>')
    html_body = "\n".join(h)

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
    logger.info(f"Unique items: {len(items)}")

    seen_urls = load_seen_urls()
    new_items = []
    new_urls = set()
    for it in items:
        nu = normalize_url(it["link"])
        if nu and nu not in seen_urls:
            new_items.append(it)
            new_urls.add(nu)

    logger.info(f"New items (not sent before): {len(new_items)}")

    yandexgpt_cfg = config.get("yandexgpt", {})
    api_key = os.getenv(yandexgpt_cfg.get("api_key_env", ""), "")
    folder_id = os.getenv(yandexgpt_cfg.get("folder_id_env", ""), "")

    top_items = []
    if new_items and api_key and folder_id:
        logger.info("Analyzing news with YandexGPT...")
        analyzed = analyze_news(new_items, yandexgpt_cfg)
        scored = [i for i in analyzed if "analysis" in i]
        scored.sort(key=lambda x: abs(x["analysis"]["overall_score"]), reverse=True)
        top_items = scored[:TOP_N]
        logger.info(f"Top items selected: {len(top_items)}")
    else:
        logger.warning("No new items or YandexGPT not configured")

    logger.info("Fetching GFS forecast...")
    gfs = None
    weather_note = ""
    try:
        gfs = fetch_gfs_forecast()
        if gfs:
            weather_note = gfs_weather_assessment(gfs)
    except Exception as e:
        logger.error(f"GFS failed: {e}")

    conclusion = None
    if api_key and folder_id:
        logger.info("Building overall conclusion...")
        conclusion = make_overall_conclusion(top_items, gfs, weather_note, yandexgpt_cfg)

    if not top_items and not gfs:
        logger.info("Nothing to send, skipping email")
        return

    msg = build_email(top_items, config, gfs=gfs, weather_note=weather_note, conclusion=conclusion)
    send_email(msg, config)
    save_seen_urls(seen_urls | new_urls)
    logger.info("Done!")


if __name__ == "__main__":
    main()