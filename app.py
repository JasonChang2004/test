import json
import os
import re
import sys
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv()

UTC = timezone.utc
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
SOURCES_PATH = CONFIG_DIR / "sources.json"
STATE_PATH = DATA_DIR / "state.json"
REQUEST_TIMEOUT = 20

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("threads_discord_bot")


class BotError(Exception):
    pass


@dataclass
class Source:
    id: str
    platform: str
    name: str
    url: str
    enabled: bool = True
    check_interval_minutes: int = 60
    parser_type: str = "threads_public_profile"


@dataclass
class Post:
    post_id: str
    url: str
    text: str
    published_at: Optional[str]
    source_name: str

    @property
    def dedupe_key(self) -> str:
        return self.post_id or self.url


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, default: Any) -> Any:
        if not self.path.exists():
            return default
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            raise BotError(f"Invalid JSON file: {self.path} ({exc})") from exc

    def atomic_save(self, data: Any) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(self.path)


class SourceLoader:
    def __init__(self, path: Path):
        self.store = JsonStore(path)

    def load_sources(self) -> List[Source]:
        raw = self.store.load(default=[])
        if not isinstance(raw, list):
            raise BotError("sources.json must be a JSON array")

        sources: List[Source] = []
        for item in raw:
            try:
                source = Source(**item)
            except TypeError as exc:
                raise BotError(f"Invalid source config: {item} ({exc})") from exc

            if source.platform != "threads":
                logger.warning("Skip unsupported platform: %s", source.platform)
                continue

            if source.parser_type != "threads_public_profile":
                logger.warning("Skip unsupported parser_type: %s", source.parser_type)
                continue

            sources.append(source)

        return sources


class StateStore:
    def __init__(self, path: Path):
        self.store = JsonStore(path)
        self.state: Dict[str, Dict[str, Any]] = self.store.load(default={})
        if not isinstance(self.state, dict):
            raise BotError("state.json must be a JSON object")

    def get_source_state(self, source_id: str) -> Dict[str, Any]:
        if source_id not in self.state:
            self.state[source_id] = {
                "last_checked_at": None,
                "last_success_at": None,
                "last_error_at": None,
                "last_error_message": None,
                "notified_posts": [],
            }
        return self.state[source_id]

    def should_check(self, source: Source, now: datetime) -> bool:
        item = self.get_source_state(source.id)
        last_checked_at = item.get("last_checked_at")
        if not last_checked_at:
            return True

        try:
            last_dt = datetime.fromisoformat(last_checked_at.replace("Z", "+00:00"))
        except ValueError:
            return True

        return now >= last_dt + timedelta(minutes=source.check_interval_minutes)

    def mark_checked(self, source_id: str, now: datetime) -> None:
        item = self.get_source_state(source_id)
        item["last_checked_at"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def mark_success(self, source_id: str, now: datetime) -> None:
        item = self.get_source_state(source_id)
        item["last_success_at"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        item["last_error_at"] = None
        item["last_error_message"] = None

    def mark_error(self, source_id: str, now: datetime, message: str) -> None:
        item = self.get_source_state(source_id)
        item["last_error_at"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        item["last_error_message"] = message

    def is_notified(self, source_id: str, dedupe_key: str) -> bool:
        item = self.get_source_state(source_id)
        return dedupe_key in item.get("notified_posts", [])

    def add_notified_post(self, source_id: str, dedupe_key: str, keep_last: int = 50) -> None:
        item = self.get_source_state(source_id)
        posts = item.setdefault("notified_posts", [])
        if dedupe_key not in posts:
            posts.insert(0, dedupe_key)
        item["notified_posts"] = posts[:keep_last]

    def save(self) -> None:
        self.store.atomic_save(self.state)


class ThreadsFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def fetch_latest_posts(self, source: Source, limit: int = 5) -> List[Post]:
        html = self._get_profile_html(source.url)
        posts = self._extract_posts_from_html(html, source)
        if not posts:
            raise BotError(
                f"Could not extract posts from Threads profile: {source.url}. "
                "Threads page structure may have changed."
            )
        return posts[:limit]

    def _get_profile_html(self, url: str) -> str:
        response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.text

    def _extract_posts_from_html(self, html: str, source: Source) -> List[Post]:
        post_urls = self._extract_post_urls(html)
        text_candidates = self._extract_text_candidates(html)
        time_candidates = self._extract_time_candidates(html)

        posts: List[Post] = []
        for idx, post_url in enumerate(post_urls):
            post_id = self._post_id_from_url(post_url)
            if not post_id:
                continue

            text = text_candidates[idx] if idx < len(text_candidates) else ""
            published_at = time_candidates[idx] if idx < len(time_candidates) else None

            posts.append(
                Post(
                    post_id=post_id,
                    url=post_url,
                    text=self._clean_text(text) or "(no preview text)",
                    published_at=published_at,
                    source_name=source.name,
                )
            )

        deduped: Dict[str, Post] = {}
        for post in posts:
            deduped[post.dedupe_key] = post

        return list(deduped.values())

    def _extract_post_urls(self, html: str) -> List[str]:
        url_patterns = [
            r'https://www\.threads\.net/@[^"\s<>]+/post/[^"\s<>?]+',
            r'/@[^"\s<>]+/post/[^"\s<>?]+',
        ]
        urls: List[str] = []

        for pattern in url_patterns:
            for match in re.findall(pattern, html):
                full_url = urljoin("https://www.threads.net", match)
                if full_url not in urls:
                    urls.append(full_url)

        return urls

    def _extract_text_candidates(self, html: str) -> List[str]:
        texts: List[str] = []
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup.find_all("meta"):
            key = (tag.get("property") or "") + "|" + (tag.get("name") or "")
            content = tag.get("content") or ""
            if not content:
                continue
            if "description" in key.lower() and len(content.strip()) > 10:
                if content.strip() not in texts:
                    texts.append(content.strip())

        for match in re.findall(r'"text"\s*:\s*"(.*?)"', html):
            try:
                candidate = bytes(match, "utf-8").decode("unicode_escape", errors="ignore")
            except Exception:
                candidate = match
            candidate = self._clean_text(candidate)
            if candidate and candidate not in texts:
                texts.append(candidate)

        return texts

    def _extract_time_candidates(self, html: str) -> List[str]:
        candidates: List[str] = []

        for match in re.findall(r'"taken_at"\s*:\s*(\d{10,13})', html):
            try:
                ts = int(match)
                if ts > 10**12:
                    ts = ts / 1000
                dt = datetime.fromtimestamp(ts, tz=UTC)
                iso = dt.isoformat().replace("+00:00", "Z")
                if iso not in candidates:
                    candidates.append(iso)
            except Exception:
                continue

        for match in re.findall(r'datetime="([^"]+)"', html):
            if match not in candidates:
                candidates.append(match)

        return candidates

    def _post_id_from_url(self, url: str) -> str:
        match = re.search(r'/post/([^/?#]+)', url)
        return match.group(1) if match else ""

    def _clean_text(self, text: str, limit: int = 180) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        if len(text) > limit:
            return text[: limit - 3].rstrip() + "..."
        return text


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        if not webhook_url:
            raise BotError("DISCORD_WEBHOOK_URL is required")
        self.webhook_url = webhook_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def send_post(self, post: Post) -> None:
        payload = {"content": self._format_message(post)}
        response = self.session.post(
            self.webhook_url,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

    def _format_message(self, post: Post) -> str:
        published_text = post.published_at or "unknown"
        return (
            "【新貼文通知】\n"
            f"來源：{post.source_name}\n"
            f"時間：{published_text}\n"
            f"摘要：{post.text}\n"
            f"連結：{post.url}"
        )


class BotRunner:
    def __init__(self):
        self.source_loader = SourceLoader(SOURCES_PATH)
        self.state_store = StateStore(STATE_PATH)
        self.fetcher = ThreadsFetcher()
        self.notifier = DiscordNotifier(os.getenv("DISCORD_WEBHOOK_URL", ""))

    def run(self) -> int:
        started_at = datetime.now(tz=UTC)
        logger.info("Job started")

        notified_count = 0
        failed_count = 0

        sources = self.source_loader.load_sources()
        enabled_sources = [s for s in sources if s.enabled]
        logger.info("Loaded %s enabled sources", len(enabled_sources))

        for source in enabled_sources:
            now = datetime.now(tz=UTC)

            if not self.state_store.should_check(source, now):
                logger.info("Skip source=%s because interval not reached", source.id)
                continue

            self.state_store.mark_checked(source.id, now)

            try:
                posts = self.fetcher.fetch_latest_posts(source)
                new_posts = [
                    p for p in posts
                    if not self.state_store.is_notified(source.id, p.dedupe_key)
                ]

                if not new_posts:
                    logger.info("No new posts | source=%s", source.id)
                    self.state_store.mark_success(source.id, datetime.now(tz=UTC))
                    continue

                for post in reversed(new_posts):
                    self.notifier.send_post(post)
                    self.state_store.add_notified_post(source.id, post.dedupe_key)
                    notified_count += 1
                    logger.info("Notified post | source=%s | post=%s", source.id, post.dedupe_key)

                self.state_store.mark_success(source.id, datetime.now(tz=UTC))

            except Exception as exc:
                failed_count += 1
                logger.exception("Source failed | source=%s", source.id)
                self.state_store.mark_error(source.id, datetime.now(tz=UTC), str(exc))

        self.state_store.save()

        finished_at = datetime.now(tz=UTC)
        logger.info(
            "Job finished | duration=%ss | notified=%s | failed=%s",
            int((finished_at - started_at).total_seconds()),
            notified_count,
            failed_count,
        )
        return 0 if failed_count == 0 else 1


def main() -> int:
    try:
        runner = BotRunner()
        return runner.run()
    except Exception:
        logger.exception("Fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())