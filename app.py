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
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


load_dotenv()

UTC = timezone.utc
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
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
    thread_id: Optional[str] = None


@dataclass
class Post:
    post_id: str
    url: str
    text: str
    published_at: Optional[str]
    source_name: str
    image_url: Optional[str] = None

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
        
        # 初始化全域健康檢查狀態
        if "_health_check" not in self.state:
            self.state["_health_check"] = {
                "last_notification_at": None,
                "last_health_check_at": None,
            }

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
    
    def update_last_notification(self, now: datetime) -> None:
        """更新最後一次發送通知的時間"""
        if "_health_check" not in self.state:
            self.state["_health_check"] = {}
        self.state["_health_check"]["last_notification_at"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    
    def should_send_health_check(self, now: datetime, interval_hours: int = 24) -> bool:
        """檢查是否需要發送健康檢查通知（預設 24 小時）"""
        health = self.state.get("_health_check", {})
        last_notification = health.get("last_notification_at")
        
        if not last_notification:
            return False  # 如果從未發送過通知，不需要健康檢查
        
        try:
            last_dt = datetime.fromisoformat(last_notification.replace("Z", "+00:00"))
            hours_since = (now - last_dt).total_seconds() / 3600
            return hours_since >= interval_hours
        except (ValueError, AttributeError):
            return False
    
    def update_health_check(self, now: datetime) -> None:
        """更新健康檢查時間"""
        if "_health_check" not in self.state:
            self.state["_health_check"] = {}
        self.state["_health_check"]["last_health_check_at"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        # 同時更新最後通知時間，避免重複發送
        self.state["_health_check"]["last_notification_at"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
        """使用 Playwright 抓取 Threads 頁面（需要執行 JavaScript）"""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers(DEFAULT_HEADERS)
                
                logger.info("Fetching Threads profile: %s", url)
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)  # 額外等待確保內容載入
                
                html = page.content()
                browser.close()
                
                logger.info("Successfully fetched HTML (length: %d)", len(html))
                return html
        except PlaywrightTimeoutError as exc:
            raise BotError(f"Timeout while loading Threads profile: {url}") from exc
        except Exception as exc:
            raise BotError(f"Failed to fetch Threads profile: {url} ({exc})") from exc

    def _extract_posts_from_html(self, html: str, source: Source) -> List[Post]:
        post_urls = self._extract_post_urls(html)
        text_candidates = self._extract_text_candidates(html)
        time_candidates = self._extract_time_candidates(html)
        image_candidates = self._extract_image_candidates(html)

        logger.info("Extracted data | post_urls=%d | texts=%d | times=%d | images=%d", 
                   len(post_urls), len(text_candidates), len(time_candidates), len(image_candidates))
        
        # 印出詳細資訊以便診斷
        logger.info("=== Post URLs ===")
        for i, url in enumerate(post_urls[:5]):
            logger.info("  [%d] %s", i, url)
        
        logger.info("=== Text Candidates ===")
        for i, text in enumerate(text_candidates[:5]):
            preview = text[:100] if len(text) > 100 else text
            logger.info("  [%d] %s", i, preview)
        
        logger.info("=== Time Candidates ===")
        for i, time in enumerate(time_candidates[:5]):
            logger.info("  [%d] %s", i, time)

        posts: List[Post] = []
        for idx, post_url in enumerate(post_urls):
            post_id = self._post_id_from_url(post_url)
            if not post_id:
                continue

            text = text_candidates[idx] if idx < len(text_candidates) else ""
            published_at = time_candidates[idx] if idx < len(time_candidates) else None
            image_url = image_candidates[idx] if idx < len(image_candidates) else None

            posts.append(
                Post(
                    post_id=post_id,
                    url=post_url,
                    text=self._clean_text(text) or "(no preview text)",
                    published_at=published_at,
                    source_name=source.name,
                    image_url=image_url,
                )
            )

        deduped: Dict[str, Post] = {}
        for post in posts:
            deduped[post.dedupe_key] = post

        logger.info("=== Final Posts (after dedup) ===")
        for i, post in enumerate(list(deduped.values())):
            logger.info("Post %d:", i + 1)
            logger.info("  ID: %s", post.post_id)
            logger.info("  URL: %s", post.url)
            logger.info("  Text: %s", post.text)
            logger.info("  Published: %s", post.published_at)

        return list(deduped.values())

    def _extract_post_urls(self, html: str) -> List[str]:
        """提取貼文 URL（支援 threads.net/com 和相對路徑）"""
        url_patterns = [
            r'https://www\.threads\.net/@[^"\s<>]+/post/[^"\s<>?]+',
            r'https://www\.threads\.com/@[^"\s<>]+/post/[^"\s<>?]+',
            r'/@[^"/\s<>]+/post/[A-Za-z0-9_-]+',  # 相對路徑
        ]
        urls: List[str] = []

        for pattern in url_patterns:
            for match in re.findall(pattern, html):
                # 轉換為完整 URL
                if match.startswith('/'):
                    full_url = urljoin("https://www.threads.net", match)
                else:
                    full_url = match
                    
                if full_url not in urls:
                    urls.append(full_url)

        return urls

    def _extract_text_candidates(self, html: str) -> List[str]:
        texts: List[str] = []
        soup = BeautifulSoup(html, "html.parser")

        # 跳過個人簡介相關的 meta description
        skip_keywords = ["Followers", "Threads •", "See the latest conversations"]
        
        for tag in soup.find_all("meta"):
            key = (tag.get("property") or "") + "|" + (tag.get("name") or "")
            content = tag.get("content") or ""
            if not content:
                continue
            if "description" in key.lower() and len(content.strip()) > 10:
                # 過濾掉個人簡介
                if any(keyword in content for keyword in skip_keywords):
                    continue
                if content.strip() not in texts:
                    texts.append(content.strip())

        # 從 JSON 資料中提取文字（這是最可靠的來源）
        for match in re.findall(r'"text"\s*:\s*"(.*?)"', html):
            try:
                candidate = bytes(match, "utf-8").decode("unicode_escape", errors="ignore")
            except Exception:
                candidate = match
            candidate = self._clean_text(candidate)
            
            # 過濾掉個人簡介和太短的文字
            if candidate and len(candidate) > 20:
                if any(keyword in candidate for keyword in skip_keywords):
                    continue
                if candidate not in texts:
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
                # 轉換為台北時區
                dt_taipei = dt.astimezone(TAIPEI_TZ)
                formatted = dt_taipei.strftime("%Y-%m-%d %H:%M:%S")
                if formatted not in candidates:
                    candidates.append(formatted)
            except Exception:
                continue

        for match in re.findall(r'datetime="([^"]+)"', html):
            # 嘗試解析 ISO 格式並轉換為台北時區
            try:
                dt = datetime.fromisoformat(match.replace("Z", "+00:00"))
                dt_taipei = dt.astimezone(TAIPEI_TZ)
                formatted = dt_taipei.strftime("%Y-%m-%d %H:%M:%S")
                if formatted not in candidates:
                    candidates.append(formatted)
            except Exception:
                if match not in candidates:
                    candidates.append(match)

        return candidates

    def _extract_image_candidates(self, html: str) -> List[Optional[str]]:
        """提取貼文圖片 URL"""
        images: List[Optional[str]] = []
        
        # 從 JSON 中提取 image_versions2 的圖片 URL
        pattern = r'"image_versions2"[^}]*?"url"\s*:\s*"([^"]+)"'
        for match in re.findall(pattern, html):
            # 處理 JSON 轉義的反斜線
            url = match.replace(r'\/', '/')
            
            # 過濾掉頭像圖片（t51.82787-19 是頭像，t51.82787-15 是貼文圖）
            if "/t51.82787-19/" not in url and url not in images:
                images.append(url)
        
        # 如果沒找到，嘗試從 img 標籤提取
        if not images:
            pattern = r'<img[^>]*src="(https://scontent[^"]+\.cdninstagram\.com[^"]+t51\.82787-15[^"]+)"'
            for match in re.findall(pattern, html):
                if match not in images:
                    images.append(match)
        
        return images

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

    def send_post(self, post: Post, thread_id: Optional[str] = None) -> None:
        payload = {"embeds": [self._format_embed(post)]}
        
        # 如果指定了 thread_id，則發送到該 thread
        url = self.webhook_url
        if thread_id:
            url = f"{self.webhook_url}?thread_id={thread_id}"
        
        response = self.session.post(
            url,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    
    def send_health_check(self, hours_since_last: float) -> None:
        """發送健康檢查通知"""
        payload = {"embeds": [self._format_health_check_embed(hours_since_last)]}
        response = self.session.post(
            self.webhook_url,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

    def _format_embed(self, post: Post) -> Dict[str, Any]:
        published_text = post.published_at or "未知時間"
        
        # 截取文字摘要（Discord embed description 限制 4096 字元）
        description = post.text
        if len(description) > 300:
            description = description[:297] + "..."
        
        embed = {
            "title": f"📬 {post.source_name}",
            "description": description,
            "url": post.url,
            "color": 5814783,  # 藍紫色
            "fields": [
                {"name": "🕒 發布時間", "value": published_text, "inline": True},
            ],
            "footer": {"text": "Threads Monitor Bot"},
        }
        
        # 如果有圖片，加入 image 欄位
        if post.image_url:
            embed["image"] = {"url": post.image_url}
        
        return embed
    
    def _format_health_check_embed(self, hours_since_last: float) -> Dict[str, Any]:
        """格式化健康檢查訊息"""
        now_taipei = datetime.now(tz=TAIPEI_TZ)
        
        return {
            "title": "✅ 系統健康檢查",
            "description": f"Threads Monitor Bot 正常運作中\n\n已經 **{hours_since_last:.1f} 小時**沒有新貼文通知。",
            "color": 5763719,  # 綠色
            "fields": [
                {"name": "🕒 檢查時間", "value": now_taipei.strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
                {"name": "📊 狀態", "value": "正常運作 ✓", "inline": True},
            ],
            "footer": {"text": "自動健康檢查 • 每 24 小時"},
        }


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
                    self.notifier.send_post(post, thread_id=source.thread_id)
                    self.state_store.add_notified_post(source.id, post.dedupe_key)
                    self.state_store.update_last_notification(datetime.now(tz=UTC))
                    notified_count += 1
                    logger.info("Notified post | source=%s | post=%s | thread=%s", 
                               source.id, post.dedupe_key, source.thread_id or "main")

                self.state_store.mark_success(source.id, datetime.now(tz=UTC))

            except Exception as exc:
                failed_count += 1
                logger.exception("Source failed | source=%s", source.id)
                self.state_store.mark_error(source.id, datetime.now(tz=UTC), str(exc))

        # 檢查是否需要發送健康檢查通知
        now = datetime.now(tz=UTC)
        if self.state_store.should_send_health_check(now, interval_hours=24):
            try:
                health = self.state_store.state.get("_health_check", {})
                last_notification = health.get("last_notification_at")
                if last_notification:
                    last_dt = datetime.fromisoformat(last_notification.replace("Z", "+00:00"))
                    hours_since = (now - last_dt).total_seconds() / 3600
                    
                    self.notifier.send_health_check(hours_since)
                    self.state_store.update_health_check(now)
                    logger.info("Sent health check notification | hours_since_last=%.1f", hours_since)
            except Exception as exc:
                logger.exception("Failed to send health check notification")
        
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