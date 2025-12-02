# -----------------------------
# Cover Time based via Modbus
# -----------------------------
import logging
import asyncio
from datetime import timedelta

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    PLATFORM_SCHEMA,
    CoverEntity,
)
from homeassistant.const import (
    CONF_NAME,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_STOP_COVER,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity

_LOGGER = logging.getLogger(__name__)

CONF_DEVICES = "devices"
CONF_SLAVE = "slave"
CONF_COIL_OPEN = "coil_open"
CONF_COIL_CLOSE = "coil_close"
CONF_TRAVEL_UP = "travel_up"
CONF_TRAVEL_DOWN = "travel_down"
CONF_HUB = "hub"
DEFAULT_TRAVEL_TIME = 25

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HUB): cv.string,
        vol.Optional(CONF_DEVICES, default={}): vol.Schema(
            {
                cv.string: {
                    vol.Optional(CONF_NAME): cv.string,
                    vol.Required(CONF_SLAVE): cv.positive_int,
                    vol.Required(CONF_COIL_OPEN): cv.positive_int,
                    vol.Required(CONF_COIL_CLOSE): cv.positive_int,
                    vol.Optional(CONF_TRAVEL_UP, default=DEFAULT_TRAVEL_TIME): cv.positive_int,
                    vol.Optional(CONF_TRAVEL_DOWN, default=DEFAULT_TRAVEL_TIME): cv.positive_int,
                }
            }
        ),
    }
)


def devices_from_config(hass, config):
    devices = []
    hub = config[CONF_HUB]
    for device_id, dev_conf in config[CONF_DEVICES].items():
        name = dev_conf.get(CONF_NAME)
        slave = dev_conf[CONF_SLAVE]
        coil_open = dev_conf[CONF_COIL_OPEN]
        coil_close = dev_conf[CONF_COIL_CLOSE]
        travel_up = dev_conf[CONF_TRAVEL_UP]
        travel_down = dev_conf[CONF_TRAVEL_DOWN]

        device = CoverTimeModbus(
            hass, hub, device_id, name, slave, coil_open, coil_close, travel_up, travel_down
        )
        devices.append(device)
    return devices


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities(devices_from_config(hass, config))


class CoverTimeModbus(CoverEntity, RestoreEntity):
    """Cover entity using Modbus hub instead of I2C."""

    def __init__(self, hass, hub, device_id, name, slave, coil_open, coil_close, travel_up, travel_down):
        from xknx.devices import TravelCalculator

        self.hass = hass
        self._hub = hub
        self._device_id = device_id
        self._name = name or device_id
        self._slave = slave
        self._coil_open = coil_open
        self._coil_close = coil_close
        self._travel_up = travel_up
        self._travel_down = travel_down

        self.tc = TravelCalculator(travel_down, travel_up)
        self._unsubscribe_auto_updater = None

    async def async_added_to_hass(self):
        old_state = await self.async_get_last_state()
        if old_state and old_state.attributes.get(ATTR_CURRENT_POSITION) is not None:
            self.tc.set_position(int(old_state.attributes.get(ATTR_CURRENT_POSITION)))

    @property
    def name(self):
        return self._name

    @property
    def device_state_attributes(self):
        return {
            "travel_up": self._travel_up,
            "travel_down": self._travel_down
        }

    @property
    def current_cover_position(self):
        return self.tc.current_position()

    @property
    def is_opening(self):
        from xknx.devices import TravelStatus
        return self.tc.is_traveling() and self.tc.travel_direction == TravelStatus.DIRECTION_UP

    @property
    def is_closing(self):
        from xknx.devices import TravelStatus
        return self.tc.is_traveling() and self.tc.travel_direction == TravelStatus.DIRECTION_DOWN

    @property
    def is_closed(self):
        return self.tc.is_closed()

    @property
    def assumed_state(self):
        return True

    # -----------------------------
    # Old working async_set_cover_position logic restored
    # -----------------------------
    async def async_set_cover_position(self, **kwargs):
        if ATTR_POSITION in kwargs:
            position = kwargs[ATTR_POSITION]
            _LOGGER.debug("async_set_cover_position: %d", position)
            await self.set_position(position)

    async def async_close_cover(self, **kwargs):
        _LOGGER.debug("async_close_cover")
        self.tc.start_travel_down()
        self.start_auto_updater()
        await self._async_send_modbus_command(SERVICE_CLOSE_COVER)

    async def async_open_cover(self, **kwargs):
        _LOGGER.debug("async_open_cover")
        self.tc.start_travel_up()
        self.start_auto_updater()
        await self._async_send_modbus_command(SERVICE_OPEN_COVER)

    async def async_stop_cover(self, **kwargs):
        _LOGGER.debug("async_stop_cover")
        self.tc.stop()
        await self._async_send_modbus_command(SERVICE_STOP_COVER)
        self.stop_auto_updater()

    # -----------------------------
    # Full set_position logic from old code
    # -----------------------------
    async def set_position(self, position):
        current = self.tc.current_position()
        _LOGGER.debug("set_position requested: %d (current %d)", position, current)

        if position == current:
            return

        command = None
        if position < current:
            command = SERVICE_CLOSE_COVER
            self.tc.start_travel_down()
        elif position > current:
            command = SERVICE_OPEN_COVER
            self.tc.start_travel_up()

        # Calculate remaining runtime
        self.tc.start_travel(position)
        self.start_auto_updater()

        _LOGGER.debug("set_position executing command %s", command)
        await self._async_send_modbus_command(command)

    # -----------------------------
    # Auto updater
    # -----------------------------
    def start_auto_updater(self):
        if self._unsubscribe_auto_updater is None:
            self._unsubscribe_auto_updater = async_track_time_interval(
                self.hass, self._auto_updater_hook, timedelta(seconds=0.1)
            )

    def stop_auto_updater(self):
        if self._unsubscribe_auto_updater is not None:
            self._unsubscribe_auto_updater()
            self._unsubscribe_auto_updater = None

    @callback
    def _auto_updater_hook(self, now):
        self.async_schedule_update_ha_state()
        if self.tc.position_reached():
            self.stop_auto_updater()
        self.hass.async_create_task(self._auto_stop_if_needed())

    async def _auto_stop_if_needed(self):
        if self.tc.position_reached():
            await self._async_send_modbus_command(SERVICE_STOP_COVER)
            self.tc.stop()

    # -----------------------------
    # Modbus command sending (mirrors old _async_handle_command)
    # -----------------------------
    async def _async_send_modbus_command(self, command):
        if command == SERVICE_OPEN_COVER:
            # Close coil off first
            await self.hass.services.async_call("modbus", "write_coil", {
                "hub": self._hub,
                "unit": self._slave,
                "address": self._coil_close,
                "state": False
            })
            await asyncio.sleep(0.3)
            # Open coil on
            await self.hass.services.async_call("modbus", "write_coil", {
                "hub": self._hub,
                "unit": self._slave,
                "address": self._coil_open,
                "state": True
            })

        elif command == SERVICE_CLOSE_COVER:
            # Open coil off first
            await self.hass.services.async_call("modbus", "write_coil", {
                "hub": self._hub,
                "unit": self._slave,
                "address": self._coil_open,
                "state": False
            })
            await asyncio.sleep(0.3)
            # Close coil on
            await self.hass.services.async_call("modbus", "write_coil", {
                "hub": self._hub,
                "unit": self._slave,
                "address": self._coil_close,
                "state": True
            })

        elif command == SERVICE_STOP_COVER:
            # Turn all coils off
            await self.hass.services.async_call("modbus", "write_coil", {
                "hub": self._hub,
                "unit": self._slave,
                "address": self._coil_open,
                "state": False
            })
            await self.hass.services.async_call("modbus", "write_coil", {
                "hub": self._hub,
                "unit": self._slave,
                "address": self._coil_close,
                "state": False
            })
