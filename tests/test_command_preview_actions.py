"""Keep quoted action controls inert in command results."""

import asyncio
from types import SimpleNamespace
from unittest import mock

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from data.plugins.astrbot_plugin_active_function.main import ActiveFunctionPlugin
from data.plugins.astrbot_plugin_active_function_candidate.action_sender import (
    handle_target_action_decorate,
)
from data.plugins.astrbot_plugin_active_function_candidate.active_function_candidate import (
    ActiveFunctionCandidate,
)
from data.plugins.astrbot_plugin_active_function_candidate.main import (
    ActiveFunctionCandidatePlugin,
)
from data.plugins.astrbot_plugin_active_function_candidate.tests.test_action_sender import (
    FakeEvent,
    FakePlugin,
)
from data.plugins.astrbot_plugin_llm_amnesia.main import (
    build_round_previews,
    render_round_previews,
)


def test_forget_preview_survives_all_action_entry_points():
    history = [
        {"role": "user", "content": "test question [poke]"},
        {"role": "assistant", "content": "[poke]one$[recall]two$[reply:123]three"},
    ]
    text = "preview\n" + render_round_previews(build_round_previews(history))

    async def scenario():
        for sync in (False, True):
            event = FakeEvent()
            event.result = MessageEventResult().message(text)
            event.set_extra("virtual_life_consciousness_sync", sync)
            event.set_extra("_active_func_bridge_bound", True)
            event.set_extra("enable_streaming", True)
            original_result = event.result
            plugin = FakePlugin()
            upstream = object.__new__(ActiveFunctionPlugin)
            bridge = object.__new__(ActiveFunctionCandidatePlugin)
            bridge._active_plugin = mock.Mock(
                side_effect=AssertionError("command reached action dispatch")
            )

            assert await handle_target_action_decorate(plugin, event) is False
            await ActiveFunctionCandidate().guard_control_chain(event)
            await bridge.dispatch_target_actions(event)
            assert await upstream._disabled(event) is True
            assert event.result is original_result
            assert len(event.result.chain) == 1
            assert event.result.chain[0].text == text
            assert plugin.sends == []
            assert plugin.pokes == 0
            assert event.clear_count == 0
            assert not event.get_extra("_recall_handled")

    asyncio.run(scenario())


def test_model_action_result_still_dispatches():
    async def scenario():
        event = FakeEvent("[poke]one$two")
        event.result.result_content_type = ResultContentType.LLM_RESULT
        event.set_extra("_active_func_bridge_bound", True)
        plugin = FakePlugin()
        assert await handle_target_action_decorate(plugin, event) is True
        assert plugin.pokes == 1
        assert len(plugin.sends) == 2
        assert event.clear_count == 1

        upstream = object.__new__(ActiveFunctionPlugin)
        upstream._scene_disabled = lambda _event: False
        upstream._session_inactive = mock.AsyncMock(return_value=False)
        for kind in (
            ResultContentType.LLM_RESULT,
            ResultContentType.STREAMING_RESULT,
            ResultContentType.STREAMING_FINISH,
        ):
            probe = FakeEvent()
            probe.result = SimpleNamespace(
                chain=[Plain("[poke]")], result_content_type=kind
            )
            assert await upstream._disabled(probe) is False

    asyncio.run(scenario())
