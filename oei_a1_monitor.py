#!/usr/bin/env python3
"""
Мониторинг записи на экзамен ÖSD A1 в Австрийском институте (Österreich Institut, Москва).

Что проверяет:
  1. Страницы сайта https://oesterreichinstitut.ru/exam и /exam_day:
     - пропала ли плашка «В ближайшее время мы откроем запись на новые даты»;
     - появились ли новые строки с A1, датами, словами «запись», «места», «резерв» и т.п.
  2. Публичный Telegram-канал института https://t.me/s/oeimsk
     (новые даты экзаменов институт объявляет именно там):
     - новые посты, где упоминаются экзамен / A1 / запись / даты.

Уведомления: в консоль + (опционально) в Telegram через вашего бота.

Только стандартная библиотека Python 3.8+, ничего ставить не нужно.

Запуск:
  python3 oei_a1_monitor.py                 # проверять каждые 10 минут, бесконечно
  python3 oei_a1_monitor.py --interval 5    # каждые 5 минут
  python3 oei_a1_monitor.py --once          # одна проверка (для cron / планировщика)
  python3 oei_a1_monitor.py --test-notify   # проверить, что уведомления доходят

Уведомления в Telegram (рекомендуется):
  1. Напишите @BotFather -> /newbot -> получите токен.
  2. Напишите своему боту любое сообщение.
  3. Откройте https://api.telegram.org/bot<ТОКЕН>/getUpdates и найдите "chat":{"id": ...}
  4. Задайте переменные окружения (или параметры --tg-token / --tg-chat):
       export TG_BOT_TOKEN="123456:ABC..."
       export TG_CHAT_ID="123456789"
"""

import argparse
import difflib
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------- настройки

SITE_PAGES = [
    "https://oesterreichinstitut.ru/exam",
    "https://oesterreichinstitut.ru/exam_day",
]
TELEGRAM_CHANNEL = "oeimsk"  # https://t.me/oeimsk

# Плашка, которая висит, пока запись закрыта. Если она исчезнет — повод проверить сайт.
CLOSED_NOTICE = re.compile(r"откроем\s+запись", re.I)

# Что считаем «интересным» в новых строках на сайте и в постах канала.
LEVEL_RE = re.compile(r"\bA\s?1\b|\bА\s?1\b|ZA1|Zertifikat\s*A1", re.I)  # латинская и кириллическая А
KEYWORDS_RE = re.compile(
    r"запис|регистрац|свободн|мест[ао]?\b|резерв|лист\s+ожидания|"
    r"нов(ые|ая)\s+дат|экзамен|ÖSD|OSD|ОСД",
    re.I,
)
DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}(\.\d{2,4})?\b")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
STATE_FILE = Path(__file__).with_name("oei_monitor_state.json")


# ---------------------------------------------------------------- утилиты

def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


class TextExtractor(HTMLParser):
    """Достаёт видимый текст из HTML построчно (без script/style)."""

    SKIP = {"script", "style", "noscript", "svg", "head", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "td", "th", "ul", "ol", "table", "a", "span"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def lines(self):
        text = "".join(self.parts).replace("\xa0", " ")
        out = []
        for line in text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line and (not out or out[-1] != line):
                out.append(line)
        return out


def page_lines(html: str):
    p = TextExtractor()
    p.feed(html)
    return p.lines()


def is_interesting(text: str) -> bool:
    return bool(LEVEL_RE.search(text) or KEYWORDS_RE.search(text) or DATE_RE.search(text))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"pages": {}, "tg_last_id": 0}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- уведомления

class Notifier:
    def __init__(self, tg_token=None, tg_chat=None):
        self.tg_token = tg_token
        self.tg_chat = tg_chat

    def send(self, text: str) -> None:
        print("\n" + "=" * 60 + "\n🔔 " + text + "\n" + "=" * 60 + "\n", flush=True)
        print("\a", end="", flush=True)  # звуковой сигнал терминала
        if self.tg_token and self.tg_chat:
            self._telegram(text)

    def _telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.tg_chat,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }).encode()
        try:
            urllib.request.urlopen(url, data=data, timeout=20).read()
        except Exception as e:
            log(f"Не удалось отправить в Telegram: {e}")


# ---------------------------------------------------------------- проверка сайта

def check_site(state: dict, notifier: Notifier) -> None:
    for url in SITE_PAGES:
        try:
            lines = page_lines(fetch(url))
        except Exception as e:
            log(f"Ошибка загрузки {url}: {e}")
            continue
        if len(lines) < 5:
            log(f"{url}: подозрительно мало текста, пропускаю")
            continue

        text = "\n".join(lines)
        digest = hashlib.sha256(text.encode()).hexdigest()
        notice_now = bool(CLOSED_NOTICE.search(text))
        a1_lines = [l for l in lines if LEVEL_RE.search(l)]

        prev = state["pages"].get(url)
        if prev is None:
            log(f"{url}: первая проверка, запоминаю. "
                f"Плашка «откроем запись»: {'есть' if notice_now else 'нет'}; "
                f"строк с A1: {len(a1_lines)}")
        elif prev["hash"] == digest:
            log(f"{url}: без изменений")
        else:
            old_lines = prev["lines"]
            added = [l[1:].strip() for l in difflib.ndiff(old_lines, lines)
                     if l.startswith("+ ")]
            interesting = [l for l in added if is_interesting(l)]
            log(f"{url}: страница изменилась (+{len(added)} строк, "
                f"интересных: {len(interesting)})")

            msgs = []
            if prev.get("notice") and not notice_now:
                msgs.append("❗ Плашка «В ближайшее время мы откроем запись» ИСЧЕЗЛА — "
                            "похоже, запись открыта!")
            new_a1 = [l for l in interesting if LEVEL_RE.search(l)]
            if new_a1:
                msgs.append("Новые строки с A1:\n• " + "\n• ".join(new_a1[:15]))
            other = [l for l in interesting if l not in new_a1]
            if other:
                msgs.append("Другие изменения:\n• " + "\n• ".join(other[:10]))
            if msgs:
                notifier.send("Сайт Австрийского института:\n" + url + "\n\n"
                              + "\n\n".join(msgs))

        state["pages"][url] = {"hash": digest, "lines": lines, "notice": notice_now}


# ---------------------------------------------------------------- проверка Telegram-канала

POST_RE = re.compile(r'data-post="%s/(\d+)"' % TELEGRAM_CHANNEL)


def parse_channel(html: str):
    """Возвращает [(id, текст)] постов из https://t.me/s/<channel>."""
    posts = []
    chunks = re.split(r'(?=<div class="tgme_widget_message_wrap)', html)
    for chunk in chunks:
        m = POST_RE.search(chunk)
        if not m:
            continue
        tm = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            chunk, re.S)
        body = tm.group(1) if tm else ""
        body = re.sub(r"<br\s*/?>", "\n", body)
        text = "\n".join(page_lines(body)) if body else ""
        posts.append((int(m.group(1)), text))
    return posts


def check_telegram(state: dict, notifier: Notifier) -> None:
    url = f"https://t.me/s/{TELEGRAM_CHANNEL}"
    try:
        posts = parse_channel(fetch(url))
    except Exception as e:
        log(f"Ошибка загрузки Telegram-канала: {e}")
        return
    if not posts:
        log("Telegram: посты не найдены (возможно, изменилась вёрстка)")
        return

    last = state.get("tg_last_id", 0)
    newest = max(p[0] for p in posts)
    if last == 0:
        log(f"Telegram: первая проверка, последний пост #{newest}")
    else:
        new_posts = sorted(p for p in posts if p[0] > last)
        log(f"Telegram: новых постов {len(new_posts)}")
        for pid, text in new_posts:
            if not text:
                continue
            a1 = bool(LEVEL_RE.search(text))
            if a1 or KEYWORDS_RE.search(text):
                head = "🟢 Пост про A1!" if a1 else "Новый пост про экзамены/запись"
                notifier.send(f"{head}\nhttps://t.me/{TELEGRAM_CHANNEL}/{pid}\n\n"
                              f"{text[:1500]}")
    state["tg_last_id"] = max(last, newest)


# ---------------------------------------------------------------- main

def run_once(notifier: Notifier, use_tg_channel: bool) -> None:
    state = load_state()
    check_site(state, notifier)
    if use_tg_channel:
        check_telegram(state, notifier)
    save_state(state)


def main():
    ap = argparse.ArgumentParser(description="Мониторинг записи на ÖSD A1, Österreich Institut Москва")
    ap.add_argument("--interval", type=float, default=10, help="интервал проверки, минут (по умолчанию 10)")
    ap.add_argument("--once", action="store_true", help="одна проверка и выход (для cron)")
    ap.add_argument("--no-telegram-channel", action="store_true", help="не проверять канал t.me/oeimsk")
    ap.add_argument("--tg-token", default=os.getenv("TG_BOT_TOKEN"), help="токен Telegram-бота")
    ap.add_argument("--tg-chat", default=os.getenv("TG_CHAT_ID"), help="chat_id для уведомлений")
    ap.add_argument("--test-notify", action="store_true", help="отправить тестовое уведомление и выйти")
    args = ap.parse_args()

    notifier = Notifier(args.tg_token, args.tg_chat)
    if not (args.tg_token and args.tg_chat):
        log("Telegram-бот не настроен — уведомления будут только в консоли.")

    if args.test_notify:
        notifier.send("Тест: мониторинг ÖSD A1 работает ✅")
        return

    if args.once:
        run_once(notifier, not args.no_telegram_channel)
        return

    interval = max(args.interval, 2) * 60  # не чаще раза в 2 минуты — не нагружаем сайт
    log(f"Старт. Проверка каждые {interval / 60:.0f} мин. Ctrl+C — выход.")
    while True:
        try:
            run_once(notifier, not args.no_telegram_channel)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"Непредвиденная ошибка: {e}")
        time.sleep(interval + random.uniform(0, 30))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Остановлено.")
