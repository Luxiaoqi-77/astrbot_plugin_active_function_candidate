"""Offline coverage for owner-only active-function delivery."""

from __future__ import annotations

import asyncio
import importlib
import re
from types import SimpleNamespace
from typing import Any
from unittest import mock

from astrbot.core.message.components import Plain
from data.plugins.astrbot_plugin_active_function_candidate.action_sender import (
    ActionSegment,
    TurnRecallLedger,
    _delayed_owned_recall,
    build_action_plan,
    deliver_action_plan,
    handle_target_action_decorate,
    recall_response_accepted,
)
from data.plugins.astrbot_plugin_active_function_candidate.active_function_candidate import (
    OWNER_SENDER_ID,
    OWNER_UMO,
)


class FakeEvent:
    """Minimal owner-DM event used by the side-effect-free tests."""

    def __init__(self, text: str = ""):
        self.unified_msg_origin = OWNER_UMO
        self.bot = object()
        self.result = SimpleNamespace(chain=[Plain(text)], async_stream=None)
        self.extras: dict[str, Any] = {}
        self.clear_count = 0
        self._has_send_oper = False

    def get_sender_id(self) -> str:
        return OWNER_SENDER_ID

    def get_group_id(self):
        return None

    def get_self_id(self) -> str:
        return "2258780898"

    def get_extra(self, key: str, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value

    def get_result(self):
        return self.result

    def clear_result(self) -> None:
        self.clear_count += 1
        self.result = None


class FakeRecallBot:
    """Bot stub exposing only ``delete_msg`` with a scripted response list."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[int] = []

    async def delete_msg(self, message_id: int):
        self.calls.append(message_id)
        item = self.responses.pop(0) if self.responses else None
        if isinstance(item, BaseException):
            raise item
        return item


class FakeSplitter:
    """Retain delimiters exactly like the splitter helper before post-cleaning."""

    _is_pro_mode = True

    @staticmethod
    def _get_cfg(key: str, default=None):
        values = {
            "split_mode": "regex",
            "clean_after_regex": r"[\r\n]+|\$+$",
        }
        return values.get(key, default)

    @staticmethod
    def split_text_for_active_function(text: str, _event: Any) -> list[str]:
        chunks = []
        current = ""
        for part in re.split(r"(\$+)", text):
            if not part:
                continue
            current += part
            if re.fullmatch(r"\$+", part):
                chunks.append(current)
                current = ""
        if current:
            chunks.append(current)
        return chunks


class FakePlugin:
    """Custom sender surface needed by delivery and duplicate-entry tests."""

    def __init__(self, splitter: FakeSplitter | None = None):
        self.segment_interval = 0
        self.recall_delay = 0
        self.recall_tag = "[recall]"
        self.poke_tag = "[poke]"
        self.recall_enable = True
        self.poke_enable = True
        self.reply_enable = True
        self._candidate_bridge_ready = True
        self._recall_tasks = []
        self.sends = []
        self.pokes = 0
        self._reply_mgr = SimpleNamespace(
            _reply_tag_pattern=re.compile(r"\[reply:(-?\d+)\]", re.I),
            strip_all_reply_variants=lambda value: re.sub(
                r"\[reply:-?\d+\]", "", value, flags=re.I
            ),
        )
        metadata = SimpleNamespace(star_cls=splitter or FakeSplitter())
        self.context = SimpleNamespace(get_registered_star=lambda _name: metadata)

    async def _send_msg(self, _event: Any, _bot: Any, payload: list[dict]):
        self.sends.append(payload)
        return {"message_id": str(len(self.sends))}

    async def _fire_poke(self, _event: Any, _bot: Any) -> None:
        self.pokes += 1


def test_plan_cleans_boundaries_and_preserves_currency() -> None:
    event = FakeEvent()
    plan = build_action_plan(
        event,
        "价格$5.00$第一段$[poke]第二段$收据5$",
        splitter=FakeSplitter(),
    )

    assert plan is not None
    assert [(segment.text, segment.poke_count) for segment in plan] == [
        ("价格$5.00", 0),
        ("第一段", 0),
        ("第二段", 1),
        ("收据5$", 0),
    ]
    assert [segment.fallback_text for segment in plan] == [
        "价格$5.00",
        "第一段",
        "第二段",
        "收据5$",
    ]


def test_plan_keeps_action_order_and_removes_trailing_delimiters() -> None:
    plan = build_action_plan(
        FakeEvent(),
        "A$[poke]B$[recall]C$$",
        splitter=FakeSplitter(),
    )

    assert plan is not None
    assert [(item.text, item.poke_count, item.recall) for item in plan] == [
        ("A", 0, False),
        ("B", 1, False),
        ("C", 0, True),
    ]


def test_delivery_preserves_order_when_poke_fails() -> None:
    event = FakeEvent()
    plugin = FakePlugin()
    calls = []

    async def send_message(_event: Any, _bot: Any, payload: list[dict]):
        calls.append(("send", payload[-1]["data"]["text"]))
        return {"message_id": str(len(calls))}

    async def fail_poke(_event: Any, _bot: Any) -> None:
        calls.append(("poke", None))
        raise RuntimeError("offline poke failure")

    async def scenario():
        return await deliver_action_plan(
            plugin,
            event,
            [
                ActionSegment("A"),
                ActionSegment("B", poke_count=1),
                ActionSegment("C"),
            ],
            ledger=TurnRecallLedger(),
            send_message=send_message,
            fire_poke=fail_poke,
            bot=object(),
        )

    outcome = asyncio.run(scenario())

    assert outcome.status == "complete"
    assert calls == [("send", "A"), ("send", "B"), ("poke", None), ("send", "C")]
    assert event.clear_count == 1


def test_definite_send_failure_does_not_clear_or_retry() -> None:
    event = FakeEvent()

    async def fail_send(_event: Any, _bot: Any, _payload: list[dict]):
        raise RuntimeError("offline send rejection")

    async def unused_poke(_event: Any, _bot: Any) -> None:
        raise AssertionError("poke must not run after a rejected send")

    async def scenario():
        return await deliver_action_plan(
            FakePlugin(),
            event,
            [ActionSegment("A", poke_count=1), ActionSegment("B")],
            ledger=TurnRecallLedger(),
            send_message=fail_send,
            fire_poke=unused_poke,
            bot=object(),
        )

    outcome = asyncio.run(scenario())

    assert outcome.status == "failed"
    assert outcome.confirmed_sends == 0
    assert outcome.fallback_index == 0
    assert event.clear_count == 0


def test_duplicate_decorator_entry_sends_and_pokes_once() -> None:
    event = FakeEvent("A$[poke]")
    event.set_extra("_active_func_bridge_bound", True)
    plugin = FakePlugin()

    async def scenario():
        return await asyncio.gather(
            handle_target_action_decorate(plugin, event),
            handle_target_action_decorate(plugin, event),
        )

    outcomes = asyncio.run(scenario())

    assert outcomes == [True, True]
    assert len(plugin.sends) == 1
    assert plugin.sends[0][-1]["data"]["text"] == "A"
    assert plugin.pokes == 1
    assert event.clear_count == 1


def test_recall_response_accepted_contract() -> None:
    """Only an explicit non-success envelope may reject a recall response."""
    assert recall_response_accepted(None) is True
    assert recall_response_accepted({"message_id": 1}) is True
    assert recall_response_accepted({"status": "ok", "retcode": 0}) is True
    assert recall_response_accepted({"status": "OK"}) is True
    assert recall_response_accepted({"status": "success"}) is True
    assert recall_response_accepted({"status": "ok", "retcode": "0.0"}) is True
    assert recall_response_accepted("opaque-transport-value") is True
    assert recall_response_accepted({"status": "failed"}) is False
    assert recall_response_accepted({"status": "failed", "retcode": 100}) is False
    assert recall_response_accepted({"retcode": 100}) is False
    assert recall_response_accepted({"status": "ok", "retcode": "1"}) is False


def test_failed_recall_envelope_is_not_retried() -> None:
    """A returned failure envelope consumes the claim and never retries."""
    event = FakeEvent()
    event.set_extra("_active_func_bridge_bound", True)
    bot = FakeRecallBot([{"status": "failed", "retcode": 100}])
    ledger = TurnRecallLedger()
    turn_id = ledger.turn_id(event)
    assert ledger.register_send(event, turn_id, "9101") is True

    async def scenario():
        await _delayed_owned_recall(FakePlugin(), event, bot, ledger, turn_id, "9101")
        # The claim is gone, so a second attempt must not touch the platform.
        await _delayed_owned_recall(FakePlugin(), event, bot, ledger, turn_id, "9101")

    asyncio.run(scenario())

    assert bot.calls == [9101]


def test_accepted_recall_envelope_deletes_once() -> None:
    event = FakeEvent()
    event.set_extra("_active_func_bridge_bound", True)
    bot = FakeRecallBot([{"status": "ok", "retcode": 0}])
    ledger = TurnRecallLedger()
    turn_id = ledger.turn_id(event)
    assert ledger.register_send(event, turn_id, "9101") is True

    async def scenario():
        await _delayed_owned_recall(FakePlugin(), event, bot, ledger, turn_id, "9101")

    asyncio.run(scenario())

    assert bot.calls == [9101]


def test_recall_transport_exception_does_not_retry() -> None:
    event = FakeEvent()
    event.set_extra("_active_func_bridge_bound", True)
    bot = FakeRecallBot([RuntimeError("offline delete rejection")])
    ledger = TurnRecallLedger()
    turn_id = ledger.turn_id(event)
    assert ledger.register_send(event, turn_id, "9101") is True

    async def scenario():
        await _delayed_owned_recall(FakePlugin(), event, bot, ledger, turn_id, "9101")
        await _delayed_owned_recall(FakePlugin(), event, bot, ledger, turn_id, "9101")

    asyncio.run(scenario())

    assert bot.calls == [9101]


def test_delivery_schedules_receipt_scoped_recall_once() -> None:
    """The production delivery path binds the send receipt to one recall call."""
    event = FakeEvent()
    plugin = FakePlugin()
    bot = FakeRecallBot([{"status": "failed", "retcode": 100}])

    async def scenario():
        outcome = await deliver_action_plan(
            plugin,
            event,
            [ActionSegment("A", recall=True)],
            ledger=TurnRecallLedger(),
            send_message=plugin._send_msg,
            fire_poke=plugin._fire_poke,
            bot=bot,
        )
        tasks = list(getattr(plugin, "_recall_tasks", []))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return outcome

    outcome = asyncio.run(scenario())

    assert outcome.status == "complete"
    assert bot.calls == [1]


def _run_recall_with_logger(responses: list[Any]) -> tuple[Any, list[str]]:
    """Run one receipt-scoped recall while capturing every logger call."""
    module = importlib.import_module(
        "data.plugins.astrbot_plugin_active_function_candidate.action_sender"
    )
    event = FakeEvent()
    event.set_extra("_active_func_bridge_bound", True)
    bot = FakeRecallBot(responses)
    ledger = TurnRecallLedger()
    turn_id = ledger.turn_id(event)
    assert ledger.register_send(event, turn_id, "9101") is True
    with mock.patch.object(module, "logger") as fake_logger:
        asyncio.run(
            module._delayed_owned_recall(FakePlugin(), event, bot, ledger, turn_id, "9101")
        )
        records = []
        for call in fake_logger.method_calls:
            records.append(f"{call[0]}:{call[1]}")
    return bot, records


def test_failed_recall_envelope_is_reported_not_silently_accepted() -> None:
    """A returned failure envelope must warn, and must not claim success.

    This is the regression that the pre-fix helper missed: it awaited
    ``delete_msg`` and discarded the envelope, so a platform-side rejection was
    indistinguishable from a successful recall in the logs.
    """
    bot, records = _run_recall_with_logger([{"status": "failed", "retcode": 100}])

    assert bot.calls == [9101]
    assert any(record.startswith("warning:") for record in records)
    assert not any(record.startswith("info:") for record in records)
    assert any("'failed'" in record and "100" in record for record in records)


def test_accepted_recall_envelope_is_reported_as_success() -> None:
    bot, records = _run_recall_with_logger([{"status": "ok", "retcode": 0}])

    assert bot.calls == [9101]
    assert any(record.startswith("info:") for record in records)
    assert not any(record.startswith("warning:") for record in records)


def test_recall_transport_exception_is_reported_as_warning() -> None:
    bot, records = _run_recall_with_logger([RuntimeError("offline delete rejection")])

    assert bot.calls == [9101]
    assert any(record.startswith("warning:") for record in records)
    assert not any(record.startswith("info:") for record in records)
