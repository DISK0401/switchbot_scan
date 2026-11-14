import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import switchbot_scan as sb  # noqa: E402

UUID = sb.SWITCHBOT_SERVICE_UUIDS[0]
MAC = bytes.fromhex("aabbccddeeff")


def service(battery: int, temp_dec: int = 0, temp_int: int = 0, humidity: int = 0, above_freezing: bool = True) -> dict:
    """[0]flags [1]flags [2]battery [3]温度小数 [4]温度整数（最上位bit=1 で0℃以上） [5]湿度"""
    return {UUID: bytes([0x54, 0x00, battery, temp_dec, (0x80 if above_freezing else 0) | temp_int, humidity])}


def manufacturer(temp_dec: int, temp_int: int, humidity: int, above_freezing: bool = True) -> dict:
    """MAC(6byte) + [0][1]その他 [2]温度小数 [3]温度整数 [4]湿度"""
    payload = bytes([0x00, 0x00, temp_dec, (0x80 if above_freezing else 0) | temp_int, humidity])
    return {sb.SWITCHBOT_COMPANY_ID: MAC + payload}


def test_old_device():
    r = sb.parse_old_device(service(battery=87, temp_dec=4, temp_int=23, humidity=55), {})
    assert (r.battery, r.temperature, r.humidity) == (87, 23.4, pytest.approx(0.55))


def test_old_device_below_freezing():
    r = sb.parse_old_device(service(battery=50, temp_dec=3, temp_int=2, humidity=80, above_freezing=False), {})
    assert r.temperature == -2.3


def test_old_device_legacy_uuid():
    data = service(battery=10, temp_dec=1, temp_int=20, humidity=40)[UUID]
    r = sb.parse_old_device({sb.SWITCHBOT_SERVICE_UUIDS[1]: data}, {})
    assert r.temperature == 20.1


def test_new_device():
    r = sb.parse_new_device(service(battery=99), manufacturer(7, 12, 63))
    assert (r.battery, r.temperature, r.humidity) == (99, 12.7, pytest.approx(0.63))


def test_new_device_below_freezing():
    r = sb.parse_new_device({}, manufacturer(5, 4, 40, above_freezing=False))
    assert r.temperature == -4.5 and r.battery is None


def test_unrelated_data_is_ignored():
    r = sb.parse_old_device({"0000180f-0000-1000-8000-00805f9b34fb": b"\x01"}, {0x004C: b"\x02\x15"})
    assert not r.has_any()


def test_latest_readings_merge_and_stale():
    dev = sb.Device(mac="AA:BB:CC:DD:EE:02", name="屋外", type="new")
    latest = sb.LatestReadings({dev.mac: dev})
    latest.update("aa:bb:cc:dd:ee:02", service(battery=80), {})          # 電池だけのパケット
    latest.update("AA:BB:CC:DD:EE:02", {}, manufacturer(0, 15, 50))      # 温湿度だけのパケット
    latest.update("11:22:33:44:55:66", service(battery=1), {})           # 対象外のデバイス
    (point,) = latest.points(stale_seconds=60)
    assert point["fields"] == {"temperature": 15.0, "humidity": pytest.approx(0.5), "battery": 80}
    assert latest.points(stale_seconds=-1) == []


def test_build_point_format():
    dev = sb.Device(mac="AA:BB:CC:DD:EE:01", name="リビング", type="old")
    p = sb.build_point(dev, sb.Reading(battery=90, temperature=21.0, humidity=0.45), datetime.now(timezone.utc))
    assert p["measurement"] == "switchbot_smartmeter"
    assert p["tags"] == {"mac_address": "AA:BB:CC:DD:EE:01", "device_name": "リビング"}
    assert p["fields"] == {"temperature": 21.0, "humidity": 0.45, "battery": 90}
    assert isinstance(p["fields"]["temperature"], float) and isinstance(p["fields"]["battery"], int)


def test_example_devices_file_is_valid():
    devices = sb.load_devices(Path(__file__).resolve().parents[1] / "devices.example.toml")
    assert {d.type for d in devices.values()} <= {"old", "new"}
