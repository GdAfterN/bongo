from __future__ import annotations

import html
import ipaddress
import json
import math
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from pydantic import BaseModel, Field, ValidationError

from .providers import ConversationProvider


HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
NEWS_LIMIT = 20
MODEL_GENERATION_ATTEMPTS = 3
MODEL_RETRY_DELAYS = (2.0, 5.0)
NETWORK_FETCH_ATTEMPTS = 5
NETWORK_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
AI_TERMS = (
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "llm",
    "language model",
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "deepmind",
    "mistral",
    "hugging face",
    "transformer",
    "inference",
    "agent",
    "rag",
    "vision model",
    "diffusion",
    "nvidia",
)
AI_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(re.escape(term).replace(r"\ ", r"\s+") for term in AI_TERMS)
    + r")(?![a-z0-9])",
    re.IGNORECASE,
)

BRIEF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_id": {"type": "integer"},
        "title": {"type": "string", "maxLength": 48},
        "summary": {"type": "string", "minLength": 130, "maxLength": 180},
    },
    "required": ["source_id", "title", "summary"],
}


class BriefDraft(BaseModel):
    source_id: int
    title: str = Field(min_length=1, max_length=48)
    summary: str = Field(min_length=130, max_length=180)


class NewsBrief(BaseModel):
    id: int
    title: str = Field(min_length=1, max_length=48)
    summary: str = Field(min_length=1, max_length=420)
    published_at: str
    author: str
    original_url: str
    discussion_url: str
    original_title: str


class HackerNewsClient:
    def __init__(
        self,
        fetch_json: Callable[[str], Any] | None = None,
        fetch_article_text: Callable[[str], str] | None = None,
        timeout: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.timeout = timeout
        self.sleep = sleep
        self.fetch_json = fetch_json or self._fetch_json
        self.fetch_article_text = fetch_article_text or (
            self._fetch_article_text if fetch_json is None else lambda _url: ""
        )

    def _fetch_json(self, path: str) -> Any:
        if not re.fullmatch(r"/(?:topstories\.json|beststories\.json|item/\d+\.json)", path):
            raise ValueError("不允许访问 Hacker News API 以外的地址")
        request = Request(
            HN_API_BASE + path,
            headers={"User-Agent": "BongoStudy/1.0 HackerNewsReader"},
        )
        response = self._open_with_retry(urlopen, request)
        try:
            payload = response.read(2 * 1024 * 1024 + 1)
        finally:
            response.close()
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("Hacker News 响应过大")
        return json.loads(payload.decode("utf-8"))

    def recent_items(
        self,
        limit: int = 160,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        if progress:
            progress({"percent": 5, "stage": "正在读取 Hacker News 榜单"})
        with ThreadPoolExecutor(max_workers=2) as executor:
            lists = list(executor.map(
                self.fetch_json,
                ("/topstories.json", "/beststories.json"),
            ))
        if any(not isinstance(story_ids, list) for story_ids in lists):
            raise ValueError("Hacker News 热门列表格式错误")
        valid_ids = []
        seen_ids = set()
        for story_ids in lists:
            for item_id in story_ids[:limit]:
                if isinstance(item_id, int) and item_id not in seen_ids:
                    valid_ids.append(item_id)
                    seen_ids.add(item_id)
        if progress:
            progress({
                "percent": 12,
                "stage": "榜单读取完成",
                "detail": f"准备读取 {len(valid_ids)} 个帖子",
            })
        items = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(self._fetch_item, item_id) for item_id in valid_ids]
            total = max(len(futures), 1)
            for completed, future in enumerate(as_completed(futures), 1):
                item = future.result()
                if item is not None:
                    items.append(item)
                if progress and (completed == total or completed % 8 == 0):
                    progress({
                        "percent": 12 + round(43 * completed / total),
                        "stage": "正在读取帖子详情",
                        "detail": f"已读取 {completed}/{total} 个帖子",
                    })
        return items

    def _fetch_item(self, item_id: int) -> dict[str, Any] | None:
        try:
            item = self.fetch_json(f"/item/{item_id}.json")
        except Exception:
            return None
        return item if isinstance(item, dict) else None

    def _fetch_article_text(self, url: str) -> str:
        current_url = url
        opener = build_opener(_NoRedirectHandler())
        for _redirect in range(4):
            self._validate_public_url(current_url)
            request = Request(
                current_url,
                headers={
                    "User-Agent": "BongoStudy/1.0 ArticleReader",
                    "Accept": "text/html,text/plain,application/json;q=0.8",
                },
            )
            try:
                response = self._open_with_retry(opener.open, request)
            except HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location", "")
                    if not location:
                        return ""
                    current_url = urljoin(current_url, location)
                    continue
                return ""
            try:
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "text/plain", "application/json"}:
                    return ""
                payload = response.read(1_500_001)
                if len(payload) > 1_500_000:
                    return ""
                encoding = response.headers.get_content_charset() or "utf-8"
                decoded = payload.decode(encoding, errors="replace")
            finally:
                response.close()
            if content_type == "text/html":
                parser = _ArticleTextParser()
                parser.feed(decoded)
                decoded = parser.text()
            return self._compact_text(decoded)[:5000]
        return ""

    def _open_with_retry(self, opener, request: Request):
        last_error: Exception | None = None
        for attempt in range(1, NETWORK_FETCH_ATTEMPTS + 1):
            try:
                return opener(request, timeout=self.timeout)
            except HTTPError:
                raise
            except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
                last_error = exc
                if attempt == NETWORK_FETCH_ATTEMPTS:
                    break
                self.sleep(NETWORK_RETRY_DELAYS[attempt - 1])
        raise last_error or RuntimeError("网络抓取失败")

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("原文链接不是可访问的 HTTP 地址")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError("原文域名无法解析")
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("拒绝访问非公网原文地址")

    @staticmethod
    def _compact_text(value: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(value)).strip()


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _ArticleTextParser(HTMLParser):
    _SKIP_TAGS = {"script", "style", "svg", "nav", "footer", "header", "form"}
    _BREAK_TAGS = {"p", "div", "article", "section", "h1", "h2", "h3", "li", "pre", "code"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self._BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in self._BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join(self._parts)


class HackerNewsTool:
    name = "fetch_hacker_news_ai_trends"

    def __init__(
        self,
        client: HackerNewsClient | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.client = client or HackerNewsClient()
        self.now = now

    def execute(
        self,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        current_time = self.now()
        candidates = []
        items = self.client.recent_items(progress=progress)
        if progress:
            progress({
                "percent": 58,
                "stage": "正在筛选 AI 资讯",
                "detail": f"共读取 {len(items)} 个有效帖子",
            })
        for item in items:
            normalized = self._normalize(item, current_time)
            if normalized is not None:
                candidates.append(normalized)
        candidates.sort(key=lambda item: (-item["hot_score"], -item["score"], item["id"]))
        candidates = candidates[:32]
        if progress:
            progress({
                "percent": 62,
                "stage": "本地筛选完成",
                "detail": f"获得 {len(candidates)} 条 AI 候选",
            })
        self._enrich_article_text(candidates, progress)
        candidates = [
            item for item in candidates
            if len(f"{item['title']} {item['text']} {item['article_text']}") >= 280
        ]
        if progress:
            progress({
                "percent": 72,
                "stage": "原文提取完成",
                "detail": f"获得 {len(candidates)} 条正文信息充足的候选",
            })
        return {
            "source": "Hacker News",
            "fetched_at": int(current_time),
            "items": candidates[:NEWS_LIMIT],
        }

    def _enrich_article_text(
        self,
        candidates: list[dict[str, Any]],
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        def fetch(candidate: dict[str, Any]) -> tuple[int, str]:
            external_url = candidate["external_url"]
            if not external_url:
                return candidate["id"], ""
            try:
                return candidate["id"], self.client.fetch_article_text(external_url)
            except Exception:
                return candidate["id"], ""

        article_texts: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(fetch, candidate) for candidate in candidates]
            total = max(len(futures), 1)
            for completed, future in enumerate(as_completed(futures), 1):
                item_id, article_text = future.result()
                article_texts[item_id] = article_text
                if progress and (completed == total or completed % 4 == 0):
                    progress({
                        "percent": 62 + round(10 * completed / total),
                        "stage": "正在提取原文重点",
                        "detail": f"已处理 {completed}/{total} 个原文链接",
                    })
        for candidate in candidates:
            candidate["article_text"] = article_texts.get(candidate["id"], "")

    @staticmethod
    def _normalize(item: dict[str, Any], current_time: float) -> dict[str, Any] | None:
        if item.get("type") != "story" or item.get("deleted") or item.get("dead"):
            return None
        try:
            item_id = int(item["id"])
            created_at = int(item["time"])
        except (KeyError, TypeError, ValueError):
            return None
        age_hours = max(0.0, (current_time - created_at) / 3600)
        if age_hours > 96:
            return None
        title = str(item.get("title") or "").strip()
        original_text = HackerNewsTool._plain_text(str(item.get("text") or ""))
        if not title or not AI_PATTERN.search(f"{title}\n{original_text}"):
            return None
        score = max(0, int(item.get("score") or 0))
        comments = max(0, int(item.get("descendants") or 0))
        hot_score = (score + 2 * comments + 1) / math.pow(age_hours + 2, 1.35)
        external_url = str(item.get("url") or "").strip()
        discussion_url = f"https://news.ycombinator.com/item?id={item_id}"
        return {
            "id": item_id,
            "title": title,
            "text": original_text[:800],
            "article_text": "",
            "author": str(item.get("by") or "未知作者"),
            "score": score,
            "comments": comments,
            "created_at": created_at,
            "age_hours": round(age_hours, 1),
            "hot_score": round(hot_score, 6),
            "discussion_url": discussion_url,
            "external_url": external_url,
            "original_url": external_url or discussion_url,
        }

    @staticmethod
    def _plain_text(value: str) -> str:
        value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)
        value = re.sub(r"</?p\b[^>]*>", "\n", value, flags=re.IGNORECASE)
        value = re.sub(r"<[^>]+>", "", value)
        return html.unescape(value).strip()


class HackerNewsDigestGenerator:
    """Fetches HN once, then asks the model to write each brief independently."""

    def __init__(
        self,
        provider: ConversationProvider,
        tool: HackerNewsTool,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.provider = provider
        self.tool = tool
        self.sleep = sleep

    def run(
        self,
        progress: Callable[[dict[str, Any]], None] | None = None,
        item_completed: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        observation = self.tool.execute(progress=progress)
        candidates = observation["items"]
        if len(candidates) < NEWS_LIMIT:
            raise ValueError(
                f"当前只找到 {len(candidates)} 条 AI 来源，不足以生成 {NEWS_LIMIT} 条简讯"
            )
        selected_sources = candidates[:NEWS_LIMIT]
        system_prompt = (
            "将给定的一条 Hacker News 来源翻译并整理为中文AI简讯。title不超过48字；"
            "summary控制在150字左右，必须为130至180个字符，并包含：文章解决或讨论的核心问题、"
            "关键结论、具体技术要点或实现方式、适用场景或潜在影响。使用连贯的2至3句话，不写空泛套话。"
            "只能依据title、hn_text和article_text，不得补充来源中未提供的参数、性能、发布日期或结论。"
            "source_id必须原样返回。不要返回作者、时间或链接，这些字段由本地程序从原始数据回填。"
        )
        briefs: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for index, source in enumerate(selected_sources, start=1):
            try:
                draft = self._generate_one_with_retry(
                    source,
                    index,
                    system_prompt,
                    progress,
                )
                brief = self._build_brief(source, draft)
                briefs.append(brief)
                if item_completed:
                    item_completed(self._digest(observation, briefs, failures, index, False))
            except Exception as exc:
                if not self._retryable_generation_error(exc):
                    raise
                failures.append({
                    "source_id": source["id"],
                    "source_title": source["title"],
                    "error": str(exc)[:500],
                })
                if progress:
                    progress({
                        "percent": 72 + round(28 * index / NEWS_LIMIT),
                        "stage": f"第 {index}/{NEWS_LIMIT} 条生成失败",
                        "detail": "已保留成功简讯，将继续处理下一条来源",
                    })
        if not briefs:
            raise RuntimeError(f"{NEWS_LIMIT} 条来源均未能生成有效简讯")
        digest = self._digest(
            observation,
            briefs,
            failures,
            NEWS_LIMIT,
            True,
        )
        if progress:
            progress({
                "percent": 100,
                "stage": "抓取完成",
                "detail": f"已生成 {len(briefs)} 条中文简讯，失败 {len(failures)} 条",
            })
        return digest

    def _generate_one_with_retry(
        self,
        source: dict[str, Any],
        index: int,
        system_prompt: str,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> BriefDraft:
        last_error: Exception | None = None
        attempts_made = 0
        for attempt in range(1, MODEL_GENERATION_ATTEMPTS + 1):
            attempts_made = attempt
            source_payload = {
                "source_id": source["id"],
                "title": source["title"],
                "hn_text": source["text"],
                "article_text": source["article_text"][:5000],
                "score": source["score"],
                "comments": source["comments"],
                "age_hours": source["age_hours"],
            }
            if progress:
                progress({
                    "percent": 72 + round(28 * (index - 1) / NEWS_LIMIT),
                    "stage": f"正在生成第 {index}/{NEWS_LIMIT} 条",
                    "detail": f"当前来源：{source['title']} · 第 {attempt}/{MODEL_GENERATION_ATTEMPTS} 次尝试",
                })
            try:
                draft_payload = self.provider.complete(
                    [{
                        "role": "user",
                        "content": "Hacker News 单条来源事实：\n"
                        + json.dumps(source_payload, ensure_ascii=False),
                    }],
                    system_prompt,
                    BRIEF_SCHEMA,
                )
                draft = BriefDraft.model_validate(draft_payload)
                if draft.source_id != source["id"]:
                    raise ValueError("模型返回的 source_id 与当前来源不一致")
                if progress:
                    progress({
                        "percent": 72 + round(28 * index / NEWS_LIMIT),
                        "stage": f"已完成第 {index}/{NEWS_LIMIT} 条",
                        "detail": draft.title,
                    })
                return draft
            except Exception as exc:
                last_error = exc
                if not self._retryable_generation_error(exc) or attempt == MODEL_GENERATION_ATTEMPTS:
                    break
                delay = MODEL_RETRY_DELAYS[attempt - 1]
                if progress:
                    progress({
                        "percent": 72 + round(28 * (index - 1) / NEWS_LIMIT),
                        "stage": f"第 {index}/{NEWS_LIMIT} 条生成失败，准备重试",
                        "detail": f"第 {attempt}/{MODEL_GENERATION_ATTEMPTS} 次失败，{int(delay)} 秒后重试：{str(exc)[:240]}",
                    })
                self.sleep(delay)
        raise RuntimeError(
            f"第 {index}/{NEWS_LIMIT} 条简讯生成失败，已尝试 {attempts_made} 次：{last_error}"
        ) from last_error

    @staticmethod
    def _build_brief(source: dict[str, Any], draft: BriefDraft) -> dict[str, Any]:
        return NewsBrief(
            id=source["id"],
            title=draft.title.strip(),
            summary=draft.summary.strip(),
            published_at=datetime.fromtimestamp(source["created_at"], timezone.utc).isoformat(),
            author=source["author"],
            original_url=source["original_url"],
            discussion_url=source["discussion_url"],
            original_title=source["title"],
        ).model_dump()

    @staticmethod
    def _digest(
        observation: dict[str, Any],
        briefs: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        processed: int,
        complete: bool,
    ) -> dict[str, Any]:
        return {
            "source": observation["source"],
            "fetched_at": observation["fetched_at"],
            "mode": "direct",
            "items": list(briefs),
            "processed": processed,
            "total": NEWS_LIMIT,
            "failures": list(failures),
            "complete": complete,
        }

    @staticmethod
    def _retryable_generation_error(exc: Exception) -> bool:
        if isinstance(exc, ValidationError):
            return True
        detail = str(exc).lower()
        permanent_markers = (
            "api_key is not configured",
            "invalid api key",
            "authentication",
            "unauthorized",
            "permission denied",
            "was not found in path",
            "could not be started",
            "unknown provider",
        )
        return not any(marker in detail for marker in permanent_markers)


def validate_cached_digest(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return None
    try:
        items = [NewsBrief.model_validate(item).model_dump() for item in payload["items"]]
        fetched_at = int(payload["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 1 <= len(items) <= NEWS_LIMIT:
        return None
    return {**payload, "fetched_at": fetched_at, "items": items}
