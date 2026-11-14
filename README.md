# switchot_scan

SwitchBot 温湿度計の BLE アドバタイズを受信し、InfluxDB v2 に記録する常駐スクリプト。

- 旧型温湿度計（`type = "old"`）と、新型・屋外用（`type = "new"`）に対応
- 記録先: measurement `switchbot_smartmeter`、tag `mac_address` / `device_name`、field `temperature` / `humidity`（0〜1）/ `battery`

## セットアップ（Debian / Ubuntu）

```bash
sudo apt install python3-venv build-essential libglib2.0-dev bluez
sudo git clone https://github.com/DISK0401/switchot_scan.git /opt/switchbot
cd /opt/switchbot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

sudo cp .env.example .env && sudo chmod 600 .env       # 接続情報を記入
sudo cp devices.example.toml devices.toml              # 対象デバイスを記入
```

## 動作確認

```bash
sudo .venv/bin/python switchbot_scan.py --dry-run --once   # InfluxDB へは書き込まずに表示
```

## 常駐（systemd）

```bash
sudo cp deploy/switchbot_scan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now switchbot_scan.service
```

## テスト

```bash
pip install pytest && pytest
```

`.env` と `devices.toml` は `.gitignore` 済みのため、誤ってコミットされない。
