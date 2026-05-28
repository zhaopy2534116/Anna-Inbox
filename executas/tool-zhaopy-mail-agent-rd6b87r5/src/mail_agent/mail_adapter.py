"""Mail adapter — reads from local Gmail cache and live Gmail API."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .types import MessageDetail, MessageLite, ThreadContext

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"

SUPPORTED_MAILBOXES = {
    "zhaopy2121@gamil.com": "zhaopy2121@gmail.com",
    "zhaopy2121@gmail.com": "zhaopy2121@gmail.com",
    "kate@anna.partners": "kate@anna.partners",
    "hr@anna.partners": "hr@anna.partners",
}


def beijing_now() -> str:
    return datetime.now(BEIJING_TZ).isoformat()


def _tool_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _repo_root() -> Path:
    # mail_adapter.py is at src/mail_agent/ → parents[5] = repo root
    return Path(__file__).resolve().parents[5]


def sanitize_mailbox_id(mailbox: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in mailbox.strip())
    return safe.strip("._") or "default"


_discovered_email: str = ""


def normalize_mailbox(mailbox: str) -> str:
    global _discovered_email
    raw = str(mailbox or "").strip().lower()

    # Platform path: if a token is available, discover the authorized email once.
    if os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN"):
        if not _discovered_email:
            _discovered_email = get_authorized_email().lower()
        if not _discovered_email:
            raise ValueError("Gmail token is present but could not resolve authorized email from profile")
        # Accept any mailbox that matches the discovered email; treat unknown names as the discovered email.
        if raw == _discovered_email:
            return _discovered_email
        # Also accept known aliases from the static allowlist that resolve to the same address.
        known = SUPPORTED_MAILBOXES.get(raw)
        if known and known == _discovered_email:
            return known
        # Accept the raw input if it's a valid email format (contains @).
        if "@" in raw and "." in raw.split("@")[-1]:
            return raw
        return _discovered_email

    # Local dev path — use static allowlist.
    normalized = SUPPORTED_MAILBOXES.get(raw)
    if not normalized:
        raise ValueError(f"Unsupported mailbox: {mailbox}")
    return normalized


# ── Cache paths ───────────────────────────────────────────────────

def cache_dir() -> Path:
    override = os.environ.get("ZHAOPY_MAIL_AGENT_DATA_DIR")
    base = Path(override).expanduser().resolve() if override else _tool_root() / ".data"
    path = base / "gmail_cache" / "mailboxes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _mailbox_cache_dir(mailbox: str) -> Path:
    path = cache_dir() / sanitize_mailbox_id(mailbox)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path(mailbox: str) -> Path:
    return _mailbox_cache_dir(mailbox) / "index.json"


def _message_path(mailbox: str, message_id: str) -> Path:
    return _mailbox_cache_dir(mailbox) / f"{sanitize_mailbox_id(message_id)}.json"


# ── Cache read / write ────────────────────────────────────────────

def list_messages(mailbox: str) -> list[dict[str, Any]]:
    path = _index_path(mailbox)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return payload.get("messages") if isinstance(payload, dict) else []


def read_cache(mailbox: str) -> dict[str, Any]:
    path = _index_path(mailbox)
    if not path.exists():
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    if not isinstance(payload, dict):
        return {"mailbox": mailbox, "messages": [], "updated_at": None}
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    return {"mailbox": mailbox, "messages": messages, "updated_at": payload.get("updated_at")}


def write_index(mailbox: str, messages: list[dict[str, Any]]) -> None:
    payload = {
        "mailbox": mailbox,
        "updated_at": beijing_now(),
        "message_count": len(messages),
        "messages": messages,
    }
    _index_path(mailbox).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_message(mailbox: str, message: dict[str, Any]) -> None:
    message_id = str(message.get("id") or "")
    if not message_id:
        raise ValueError("Cannot cache Gmail message without id")
    _message_path(mailbox, message_id).write_text(
        json.dumps(message, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read_message(mailbox: str, message_id: str) -> dict[str, Any]:
    path = _message_path(mailbox, message_id)
    if not path.exists():
        raise ValueError(f"Cached message not found: {message_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Cached message is invalid: {message_id}")
    return payload


def message_summary(message: dict[str, Any]) -> dict[str, Any]:
    body_text = str(message.get("body_text") or "")
    headers = message.get("raw_headers") if isinstance(message.get("raw_headers"), dict) else {}
    summary_keys = [
        "id", "thread_id", "mailbox", "history_id", "internal_date",
        "date", "from", "to", "cc", "bcc", "subject", "message_id",
        "in_reply_to", "references", "label_ids", "snippet",
        "size_estimate", "mime_type", "attachments", "fetched_at",
    ]
    summary = {key: message.get(key) for key in summary_keys}
    summary["body_preview"] = body_text[:500]
    summary["body_length"] = len(body_text)
    summary["raw_header_count"] = len(headers)
    summary["json_file"] = str(_message_path(str(message.get("mailbox") or ""), str(message.get("id") or "")))
    return summary


# ── Gmail API token management ────────────────────────────────────

def _token_dir() -> Path:
    override = os.environ.get("ANNA_INBOX_TOKEN_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / "executas" / "anna-inbox-tool" / ".secrets" / "gmail_tokens"


def _load_token_record(mailbox: str) -> dict[str, Any]:
    candidates = [
        _token_dir() / f"{sanitize_mailbox_id(mailbox)}.json",
        _token_dir() / "default.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            record["_token_file"] = str(path)
            return record
    raise ValueError(f"Local Gmail token file not found for {mailbox}")


def _should_refresh_token(record: dict[str, Any]) -> bool:
    if not record.get("refresh_token"):
        return False
    try:
        return float(record.get("expires_at") or 0) <= time.time() + 60
    except (TypeError, ValueError):
        return False


def _refresh_access_token(record: dict[str, Any]) -> None:
    client_id = record.get("client_id")
    client_secret = record.get("client_secret")
    refresh_token = record.get("refresh_token")
    if not client_id or not client_secret or not refresh_token:
        raise ValueError("Gmail refresh token is missing client metadata")
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URI, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    record["access_token"] = payload["access_token"]
    if "expires_in" in payload:
        record["expires_at"] = int(time.time()) + int(payload["expires_in"])
    record["updated_at"] = beijing_now()
    token_file = record.get("_token_file")
    if token_file:
        clean_record = {key: value for key, value in record.items() if not key.startswith("_")}
        Path(str(token_file)).write_text(json.dumps(clean_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_access_token(mailbox: str) -> str:
    # Platform-injected credential — use directly, no refresh needed.
    platform_token = os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN")
    if platform_token and platform_token.strip():
        return str(platform_token).strip()

    # Local dev — read from JSON token file with refresh support.
    record = _load_token_record(mailbox)
    if _should_refresh_token(record):
        _refresh_access_token(record)
    token = record.get("access_token")
    if not token:
        raise ValueError(f"Local Gmail access token is missing for {mailbox}")
    return str(token)


def get_authorized_email() -> str:
    """Discover the authorized Gmail account from a platform-injected token.

    Calls Gmail users/me/profile with the token from os.environ.
    Returns the authorized email address, or empty string if not available.
    """
    token = os.environ.get("GMAIL_ACCESS_TOKEN") or os.environ.get("GOOGLE_ACCESS_TOKEN")
    if not token or not token.strip():
        return ""

    req = urllib.request.Request(
        GMAIL_API_BASE + "/users/me/profile",
        headers={"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            profile = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""

    return str(profile.get("emailAddress") or "").strip()


# ── Gmail API request ─────────────────────────────────────────────

def gmail_request(mailbox: str, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    token = get_access_token(mailbox)
    url = GMAIL_API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            detail_json = json.loads(detail) if detail else {"status_code": exc.code}
        except Exception:
            detail_json = {"status_code": exc.code}
        raise ValueError(f"Gmail API request failed: {exc.code} {detail_json}") from exc
    return json.loads(raw) if raw else {}


# ── Gmail message parsing ─────────────────────────────────────────

def _header_map(message: dict[str, Any]) -> dict[str, str]:
    headers = ((message.get("payload") or {}).get("headers") or [])
    result: dict[str, str] = {}
    for header in headers:
        if isinstance(header, dict) and header.get("name"):
            result[str(header["name"]).lower()] = str(header.get("value") or "")
    return result


def _decode_body(message: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = body.get("data")
        if data and mime_type in {"text/plain", "text/html"}:
            try:
                decoded = base64.urlsafe_b64decode(
                    str(data) + "=" * (-len(str(data)) % 4)
                ).decode("utf-8", errors="replace")
                parts.append(decoded)
            except Exception:
                return
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    walk(payload)
    return "\n\n".join(part.strip() for part in parts if part.strip())[:30000]


def _extract_attachments(part: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        filename = str(node.get("filename") or "")
        body = node.get("body") if isinstance(node.get("body"), dict) else {}
        if filename:
            attachments.append({
                "filename": filename,
                "mimeType": node.get("mimeType"),
                "size": body.get("size"),
                "attachmentId": body.get("attachmentId"),
            })
        for child in node.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(part)
    return attachments


def _normalize_message(mailbox: str, message: dict[str, Any]) -> dict[str, Any]:
    headers = _header_map(message)
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "mailbox": mailbox,
        "history_id": message.get("historyId"),
        "internal_date": message.get("internalDate"),
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
        "label_ids": message.get("labelIds") or [],
        "snippet": message.get("snippet") or "",
        "size_estimate": message.get("sizeEstimate"),
        "mime_type": payload.get("mimeType"),
        "attachments": _extract_attachments(payload),
        "body_text": _decode_body(message),
        "raw_headers": headers,
        "fetched_at": beijing_now(),
    }


# ── Gmail live search ─────────────────────────────────────────────

def search_gmail(mailbox: str, query: str, max_results: int = 100) -> list[str]:
    """Search Gmail with a query string, return list of message IDs."""
    try:
        payload = gmail_request(mailbox, "/users/me/messages", {
            "q": query,
            "maxResults": min(max_results, 500),
            "fields": "messages/id,nextPageToken",
        })
    except ValueError:
        return []
    refs = payload.get("messages") if isinstance(payload, dict) else []
    if not refs:
        return []
    return [str(ref["id"]) for ref in refs if isinstance(ref, dict) and ref.get("id")]


def _is_at_or_before_stop_time(message: dict[str, Any], stop_internal_date: str) -> bool:
    if not stop_internal_date:
        return False
    try:
        return int(message.get("internal_date") or 0) <= int(stop_internal_date)
    except (TypeError, ValueError):
        return False


def fetch_and_cache_message(mailbox: str, message_id: str) -> dict[str, Any] | None:
    """Fetch a full Gmail message and cache it locally. Returns the normalized message dict."""
    try:
        full = gmail_request(
            mailbox,
            f"/users/me/messages/{urllib.parse.quote(message_id, safe='')}",
            {"format": "full"},
        )
    except ValueError:
        return None
    normalized = _normalize_message(mailbox, full)
    write_message(mailbox, normalized)
    return normalized


_FETCH_WORKERS = 6


def live_search_and_cache(
    mailbox: str,
    query: str,
    max_results: int = 100,
    *,
    stop_at_internal_date: str = "",
) -> list[str]:
    """Search Gmail, fetch+cache new messages, return list of message IDs.

    Uses ThreadPoolExecutor to fetch uncached messages concurrently.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    msg_ids = search_gmail(mailbox, query, max_results)
    if not msg_ids:
        return []

    existing = read_cache(mailbox)
    existing_by_id: dict[str, dict[str, Any]] = {
        str(item.get("id")): item for item in existing["messages"] if item.get("id")
    }
    cached_ids = set(existing_by_id.keys())

    # Split into cached (process in order) and uncached (fetch concurrently)
    uncached_to_fetch: list[str] = []
    ordered: list[dict[str, Any]] = []
    for msg_id in msg_ids:
        if msg_id in cached_ids:
            ordered.append(existing_by_id[msg_id])
        else:
            uncached_to_fetch.append(msg_id)
            ordered.append({})  # placeholder, filled after concurrent fetch

    # Fetch uncached messages concurrently
    if uncached_to_fetch:
        fetched: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            future_to_mid = {pool.submit(fetch_and_cache_message, mailbox, mid): mid for mid in uncached_to_fetch}
            for future in as_completed(future_to_mid):
                mid = future_to_mid[future]
                try:
                    result = future.result()
                    if result:
                        fetched[mid] = message_summary(result)
                except Exception:
                    pass

        # Fill placeholders with fetched results
        for i, entry in enumerate(ordered):
            if isinstance(entry, dict) and not entry:
                mid = msg_ids[i]
                if mid in fetched:
                    ordered[i] = fetched[mid]

    # Build returned IDs respecting stop time boundary
    returned_ids: list[str] = []
    for mid, msg in zip(msg_ids, ordered):
        if isinstance(msg, dict) and msg:
            if _is_at_or_before_stop_time(msg, stop_at_internal_date):
                break
            returned_ids.append(mid)

    # Update index
    all_cached = dict(existing_by_id)
    for msg in ordered:
        if isinstance(msg, dict) and msg:
            all_cached[str(msg.get("id"))] = msg

    merged = sorted(all_cached.values(), key=lambda item: int(item.get("internal_date") or 0), reverse=True)
    write_index(mailbox, merged)
    return returned_ids


# ── MessageLite / MessageDetail / ThreadContext ────────────────────

def _to_message_lite(msg: dict[str, Any]) -> MessageLite:
    """Convert cached message summary to MessageLite."""
    return MessageLite(
        message_id=str(msg.get("id") or ""),
        thread_id=str(msg.get("thread_id") or ""),
        from_addr=str(msg.get("from") or ""),
        to_addr=str(msg.get("to") or ""),
        cc=str(msg.get("cc") or ""),
        subject=str(msg.get("subject") or ""),
        snippet=str(msg.get("snippet") or ""),
        internal_date=str(msg.get("internal_date") or ""),
        label_ids=[str(l) for l in (msg.get("label_ids") or [])],
        unread="UNREAD" in str(msg.get("label_ids") or "").upper(),
        starred="STARRED" in str(msg.get("label_ids") or "").upper(),
        important="IMPORTANT" in str(msg.get("label_ids") or "").upper(),
        has_attachment=bool(msg.get("attachments") and len(msg.get("attachments") or []) > 0),
        headers={
            "list_unsubscribe": str(msg.get("raw_headers", {}).get("list-unsubscribe", "")),
            "list_id": str(msg.get("raw_headers", {}).get("list-id", "")),
            "auto_submitted": str(msg.get("raw_headers", {}).get("auto-submitted", "")),
            "precedence": str(msg.get("raw_headers", {}).get("precedence", "")),
        },
    )


def get_messages_lite(mailbox: str, message_ids: list[str]) -> list[MessageLite]:
    results: list[MessageLite] = []
    for msg_id in message_ids:
        path = _message_path(mailbox, msg_id)
        if not path.exists():
            continue
        try:
            msg = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            results.append(_to_message_lite(msg))
    return results


def get_message_detail(mailbox: str, message_id: str) -> MessageDetail | None:
    path = _message_path(mailbox, message_id)
    if not path.exists():
        return None
    try:
        msg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(msg, dict):
        return None

    body_text = str(msg.get("body_text") or "")
    return MessageDetail(
        message_id=str(msg.get("id") or ""),
        thread_id=str(msg.get("thread_id") or ""),
        from_addr=str(msg.get("from") or ""),
        to_addr=str(msg.get("to") or ""),
        cc=str(msg.get("cc") or ""),
        subject=str(msg.get("subject") or ""),
        snippet=str(msg.get("snippet") or ""),
        internal_date=str(msg.get("internal_date") or ""),
        label_ids=[str(l) for l in (msg.get("label_ids") or [])],
        unread="UNREAD" in str(msg.get("label_ids") or "").upper(),
        starred="STARRED" in str(msg.get("label_ids") or "").upper(),
        important="IMPORTANT" in str(msg.get("label_ids") or "").upper(),
        has_attachment=bool(msg.get("attachments") and len(msg.get("attachments") or []) > 0),
        headers={
            "list_unsubscribe": str(msg.get("raw_headers", {}).get("list-unsubscribe", "")),
            "list_id": str(msg.get("raw_headers", {}).get("list-id", "")),
            "auto_submitted": str(msg.get("raw_headers", {}).get("auto-submitted", "")),
            "precedence": str(msg.get("raw_headers", {}).get("precedence", "")),
        },
        body_text=body_text,
    )


def get_thread_context(mailbox: str, thread_id: str, max_messages: int = 10) -> ThreadContext:
    all_msgs = list_messages(mailbox)
    thread_msgs: list[MessageDetail] = []
    for summary in all_msgs:
        if str(summary.get("thread_id") or "") == thread_id:
            detail = get_message_detail(mailbox, str(summary.get("id") or ""))
            if detail:
                thread_msgs.append(detail)
        if len(thread_msgs) >= max_messages:
            break

    thread_msgs.sort(key=lambda m: m.internal_date)
    return ThreadContext(thread_id=thread_id, messages=thread_msgs)


# ── Send reply via Gmail API ────────────────────────────────────────

def send_reply(
    mailbox: str,
    thread_id: str,
    to_addr: str,
    body: str,
    *,
    reply_mode: str = "reply_to_sender",
    cc_addr: str = "",
) -> dict[str, Any]:
    """Send a reply email via Gmail API.

    WARNING: This performs a real send. Callers should default to dry_run=True
    and only call this function after explicit user confirmation.
    """
    from email.mime.text import MIMEText
    import base64 as b64

    # Fetch the original message to get Message-ID and subject for threading
    thread_ctx = get_thread_context(mailbox, thread_id, max_messages=1)
    original_msg_id = ""
    original_subject = "Re: "
    if thread_ctx.messages:
        latest = thread_ctx.messages[-1]
        original_subject = latest.subject or ""
        if not original_subject.lower().startswith("re:"):
            original_subject = f"Re: {original_subject}"
        # Try to get Message-ID from headers
        try:
            raw_msg = get_message_detail(mailbox, latest.message_id)
            if raw_msg and raw_msg.headers:
                for h in raw_msg.headers:
                    if h.lower() == "message-id":
                        original_msg_id = raw_msg.headers[h]
                        break
        except Exception:
            pass

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to_addr
    if reply_mode == "reply_all" and cc_addr:
        msg["Cc"] = cc_addr
    msg["Subject"] = original_subject
    if original_msg_id:
        msg["In-Reply-To"] = original_msg_id
        msg["References"] = original_msg_id

    raw_bytes = msg.as_bytes()
    raw_b64 = b64.urlsafe_b64encode(raw_bytes).decode("ascii")

    token = get_access_token(mailbox)
    url = f"{GMAIL_API_BASE}/users/me/messages/send"
    payload = json.dumps({"raw": raw_b64, "threadId": thread_id}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            detail_json = json.loads(detail) if detail else {"status_code": exc.code}
        except Exception:
            detail_json = {"status_code": exc.code}
        raise ValueError(f"Gmail API send failed: {exc.code} {detail_json}") from exc

    return json.loads(raw) if raw else {}
