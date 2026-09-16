"""Diagnostics support for the ISEO Argo BLE Lock integration."""

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS, CONF_UUID
from homeassistant.core import HomeAssistant

from .const import CONF_PRIV_SCALAR
from .coordinator import IseoConfigEntry

# The UUID and private scalar together are the gateway's credentials on the lock.
ENTRY_DATA_TO_REDACT = frozenset({CONF_ADDRESS, CONF_UUID, CONF_PRIV_SCALAR})
SERVICE_INFO_TO_REDACT = frozenset({"address", "device", "name", "source"})


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IseoConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    service_info = bluetooth.async_last_service_info(
        hass, coordinator.address, connectable=True
    )
    state = coordinator.data

    return {
        "entry_data": async_redact_data(dict(entry.data), ENTRY_DATA_TO_REDACT),
        "service_info": async_redact_data(
            service_info.as_dict() if service_info else None, SERVICE_INFO_TO_REDACT
        ),
        "door_status_supported": coordinator.door_status_supported,
        "last_poll_successful": coordinator.last_poll_successful,
        "lock_state": {
            "aux_battery_low": state.aux_battery_low,
            "battery_level": state.battery_level,
            "door_closed": state.door_closed,
            "firmware_info": state.firmware_info,
            "invitation_pending": state.invitation_pending,
            "operational_mode": state.operational_mode,
            "passage_mode_light": state.passage_mode_light,
            "passage_mode_normal": state.passage_mode_normal,
            "privacy_mode": state.privacy_mode,
            "vip_mode": state.vip_mode,
        }
        if state
        else None,
    }
