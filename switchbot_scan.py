"""
SwitchBot 温湿度計の BLE アドバタイズを受信し、InfluxDB v2 に記録する。

設定:
  - 接続情報などの秘密情報 … 環境変数（.env から読み込み可。.env.example 参照）
  - デバイスの名前         … devices.toml（任意）、SwitchBot API（任意）、どちらも無ければ MAC アドレス

使い方:
  switchbot_scan.py            # 常駐してスキャンし、InfluxDB へ書き込む
  switchbot_scan.py --dry-run  # InfluxDB へは書き込まず、取得値を表示する（動作確認用）
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sys
import time
import tomllib
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

logger = logging.getLogger("switchbot_scan")

MEASUREMENT = "switchbot_smartmeter"
SWITCHBOT_COMPANY_ID = 0x0969  # Woan Technology（SwitchBot）の Bluetooth SIG 会社 ID
SWITCHBOT_SERVICE_UUIDS = (
    "0000fd3d-0000-1000-8000-00805f9b34fb",  # 現行
    "00000d00-0000-1000-8000-00805f9b34fb",  # 旧ファームウェア
)
SWITCHBOT_API_URL = "https://api.switch-bot.com/v1.1/devices"


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Device:
    mac: str
    name: str
    type: str = "auto"  # 互換のため残している。機種は受信データから判別する


@dataclass(frozen=True)
class Settings:
    influx_url: str
    influx_org: str
    influx_token: str
    influx_bucket: str
    interval_seconds: float
    stale_seconds: float
    watchdog_seconds: float
    devices: dict[str, Device]
    record_unknown: bool = True
    switchbot_token: str = ""
    switchbot_secret: str = ""
    name_refresh_seconds: float = 6 * 3600
    spool_file: Optional[Path] = None
    spool_max_bytes: int = 50 * 1024 * 1024


def load_devices(path: Path) -> dict[str, Device]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    devices: dict[str, Device] = {}
    for entry in data.get("devices", []):
        mac = str(entry["mac"]).upper()
        dev_type = str(entry.get("type", "auto"))
        if dev_type not in ("auto", "old", "new"):
            raise ValueError(f"{path}: {mac} の type は省略するか 'auto' / 'old' / 'new' を指定してください（{dev_type!r}）")
        devices[mac] = Device(mac=mac, name=str(entry["name"]), type=dev_type)
    return devices


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


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

    devices_env = os.environ.get("SWITCHBOT_DEVICES_FILE")
    devices_file = Path(devices_env) if devices_env else base_dir / "devices.toml"
    if devices_file.is_file():
        devices = load_devices(devices_file)
    elif devices_env:  # 明示したファイルが無いのは設定の誤り
        raise SystemExit(f"デバイス定義ファイルが見つかりません: {devices_file}")
    else:
        devices = {}

    record_unknown = _env_bool("SWITCHBOT_RECORD_UNKNOWN", True)
    if not devices and not record_unknown:
        raise SystemExit("devices.toml が無く SWITCHBOT_RECORD_UNKNOWN=0 のため、記録するデバイスがありません")

    spool_env = os.environ.get("SWITCHBOT_SPOOL_FILE")
    return Settings(
        influx_url=env("INFLUXDB_URL"),
        influx_org=env("INFLUXDB_ORG"),
        influx_token=env("INFLUXDB_TOKEN"),
        influx_bucket=env("INFLUXDB_BUCKET"),
        interval_seconds=float(os.environ.get("SWITCHBOT_INTERVAL_SECONDS", "10")),
        stale_seconds=float(os.environ.get("SWITCHBOT_STALE_SECONDS", "300")),
        watchdog_seconds=float(os.environ.get("SWITCHBOT_WATCHDOG_SECONDS", "600")),
        devices=devices,
        record_unknown=record_unknown,
        switchbot_token=os.environ.get("SWITCHBOT_API_TOKEN", ""),
        switchbot_secret=os.environ.get("SWITCHBOT_API_SECRET", ""),
        name_refresh_seconds=float(os.environ.get("SWITCHBOT_NAME_REFRESH_SECONDS", str(6 * 3600))),
        spool_file=Path(spool_env) if spool_env else base_dir / "spool.lp",
        spool_max_bytes=int(float(os.environ.get("SWITCHBOT_SPOOL_MAX_MB", "50")) * 1024 * 1024),
    )


# ---------------------------------------------------------------------------
# アドバタイズデータの解析
#   service_data      : {UUID 文字列: UUID を除いたデータ}
#   manufacturer_data : {会社 ID: 会社 ID を除いたデータ}
# ---------------------------------------------------------------------------
# 機種コード（Service Data 先頭バイトの下位7bit）。参考: pySwitchbot の adv_parser.py
METER_MODELS = {
    "T": "Meter",
    "i": "Meter Plus",
    "w": "Indoor/Outdoor Thermo-Hygrometer",
    "4": "Meter Pro",
    "5": "Meter Pro CO2",
}
CO2_MAX_PPM = 9999


@dataclass
class Reading:
    battery: Optional[int] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None  # 0.0〜1.0（従来データとの互換のため % / 100 で記録）
    co2: Optional[int] = None  # ppm（Meter Pro CO2 のみ）
    rssi: Optional[int] = None  # 受信した電波の強さ（dBm）

    def has_any(self) -> bool:
        """計測値を1つでも含むか（rssi は計測値に含めない）"""
        return any(v is not None for v in (self.battery, self.temperature, self.humidity, self.co2))


def _decode_temperature(decimal_byte: int, integer_byte: int) -> float:
    """下位4bit=小数第1位、次バイト下位7bit=整数部、最上位bit=0 のとき氷点下（値は常に摂氏）"""
    temperature = (decimal_byte & 0b00001111) / 10 + (integer_byte & 0b01111111)
    if not integer_byte & 0b10000000:
        temperature = -temperature
    return round(temperature, 1)


def _decode_humidity(byte: int) -> float:
    """下位7bit=湿度(%)。最上位bit は本体の表示単位（華氏）のフラグなので使わない"""
    return (byte & 0b01111111) / 100


def _switchbot_service_data(service_data: Mapping[str, bytes]) -> Optional[bytes]:
    for uuid_ in SWITCHBOT_SERVICE_UUIDS:
        if uuid_ in service_data:
            return service_data[uuid_]
    return None


def detect_model(service_data: Mapping[str, bytes]) -> Optional[str]:
    """Service Data の先頭バイトから機種コードを返す（温湿度計でなければ None）"""
    data = _switchbot_service_data(service_data)
    if not data:
        return None
    code = chr(data[0] & 0b01111111)
    return code if code in METER_MODELS else None


def parse_meter(service_data: Mapping[str, bytes], manufacturer_data: Mapping[int, bytes],
                model: Optional[str] = None) -> Reading:
    """温湿度計のアドバタイズを解析する。

    - 電池残量 : Service Data の [2]
    - 温度・湿度: Manufacturer Data（会社 ID の後、MAC 6byte に続く [2]〜[4]）を優先し、
                  無ければ Service Data の [3]〜[5]（旧型の Meter や旧ファームウェア）
    - CO2      : Meter Pro CO2 のみ、Manufacturer Data の [13]〜[14]（ビッグエンディアン）
    新型は電池と温湿度が別のパケットで届くことがあるため、どちらか片方だけでも返す。
    """
    reading = Reading()
    data = _switchbot_service_data(service_data)
    model = detect_model(service_data) or model

    if data is not None and len(data) >= 3:
        reading.battery = data[2] & 0b01111111

    mdata = manufacturer_data.get(SWITCHBOT_COMPANY_ID)
    if mdata is not None and len(mdata) >= 11:
        reading.temperature = _decode_temperature(mdata[8], mdata[9])
        reading.humidity = _decode_humidity(mdata[10])
    elif data is not None and len(data) >= 6:
        reading.temperature = _decode_temperature(data[3], data[4])
        reading.humidity = _decode_humidity(data[5])

    if model == "5" and mdata is not None and len(mdata) >= 15:
        co2 = int.from_bytes(mdata[13:15], "big")
        if co2 <= CO2_MAX_PPM:
            reading.co2 = co2
    return reading


def build_point(device: Device, reading: Reading, now: datetime) -> dict:
    fields: dict[str, float | int] = {}
    if reading.temperature is not None:
        fields["temperature"] = float(reading.temperature)
    if reading.humidity is not None:
        fields["humidity"] = float(reading.humidity)
    if reading.battery is not None:
        fields["battery"] = int(reading.battery)
    if reading.co2 is not None:
        fields["co2"] = int(reading.co2)
    if reading.rssi is not None:
        fields["rssi"] = int(reading.rssi)
    return {
        "measurement": MEASUREMENT,
        "time": now,
        "tags": {"mac_address": device.mac, "device_name": device.name},
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# SwitchBot API（デバイス名の取得。任意）
# ---------------------------------------------------------------------------
def switchbot_api_headers(token: str, secret: str, t: Optional[str] = None, nonce: Optional[str] = None) -> dict:
    """SwitchBot API v1.1 の認証ヘッダー（sign = Base64(HMAC-SHA256(secret, token + t + nonce))）"""
    t = t or str(int(time.time() * 1000))
    nonce = nonce or str(uuid.uuid4())
    digest = hmac.new(secret.encode(), f"{token}{t}{nonce}".encode(), hashlib.sha256).digest()
    return {
        "Authorization": token,
        "sign": base64.b64encode(digest).decode(),
        "t": t,
        "nonce": nonce,
        "Content-Type": "application/json; charset=utf8",
    }


def parse_device_list(body: dict) -> dict[str, str]:
    """GET /v1.1/devices の応答から {MAC: アプリで付けた名前} を作る（deviceId が MAC 形式のものだけ）"""
    if body.get("statusCode") != 100:
        raise RuntimeError(f"SwitchBot API がエラーを返しました: {body.get('statusCode')} {body.get('message')}")
    names: dict[str, str] = {}
    for item in body.get("body", {}).get("deviceList", []):
        device_id = str(item.get("deviceId", ""))
        name = item.get("deviceName")
        if name and re.fullmatch(r"[0-9A-Fa-f]{12}", device_id):
            mac = ":".join(device_id[i:i + 2] for i in range(0, 12, 2)).upper()
            names[mac] = str(name)
    return names


def fetch_device_names(token: str, secret: str, timeout: float = 15) -> dict[str, str]:
    request = urllib.request.Request(SWITCHBOT_API_URL, headers=switchbot_api_headers(token, secret))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_device_list(json.load(response))


# ---------------------------------------------------------------------------
# 受信値の保持
# ---------------------------------------------------------------------------
@dataclass
class _Entry:
    reading: Reading = field(default_factory=Reading)
    seen: float = 0.0


class LatestReadings:
    """デバイスごとに最後に受信した値を保持する（新型は電池と温湿度が別パケットで届くため値をマージする）

    devices.toml に書いたデバイスはそのまま記録する。書いていないデバイスは、record_unknown が真で、
    温湿度計だと機種を判別できたものだけを記録する（プラグなど別の機器のデータを温度として読まないため）。
    名前は devices.toml → SwitchBot API → MAC アドレスの順に決める。
    """

    def __init__(self, devices: dict[str, Device], record_unknown: bool = False) -> None:
        self.devices = devices
        self.record_unknown = record_unknown
        self.api_names: dict[str, str] = {}
        self._latest: dict[str, _Entry] = {}
        self._models: dict[str, str] = {}
        self.last_received = time.monotonic()  # 対象デバイスから最後に受信した時刻（ウォッチドッグ用）

    def device_for(self, mac: str) -> Device:
        if mac in self.devices:
            return self.devices[mac]
        return Device(mac=mac, name=self.api_names.get(mac, mac))

    def update(self, mac: str, service_data: Mapping[str, bytes], manufacturer_data: Mapping[int, bytes],
               rssi: Optional[int] = None) -> None:
        mac = mac.upper()
        registered = mac in self.devices
        model = detect_model(service_data)
        if model is not None and self._models.get(mac) != model:
            if registered or self.record_unknown:
                name = self.device_for(mac).name
                label = f"{mac} を" if name == mac else f"{name}（{mac}）を"
                logger.info("%s %s として認識しました%s", label, METER_MODELS[model],
                            "" if registered else "（devices.toml に未登録）")
            self._models[mac] = model
        if not registered and not (self.record_unknown and mac in self._models):
            return
        new = parse_meter(service_data, manufacturer_data, self._models.get(mac))
        if not new.has_any():
            return
        self.last_received = time.monotonic()
        old = self._latest.get(mac, _Entry()).reading
        merged = Reading(
            battery=new.battery if new.battery is not None else old.battery,
            temperature=new.temperature if new.temperature is not None else old.temperature,
            humidity=new.humidity if new.humidity is not None else old.humidity,
            co2=new.co2 if new.co2 is not None else old.co2,
            rssi=rssi if rssi is not None else old.rssi,
        )
        self._latest[mac] = _Entry(merged, time.monotonic())

    def points(self, stale_seconds: float) -> list[dict]:
        now_mono = time.monotonic()
        now = datetime.now(timezone.utc)
        return [
            build_point(self.device_for(mac), entry.reading, now)
            for mac, entry in self._latest.items()
            if now_mono - entry.seen <= stale_seconds
        ]


# ---------------------------------------------------------------------------
# 書き込めなかったデータの保存と再送
# ---------------------------------------------------------------------------
class Spool:
    """InfluxDB に書き込めなかった行（line protocol）をファイルに貯め、復旧後に古い順に送る。

    ファイルに残すため、プログラムを再起動しても失われない。max_bytes を超えたら古い行から捨てる。
    """

    CHUNK_LINES = 5000

    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def lines(self) -> list[str]:
        try:
            return [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        except FileNotFoundError:
            return []

    def _rewrite(self, lines: list[str]) -> None:
        if not lines:
            self.path.unlink(missing_ok=True)
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def append(self, lines: list[str]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.writelines(line + "\n" for line in lines)
        if self.path.stat().st_size > self.max_bytes:
            kept = self.lines()
            size = 0
            keep_from = len(kept)
            # 上限の 9 割に収まるよう、新しい行から数えて残す
            while keep_from > 0 and size + len(kept[keep_from - 1].encode()) + 1 <= self.max_bytes * 0.9:
                keep_from -= 1
                size += len(kept[keep_from].encode()) + 1
            logger.warning("保存したデータが上限（%d MB）を超えたため、古い %d 行を捨てました",
                           self.max_bytes // (1024 * 1024), keep_from)
            self._rewrite(kept[keep_from:])

    def reject(self, lines: list[str]) -> Path:
        """送っても受け付けられないデータを別のファイルに移す"""
        rejected = self.path.with_name(self.path.name + ".rejected")
        with rejected.open("a", encoding="utf-8") as f:
            f.writelines(line + "\n" for line in lines)
        return rejected

    def _send(self, write: Callable[[list[str]], None], lines: list[str],
              is_retryable: Callable[[Exception], bool]) -> None:
        """lines を書き込む。受け付けられないデータは reject に移し、一時的な失敗は例外のまま返す"""
        try:
            write(lines)
        except Exception as e:
            if is_retryable(e):
                raise
            path = self.reject(lines)
            logger.error("InfluxDB がデータを受け付けなかったため %d 行を %s に移しました: %s", len(lines), path, e)

    def flush(self, write: Callable[[list[str]], None], new_lines: list[str],
              is_retryable: Callable[[Exception], bool]) -> None:
        """new_lines を書き込み、成功したら保存済みの行も古い順に送る。一時的な失敗なら保存する。

        InfluxDB は時刻つきで記録するため、送る順番が前後しても結果は同じになる。止まっているあいだは
        保存ファイルを読まずに追記だけを行う。
        """
        try:
            self._send(write, new_lines, is_retryable)
        except Exception as e:
            self.append(new_lines)
            logger.warning("InfluxDB への書き込みに失敗したため %d 行を保存しました（復旧後に送ります）: %s",
                           len(new_lines), e)
            return
        if not self.path.exists():
            return
        pending = self.lines()
        sent = 0
        try:
            while sent < len(pending):
                chunk = pending[sent:sent + self.CHUNK_LINES]
                self._send(write, chunk, is_retryable)
                sent += len(chunk)
        except Exception as e:
            self._rewrite(pending[sent:])
            logger.warning("保存していたデータの再送が途中で失敗しました（%d / %d 行を送信）: %s", sent, len(pending), e)
            return
        self._rewrite([])
        logger.info("保存していた %d 行を InfluxDB に送りました", len(pending))


def is_retryable_error(e: Exception) -> bool:
    """400・422（データの形の誤りや型の不一致）は再送しても通らないので再送しない。

    それ以外（接続の失敗、5xx、429、トークンや bucket の設定の誤りによる 401・403・404 など）は、
    原因を直せば受け付けられるため保存して再送する。
    """
    return getattr(e, "status", None) not in (400, 422)


# ---------------------------------------------------------------------------
# 受信と書き込み
# ---------------------------------------------------------------------------
async def refresh_names(settings: Settings, latest: LatestReadings, stop: asyncio.Event) -> None:
    """SwitchBot API からデバイス名を定期的に取得する（トークンを設定したときだけ）"""
    while not stop.is_set():
        try:
            names = await asyncio.to_thread(fetch_device_names, settings.switchbot_token, settings.switchbot_secret)
        except Exception as e:  # 取得できなくても記録は続ける（前回の名前か MAC アドレスを使う）
            logger.warning("SwitchBot API からデバイス名を取得できませんでした: %s", e)
        else:
            if names != latest.api_names:
                logger.info("SwitchBot API から %d 台分のデバイス名を取得しました", len(names))
            latest.api_names = names
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.name_refresh_seconds)
        except TimeoutError:
            pass


async def run(settings: Settings, dry_run: bool, once: bool) -> None:
    from bleak import BleakScanner  # BLE 環境でのみ必要なため遅延インポート

    latest = LatestReadings(settings.devices, record_unknown=settings.record_unknown)

    def on_advertisement(device, adv) -> None:
        latest.update(device.address, adv.service_data, adv.manufacturer_data, adv.rssi)

    client = write_lines = None
    spool = Spool(settings.spool_file, settings.spool_max_bytes) if settings.spool_file else None
    if not dry_run:
        from influxdb_client import InfluxDBClient, Point, WritePrecision
        from influxdb_client.client.write_api import SYNCHRONOUS

        client = InfluxDBClient(url=settings.influx_url, token=settings.influx_token, org=settings.influx_org,
                                timeout=15_000)
        write_api = client.write_api(write_options=SYNCHRONOUS)

        def write_lines(lines: list[str]) -> None:
            write_api.write(bucket=settings.influx_bucket, org=settings.influx_org, record=lines,
                            write_precision=WritePrecision.NS)

        def to_lines(points: list[dict]) -> list[str]:
            return [Point.from_dict(p, write_precision=WritePrecision.NS).to_line_protocol() for p in points]

        if spool and spool.lines():
            logger.info("前回までに保存した %d 行があります。復旧していれば送ります", len(spool.lines()))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop.set)  # systemctl stop で正常に終了させる
    except (NotImplementedError, AttributeError):  # Windows では使えない（Ctrl+C で終了する）
        pass

    tasks = []
    if settings.switchbot_token and settings.switchbot_secret:
        tasks.append(asyncio.create_task(refresh_names(settings, latest, stop)))

    logger.info("スキャン開始（登録 %d 台、未登録の温湿度計を%s、%s 秒ごとに記録%s）",
                len(settings.devices), "記録する" if settings.record_unknown else "記録しない",
                settings.interval_seconds, "、dry-run" if dry_run else "")
    try:
        async with BleakScanner(detection_callback=on_advertisement, scanning_mode="active"):
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.interval_seconds)
                    break
                except TimeoutError:
                    pass
                silent = time.monotonic() - latest.last_received
                if settings.watchdog_seconds > 0 and silent > settings.watchdog_seconds:
                    # bluetoothd の再起動などでスキャンが止まると、例外が出ないまま受信だけが途絶える。
                    # 異常終了して systemd（Restart=always）に再起動させる
                    raise RuntimeError(f"{silent:.0f} 秒間どのデバイスからも受信できていないため終了します")
                points = latest.points(settings.stale_seconds)
                if not points:
                    logger.debug("記録対象のデータがありません")
                elif dry_run:
                    for p in points:
                        print(p["tags"]["device_name"], p["fields"], flush=True)
                elif spool is not None:
                    await asyncio.to_thread(spool.flush, write_lines, to_lines(points), is_retryable_error)
                else:
                    try:
                        await asyncio.to_thread(write_lines, to_lines(points))
                    except Exception as e:  # 一時的な接続断で常駐プロセスを落とさない
                        logger.warning("InfluxDB への書き込みに失敗: %s", e)
                if once:
                    break
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
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
    except RuntimeError as e:
        logger.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
