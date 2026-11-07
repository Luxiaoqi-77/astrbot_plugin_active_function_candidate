"""Installable companion candidate that connects the guards/action adapter."""

from __future__ import annotations

import re
from types import MethodType
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.provider.entities import ProviderRequest

from .action_sender import handle_target_action_decorate
from .active_function_candidate import (
    ActiveFunctionCandidate,
    EventReplayGuard,
    is_owner_private_turn,
    strip_unhandled_control_tags,
)


@register(
    "astrbot_plugin_active_function_candidate",
    "Codex",
    "Owner-scoped adaptation for the pinned active-function plugin",
    "0.1.0",
)
class ActiveFunctionCandidatePlugin(Star):
    """Bridge candidate safeguards into the existing AstrBot plugin manager."""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        self.replays = EventReplayGuard()
        self.guards = ActiveFunctionCandidate()
        self._patched_active_plugin = None
        self._original_scene_disabled = None
        self._binding_complete = False

    def _active_plugin(self):
        get_star = getattr(self.context, "get_registered_star", None)
        metadata = get_star("astrbot_plugin_active_function") if callable(get_star) else None
        return getattr(metadata, "star_cls", None)

    def _restrict_upstream_plugin(self, active: Any) -> bool:
        """Open the upstream gate only after the companion binding is complete."""
        if active is self._patched_active_plugin:
            return bool(
                self._binding_complete
                and getattr(active, "_candidate_bridge_ready", False) is True
            )
        self._restore_upstream_plugin()
        if not hasattr(active, "_candidate_bridge_ready"):
            return False
        # Close first, including when recovering from a companion reload whose
        # prior termination callback did not complete.
        active._candidate_bridge_ready = False
        required_methods = ("_send_msg", "_fire_poke", "_get_bot_from_platform")
        if any(not callable(getattr(active, name, None)) for name in required_methods):
            return False
        reply_manager = getattr(active, "_reply_mgr", None)
        if reply_manager is None or not callable(
            getattr(reply_manager, "build_reply_prompt", None)
        ):
            return False
        if not all(
            isinstance(getattr(active, name, None), bool)
            for name in ("recall_enable", "reply_enable", "poke_enable")
        ):
            return False
        get_star = getattr(getattr(active, "context", None), "get_registered_star", None)
        splitter_meta = get_star("astrbot_plugin_splitter") if callable(get_star) else None
        splitter = getattr(splitter_meta, "star_cls", None)
        if not callable(getattr(splitter, "split_text_for_active_function", None)):
            return False
        original = getattr(active, "_scene_disabled", None)
        if not callable(original):
            return False

        def owner_private_only(_active: Any, event: AstrMessageEvent) -> bool:
            if getattr(_active, "_candidate_bridge_ready", False) is not True:
                return True
            return not is_owner_private_turn(event) or bool(original(event))

        self._patched_active_plugin = active
        self._original_scene_disabled = original
        active._scene_disabled = MethodType(owner_private_only, active)
        # This is the final binding operation: upstream hooks remain disabled
        # until callbacks, feature flags, quote manager, and splitter are ready.
        active._candidate_bridge_ready = True
        self._binding_complete = True
        return True

    def _restore_upstream_plugin(self) -> None:
        active = self._patched_active_plugin
        original = self._original_scene_disabled
        if active is not None:
            # Revoke the upstream gate before restoring the patched original.
            active._candidate_bridge_ready = False
        if active is not None and callable(original):
            active._scene_disabled = original
        self._patched_active_plugin = None
        self._original_scene_disabled = None
        self._binding_complete = False

    async def terminate(self) -> None:
        self._restore_upstream_plugin()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=2147483648)
    async def mark_poke_source_and_scope(self, event: AstrMessageEvent) -> None:
        """Run before memory/activity hooks and upstream message caching."""
        active = self._active_plugin()
        if active is None:
            self._restore_upstream_plugin()
            bound = False
        else:
            bound = self._restrict_upstream_plugin(active)
        event.set_extra("_active_func_bridge_bound", bound)
        event.set_extra(
            "_active_func_reply_enabled",
            bool(bound and getattr(active, "reply_enable", False)),
        )
        await self.guards.mark_owner_poke_provenance(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=99)
    async def clear_target_reply_cache(self, event: AstrMessageEvent) -> None:
        """Erase active-function's just-cached target message after priority 100."""
        if str(getattr(event, "unified_msg_origin", "")) != self._owner_umo():
            return
        active = self._active_plugin()
        manager = getattr(active, "_reply_mgr", None)
        cache = getattr(manager, "_cache", None)
        if isinstance(cache, dict):
            cache.pop(self._owner_umo(), None)

    @staticmethod
    def _owner_umo() -> str:
        from .active_function_candidate import OWNER_UMO

        return OWNER_UMO

    @filter.on_llm_request(priority=-300)
    async def remove_stale_quote_prompt(
        self, event: AstrMessageEvent, request: ProviderRequest
    ) -> None:
        """Keep upstream actions but remove its persistent-cache quote prompt."""
        if not is_owner_private_turn(event):
            return
        active = self._active_plugin()
        if active is None or not self._binding_complete or active is not self._patched_active_plugin:
            return
        event.set_extra("_active_func_reply_enabled", getattr(active, "reply_enable", False) is True)
        try:
            override = event.get_extra("enable_streaming")
            if override is None:
                provider_settings = self.context.get_config(event.unified_msg_origin).get(
                    "provider_settings", {}
                )
                streaming = bool(provider_settings.get("streaming_response", False))
            else:
                streaming = bool(override)
        except Exception:
            # Unknown streaming state is unsafe for action tags: suppress the
            # active-function prompt and let the stream guard fail closed.
            streaming = True
        system_prompt = request.system_prompt or ""
        suffix_builder = getattr(active, "_build_system_prompt_suffix", None)
        suffix = suffix_builder(event) if callable(suffix_builder) else ""
        if streaming:
            if suffix:
                system_prompt = system_prompt.replace(suffix, "")
            event.set_extra("_active_func_actions_blocked", True)
            event.set_extra("_active_func_reply_blocked", True)
            event.set_extra("continuous_target_reply_controls", False)
            event.set_extra("continuous_quote_selected_id", None)
            event.set_extra("continuous_quote_snapshot", ())
        else:
            manager = getattr(active, "_reply_mgr", None)
            build_reply_prompt = getattr(manager, "build_reply_prompt", None)
            reply_prompt = build_reply_prompt(self._owner_umo()) if callable(build_reply_prompt) else ""
            if reply_prompt:
                system_prompt = system_prompt.replace(reply_prompt, "")
            if getattr(active, "reply_enable", True) is False:
                event.set_extra("_active_func_reply_blocked", True)
                event.set_extra("continuous_target_reply_controls", False)
                event.set_extra("continuous_quote_selected_id", None)
                event.set_extra("continuous_quote_snapshot", ())
                system_prompt = re.sub(
                    r"\[当前批次可选引用\].*?\[当前批次可选引用结束\]",
                    "",
                    system_prompt,
                    flags=re.S,
                )
        if not self._binding_complete or active is not self._patched_active_plugin:
            event.set_extra("_active_func_actions_blocked", True)
            event.set_extra("_active_func_reply_blocked", True)
            event.set_extra("continuous_target_reply_controls", False)
            event.set_extra("continuous_quote_selected_id", None)
            event.set_extra("continuous_quote_snapshot", ())
            if suffix:
                system_prompt = system_prompt.replace(suffix, "")
        request.system_prompt = system_prompt

    @filter.on_decorating_result(priority=14)
    async def guard_target_result(self, event: AstrMessageEvent) -> None:
        await self.guards.guard_control_chain(event)

    @filter.on_decorating_result(priority=13)
    async def dispatch_target_actions(self, event: AstrMessageEvent) -> None:
        if (
            getattr(event.get_result(), "result_content_type", None)
            == ResultContentType.GENERAL_RESULT
        ):
            return
        active = self._active_plugin()
        if active is not None and self._binding_complete and active is self._patched_active_plugin:
            await handle_target_action_decorate(active, event)
        else:
            strip_unhandled_control_tags(event)
