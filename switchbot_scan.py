"""
SwitchBot 温湿度計の BLE アドバタイズを受信し、InfluxDB v2 に記録する。

設定:
  - 接続情報などの秘密情報 … 環境変数（.env から読み込み可。.env.example 参照）
  - 対象デバイスの一覧     … devices.toml（devices.example.toml 参照）

使い方:
  switchbot_scan.py            # 常駐してスキャンし、InfluxDB へ書き込む
  switchbot_scan.py --dry-run  # InfluxDB へは書き込まず、取得値を表示する（動作確認用）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger("switchbot_scan")

MEASUREMENT = "switchbot_smartmeter"
SWITCHBOT_COMPANY_ID = 0x0969  # Woan Technology（SwitchBot）の Bluetooth SIG 会社 ID
SWITCHBOT_SERVICE_UUIDS = (
    "0000fd3d-0000-1000-8000-00805f9b34fb",  # 現行
    "00000d00-0000-1000-8000-00805f9b34fb",  # 旧ファームウェア
)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Device:
    mac: str
    name: str
    type: str  # "old"（旧型温湿度計） / "new"（新型・屋外用など）


@dataclass(frozen=True)
class Settings:
    influx_url: str
    influx_org: str
    influx_token: str
    influx_bucket: str
    interval_seconds: float
    stale_seconds: float
    devices: dict[str, Device]


def load_devices(path: Path) -> dict[str, Device]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    devices: dict[str, Device] = {}
    for entry in data.get("devices", []):
        mac = str(entry["mac"]).upper()
        dev_type = str(entry.get("type", "old"))
        if dev_type not in ("old", "new"):
            raise ValueError(f"{path}: {mac} の type は 'old' か 'new' を指定してください（{dev_type!r}）")
        devices[mac] = Device(mac=mac, name=str(entry["name"]), type=dev_type)
    if not devices:
        raise ValueError(f"{path}: [[devices]] が1件も定義されていません")
    return devices


def load_settings(require_influx: bool) -> Settings:
    base_dir = Path(__file__).resolve().parent
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv が無くても環境変数だけで動作する
        pass
    else:
        load_dotenv(base_dir / ".env")

    def env(name: str) -> str:
        value = os.environ.get(name, "")
        if not value and require_influx:
            raise SystemExit(f"環境変数 {name} が設定されていません（.env.example を参照）")
        return value

    devices_file = Path(os.environ.get("SWITCHBOT_DEVICES_FILE", base_dir / "devices.toml"))
    if not devices_file.is_file():
        raise SystemExit(f"デバイス定義ファイルが見つかりません: {devices_file}（devices.example.toml をコピーして作成）")

    return Settings(
        influx_url=env("INFLUXDB_URL"),
        influx_org=env("INFLUXDB_ORG"),
        influx_token=env("INFLUXDB_TOKEN"),
        influx_bucket=env("INFLUXDB_BUCKET"),
        interval_seconds=float(os.environ.get("SWITCHBOT_INTERVAL_SECONDS", "10")),
        stale_seconds=float(os.environ.get("SWITCHBOT_STALE_SECONDS", "300")),
        devices=load_devices(devices_file),
    )


# ---------------------------------------------------------------------------
# アドバタイズデータの解析
#   service_data      : {UUID 文字列: UUID を除いたデータ}
#   manufacturer_data : {会社 ID: 会社 ID を除いたデータ}
# ---------------------------------------------------------------------------
@dataclass
class Reading:
    battery: Optional[int] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None  # 0.0〜1.0（従来データとの互換のため % / 100 で記録）

    def has_any(self) -> bool:
        return any(v is not None for v in (self.battery, self.temperature, self.humidity))


def _decode_temperature(decimal_byte: int, integer_byte: int) -> float:
    """下位4bit=小数第1位、次バイト下位7bit=整数部、最上位bit=0 のとき氷点下"""
    temperature = (decimal_byte & 0b00001111) / 10 + (integer_byte & 0b01111111)
    if not integer_byte & 0b10000000:
        temperature = -temperature
    return round(temperature, 1)


def _switchbot_service_data(service_data: Mapping[str, bytes]) -> Optional[bytes]:
    for uuid in SWITCHBOT_SERVICE_UUIDS:
        if uuid in service_data:
            return service_data[uuid]
    return None


def parse_old_device(service_data: Mapping[str, bytes], manufacturer_data: Mapping[int, bytes]) -> Reading:
    """旧型温湿度計: Service Data に電池・温度・湿度がすべて含まれる"""
    reading = Reading()
    data = _switchbot_service_data(service_data)
    if data is not None and len(data) >= 6:
        reading.battery = data[2] & 0b01111111
        reading.temperature = _decode_temperature(data[3], data[4])
        reading.humidity = (data[5] & 0b01111111) * 0.01
    return reading


def parse_new_device(service_data: Mapping[str, bytes], manufacturer_data: Mapping[int, bytes]) -> Reading:
    """新型（屋外用など）: 電池は Service Data、温度・湿度は Manufacturer Data（MAC 6byte の後）に含まれる"""
    reading = Reading()
    data = _switchbot_service_data(service_data)
    if data is not None and len(data) >= 3:
        reading.battery = data[2] & 0b01111111
    mdata = manufacturer_data.get(SWITCHBOT_COMPANY_ID)
    if mdata is not None and len(mdata) >= 6 + 5:
        payload = mdata[6:]
        reading.temperature = _decode_temperature(payload[2], payload[3])
        reading.humidity = (payload[4] & 0b01111111) * 0.01
    return reading


PARSERS = {"old": parse_old_device, "new": parse_new_device}


def build_point(device: Device, reading: Reading, now: datetime) -> dict:
    fields: dict[str, float | int] = {}
    if reading.temperature is not None:
        fields["temperature"] = float(reading.temperature)
    if reading.humidity is not None:
        fields["humidity"] = float(reading.humidity)
    if reading.battery is not None:
        fields["battery"] = int(reading.battery)
    return {
        "measurement": MEASUREMENT,
        "time": now,
        "tags": {"mac_address": device.mac, "device_name": device.name},
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# 受信と書き込み
# ---------------------------------------------------------------------------
class LatestReadings:
    """デバイスごとに最後に受信した値を保持する（新型は電池と温湿度が別パケットで届くため値をマージする）"""

    def __init__(self, devices: dict[str, Device]) -> None:
        self.devices = devices
        self._latest: dict[str, tuple[Reading, float]] = {}

    def update(self, mac: str, service_data: Mapping[str, bytes], manufacturer_data: Mapping[int, bytes]) -> None:
        device = self.devices.get(mac.upper())
        if device is None:
            return
        new = PARSERS[device.type](service_data, manufacturer_data)
        if not new.has_any():
            return
        old, _ = self._latest.get(device.mac, (Reading(), 0.0))
        merged = Reading(
            battery=new.battery if new.battery is not None else old.battery,
            temperature=new.temperature if new.temperature is not None else old.temperature,
            humidity=new.humidity if new.humidity is not None else old.humidity,
        )
        self._latest[device.mac] = (merged, time.monotonic())

    def points(self, stale_seconds: float) -> list[dict]:
        now_mono = time.monotonic()
        now = datetime.now(timezone.utc)
        return [
            build_point(self.devices[mac], reading, now)
            for mac, (reading, seen) in self._latest.items()
            if now_mono - seen <= stale_seconds
        ]


async def run(settings: Settings, dry_run: bool, once: bool) -> None:
    from bleak import BleakScanner  # BLE 環境でのみ必要なため遅延インポート

    latest = LatestReadings(settings.devices)

    def on_advertisement(device, adv) -> None:
        latest.update(device.address, adv.service_data, adv.manufacturer_data)

    client = write_api = None
    if not dry_run:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        client = InfluxDBClient(url=settings.influx_url, token=settings.influx_token, org=settings.influx_org)
        write_api = client.write_api(write_options=SYNCHRONOUS)

    logger.info("スキャン開始（対象 %d 台、%s 秒ごとに記録%s）",
                len(settings.devices), settings.interval_seconds, "、dry-run" if dry_run else "")
    try:
        async with BleakScanner(detection_callback=on_advertisement, scanning_mode="active"):
            while True:
                await asyncio.sleep(settings.interval_seconds)
                points = latest.points(settings.stale_seconds)
                if not points:
                    logger.debug("記録対象のデータがありません")
                elif dry_run:
                    for p in points:
                        print(p["tags"]["device_name"], p["fields"], flush=True)
                else:
                    try:
                        await asyncio.to_thread(
                            write_api.write, bucket=settings.influx_bucket, org=settings.influx_org, record=points
                        )
                        logger.debug("%d 件書き込み", len(points))
                    except Exception as e:  # 一時的な接続断で常駐プロセスを落とさない
                        logger.warning("InfluxDB への書き込みに失敗: %s", e)
                if once:
                    break
    finally:
        if client is not None:
            client.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="InfluxDB へ書き込まず、取得値を表示する")
    parser.add_argument("--once", action="store_true", help="1回分の記録を行って終了する")
    parser.add_argument("--debug", action="store_true", help="デバッグログを出力する")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if (args.debug or os.environ.get("SWITCHBOT_DEBUG") == "1") else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = load_settings(require_influx=not args.dry_run)
    try:
        asyncio.run(run(settings, dry_run=args.dry_run, once=args.once))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
