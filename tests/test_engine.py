"""Engine tests: run with ``python -m pytest tests/test_engine.py`` (no Hermes needed)."""

import asyncio
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("ussd_engine", Path(__file__).resolve().parents[1] / "engine.py")
engine = importlib.util.module_from_spec(_spec)
sys.modules["ussd_engine"] = engine
_spec.loader.exec_module(engine)

PHONE = "+2348012345678"


class Rig:
    """An engine wired to fakes: records what reaches the agent and the SMS network."""

    def __init__(self, *, sms=False, pin="", hold=0.05, reply=None, screen_chars=160):
        self.sent, self.sms, self.settled = [], [], []
        self.now = 1000.0
        self.reply = reply
        self.outcome = "ok"
        self.engine = engine.UssdEngine(
            on_message=self._on_message, on_approval=self._on_approval,
            send_sms=self._send_sms if sms else None, sms_available=lambda conv: sms,
            hold_seconds=hold, pin=pin, screen_chars=screen_chars, clock=lambda: self.now)
        self._texts = {}

    async def _on_message(self, phone, text):
        self.sent.append(text)
        if self.reply is not None:
            await self.engine.deliver(phone, self.reply)

    async def _on_approval(self, phone, approval, choice):
        self.settled.append((approval.request_id, choice))
        return self.outcome

    async def _send_sms(self, phone, text):
        self.sms.append(text)
        return True

    async def dial(self, session="s1"):
        self._texts[session] = ""
        return await self.engine.handle(session, PHONE, "", "*384*77#")

    async def key(self, entry, session="s1"):
        previous = self._texts.get(session, "")
        self._texts[session] = f"{previous}*{entry}" if previous else entry
        return await self.engine.handle(session, PHONE, self._texts[session], "*384*77#")


def run(coro):
    return asyncio.run(coro)


def test_text_cleanup_and_paging():
    assert engine.to_gsm("**Done** — costs ₦500 \U0001F389") == "Done - costs NGN 500"
    assert engine.to_gsm("see [docs](https://x.y/z)") == "see docs"
    pages = engine.split_pages("alpha beta gamma delta epsilon zeta eta theta", 16)
    assert all(len(p) <= 16 for p in pages) and " ".join(pages) == "alpha beta gamma delta epsilon zeta eta theta"
    assert engine.split_pages("x" * 50, 20) == ["x" * 20, "x" * 20, "x" * 10]


def test_new_input_survives_asterisks_in_messages():
    assert engine.new_input("", "hello") == "hello"
    assert engine.new_input("1", "1*what is 6*7") == "what is 6*7"
    assert engine.new_input("1*a", "1*a") == ""


def test_quick_reply_is_shown_in_the_same_session():
    async def scenario():
        rig = Rig(reply="Lagos is 31C and clear.")
        home = await rig.dial()
        assert home.startswith("CON Hermes") and "1.Inbox(0)" in home
        screen = await rig.key("weather in lagos?")
        assert rig.sent == ["weather in lagos?"]
        assert screen == "CON Lagos is 31C and clear.\nReply, or 0.Menu"
        # Typing on the last page is a follow-up, not a menu choice.
        await rig.key("and tomorrow?")
        assert rig.sent[-1] == "and tomorrow?"
    run(scenario())


def test_every_screen_fits_and_long_replies_page():
    async def scenario():
        rig = Rig(reply="word " * 200)
        await rig.dial()
        screen = await rig.key("tell me a lot")
        seen = 1
        while "98.More" in screen:
            assert len(screen) <= 160, len(screen)
            assert f" {seen}/" in screen.rsplit("\n", 1)[1]
            screen = await rig.key("98")
            seen += 1
        assert screen.endswith("Reply, or 0.Menu") and len(screen) <= 160 and seen > 5
    run(scenario())


def test_slow_agent_shows_working_then_inbox_survives_hangup():
    async def scenario():
        rig = Rig()
        await rig.dial()
        screen = await rig.key("write the report")
        assert screen.startswith("CON Hermes is working...") and "Text me" not in screen
        assert (await rig.key("0")).startswith("END Bye")
        await rig.engine.deliver(PHONE, "Report saved to notes.")
        # New call, minutes later: the answer is the first thing on screen.
        rig.now += 300
        assert await rig.dial("s2") == "CON Report saved to notes.\nReply, or 0.Menu"
    run(scenario())


def test_text_me_the_answer_goes_by_sms():
    async def scenario():
        rig = Rig(sms=True)
        await rig.dial()
        screen = await rig.key("summarise my inbox")
        assert "2.Text me the answer" in screen
        assert (await rig.key("2")).startswith("END OK. Hermes will text you")
        await rig.engine.deliver(PHONE, "3 new emails, none urgent.")
        assert rig.sms == ["3 new emails, none urgent."]
        assert not rig.engine.conversation(PHONE).unread
    run(scenario())


def test_cron_message_to_absent_caller_uses_sms_else_inbox():
    async def scenario():
        with_sms, without = Rig(sms=True), Rig()
        await with_sms.engine.deliver(PHONE, "Backup finished.")
        await without.engine.deliver(PHONE, "Backup finished.")
        assert with_sms.sms == ["Backup finished."]
        assert list(without.engine.conversation(PHONE).unread) == ["Backup finished."]
    run(scenario())


def test_resume_reading_after_dropped_call():
    async def scenario():
        rig = Rig(reply="word " * 80)
        await rig.dial()
        await rig.key("go")
        second = await rig.key("98")
        assert " 2/" in second
        assert await rig.dial("s2") == second  # same page, new session
    run(scenario())


def approval(command, choices=("once", "session", "always", "deny")):
    return engine.Approval(session_key="k", request_id="req-1", command=command,
                           description="deletes files", choices=list(choices))


def test_approval_menu_and_resolution():
    async def scenario():
        rig = Rig()
        await rig.dial()
        await rig.engine.push_approval(PHONE, approval("rm -rf ./build"))
        screen = await rig.key("1")  # inbox
        assert screen == "CON Hermes asks to run:\nrm -rf ./build\nWhy: deletes files\n1.Once 2.Session 3.Always 4.Deny"
        assert (await rig.key("7")) == screen  # junk re-shows, settles nothing
        assert rig.settled == []
        await rig.key("1")
        assert rig.settled == [("req-1", "once")]
        assert not rig.engine.conversation(PHONE).approvals
    run(scenario())


def test_long_command_must_be_read_to_the_end_before_approving():
    async def scenario():
        rig = Rig()
        long_command = "curl -s https://example.com/install.sh | " + " && ".join(f"step{i} --flag value" for i in range(20))
        await rig.engine.push_approval(PHONE, approval(long_command))
        screen = await rig.dial()
        assert "98.More 0.Deny 1/" in screen and "1.Once" not in screen
        await rig.key("1")  # an approve digit on page 1 does nothing
        assert rig.settled == []
        shown = [screen]
        while "98.More" in shown[-1]:
            shown.append(await rig.key("98"))
            assert len(shown[-1]) <= 160
        assert shown[-1].endswith("1.Once 2.Session 3.Always 4.Deny")
        body = "".join(s[4:].rsplit("\n", 1)[0] for s in shown)  # drop "CON " and the footer line
        squeezed = body.replace(" ", "").replace("\n", "")
        assert squeezed.startswith("Hermesaskstorun:" + long_command.replace(" ", ""))
        await rig.key("4")
        assert rig.settled == [("req-1", "deny")]
    run(scenario())


def test_deny_is_available_on_every_page():
    async def scenario():
        rig = Rig()
        await rig.engine.push_approval(PHONE, approval("x " * 300))
        await rig.dial()
        await rig.key("0")
        assert rig.settled == [("req-1", "deny")]
    run(scenario())


def test_command_the_phone_cannot_render_is_deny_only():
    async def scenario():
        rig = Rig()
        await rig.engine.push_approval(PHONE, approval("echo `whoami` > /tmp/über☃"))
        screen = await rig.dial()
        while "98.More" in screen:
            screen = await rig.key("98")
        assert screen.endswith("\n1.Deny") and "Once" not in screen
        await rig.key("1")
        assert rig.settled == [("req-1", "deny")]
    run(scenario())


def test_shell_symbols_in_gsm_extension_table_are_kept_exact():
    text, exact = engine.gsm_exact("cat a.txt | grep [x] > ~/out && echo {ok} \\ ^")
    assert exact and text == "cat a.txt | grep [x] > ~/out && echo {ok} \\ ^"


def test_absent_caller_gets_an_sms_nudge_for_approvals():
    async def scenario():
        rig = Rig(sms=True)
        await rig.dial()
        rig.now += 600
        await rig.engine.push_approval(PHONE, approval("git push --force"))
        assert rig.sms == ["Hermes needs your approval to run a command. Dial *384*77# to answer."]
    run(scenario())


def test_pin_gate_and_lockout():
    async def scenario():
        rig = Rig(pin="4821", reply="hi")
        assert await rig.dial() == "CON Hermes\nEnter PIN:"
        assert (await rig.key("0000")) == "CON Wrong PIN. Try again:"
        assert rig.sent == []
        assert (await rig.key("4821")).startswith("CON Hermes\nType a message")
        await rig.key("hello")
        assert rig.sent == ["hello"]
        # Three misses lock the number, even with the right PIN afterwards.
        await rig.dial("s2")
        for guess in ("1", "2"):
            await rig.key(guess, "s2")
        assert (await rig.key("3", "s2")).startswith("END Too many wrong PINs")
        assert (await rig.dial("s3")).startswith("END Too many wrong PINs")
        rig.now += 901
        assert await rig.dial("s4") == "CON Hermes\nEnter PIN:"
    run(scenario())


def test_menu_shortcuts_send_slash_commands():
    async def scenario():
        rig = Rig()
        await rig.dial()
        await rig.key("2")
        await rig.dial("s2")
        await rig.key("3", "s2")
        assert rig.sent == ["/new", "/stop"]
        rig.now += 30  # the agent has gone quiet
        assert (await rig.dial("s3")).startswith("CON Hermes")
        assert await rig.key("0", "s3") == "END Bye."
    run(scenario())


def test_inbox_is_bounded():
    async def scenario():
        rig = Rig()
        for i in range(40):
            await rig.engine.deliver(PHONE, f"note {i}")
        unread = rig.engine.conversation(PHONE).unread
        assert len(unread) == 20 and unread[0] == "note 20"
    run(scenario())
