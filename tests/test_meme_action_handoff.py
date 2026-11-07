"""Meme cleanup leaves controls for the downstream action consumer."""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.message.components import Plain, Reply
from astrbot.core.provider.entities import LLMResponse
from data.plugins.meme_manager.backend.meme_parser import MemeParser
from data.plugins.astrbot_plugin_active_function.main import ActiveFunctionPlugin
from data.plugins.astrbot_plugin_active_function_candidate.action_sender import handle_target_action_decorate
from data.plugins.astrbot_plugin_active_function_candidate.tests.test_action_sender import FakeEvent, FakePlugin


def test_meme_cleanup_to_action_capture_and_quoted_dispatch():
    async def run():
        event = FakeEvent()
        response = LLMResponse(role="assistant", completion_text=MemeParser.parse(
            "hello &&happy&&$[poke]", ("happy",), alternative=True, remove_invalid=True,
        ).text)
        upstream = object.__new__(ActiveFunctionPlugin)
        upstream._disabled = AsyncMock(return_value=False)
        upstream.recall_tag = "[recall]"
        upstream.poke_tag = "[poke]"
        upstream.history_tag_mode = "strip"
        upstream._reply_mgr = FakePlugin()._reply_mgr
        upstream._record_action_diagnostic = Mock()
        upstream._patch_last_assistant_message = Mock()
        with patch("data.plugins.astrbot_plugin_active_function.main.AiocqhttpMessageEvent", FakeEvent):
            await upstream.on_llm_response(event, response)
        assert "[poke]" in event.get_extra("_active_func_original_text", "")
        assert "[poke]" not in response.completion_text
        event.result.chain = [Reply(id="123"), Plain(response.completion_text)]
        event.set_extra("_active_func_bridge_bound", True)
        plugin = FakePlugin()
        await handle_target_action_decorate(plugin, event)
        assert plugin.pokes == 1
        assert plugin.sends == []
        assert isinstance(event.result.chain[0], Reply)
        assert all("[poke]" not in c.text for c in event.result.chain if isinstance(c, Plain))
        await handle_target_action_decorate(plugin, event)
        assert plugin.pokes == 1
    asyncio.run(run())
