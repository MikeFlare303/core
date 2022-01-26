"""Collection of devices controllable by Hue."""
import logging
from datetime import datetime

from emulated_hue import const
from emulated_hue.config import Config
from emulated_hue.utils import clamp

from .homeassistant import HomeAssistantController
from .models import ALL_STATES, EntityState
from .scheduler import add_scheduler

import functools
from typing import Any

LOGGER = logging.getLogger(__name__)

__device_cache = {}

def ensure_control_state(func):
    """Ensure that the control state exists and create one if it doesn't."""

    @functools.wraps(func)
    def wrapped_func(*args, **kwargs):
        """Wrapped function."""
        cls = args[0] or kwargs.get('cls')  # type: OnOffDevice
        setting: Any = args[1] or kwargs.get('setting')
        control_id: int | None = args[2] or kwargs.get('control_id')
        if control_id is None:
            # control id is None or invalid
            control_id = cls._new_control_state()
        elif not cls._control_state.get(control_id):
            # control id is valid but control state doesn't exist
            LOGGER.warning("Control id %s is not valid, creating a new one", control_id)
            cls._new_control_state(control_id)
        return func(cls, setting, control_id, cls._control_state.get(control_id))


    return wrapped_func


class Device:
    """Get device properties from an entity id."""

    def __init__(self, ctrl_hass: HomeAssistantController, entity_id: str):
        """Initialize device."""
        self._ctrl_hass: HomeAssistantController = ctrl_hass
        self._entity_id: str = entity_id

        self._device_id: str = self._ctrl_hass.get_device_id_from_entity_id(
            self._entity_id
        )
        self._device_attributes: dict = {}
        if self._device_id:
            self._device_attributes = self._ctrl_hass.get_device_attributes(
                self._device_id
            )

        self._unique_id: str | None = None
        if identifiers := self._device_attributes.get("identifiers"):
            if isinstance(identifiers, dict):
                # prefer real zigbee address if we have that
                # might come in handy later when we want to
                # send entertainment packets to the zigbee mesh
                for key, value in identifiers:
                    if key == "zha":
                        self._unique_id = value
            elif isinstance(identifiers, list):
                # simply grab the first available identifier for now
                # may inprove this in the future
                for identifier in identifiers:
                    if isinstance(identifier, list):
                        self._unique_id = identifier[-1]
                        break
                    elif isinstance(identifier, str):
                        self._unique_id = identifier
                        break

    @property
    def manufacturer(self) -> str | None:
        """Return manufacturer."""
        return self._device_attributes.get("manufacturer")

    @property
    def model(self) -> str | None:
        """Return device model."""
        return self._device_attributes.get("model")

    @property
    def name(self) -> str | None:
        """Return device name."""
        return self._device_attributes.get("name")

    @property
    def sw_version(self) -> str | None:
        """Return software version."""
        return self._device_attributes.get("sw_version")

    @property
    def unique_id(self) -> str | None:
        """Return unique id."""
        return self._unique_id


class OnOffDevice:
    """OnOffDevice class."""

    def __init__(
            self,
            ctrl_hass: HomeAssistantController,
            ctrl_config: Config,
            light_id: str,
            entity_id: str,
            config: dict,
            hass_state_dict: dict,
    ):
        """Initialize OnOffDevice."""
        self._ctrl_hass: HomeAssistantController = ctrl_hass
        self._ctrl_config: Config = ctrl_config
        self._light_id: str = light_id
        self._entity_id: str = entity_id

        self._device = Device(ctrl_hass, entity_id)

        self._hass_state_dict: dict = hass_state_dict  # state from Home Assistant

        self._config: dict = config
        self._name: str = self._config.get("name", "")
        self._unique_id: str = self._config.get("uniqueid", "")
        self._enabled: bool = self._config.get("enabled")

        # throttling
        self._throttle_ms: int | None = self._config.get("throttle")
        self._last_update: float = datetime.now().timestamp()
        self._default_transition: float = const.DEFAULT_TRANSITION_SECONDS
        if self._throttle_ms > self._default_transition:
            self._default_transition = self._throttle_ms / 1000

        self._hass_state: None | EntityState = None  # EntityState from Home Assistant
        self._config_state: None | EntityState = None  # Latest state and stored in config
        self._control_state: dict[int: EntityState] = {}  # Control state

    def __next_control_state_id(self):
        """Return next control state id."""
        if self._control_state:
            return max(self._control_state.keys()) + 1
        return 0

    @property
    def enabled(self) -> bool:
        """Return enabled state."""
        return self._enabled

    @property
    def DeviceProperties(self) -> Device:
        """Return device object."""
        return self._device

    @property
    def unique_id(self) -> str:
        """Return hue unique id."""
        return self._unique_id

    @property
    def name(self) -> str:
        """Return device name, prioritizing local config."""
        return self._name or self._hass_state_dict.get(const.HASS_ATTR, {}).get(
            "friendly_name"
        )

    @property
    def light_id(self) -> str:
        """Return light id."""
        return self._light_id

    @property
    def entity_id(self) -> str:
        """Return entity id."""
        return self._entity_id

    @property
    def reachable(self) -> bool:
        """Return if device is reachable."""
        return self._config_state.reachable

    @property
    def power_state(self) -> bool:
        """Return power state."""
        return self._config_state.power_state

    async def _async_save_config(self) -> None:
        """Save config to file."""
        await self._ctrl_config.async_set_storage_value(
            "lights", self._light_id, self._config
        )

    async def _async_update_config_states(self, control_state: EntityState | None = None) -> None:
        """Update config states."""
        save_state = {}
        for state in ALL_STATES:
            # prioritize our last command if exists, then hass then last saved state
            if control_state and getattr(control_state, state) is not None:
                best_value = getattr(control_state, state)
            elif self._hass_state and getattr(self._hass_state, state) is not None:
                best_value = getattr(self._hass_state, state)
            else:
                best_value = self._config.get("state", {}).get(state, None)
            save_state[state] = best_value

        self._config["state"] = save_state
        self._config_state = EntityState(**save_state)
        await self._async_save_config()

    def _update_device_state(self, full_update: bool) -> None:
        """Update EntityState object. Do not set defaults or last state will not work. Set @property."""
        if full_update:
            self._hass_state = EntityState(
                power_state=self._hass_state_dict["state"] == const.HASS_STATE_ON,
                reachable=self._hass_state_dict["state"]
                          != const.HASS_STATE_UNAVAILABLE,
                transition_seconds=self._default_transition,
            )

    async def _async_update_allowed(self, control_state: EntityState) -> bool:
        """Check if update is allowed using basic throttling, only update every throttle_ms."""
        if self._throttle_ms is None or self._throttle_ms == 0:
            return True
        # if wanted state is equal to the current state, dont change
        if self._config_state == control_state:
            return False
        # if the last update was less than the throttle time ago, dont change
        now_timestamp = datetime.now().timestamp()
        if now_timestamp - self._last_update < self._throttle_ms / 1000:
            return False

        self._last_update = now_timestamp
        return True

    def _new_control_state(self, control_id: int | None = None) -> int:
        """Create new control state based on last known power state, passing new id."""
        if not control_id:
            control_id = self.__next_control_state_id()
        self._control_state[control_id] = EntityState(
            power_state=self._config_state.power_state,
            transition_seconds=self._default_transition,
        )
        return control_id

    async def async_update_state(self, full_update: bool = True) -> None:
        """Update EntityState object with Hass state."""
        if self._enabled or not self._config_state:
            self._hass_state_dict = self._ctrl_hass.get_entity_state(self._entity_id)
            # Cascades up the inheritance chain to update the state
            self._update_device_state(full_update)
            await self._async_update_config_states()

    @property
    def transition_seconds(self) -> float:
        """Return transition seconds."""
        return self._config_state.transition_seconds

    @ensure_control_state
    def set_transition_ms(self, transition_ms: float, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set transition in milliseconds."""
        if transition_ms < self._throttle_ms:
            transition_ms = self._throttle_ms
        __control_state.transition_seconds = transition_ms / 1000
        return control_id

    def set_transition_seconds(self, transition_seconds: float, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set transition in seconds."""
        return self.set_transition_ms(transition_seconds * 1000, control_id)

    @ensure_control_state
    def set_power_state(self, power_state: bool, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set power state."""
        __control_state.power_state = power_state
        return control_id

    async def async_execute(self, control_id: int) -> None:
        """Execute control state."""
        control_state = self._control_state.pop(control_id, None)
        if not control_state:
            LOGGER.warning("No state to execute for device %s", self._entity_id)
            return

        if not await self._async_update_allowed(control_state):
            return
        if control_state.power_state:
            await self._ctrl_hass.async_turn_on(
                self._entity_id, control_state.to_hass_data()
            )
        else:
            await self._ctrl_hass.async_turn_off(
                self._entity_id, control_state.to_hass_data()
            )
        await self._async_update_config_states(control_state)



class BrightnessDevice(OnOffDevice):
    """BrightnessDevice class."""

    # Override
    def _update_device_state(self, full_update: bool) -> None:
        """Update EntityState object."""
        super()._update_device_state(full_update)
        self._hass_state.brightness = self._hass_state_dict.get(
            const.HASS_ATTR, {}
        ).get(const.HASS_ATTR_BRIGHTNESS)

    @property
    def brightness(self) -> int:
        """Return brightness."""
        return self._config_state.brightness or 0

    @ensure_control_state
    def set_brightness(self, brightness: int, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set brightness from 0-255."""
        __control_state.brightness = int(clamp(brightness, 1, 255))
        return control_id

    @property
    def flash_state(self) -> str | None:
        """
        Return flash state.

            :return: flash state, one of "short", "long", None
        """
        return self._config_state.flash_state

    @ensure_control_state
    def set_flash(self, flash: str, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """
        Set flash.

            :param flash: Can be one of "short" or "long"
        """
        __control_state.flash_state = flash
        return control_id


class CTDevice(BrightnessDevice):
    """CTDevice class."""

    # Override
    def _update_device_state(self, full_update: bool) -> None:
        """Update EntityState object."""
        super()._update_device_state(full_update)
        self._hass_state.color_temp = self._hass_state_dict.get(
            const.HASS_ATTR, {}
        ).get(const.HASS_ATTR_COLOR_TEMP)
        self._hass_state.color_mode = self._hass_state_dict.get(
            const.HASS_ATTR, {}
        ).get(const.HASS_COLOR_MODE)

    @property
    def color_mode(self) -> str:
        """Return color mode."""
        return self._config_state.color_mode or const.HASS_ATTR_COLOR_TEMP

    @property
    def min_mireds(self) -> int | None:
        """Return min_mireds from hass."""
        return self._hass_state_dict.get(const.HASS_ATTR, {}).get("min_mireds")

    @property
    def max_mireds(self) -> int | None:
        """Return max_mireds from hass."""
        return self._hass_state_dict.get(const.HASS_ATTR, {}).get("max_mireds")

    @property
    def color_temp(self) -> int:
        """Return color temp."""
        return self._config_state.color_temp or 153

    @ensure_control_state
    def set_color_temperature(self, color_temperature: int, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set color temperature."""
        __control_state.color_temp = color_temperature
        __control_state.color_mode = const.HASS_COLOR_MODE_COLOR_TEMP
        return control_id

    # Override
    def set_flash(self, flash: str, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set flash with color_temp."""
        control_id = super().set_flash(flash, control_id)
        self.set_color_temperature(self.color_temp, control_id)
        return control_id


class RGBDevice(BrightnessDevice):
    """RGBDevice class."""

    def _update_device_state(self, full_update: bool = True) -> None:
        """Update EntityState object."""
        super()._update_device_state(full_update)
        self._hass_state.hue_saturation = self._hass_state_dict.get(
            const.HASS_ATTR, {}
        ).get(const.HASS_ATTR_HS_COLOR)
        self._hass_state.xy_color = self._hass_state_dict.get(const.HASS_ATTR, {}).get(
            const.HASS_ATTR_XY_COLOR
        )
        self._hass_state.rgb_color = self._hass_state_dict.get(const.HASS_ATTR, {}).get(
            const.HASS_ATTR_RGB_COLOR
        )
        self._hass_state.color_mode = self._hass_state_dict.get(
            const.HASS_ATTR, {}
        ).get(const.HASS_COLOR_MODE)

    @property
    def color_mode(self) -> str:
        """Return color mode."""
        return self._config_state.color_mode or const.HASS_COLOR_MODE_XY

    @property
    def hue_sat(self) -> list[int]:
        """Return hue_saturation."""
        return self._config_state.hue_saturation or [0, 0]

    @ensure_control_state
    def set_hue_sat(self, hue_sat: tuple[int | float, int | float], control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set hue and saturation colors."""
        hue, sat = hue_sat
        __control_state.hue_saturation = [int(hue), int(sat)]
        __control_state.color_mode = const.HASS_COLOR_MODE_HS
        return control_id

    @property
    def xy_color(self) -> list[float]:
        """Return xy_color."""
        return self._config_state.xy_color or [0, 0]

    def set_xy(self, x_y: tuple[float, float], control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set xy colors."""
        x, y = x_y
        __control_state.xy_color = [float(x), float(y)]
        __control_state.color_mode = const.HASS_COLOR_MODE_XY
        return control_id

    @property
    def rgb_color(self) -> list[int]:
        """Return rgb_color."""
        return self._config_state.rgb_color

    @ensure_control_state
    def set_rgb(self, r_g_b: tuple[int, int, int], control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set rgb colors."""
        r, g, b = r_g_b
        __control_state.rgb_color = [int(r), int(g), int(b)]
        __control_state.color_mode = const.HASS_COLOR_MODE_RGB
        return control_id

    # Override
    def set_flash(self, flash: str, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set flash."""
        control_id = super().set_flash(flash, control_id)
        # HASS now requires a color target to be sent when flashing
        # Use white color to indicate the light
        self.set_hue_sat((self.hue_sat[0], self.hue_sat[1]), control_id)
        return control_id

    @property
    def effect(self) -> str | None:
        """Return effect."""
        return self._config_state.effect

    @ensure_control_state
    def set_effect(self, effect: str, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set effect."""
        __control_state.effect = effect
        return control_id


class RGBWDevice(CTDevice, RGBDevice):
    """RGBWDevice class."""

    def _update_device_state(self, full_update: bool = True) -> None:
        """Update EntityState object."""
        CTDevice._update_device_state(self, True)
        RGBDevice._update_device_state(self, False)

    # Override
    def set_flash(self, flash: str, control_id: int | None = None, __control_state: EntityState | None = None) -> int:
        """Set flash."""
        if self.color_mode == const.HASS_ATTR_COLOR_TEMP:
            return CTDevice.set_flash(self, flash, control_id)
        else:
            return RGBDevice.set_flash(self, flash, control_id)


async def async_get_device(
        ctrl_hass: HomeAssistantController, ctrl_config: Config, entity_id: str
) -> OnOffDevice | BrightnessDevice | CTDevice | RGBDevice | RGBWDevice:
    """Infer light object type from Home Assistant state and returns corresponding object."""
    if entity_id in __device_cache.keys():
        return __device_cache[entity_id]

    light_id: str = await ctrl_config.async_entity_id_to_light_id(entity_id)
    config: dict = await ctrl_config.async_get_light_config(light_id)

    hass_state_dict = ctrl_hass.get_entity_state(entity_id)
    entity_color_modes = hass_state_dict[const.HASS_ATTR].get(
        const.HASS_ATTR_SUPPORTED_COLOR_MODES, []
    )

    if any(
            color_mode
            in [
                const.HASS_COLOR_MODE_HS,
                const.HASS_COLOR_MODE_XY,
                const.HASS_COLOR_MODE_RGB,
                const.HASS_COLOR_MODE_RGBW,
                const.HASS_COLOR_MODE_RGBWW,
            ]
            for color_mode in entity_color_modes
    ) and any(
        color_mode
        in [
            const.HASS_COLOR_MODE_COLOR_TEMP,
            const.HASS_COLOR_MODE_RGBW,
            const.HASS_COLOR_MODE_RGBWW,
            const.HASS_COLOR_MODE_WHITE,
        ]
        for color_mode in entity_color_modes
    ):
        device_obj = RGBWDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    elif any(
            color_mode
            in [
                const.HASS_COLOR_MODE_HS,
                const.HASS_COLOR_MODE_XY,
                const.HASS_COLOR_MODE_RGB,
            ]
            for color_mode in entity_color_modes
    ):
        device_obj = RGBDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    elif const.HASS_COLOR_MODE_COLOR_TEMP in entity_color_modes:
        device_obj = CTDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    elif const.HASS_COLOR_MODE_BRIGHTNESS in entity_color_modes:
        device_obj = BrightnessDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    else:
        device_obj = OnOffDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    await device_obj.async_update_state()
    # Pull device state from Home Assistant every 5 seconds
    add_scheduler(device_obj.async_update_state, 5000)
    __device_cache[entity_id] = device_obj
    return device_obj
