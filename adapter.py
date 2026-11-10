"""USSD gateway adapter for Hermes Agent (Africa's Talking wire format).

Inbound: the telco gateway POSTs each keypress round of a USSD session to
``/ussd/<USSD_WEBHOOK_SECRET>`` as form data (``sessionId``, ``phoneNumber``,
``serviceCode``, ``text``) and shows whatever plain text comes back.

Outbound: agent messages go to a per-number inbox that the next screen reads
from. With Africa's Talking SMS credentials set, messages for a caller who has
hung up (or asked for it) go out as SMS instead.

All screen logic lives in ``engine.py``; this file is the Hermes and HTTP glue.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import ipaddress
import json
import logging
import re
import urllib.parse
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, get_scoped_secret, seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, ExecApprovalPrompt, SendResult, gateway_trust_env
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import redact_phone

from .engine import Approval, Conversation, UssdEngine, normalize_phone, to_gsm

try:
    import aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # declared in plugin.yaml; absent only on a broken install
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLATFORM_NAME = "ussd"
DEFAULT_PORT = 8788
DEFAULT_HOST = "127.0.0.1"
MIN_SECRET_LENGTH = 16
MAX_BODY_BYTES = 16_384
MAX_TEXT_CHARS = 2_000
SMS_CHUNK = 1_200
AT_LIVE_URL = "https://api.africastalking.com/version1/messaging"
AT_SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"
_PHONE_RE = re.compile(r"^\+\d{6,15}$")
_FORWARD_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "X-Real-Ip", "Cf-Connecting-Ip")
_SIMULATOR_DIR = Path(__file__).resolve().parent / "simulator"
_SIMULATOR_PAGE = _SIMULATOR_DIR / "index.html"
_SIMULATOR_FONT = _SIMULATOR_DIR / "montserrat.woff2"  # bundled, SIL OFL 1.1 (simulator/OFL.txt)

_PLATFORM_HINT = (
    "You are replying over USSD to a keypad phone. One screen shows about 130 characters of plain "
    "text and the reader pages through anything longer, so lead with the answer and keep it to one "
    "or two short sentences unless asked for more. No markdown, tables, emoji or links. The user "
    "types on a numeric keypad: when you need a choice from them, offer numbered options."
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _setting(extra: Optional[dict], key: str, env: str, default: Any = "") -> str:
    return str(extra_or_secret(extra, key, env, default) or "").strip()


def _number(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _at_endpoint(username: str) -> str:
    return AT_SANDBOX_URL if username.strip().lower() == "sandbox" else AT_LIVE_URL


async def _at_send_sms(session: Any, *, username: str, api_key: str, sender_id: str, to: str, message: str) -> bool:
    """Send one SMS through Africa's Talking. True when the gateway accepted it."""
    form = {"username": username, "to": to, "message": message}
    if sender_id:
        form["from"] = sender_id
    headers = {"apiKey": api_key, "Accept": "application/json"}
    try:
        async with session.post(_at_endpoint(username), data=form, headers=headers) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.error("[ussd] SMS to %s refused: HTTP %s", redact_phone(to), resp.status)
                return False
            try:
                recipients = json.loads(body)["SMSMessageData"]["Recipients"]
            except (ValueError, KeyError, TypeError):
                recipients = []
            accepted = any(str(r.get("status", "")).lower() == "success" for r in recipients if isinstance(r, dict))
            if not accepted:
                logger.error("[ussd] SMS to %s not accepted by Africa's Talking", redact_phone(to))
            return accepted
    except Exception as exc:  # network errors must not take the agent turn down
        logger.error("[ussd] SMS to %s failed: %s", redact_phone(to), type(exc).__name__)
        return False


def _sms_chunks(text: str) -> list:
    return [text[i:i + SMS_CHUNK] for i in range(0, len(text), SMS_CHUNK)] or [""]


class UssdAdapter(BasePlatformAdapter):
    """Hermes on a dial code: one session per phone number."""

    MAX_MESSAGE_LENGTH = 4000
    splits_long_messages = True  # send() pages anything, so cron hands over the whole payload

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))
        extra = getattr(config, "extra", None) or {}
        self._secret = _setting(extra, "webhook_secret", "USSD_WEBHOOK_SECRET")
        self._host = _setting(extra, "webhook_host", "USSD_WEBHOOK_HOST", DEFAULT_HOST) or DEFAULT_HOST
        self._port = int(_number(_setting(extra, "webhook_port", "USSD_WEBHOOK_PORT", DEFAULT_PORT), DEFAULT_PORT))
        self._simulator = _setting(extra, "simulator", "USSD_SIMULATOR", "true").lower() not in ("0", "false", "no", "off")
        self._at_username = _setting(extra, "at_username", "AFRICASTALKING_USERNAME")
        self._at_api_key = _setting(extra, "at_api_key", "AFRICASTALKING_API_KEY")
        self._at_sender = _setting(extra, "at_sender_id", "AFRICASTALKING_SENDER_ID")
        self._allow_all = _truthy(get_scoped_secret("USSD_ALLOW_ALL_USERS", ""))
        raw_allowed = str(get_scoped_secret("USSD_ALLOWED_USERS", "") or "")
        self._allowed = {normalize_phone(p) for p in raw_allowed.split(",") if p.strip() and p.strip() != "*"}
        self._allow_all = self._allow_all or "*" in [p.strip() for p in raw_allowed.split(",")]
        self._engine = UssdEngine(
            on_message=self._dispatch,
            on_approval=self._settle_approval,
            send_sms=self._send_sms,
            sms_available=self._sms_available,
            screen_chars=int(_number(_setting(extra, "screen_chars", "USSD_SCREEN_CHARS", 160), 160)),
            hold_seconds=_number(_setting(extra, "hold_seconds", "USSD_HOLD_SECONDS", 6), 6.0),
            pin=_setting(extra, "pin", "USSD_PIN"),
        )
        self._runner: Any = None
        self._http: Any = None
        self._tasks: set = set()
        self._sim_sms: Dict[str, Deque[str]] = {}

    # ------------------------------------------------------------------ lifecycle

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error("ussd_missing_aiohttp", "[ussd] aiohttp is not installed", retryable=False)
            return False
        if len(self._secret) < MIN_SECRET_LENGTH:
            message = (
                f"[ussd] Refusing to start: USSD_WEBHOOK_SECRET must be at least {MIN_SECRET_LENGTH} characters. "
                "USSD gateways do not sign their callbacks, so this secret in the callback URL is what "
                "stops anyone else from posting as your phone. Generate one with: openssl rand -hex 24")
            logger.error(message)
            self._set_fatal_error("ussd_weak_secret", message, retryable=False)
            return False
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_post("/ussd/{token}", self._handle_ussd)
        app.router.add_get("/ussd/{token}/sms", self._handle_sim_sms)
        app.router.add_get("/sim", self._handle_sim_page)
        app.router.add_get("/sim/montserrat.woff2", self._handle_sim_font)
        app.router.add_get("/health", self._handle_health)
        runner = web.AppRunner(app, access_log=None)  # the callback path holds the secret: never log it
        await runner.setup()
        try:
            await web.TCPSite(runner, self._host, self._port).start()
        except OSError as exc:
            await runner.cleanup()
            message = f"[ussd] Cannot listen on {self._host}:{self._port}: {exc}"
            logger.error(message)
            self._set_fatal_error("ussd_bind_failed", message, retryable=True)
            return False
        self._runner = runner
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=gateway_trust_env())
        self._mark_connected(listener_base=f"http://{self._host}:{self._port}")
        logger.info("[ussd] Listening on %s:%d (callback path /ussd/<secret>; SMS %s; simulator %s)",
                    self._host, self._port, "on" if self._at_ready else "off", "on" if self._simulator else "off")
        return True

    async def disconnect(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._http is not None:
            await self._http.close()
            self._http = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    # ------------------------------------------------------------------ outbound (agent -> phone)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict] = None) -> SendResult:
        phone = normalize_phone(chat_id)
        if not _PHONE_RE.match(phone):
            return SendResult(success=False, error="USSD chat ids are phone numbers in international format (+234...)")
        await self._engine.deliver(phone, content or "")
        return SendResult(success=True, message_id=uuid.uuid4().hex)

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        self._engine.note_typing(normalize_phone(chat_id))

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        return to_gsm(content)

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Render an exec approval as a numbered keypad menu (see ``UssdEngine._approval_pages``)."""
        phone = normalize_phone(prompt.chat_id)
        if not _PHONE_RE.match(phone):
            return SendResult(success=False, error="not a phone number")
        await self._engine.push_approval(phone, Approval(
            session_key=prompt.session_key, request_id=prompt.request_id, command=prompt.command or "",
            description=prompt.description or "", choices=list(prompt.choices)))
        return SendResult(success=True, message_id=prompt.request_id or uuid.uuid4().hex)

    # ------------------------------------------------------------------ engine callbacks

    async def _dispatch(self, phone: str, text: str) -> None:
        """Hand a typed message to the gateway without blocking the USSD reply."""
        message_id = uuid.uuid4().hex
        source = self.build_source(chat_id=phone, chat_name=phone, chat_type="dm", user_id=phone,
                                   user_name=phone, message_id=message_id)
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                             raw_message={"transport": "ussd"}, message_id=message_id)
        task = asyncio.create_task(self.handle_message(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _settle_approval(self, phone: str, approval: Approval, choice: str) -> str:
        """Resolve exactly the request this menu was drawn for. Returns ok / expired / refused:<why>."""
        try:
            from gateway.approval_actor import approval_refusal, refusal_text
            from tools.approval import resolve_gateway_approval_card
            requester = approval_refusal(approval.session_key, phone, extra=self.config.extra,
                                         request_id=approval.request_id)
            if requester:
                return "refused:" + refusal_text(requester)
            count = resolve_gateway_approval_card(approval.session_key, choice, approval.request_id,
                                                  actor_id=phone, extra=self.config.extra)
        except Exception as exc:
            logger.error("[ussd] approval from %s could not be settled: %s", redact_phone(phone), exc)
            return "expired"
        logger.info("[ussd] %s answered an approval: %s (%d settled)", redact_phone(phone), choice, count)
        return "ok" if count else "expired"

    @property
    def _at_ready(self) -> bool:
        return bool(self._at_username and self._at_api_key)

    def _sms_available(self, conv: Conversation) -> bool:
        return self._at_ready or (self._simulator and conv.simulated)

    async def _send_sms(self, phone: str, text: str) -> bool:
        conv = self._engine.conversation(phone)
        if self._simulator and conv.simulated:  # the built-in simulator has its own SMS tray
            tray = self._sim_sms.setdefault(phone, deque(maxlen=30))
            tray.extend(_sms_chunks(text))
            return True
        if not self._at_ready or self._http is None:
            return False
        ok = True
        for chunk in _sms_chunks(text):
            ok = await _at_send_sms(self._http, username=self._at_username, api_key=self._at_api_key,
                                    sender_id=self._at_sender, to=phone, message=chunk) and ok
        return ok

    # ------------------------------------------------------------------ HTTP

    def _token_ok(self, request: Any) -> bool:
        token = request.match_info.get("token", "")
        return hmac.compare_digest(token.encode("utf-8"), self._secret.encode("utf-8"))

    @staticmethod
    def _is_local(request: Any) -> bool:
        """True for a request made on this machine and not relayed by a tunnel or proxy."""
        if any(header in request.headers for header in _FORWARD_HEADERS):
            return False
        try:
            return ipaddress.ip_address(request.remote or "").is_loopback
        except ValueError:
            return False

    async def _handle_health(self, request: Any) -> Any:
        return web.Response(text="ok")

    async def _handle_ussd(self, request: Any) -> Any:
        if not self._token_ok(request):
            return web.Response(status=404, text="not found")
        try:
            raw = await request.read()
            form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except Exception:
            return web.Response(status=400, text="bad request")

        def field(name: str) -> str:
            return (form.get(name) or [""])[0]

        session_id = field("sessionId").strip()[:128]
        phone = normalize_phone(field("phoneNumber"))
        text = field("text")[:MAX_TEXT_CHARS]
        if not session_id or not _PHONE_RE.match(phone):
            return web.Response(status=400, text="bad request")
        if self._allowed and not self._allow_all and phone not in self._allowed:
            logger.warning("[ussd] refused %s: not in USSD_ALLOWED_USERS", redact_phone(phone))
            return self._screen("END This number is not allowed to use this Hermes.")
        simulated = self._simulator and self._is_local(request) and request.headers.get("X-Hermes-Ussd-Simulator") == "1"
        try:
            screen = await self._engine.handle(session_id, phone, text, field("serviceCode").strip()[:32],
                                               simulated=simulated)
        except Exception:
            logger.exception("[ussd] screen failed for %s", redact_phone(phone))
            screen = "END Hermes hit an error. Please dial again."
        return self._screen(screen)

    @staticmethod
    def _screen(text: str) -> Any:
        return web.Response(text=text, content_type="text/plain", charset="utf-8")

    async def _handle_sim_page(self, request: Any) -> Any:
        if not (self._simulator and self._is_local(request)):
            return web.Response(status=404, text="not found")
        try:
            page = _SIMULATOR_PAGE.read_text(encoding="utf-8")
        except OSError:
            return web.Response(status=404, text="not found")
        return web.Response(text=page, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def _handle_sim_font(self, request: Any) -> Any:
        """The simulator's typeface, served from the plugin folder so the page fetches nothing remote."""
        if not (self._simulator and self._is_local(request)):
            return web.Response(status=404, text="not found")
        try:
            font = _SIMULATOR_FONT.read_bytes()
        except OSError:
            return web.Response(status=404, text="not found")
        return web.Response(body=font, content_type="font/woff2",
                            headers={"Cache-Control": "max-age=86400", "X-Content-Type-Options": "nosniff"})

    async def _handle_sim_sms(self, request: Any) -> Any:
        """SMS tray for the built-in simulator: local requests only, drained on read."""
        if not (self._simulator and self._is_local(request) and self._token_ok(request)):
            return web.Response(status=404, text="not found")
        phone = normalize_phone(request.query.get("phone", ""))
        tray = self._sim_sms.get(phone)
        messages = list(tray) if tray else []
        if tray:
            tray.clear()
        return web.json_response({"messages": messages}, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- registration

def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE and bool(str(get_scoped_secret("USSD_WEBHOOK_SECRET", "") or "").strip())


def validate_config(config: Any) -> bool:
    return bool(_setting(getattr(config, "extra", None), "webhook_secret", "USSD_WEBHOOK_SECRET"))


def _env_enablement() -> Optional[dict]:
    if not str(get_scoped_secret("USSD_WEBHOOK_SECRET", "") or "").strip():
        return None
    return seed_extra_from_env(
        (("USSD_WEBHOOK_SECRET", "webhook_secret", None),
         ("USSD_WEBHOOK_HOST", "webhook_host", None),
         ("USSD_WEBHOOK_PORT", "webhook_port", None)),
        home_env="USSD_HOME_CHANNEL",
    )


async def _standalone_send(pconfig: Any, chat_id: str, message: str, *, thread_id: Any = None,
                           media_files: Any = None, force_document: bool = False) -> dict:
    """Cron and ``hermes send`` delivery when no gateway is running: USSD is pull-only, so this is SMS."""
    extra = getattr(pconfig, "extra", None) or {}
    username = _setting(extra, "at_username", "AFRICASTALKING_USERNAME")
    api_key = _setting(extra, "at_api_key", "AFRICASTALKING_API_KEY")
    sender = _setting(extra, "at_sender_id", "AFRICASTALKING_SENDER_ID")
    phone = normalize_phone(chat_id)
    if not AIOHTTP_AVAILABLE:
        return {"error": "aiohttp is not installed"}
    if not _PHONE_RE.match(phone):
        return {"error": "USSD targets are phone numbers in international format (+234...)"}
    if not (username and api_key):
        return {"error": "USSD can only push through SMS: set AFRICASTALKING_USERNAME and AFRICASTALKING_API_KEY"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=gateway_trust_env()) as session:
        for chunk in _sms_chunks(to_gsm(message)):
            if not await _at_send_sms(session, username=username, api_key=api_key, sender_id=sender,
                                      to=phone, message=chunk):
                return {"error": "Africa's Talking did not accept the SMS"}
    return {"success": True, "platform": PLATFORM_NAME, "chat_id": phone, "message_id": uuid.uuid4().hex}


def register(ctx: Any) -> None:
    """Plugin entry point."""
    kwargs: Dict[str, Any] = dict(
        name=PLATFORM_NAME,
        label="USSD (Africa's Talking)",
        adapter_factory=UssdAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["USSD_WEBHOOK_SECRET"],
        install_hint="Enable the plugin with `hermes plugins enable ussd-platform` so aiohttp is installed",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="USSD_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="USSD_ALLOWED_USERS",
        allow_all_env="USSD_ALLOW_ALL_USERS",
        max_message_length=UssdAdapter.MAX_MESSAGE_LENGTH,
        platform_hint=_PLATFORM_HINT,
        emoji="\U0001F4DF",
    )
    # Newer PlatformEntry fields: older cores reject unknown kwargs, so feature-detect.
    from gateway.platform_registry import PlatformEntry
    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    for key, value in (
        ("display_tier", "minimal"),
        ("pii_safe", True),
        ("allowlist_suppresses_pairing", True),
        ("dashboard_description", "Talk to Hermes from any phone by dialling a USSD code."),
        ("docs_url", "https://developers.africastalking.com/docs/ussd/overview"),
    ):
        if key in known:
            kwargs[key] = value
    ctx.register_platform(**{k: v for k, v in kwargs.items() if k in known or k in ("name", "label", "adapter_factory")})
