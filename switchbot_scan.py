"""
SwitchBot 温湿度計の BLE アドバタイズを受信し、InfluxDB v2 に記録する。

設定:
  - 接続情報などの秘密情報 … 環境変数（.env から読み込み可。.env.example 参照）
  - 対象デバイスの一覧     … devices.toml（devices.example.toml 参照）

使い方:
  python switchbot_scan.py            # 常駐してスキャン → InfluxDB へ書き込み
  python switchbot_scan.py --dry-run  # InfluxDB へは書き込まず、取得値を表示する（動作確認用）
"""

from __future__ import annotations

import argparse
import binascii
import logging
import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("switchbot_scan")

MEASUREMENT = "switchbot_smartmeter"
AD_TYPE_SERVICE_DATA = 22       # 0x16: Service Data（16-bit UUID）
AD_TYPE_MANUFACTURER = 255      # 0xFF: Manufacturer Specific Data


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
    scan_seconds: float
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

    def env(name: str, default: Optional[str] = None) -> str:
        value = os.environ.get(name, default)
        if value is None or value == "":
            if require_influx:
                raise SystemExit(f"環境変数 {name} が設定されていません（.env.example を参照）")
            return ""
        return value

    devices_file = Path(os.environ.get("SWITCHBOT_DEVICES_FILE", base_dir / "devices.toml"))
    if not devices_file.is_file():
        raise SystemExit(f"デバイス定義ファイルが見つかりません: {devices_file}（devices.example.toml をコピーして作成）")

    return Settings(
        influx_url=env("INFLUXDB_URL"),
        influx_org=env("INFLUXDB_ORG"),
        influx_token=env("INFLUXDB_TOKEN"),
        influx_bucket=env("INFLUXDB_BUCKET"),
        scan_seconds=float(os.environ.get("SWITCHBOT_SCAN_SECONDS", "10")),
        devices=load_devices(devices_file),
    )


# ---------------------------------------------------------------------------
# アドバタイズデータの解析
# ---------------------------------------------------------------------------
@dataclass
class Reading:
    battery: Optional[int] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None  # 0.0〜1.0（従来データとの互換のため % / 100 で記録）

    def is_complete(self) -> bool:
        return None not in (self.battery, self.temperature, self.humidity)


def _decode_temperature(decimal_byte: int, integer_byte: int) -> float:
    """下位4bit=小数第1位、次バイト下位7bit=整数部、最上位bit=0 のとき氷点下"""
    temperature = (decimal_byte & 0b00001111) / 10 + (integer_byte & 0b01111111)
    if not integer_byte & 0b10000000:
        temperature = -temperature
    return round(temperature, 1)


def parse_old_device(scan_data: Iterable[tuple[int, str, str]]) -> Reading:
    """旧型温湿度計: Service Data に電池・温度・湿度がすべて含まれる"""
    reading = Reading()
    for adtype, _desc, value in scan_data:
        if adtype != AD_TYPE_SERVICE_DATA:
            continue
        data = binascii.unhexlify(value[4:])  # 先頭2バイトはサービス UUID
        if len(data) < 6:
            continue
        reading.battery = data[2] & 0b01111111
        reading.temperature = _decode_temperature(data[3], data[4])
        reading.humidity = (data[5] & 0b01111111) * 0.01
    return reading


def parse_new_device(scan_data: Iterable[tuple[int, str, str]]) -> Reading:
    """新型（屋外用など）: 電池は Service Data、温度・湿度は Manufacturer Data に含まれる"""
    reading = Reading()
    for adtype, _desc, value in scan_data:
        if adtype == AD_TYPE_SERVICE_DATA:
            data = binascii.unhexlify(value[4:])
            if len(data) >= 3:
                reading.battery = data[2] & 0b01111111
        elif adtype == AD_TYPE_MANUFACTURER:
            data = binascii.unhexlify(value[16:])  # 先頭8バイトは会社ID + MAC アドレス
            if len(data) >= 5:
                reading.temperature = _decode_temperature(data[2], data[3])
                reading.humidity = (data[4] & 0b01111111) * 0.01
    return reading


def build_point(device: Device, reading: Reading, now: datetime) -> dict:
    fields = {
        key: value
        for key, value in (
            ("temperature", None if reading.temperature is None else float(reading.temperature)),
            ("humidity", None if reading.humidity is None else float(reading.humidity)),
            ("battery", reading.battery),
        )
        if value is not None
    }
    return {
        "measurement": MEASUREMENT,
        "time": now,
        "tags": {"mac_address": device.mac, "device_name": device.name},
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------
def scan_once(scanner, settings: Settings) -> list[dict]:
    points: list[dict] = []
    now = datetime.now(timezone.utc)
    for found in scanner.scan(settings.scan_seconds):
        device = settings.devices.get(found.addr.upper())
        if device is None:
            continue
        scan_data = found.getScanData()
        reading = parse_old_device(scan_data) if device.type == "old" else parse_new_device(scan_data)
        if not any(v is not None for v in (reading.battery, reading.temperature, reading.humidity)):
            logger.debug("%s: 解析できるデータがありませんでした", device.name)
            continue
        if not reading.is_complete():
            logger.debug("%s: 一部の値のみ取得 %s", device.name, reading)
        points.append(build_point(device, reading, now))
    return points


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="InfluxDB へ書き込まず、取得値を表示する")
    parser.add_argument("--once", action="store_true", help="1回だけスキャンして終了する")
    parser.add_argument("--debug", action="store_true", help="デバッグログを出力する")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if (args.debug or os.environ.get("SWITCHBOT_DEBUG") == "1") else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = load_settings(require_influx=not args.dry_run)

    from bluepy.btle import Scanner  # BLE 環境（Linux）でのみ必要なため遅延インポート

    scanner = Scanner()
    client = write_api = None
    if not args.dry_run:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        client = InfluxDBClient(url=settings.influx_url, token=settings.influx_token, org=settings.influx_org)
        write_api = client.write_api(write_options=SYNCHRONOUS)

    logger.info("スキャン開始（対象 %d 台, %s 秒間隔%s）", len(settings.devices), settings.scan_seconds,
                ", dry-run" if args.dry_run else "")
    try:
        while True:
            points = scan_once(scanner, settings)
            if points:
                if args.dry_run:
                    for p in points:
                        print(p["tags"]["device_name"], p["fields"], flush=True)
                else:
                    try:
                        write_api.write(bucket=settings.influx_bucket, org=settings.influx_org, record=points)
                        logger.debug("%d 件書き込み", len(points))
                    except Exception as e:  # 一時的な接続断で常駐プロセスを落とさない
                        logger.warning("InfluxDB への書き込みに失敗: %s", e)
            if args.once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
