"""Adapter tests against a real Hermes install.

Run from a Hermes checkout's virtualenv:

    python -m pytest -c tests/pytest.ini --rootdir tests tests/test_adapter_live.py

They start the adapter's HTTP server on a free port, post the same form data a
USSD gateway posts, and (for approvals) settle a real ``tools.approval`` entry.
Skipped when Hermes is not importable.
"""

import asyncio
import importlib
import socket
import sys
import urllib.parse
from pathlib import Path

import pytest

pytest.importorskip("gateway.platforms.base")
aiohttp = pytest.importorskip("aiohttp")

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-secret-0123456789abcdef"
PHONE = "+2348012345678"


def _load_plugin():
    """Import the plugin directory as a package, the way the Hermes loader does."""
    name = "ussd_platform_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _register_platform(plugin):
    """Put the plugin's platform in the registry, as enabling the plugin does."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    class Ctx:
        def register_platform(self, **kwargs):
            platform_registry.register(PlatformEntry(source="plugin", plugin_name="ussd-platform", **kwargs),
                                       scope=platform_registry.current_scope_key())

    plugin.register(Ctx())


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Phone:
    def __init__(self, http, base, number=PHONE, simulator=False):
        self.http, self.base, self.number, self.simulator = http, base, number, simulator
        self.text, self.session = "", "sess-1"

    async def post(self, text, *, token=SECRET, headers=None):
        form = {"sessionId": self.session, "serviceCode": "*384*77#", "phoneNumber": self.number, "text": text}
        hdrs = dict(headers or {})
        if self.simulator:
            hdrs["X-Hermes-Ussd-Simulator"] = "1"
        async with self.http.post(f"{self.base}/ussd/{token}", data=urllib.parse.urlencode(form),
                                  headers={"Content-Type": "application/x-www-form-urlencoded", **hdrs}) as resp:
            return resp.status, await resp.text()

    async def dial(self, session="sess-1"):
        self.session, self.text = session, ""
        return (await self.post(""))[1]

    async def key(self, entry):
        self.text = f"{self.text}*{entry}" if self.text else entry
        return (await self.post(self.text))[1]


async def _rig(monkeypatch, *, extra=None, allowed=""):
    plugin = _load_plugin()
    _register_platform(plugin)
    adapter_mod = sys.modules[plugin.__name__ + ".adapter"]
    from gateway.config import PlatformConfig
    monkeypatch.setenv("USSD_ALLOWED_USERS", allowed)
    monkeypatch.delenv("USSD_ALLOW_ALL_USERS", raising=False)
    for name in ("USSD_PIN", "AFRICASTALKING_USERNAME", "AFRICASTALKING_API_KEY", "USSD_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    port = _free_port()
    config = PlatformConfig(enabled=True, extra={"webhook_secret": SECRET, "webhook_port": port,
                                                 "hold_seconds": 0.3, **(extra or {})})
    adapter = adapter_mod.UssdAdapter(config)
    received = []

    async def fake_handle_message(event):  # stands in for the gateway + model
        received.append(event)
        await adapter.send(event.source.chat_id, f"**Echo:** {event.text} ✅")

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
    monkeypatch.setattr(adapter, "_write_runtime_status_safe", lambda *a, **k: None, raising=False)
    assert await adapter.connect()
    return adapter, received, f"http://127.0.0.1:{port}"


def test_registers_one_platform_named_ussd():
    plugin = _load_plugin()
    calls = []

    class Ctx:
        def register_platform(self, **kwargs):
            calls.append(kwargs)

    plugin.register(Ctx())
    assert len(calls) == 1 and calls[0]["name"] == "ussd"
    assert calls[0]["allowed_users_env"] == "USSD_ALLOWED_USERS"
    assert callable(calls[0]["adapter_factory"]) and callable(calls[0]["standalone_sender_fn"])


def test_round_trip_over_http(monkeypatch):
    async def scenario():
        adapter, received, base = await _rig(monkeypatch)
        try:
            async with aiohttp.ClientSession() as http:
                phone = Phone(http, base)
                assert (await phone.dial()).startswith("CON Hermes\nType a message")
                screen = await phone.key("what is 6*7")
                assert screen == "CON Echo: what is 6*7\nReply, or 0.Menu"
                event = received[0]
                assert event.text == "what is 6*7" and event.source.chat_id == PHONE
                assert event.source.user_id == PHONE and event.source.chat_type == "dm"
                assert str(event.source.platform.value) == "ussd"
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())


def test_wrong_secret_bad_phone_and_allowlist(monkeypatch):
    async def scenario():
        adapter, received, base = await _rig(monkeypatch, allowed="+2348000000001")
        try:
            async with aiohttp.ClientSession() as http:
                phone = Phone(http, base)
                status, body = await phone.post("", token="wrong-secret-wrong-secret")
                assert status == 404 and "CON" not in body
                assert (await phone.dial()) == "END This number is not allowed to use this Hermes."
                assert (await Phone(http, base, number="+2348000000001").dial()).startswith("CON Hermes")
                status, _ = await Phone(http, base, number="12; DROP").post("")
                assert status == 400
                assert received == []
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())


def test_weak_secret_refuses_to_start(monkeypatch):
    async def scenario():
        plugin = _load_plugin()
        _register_platform(plugin)
        adapter_mod = sys.modules[plugin.__name__ + ".adapter"]
        from gateway.config import PlatformConfig
        monkeypatch.delenv("USSD_WEBHOOK_SECRET", raising=False)
        adapter = adapter_mod.UssdAdapter(PlatformConfig(enabled=True, extra={"webhook_secret": "short",
                                                                              "webhook_port": _free_port()}))
        monkeypatch.setattr(adapter, "_write_runtime_status_safe", lambda *a, **k: None, raising=False)
        assert await adapter.connect() is False
        assert adapter.has_fatal_error
    asyncio.run(scenario())


def test_simulator_is_local_only_and_has_an_sms_tray(monkeypatch):
    async def scenario():
        adapter, _, base = await _rig(monkeypatch)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(f"{base}/sim") as resp:
                    assert resp.status == 200 and "text/html" in resp.headers["Content-Type"]
                # The same request relayed by a tunnel must not see the page or the tray.
                relayed = {"X-Forwarded-For": "203.0.113.9"}
                async with http.get(f"{base}/sim", headers=relayed) as resp:
                    assert resp.status == 404
                async with http.get(f"{base}/ussd/{SECRET}/sms?phone={urllib.parse.quote(PHONE)}", headers=relayed) as resp:
                    assert resp.status == 404
                phone = Phone(http, base, simulator=True)
                await phone.dial()
                adapter.handle_message = lambda event: asyncio.sleep(0)  # an agent that never answers in time
                assert "2.Text me the answer" in await phone.key("slow question")
                assert (await phone.key("2")).startswith("END OK. Hermes will text you")
                await adapter.send(PHONE, "Here is the late answer.")
                async with http.get(f"{base}/ussd/{SECRET}/sms?phone={urllib.parse.quote(PHONE)}") as resp:
                    assert (await resp.json()) == {"messages": ["Here is the late answer."]}
                async with http.get(f"{base}/ussd/{SECRET}/sms?phone={urllib.parse.quote(PHONE)}") as resp:
                    assert (await resp.json()) == {"messages": []}
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())


def test_real_approval_entry_is_settled_from_the_keypad(monkeypatch):
    async def scenario():
        from tools import approval
        from tools.approval_gateway_wait import _ApprovalEntry
        adapter, _, base = await _rig(monkeypatch)
        session_key = "ussd-test-session"
        entry = _ApprovalEntry({"request_id": "req-42", "command": "rm -rf ./build"})
        entry.card_deny_only = True  # every request starts Deny-only until its card shows the whole command
        monkeypatch.setitem(approval._gateway_queues, session_key, [entry])
        try:
            async with aiohttp.ClientSession() as http:
                phone = Phone(http, base)
                await phone.dial()
                result = await adapter.send_exec_approval(PHONE, "rm -rf ./build", session_key,
                                                          description="recursive delete", request_id="req-42")
                assert result.success
                assert entry.card_deny_only is False  # the base lifted it: our menu carries the whole command
                screen = await phone.key("1")
                assert screen.startswith("CON Hermes asks to run:\nrm -rf ./build\nWhy: recursive delete\n1.Once")
                await phone.key("1")
                assert entry.result == "once" and entry.event.is_set()
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())
