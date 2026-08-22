# -*- coding: utf-8 -*-
"""Self-contained MiHome integration used by reality touch.

The companion owns the integration boundary and its data files.  The legacy
MiHome plugin is only a migration source for auth/state files; no legacy
plugin module is imported here.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from mijiaAPI import mijiaAPI, LoginError, APIError, DeviceNotFoundError, DeviceSetError, DeviceActionError
except Exception:  # pragma: no cover - optional runtime dependency
    mijiaAPI = None
    LoginError = APIError = DeviceNotFoundError = DeviceSetError = DeviceActionError = Exception


class MiHomeIntegrationError(Exception):
    pass


class MiHomeIntegration:
    """Owns MiHome auth, catalog and model-facing safe operations."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        root = Path(get_astrbot_data_path())
        self.data_dir = root / "plugin_data" / "astrbot_plugin_private_companion" / "mihome"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.auth_path = self.data_dir / "auth.json"
        self.state_path = self.data_dir / "state.json"
        self._lock = asyncio.Lock()
        self._login_process: asyncio.subprocess.Process | None = None
        self._login_task: asyncio.Task | None = None
        self._login_running = False
        self._login_status = "idle"
        self._login_message = ""
        self._login_qr_url = ""
        self._login_qr_image = ""
        self._login_started_at = ""
        self._login_finished_at = ""
        self._migrate_legacy_files()
        self.api = mijiaAPI(str(self.auth_path)) if mijiaAPI is not None else None

    def _migrate_legacy_files(self) -> None:
        legacy_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_mihome"
        migrated = False
        for filename in ("auth.json", "state.json"):
            target = self.data_dir / filename
            source = legacy_dir / filename
            if target.exists() or not source.exists():
                continue
            try:
                shutil.copy2(source, target)
                migrated = True
                logger.info("[PrivateCompanion] 已迁移旧米家%s到本体数据目录", filename)
            except Exception as exc:
                logger.warning("[PrivateCompanion] 米家%s迁移失败: %s", filename, str(exc)[:160])
        if migrated:
            self._update_state(data_migrated=True)

    def _state(self) -> dict[str, Any]:
        try:
            if self.state_path.exists():
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
        except Exception:
            pass
        return {}

    def _update_state(self, **values: Any) -> None:
        state = self._state()
        state.update(values)
        try:
            self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("[PrivateCompanion] 米家状态保存失败: %s", str(exc)[:160])

    @staticmethod
    def _text(value: Any, limit: int = 160) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    def _config_list(self, key: str) -> list[str]:
        config = getattr(self.plugin, "config", {})
        raw = config.get(key, config.get("reality_touch_scene_allowlist", []))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = raw.split(",")
        if not isinstance(raw, (list, tuple, set)):
            return []
        return [self._text(item, 120) for item in raw if self._text(item, 120)]

    def _device_aliases(self) -> dict[str, str]:
        config = getattr(self.plugin, "config", {})
        raw = config.get("mihome_device_map", config.get("device_map", {}))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        return {self._text(key, 80): self._text(value, 120) for key, value in (raw.items() if isinstance(raw, dict) else []) if self._text(key, 80) and self._text(value, 120)}

    def status(self) -> dict[str, Any]:
        state = self._state()
        enabled = bool(getattr(self.plugin, "enable_mihome_integration", False))
        scenes = state.get("scenes") if isinstance(state.get("scenes"), list) else []
        devices = state.get("devices") if isinstance(state.get("devices"), list) else []
        return {
            "enabled": enabled,
            "available": self.api is not None,
            "auth_exists": self.auth_path.exists(),
            "login_in_progress": self._login_running,
            "login_status": self._login_status,
            "login_message": self._login_message,
            "qr_url": self._login_qr_url if self._login_running else "",
            "qr_image": self._login_qr_image if self._login_running else "",
            "login_started_at": self._login_started_at,
            "login_finished_at": self._login_finished_at,
            "scene_count": len(scenes),
            "device_count": len(devices),
            "allowlist_count": len(self._config_list("mihome_scene_allowlist")),
            "device_alias_count": len(self._device_aliases()),
            "direct_control_enabled": bool(getattr(self.plugin, "mihome_allow_direct_device_control", False)),
            "read_state_enabled": bool(getattr(self.plugin, "mihome_read_state_enabled", True)),
            "data_migrated": bool(state.get("data_migrated")),
            "last_login_at": state.get("last_login_at", ""),
            "last_error": state.get("last_error", ""),
            "data_dir": str(self.data_dir),
        }

    async def close(self) -> None:
        task = self._login_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._login_task = None
        process = self._login_process
        if process is not None and process.returncode is None:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        self._login_process = None
        self._login_running = False

    async def logout(self) -> bool:
        await self.close()
        removed = False
        try:
            if self.auth_path.exists():
                self.auth_path.unlink()
                removed = True
        except Exception:
            pass
        self.api = mijiaAPI(str(self.auth_path)) if mijiaAPI is not None else None
        self._update_state(last_login_at="", last_error="", scenes=[], devices=[])
        self._login_status = "idle"
        self._login_message = ""
        self._login_qr_url = ""
        self._login_qr_image = ""
        self._login_started_at = ""
        self._login_finished_at = ""
        return removed

    async def login(self, qr_callback: Callable[[str], Awaitable[None] | None]) -> dict[str, Any]:
        if self.api is None:
            self._login_status = "unavailable"
            self._login_message = "mijiaAPI 未安装"
            return {"status": "unavailable", "message": "mijiaAPI 未安装"}
        if self._login_running:
            return {"status": "in_progress"}
        self._login_running = True
        self._login_status = "starting"
        self._login_message = "正在准备米家授权"
        self._login_qr_url = ""
        self._login_qr_image = ""
        self._login_started_at = datetime.now().isoformat(timespec="seconds")
        self._login_finished_at = ""
        process = None
        try:
            worker = Path(__file__).with_name("_mihome_login_worker.py")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(worker), str(self.auth_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            self._login_process = process
            buffer = ""
            qr_sent = False

            async def consume_output() -> None:
                nonlocal buffer, qr_sent
                while process.stdout is not None:
                    chunk = await process.stdout.read(256)
                    if not chunk:
                        break
                    buffer = (buffer + chunk.decode("utf-8", errors="replace"))[-16384:]
                    if not qr_sent:
                        match = re.search(r"https://account\.xiaomi\.com/pass/qr/login\?[^\s'\"]+", buffer.replace("\n", ""))
                        if match and "ticket=" in match.group(0):
                            qr_sent = True
                            self._login_qr_url = match.group(0)
                            self._login_qr_image = self._qr_image_data_url(self._login_qr_url)
                            self._login_status = "awaiting_scan"
                            self._login_message = "请使用米家 App 扫码授权"
                            callback_result = qr_callback(match.group(0))
                            if hasattr(callback_result, "__await__"):
                                await callback_result

            await asyncio.wait_for(asyncio.gather(process.wait(), consume_output()), timeout=120)
            if process.returncode == 0:
                self.api = mijiaAPI(str(self.auth_path))
                self._update_state(last_login_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), last_error="")
                self._login_status = "success" if qr_sent else "already_logged_in"
                self._login_message = "米家授权成功" if qr_sent else "米家已经处于登录状态"
                return {"status": "success" if qr_sent else "already_logged_in"}
            self._update_state(last_error=buffer[-800:])
            self._login_status = "error"
            self._login_message = self._text(buffer[-800:], 800)
            return {"status": "error", "message": self._text(buffer[-800:], 800)}
        except asyncio.TimeoutError:
            if process is not None:
                process.kill()
            self._login_status = "timeout"
            self._login_message = "米家登录二维码已超时"
            return {"status": "timeout"}
        except Exception as exc:
            self._update_state(last_error=str(exc))
            self._login_status = "error"
            self._login_message = self._text(exc, 300)
            return {"status": "error", "message": self._text(exc, 300)}
        finally:
            self._login_running = False
            self._login_process = None
            self._login_finished_at = datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _qr_image_data_url(url: str) -> str:
        """Render the Xiaomi login URL when the optional QR package is present."""
        try:
            import qrcode

            image = qrcode.make(url)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception:
            return ""

    async def start_login(self) -> dict[str, Any]:
        """Start QR login without holding the page request open."""
        if self.api is None:
            self._login_status = "unavailable"
            self._login_message = "mijiaAPI 未安装"
            return self.status()
        if self._login_running:
            return self.status()

        async def qr_callback(_url: str) -> None:
            return None

        async def runner() -> None:
            result = await self.login(qr_callback)
            if result.get("status") == "error":
                self._login_message = self._text(result.get("message"), 800)

        task = asyncio.create_task(runner(), name="private-companion-mihome-login")
        self._login_task = task
        # Let the runner mark the service as busy before the page response is built.
        await asyncio.sleep(0)

        def clear_task(done: asyncio.Task) -> None:
            if self._login_task is done:
                self._login_task = None
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(clear_task)
        return self.status()

    async def _login(self) -> None:
        if self.api is None:
            raise MiHomeIntegrationError("mijiaAPI 未安装")
        if not self.auth_path.exists():
            raise MiHomeIntegrationError("米家尚未登录")
        try:
            await asyncio.wait_for(asyncio.to_thread(self.api.login), timeout=15)
        except LoginError as exc:
            raise MiHomeIntegrationError("米家登录已失效") from exc

    @staticmethod
    def _scene(item: dict[str, Any]) -> dict[str, str]:
        return {
            "scene_id": str(item.get("scene_id") or item.get("id") or item.get("sceneId") or "").strip(),
            "scene_name": str(item.get("scene_name") or item.get("name") or item.get("title") or "").strip(),
            "home_id": str(item.get("home_id") or item.get("homeId") or "").strip(),
        }

    async def list_scenes(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not bool(getattr(self.plugin, "enable_mihome_integration", False)):
            return {"ok": False, "reason": "mihome_disabled", "scenes": []}
        try:
            async with self._lock:
                await self._login()
                raw = await asyncio.wait_for(asyncio.to_thread(self.api.get_scenes_list), timeout=30)
            scenes = [self._scene(item) for item in (raw or []) if isinstance(item, dict)]
            scenes = [item for item in scenes if item["scene_id"] and item["scene_name"]]
            allow = set(self._config_list("mihome_scene_allowlist"))
            # Device actions stay opt-in. An empty allowlist exposes no scene
            # to the model until an administrator explicitly selects it.
            include_unlisted = bool((_payload or {}).get("include_unlisted"))
            visible = scenes if include_unlisted else [
                item for item in scenes
                if allow and (item["scene_id"] in allow or item["scene_name"] in allow)
            ]
            self._update_state(scenes=visible, scene_cache_updated_at=datetime.now().isoformat(), last_error="")
            return {"ok": True, "scenes": visible, "reason": "scene_allowlist_empty" if not allow else ""}
        except Exception as exc:
            self._update_state(last_error=self._text(exc, 300))
            return {"ok": False, "reason": "mihome_unavailable", "message": self._text(exc, 300), "scenes": []}

    async def run_scene(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request = dict(payload or {})
        requested_id = self._text(request.get("scene_id"), 160)
        requested_name = self._text(request.get("scene_name"), 160)
        catalog = await self.list_scenes(request)
        selected = next((item for item in catalog.get("scenes", []) if item["scene_id"] == requested_id or item["scene_name"] == requested_name), None)
        if not selected:
            return {"ok": False, "reason": "scene_not_found"}
        try:
            async with self._lock:
                await self._login()
                kwargs = {"scene_id": selected["scene_id"]}
                if selected.get("home_id"):
                    kwargs["home_id"] = selected["home_id"]
                await asyncio.wait_for(asyncio.to_thread(self.api.run_scene, **kwargs), timeout=25)
            self._update_state(last_scene_name=selected["scene_name"], last_error="")
            return {"ok": True, **selected}
        except Exception as exc:
            self._update_state(last_error=self._text(exc, 300))
            return {"ok": False, "reason": "scene_execution_failed", "message": self._text(exc, 300), **selected}

    async def list_devices(self, _payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not bool(getattr(self.plugin, "enable_mihome_integration", False)):
            return {"ok": False, "reason": "mihome_disabled", "devices": []}
        try:
            async with self._lock:
                await self._login()
                raw = await asyncio.wait_for(asyncio.to_thread(self.api.get_devices_list), timeout=25)
            aliases = self._device_aliases()
            include_unmapped = bool((_payload or {}).get("include_unmapped"))
            devices = []
            for item in raw or []:
                if not isinstance(item, dict) or not item.get("did"):
                    continue
                did = self._text(item.get("did"), 120)
                name = self._text(item.get("name") or did, 100)
                alias = next((key for key, value in aliases.items() if value == did), "")
                if not alias and not include_unmapped:
                    continue
                devices.append({"did": did, "name": name, "alias": alias, "model": self._text(item.get("model"), 100), "online": item.get("isOnline")})
            self._update_state(devices=devices, last_error="")
            return {"ok": True, "devices": devices}
        except Exception as exc:
            return {"ok": False, "reason": "mihome_unavailable", "message": self._text(exc, 300), "devices": []}

    async def get_device_state(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not bool(getattr(self.plugin, "enable_mihome_integration", False)):
            return {"ok": False, "reason": "mihome_disabled"}
        if not bool(getattr(self.plugin, "mihome_read_state_enabled", True)):
            return {"ok": False, "reason": "mihome_read_state_disabled"}
        request = dict(payload or {})
        alias = self._text(request.get("alias") or request.get("device"), 100)
        devices = (await self.list_devices(request)).get("devices", [])
        item = next((device for device in devices if device.get("alias") == alias or device.get("name") == alias or device.get("did") == alias), None)
        if not item:
            return {"ok": False, "reason": "device_not_found"}
        try:
            from mijiaAPI import mijiaDevice
            async with self._lock:
                device = await asyncio.wait_for(asyncio.to_thread(mijiaDevice, self.api, did=item["did"], sleep_time=1.0), timeout=15)
                props = getattr(device, "prop_list", {}) if isinstance(getattr(device, "prop_list", {}), dict) else {}
                keys = [str(key) for key, info in list(props.items())[:20] if "read" in str(getattr(info, "rw", "")).lower()]
                values: dict[str, Any] = {}
                for key in keys[:8]:
                    try:
                        values[key] = await asyncio.wait_for(asyncio.to_thread(device.get, key), timeout=5)
                    except Exception:
                        continue
            return {"ok": True, "device": item, "state": values}
        except Exception as exc:
            return {"ok": False, "reason": "device_state_failed", "message": self._text(exc, 300)}

    async def control_device(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not bool(getattr(self.plugin, "enable_mihome_integration", False)):
            return {"ok": False, "reason": "mihome_disabled"}
        if not bool(getattr(self.plugin, "mihome_allow_direct_device_control", False)):
            return {"ok": False, "reason": "direct_device_control_disabled"}
        request = dict(payload or {})
        alias = self._text(request.get("alias") or request.get("device"), 100)
        prop = self._text(request.get("property") or request.get("prop"), 80)
        value = request.get("value")
        devices = (await self.list_devices(request)).get("devices", [])
        item = next((device for device in devices if device.get("alias") == alias or device.get("name") == alias or device.get("did") == alias), None)
        if not item or not prop:
            return {"ok": False, "reason": "device_or_property_missing"}
        if bool(getattr(self.plugin, "mihome_require_explicit_confirmation", True)) and request.get("confirmed") is not True:
            return {"ok": False, "reason": "explicit_confirmation_required"}
        try:
            from mijiaAPI import mijiaDevice
            async with self._lock:
                device = await asyncio.wait_for(asyncio.to_thread(mijiaDevice, self.api, did=item["did"], sleep_time=1.0), timeout=15)
                await asyncio.wait_for(asyncio.to_thread(device.set, prop, value), timeout=15)
            return {"ok": True, "device": item, "property": prop, "value": value}
        except (DeviceNotFoundError, DeviceSetError, DeviceActionError) as exc:
            return {"ok": False, "reason": "device_rejected", "message": self._text(exc, 240)}
        except Exception as exc:
            return {"ok": False, "reason": "device_control_failed", "message": self._text(exc, 300)}
