import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import switchbot_scan as sb  # noqa: E402

UUID = sb.SWITCHBOT_SERVICE_UUIDS[0]
MAC = bytes.fromhex("aabbccddeeff")


def service(battery: int, temp_dec: int = 0, temp_int: int = 0, humidity: int = 0, above_freezing: bool = True,
            model: str = "T") -> dict:
    """[0]機種 [1]flags [2]battery [3]温度小数 [4]温度整数（最上位bit=1 で0℃以上） [5]湿度"""
    return {UUID: bytes([ord(model), 0x00, battery, temp_dec, (0x80 if above_freezing else 0) | temp_int, humidity])}


def manufacturer(temp_dec: int, temp_int: int, humidity: int, above_freezing: bool = True, co2: int | None = None) -> dict:
    """MAC(6byte) + [0][1]その他 [2]温度小数 [3]温度整数 [4]湿度 (+ [5]〜[6]その他 [7][8]CO2)"""
    payload = bytes([0x00, 0x00, temp_dec, (0x80 if above_freezing else 0) | temp_int, humidity])
    if co2 is not None:
        payload += bytes([0x00, 0x00]) + co2.to_bytes(2, "big")
    return {sb.SWITCHBOT_COMPANY_ID: MAC + payload}


def test_old_device():
    r = sb.parse_meter(service(battery=87, temp_dec=4, temp_int=23, humidity=55), {})
    assert (r.battery, r.temperature, r.humidity) == (87, 23.4, pytest.approx(0.55))


def test_old_device_below_freezing():
    r = sb.parse_meter(service(battery=50, temp_dec=3, temp_int=2, humidity=80, above_freezing=False), {})
    assert r.temperature == -2.3


def test_old_device_legacy_uuid():
    data = service(battery=10, temp_dec=1, temp_int=20, humidity=40)[UUID]
    r = sb.parse_meter({sb.SWITCHBOT_SERVICE_UUIDS[1]: data}, {})
    assert r.temperature == 20.1


def test_new_device():
    r = sb.parse_meter(service(battery=99, model="w"), manufacturer(7, 12, 63))
    assert (r.battery, r.temperature, r.humidity) == (99, 12.7, pytest.approx(0.63))


def test_new_device_below_freezing():
    r = sb.parse_meter({}, manufacturer(5, 4, 40, above_freezing=False))
    assert r.temperature == -4.5 and r.battery is None


def test_unrelated_data_is_ignored():
    r = sb.parse_meter({"0000180f-0000-1000-8000-00805f9b34fb": b"\x01"}, {0x004C: b"\x02\x15"})
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
    assert devices


def test_legacy_type_is_accepted(tmp_path):
    f = tmp_path / "devices.toml"
    f.write_text('[[devices]]\nmac = "aa:bb:cc:dd:ee:01"\nname = "x"\ntype = "old"\n', encoding="utf-8")
    assert list(sb.load_devices(f)) == ["AA:BB:CC:DD:EE:01"]


def test_manufacturer_data_takes_precedence():
    """両方に温湿度があるときは Manufacturer Data を使う（pySwitchbot と同じ）"""
    r = sb.parse_meter(service(battery=70, temp_dec=0, temp_int=10, humidity=10), manufacturer(5, 25, 60))
    assert (r.temperature, r.humidity) == (25.5, pytest.approx(0.6))


def test_fahrenheit_flag_is_ignored():
    """湿度バイトの最上位bit（本体の表示単位が華氏）は湿度に含めない"""
    r = sb.parse_meter({}, manufacturer(0, 20, 0x80 | 45))
    assert (r.temperature, r.humidity) == (20.0, pytest.approx(0.45))


def test_detect_model():
    assert sb.detect_model(service(battery=1, model="i")) == "i"
    assert sb.detect_model(service(battery=1, model="H")) is None  # 温湿度計以外（Bot など）
    assert sb.detect_model({}) is None


def test_meter_pro_co2():
    r = sb.parse_meter(service(battery=90, model="5"), manufacturer(3, 22, 50, co2=812))
    assert r.co2 == 812
    p = sb.build_point(sb.Device(mac="AA:BB:CC:DD:EE:03", name="書斎"), r, datetime.now(timezone.utc))
    assert p["fields"]["co2"] == 812 and isinstance(p["fields"]["co2"], int)


def test_co2_needs_model_and_range():
    assert sb.parse_meter(service(battery=90, model="4"), manufacturer(3, 22, 50, co2=812)).co2 is None
    assert sb.parse_meter(service(battery=90, model="5"), manufacturer(3, 22, 50, co2=0xFFFF)).co2 is None


def test_co2_uses_cached_model_for_manufacturer_only_packet():
    dev = sb.Device(mac="AA:BB:CC:DD:EE:03", name="書斎")
    latest = sb.LatestReadings({dev.mac: dev})
    latest.update(dev.mac, service(battery=90, model="5"), {})
    latest.update(dev.mac, {}, manufacturer(3, 22, 50, co2=640))
    (point,) = latest.points(stale_seconds=60)
    assert point["fields"]["co2"] == 640


def test_humidity_has_no_float_noise():
    r = sb.parse_meter(service(battery=1, humidity=55), {})
    assert r.humidity == 0.55
