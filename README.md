# switchot_scan

SwitchBot 温湿度計が発信している Bluetooth Low Energy（BLE）のアドバタイズを受信し、温度・湿度・電池残量を InfluxDB v2 に記録する常駐プログラムです。計測値は SwitchBot ハブやクラウドを通さずに、手元の Linux マシンだけで集められます。

## 対応しているデバイス

| 機種 | 記録する値 |
|---|---|
| 温湿度計（Meter） | 温度・湿度・電池残量 |
| 温湿度計プラス（Meter Plus） | 温度・湿度・電池残量 |
| 防水温湿度計（Indoor/Outdoor Thermo-Hygrometer） | 温度・湿度・電池残量 |
| 温湿度計 Pro（Meter Pro） | 温度・湿度・電池残量 |
| CO2 センサー（Meter Pro CO2） | 温度・湿度・電池残量・CO2 濃度 |

機種は受信したデータから自動で判別します。電池残量は Service Data から、温度と湿度は Manufacturer Data から読み、Manufacturer Data に温度と湿度が含まれない旧ファームウェアでは Service Data から読みます。解析の方法は Home Assistant が使っている [pySwitchbot](https://github.com/sblibs/pySwitchbot) に合わせています。

## InfluxDB に記録するデータ

| 種類 | 名前 | 内容 |
|---|---|---|
| measurement | `switchbot_smartmeter` | |
| tag | `mac_address` | デバイスの MAC アドレス（大文字） |
| tag | `device_name` | デバイスの名前（決め方は「デバイスの名前と記録の対象」を参照） |
| field | `temperature` | 温度（℃、小数第1位まで） |
| field | `humidity` | 湿度（0.0〜1.0。55% なら 0.55） |
| field | `battery` | 電池残量（%） |
| field | `co2` | CO2 濃度（ppm。Meter Pro CO2 のみ） |
| field | `rssi` | 受信した電波の強さ（dBm。-40 なら強く、-90 なら弱い） |

受信できなかった値は -1 などの仮の値で埋めず、記録から省きます。

## 必要なもの

- Bluetooth アダプターのある Linux（BlueZ が動作しているもの）。Windows でも動作します（「Windows で使う」を参照）
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
sudo cp devices.example.toml devices.toml           # 名前を決めたいデバイスを記入（省略できる）
```

`.env` と `devices.toml` は `.gitignore` に登録してあるため、誤ってコミットされることはありません。

### デバイスの名前と記録の対象

受信した温湿度計は、`devices.toml` に書いていなくても記録します。`device_name` には、次の順で見つかった名前を使います。

1. `devices.toml` に書いた名前
2. SwitchBot アプリで付けた名前（`.env` に `SWITCHBOT_API_TOKEN` と `SWITCHBOT_API_SECRET` を設定したときだけ）
3. MAC アドレス

アプリで付けた名前は Bluetooth では送られてこないため、SwitchBot API から取得します。トークンとシークレットは、アプリの「プロフィール > 設定」で「アプリバージョン」を10回タップすると表示される「開発者向けオプション」で確認できます。

SwitchBot API を呼び出すのは、`devices.toml` に書いていない温湿度計を見つけたときです。すべての温湿度計を `devices.toml` に書いていれば、API は呼び出しません。

- 名前を取得できるまでは、10分（`SWITCHBOT_NAME_RETRY_SECONDS`）ごとに取得し直します。そのあいだのデータは InfluxDB に送らずに `pending.jsonl` に保存し、名前が分かったら、その名前で受信したときの時刻のまま送ります。MAC アドレスを名前にしたデータが混ざらないようにするためです。
- 名前を取得できたあとも、名前の変更を反映するため、24時間（`SWITCHBOT_NAME_REFRESH_SECONDS`）ごとに取得します。アプリで名前を変えると `device_name` も変わるため、名前を固定したいデバイスは `devices.toml` に書いてください。
- 自分のアカウントに登録していない温湿度計（近所のものなど）は、名前を取得できないため記録しません。そのデータは `pending.jsonl` に貯まり続け、上限（`SWITCHBOT_PENDING_MAX_MB`、既定は 10 MB）を超えると古いものから捨てます。

API のトークンを設定していないときは、MAC アドレスを名前にしてすぐに記録します。

近所の温湿度計の電波まで届く環境では、それらも記録されます。`devices.toml` に書いたデバイスだけを記録するには、`.env` に `SWITCHBOT_RECORD_UNKNOWN=0` を設定します。

### devices.toml の書き方

```toml
[[devices]]
mac  = "AA:BB:CC:DD:EE:01"   # SwitchBot アプリの「デバイス情報」で確認できます
name = "リビング"             # InfluxDB の device_name タグになります
```

以前のバージョンで使っていた `type = "old"` / `type = "new"` は、書いたままでも動作します。機種は自動で判別するため、この値は使いません。

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

InfluxDB に書き込めなかったデータは `spool.lp` に保存し、書き込めるようになったら送ります。ファイルに保存するため、プログラムを再起動しても失われません。保存する量の上限は `SWITCHBOT_SPOOL_MAX_MB`（既定は 50 MB。4台なら10日分ほど）で、超えた分は古いものから捨てます。InfluxDB がデータの形の誤りで受け付けなかったデータ（HTTP 400・422）は、再送せずに `spool.lp.rejected` に移します。

BLE の受信が `SWITCHBOT_WATCHDOG_SECONDS`（既定は 600 秒）のあいだ途絶えると、プログラムはエラーで終了し、systemd が再起動します。bluetoothd の再起動などでスキャンが止まっても、エラーが出ないまま記録だけが途切れることを防ぐためです。

## 更新

```bash
cd /opt/switchbot
sudo git pull
sudo mise install
sudo mise exec -- uv sync --no-dev
sudo systemctl restart switchbot_scan.service
```

`mise.toml` の Python のバージョンを上げたときは、`.venv` を削除してから `mise exec -- uv sync --no-dev` を実行し、新しい Python で作り直してください。

## Windows で使う

Windows では、Bluetooth アダプターがあれば同じプログラムで受信できます。PowerShell で次のように実行します。

```powershell
winget install jdx.mise
git clone https://github.com/DISK0401/switchot_scan.git
cd switchot_scan
mise trust; mise install
mise exec -- uv sync --no-dev
Copy-Item .env.example .env; Copy-Item devices.example.toml devices.toml   # それぞれ記入する
mise exec -- uv run python switchbot_scan.py --dry-run --once
```

Windows には systemd がないため、ウォッチドッグで終了したあとに起動し直す処理を自分で用意します。たとえば、次の内容の PowerShell スクリプトを、タスク スケジューラで「ログオン時」に実行するよう登録します。

```powershell
Set-Location C:\path\to\switchot_scan
while ($true) {
    .\.venv\Scripts\python.exe switchbot_scan.py
    Start-Sleep -Seconds 10
}
```

## 開発

```bash
mise install                  # Python と uv を用意
mise exec -- uv sync          # テスト用ライブラリも含めて用意
mise exec -- uv run pytest    # アドバタイズデータの解析処理をテスト
```

依存ライブラリのバージョンは `uv.lock` で固定しています。更新するときは `mise exec -- uv lock --upgrade` を実行し、テストが通ることを確認してからコミットしてください。
