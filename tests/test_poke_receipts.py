import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from data.plugins.astrbot_plugin_active_function.main import ActiveFunctionPlugin
from data.plugins.astrbot_plugin_active_function.poke_receipts import PokeReceipts


def plugin_for(path, success=True):
    plugin = object.__new__(ActiveFunctionPlugin)
    plugin._poke_receipts = PokeReceipts(path)
    plugin._poke_mgr = SimpleNamespace(send_poke=AsyncMock(return_value=success))
    plugin._record_action_diagnostic = Mock()
    plugin._session_inactive = AsyncMock(return_value=False)
    plugin._scene_disabled = Mock(return_value=False)
    plugin.prompt_enable = False
    return plugin


def event_for(scope="owner"):
    return SimpleNamespace(
        unified_msg_origin=scope, get_sender_id=lambda: "123", get_group_id=lambda: ""
    )


def test_actual_action_survives_reload_and_is_scoped(tmp_path):
    async def run():
        path = tmp_path / "receipts.json"
        plugin = plugin_for(path)
        await plugin._fire_poke(event_for(), object())
        reloaded = plugin_for(path)
        request = SimpleNamespace(system_prompt="original")
        await reloaded.inject_prompt(event_for(), request)
        assert "平台回执成功" in request.system_prompt
        assert "[poke]" not in request.system_prompt
        assert not reloaded._poke_receipts.prompt("other")
        assert request.system_prompt.startswith("original")

    asyncio.run(run())


def test_failure_is_not_success(tmp_path):
    async def run():
        plugin = plugin_for(tmp_path / "receipts.json", False)
        await plugin._fire_poke(event_for(), object())
        prompt = plugin._poke_receipts.prompt("owner")
        assert "未确认成功" in prompt
        assert "平台回执成功" not in prompt

    asyncio.run(run())


def test_tags_alone_create_no_receipt_and_disabled_scene_gets_none(tmp_path):
    async def run():
        plugin = plugin_for(tmp_path / "receipts.json")
        assert not plugin._poke_receipts.prompt("owner")
        plugin._poke_receipts.record("owner", True)
        plugin._scene_disabled.return_value = True
        request = SimpleNamespace(system_prompt="original")
        await plugin.inject_prompt(event_for(), request)
        assert request.system_prompt == "original"
        plugin._scene_disabled.return_value = False
        plugin._session_inactive.return_value = True
        await plugin.inject_prompt(event_for(), request)
        assert request.system_prompt == "original"

    asyncio.run(run())


def test_expired_corrupt_and_multiple_results(tmp_path):
    path = tmp_path / "receipts.json"
    store = PokeReceipts(path)
    store.record("owner", True, at=1)
    assert not store.prompt("owner")
    store.record("owner", True)
    store.record("owner", False)
    assert len(store.read()) == 2
    assert "平台回执成功" in store.prompt("owner")
    assert "未确认成功" in store.prompt("owner")
    path.write_text("broken")
    assert not store.prompt("owner")
