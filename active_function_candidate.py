"""Offline-safe integration guards for the active-function plugin candidate.

This module is an adapter, not a copy of the upstream plugin. It deliberately
does not send messages or call the OneBot API.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Plain, Reply
from astrbot.core.message.message_event_result import MessageChain, ResultContentType
from data.plugins.xiaohui_control_mode import blocked as control_isolated

OWNER_UMO = "qq_napcat_100000002:FriendMessage:100000001"
OWNER_SENDER_ID = "100000001"
_ACTIVE_REPLY_TAG = re.compile(r"\[reply:\d+\]", re.IGNORECASE)
_CONTROL_TAG = re.compile(r"\[\s*(?:reply\s*:[^\]]*|recall|poke)\s*\]", re.I)
_CONTROL_HEAD = re.compile(r"^\[\s*(?:reply|recall|poke)\b", re.I)
_QQ_REPLY_PREFIX = "<qq_reply"
_QQ_REPLY = re.compile(r"<qq_reply\b[^>]*>.*?</qq_reply\s*>", re.I | re.S)
_QQ_REPLY_CLOSE = re.compile(r"</qq_reply\s*>", re.I)


def _feed_control_filter(text: str, carry: str = "", *, final: bool = False) -> tuple[str, str]:
    """Drop active-function controls incrementally without exposing split tags."""
    value = carry + text
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "<":
            tail = value[index:].lower()
            if not final and _QQ_REPLY_PREFIX.startswith(tail):
                return "".join(output), value[index:]
            if tail.startswith(_QQ_REPLY_PREFIX):
                match = _QQ_REPLY.search(value, index)
                if match:
                    index = match.end()
                    continue
                if not final and len(value) - index <= 4096:
                    return "".join(output), value[index:]
                if final:
                    break
            close = _QQ_REPLY_CLOSE.match(value, index)
            if close:
                index = close.end()
                continue
        if value[index] != "[":
            output.append(value[index])
            index += 1
            continue
        end = value.find("]", index + 1)
        if end < 0:
            if not final and len(value) - index <= 96:
                return "".join(output), value[index:]
            if final:
                break
            output.append(value[index])
            index += 1
            continue
        candidate = value[index : end + 1]
        if _CONTROL_TAG.fullmatch(candidate) or _CONTROL_HEAD.match(candidate):
            index = end + 1
            continue
        output.append(value[index])
        index += 1
    return "".join(output), ""


async def _sanitize_control_stream(stream):
    """Filter controls from actual streamed chunks before platform delivery."""
    carry = ""
    async for chain in stream:
        filtered = MessageChain()
        for component in getattr(chain, "chain", ()):
            if isinstance(component, Plain):
                safe, carry = _feed_control_filter(component.text, carry)
                if safe:
                    filtered.message(safe)
            else:
                filtered.chain.append(component)
        if filtered.chain:
            yield filtered
    safe, carry = _feed_control_filter("", carry, final=True)
    if safe:
        yield MessageChain().message(safe)


def strip_unhandled_control_tags(event: Any) -> None:
    """Fail-closed visible cleanup when upstream action handlers are absent."""
    if not is_owner_private_turn(event):
        return
    result = event.get_result()
    if result is None:
        return
    for index, component in enumerate(getattr(result, "chain", ())):
        if not isinstance(component, Plain):
            continue
        cleaned = _CONTROL_TAG.sub("", component.text)
        cleaned = _ACTIVE_REPLY_TAG.sub("", cleaned)
        cleaned = _QQ_REPLY.sub("", cleaned)
        cleaned = re.sub(r"</?qq_reply\b[^>]*>?", "", cleaned, flags=re.I)
        result.chain[index] = Plain(cleaned)
    original = event.get_extra("_active_func_original_text")
    if isinstance(original, str):
        cleaned = _CONTROL_TAG.sub("", original)
        cleaned = _ACTIVE_REPLY_TAG.sub("", cleaned)
        cleaned = _QQ_REPLY.sub("", cleaned)
        cleaned = re.sub(r"</?qq_reply\b[^>]*>?", "", cleaned, flags=re.I)
        event.set_extra("_active_func_original_text", cleaned)
    event.set_extra("_active_func_actions_blocked", True)
    event.set_extra("_recall_handled", True)


def is_owner_private_turn(event: Any) -> bool:
    """Require the one owner DM for any candidate action."""
    try:
        return (
            str(event.unified_msg_origin) == OWNER_UMO
            and str(event.get_sender_id()) == OWNER_SENDER_ID
            and not event.get_group_id()
        )
    except Exception:
        return False


def active_reply_is_delegated(event: Any) -> bool:
    """Use the existing batch-local native quote path when it is available."""
    try:
        return bool(
            is_owner_private_turn(event)
            and event.get_extra("continuous_target_reply_controls")
            and event.get_extra("continuous_quote_snapshot")
            and not event.get_extra("enable_streaming", False)
        )
    except Exception:
        return False


class EventReplayGuard:
    """Deduplicate transport retries by event ID without imposing a rate cap."""

    def __init__(self, ttl_seconds: float = 600.0, max_entries: int = 512):
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    def admit(self, session: str, event_id: str | int | None, now: float | None = None) -> bool:
        """Reject a replay of one transport ID; distinct IDs always remain eligible."""
        if event_id is None or not str(event_id):
            return True
        current = time.monotonic() if now is None else float(now)
        key = (str(session), str(event_id))
        while self._seen:
            oldest, expiry = next(iter(self._seen.items()))
            if expiry > current:
                break
            self._seen.pop(oldest, None)
        expiry = self._seen.get(key)
        if expiry is not None and expiry > current:
            return False
        self._seen.pop(key, None)
        self._seen[key] = current + self.ttl_seconds
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        return True


class ForgetInvalidation:
    """Track only session cache invalidation after a confirmed forget commit."""

    def __init__(self):
        self.generation: dict[str, int] = {}
        self.cached_message_ids: dict[str, set[str]] = {}

    def remember(self, session: str, message_ids: set[str]) -> None:
        """Record the source IDs currently eligible for candidate lookup."""
        self.cached_message_ids[str(session)] = {str(message_id) for message_id in message_ids}

    def on_forget_committed(self, session: str, committed: bool) -> int:
        """Advance the session generation only after its history write succeeds."""
        key = str(session)
        if committed:
            self.generation[key] = self.generation.get(key, 0) + 1
            self.cached_message_ids.pop(key, None)
        return self.generation.get(key, 0)


class ActiveFunctionCandidate:
    """Guard active-function controls while retaining framework result ownership."""

    def __init__(self, action_controls_enabled: bool = True):
        self.action_controls_enabled = bool(action_controls_enabled)
        self.replays = EventReplayGuard()
        self.forget_invalidation = ForgetInvalidation()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=2147483648)
    async def mark_owner_poke_provenance(self, event: AstrMessageEvent) -> None:
        """Mark and replay-filter real owner poke notices before memory hooks."""
        if not is_owner_private_turn(event):
            return
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if not (
            raw.get("post_type") == "notice"
            and raw.get("notice_type") == "notify"
            and raw.get("sub_type") == "poke"
            and str(raw.get("user_id", "")) == OWNER_SENDER_ID
            and str(raw.get("target_id", "")) == str(event.get_self_id() or "")
        ):
            return
        event_id = raw.get("message_id") or getattr(message_obj, "message_id", None)
        if not self.replays.admit(OWNER_UMO, event_id):
            event.stop_event()
            return
        event.set_extra("_active_function_synthetic_poke", True)

    @filter.on_decorating_result(priority=14)
    async def guard_control_chain(self, event: AstrMessageEvent) -> None:
        """Delegate current-batch quote controls and strip unsafe stream tags.

        This hook never sends, splits, recalls, or pokes. It preserves the
        current result chain so later framework/quote hooks can run once.
        """
        if not is_owner_private_turn(event):
            return
        result = event.get_result()
        if result is None:
            return
        if getattr(result, "result_content_type", None) == ResultContentType.GENERAL_RESULT:
            return

        stream = getattr(result, "async_stream", None)
        streaming = stream is not None or bool(
            event.get_extra("enable_streaming", False)
            or event.get_extra("streaming_response", False)
        )
        if active_reply_is_delegated(event):
            event.set_extra("_active_func_skip_reply_cache", True)

        if streaming:
            event.set_extra("_active_func_actions_blocked", True)
            event.set_extra("_active_func_reply_blocked", True)
            event.set_extra("continuous_target_reply_controls", False)
            event.set_extra("continuous_quote_selected_id", None)
            event.set_extra("continuous_quote_snapshot", ())
            event.set_extra("_recall_handled", True)
            if stream is not None:
                result.async_stream = _sanitize_control_stream(stream)
            strip_unhandled_control_tags(event)
            result.chain = [component for component in result.chain if not isinstance(component, Reply)]

        if (event.get_extra("virtual_life_consciousness_sync", False) is True or control_isolated(event.unified_msg_origin)):
            event.set_extra("_active_func_actions_blocked", True)
            event.set_extra("_active_func_reply_blocked", True)
            event.set_extra("continuous_target_reply_controls", False)
            event.set_extra("continuous_quote_selected_id", None)
            event.set_extra("continuous_quote_snapshot", ())
            event.set_extra("_recall_handled", True)
            strip_unhandled_control_tags(event)
            result.chain = [component for component in result.chain if not isinstance(component, Reply)]
            return

        if event.get_extra("_active_func_reply_blocked") is True:
            result.chain = [component for component in result.chain if not isinstance(component, Reply)]
            event.set_extra("continuous_quote_selected_id", None)
            event.set_extra("continuous_quote_snapshot", ())

        # In the owner DM the current-batch selector is the only quote authority.
        # Remove all upstream cache IDs from both outgoing text and the source
        # string lower-priority active-function handlers may inspect.
        event.set_extra("_active_func_skip_reply_cache", True)
        for index, component in enumerate(result.chain):
            if isinstance(component, Plain) and _ACTIVE_REPLY_TAG.search(component.text):
                result.chain[index] = Plain(_ACTIVE_REPLY_TAG.sub("", component.text))
        original = event.get_extra("_active_func_original_text")
        if isinstance(original, str) and _ACTIVE_REPLY_TAG.search(original):
            event.set_extra("_active_func_original_text", _ACTIVE_REPLY_TAG.sub("", original))

