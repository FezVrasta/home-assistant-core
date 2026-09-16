"""Config flow for ISEO Argo BLE Lock."""

from collections.abc import Mapping
import logging
from typing import Any, override
import uuid as uuid_module

from cryptography.hazmat.primitives.asymmetric import ec
from iseo_argo_ble import IseoAuthError, IseoConnectionError, is_iseo_advertisement
import probatio

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_UUID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import CONF_PRIV_SCALAR, DOMAIN, GATEWAY_NAME
from .coordinator import async_build_client

_LOGGER = logging.getLogger(__name__)


def _generate_identity() -> ec.EllipticCurvePrivateKey:
    """Generate a fresh SECP224R1 private key for use as an Argo BT identity."""
    return ec.generate_private_key(ec.SECP224R1())


def _discover_locks(hass: HomeAssistant) -> list[BluetoothServiceInfoBleak]:
    """Query HA's bluetooth integration for nearby ISEO locks."""
    all_devices = sorted(
        async_discovered_service_info(hass, connectable=True),
        key=lambda i: i.rssi,
        reverse=True,
    )
    _LOGGER.debug(
        "HA bluetooth cache — %d connectable device(s) visible", len(all_devices)
    )

    found: list[BluetoothServiceInfoBleak] = []
    for info in all_devices:
        if not is_iseo_advertisement(list(info.service_uuids or [])):
            continue
        _LOGGER.debug(
            "  %s  name=%r  rssi=%d — ISEO lock",
            info.address,
            info.name,
            info.rssi,
        )
        found.append(info)

    return found


class IseoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for ISEO Argo BLE Lock."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._address: str = ""
        self._device_name: str = ""
        self._uuid_hex: str = ""
        self._priv_scalar: str = ""

    async def _async_generate_identity(self, address: str, name: str) -> None:
        """Generate the gateway identity this flow will enroll on the lock."""
        priv = await self.hass.async_add_executor_job(_generate_identity)
        self._address = address
        self._device_name = name
        self._uuid_hex = uuid_module.uuid4().bytes.hex()
        self._priv_scalar = hex(priv.private_numbers().private_value)

    @property
    def _entry_data(self) -> dict[str, str]:
        """Return the config entry data for the identity being enrolled."""
        return {
            CONF_ADDRESS: self._address,
            CONF_UUID: self._uuid_hex,
            CONF_PRIV_SCALAR: self._priv_scalar,
        }

    async def _async_enroll_gateway(self, data: Mapping[str, Any]) -> dict[str, str]:
        """Register the identity in ``data`` as a gateway on the lock.

        Returns the form errors, empty when the enrollment succeeded.
        """
        if not (
            ble_device := async_ble_device_from_address(
                self.hass, data[CONF_ADDRESS], connectable=True
            )
        ):
            return {"base": "cannot_connect"}

        client = await async_build_client(self.hass, data, ble_device)
        try:
            await client.setup_gateway(name=GATEWAY_NAME)
        except IseoConnectionError:
            return {"base": "cannot_connect"}
        except IseoAuthError as exc:
            _LOGGER.debug("Gateway setup failed: %s", exc)
            return {"base": "auth_failed"}
        except Exception:
            _LOGGER.exception("Unexpected error during gateway setup")
            return {"base": "unknown"}
        return {}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a lock from HA's BLE cache."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]

            await self.async_set_unique_id(format_mac(address))
            self._abort_if_unique_id_configured()

            discovered = self._discovered.get(address)
            await self._async_generate_identity(
                address, discovered.name if discovered else ""
            )

            return await self.async_step_gw_register()

        configured = {
            entry.data.get(CONF_ADDRESS) for entry in self._async_current_entries()
        }
        found = [
            info
            for info in _discover_locks(self.hass)
            if info.address not in configured
        ]
        self._discovered = {info.address: info for info in found}

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=info.address,
                                    label=(
                                        f"{info.name or 'Unknown'}  —  {info.address}"
                                        f"  (RSSI {info.rssi} dBm)"
                                    ),
                                )
                                for info in found
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    @override
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Called by HA when a matching BLE advertisement is seen."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()

        if not is_iseo_advertisement(list(discovery_info.service_uuids or [])):
            return self.async_abort(reason="not_iseo_device")

        await self._async_generate_identity(
            discovery_info.address, discovery_info.name or discovery_info.address
        )

        self.context["title_placeholders"] = {"name": self._device_name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the discovered lock before proceeding to enrollment."""
        if user_input is not None:
            return await self.async_step_gw_register()

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._device_name},
        )

    async def async_step_gw_register(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Register the gateway and enable log notifications (requires Master Card)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entry_data = self._entry_data
            if not (errors := await self._async_enroll_gateway(entry_data)):
                return self.async_create_entry(
                    title=self._device_name or f"ISEO Lock ({self._address})",
                    data=entry_data,
                )

        return self.async_show_form(
            step_id="gw_register",
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle the lock no longer accepting the stored gateway identity."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enroll the stored gateway identity on the lock."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (errors := await self._async_enroll_gateway(reauth_entry.data)):
                return self.async_update_reload_and_abort(reauth_entry)

        return self.async_show_form(step_id="reauth_confirm", errors=errors)
