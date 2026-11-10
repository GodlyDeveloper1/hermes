"""USSD conversation engine: turns agent messages into keypad screens.

Pure Python, no Hermes imports, so it can be tested on its own. The adapter
feeds it three things (a USSD request, an agent message, an approval prompt)
and gives it three callbacks (dispatch a user message, settle an approval,
send an SMS).

USSD in one paragraph: the phone dials a short code, the network POSTs each
keypress round to a webhook, and the reply is one screen of plain text that
starts with ``CON`` (keep the session open and show an input box) or ``END``
(show the text and hang up). A screen holds about 160-182 characters, the
network gives up if the reply takes more than a few seconds, and the whole
session dies after a minute or two of silence. An agent turn can take far
longer than that, so nothing here assumes the reply arrives inside the
session: replies land in an inbox that survives hang-ups, and the caller
can ask for the answer by SMS instead.
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Deque, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- text

# GSM 03.38 basic character set: what a keypad phone is guaranteed to render.
_GSM_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

_REPLACEMENTS = {
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-", "…": "...", "•": "-", "·": "-",
    "→": "->", "←": "<-", "✓": "ok", "✔": "ok", "✕": "x", "✖": "x",
    "₦": "NGN ", "€": "EUR ", "₹": "INR ", "₵": "GHS ", " ": " ",
    "\t": " ", "\r": "", "[": "(", "]": ")", "{": "(", "}": ")", "~": "-", "^": "", "|": "/",
    "\\": "/", "`": "'",
}

_MD_SUBS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"```[A-Za-z0-9_+-]*\n?"), ""),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.DOTALL), r"\1"),
    (re.compile(r"(?<!\w)__(.+?)__(?!\w)", re.DOTALL), r"\1"),
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", re.DOTALL), r"\1"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),
    (re.compile(r"^\s*[*+]\s+", re.MULTILINE), "- "),
    (re.compile(r"^\s*>\s?", re.MULTILINE), ""),
)


def strip_markdown(text: str) -> str:
    """Drop markdown markers a keypad phone would show as literal symbols."""
    for pattern, repl in _MD_SUBS:
        text = pattern.sub(repl, text)
    return text


def to_gsm(text: str, *, markdown: bool = True) -> str:
    """Plain GSM-7 text: no markdown, no emoji, accents folded where GSM lacks them."""
    if markdown:
        text = strip_markdown(text)
    out: List[str] = []
    for ch in text:
        ch = _REPLACEMENTS.get(ch, ch)
        for c in ch:
            if c in _GSM_BASIC:
                out.append(c)
                continue
            folded = unicodedata.normalize("NFKD", c)
            out.extend(f for f in folded if f in _GSM_BASIC)
    cleaned = "".join(out)
    cleaned = re.sub(r"[ ]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# GSM 03.38 extension table: two septets each, but every GSM phone renders them.
_GSM_EXTENSION = frozenset("^{}\\[~]|\u20ac")


def gsm_exact(text: str) -> Tuple[str, bool]:
    """``text`` for a keypad screen without changing its meaning, and whether that worked.

    Used for shell commands in approval prompts, where folding a backtick into a
    quote or dropping an emoji would show the caller a different command from the
    one that runs. Only whitespace is normalised (tabs and carriage returns).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    exact = all(c in _GSM_BASIC or c in _GSM_EXTENSION for c in text)
    if exact:
        return text, True
    return "".join(c if (c in _GSM_BASIC or c in _GSM_EXTENSION) else "?" for c in text), False


def split_pages(text: str, budget: int) -> List[str]:
    """Split ``text`` into chunks of at most ``budget`` characters, on line or word breaks."""
    budget = max(8, budget)
    rest = text.strip()
    pages: List[str] = []
    while len(rest) > budget:
        window = rest[: budget + 1]
        cut = window.rfind("\n")
        if cut < budget // 2:
            cut = max(cut, window.rfind(" "))
        if cut <= 0:
            cut = budget
        pages.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest or not pages:
        pages.append(rest)
    return pages


def new_input(previous: str, current: str) -> str:
    """The caller's latest entry.

    Gateways send every entry of the session joined by ``*`` (``1*hello*98``).
    Splitting on ``*`` would break a typed message that contains one, so the
    entry is whatever follows the text already seen.
    """
    if current == previous:
        return ""
    if not previous:
        return current
    if current.startswith(previous + "*"):
        return current[len(previous) + 1:]
    return current.rsplit("*", 1)[-1]


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"[^\d+]", "", phone or "")
    if digits and not digits.startswith("+"):
        digits = "+" + digits
    return digits


# --------------------------------------------------------------------------- state

_CHOICE_LABELS = {"once": "Once", "session": "Session", "always": "Always", "deny": "Deny"}


@dataclass
class Approval:
    """A command the agent wants to run, waiting on the caller's keypad."""
    session_key: str
    request_id: Optional[str]
    command: str
    description: str
    choices: List[str]
    pages: List[str] = field(default_factory=list)
    index: int = 0
    unreadable: bool = False


@dataclass
class Conversation:
    """Everything kept for one phone number. Survives hang-ups."""
    phone: str
    unread: Deque[str] = field(default_factory=deque)
    reading: List[str] = field(default_factory=list)
    reading_index: int = 0
    approvals: Deque[Approval] = field(default_factory=deque)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    last_typing: float = 0.0
    last_seen: float = 0.0
    service_code: str = ""
    sms_on_arrival: bool = False
    simulated: bool = False
    pin_fails: int = 0
    locked_until: float = 0.0

    @property
    def has_news(self) -> bool:
        return bool(self.unread or self.approvals)


@dataclass
class Session:
    """One dial-in. Dies when the caller hangs up or the network times out."""
    phone: str
    previous_text: str = ""
    view: str = "home"
    pin_ok: bool = False
    fresh: bool = True
    touched: float = 0.0


MessageFn = Callable[[str, str], Awaitable[None]]
ApprovalFn = Callable[[str, Approval, str], Awaitable[str]]
SmsFn = Callable[[str, str], Awaitable[bool]]


class UssdEngine:
    """Screen state machine. One instance per adapter."""

    MORE = "98"
    MAX_UNREAD = 20
    MAX_MESSAGE_CHARS = 4000
    SESSION_TTL = 600.0
    PIN_ATTEMPTS = 3
    PIN_LOCK_SECONDS = 900.0

    def __init__(
        self,
        *,
        on_message: MessageFn,
        on_approval: ApprovalFn,
        send_sms: Optional[SmsFn] = None,
        sms_available: Callable[[Conversation], bool] = lambda conv: False,
        screen_chars: int = 160,
        hold_seconds: float = 6.0,
        offline_after: float = 90.0,
        pin: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_message = on_message
        self._on_approval = on_approval
        self._send_sms = send_sms
        self._sms_available = sms_available
        self.screen_chars = max(60, min(int(screen_chars), 182))
        self.hold_seconds = max(0.0, float(hold_seconds))
        self.offline_after = float(offline_after)
        self._pin = pin or ""
        self._clock = clock
        self._conversations: Dict[str, Conversation] = {}
        self._sessions: Dict[str, Session] = {}
        self._tasks: set = set()

    # ------------------------------------------------------------------ lookups

    def conversation(self, phone: str) -> Conversation:
        conv = self._conversations.get(phone)
        if conv is None:
            conv = self._conversations[phone] = Conversation(phone=phone)
        return conv

    def _session(self, session_id: str, phone: str) -> Session:
        now = self._clock()
        if len(self._sessions) > 256:
            for key in [k for k, s in self._sessions.items() if now - s.touched > self.SESSION_TTL]:
                self._sessions.pop(key, None)
        sess = self._sessions.get(session_id)
        if sess is None or sess.phone != phone:
            sess = self._sessions[session_id] = Session(phone=phone)
        sess.touched = now
        return sess

    def _sms_ok(self, conv: Conversation) -> bool:
        return self._send_sms is not None and bool(self._sms_available(conv))

    def _spawn(self, coro: Awaitable) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ inbound from the agent

    def note_typing(self, phone: str) -> None:
        self.conversation(phone).last_typing = self._clock()

    def _online(self, conv: Conversation) -> bool:
        return conv.last_seen > 0 and self._clock() - conv.last_seen <= self.offline_after

    async def deliver(self, phone: str, content: str) -> None:
        """An agent message for ``phone``: to the inbox, or by SMS when the caller is gone."""
        text = to_gsm(content)[: self.MAX_MESSAGE_CHARS]
        if not text:
            return
        conv = self.conversation(phone)
        if self._sms_ok(conv) and (conv.sms_on_arrival or not self._online(conv)):
            if await self._send_sms(phone, text):  # type: ignore[misc]
                return
        conv.unread.append(text)
        while len(conv.unread) > self.MAX_UNREAD:
            conv.unread.popleft()
        conv.event.set()

    async def push_approval(self, phone: str, approval: Approval) -> None:
        """Queue an approval prompt; nudge an absent caller by SMS."""
        conv = self.conversation(phone)
        approval.pages = self._approval_pages(approval)
        conv.approvals.append(approval)
        conv.event.set()
        if self._sms_ok(conv) and (conv.sms_on_arrival or not self._online(conv)):
            code = conv.service_code or "your Hermes code"
            await self._send_sms(phone, f"Hermes needs your approval to run a command. Dial {code} to answer.")  # type: ignore[misc]

    # ------------------------------------------------------------------ inbound from the phone

    async def handle(self, session_id: str, phone: str, text: str, service_code: str = "",
                     *, simulated: bool = False) -> str:
        """One USSD round: returns the ``CON``/``END`` screen."""
        conv = self.conversation(phone)
        sess = self._session(session_id, phone)
        now = self._clock()
        conv.last_seen = now
        conv.sms_on_arrival = False
        conv.simulated = conv.simulated or simulated
        if service_code:
            conv.service_code = service_code
        entry = new_input(sess.previous_text, text or "").strip()
        sess.previous_text = text or ""
        first = sess.fresh
        sess.fresh = False

        if conv.locked_until > now:
            return self._end("Too many wrong PINs. Try again later.")
        if self._pin and not sess.pin_ok:
            if not entry:
                return self._con("Hermes\nEnter PIN:")
            if hmac.compare_digest(entry.encode(), self._pin.encode()):
                sess.pin_ok = True
                conv.pin_fails = 0
                entry = ""
                first = True
            else:
                conv.pin_fails += 1
                if conv.pin_fails >= self.PIN_ATTEMPTS:
                    conv.pin_fails = 0
                    conv.locked_until = now + self.PIN_LOCK_SECONDS
                    return self._end("Too many wrong PINs. Try again later.")
                return self._con("Wrong PIN. Try again:")

        if first and not entry:
            return self._landing(conv, sess)
        return await self._route(conv, sess, entry)

    async def _route(self, conv: Conversation, sess: Session, entry: str) -> str:
        if sess.view == "approval" and conv.approvals:
            return await self._approval_entry(conv, sess, entry)
        if sess.view == "wait":
            if entry in ("", "1"):
                return await self._await_and_show(conv, sess)
            if entry == "2" and self._sms_ok(conv):
                conv.sms_on_arrival = True
                return self._end("OK. Hermes will text you the answer.")
            if entry == "3":
                return await self._send(conv, sess, "/stop")
            if entry == "0":
                return self._end("Bye. The answer will wait in your inbox.")
            return await self._send(conv, sess, entry)
        if sess.view == "read" and conv.reading:
            last = conv.reading_index >= len(conv.reading) - 1
            if entry == "" or (entry == self.MORE and last):
                return self._show_reading(conv, sess)
            if entry == self.MORE and not last:
                conv.reading_index += 1
                return self._show_reading(conv, sess)
            if entry == "9" and not last and self._sms_ok(conv):
                rest = "\n".join(conv.reading[conv.reading_index + 1:])
                conv.reading, conv.reading_index = [], 0
                self._spawn(self._send_sms(conv.phone, rest))  # type: ignore[misc]
                return self._end("Sent the rest by SMS.")
            if entry == "0":
                if last:
                    conv.reading, conv.reading_index = [], 0
                return self._home(conv, sess)
            if last:
                conv.reading, conv.reading_index = [], 0
            return await self._send(conv, sess, entry)
        # home
        if entry == "":
            return self._landing(conv, sess)
        if entry == "1":
            if conv.approvals or conv.unread or conv.reading:
                return self._landing(conv, sess, force=True)
            return self._home(conv, sess, note="No new messages.")
        if entry == "2":
            return await self._send(conv, sess, "/new")
        if entry == "3":
            return await self._send(conv, sess, "/stop")
        if entry == "0":
            return self._end("Bye.")
        return await self._send(conv, sess, entry)

    # ------------------------------------------------------------------ screens

    @staticmethod
    def _con(text: str) -> str:
        return "CON " + text

    @staticmethod
    def _end(text: str) -> str:
        return "END " + text

    def _working(self, conv: Conversation) -> bool:
        return conv.last_typing > 0 and self._clock() - conv.last_typing < 5.0

    def _landing(self, conv: Conversation, sess: Session, *, force: bool = False) -> str:
        """What a caller sees first: whatever is most urgent."""
        if conv.approvals:
            return self._show_approval(conv, sess)
        if conv.reading and conv.reading_index < len(conv.reading) - 1:
            return self._show_reading(conv, sess)  # pick up where the last call dropped
        if conv.unread:
            return self._open_inbox(conv, sess)
        if conv.reading and force:
            return self._show_reading(conv, sess)
        if self._working(conv):
            return self._wait_screen(conv, sess)
        return self._home(conv, sess)

    def _home(self, conv: Conversation, sess: Session, note: str = "") -> str:
        sess.view = "home"
        waiting = len(conv.unread) + len(conv.approvals) + (1 if conv.reading else 0)
        head = note or "Hermes"
        return self._con(f"{head}\nType a message, or:\n1.Inbox({waiting}) 2.New chat\n3.Stop 0.Exit")

    def _wait_screen(self, conv: Conversation, sess: Session) -> str:
        sess.view = "wait"
        lines = ["Hermes is working...", "1.Check again"]
        if self._sms_ok(conv):
            lines.append("2.Text me the answer")
        lines.append("3.Stop")
        lines.append("0.Hang up, read later")
        return self._con("\n".join(lines))

    def _open_inbox(self, conv: Conversation, sess: Session) -> str:
        document = "\n\n".join(conv.unread)
        conv.unread.clear()
        if not conv.approvals:
            conv.event.clear()
        more = self._more_footer(conv, 99, 99)
        budget = self.screen_chars - 4 - 1 - max(len(more), len(self._LAST_FOOTER))
        conv.reading = split_pages(document, budget)
        conv.reading_index = 0
        return self._show_reading(conv, sess)

    _LAST_FOOTER = "Reply, or 0.Menu"

    def _more_footer(self, conv: Conversation, page: int, total: int) -> str:
        sms = " 9.SMS" if self._sms_ok(conv) else ""
        return f"{self.MORE}.More{sms} 0.Menu {page}/{total}"

    def _show_reading(self, conv: Conversation, sess: Session) -> str:
        sess.view = "read"
        total = len(conv.reading)
        index = min(conv.reading_index, total - 1)
        footer = self._LAST_FOOTER if index >= total - 1 else self._more_footer(conv, index + 1, total)
        return self._con(f"{conv.reading[index]}\n{footer}")

    # ------------------------------------------------------------------ approvals

    def _approval_footer(self, approval: Approval) -> str:
        return " ".join(f"{i}.{_CHOICE_LABELS.get(c, c.title())}" for i, c in enumerate(approval.choices, start=1))

    _APPROVAL_MORE = "98.More 0.Deny"

    def _approval_pages(self, approval: Approval) -> List[str]:
        """The whole command, paged. The choices sit on the last page only, so
        nobody can approve a command they have not read to the end."""
        command, exact = gsm_exact(approval.command)
        if not exact:
            # The phone cannot show this command faithfully: it can be refused here, never approved.
            approval.unreadable = True
            approval.choices = [c for c in approval.choices if c == "deny"]
        body = "Hermes asks to run:\n" + command
        why = to_gsm(approval.description or "")
        if why:
            body += "\nWhy: " + why
        if approval.unreadable:
            body += "\nThis phone cannot show every symbol (?). Deny here, or approve on another device."
        budget = self.screen_chars - 4 - 1 - max(len(self._approval_footer(approval)), len(self._APPROVAL_MORE) + 6)
        return split_pages(body, budget)

    def _show_approval(self, conv: Conversation, sess: Session, note: str = "") -> str:
        sess.view = "approval"
        approval = conv.approvals[0]
        total = len(approval.pages)
        last = approval.index >= total - 1
        footer = self._approval_footer(approval) if last else f"{self._APPROVAL_MORE} {approval.index + 1}/{total}"
        prefix = f"{note}\n" if note else ""
        return self._con(f"{prefix}{approval.pages[approval.index]}\n{footer}")

    async def _approval_entry(self, conv: Conversation, sess: Session, entry: str) -> str:
        approval = conv.approvals[0]
        last = approval.index >= len(approval.pages) - 1
        choice: Optional[str] = None
        if entry == "0" and "deny" in approval.choices:
            choice = "deny"
        elif not last:
            if entry == self.MORE:
                approval.index += 1
            return self._show_approval(conv, sess)
        elif entry.isdigit() and 1 <= int(entry) <= len(approval.choices):
            choice = approval.choices[int(entry) - 1]
        if choice is None:
            return self._show_approval(conv, sess)
        outcome = await self._on_approval(conv.phone, approval, choice)
        conv.approvals.popleft()
        if not conv.has_news:
            conv.event.clear()
        if outcome.startswith("refused"):
            return self._end(to_gsm(outcome.partition(":")[2]) or "You cannot answer this approval.")
        if outcome == "expired":
            return self._home(conv, sess, note="That request expired.")
        return await self._await_and_show(conv, sess)

    # ------------------------------------------------------------------ talking to the agent

    async def _send(self, conv: Conversation, sess: Session, text: str) -> str:
        if not conv.has_news:
            conv.event.clear()
        conv.last_typing = self._clock()
        await self._on_message(conv.phone, text)
        return await self._await_and_show(conv, sess)

    async def _await_and_show(self, conv: Conversation, sess: Session) -> str:
        """Hold the screen for a few seconds in case the agent is quick."""
        if not conv.has_news and self.hold_seconds > 0:
            try:
                await asyncio.wait_for(conv.event.wait(), timeout=self.hold_seconds)
            except asyncio.TimeoutError:
                pass
        if conv.approvals:
            return self._show_approval(conv, sess)
        if conv.unread:
            return self._open_inbox(conv, sess)
        return self._wait_screen(conv, sess)
