import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_COOLDOWN_MINUTES = int(os.environ.get("META_API_COOLDOWN_MINUTES", "60"))
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()
GITHUB_TOKEN = os.environ.get("META_GITHUB_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


class MetaRateLimitError(RuntimeError):
    def __init__(self, context="", code=None, subcode=None, http_status=None):
        self.context = context
        self.code = code
        self.subcode = subcode
        self.http_status = http_status
        super().__init__(
            f"Meta rate limit — {context}; HTTP {http_status}, "
            f"code={code}, subcode={subcode}"
        )


def _now():
    return datetime.now(timezone.utc)


def _path(channel):
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(channel or "main"))
    return f".meta_api_cooldown_{safe}.json"


def _load(channel):
    try:
        with open(_path(channel), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"⚠️ API guard state read error: {exc}", flush=True)
        return {}


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def cooldown_active(channel="main"):
    state = _load(channel)
    until = _parse_dt(state.get("until"))
    return bool(until and _now() < until.astimezone(timezone.utc)), state


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            plain = html.unescape(re.sub(r"<[^>]+>", "", message))
            retry = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": plain,
                "disable_web_page_preview": "true",
            }).encode("utf-8")
            try:
                urllib.request.urlopen(
                    urllib.request.Request(url, data=retry),
                    timeout=10,
                )
                return
            except Exception:
                pass
        print(f"⚠️ API guard Telegram HTTP {exc.code}", flush=True)
    except Exception as exc:
        print(f"⚠️ API guard Telegram error: {exc}", flush=True)


def _persist(channel, state, commit_message):
    path = _path(channel)
    content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)

    if not REPOSITORY or not GITHUB_TOKEN:
        print(
            "⚠️ API cooldown збережено лише локально: "
            "META_GITHUB_TOKEN/GITHUB_REPOSITORY не задані.",
            flush=True,
        )
        return False

    encoded_path = urllib.parse.quote(path, safe="/")
    url = f"https://api.github.com/repos/{REPOSITORY}/contents/{encoded_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fb-ads-automator-api-guard",
    }

    sha = None
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as response:
            current = json.loads(response.read().decode("utf-8"))
            sha = current.get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Cooldown state GET HTTP {exc.code}: {body}") from exc

    payload = {
        "message": commit_message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cooldown state PUT HTTP {exc.code}: {body}") from exc


def set_cooldown(channel, reason, triggered_by, minutes=None):
    minutes = int(minutes or DEFAULT_COOLDOWN_MINUTES)
    now = _now()
    until = now + timedelta(minutes=minutes)
    state = {
        "channel": channel,
        "set_at": now.isoformat(),
        "until": until.isoformat(),
        "reason": str(reason),
        "triggered_by": str(triggered_by),
    }
    _persist(channel, state, f"Set Meta API cooldown: {channel}")
    send_telegram(
        "🚨 <b>META API RATE LIMIT</b>\n"
        f"Канал: <b>{html.escape(str(channel))}</b>\n"
        f"Автоматика Meta ставить API на паузу на <b>{minutes} хв</b>.\n"
        f"Причина: {html.escape(str(reason))}\n"
        "Cron залишається активним; наступні запуски під час cooldown "
        "завершаться без Meta API-запитів.\n"
        "Реклама продовжує працювати."
    )
    print(f"🚨 Meta API cooldown до {until.isoformat()} ({channel})", flush=True)


def clear_after_success(channel):
    active, state = cooldown_active(channel)
    if active:
        return
    previous_until = _parse_dt(state.get("until"))
    if not previous_until:
        return

    cleared = {
        "channel": channel,
        "until": None,
        "recovered_at": _now().isoformat(),
        "previous_reason": state.get("reason"),
        "previous_triggered_by": state.get("triggered_by"),
    }
    _persist(channel, cleared, f"Clear Meta API cooldown: {channel}")
    send_telegram(
        "✅ <b>META API ВІДНОВИЛОСЬ</b>\n"
        f"Канал: <b>{html.escape(str(channel))}</b>\n"
        "Перший запуск після cooldown завершився без нового rate limit."
    )
    print(f"✅ Meta API cooldown очищено ({channel})", flush=True)
