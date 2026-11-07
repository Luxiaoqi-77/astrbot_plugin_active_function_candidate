"""Owner-only active-function action planning and receipt-scoped recall.

This candidate helper is side-effect free until ``deliver_action_plan`` is
called with the upstream plugin's sender and poke callbacks. Offline tests use
stubs; this module never contacts a platform itself.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import ResultContentType
from data.plugins.xiaohui_control_mode import blocked as control_isolated

from .active_function_candidate import OWNER_SENDER_ID, OWNER_UMO, is_owner_private_turn

_NEXT_TAG = re.compile(
    r"\[?\s*(?:next|Next|NEXT)\s*(?:[:\uff1a]\s*[^\]]*\s*)?\]?",
    re.IGNORECASE,
)
_FALLBACK_REPLY = re.compile(r"\[reply:-?\d+\]", re.I)
_QQ_REPLY = re.compile(r"<qq_reply\b[^>]*>.*?</qq_reply\s*>", re.I | re.S)
_QQ_REPLY_OPEN_TAIL = re.compile(r"<qq_reply\b[^>]*>.*$", re.I | re.S)
_CURRENCY_DOLLAR = re.compile(r"\$(?=\d)|(?<=\d)\$(?=$|[.,!?;:，。！？；：)\]}）】])")


def _protect_currency_dollars(text: str) -> tuple[str, str | None]:
    """Hide currency dollars from a splitter that uses ``$`` as a delimiter."""
    if not _CURRENCY_DOLLAR.search(text):
        return text, None
    marker = f"\ue000{uuid.uuid4().hex}\ue001"
    return _CURRENCY_DOLLAR.sub(marker, text), marker


def _apply_splitter_cleanup(text: str, splitter: Any) -> str:
    """Mirror the splitter's configured post-cleaning for custom sends."""
    get_cfg = getattr(splitter, "_get_cfg", None)
    if not callable(get_cfg):
        return text
    try:
        split_mode = (
            "regex"
            if getattr(splitter, "_is_pro_mode", False)
            else get_cfg("split_mode", "regex")
        )
        if split_mode == "simple":
            items = get_cfg("clean_after_items", [])
            if isinstance(items, (list, tuple)):
                for item in items:
                    if item:
                        text = text.replace(str(item), "")
            return text
        pattern = get_cfg("clean_after_regex", "")
        return re.sub(pattern, "", text, flags=re.DOTALL) if pattern else text
    except Exception:
        # A malformed live config must not suppress the ordinary fallback path.
        return text


@dataclass(frozen=True)
class ActionSegment:
    text: str
    recall: bool = False
    poke_count: int = 0
    reply_message_id: str | None = None
    fallback_text: str | None = None


@dataclass(frozen=True)
class DeliveryOutcome:
    """At-most-once send result and the first segment eligible for fallback."""

    status: str
    confirmed_sends: int
    fallback_index: int | None = None
    recalled_ids: tuple[str, ...] = ()


@dataclass
class _EventDeliveryState:
    """Serialize duplicate decorator entry for one event result."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    status: str = "new"
    plan: list[ActionSegment] | None = None
    confirmed_sends: int = 0
    fallback_index: int | None = None
    send_in_flight: bool = False
    delivery_outcome: str | None = None


def validated_batch_quote(event: Any) -> str | None:
    """Return only a selected original ID present in this event's batch snapshot."""
    try:
        selected = str(event.get_extra("continuous_quote_selected_id") or "")
        snapshot = event.get_extra("continuous_quote_snapshot") or ()
        allowed = {str(message_id) for message_id, _ in snapshot}
        if selected and selected in allowed and re.fullmatch(r"-?\d+", selected):
            return selected
    except Exception:
        pass
    return None


def build_action_plan(
    event: Any,
    text: str,
    *,
    splitter: Any,
    recall_tag: str = "[recall]",
    poke_tag: str = "[poke]",
    reply_tag_pattern: re.Pattern | None = None,
    strip_reply_variants: Callable[[str], str] | None = None,
    allow_recall: bool = True,
    allow_poke: bool = True,
    allow_reply: bool = True,
) -> list[ActionSegment] | None:
    """Parse current-turn controls using the splitter's own configured chunks.

    A missing or incompatible splitter fails closed (``None``), so the caller
    can strip controls and leave the ordinary response on AstrBot's chain.
    ``[reply:id]`` IDs are discarded; native quote authority comes only from
    the validated current-batch selection.
    """
    if not is_owner_private_turn(event):
        return None
    split = getattr(splitter, "split_text_for_active_function", None)
    if not callable(split):
        return None
    split_text, currency_marker = _protect_currency_dollars(text)
    try:
        chunks = split(split_text, event)
    except Exception:
        return None
    if not isinstance(chunks, list) or not all(isinstance(chunk, str) for chunk in chunks):
        return None

    quote_id = validated_batch_quote(event) if allow_reply else None
    reply_pattern = reply_tag_pattern or _FALLBACK_REPLY
    result: list[ActionSegment] = []
    quote_pending = quote_id is not None
    for raw in chunks:
        recall = bool(allow_recall and recall_tag and recall_tag in raw)
        poke_count = raw.count(poke_tag) if allow_poke and poke_tag else 0
        clean = raw.replace(recall_tag, "") if recall_tag else raw
        clean = clean.replace(poke_tag, "") if poke_tag else clean
        clean = reply_pattern.sub("", clean)
        clean = _QQ_REPLY.sub("", clean)
        clean = _QQ_REPLY_OPEN_TAIL.sub("", clean)
        clean = re.sub(r"</?qq_reply\b[^>]*>?", "", clean, flags=re.I)
        if strip_reply_variants:
            clean = strip_reply_variants(clean)
        clean = _NEXT_TAG.sub("", clean)
        clean = _apply_splitter_cleanup(clean, splitter)
        if currency_marker:
            clean = clean.replace(currency_marker, "$")
        fallback_text = clean
        clean = clean.strip()
        reply_id = quote_id if clean and quote_pending else None
        if reply_id:
            quote_pending = False
        if clean or poke_count:
            result.append(
                ActionSegment(
                    text=clean,
                    recall=recall and bool(clean),
                    poke_count=poke_count,
                    reply_message_id=reply_id,
                    fallback_text=fallback_text,
                )
            )
    return result


class TurnRecallLedger:
    """Authorize deletion only for a bot send receipt in its originating turn."""

    def __init__(self, ttl_seconds: float = 300.0, max_turns: int = 256):
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_turns = max(1, int(max_turns))
        self._turns: dict[str, dict[str, Any]] = {}

    @staticmethod
    def turn_id(event: Any) -> str:
        existing = event.get_extra("_active_func_turn_id")
        if existing:
            return str(existing)
        created = uuid.uuid4().hex
        event.set_extra("_active_func_turn_id", created)
        return created

    def register_send(self, event: Any, turn_id: str, message_id: Any) -> bool:
        if not is_owner_private_turn(event):
            return False
        value = str(message_id or "")
        if not re.fullmatch(r"\d+", value):
            return False
        now = time.monotonic()
        self._prune(now)
        record = self._turns.get(str(turn_id))
        identity = self._identity(event)
        if record and record["identity"] != identity:
            return False
        if not record:
            record = {
                "identity": identity,
                "ids": set(),
                "expires_at": now + self.ttl_seconds,
            }
            self._turns[str(turn_id)] = record
        record["ids"].add(value)
        while len(self._turns) > self.max_turns:
            oldest = next(iter(self._turns))
            self._turns.pop(oldest, None)
        return True

    def claim_recall(self, event: Any, turn_id: str, message_id: Any) -> bool:
        if not is_owner_private_turn(event):
            return False
        now = time.monotonic()
        self._prune(now)
        record = self._turns.get(str(turn_id))
        value = str(message_id or "")
        if (
            not record
            or record["identity"] != self._identity(event)
            or value not in record["ids"]
        ):
            return False
        record["ids"].remove(value)
        if not record["ids"]:
            self._turns.pop(str(turn_id), None)
        return True

    @staticmethod
    def _identity(event: Any) -> tuple[str, str, str]:
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        return OWNER_UMO, OWNER_SENDER_ID, self_id

    def _prune(self, now: float) -> None:
        expired = [
            key for key, value in self._turns.items()
            if value["expires_at"] <= now
        ]
        for key in expired:
            self._turns.pop(key, None)


async def deliver_action_plan(
    plugin: Any,
    event: Any,
    plan: list[ActionSegment],
    *,
    ledger: TurnRecallLedger,
    send_message: Callable[[Any, Any, list[dict]], Awaitable[Any]],
    fire_poke: Callable[[Any, Any], Awaitable[Any]],
    bot: Any,
    delivery_state: _EventDeliveryState | None = None,
) -> DeliveryOutcome:
    """Execute once; never clear the result unless every segment was handled.

    A normal callback exception means rejected before delivery. Timeout and
    cancellation are uncertain, so that attempted segment is never retried.
    The caller can fall back to ``fallback_index`` without replaying confirmed
    or possibly delivered segments.
    """
    if not is_owner_private_turn(event):
        return DeliveryOutcome("rejected", 0)
    turn_id = ledger.turn_id(event)
    if delivery_state is not None:
        delivery_state.plan = plan
        delivery_state.confirmed_sends = 0
        delivery_state.fallback_index = 0
        delivery_state.send_in_flight = False
    recalled_ids: list[str] = []
    sent_count = 0
    for index, segment in enumerate(plan):
        if delivery_state is not None:
            delivery_state.fallback_index = index
            delivery_state.send_in_flight = False
        send_result = None
        if segment.text:
            if sent_count and float(getattr(plugin, "segment_interval", 0) or 0) > 0:
                try:
                    await asyncio.sleep(float(plugin.segment_interval))
                except asyncio.CancelledError:
                    if delivery_state is not None:
                        delivery_state.delivery_outcome = "failed"
                    return DeliveryOutcome("failed", sent_count, index, tuple(recalled_ids))
                except Exception:
                    if delivery_state is not None:
                        delivery_state.delivery_outcome = "failed"
                    return DeliveryOutcome("failed", sent_count, index, tuple(recalled_ids))
            payload = []
            if segment.reply_message_id:
                payload.append({"type": "reply", "data": {"id": segment.reply_message_id}})
            payload.append({"type": "text", "data": {"text": segment.text}})
            if delivery_state is not None:
                delivery_state.send_in_flight = True
            try:
                send_result = await send_message(event, bot, payload)
            except asyncio.CancelledError:
                if delivery_state is not None:
                    delivery_state.send_in_flight = False
                    delivery_state.fallback_index = index + 1
                    delivery_state.delivery_outcome = "uncertain"
                if segment.reply_message_id:
                    event.set_extra("continuous_quote_consumed", True)
                return DeliveryOutcome("uncertain", sent_count, index + 1, tuple(recalled_ids))
            except TimeoutError:
                if delivery_state is not None:
                    delivery_state.send_in_flight = False
                    delivery_state.fallback_index = index + 1
                    delivery_state.delivery_outcome = "uncertain"
                if segment.reply_message_id:
                    event.set_extra("continuous_quote_consumed", True)
                return DeliveryOutcome("uncertain", sent_count, index + 1, tuple(recalled_ids))
            except Exception:
                if delivery_state is not None:
                    delivery_state.send_in_flight = False
                    delivery_state.fallback_index = index
                    delivery_state.delivery_outcome = "failed"
                return DeliveryOutcome("failed", sent_count, index, tuple(recalled_ids))
            message_id = (
                str(send_result.get("message_id"))
                if isinstance(send_result, dict) and send_result.get("message_id")
                else ""
            )
            if not message_id:
                # The pinned sender contract returns a message ID. A normal
                # return without one is not proof that the platform rejected
                # the send, so treat it like an ambiguous acknowledgement.
                if delivery_state is not None:
                    delivery_state.send_in_flight = False
                    delivery_state.fallback_index = index + 1
                    delivery_state.delivery_outcome = "uncertain"
                if segment.reply_message_id:
                    event.set_extra("continuous_quote_consumed", True)
                return DeliveryOutcome("uncertain", sent_count, index + 1, tuple(recalled_ids))
            if delivery_state is not None:
                delivery_state.send_in_flight = False
            sent_count += 1
            if delivery_state is not None:
                delivery_state.confirmed_sends = sent_count
                delivery_state.fallback_index = index + 1
            if segment.reply_message_id:
                event.set_extra("continuous_quote_consumed", True)
            if message_id and segment.recall and ledger.register_send(event, turn_id, message_id):
                recalled_ids.append(message_id)
                try:
                    task = asyncio.create_task(
                        _delayed_owned_recall(
                            plugin, event, bot, ledger, turn_id, message_id
                        )
                    )
                    tasks = getattr(plugin, "_recall_tasks", None)
                    if isinstance(tasks, list):
                        tasks.append(task)
                        remove = getattr(plugin, "_remove_task", None)
                        if callable(remove):
                            task.add_done_callback(remove)
                except Exception:
                    # A recall scheduling error must not suppress the dialogue.
                    pass
        for _ in range(segment.poke_count):
            try:
                await fire_poke(event, bot)
            except asyncio.CancelledError:
                # Preserve dialogue if shutdown/cancellation interrupts a poke.
                continue
            except Exception:
                # Pokes are at-most-once best-effort actions. Their failure is
                # isolated so subsequent generated dialogue is still sent.
                continue
    event.set_extra("_active_func_target_action_handled", True)
    event.set_extra("_recall_handled", True)
    event.clear_result()
    event._has_send_oper = True
    if delivery_state is not None:
        delivery_state.delivery_outcome = "complete"
    return DeliveryOutcome("complete", sent_count, None, tuple(recalled_ids))


def _clean_target_controls(result: Any, text: str, plugin: Any, event: Any) -> None:
    """Strip controls in-place while preserving all non-text chain components."""
    recall_tag = str(getattr(plugin, "recall_tag", "[recall]") or "")
    poke_tag = str(getattr(plugin, "poke_tag", "[poke]") or "")
    reply_manager = getattr(plugin, "_reply_mgr", None)

    def clean(value: str) -> str:
        for tag in (recall_tag, poke_tag):
            if tag:
                value = value.replace(tag, "")
        value = _FALLBACK_REPLY.sub("", value)
        if callable(getattr(reply_manager, "strip_all_reply_variants", None)):
            value = reply_manager.strip_all_reply_variants(value)
        value = _QQ_REPLY.sub("", value)
        value = _QQ_REPLY_OPEN_TAIL.sub("", value)
        value = re.sub(r"</?qq_reply\b[^>]*>?", "", value, flags=re.I)
        return _NEXT_TAG.sub("", value)

    chain = getattr(result, "chain", []) or []
    result.chain = [
        Plain(clean(component.text)) if isinstance(component, Plain) else component
        for component in chain
    ]
    event.set_extra("_active_func_original_text", clean(text))
    event.set_extra("_active_func_actions_blocked", True)
    event.set_extra("_active_func_target_action_handled", True)
    event.set_extra("_recall_handled", True)


def _install_fallback_tail(
    event: Any, result: Any, plan: list[ActionSegment], start: int
) -> None:
    """Expose only unattempted dialogue to AstrBot's ordinary send path."""
    remaining = "".join(
        segment.fallback_text if segment.fallback_text is not None else segment.text
        for segment in plan[start:]
    )
    result.chain = [Plain(remaining)] if remaining else []
    event.set_extra("_active_func_original_text", remaining)
    event.set_extra("_active_func_actions_blocked", True)
    event.set_extra("_active_func_target_action_handled", True)
    event.set_extra("_recall_handled", True)


async def _handle_target_action_decorate_once(plugin: Any, event: Any) -> bool:
    """Upstream `on_decorating_result` bridge for the target owner DM.

    Call this from a priority-13 hook, after the candidate guard (14) and
    before upstream action handlers (12/11/10). It returns True when it took
    over delivery. Missing splitter support and streaming both fail closed.
    """
    if not is_owner_private_turn(event):
        return False
    result = event.get_result()
    if result is None:
        return False
    if getattr(result, "result_content_type", None) == ResultContentType.GENERAL_RESULT:
        return False
    if (event.get_extra("virtual_life_consciousness_sync") is True or control_isolated(event.unified_msg_origin)):
        return False
    if getattr(result, "async_stream", None) is not None:
        return False

    original = event.get_extra("_active_func_original_text")
    text = original if isinstance(original, str) and original else "".join(
        component.text
        for component in getattr(result, "chain", ())
        if isinstance(component, Plain)
    )
    recall_tag = str(getattr(plugin, "recall_tag", "[recall]") or "")
    poke_tag = str(getattr(plugin, "poke_tag", "[poke]") or "")
    reply_manager = getattr(plugin, "_reply_mgr", None)
    has_action = bool(
        (recall_tag and recall_tag in text) or (poke_tag and poke_tag in text)
    )
    if not has_action:
        has_reply_control = bool(
            _FALLBACK_REPLY.search(text)
            or _QQ_REPLY.search(text)
            or _QQ_REPLY_OPEN_TAIL.search(text)
            or (
                callable(getattr(reply_manager, "strip_all_reply_variants", None))
                and reply_manager.strip_all_reply_variants(text) != text
            )
        )
        if has_reply_control:
            _clean_target_controls(result, text, plugin, event)
        return False
    if (
        getattr(plugin, "_candidate_bridge_ready", False) is not True
        or event.get_extra("_active_func_bridge_bound") is not True
    ):
        _clean_target_controls(result, text, plugin, event)
        return False
    if event.get_extra("_active_func_actions_blocked") is True:
        _clean_target_controls(result, text, plugin, event)
        return False

    # Keep mixed text/media replies on the ordinary chain so images, records,
    # video and files preserve their existing order and transport behavior.
    if any(not isinstance(component, Plain) for component in getattr(result, "chain", ())):
        if poke_tag and poke_tag in text and getattr(plugin, "poke_enable", False) is True:
            bot = getattr(event, "bot", None)
            if bot is None:
                getter = getattr(plugin, "_get_bot_from_platform", None)
                bot = getter(event) if callable(getter) else None
            if bot is not None:
                diagnostic = getattr(plugin, "_record_action_diagnostic", None)
                if callable(diagnostic):
                    diagnostic(
                        event,
                        stage="poke_parse",
                        reason="mixed_chain_standalone",
                        tag_count=text.count(poke_tag),
                    )
                try:
                    await plugin._fire_poke(event, bot)
                except (asyncio.CancelledError, Exception):
                    logger.debug("[ActiveFunction] Mixed-chain poke did not complete")
        _clean_target_controls(result, text, plugin, event)
        return False

    get_star = getattr(getattr(plugin, "context", None), "get_registered_star", None)
    metadata = get_star("astrbot_plugin_splitter") if callable(get_star) else None
    splitter = getattr(metadata, "star_cls", None)
    reply_manager = getattr(plugin, "_reply_mgr", None)
    plan = build_action_plan(
        event,
        text,
        splitter=splitter,
        recall_tag=recall_tag,
        poke_tag=poke_tag,
        reply_tag_pattern=getattr(reply_manager, "_reply_tag_pattern", None),
        strip_reply_variants=getattr(reply_manager, "strip_all_reply_variants", None),
        allow_recall=getattr(plugin, "recall_enable", True) is True,
        allow_poke=getattr(plugin, "poke_enable", True) is True,
        allow_reply=getattr(plugin, "reply_enable", True) is True,
    )
    if plan is None:
        _clean_target_controls(result, text, plugin, event)
        return False
    if not plan:
        _clean_target_controls(result, text, plugin, event)
        return False

    bot = getattr(event, "bot", None)
    if bot is None:
        getter = getattr(plugin, "_get_bot_from_platform", None)
        bot = getter(event) if callable(getter) else None
    if bot is None:
        # Without the target bot instance, strip controls and keep the normal
        # framework result path; don't clear a result that was not sent.
        _clean_target_controls(result, text, plugin, event)
        return False

    ledger = getattr(plugin, "_active_func_turn_recall_ledger", None)
    if not isinstance(ledger, TurnRecallLedger):
        ledger = TurnRecallLedger(
            ttl_seconds=max(300.0, float(getattr(plugin, "recall_delay", 0) or 0) + 60.0)
        )
        plugin._active_func_turn_recall_ledger = ledger
    outcome = await deliver_action_plan(
        plugin,
        event,
        plan,
        ledger=ledger,
        send_message=plugin._send_msg,
        fire_poke=plugin._fire_poke,
        bot=bot,
        delivery_state=event.get_extra("_active_func_action_delivery_state"),
    )
    event.set_extra("_active_func_delivery_outcome", outcome.status)
    if outcome.status == "complete":
        return True
    if outcome.status == "failed" and outcome.confirmed_sends == 0:
        # A definite first-send rejection means nothing was delivered. Keep the
        # original chain and let AstrBot send its fully cleaned text normally.
        _clean_target_controls(result, text, plugin, event)
        return False
    if outcome.fallback_index is not None:
        _install_fallback_tail(event, result, plan, outcome.fallback_index)
        return False
    _clean_target_controls(result, text, plugin, event)
    return False


async def handle_target_action_decorate(plugin: Any, event: Any) -> bool:
    """Serialize delivery per event so duplicate hooks cannot repeat actions."""
    if not is_owner_private_turn(event):
        return False
    state = event.get_extra("_active_func_action_delivery_state")
    if not isinstance(state, _EventDeliveryState):
        state = _EventDeliveryState()
        event.set_extra("_active_func_action_delivery_state", state)
    async with state.lock:
        if state.status != "new":
            return state.status == "complete"
        state.status = "running"
        try:
            completed = await _handle_target_action_decorate_once(plugin, event)
        except asyncio.CancelledError:
            _recover_interrupted_delivery(plugin, event, state)
            completed = False
        except Exception:
            _recover_interrupted_delivery(plugin, event, state)
            completed = False
        state.status = "complete" if completed else "fallback"
        return completed


def _recover_interrupted_delivery(
    plugin: Any, event: Any, state: _EventDeliveryState
) -> None:
    """Leave a safe framework fallback after cancellation or unexpected errors."""
    result = event.get_result()
    if result is None:
        return
    original = event.get_extra("_active_func_original_text")
    text = original if isinstance(original, str) else "".join(
        component.text for component in getattr(result, "chain", ())
        if isinstance(component, Plain)
    )
    if state.plan is None or (
        state.confirmed_sends == 0
        and not state.send_in_flight
        and state.delivery_outcome != "uncertain"
    ):
        event.set_extra("_active_func_delivery_outcome", "failed")
        _clean_target_controls(result, text, plugin, event)
        return
    index = state.fallback_index if state.fallback_index is not None else len(state.plan)
    if state.send_in_flight:
        state.delivery_outcome = "uncertain"
        event.set_extra("_active_func_delivery_outcome", "uncertain")
        if index < len(state.plan) and state.plan[index].reply_message_id:
            event.set_extra("continuous_quote_consumed", True)
        index += 1
    elif state.delivery_outcome == "uncertain":
        event.set_extra("_active_func_delivery_outcome", "uncertain")
    else:
        state.delivery_outcome = "failed"
        event.set_extra("_active_func_delivery_outcome", "failed")
    _install_fallback_tail(event, result, state.plan, index)


def recall_response_accepted(response: Any) -> bool:
    """Return whether a OneBot ``delete_msg`` response is consistent with success.

    OneBot can return a failed envelope without raising, so only an explicit
    ``status=ok/success`` and a zero ``retcode`` are trusted. Data-only return
    values (or a missing envelope) stay accepted, matching the upstream
    ActiveFunction recall check so both paths behave identically.

    Args:
        response: Raw return value of the platform ``delete_msg`` call.

    Returns:
        ``True`` when the response does not contradict success.
    """
    if not isinstance(response, dict):
        return True
    status = response.get("status")
    if status is not None and str(status).strip().lower() not in {"ok", "success"}:
        return False
    retcode = response.get("retcode")
    if retcode is not None and str(retcode).strip() not in {"0", "0.0"}:
        return False
    return True


async def _delayed_owned_recall(
    plugin: Any,
    event: Any,
    bot: Any,
    ledger: TurnRecallLedger,
    turn_id: str,
    message_id: str,
) -> None:
    delay = max(0.0, float(getattr(plugin, "recall_delay", 0) or 0))
    if delay:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
    if not ledger.claim_recall(event, turn_id, message_id):
        return
    delete = getattr(bot, "delete_msg", None)
    if not callable(delete):
        logger.warning(
            "[ActiveFunction] Recall unavailable: delete_msg missing "
            "message_id=%s",
            message_id,
        )
        return
    try:
        response = await delete(message_id=int(message_id))
    except asyncio.CancelledError:
        # The ledger claim was consumed; never retry an uncertain recall.
        return
    except Exception:
        # A failed recall must not affect the already delivered dialogue.
        logger.warning(
            "[ActiveFunction] Recall transport failed message_id=%s", message_id
        )
        return
    # A returned failure envelope must not be reported as a successful recall.
    # The claim is already consumed, so this only records the real outcome and
    # never retries.
    if not recall_response_accepted(response):
        detail = response if isinstance(response, dict) else {}
        logger.warning(
            "[ActiveFunction] Recall rejected by platform message_id=%s "
            "status=%r retcode=%r",
            message_id,
            detail.get("status"),
            detail.get("retcode"),
        )
        return
    logger.info("[ActiveFunction] Recalled message: %s", message_id)
