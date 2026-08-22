# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time

from astrbot_plugin_private_companion.main import PrivateCompanionExtensionAPI, PrivateCompanionPlugin
from astrbot_plugin_private_companion.reality_companion_bridge import RealityCompanionBridgeMixin


class _Host(RealityCompanionBridgeMixin):
    admin_user_ids = ["admin-1"]

    def __init__(self) -> None:
        self.data = {
            "users": {
                "target-1": {"user_id": "target-1", "nickname": "Primary"},
                "owner-1": {"user_id": "owner-1", "relationship_role": "owner"},
            }
        }

    @staticmethod
    def _configured_target_ids() -> list[str]:
        return ["target-1"]

    @staticmethod
    def _relationship_owner_user_ids() -> set[str]:
        return {"owner-1"}

    @staticmethod
    def _is_configured_admin_user_id(value: str) -> bool:
        return value == "admin-1"


def test_reality_extension_api_recognizes_configured_primary_targets() -> None:
    api = PrivateCompanionExtensionAPI(_Host())

    assert api.get_reality_touch_authorized_user_ids() == ["admin-1", "owner-1", "target-1"]
    target = api.get_reality_touch_host_context("target-1")

    assert target["is_primary_user"] is True
    assert target["eligible"] is True


def test_reality_extension_api_forwards_mobile_location_updates() -> None:
    class Host(_Host):
        async def _handle_mobile_location_update(self, user_id: str) -> dict:
            return {"handled": user_id == "target-1"}

    api = PrivateCompanionExtensionAPI(Host())

    result = asyncio.run(api.notify_mobile_location_update("target-1"))

    assert result == {"handled": True}


def test_reality_provider_api_keeps_callbacks_bounded_and_isolated() -> None:
    host = _Host()

    async def run_scene(payload: dict) -> dict:
        return {"ok": True, "scene_id": payload.get("scene_id")}

    api = PrivateCompanionExtensionAPI(host)
    assert api.register_reality_touch_provider({
        "name": "mihome",
        "label": "米家",
        "run_scene": run_scene,
        "owner": "astrbot_plugin_mihome",
    }) is True
    assert api.list_reality_touch_providers() == [{
        "name": "mihome",
        "label": "米家",
        "capabilities": ["run_scene"],
        "owner": "astrbot_plugin_mihome",
    }]
    result = asyncio.run(api.call_reality_touch_provider("mihome", "run_scene", {"scene_id": "s1", "confirmed": True}))
    assert result == {"ok": True, "scene_id": "s1"}
    assert api.unregister_reality_touch_provider("mihome") is True


def test_reality_provider_api_rejects_unbounded_registration() -> None:
    api = PrivateCompanionExtensionAPI(_Host())
    assert api.register_reality_touch_provider({"name": "bad/name", "run_scene": lambda _: {}}) is False
    assert asyncio.run(api.call_reality_touch_provider("missing", "run_scene", {}))["ok"] is False


def test_model_planner_can_select_only_catalogued_scene() -> None:
    class Host(_Host):
        async def _llm_call(self, _prompt: str, **_kwargs) -> str:
            return '{"provider":"mihome","operation":"run_scene","scene_name":"晚安"}'

    async def list_scenes(_payload: dict) -> dict:
        return {"ok": True, "scenes": [{"scene_id": "scene-1", "scene_name": "晚安"}]}

    async def run_scene(payload: dict) -> dict:
        return {"ok": True, "scene_name": payload.get("scene_name")}

    host = Host()
    api = PrivateCompanionExtensionAPI(host)
    api.register_reality_touch_provider({"name": "mihome", "list_scenes": list_scenes, "run_scene": run_scene})

    result = asyncio.run(api.resolve_reality_touch_request("target-1", "帮我执行晚安场景"))

    assert result["ok"] is True
    assert result["operation"] == "run_scene"
    assert result["scene_name"] == "晚安"


def test_model_planner_does_not_operate_for_greeting() -> None:
    class Host(_Host):
        async def _llm_call(self, _prompt: str, **_kwargs) -> str:
            return '{"provider":"mihome","operation":"none","reason":"普通问候"}'

    host = Host()
    api = PrivateCompanionExtensionAPI(host)
    api.register_reality_touch_provider({"name": "mihome", "list_scenes": lambda _: {"ok": True, "scenes": []}})

    result = asyncio.run(api.resolve_reality_touch_request("target-1", "晚安"))

    assert result == {"ok": True, "handled": False, "reason": "普通问候"}


class _RecordingHost(RealityCompanionBridgeMixin):
    def __init__(self) -> None:
        self.data = {"users": {"u": {"user_id": "u"}}}
        self._data_lock = asyncio.Lock()
        self.saved = 0

    def _get_user(self, user_id: str) -> dict:
        return self.data["users"][user_id]

    def _save_data_sync(self) -> None:
        self.saved += 1


class _RealityApi:
    def __init__(self) -> None:
        self.output: dict = {}

    async def record_reality_touch_output(self, _user_id: str, text: str, **kwargs) -> dict:
        self.output = {
            "text": text,
            "source": kwargs.get("source", "reality_touch_audio"),
            "delivered_at": kwargs.get("delivered_at"),
        }
        return {"recorded": True}

    def recent_output(self, _user_id: str) -> dict:
        return dict(self.output)


def test_core_extension_does_not_record_reality_output_without_split_plugin() -> None:
    host = _RecordingHost()
    api = PrivateCompanionExtensionAPI(host)

    result = asyncio.run(
        api.record_reality_touch_output(
            "u",
            "早呀，该起床啦。",
            source="wakeup_alarm",
            delivered_at=1000,
        )
    )
    assert result == {"recorded": False, "reason": "reality_companion_unavailable"}
    assert "last_reality_touch_output" not in host.data["users"]["u"]
    assert host.saved == 0


def test_recent_reality_output_and_user_reply_form_one_continuous_exchange() -> None:
    host = _RecordingHost()
    reality_api = _RealityApi()
    host._reality_companion_api = lambda: reality_api
    delivered_at = time.time() - 3
    asyncio.run(
        host._record_reality_touch_output(
            "u",
            "早呀，该起床啦。",
            source="wakeup_alarm",
            delivered_at=delivered_at,
        )
    )
    user = host.data["users"]["u"]
    user.update({"last_user_message": "早", "last_user_message_at": delivered_at + 2})

    context = host._format_reality_touch_continuity_context(user)

    assert "Bot 已通过现实音频设备对用户说：早呀，该起床啦。" in context
    assert "用户随后在私聊回应：早" in context
    assert "不要把它当作首次问候" in context


def test_missing_reality_plugin_does_not_write_new_runtime_state_to_core() -> None:
    host = _RecordingHost()
    host._reality_companion_api = lambda: None

    result = asyncio.run(host._record_reality_touch_output("u", "不会写进本体"))

    assert result == {"recorded": False, "reason": "reality_companion_unavailable"}
    assert "last_reality_touch_output" not in host.data["users"]["u"]
    assert host.saved == 0


class _PreflightEvent:
    def __init__(self, text: str = "早") -> None:
        self.message_str = text
        self.stopped = False
        self.replies: list[str] = []

    def get_sender_id(self) -> str:
        return "u"

    def stop_event(self) -> None:
        self.stopped = True


class _PreflightRealityApi:
    def __init__(self, *, eligible: bool = True, reply: str | None = None, raise_on_apply: bool = False) -> None:
        self.eligible = eligible
        self.reply = reply
        self.raise_on_apply = raise_on_apply
        self.apply_calls: list[tuple[str, str]] = []

    def camera_user_eligible(self, _user_id: str) -> bool:
        return self.eligible

    def apply_pending_confirmation(self, user_id: str, text: str) -> str | None:
        self.apply_calls.append((user_id, text))
        if self.raise_on_apply:
            raise RuntimeError("external plugin failure")
        return self.reply


class _PreflightHost(RealityCompanionBridgeMixin):
    # 直接读取插件类的契约常量：若插件侧丢失该常量，本文件在收集阶段即失败。
    _REALITY_TOUCH_CAMERA_CAPABILITY = PrivateCompanionPlugin._REALITY_TOUCH_CAMERA_CAPABILITY

    def __init__(self, api: _PreflightRealityApi) -> None:
        self.data = {"users": {"u": {"user_id": "u"}}}
        self._data_lock = asyncio.Lock()
        self._api = api
        self.saves = 0

    def _reality_companion_api(self) -> _PreflightRealityApi:
        return self._api

    def _save_data_sync(self) -> None:
        self.saves += 1

    async def _reply(self, event: _PreflightEvent, text: str, **_kwargs) -> None:
        event.replies.append(text)

    async def preflight(self, event: _PreflightEvent) -> bool:
        return await PrivateCompanionPlugin._handle_private_message_preflight(self, event)


def _legacy_camera_pending() -> dict:
    return {
        "capability": "camera_single_frame",
        "requested_at": 1000.0,
        "expires_at": 1600.0,
    }


def test_plugin_class_keeps_camera_capability_contract_constant() -> None:
    assert PrivateCompanionPlugin._REALITY_TOUCH_CAMERA_CAPABILITY == "camera_single_frame"


def test_preflight_clears_legacy_camera_pending_for_ineligible_user() -> None:
    api = _PreflightRealityApi(eligible=False, reply="不应到达")
    host = _PreflightHost(api)
    host.data["users"]["u"]["reality_touch_pending_consent"] = _legacy_camera_pending()
    event = _PreflightEvent("我理解风险并确认授权")

    handled = asyncio.run(host.preflight(event))

    assert handled is True
    assert event.stopped is True
    assert event.replies == ["主机摄像头只允许 AstrBot 管理员或主要用户本人授权和使用。"]
    assert "reality_touch_pending_consent" not in host.data["users"]["u"]
    assert host.saves == 1
    assert api.apply_calls == []


def test_preflight_forwards_legacy_camera_pending_for_eligible_user() -> None:
    api = _PreflightRealityApi(eligible=True, reply="现实触及摄像头独立授权已记录。")
    host = _PreflightHost(api)
    host.data["users"]["u"]["reality_touch_pending_consent"] = _legacy_camera_pending()
    event = _PreflightEvent("我理解风险并确认授权")

    handled = asyncio.run(host.preflight(event))

    assert handled is True
    assert event.replies == ["现实触及摄像头独立授权已记录。"]
    assert api.apply_calls == [("u", "我理解风险并确认授权")]
    assert "reality_touch_pending_consent" in host.data["users"]["u"]
    assert host.saves == 0


def test_preflight_external_handler_failure_does_not_break_private_chat() -> None:
    api = _PreflightRealityApi(raise_on_apply=True)
    host = _PreflightHost(api)
    host.data["users"]["u"]["reality_touch_pending_consent"] = {"capability": "local_audio"}
    event = _PreflightEvent("在吗")

    handled = asyncio.run(host.preflight(event))

    assert handled is False
    assert event.stopped is False
    assert event.replies == []
    assert api.apply_calls == [("u", "在吗")]


def test_preflight_without_pending_still_consults_external_handler() -> None:
    api = _PreflightRealityApi(reply=None)
    host = _PreflightHost(api)
    event = _PreflightEvent("在吗")

    handled = asyncio.run(host.preflight(event))

    assert handled is False
    assert event.replies == []
    assert api.apply_calls == []
