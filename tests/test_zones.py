import asyncio
import base64
import unittest
from unittest.mock import Mock

from govee_local_api.controller import GoveeController
from govee_local_api.device import GoveeDevice, SegmentUpdate, ZoneUpdate
from govee_local_api.light_capabilities import GOVEE_LIGHT_CAPABILITIES
from govee_local_api.message import (
    kelvin_to_rgb,
    zone_brightness_frame,
    zone_color_frame,
    zone_flow_frame,
    zone_power_frame,
    zone_temperature_frame,
)

RIPPLE, SIDE, LOWER = 1, 2, 3


def hex_of(frame: bytes) -> str:
    """Frame bytes up to the last non-zero byte (frames are zero-padded to 19)."""
    assert len(frame) == 19
    return frame.rstrip(b"\x00").hex(" ")


class TestZoneFrames(unittest.TestCase):
    """Frames as verified on H60B0 hardware."""

    def test_power(self):
        assert hex_of(zone_power_frame(LOWER, True)) == "33 30 03 01"
        assert hex_of(zone_power_frame(SIDE, False)) == "33 30 02"

    def test_colour_without_and_with_segment_mask(self):
        assert hex_of(zone_color_frame(RIPPLE, (255, 0, 0))) == "33 05 2c 01 01 ff"
        assert (
            hex_of(zone_color_frame(SIDE, (0, 0, 255), 0xFF))
            == "33 05 2c 02 01 00 00 ff 00 00 ff"
        )

    def test_temperature_sends_rgb_approximation_for_colour_zones(self):
        r, g, b = kelvin_to_rgb(4000)
        assert (r, g, b) == (255, 205, 166)
        assert (
            hex_of(zone_temperature_frame(RIPPLE, 4000))
            == "33 05 2c 01 01 ff cd a6 0f a0"
        )

    def test_temperature_without_colour_bytes_for_white_only_zones(self):
        assert (
            hex_of(zone_temperature_frame(LOWER, 3000, rgb=False))
            == "33 05 2c 03 01 0b b8"
        )

    def test_brightness_and_flow(self):
        assert hex_of(zone_brightness_frame(SIDE, 50, 0x05)) == "33 05 2c 02 02 32 05"
        assert hex_of(zone_brightness_frame(LOWER, 150)) == "33 05 2c 03 02 64"
        assert hex_of(zone_flow_frame(RIPPLE, 20)) == "33 05 2c 01 03 14"


class ZoneControllerTest(unittest.TestCase):
    def setUp(self):
        self.controller = GoveeController.__new__(GoveeController)
        self.controller._send_message = Mock()
        self.controller._logger = Mock()
        self.device = GoveeDevice(
            self.controller, "192.168.1.100", "AA:BB", "H60B0",
            GOVEE_LIGHT_CAPABILITIES["H60B0"],
        )  # fmt: skip
        self.device.set_power_state(True)

    def sent(self) -> list[str]:
        """Every frame sent, without padding or checksum ("turn 1" for power-on)."""
        out = []
        for call in self.controller._send_message.call_args_list:
            msg = call[0][0].as_dict()["msg"]
            if msg["cmd"] != "ptReal":
                out.append(f"{msg['cmd']} {msg['data']['value']}")
                continue
            out += [
                base64.b64decode(c)[:-1].rstrip(b"\x00").hex(" ")
                for c in msg["data"]["command"]
            ]
        return out

    def zones(self, *updates: ZoneUpdate) -> list[str]:
        self.controller._send_message.reset_mock()
        asyncio.run(self.device.set_zones(list(updates)))
        return self.sent()


class TestSetZones(ZoneControllerTest):
    def test_h60b0_zones(self):
        caps = GOVEE_LIGHT_CAPABILITIES["H60B0"]
        assert [z.name for z in caps.zones] == ["ripple", "side", "lower"]
        assert caps.max_zones_on == 2
        assert tuple(caps.temperature_range) == (2700, 6500)

    def test_switch_on_with_settings(self):
        assert self.zones(
            ZoneUpdate(SIDE, on=True, rgb=(0, 0, 255), brightness=40)
        ) == [
            "33 30 02 01",
            "33 05 2c 02 01 00 00 ff 00 00 ff",
            "33 05 2c 02 02 28 ff",
        ]
        assert self.device.zones_on == (SIDE,)

    def test_third_zone_switches_off_the_longest_lit(self):
        self.zones(ZoneUpdate(RIPPLE, on=True))
        self.zones(ZoneUpdate(SIDE, on=True))
        assert self.zones(ZoneUpdate(LOWER, on=True)) == ["33 30 01", "33 30 03 01"]
        assert self.device.zones_on == (SIDE, LOWER)

    def test_zones_switched_on_together_are_kept(self):
        self.zones(ZoneUpdate(RIPPLE, on=True))
        self.zones(ZoneUpdate(SIDE, on=True))
        # Ripple is the longest lit, but it's switched on again in this call
        sent = self.zones(ZoneUpdate(RIPPLE, on=True), ZoneUpdate(LOWER, on=True))
        assert sent == ["33 30 01 01", "33 30 02", "33 30 03 01"]
        assert self.device.zones_on == (RIPPLE, LOWER)

    def test_switching_off_goes_first_and_frees_a_slot(self):
        self.zones(ZoneUpdate(RIPPLE, on=True))
        self.zones(ZoneUpdate(SIDE, on=True))
        sent = self.zones(ZoneUpdate(LOWER, on=True), ZoneUpdate(SIDE, on=False))
        assert sent == ["33 30 02", "33 30 03 01"]
        assert self.device.zones_on == (RIPPLE, LOWER)

    def test_switching_off_the_last_zone_powers_the_light_off(self):
        self.zones(ZoneUpdate(RIPPLE, on=True))
        self.zones(ZoneUpdate(LOWER, on=True))
        self.zones(ZoneUpdate(RIPPLE, on=False), ZoneUpdate(LOWER, on=False))
        assert not self.device.on
        # The light comes back with the zone switched off last
        assert self.device.zones_on == (LOWER,)
        assert not self.device.zone_on(RIPPLE)

    def test_zone_switched_on_while_off_powers_on_with_only_that_zone(self):
        self.zones(ZoneUpdate(SIDE, on=True))
        self.zones(ZoneUpdate(SIDE, on=False))
        assert not self.device.on
        assert self.zones(ZoneUpdate(RIPPLE, on=True, rgb=(255, 0, 0))) == [
            "turn 1",
            "33 30 02",
            "33 30 03",
            "33 30 01 01",
            "33 05 2c 01 01 ff",
        ]
        assert self.device.on
        assert self.device.zones_on == (RIPPLE,)

    def test_power_on_lights_the_zones_lit_before(self):
        self.zones(ZoneUpdate(LOWER, on=True))
        self.zones(ZoneUpdate(RIPPLE, on=True))
        self.device.set_power_state(False)  # e.g. switched off as a whole
        self.controller._send_message.reset_mock()
        asyncio.run(self.device.turn_on())
        assert self.sent() == ["turn 1", "33 30 02", "33 30 03 01", "33 30 01 01"]
        assert self.device.zones_on == (LOWER, RIPPLE)

    def test_power_on_without_known_zones_leaves_the_light_alone(self):
        self.device.set_power_state(False)
        asyncio.run(self.device.turn_on())
        assert self.sent() == ["turn 1"]

    def test_zone_lit_after_power_on_counts_towards_the_limit(self):
        self.zones(ZoneUpdate(LOWER, on=True))
        self.zones(ZoneUpdate(LOWER, on=False))
        # Powered back on with Lower lit; Ripple and Side make three
        asyncio.run(self.device.turn_on())
        self.zones(ZoneUpdate(RIPPLE, on=True))
        assert self.zones(ZoneUpdate(SIDE, on=True)) == ["33 30 03", "33 30 02 01"]

    def test_restored_state_counts_towards_the_limit(self):
        self.device.set_zone_state(LOWER, True)
        self.device.set_zone_state(RIPPLE, True)
        assert self.zones(ZoneUpdate(SIDE, on=True)) == ["33 30 03", "33 30 02 01"]

    def test_temperature_is_clamped_to_the_lamp_range(self):
        assert self.zones(ZoneUpdate(LOWER, temperature=9000)) == [
            "33 05 2c 03 01 19 64"
        ]

    def test_colour_is_never_sent_to_a_white_only_zone(self):
        assert self.zones(ZoneUpdate(LOWER, rgb=(255, 0, 0))) == []
        self.controller._logger.warning.assert_called_once()

    def test_flow_only_on_zones_that_have_it(self):
        assert self.zones(ZoneUpdate(RIPPLE, flow=30)) == ["33 05 2c 01 03 1e"]
        assert self.zones(ZoneUpdate(SIDE, flow=30)) == []

    def test_invalid_zone_is_skipped(self):
        assert self.zones(ZoneUpdate(4, on=True), ZoneUpdate(RIPPLE, on=True)) == [
            "33 30 01 01"
        ]

    def test_device_without_zones_sends_nothing(self):
        plain = GoveeDevice(
            self.controller, "192.168.1.101", "CC:DD", "H6088",
            GOVEE_LIGHT_CAPABILITIES["H6088"],
        )  # fmt: skip
        asyncio.run(plain.set_zones([ZoneUpdate(1, on=True)]))
        self.controller._send_message.assert_not_called()


class TestSetZoneSegments(ZoneControllerTest):
    def segments(self, zone: int, *updates: SegmentUpdate) -> list[str]:
        self.controller._send_message.reset_mock()
        asyncio.run(self.device.set_zone_segments(zone, list(updates)))
        return self.sent()

    def test_shared_settings_are_merged(self):
        updates = [
            SegmentUpdate(i, rgb=(255, 0, 0) if i <= 4 else (0, 0, 255), brightness=100)
            for i in range(1, 9)
        ]
        assert self.segments(SIDE, *updates) == [
            "33 05 2c 02 01 ff 00 00 00 00 0f",
            "33 05 2c 02 01 00 00 ff 00 00 f0",
            "33 05 2c 02 02 64 ff",
        ]

    def test_segment_temperature(self):
        assert self.segments(SIDE, SegmentUpdate(3, temperature=2700)) == [
            "33 05 2c 02 01 ff a6 57 0a 8c 04"
        ]

    def test_invalid_segments_and_zones(self):
        assert self.segments(SIDE, SegmentUpdate(9, rgb=(1, 1, 1))) == []
        assert self.segments(RIPPLE, SegmentUpdate(1, rgb=(1, 1, 1))) == []
