# switchot_scan

SwitchBot 温湿度計が発信している Bluetooth Low Energy（BLE）のアドバタイズを受信し、温度・湿度・電池残量を InfluxDB v2 に記録する常駐プログラムです。SwitchBot ハブやクラウド API を使わずに、手元の Linux マシンだけで計測値を集められます。

## 対応しているデバイス

| `type` | 対象 | 受信するデータ |
|---|---|---|
| `old` | 温湿度計（旧型） | 温度・湿度・電池残量をすべて Service Data から取得 |
| `new` | 防水温湿度計（屋外用）など | 電池残量を Service Data、温度・湿度を Manufacturer Data から取得 |

## InfluxDB に記録するデータ

| 種類 | 名前 | 内容 |
|---|---|---|
| measurement | `switchbot_smartmeter` | |
| tag | `mac_address` | デバイスの MAC アドレス（大文字） |
| tag | `device_name` | `devices.toml` で付けた名前 |
| field | `temperature` | 温度（℃、小数第1位まで） |
| field | `humidity` | 湿度（0.0〜1.0。55% なら 0.55） |
| field | `battery` | 電池残量（%） |

受信できなかった値は -1 などの仮の値で埋めず、記録から省きます。

## 必要なもの

- BlueZ が動作している Linux と、Bluetooth アダプター
- [mise](https://mise.jdx.dev/)。Python と uv のバージョンを `mise.toml` で管理します
- 書き込み権限のある InfluxDB v2 のトークン

## セットアップ

```bash
sudo apt install bluez
# mise が未導入の場合は https://mise.jdx.dev/installing-mise.html の手順で入れる

sudo git clone https://github.com/DISK0401/switchot_scan.git /opt/switchbot
cd /opt/switchbot
sudo mise trust                # この mise.toml の設定を使うことを許可
sudo mise install              # mise.toml に書いた Python 3.15 と uv を用意
sudo mise exec -- uv sync --no-dev    # .venv を作り、uv.lock のとおりにライブラリを入れる

sudo cp .env.example .env && sudo chmod 600 .env    # InfluxDB の接続情報を記入
sudo cp devices.example.toml devices.toml           # 対象デバイスを記入
```

`.env` と `devices.toml` は `.gitignore` に登録してあるため、誤ってコミットされることはありません。

### devices.toml の書き方

```toml
[[devices]]
mac  = "AA:BB:CC:DD:EE:01"   # SwitchBot アプリの「デバイス情報」で確認できます
name = "リビング"             # InfluxDB の device_name タグになります
type = "old"                 # 旧型温湿度計は old、屋外用などは new
```

## 動作確認

次のコマンドは InfluxDB には書き込まずに、受信した値を画面に表示します。

```bash
sudo .venv/bin/python switchbot_scan.py --dry-run --once
```

| オプション | 内容 |
|---|---|
| `--dry-run` | InfluxDB に書き込まず、取得した値を表示する |
| `--once` | 1回分を記録（表示）したら終了する |
| `--debug` | デバッグログを出力する |

## systemd で常駐させる

```bash
sudo cp deploy/switchbot_scan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now switchbot_scan.service
journalctl -u switchbot_scan.service -f    # ログの確認
```

## 更新

```bash
cd /opt/switchbot
sudo git pull
sudo mise install
sudo mise exec -- uv sync --no-dev
sudo systemctl restart switchbot_scan.service
```

`mise.toml` の Python のバージョンを上げたときは、`.venv` を削除してから `mise exec -- uv sync --no-dev` を実行し、新しい Python で作り直してください。

## 開発

```bash
mise install                  # Python と uv を用意
mise exec -- uv sync          # テスト用ライブラリも含めて用意
mise exec -- uv run pytest    # アドバタイズデータの解析処理をテスト
```

依存ライブラリのバージョンは `uv.lock` で固定しています。更新するときは `mise exec -- uv lock --upgrade` を実行し、テストが通ることを確認してからコミットしてください。
