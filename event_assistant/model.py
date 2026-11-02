import math
import re
from urllib.parse import quote, urlparse
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
APP = "https://events.example.com/"


class SourceError(Exception):
    """A safe error code, never a raw response body or authenticated URL."""


class AuthNeeded(SourceError):
    pass


def timestamp(value):
    if value in (None, "", 0, "0", "0000-00-00 00:00:00"):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            result = float(value)
            if result > 10**11:
                result /= 1000
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace("/", "-"))
            result = dt.replace(tzinfo=TZ).timestamp() if dt.tzinfo is None else dt.timestamp()
        if not math.isfinite(result) or not 946684800 <= result <= 4102444800:
            raise ValueError()
        return int(result)
    except (ValueError, TypeError, OverflowError):
        raise SourceError("unrecognized_time_format") from None


def display_time(value):
    return datetime.fromtimestamp(value, TZ).strftime("%Y-%m-%d %H:%M") if value else "尚未公布"


def normalize(raw):
    if not isinstance(raw, dict):
        raise SourceError("invalid_event")
    identity = str(raw.get("id", ""))
    title = raw.get("title")
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', identity) or not isinstance(title, str) or not title.strip():
        raise SourceError("missing_event_identity")
    result = {"id": identity, "title": " ".join(title.split()),
              "url": raw.get("url", APP + "events/" + quote(identity, safe=""))}
    address = urlparse(result['url'])
    if address.scheme not in ('http', 'https') or not address.hostname or address.username or address.password or address.query or address.fragment:
        raise SourceError('event_url_must_be_public_without_query_or_credentials')
    for key in ("start_time", "end_time", "signup_start_time", "signup_end_time"):
        result[key] = timestamp(raw.get(key))
    for start, end in (("start_time", "end_time"), ("signup_start_time", "signup_end_time")):
        if result[start] and result[end] and result[end] < result[start]:
            raise SourceError("invalid_time_range")
    # Only copy necessary event facts; never copy meeting passwords or user profiles.
    return result


def unfinished(event, now):
    return event["end_time"] is None or event["end_time"] > now


def signup_status(event, now):
    begin, end = event["signup_start_time"], event["signup_end_time"]
    if not begin:
        return "报名开始时间尚未公布"
    if now < begin:
        return "报名尚未开始"
    if end and now >= end:
        return "报名时间已结束"
    return "已到报名时间；名额及资格请以原网站为准"
