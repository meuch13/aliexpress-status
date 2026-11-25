import argparse
import base64
import json
import random
import re
import shutil
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_QUERY = "from:aliexpress.com newer_than:180d"
CACHE_DIR = Path(".cache/gmail_messages")

DELIVERED_KEYWORDS = (
    "has been delivered",
    "order delivered",
    "package delivered",
    "delivery completed",
    "delivery confirmed",
    "confirm receipt",
    "order completed",
)

ORDER_ID_PATTERNS = [
    re.compile(r"order\s*(?:id|no\.?|number|#)?\s*[:#]?\s*(\d{14,20})", re.IGNORECASE),
    re.compile(r"\b(8\d{13,17})\b"),
]

NOISE_PHRASES = (
    "aliexpress",
    "view order",
    "track order",
    "manage orders",
    "unsubscribe",
    "privacy policy",
    "terms of use",
    "customer service",
    "help center",
    "download app",
    "all rights reserved",
    "order number",
    "order id",
    "tracking number",
    "shipped",
    "delivered",
    "in transit",
    "customs",
    "out for delivery",
    "payment",
    "confirm receipt",
    "estimated delivery",
    "total:",
    "subtotal:",
    "shipping fee",
)


def get_gmail_service(
    credentials_path: Path = Path("credentials.json"),
    token_path: Path = Path("token.json"),
):
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


def execute_with_backoff(request, max_retries: int = 6):
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp else None
            is_rate_limit = status == 429 or (
                status == 403 and b"rateLimitExceeded" in (exc.content or b"")
            )
            if not is_rate_limit or attempt == max_retries - 1:
                raise
            sleep_s = min(60.0, (2 ** attempt) + random.uniform(0.1, 0.8))
            time.sleep(sleep_s)


def decode_base64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8")).decode(
        "utf-8", errors="replace"
    )


def extract_body_parts(payload: dict[str, Any]) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            decoded = decode_base64url(data)
            if mime == "text/plain":
                plain_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(plain_parts), "\n".join(html_parts)


def _score_item_candidate(text: str) -> int:
    low = text.lower()
    if len(text) < 6 or len(text) > 140:
        return -100
    if any(phrase in low for phrase in NOISE_PHRASES):
        return -50
    if re.fullmatch(r"[\d.,\s$£€¥+-]+", text):
        return -50
    if re.search(r"\b\d{13,20}\b", text):
        return -40
    if "http://" in low or "https://" in low or "@" in text:
        return -50

    score = 0
    if re.search(r"[a-zA-Z]{3,}", text):
        score += 10
    if 12 <= len(text) <= 90:
        score += 10
    if re.search(r"\b(x\s*\d+|qty|pack|pcs|set|usb|led|cable|case|board|sensor|charger|kit)\b", low):
        score += 12
    return score


def extract_item_description(subject: str, plain_text: str, html_text: str) -> str:
    best_text = ""
    best_score = -100

    if html_text:
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        for el in soup.find_all(["td", "div", "p", "a", "span", "li"]):
            text = " ".join(el.get_text(" ", strip=True).split())
            score = _score_item_candidate(text)
            if score > best_score:
                best_score = score
                best_text = text

    for raw_line in plain_text.splitlines():
        line = " ".join(raw_line.split())
        score = _score_item_candidate(line)
        if score > best_score:
            best_score = score
            best_text = line

    if best_score > 0 and best_text:
        return best_text

    cleaned_subj = re.sub(
        r"\b(?:order|tracking)\s*(?:id|no\.?|number|#)?\s*[:#]?\s*[A-Z0-9-]{6,25}",
        "",
        subject,
        flags=re.IGNORECASE,
    )
    cleaned_subj = " ".join(cleaned_subj.strip(" -:|").split())
    return cleaned_subj or "Unknown item"


def parse_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    payload = msg.get("payload", {})
    headers = {
        h["name"].lower(): h["value"] for h in payload.get("headers", [])
    }
    subject = headers.get("subject", "")
    date_header = headers.get("date", "")
    plain_text, html_text = extract_body_parts(payload)

    if plain_text:
        body_text = plain_text
    elif html_text:
        soup = BeautifulSoup(html_text, "html.parser")
        body_text = soup.get_text(" ", strip=True)
    else:
        body_text = ""

    combined = f"{subject}\n{body_text}"

    order_id = None
    for pat in ORDER_ID_PATTERNS:
        m = pat.search(combined)
        if m:
            order_id = m.group(1)
            break

    if not order_id:
        return None

    item_description = extract_item_description(subject, plain_text, html_text)

    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc)
    except Exception:
        internal_ms = int(msg.get("internalDate", "0"))
        dt_utc = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)

    lower_combined = combined.lower()
    is_delivered = any(kw in lower_combined for kw in DELIVERED_KEYWORDS)

    status = "In transit / Update"
    if is_delivered:
        status = "Delivered"
    elif "shipped" in lower_combined or "on the way" in lower_combined:
        status = "Shipped"
    elif "customs" in lower_combined:
        status = "Customs clearance"
    elif "out for delivery" in lower_combined:
        status = "Out for delivery"
    elif "order confirmed" in lower_combined or "payment" in lower_combined:
        status = "Order confirmed"

    clean_subject = " ".join(subject.split())
    last_tracking = f"{dt_utc.strftime('%Y-%m-%d %H:%M')} UTC - {clean_subject}"

    return {
        "order_id": order_id,
        "status": status,
        "item_description": item_description,
        "timestamp": dt_utc,
        "delivered": is_delivered,
        "last_tracking": last_tracking,
    }


def fetch_unreceived_orders(
    service,
    max_messages: int = 100,
    query: str = DEFAULT_QUERY,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    message_refs: list[dict[str, str]] = []
    page_token = None

    while len(message_refs) < max_messages:
        req = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(100, max_messages - len(message_refs)),
                pageToken=page_token,
            )
        )
        resp = execute_with_backoff(req)
        message_refs.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    orders_by_id: dict[str, dict[str, Any]] = {}

    for ref in message_refs:
        msg_id = ref["id"]
        cache_file = CACHE_DIR / f"{msg_id}.json"

        if use_cache and cache_file.exists():
            msg = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            req = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
            )
            msg = execute_with_backoff(req)
            if use_cache:
                cache_file.write_text(json.dumps(msg), encoding="utf-8")
            time.sleep(0.05)

        parsed = parse_message(msg)
        if not parsed:
            continue

        oid = parsed["order_id"]
        existing = orders_by_id.get(oid)

        if existing is None:
            orders_by_id[oid] = parsed
        else:
            if parsed["delivered"]:
                existing["delivered"] = True
            if (
                parsed["item_description"]
                and parsed["item_description"] != "Unknown item"
                and existing["item_description"] == "Unknown item"
            ):
                existing["item_description"] = parsed["item_description"]
            if parsed["timestamp"] > existing["timestamp"]:
                existing["timestamp"] = parsed["timestamp"]
                existing["status"] = parsed["status"]
                existing["last_tracking"] = parsed["last_tracking"]
                if (
                    parsed["item_description"]
                    and parsed["item_description"] != "Unknown item"
                ):
                    existing["item_description"] = parsed["item_description"]

    unreceived = [o for o in orders_by_id.values() if not o["delivered"]]
    unreceived.sort(key=lambda x: x["timestamp"], reverse=True)
    return unreceived


def truncate(text: Any, width: int) -> str:
    s = " ".join(str(text or "").split())
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


def compute_column_widths(term_columns: int) -> list[tuple[str, str, int]]:
    order_id_w = 18
    status_w = 24
    desc_w = 26 if term_columns < 140 else 32
    num_cols = 4
    # Each column has 2 padding spaces + 1 leading/trailing/separator border char
    overhead = (num_cols * 3) + 1
    tracking_w = max(20, term_columns - overhead - (order_id_w + status_w + desc_w))

    return [
        ("order_id", "Order ID", order_id_w),
        ("status", "Status", status_w),
        ("item_description", "Item Description", desc_w),
        ("last_tracking", "Last Tracking", tracking_w),
    ]


def render_ascii_table(rows: list[dict[str, Any]], columns: int | None = None) -> str:
    term_cols = (
        columns
        if columns is not None
        else shutil.get_terminal_size(fallback=(120, 24)).columns
    )
    headers = compute_column_widths(term_cols)

    def border(char: str = "-") -> str:
        return "+" + "+".join(char * (w + 2) for _, _, w in headers) + "+"

    lines = [
        border("-"),
        "| " + " | ".join(f"{title:<{w}}" for _, title, w in headers) + " |",
        border("="),
    ]

    if not rows:
        inner_width = sum(w + 2 for _, _, w in headers) + (len(headers) - 1)
        msg = truncate("No unreceived AliExpress orders found.", max(1, inner_width - 2))
        lines.append("| " + f"{msg:<{inner_width - 2}}" + " |")
        lines.append(border("-"))
        return "\n".join(lines)

    for row in rows:
        display = {
            "order_id": row["order_id"],
            "status": row["status"],
            "item_description": row.get("item_description") or "Unknown item",
            "last_tracking": row["last_tracking"],
        }
        cells = [f"{truncate(display[k], w):<{w}}" for k, _, w in headers]
        lines.append("| " + " | ".join(cells) + " |")
        lines.append(border("-"))

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show unreceived AliExpress orders from Gmail in an ASCII table."
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=f"Gmail search query (default: {DEFAULT_QUERY!r})",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=100,
        help="Maximum messages to scan (default: 100)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass and refresh cached Gmail messages",
    )
    parser.add_argument(
        "--columns",
        type=int,
        default=None,
        help="Override detected terminal width in columns",
    )
    args = parser.parse_args()

    service = get_gmail_service()
    orders = fetch_unreceived_orders(
        service,
        max_messages=args.max_messages,
        query=args.query,
        use_cache=not args.no_cache,
    )
    print(render_ascii_table(orders, columns=args.columns))


if __name__ == "__main__":
    main()