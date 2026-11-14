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


# --- 未登録のデバイスと名前 ---------------------------------------------------
def test_unknown_meter_is_recorded_with_mac_name():
    latest = sb.LatestReadings({}, record_unknown=True)
    latest.update("aa:bb:cc:dd:ee:09", service(battery=80, model="i"), manufacturer(1, 18, 40), rssi=-70)
    (point,) = latest.points(stale_seconds=60)
    assert point["tags"] == {"mac_address": "AA:BB:CC:DD:EE:09", "device_name": "AA:BB:CC:DD:EE:09"}
    assert point["fields"]["rssi"] == -70


def test_unknown_non_meter_is_ignored():
    """プラグなど温湿度計以外の機器のデータを温度として読まない"""
    latest = sb.LatestReadings({}, record_unknown=True)
    plug = {UUID: bytes([ord("j"), 0x00, 0x80])}
    latest.update("AA:BB:CC:DD:EE:0A", plug, manufacturer(1, 18, 40))
    assert latest.points(stale_seconds=60) == []


def test_unknown_meter_needs_model_before_manufacturer_only_packet():
    latest = sb.LatestReadings({}, record_unknown=True)
    latest.update("AA:BB:CC:DD:EE:0B", {}, manufacturer(1, 18, 40))  # 機種がまだ分からない
    assert latest.points(stale_seconds=60) == []
    latest.update("AA:BB:CC:DD:EE:0B", service(battery=50, model="w"), {})
    latest.update("AA:BB:CC:DD:EE:0B", {}, manufacturer(1, 18, 40))
    (point,) = latest.points(stale_seconds=60)
    assert point["fields"]["temperature"] == 18.1


def test_unknown_is_ignored_when_disabled():
    latest = sb.LatestReadings({}, record_unknown=False)
    latest.update("AA:BB:CC:DD:EE:0C", service(battery=50, temp_int=20, humidity=40), {})
    assert latest.points(stale_seconds=60) == []


def test_name_priority():
    dev = sb.Device(mac="AA:BB:CC:DD:EE:01", name="toml の名前")
    latest = sb.LatestReadings({dev.mac: dev}, record_unknown=True)
    latest.api_names = {"AA:BB:CC:DD:EE:01": "API の名前", "AA:BB:CC:DD:EE:02": "API の名前2"}
    assert latest.device_for("AA:BB:CC:DD:EE:01").name == "toml の名前"
    assert latest.device_for("AA:BB:CC:DD:EE:02").name == "API の名前2"
    assert latest.device_for("AA:BB:CC:DD:EE:03").name == "AA:BB:CC:DD:EE:03"


def test_parse_device_list():
    body = {"statusCode": 100, "message": "success", "body": {"deviceList": [
        {"deviceId": "aabbccddee01", "deviceName": "書斎", "deviceType": "Meter"},
        {"deviceId": "210", "deviceName": "MAC 形式ではない ID"},
    ], "infraredRemoteList": []}}
    assert sb.parse_device_list(body) == {"AA:BB:CC:DD:EE:01": "書斎"}
    with pytest.raises(RuntimeError):
        sb.parse_device_list({"statusCode": 190, "message": "Unauthorized"})


def test_api_headers():
    import base64, hashlib, hmac
    h = sb.switchbot_api_headers("tok", "sec", t="1700000000000", nonce="n-1")
    expected = base64.b64encode(hmac.new(b"sec", b"tok1700000000000n-1", hashlib.sha256).digest()).decode()
    assert h["Authorization"] == "tok" and h["sign"] == expected and h["t"] == "1700000000000" and h["nonce"] == "n-1"


# --- 書き込めなかったデータの保存と再送 ---------------------------------------
class FakeInflux:
    def __init__(self):
        self.written: list[str] = []
        self.error: Exception | None = None

    def write(self, lines):
        if self.error:
            raise self.error
        self.written += lines


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def test_spool_keeps_data_while_down_and_resends(tmp_path):
    spool, db = sb.Spool(tmp_path / "spool.lp", 10_000_000), FakeInflux()
    db.error = ConnectionError("down")
    spool.flush(db.write, ["a 1"], sb.is_retryable_error)
    spool.flush(db.write, ["b 2"], sb.is_retryable_error)
    assert db.written == [] and sb.Spool(tmp_path / "spool.lp", 1).lines() == ["a 1", "b 2"]  # 再起動しても残る
    db.error = None
    spool.flush(db.write, ["c 3"], sb.is_retryable_error)
    assert sorted(db.written) == ["a 1", "b 2", "c 3"] and not (tmp_path / "spool.lp").exists()


def test_spool_rejects_bad_data(tmp_path):
    spool, db = sb.Spool(tmp_path / "spool.lp", 10_000_000), FakeInflux()
    db.error = HttpError(422)
    spool.flush(db.write, ["bad 1"], sb.is_retryable_error)
    assert not (tmp_path / "spool.lp").exists()
    assert (tmp_path / "spool.lp.rejected").read_text(encoding="utf-8") == "bad 1\n"


def test_spool_trims_oldest_when_full(tmp_path):
    spool, db = sb.Spool(tmp_path / "spool.lp", 100), FakeInflux()
    db.error = HttpError(503)
    for i in range(30):
        spool.flush(db.write, [f"line{i:02d}"], sb.is_retryable_error)
    lines = spool.lines()
    assert lines[-1] == "line29" and "line00" not in lines
    assert (tmp_path / "spool.lp").stat().st_size <= 100


def test_retryable_classification():
    assert sb.is_retryable_error(ConnectionError())
    assert sb.is_retryable_error(HttpError(503)) and sb.is_retryable_error(HttpError(429))
    assert sb.is_retryable_error(HttpError(401)) and sb.is_retryable_error(HttpError(404))
    assert not sb.is_retryable_error(HttpError(400)) and not sb.is_retryable_error(HttpError(422))


def test_point_to_line_protocol():
    from influxdb_client import Point, WritePrecision
    dev = sb.Device(mac="AA:BB:CC:DD:EE:01", name="リビング")
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p = sb.build_point(dev, sb.Reading(battery=90, temperature=21.0, humidity=0.45, rssi=-60), when)
    line = Point.from_dict(p, write_precision=WritePrecision.NS).to_line_protocol()
    assert line == ("switchbot_smartmeter,device_name=リビング,mac_address=AA:BB:CC:DD:EE:01 "
                    "battery=90i,humidity=0.45,rssi=-60i,temperature=21 1767225600000000000")  # i が無いので float
