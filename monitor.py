from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import re
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import math

SUCCESS_LINE = "Monitor completed"
FAILURE_LINE = "Monitor failed"
ENVIRONMENT_NAME = "monitor-state"
CURSOR_SECRET_NAME = "X_POST_ID"
AUTH_ALERT_SECRET_NAME = "X_AUTH_ALERT_SENT"
AUTH_ALERT_MESSAGE = (
    "X to Discord has stopped because the X session could not be authenticated. "
    "Re-authenticate the session to resume mirroring posts."
)
X_EPOCH_MS = 1288834974657
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_@.])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])")
MAX_TIMELINE_PAGES = 5
DISCORD_RETRY_STATUSES = {429, 500, 502, 503, 504}
MIN_RETRY_DELAY = 0.1
MAX_RETRY_DELAY = 60.0


class MonitorError(Exception):
    """Internal sanitized failure."""


class ConfigError(MonitorError):
    """Configuration is missing or invalid."""


class AuthenticationError(MonitorError):
    """X authentication failed after retry attempts were exhausted."""


@dataclass(frozen=True)
class Config:
    source_token: str
    monitored_account: str
    discord_webhook: str
    cursor: int
    repository: str
    gh_token: str
    auth_alert_sent: str


@dataclass(frozen=True)
class PostRecord:
    post_id: int
    tweet: Any
    author_handle: str
    post_url: str


@dataclass(frozen=True)
class FilterResult:
    qualifies: bool


@dataclass(frozen=True)
class TimelineResult:
    records: list[PostRecord]
    pages_retrieved: int
    more_pages: bool
    found_cursor_boundary: bool


def harden_process_output() -> None:
    logging.disable(logging.CRITICAL)
    for name in ("tweety", "httpx", "httpcore", "asyncio"):
        logging.getLogger(name).disabled = True
    warnings.simplefilter("ignore")


def escape_mask_value(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def add_mask(value: Any) -> None:
    if not os.getenv("GITHUB_ACTIONS"):
        return
    text = str(value)
    if text:
        print(f"::add-mask::{escape_mask_value(text)}", flush=True)


def normalize_username(value: str) -> str:
    if not isinstance(value, str):
        raise ConfigError()
    normalized = value.strip()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    if not USERNAME_RE.fullmatch(normalized):
        raise ConfigError()
    return normalized


def validate_webhook(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError()
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ConfigError()
    path_match = re.fullmatch(r"/api/webhooks/([^/]+)/([^/]+)", parsed.path)
    if path_match is None:
        raise ConfigError()
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() != "wait"]
    query.append(("wait", "true"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def parse_cursor(value: str | None) -> int:
    if value is None or value.strip() == "":
        raise ConfigError()
    text = value.strip()
    if not re.fullmatch(r"0|[1-9][0-9]*", text):
        raise ConfigError()
    return int(text)


def parse_auth_alert_state(value: str | None) -> str:
    if value not in {"0", "1"}:
        raise ConfigError()
    return value


def load_config() -> Config:
    source_token = os.getenv("X_SOURCE_TOKEN")
    if not isinstance(source_token, str) or not source_token.strip():
        raise ConfigError()
    gh_token = os.getenv("GH_TOKEN")
    if not isinstance(gh_token, str) or not gh_token.strip():
        raise ConfigError()
    webhook = validate_webhook(os.getenv("DISCORD_WEBHOOK", ""))
    repository = os.getenv("GITHUB_REPOSITORY")
    if not repository or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ConfigError()
    return Config(
        source_token=source_token.strip(),
        monitored_account=normalize_username(os.getenv("X_MONITORED_ACCOUNT", "")),
        discord_webhook=webhook,
        cursor=parse_cursor(os.getenv("X_POST_ID")),
        repository=repository,
        gh_token=gh_token.strip(),
        auth_alert_sent=parse_auth_alert_state(os.getenv("X_AUTH_ALERT_SENT")),
    )


def parse_positive_decimal(value: Any) -> int | None:
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        number = int(value.strip())
    else:
        return None
    return number if number > 0 else None


def get_post_id(tweet: Any) -> int | None:
    for field in ("id", "id_str", "tweet_id", "rest_id"):
        number = parse_positive_decimal(getattr(tweet, field, None))
        if number is not None:
            add_mask(str(number))
            return number
    original = getattr(tweet, "_original_tweet", None)
    if isinstance(original, dict):
        for field in ("id_str", "id", "rest_id"):
            number = parse_positive_decimal(original.get(field))
            if number is not None:
                add_mask(str(number))
                return number
    return None


def flatten_timeline(items: Any) -> list[Any]:
    flattened: list[Any] = []
    visited: set[int] = set()

    def visit(item: Any) -> None:
        if item is None or isinstance(item, (str, bytes, bytearray)):
            return
        marker = id(item)
        if marker in visited:
            return
        visited.add(marker)
        if get_post_id(item) is not None:
            flattened.append(item)
            return
        tweets = getattr(item, "tweets", None)
        if isinstance(tweets, (list, tuple)):
            for child in tweets:
                visit(child)

    iterable = items if isinstance(items, (list, tuple)) else list(items or [])
    for top_level in iterable:
        visit(top_level)
    return flattened


def safe_attr(obj: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def author_handle(tweet: Any) -> str | None:
    author = safe_attr(tweet, ("author", "user"))
    if author is None:
        return None
    for field in ("username", "screen_name", "handle"):
        value = safe_attr(author, (field,))
        if isinstance(value, str):
            with contextlib.suppress(ConfigError):
                normalized = normalize_username(value)
                add_mask(normalized)
                return normalized
    return None


def original_dict(tweet: Any) -> dict[str, Any] | None:
    value = getattr(tweet, "_original_tweet", None)
    return value if isinstance(value, dict) else None


def meaningful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in {"", "0"}
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return len(value) > 0
    return True


def bool_attr(tweet: Any, name: str) -> bool | None:
    value = getattr(tweet, name, None)
    return value if isinstance(value, bool) else None


def determine_reply(tweet: Any) -> bool | None:
    original = original_dict(tweet)
    keys = ("in_reply_to_status_id_str", "in_reply_to_user_id_str", "in_reply_to_screen_name")
    if original is not None:
        present = [key for key in keys if key in original]
        if present:
            return any(meaningful(original.get(key)) for key in present)
    return bool_attr(tweet, "is_reply")


def determine_quote(tweet: Any) -> bool | None:
    original = original_dict(tweet)
    if original is not None:
        quote_flag = original.get("is_quote_status")
        if quote_flag is True:
            return True
        quoted_populated = any(
            meaningful(original.get(key))
            for key in ("quoted_status", "quoted_status_result", "quoted_status_id_str", "quoted_status_permalink")
        )
        if quoted_populated:
            return True
        if quote_flag is False:
            return False
    return bool_attr(tweet, "is_quoted")


def determine_repost(tweet: Any) -> bool | None:
    original = original_dict(tweet)
    if original is not None:
        for key in ("retweeted_status", "retweeted_status_result", "retweeted_status_id_str"):
            if meaningful(original.get(key)):
                return True
        return False
    return bool_attr(tweet, "is_retweet")


def get_text(tweet: Any) -> str | None:
    for field in ("full_text", "text", "content"):
        value = getattr(tweet, field, None)
        if isinstance(value, str):
            return value
    original = original_dict(tweet)
    if original:
        for field in ("full_text", "text"):
            value = original.get(field)
            if isinstance(value, str):
                return value
    return None


def has_mentions(tweet: Any) -> bool | None:
    structured = getattr(tweet, "user_mentions", None)
    if structured is not None:
        try:
            if len(structured) > 0:
                return True
        except Exception:
            return True
    original = original_dict(tweet)
    if original is not None:
        entities = original.get("entities")
        if isinstance(entities, dict) and "user_mentions" in entities:
            mentions = entities.get("user_mentions")
            if isinstance(mentions, (list, tuple)) and len(mentions) > 0:
                return True
            if mentions not in (None, [], ()):
                return True
    text = get_text(tweet)
    if not isinstance(text, str):
        return None
    return MENTION_RE.search(text) is not None


def qualifies(tweet: Any) -> FilterResult:
    reply = determine_reply(tweet)
    quote = determine_quote(tweet)
    repost = determine_repost(tweet)
    mentions = has_mentions(tweet)
    if reply is None or quote is None or repost is None or mentions is None:
        return FilterResult(False)
    return FilterResult(not reply and not quote and not repost and not mentions)


def post_url(tweet: Any, monitored_handle: str, post_id: int) -> str:
    constructed = f"https://x.com/{monitored_handle}/status/{post_id}"
    add_mask(constructed)
    return constructed


def mask_public_profile_data(tweet: Any) -> None:
    author = safe_attr(tweet, ("author", "user"))
    if author is not None:
        for field in ("username", "screen_name", "handle", "name", "display_name", "url", "profile_url"):
            value = safe_attr(author, (field,))
            if isinstance(value, str) and value:
                add_mask(value)
    for media_url in image_candidates(tweet):
        add_mask(media_url)


def build_records(items: Any, monitored_handle: str) -> list[PostRecord]:
    records: dict[int, PostRecord] = {}
    for tweet in flatten_timeline(items):
        post_id = get_post_id(tweet)
        if post_id is None or post_id in records:
            continue
        handle = author_handle(tweet)
        if handle is None or handle.lower() != monitored_handle.lower():
            continue
        mask_public_profile_data(tweet)
        records[post_id] = PostRecord(post_id, tweet, handle, post_url(tweet, monitored_handle, post_id))
    return list(records.values())


def current_snowflake_boundary() -> int:
    boundary = (int(time.time() * 1000) - X_EPOCH_MS) << 22
    if boundary <= 0:
        raise MonitorError()
    add_mask(str(boundary))
    return boundary


async def close_tweety_client(app: Any) -> None:
    with contextlib.suppress(Exception):
        await app.request.session.aclose()


async def authenticate(source_token: str) -> Any:
    from tweety import TwitterAsync
    from tweety.session import MemorySession

    last_app = None
    for attempt in range(3):
        app = TwitterAsync(MemorySession(), timeout=30)
        last_app = app
        try:
            await app.load_auth_token(source_token)
            return app
        except Exception as exc:
            await close_tweety_client(app)
            if attempt == 2:
                raise AuthenticationError() from exc
            await asyncio.sleep(2**attempt)
    raise AuthenticationError() if last_app is None else AuthenticationError()


def split_iter_page(page: Any) -> tuple[Any, Any]:
    if isinstance(page, tuple) and len(page) == 2:
        first, second = page
        if isinstance(first, (list, tuple)) and not isinstance(second, (list, tuple)):
            return second, first
        return first, second
    return None, page


def page_has_more(page_state: Any) -> bool:
    for name in ("has_next_page", "has_next", "has_more", "more", "next_cursor"):
        value = getattr(page_state, name, None)
        if isinstance(value, bool):
            return value
        if meaningful(value):
            return True
    if isinstance(page_state, dict):
        for name in ("has_next_page", "has_next", "has_more", "more", "next_cursor"):
            value = page_state.get(name)
            if isinstance(value, bool):
                return value
            if meaningful(value):
                return True
    return False


async def retrieve_timeline_once(app: Any, monitored_account: str, cursor: int) -> TimelineResult:
    pages_limit = 1 if cursor == 0 else MAX_TIMELINE_PAGES
    records: dict[int, PostRecord] = {}
    pages_retrieved = 0
    more_pages = False
    found_cursor_boundary = False

    async for page in app.iter_tweets(monitored_account, pages=pages_limit, replies=True, wait_time=1):
        pages_retrieved += 1
        page_state, page_items = split_iter_page(page)
        more_pages = page_has_more(page_state)
        page_records = build_records(page_items, monitored_account)
        for record in page_records:
            records.setdefault(record.post_id, record)
            if cursor > 0 and record.post_id <= cursor:
                found_cursor_boundary = True
        if cursor > 0 and (found_cursor_boundary or not more_pages or pages_retrieved >= MAX_TIMELINE_PAGES):
            break
        if cursor == 0:
            break

    sorted_records = sorted(records.values(), key=lambda record: record.post_id)
    if (
        cursor > 0
        and pages_retrieved >= MAX_TIMELINE_PAGES
        and more_pages
        and all(record.post_id > cursor for record in sorted_records)
    ):
        raise MonitorError()
    return TimelineResult(sorted_records, pages_retrieved, more_pages, found_cursor_boundary)


async def retrieve_timeline(app: Any, monitored_account: str, cursor: int) -> TimelineResult:
    for attempt in range(3):
        try:
            return await retrieve_timeline_once(app, monitored_account, cursor)
        except Exception as exc:
            if attempt == 2:
                raise MonitorError() from exc
            await asyncio.sleep(2**attempt)
    raise MonitorError()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"


def iso_timestamp(tweet: Any) -> str | None:
    value = safe_attr(tweet, ("created_at_datetime", "created_at", "date"))
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.isoformat()
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.isoformat()
    return None


def image_candidates(tweet: Any) -> list[str]:
    candidates: list[str] = []
    for attr in ("media", "media_urls", "images", "photos"):
        value = getattr(tweet, attr, None)
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            if isinstance(item, str):
                candidates.append(item)
            else:
                for field in ("media_url_https", "url", "image_url", "preview_image_url"):
                    possible = getattr(item, field, None)
                    if isinstance(possible, str):
                        candidates.append(possible)
    original = original_dict(tweet)
    if original:
        entities = original.get("extended_entities") or original.get("entities")
        if isinstance(entities, dict):
            for media in entities.get("media", []) or []:
                if isinstance(media, dict):
                    for field in ("media_url_https", "url", "media_url"):
                        possible = media.get(field)
                        if isinstance(possible, str):
                            candidates.append(possible)
    return candidates


def first_https_image(tweet: Any) -> str | None:
    for candidate in image_candidates(tweet):
        parsed = urlsplit(candidate)
        if parsed.scheme == "https" and parsed.hostname:
            add_mask(candidate)
            return candidate
    return None


def author_display(tweet: Any, fallback_handle: str) -> str:
    author = safe_attr(tweet, ("author", "user"))
    pieces: list[str] = []
    if author is not None:
        for field in ("name", "display_name"):
            value = safe_attr(author, (field,))
            if isinstance(value, str) and value.strip():
                add_mask(value)
                pieces.append(value.strip())
                break
    pieces.append(f"@{fallback_handle}")
    return truncate(" ".join(pieces), 256)


def discord_payload(record: PostRecord) -> dict[str, Any]:
    text = get_text(record.tweet)
    payload: dict[str, Any] = {"allowed_mentions": {"parse": []}}
    if isinstance(text, str) and text.strip():
        embed: dict[str, Any] = {
            "url": record.post_url,
            "author": {"name": author_display(record.tweet, record.author_handle)},
            "description": truncate(text, 4096),
        }
        timestamp = iso_timestamp(record.tweet)
        if timestamp:
            embed["timestamp"] = timestamp
        image = first_https_image(record.tweet)
        if image:
            embed["image"] = {"url": image}
        payload["content"] = truncate(record.post_url, 2000)
        payload["embeds"] = [embed]
    else:
        payload["content"] = truncate(record.post_url, 2000)
    return payload


def safe_retry_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clamp_retry_delay(value: float) -> float:
    return min(MAX_RETRY_DELAY, max(MIN_RETRY_DELAY, value))


def discord_retry_delay(response: Any, fallback_delay: float) -> float:
    retry_after = None
    with contextlib.suppress(Exception):
        data = response.json()
        if isinstance(data, dict):
            retry_after = safe_retry_number(data.get("retry_after"))
    if retry_after is None:
        retry_after = safe_retry_number(response.headers.get("Retry-After"))
    if retry_after is None:
        retry_after = fallback_delay
    return clamp_retry_delay(retry_after)


async def deliver_discord(webhook: str, payload: dict[str, Any]) -> None:
    import httpx

    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for attempt in range(4):
            try:
                response = await client.post(webhook, json=payload)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 3:
                    raise MonitorError() from exc
                await asyncio.sleep(2**attempt)
                continue
            if 200 <= response.status_code < 300:
                return
            if response.status_code in DISCORD_RETRY_STATUSES:
                if attempt == 3:
                    raise MonitorError()
                await asyncio.sleep(discord_retry_delay(response, float(2**attempt)))
                continue
            raise MonitorError()
    # A timeout after Discord accepts a message may be retried and duplicate the notification.
    raise MonitorError()


def authentication_alert_payload() -> dict[str, Any]:
    return {"content": AUTH_ALERT_MESSAGE, "allowed_mentions": {"parse": []}}


def update_environment_secret(secret_name: str, secret_value: str, repository: str, gh_token: str) -> None:
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env["NO_COLOR"] = "1"
    env["GH_TOKEN"] = gh_token
    command = ["gh", "secret", "set", secret_name, "--env", ENVIRONMENT_NAME, "--repo", repository]
    completed = subprocess.run(
        command,
        input=f"{secret_value}\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise MonitorError()


def update_cursor_secret(post_id: int, repository: str, gh_token: str) -> None:
    update_environment_secret(CURSOR_SECRET_NAME, str(post_id), repository, gh_token)


def update_auth_alert_secret(state: str, repository: str, gh_token: str) -> None:
    update_environment_secret(AUTH_ALERT_SECRET_NAME, state, repository, gh_token)


async def handle_authentication_failure(config: Config) -> None:
    if config.auth_alert_sent == "0":
        await deliver_discord(config.discord_webhook, authentication_alert_payload())
        update_auth_alert_secret("1", config.repository, config.gh_token)
    raise AuthenticationError()


async def process(config: Config) -> None:
    try:
        app = await authenticate(config.source_token)
    except AuthenticationError:
        await handle_authentication_failure(config)
    try:
        if config.auth_alert_sent == "1":
            update_auth_alert_secret("0", config.repository, config.gh_token)
        timeline = await retrieve_timeline(app, config.monitored_account, config.cursor)
        records = timeline.records
        if config.cursor == 0:
            new_cursor = max((record.post_id for record in records), default=None)
            if new_cursor is None:
                new_cursor = current_snowflake_boundary()
            update_cursor_secret(new_cursor, config.repository, config.gh_token)
            return
        cursor = config.cursor
        for record in (record for record in records if record.post_id > cursor):
            if qualifies(record.tweet).qualifies:
                await deliver_discord(config.discord_webhook, discord_payload(record))
            update_cursor_secret(record.post_id, config.repository, config.gh_token)
            cursor = record.post_id
    finally:
        await close_tweety_client(app)


async def async_main() -> None:
    harden_process_output()
    config = load_config()
    add_mask(config.source_token)
    add_mask(config.monitored_account)
    add_mask(config.discord_webhook)
    add_mask(config.gh_token)
    await process(config)


def main() -> int:
    try:
        asyncio.run(async_main())
    except BaseException:
        print(FAILURE_LINE, flush=True)
        return 1
    print(SUCCESS_LINE, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
