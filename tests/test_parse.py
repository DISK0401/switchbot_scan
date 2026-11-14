import binascii
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import switchbot_scan as sb  # noqa: E402


def service_data(battery: int, temp_dec: int, temp_int: int, humidity: int, above_freezing: bool = True) -> tuple:
    """旧型: UUID(2byte) + [0]flags [1]flags [2]battery [3]温度小数 [4]温度整数(最上位bit=0以上) [5]湿度"""
    payload = bytes([0x54, 0x00, battery, temp_dec, (0x80 if above_freezing else 0) | temp_int, humidity])
    return (sb.AD_TYPE_SERVICE_DATA, "16b Service Data", "3dfd" + binascii.hexlify(payload).decode())


def manufacturer_data(temp_dec: int, temp_int: int, humidity: int, above_freezing: bool = True) -> tuple:
    """新型: 会社ID+MAC(8byte) + [0][1]その他 [2]温度小数 [3]温度整数 [4]湿度"""
    payload = bytes([0x00, 0x00, temp_dec, (0x80 if above_freezing else 0) | temp_int, humidity])
    return (sb.AD_TYPE_MANUFACTURER, "Manufacturer", "6909aabbccddeeff" + binascii.hexlify(payload).decode())


def test_old_device():
    r = sb.parse_old_device([service_data(battery=87, temp_dec=4, temp_int=23, humidity=55)])
    assert (r.battery, r.temperature, r.humidity) == (87, 23.4, pytest.approx(0.55))


def test_old_device_below_freezing():
    r = sb.parse_old_device([service_data(battery=50, temp_dec=3, temp_int=2, humidity=80, above_freezing=False)])
    assert r.temperature == -2.3


def test_new_device_any_order():
    # 旧実装は Service Data が先に来ると温湿度を読まずに return していた（不具合）
    for order in ((0, 1), (1, 0)):
        items = [service_data(battery=99, temp_dec=0, temp_int=0, humidity=0), manufacturer_data(7, 12, 63)]
        r = sb.parse_new_device([items[i] for i in order])
        assert (r.battery, r.temperature, r.humidity) == (99, 12.7, pytest.approx(0.63))


def test_new_device_below_freezing():
    r = sb.parse_new_device([manufacturer_data(5, 4, 40, above_freezing=False)])
    assert r.temperature == -4.5 and r.battery is None


def test_build_point_skips_missing_fields():
    dev = sb.Device(mac="AA:BB:CC:DD:EE:02", name="屋外", type="new")
    p = sb.build_point(dev, sb.Reading(temperature=10.0, humidity=0.5), datetime.now(timezone.utc))
    assert p["measurement"] == "switchbot_smartmeter"
    assert p["tags"] == {"mac_address": "AA:BB:CC:DD:EE:02", "device_name": "屋外"}
    assert p["fields"] == {"temperature": 10.0, "humidity": 0.5}  # 取得できなかった battery は記録しない


def test_example_devices_file_is_valid():
    devices = sb.load_devices(Path(__file__).resolve().parents[1] / "devices.example.toml")
    assert {d.type for d in devices.values()} <= {"old", "new"}
