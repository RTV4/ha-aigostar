"""
Light platform for Aigostar — multi-device support.

Supported TSL properties (TG7100C):
  - LightSwitch      bool  0/1
  - Brightness       int   1-100
  - ColorTemperature int   0-100  (0=warm 2700K, 100=cool 6500K)
  - LightMode        enum  0=white 1=color
  - HSVColor         struct {Hue:0-360, Saturation:0-100, Value:0-100}
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .alibaba_api import AlibabaIoTClient
from .const import (
    AIGO_BRIGHT_MAX,
    AIGO_BRIGHT_MIN,
    AIGO_HUE_MAX,
    AIGO_SAT_MAX,
    COLOR_MODEL_HINTS,
    DOMAIN,
    HA_BRIGHT_MAX,
    HSV_KEY_HUE,
    HSV_KEY_SATURATION,
    HSV_KEY_VALUE,
    KELVIN_COOL,
    KELVIN_WARM,
    LIGHT_MODE_COLOR,
    LIGHT_MODE_WHITE,
    PROP_BRIGHTNESS,
    PROP_COLOR_TEMP,
    PROP_HSV_COLOR,
    PROP_LIGHT_MODE,
    PROP_SWITCH,
    SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=SCAN_INTERVAL_SECONDS)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    devices = entry_data["devices"]
    iot_token = entry_data["iot_token"]
    app_key = entry_data["app_key"]
    app_secret = entry_data["app_secret"]

    entities = []
    for dev in devices:
        iot_id = dev.get("iotId", "")
        nick = dev.get("nickName") or dev.get("deviceName") or iot_id[:12]
        status = dev.get("status", 0)

        client = AlibabaIoTClient(
            iot_id=iot_id,
            iot_token=iot_token,
            app_key=app_key,
            app_secret=app_secret,
        )
        entities.append(AigostarLight(client, iot_id, nick, online=(status == 1), raw_device=dev))

    # Register entities for token refresh
    hass.data.setdefault(f"{DOMAIN}_entities", {})
    hass.data[f"{DOMAIN}_entities"][entry.entry_id] = entities

    _LOGGER.info("Aigostar: creating %d light entities", len(entities))
    async_add_entities(entities, update_before_add=True)


class AigostarLight(LightEntity):
    """Aigostar smart bulb."""

    _attr_has_entity_name = True
    _attr_name = None  # Use device name as entity name
    _attr_min_color_temp_kelvin = KELVIN_WARM
    _attr_max_color_temp_kelvin = KELVIN_COOL

    def __init__(
        self, client: AlibabaIoTClient, iot_id: str, name: str,
        online: bool, raw_device: dict | None = None,
    ) -> None:
        self._client = client
        self._attr_unique_id = iot_id

        raw = raw_device or {}
        product_name = raw.get("productName") or raw.get("categoryName") or "Smart Bulb"
        fw_version = raw.get("firmwareVersion") or raw.get("moduleVersion") or None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, iot_id)},
            name=name,
            manufacturer="Aigostar",
            model=product_name,
            sw_version=fw_version,
        )

        self._is_on:        bool = False
        self._brightness:   int  = 255
        self._color_temp_k: int  = 4000
        self._color_mode:   ColorMode = ColorMode.COLOR_TEMP
        self._hs_color:     tuple[float, float] = (0.0, 0.0)
        self._available:    bool = online

        # Colour capability is per device: a CCT-only bulb must not show a
        # colour wheel in the UI. Start white-only (the historical behaviour)
        # and upgrade as soon as the device proves it supports colour.
        self._model_hint = " ".join(
            str(raw.get(key) or "")
            for key in ("productName", "productKey", "categoryName")
        ).lower()
        self._color_support: bool | None = None
        self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if any(hint in self._model_hint for hint in COLOR_MODEL_HINTS):
            # Optimistic guess from the product metadata; the first poll either
            # confirms it or corrects it (the properties are the real proof).
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.HS}

    def update_token(self, new_token: str) -> None:
        """Update the iotToken after a refresh."""
        self._client.iot_token = new_token

    # ------------------------------------------------------------------
    # Colour capability
    # ------------------------------------------------------------------

    @property
    def _supports_hs(self) -> bool:
        return ColorMode.HS in (self._attr_supported_color_modes or ())

    def _set_color_support(self, supports_color: bool) -> None:
        """Latch the detected capability into the supported colour modes."""
        if self._color_support == supports_color:
            return
        self._color_support = supports_color
        modes = {ColorMode.COLOR_TEMP}
        if supports_color:
            modes.add(ColorMode.HS)
        self._attr_supported_color_modes = modes
        _LOGGER.debug(
            "Aigostar [%s] supported color modes: %s", self._attr_unique_id, modes,
        )

    def _detect_color_support(self, props: dict) -> bool | None:
        """
        Decide from a property snapshot whether the bulb can do colour.

        Returns None when the snapshot is inconclusive, so the caller keeps the
        safe COLOR_TEMP-only default. LightMode on its own is not a positive
        signal — white-only TG7100C bulbs expose it too — but a snapshot that
        carries it without HSVColor is solid proof of a white-only bulb.
        """
        if PROP_HSV_COLOR in props:
            return True
        if PROP_LIGHT_MODE in props or (PROP_SWITCH in props and PROP_BRIGHTNESS in props):
            return False
        return None

    # ------------------------------------------------------------------
    # Conversions
    # ------------------------------------------------------------------

    @staticmethod
    def _aigo_to_ha_brightness(v: int) -> int:
        v = max(AIGO_BRIGHT_MIN, min(AIGO_BRIGHT_MAX, v))
        pct = (v - AIGO_BRIGHT_MIN) / (AIGO_BRIGHT_MAX - AIGO_BRIGHT_MIN)
        return max(1, round(pct * HA_BRIGHT_MAX))

    @staticmethod
    def _ha_to_aigo_brightness(v: int) -> int:
        """Exact inverse of _aigo_to_ha_brightness, so a round-trip is stable."""
        pct = max(0, min(HA_BRIGHT_MAX, v)) / HA_BRIGHT_MAX
        return AIGO_BRIGHT_MIN + round(pct * (AIGO_BRIGHT_MAX - AIGO_BRIGHT_MIN))

    @staticmethod
    def _aigo_to_kelvin(v: int) -> int:
        """0 = warm 2700K, 100 = cool 6500K."""
        pct = v / 100.0
        return round(KELVIN_WARM + pct * (KELVIN_COOL - KELVIN_WARM))

    @staticmethod
    def _kelvin_to_aigo(k: int) -> int:
        pct = (k - KELVIN_WARM) / (KELVIN_COOL - KELVIN_WARM)
        return max(0, min(100, round(pct * 100)))

    # ------------------------------------------------------------------
    # HA properties
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int:
        return self._color_temp_k

    @property
    def color_mode(self) -> ColorMode:
        return self._color_mode

    @property
    def hs_color(self) -> tuple[float, float]:
        return self._hs_color

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # Update (polling)
    # ------------------------------------------------------------------

    def _to_number(self, key: str, value: Any) -> float | None:
        """Cloud values may be strings, None or garbage — never let that raise."""
        try:
            return float(value)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Aigostar [%s] ignoring non-numeric %s: %r",
                self._attr_unique_id, key, value,
            )
            return None

    def _parse_hsv(self, value: Any) -> dict | None:
        """HSVColor arrives either as a struct or as a JSON-encoded string."""
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                _LOGGER.debug(
                    "Aigostar [%s] cannot decode %s: %r",
                    self._attr_unique_id, PROP_HSV_COLOR, value,
                )
                return None
            if isinstance(decoded, dict):
                return decoded
        if value is not None:
            _LOGGER.debug(
                "Aigostar [%s] unexpected %s payload: %r",
                self._attr_unique_id, PROP_HSV_COLOR, value,
            )
        return None

    def _apply_props(self, props: dict) -> None:
        _LOGGER.debug("Aigostar [%s] props: %s", self._attr_unique_id, props)

        # Entities are added with update_before_add=True, so the first poll
        # lands here before HA reads supported_color_modes. Never downgrade a
        # bulb that already proved it can do colour.
        if self._color_support is not True:
            support = self._detect_color_support(props)
            if support is not None:
                self._set_color_support(support)

        if PROP_SWITCH in props:
            self._is_on = bool(props[PROP_SWITCH])

        # Follow the reported mode only when the key is really there: a partial
        # response must not silently push the bulb back into white mode.
        if PROP_LIGHT_MODE in props:
            mode = self._to_number(PROP_LIGHT_MODE, props[PROP_LIGHT_MODE])
            if mode is not None:
                self._color_mode = (
                    ColorMode.HS
                    if int(mode) == LIGHT_MODE_COLOR and self._supports_hs
                    else ColorMode.COLOR_TEMP
                )

        hsv = self._parse_hsv(props.get(PROP_HSV_COLOR))
        hsv_value: float | None = None
        if hsv is not None:
            hue = self._to_number(HSV_KEY_HUE, hsv.get(HSV_KEY_HUE))
            saturation = self._to_number(HSV_KEY_SATURATION, hsv.get(HSV_KEY_SATURATION))
            if hue is not None and saturation is not None:
                self._hs_color = (
                    max(0.0, min(float(AIGO_HUE_MAX), hue)),
                    max(0.0, min(float(AIGO_SAT_MAX), saturation)),
                )
            hsv_value = self._to_number(HSV_KEY_VALUE, hsv.get(HSV_KEY_VALUE))

        if self._color_mode == ColorMode.HS and hsv_value is not None:
            self._brightness = self._aigo_to_ha_brightness(int(hsv_value))
        elif PROP_BRIGHTNESS in props:
            brightness = self._to_number(PROP_BRIGHTNESS, props[PROP_BRIGHTNESS])
            if brightness is not None:
                self._brightness = self._aigo_to_ha_brightness(int(brightness))
        if PROP_COLOR_TEMP in props:
            color_temp = self._to_number(PROP_COLOR_TEMP, props[PROP_COLOR_TEMP])
            if color_temp is not None:
                self._color_temp_k = self._aigo_to_kelvin(int(color_temp))

    def update(self) -> None:
        try:
            props = self._client.get_properties_sync()
            self._apply_props(props)
            self._available = True
        except Exception as exc:
            _LOGGER.warning("Aigostar [%s] update failed: %s", self._attr_unique_id, exc)
            self._available = False

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _light_mode_value(self) -> int:
        """LightMode matching the colour mode we currently believe in."""
        return LIGHT_MODE_COLOR if self._color_mode == ColorMode.HS else LIGHT_MODE_WHITE

    def _hsv_item(self, hue: float, saturation: float, ha_brightness: int) -> dict[str, int]:
        return {
            HSV_KEY_HUE:        max(0, min(AIGO_HUE_MAX, round(hue))),
            HSV_KEY_SATURATION: max(0, min(AIGO_SAT_MAX, round(saturation))),
            HSV_KEY_VALUE:      self._ha_to_aigo_brightness(ha_brightness),
        }

    def _write_state(self) -> None:
        """Publish the optimistic state so the UI does not snap back."""
        if self.hass is not None and self.entity_id:
            self.schedule_update_ha_state()

    def turn_on(self, **kwargs: Any) -> None:
        try:
            items: dict[str, Any] = {PROP_SWITCH: 1}
            requested_brightness = kwargs.get(ATTR_BRIGHTNESS)
            requested_hs = kwargs.get(ATTR_HS_COLOR)
            requested_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            ha_b = (
                int(requested_brightness) if requested_brightness is not None
                else self._brightness
            )

            # The bulb holds a single LightMode, so colour temperature and
            # colour are mutually exclusive. When HA sends both in one call
            # colour temperature wins and the HS part is dropped; sending both
            # property sets would leave the result up to the firmware.
            if requested_kelvin is not None:
                k = int(requested_kelvin)
                items[PROP_COLOR_TEMP] = self._kelvin_to_aigo(k)
                items[PROP_LIGHT_MODE] = LIGHT_MODE_WHITE
                if requested_brightness is not None:
                    items[PROP_BRIGHTNESS] = self._ha_to_aigo_brightness(ha_b)
                self._color_temp_k = k
                self._color_mode = ColorMode.COLOR_TEMP

            elif requested_hs is not None:
                hue, saturation = requested_hs
                items[PROP_HSV_COLOR] = self._hsv_item(float(hue), float(saturation), ha_b)
                items[PROP_LIGHT_MODE] = LIGHT_MODE_COLOR
                self._hs_color = (float(hue), float(saturation))
                self._color_mode = ColorMode.HS

            elif requested_brightness is not None:
                if self._color_mode == ColorMode.HS and self._hs_color[1] > 0:
                    items[PROP_HSV_COLOR] = self._hsv_item(self._hs_color[0], self._hs_color[1], ha_b)
                    items[PROP_LIGHT_MODE] = LIGHT_MODE_COLOR
                else:
                    # White mode, or colour mode with a colour we never learned
                    # (saturation 0 would wash the bulb out): drive the plain
                    # Brightness property and leave LightMode untouched.
                    items[PROP_BRIGHTNESS] = self._ha_to_aigo_brightness(ha_b)

            else:
                # Plain switch-on: re-assert the mode we believe the bulb is in,
                # otherwise it returns in whatever mode the cloud kept and the
                # HA state drifts away from reality.
                items[PROP_LIGHT_MODE] = self._light_mode_value()

            if requested_brightness is not None:
                self._brightness = ha_b

            _LOGGER.debug("Aigostar [%s] turn_on items: %s", self._attr_unique_id, items)
            self._client.set_properties_sync(items)
            self._is_on     = True
            self._available = True

        except Exception as exc:
            _LOGGER.error("Aigostar turn_on failed [%s]: %s", self._attr_unique_id, exc)
            self._available = False
        finally:
            self._write_state()

    def turn_off(self, **kwargs: Any) -> None:
        try:
            items = {PROP_SWITCH: 0}
            _LOGGER.debug("Aigostar [%s] turn_off items: %s", self._attr_unique_id, items)
            self._client.set_properties_sync(items)
            self._is_on     = False
            self._available = True
        except Exception as exc:
            _LOGGER.error("Aigostar turn_off failed [%s]: %s", self._attr_unique_id, exc)
            self._available = False
        finally:
            self._write_state()
