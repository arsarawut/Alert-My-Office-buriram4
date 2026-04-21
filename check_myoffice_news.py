#!/usr/bin/env python3
"""Check My Office news and notify Telegram when new items appear."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import ssl
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPSHandler, HTTPCookieProcessor, Request, build_opener


BASE_URL = "http://209.15.117.206/myoffice/2569/"
LOGIN_URL = urljoin(BASE_URL, "index.php?name=user&file=login")
DEFAULT_NEWS_URL = urljoin(BASE_URL, "index.php?name=tkk4&category=132")
DEFAULT_ARCHIVED_NEWS_URL = urljoin(
    BASE_URL, "index.php?name=tkk4&file=rub&category=132&Page=1"
)
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = PROJECT_DIR / ".env"
DEFAULT_STATE_PATH = PROJECT_DIR / "data" / "seen_news.json"
DEFAULT_LOCK_PATH = PROJECT_DIR / "data" / "check_myoffice_news.lock"


@dataclass
class Link:
    text: str
    url: str


@dataclass
class NewsItem:
    key: str
    news_id: str
    document_number: str
    title: str
    date_text: str
    sender: str
    attachments: list[Link]
    page_url: str
    source: str


@dataclass
class RunResult:
    status: str
    message: str
    pending_count: int
    archived_count: int
    new_count: int
    first_run: bool = False
    telegram_sent: bool = False
    state_updated: bool = False


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def load_env(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_state_path() -> Path:
    return Path(os.getenv("MYOFFICE_STATE_PATH", str(DEFAULT_STATE_PATH)))


def get_lock_path() -> Path:
    return Path(os.getenv("MYOFFICE_LOCK_PATH", str(DEFAULT_LOCK_PATH)))


class MyOfficeNewsParser(HTMLParser):
    def __init__(self, page_url: str, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.source = source
        self.items: list[NewsItem] = []
        self.page_links: set[str] = set()
        self._in_news_row = False
        self._in_cell = False
        self._cells: list[dict[str, Any]] = []
        self._cell_text: list[str] = []
        self._cell_links: list[Link] = []
        self._current_link: dict[str, Any] | None = None
        self._row_news_id = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}

        if tag.lower() == "tr" and attrs_dict.get("bgcolor", "").lower() == "#ffffff":
            self._in_news_row = True
            self._cells = []
            self._row_news_id = ""
            return

        href = attrs_dict.get("href", "")
        if tag.lower() == "a" and "file=rub" in href and "Page=" in href:
            self.page_links.add(urljoin(self.page_url, href.replace("&amp;", "&")))

        if not self._in_news_row:
            return

        if tag.lower() == "td":
            self._in_cell = True
            self._cell_text = []
            self._cell_links = []
            return

        if tag.lower() == "br" and self._in_cell:
            self._cell_text.append(" ")
            if self._current_link is not None:
                self._current_link["text"].append(" ")
            return

        if tag.lower() == "a" and self._in_cell:
            self._capture_news_id(attrs_dict.get("href", ""))
            self._current_link = {
                "href": attrs_dict.get("href", ""),
                "text": [],
            }
            return

        if tag.lower() == "form":
            self._capture_news_id(attrs_dict.get("action", ""))

    def _capture_news_id(self, url: str) -> None:
        match = re.search(r"(?:[?&]|&amp;)id=(\d+)", url)
        if match:
            self._row_news_id = match.group(1)

    def handle_data(self, data: str) -> None:
        if not self._in_news_row or not self._in_cell:
            return
        self._cell_text.append(data)
        if self._current_link is not None:
            self._current_link["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "a" and self._in_news_row and self._in_cell and self._current_link:
            text = clean_text("".join(self._current_link["text"]))
            href = self._current_link["href"]
            if text and href:
                self._cell_links.append(Link(text=text, url=urljoin(self.page_url, href)))
            self._current_link = None
            return

        if tag == "td" and self._in_news_row and self._in_cell:
            self._cells.append(
                {
                    "text": clean_text("".join(self._cell_text)),
                    "links": self._cell_links,
                }
            )
            self._in_cell = False
            self._cell_text = []
            self._cell_links = []
            self._current_link = None
            return

        if tag == "tr" and self._in_news_row:
            self._finish_row()
            self._in_news_row = False

    def _finish_row(self) -> None:
        if len(self._cells) < 3:
            return

        document_number = clean_text(self._cells[0]["text"])
        detail_text = clean_text(self._cells[1]["text"])
        date_from_column = ""
        if len(self._cells) >= 5:
            date_from_column = clean_text(self._cells[2]["text"])
            sender = clean_text(self._cells[3]["text"])
        else:
            sender = clean_text(self._cells[2]["text"])
        attachments = self._cells[1]["links"]

        if (
            not document_number
            or not detail_text
            or "เลขหนังสือ" in document_number
            or "เลขทะเบียน" in document_number
        ):
            return

        title, date_text = parse_title_and_date(detail_text)
        if not date_text:
            date_text = date_from_column
        key = make_news_key(self._row_news_id, document_number, title, attachments)
        self.items.append(
            NewsItem(
                key=key,
                news_id=self._row_news_id,
                document_number=document_number,
                title=title,
                date_text=date_text,
                sender=sender,
                attachments=attachments,
                page_url=self.page_url,
                source=self.source,
            )
        )


def parse_title_and_date(detail_text: str) -> tuple[str, str]:
    before_docs = re.split(r"\s*เอกสาร\s*:", detail_text, maxsplit=1)[0].strip()
    match = re.search(r"\s*ลว\.\s*(.+)$", before_docs)
    if not match:
        return before_docs, ""

    title = before_docs[: match.start()].strip()
    date_text = match.group(1).strip()
    return title, date_text


def make_news_key(
    news_id: str, document_number: str, title: str, attachments: list[Link]
) -> str:
    if news_id:
        return f"id:{news_id}"

    first_attachment = attachments[0].url if attachments else ""
    digest = hashlib.sha256(
        f"{document_number}|{title}|{first_attachment}".encode("utf-8")
    ).hexdigest()[:16]
    return f"hash:{digest}"


class MyOfficeClient:
    def __init__(self, username: str, password: str, timeout: int) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, url: str, data: dict[str, str] | None = None) -> str:
        encoded_data = None
        if data is not None:
            encoded_data = urlencode(data).encode("utf-8")

        request = Request(
            url,
            data=encoded_data,
            headers={
                "User-Agent": "Mozilla/5.0 MyOfficeNewsChecker/1.0",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    def login(self) -> None:
        html = self.request(
            LOGIN_URL,
            {
                "username": self.username,
                "password": self.password,
                "submit": "เข้าระบบ",
            },
        )
        if "User Login" in html and "name='username'" in html:
            raise RuntimeError("เข้าสู่ระบบไม่สำเร็จ: ยังพบหน้า login หลังส่ง username/password")

    def fetch_news_page(self, news_url: str, source: str, allow_empty: bool = False) -> tuple[list[NewsItem], set[str]]:
        html = self.request(news_url)
        if ("User Login" in html or "NAME='username'" in html or "name='username'" in html) and (
            "เลขหนังสือ" not in html and "เลขทะเบียน" not in html
        ):
            raise RuntimeError("ยังไม่ได้เข้าสู่ระบบหรือ session หมดอายุ")

        parser = MyOfficeNewsParser(news_url, source=source)
        parser.feed(html)
        if not parser.items and not allow_empty:
            raise RuntimeError("ไม่พบรายการข่าวในหน้าเป้าหมาย อาจมีการเปลี่ยนโครงสร้าง HTML")
        return parser.items, parser.page_links

    def fetch_pending_news(self, news_url: str) -> list[NewsItem]:
        items, _ = self.fetch_news_page(news_url, source="pending")
        return items

    def fetch_archived_news(self, archived_url: str, max_pages: int) -> list[NewsItem]:
        first_page_items, page_links = self.fetch_news_page(
            archived_url, source="archived", allow_empty=True
        )
        all_items = {item.key: item for item in first_page_items}

        urls = sorted(page_links, key=get_page_number)
        for url in urls:
            if get_page_number(url) == 1:
                continue
            if max_pages > 0 and get_page_number(url) > max_pages:
                continue
            page_items, _ = self.fetch_news_page(url, source="archived", allow_empty=True)
            for item in page_items:
                all_items.setdefault(item.key, item)

        return list(all_items.values())


def get_page_number(url: str) -> int:
    match = re.search(r"(?:[?&]|&amp;)Page=(\d+)", url)
    return int(match.group(1)) if match else 1


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen": {}, "last_run": None}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    tmp_path.replace(path)


@contextlib.contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def build_telegram_ssl_context() -> ssl.SSLContext:
    if not env_bool("TELEGRAM_SSL_VERIFY", default=True):
        return ssl._create_unverified_context()

    ca_file = os.getenv("TELEGRAM_CA_FILE", "").strip()
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)

    try:
        import certifi  # type: ignore
    except ImportError:
        return ssl.create_default_context()

    return ssl.create_default_context(cafile=certifi.where())


def notify_telegram(token: str, chat_id: str, message: str, timeout: int) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": "true",
    }
    request = Request(url, data=urlencode(data).encode("utf-8"))
    opener = build_opener(HTTPSHandler(context=build_telegram_ssl_context()))
    with opener.open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")


def build_message(items: list[NewsItem]) -> str:
    parts = [f"พบข่าวใหม่ My Office จำนวน {len(items)} รายการ"]

    for index, item in enumerate(items, start=1):
        attachment_lines = []
        for link in item.attachments[:3]:
            attachment_lines.append(f"- {link.text}: {link.url}")
        if len(item.attachments) > 3:
            attachment_lines.append(f"- เอกสารเพิ่มเติมอีก {len(item.attachments) - 3} รายการ")

        block = [
            f"{index}. {item.document_number}",
            f"เรื่อง: {item.title}",
        ]
        if item.date_text:
            block.append(f"ลว.: {item.date_text}")
        if item.sender:
            block.append(f"ผู้ส่ง: {item.sender}")
        block.append(f"หน้าเว็บ: {item.page_url}")
        if attachment_lines:
            block.append("เอกสาร:")
            block.extend(attachment_lines)
        parts.append("\n".join(block))

    return "\n\n".join(parts)


def split_message(message: str, limit: int = 3900) -> list[str]:
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in message.split("\n\n"):
        block_len = len(block) + 2
        if current and current_len + block_len > limit:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(block)
        current_len += block_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def mark_seen(state: dict[str, Any], item: NewsItem, now: str) -> None:
    state.setdefault("seen", {})[item.key] = {
        "first_seen": now,
        "last_seen": now,
        "source": item.source,
        "news_id": item.news_id,
        "document_number": item.document_number,
        "title": item.title,
        "date_text": item.date_text,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check My Office news and notify Telegram for new items."
    )
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help="Path to .env file")
    parser.add_argument(
        "--state",
        default=None,
        help="Path to JSON state file that stores seen news",
    )
    parser.add_argument(
        "--notify-existing",
        action="store_true",
        help="Notify all current items on the first run instead of only creating a baseline",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent, but do not send Telegram messages or save state",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a short Telegram test message and exit",
    )
    return parser.parse_args()


def run_check(
    *,
    state_path: Path,
    notify_existing: bool = False,
    dry_run: bool = False,
) -> RunResult:
    username = os.getenv("MYOFFICE_USERNAME", "")
    password = os.getenv("MYOFFICE_PASSWORD", "")
    news_url = os.getenv("MYOFFICE_NEWS_URL", DEFAULT_NEWS_URL)
    archived_news_url = os.getenv("MYOFFICE_ARCHIVED_NEWS_URL", DEFAULT_ARCHIVED_NEWS_URL)
    archived_max_pages = int(os.getenv("MYOFFICE_ARCHIVED_MAX_PAGES", "0"))
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    timeout = int(os.getenv("REQUEST_TIMEOUT", "30"))

    if not username or not password:
        raise RuntimeError("กรุณาตั้งค่า MYOFFICE_USERNAME และ MYOFFICE_PASSWORD ใน .env")

    client = MyOfficeClient(username=username, password=password, timeout=timeout)
    client.login()
    pending_items = client.fetch_pending_news(news_url)
    archived_items = client.fetch_archived_news(
        archived_news_url, max_pages=archived_max_pages
    )

    state = load_state(state_path)
    seen = state.setdefault("seen", {})
    first_run = not seen
    archived_keys = {item.key for item in archived_items}
    new_items = [
        item for item in pending_items if item.key not in seen and item.key not in archived_keys
    ]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for item in archived_items:
        mark_seen(state, item, now)
    for item in pending_items:
        if item.key in seen:
            mark_seen(state, item, now)

    if first_run and new_items and not notify_existing:
        for item in new_items:
            mark_seen(state, item, now)
        state["last_run"] = now
        state["last_counts"] = {
            "pending": len(pending_items),
            "archived": len(archived_items),
            "new": len(new_items),
        }
        if not dry_run:
            save_state(state_path, state)
        if dry_run:
            message = (
                f"First run dry-run: would save baseline {len(new_items)} pending items "
                f"and {len(archived_items)} archived items. "
                "No Telegram notification would be sent."
            )
        else:
            message = (
                f"First run: saved baseline {len(new_items)} pending items "
                f"and {len(archived_items)} archived items. "
                "No Telegram notification sent."
            )
        return RunResult(
            status="baseline",
            message=message,
            pending_count=len(pending_items),
            archived_count=len(archived_items),
            new_count=len(new_items),
            first_run=True,
            state_updated=not dry_run,
        )

    if not new_items:
        state["last_run"] = now
        state["last_counts"] = {
            "pending": len(pending_items),
            "archived": len(archived_items),
            "new": 0,
        }
        if not dry_run:
            save_state(state_path, state)
        message = (
            f"No new news. Checked {len(pending_items)} pending items "
            f"and {len(archived_items)} archived items."
        )
        return RunResult(
            status="no_new",
            message=message,
            pending_count=len(pending_items),
            archived_count=len(archived_items),
            new_count=0,
            first_run=first_run,
            state_updated=not dry_run,
        )

    message = build_message(new_items)
    if dry_run:
        return RunResult(
            status="dry_run",
            message=message,
            pending_count=len(pending_items),
            archived_count=len(archived_items),
            new_count=len(new_items),
            first_run=first_run,
        )

    telegram_sent = False
    if not telegram_token or not telegram_chat_id:
        print(
            "WARNING: พบข่าวใหม่ แต่ยังไม่ได้ตั้งค่า TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID",
            file=sys.stderr,
        )
        print(textwrap.indent(message, "  "))
    else:
        for chunk in split_message(message):
            notify_telegram(telegram_token, telegram_chat_id, chunk, timeout=timeout)
        telegram_sent = True

    for item in new_items:
        mark_seen(state, item, now)
    state["last_run"] = now
    state["last_counts"] = {
        "pending": len(pending_items),
        "archived": len(archived_items),
        "new": len(new_items),
    }
    save_state(state_path, state)
    summary = (
        f"New news: {len(new_items)} item(s). Checked {len(pending_items)} pending "
        f"and {len(archived_items)} archived items. State updated."
    )
    return RunResult(
        status="new_news",
        message=summary,
        pending_count=len(pending_items),
        archived_count=len(archived_items),
        new_count=len(new_items),
        first_run=first_run,
        telegram_sent=telegram_sent,
        state_updated=True,
    )


def send_test_telegram() -> str:
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    timeout = int(os.getenv("REQUEST_TIMEOUT", "30"))
    if not telegram_token or not telegram_chat_id:
        raise RuntimeError("กรุณาตั้งค่า TELEGRAM_BOT_TOKEN และ TELEGRAM_CHAT_ID ใน .env")
    notify_telegram(
        telegram_token,
        telegram_chat_id,
        "ทดสอบแจ้งเตือน My Office: Telegram ใช้งานได้",
        timeout=timeout,
    )
    return "Telegram test message sent."


def main() -> int:
    args = parse_args()
    load_env(Path(args.env))

    if args.test_telegram:
        print(send_test_telegram())
        return 0

    lock_path = get_lock_path()
    state_path = Path(args.state) if args.state else get_state_path()
    with exclusive_lock(lock_path):
        result = run_check(
            state_path=state_path,
            notify_existing=args.notify_existing,
            dry_run=args.dry_run,
        )
    print(result.message)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
