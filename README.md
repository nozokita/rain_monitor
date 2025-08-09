## 三島駅周辺降水予測通知システム — README

> 目的
> 三島駅周辺（35.126474871810345, 138.91109391000256）で、1時間先相当の降水状況を3分ごとに監視し、閾値超過時にOutlookでメール通知する最小構成の監視システム。気象庁の降水ナウキャスト PNG タイル（APIキー不要）を用い、画像のパレットインデックス（0–65）から降水強度を mm/h に変換して推定します。

---

### 1. システム概要

- 監視地点: 三島駅（緯度 35.126474871810345、経度 138.91109391000256）ほか、複数地点に対応（UIで追加/削除）
- 監視間隔: 既定 3 分（設定で変更可）
- 監視対象:
  - 気象庁 降水ナウキャスト PNG タイルの 0/15/30/60 分先（5分刻み丸め）の降水強度
  - パレット値を降水量に変換（実装内の変換表に基づく）
- 通知方法: Windows の Outlook（COM 経由で自動送信）
- UI: Streamlit（設定の編集、状態確認、手動チェック）
- 死活監視: 毎日 09:00/17:00 の稼働通知（時刻は設定で変更可）
- 費用: 0円（PC電力・回線以外）

利用データソース（APIキー不要）

- 気象庁 降水ナウキャスト PNG タイル
  - 代表例: `https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/{z}/{x}/{y}.png`
  - 5分更新 / 最大1時間先 / 画像 PNG のパレット値（インデックス）で降水強度ステップを表現

---

### 2. アーキテクチャ

```
┌────────────────────────────┐
│  Windows PC（常時稼働推奨）       │
│  - Python 3.11+               │
│  - Outlook（M365/Exchange可）   │
│────────────────────────────│
│ ① ワーカー（monitor.py）         │ ← 3分ごと起動/常駐
│    ├ JMA targetTimes_N1.json から basetime/validtime 決定（5分刻み）
│    ├ PNGタイル取得 → パレット値（0–65）→ mm/h へ変換
│    ├ 0/15/30/60分先の降水量をログ出力
│    ├ 閾値判定 → 新規検知時のみ ② へ
│    ├ 09:00/17:00 に死活通知（設定可）
│    └ logs/monitor.log へ記録 & monitor.pid にPID出力
│
│ ② Outlook 通知（win32com.client）
│    └ 宛先メールへ送信
│
│ ③ Streamlit UI（app.py）
│    ├ 設定編集（config.json）/ 複数地点の追加・削除
│    ├ 監視トグルONでワーカー自動起動（PID管理）/ OFFで停止
│    ├ 手動チェック（--once 実行）
│    └ ログ確認 / 0/15/30/60分先をメトリクス表示
└────────────────────────────┘
```

---

### 3. リポジトリ構成

```
rain-monitor/
├── app.py            # Streamlit UI（設定・状態と手動実行、複数地点・ワーカー起動/停止）
├── monitor.py        # 監視・判定・通知ロジック（JMAナウキャスト、死活通知、PID管理）
├── check_tile.py     # デバッグ画像の解析・可視化ツール
├── config.json       # 設定（地点、閾値、通知先、間隔）
├── requirements.txt  # 依存ライブラリ
├── logs/
│   └── monitor.log   # 実行ログ（自動生成）
└── debug_images/     # 取得PNGと解析画像の保存先
```

---

### 4. 動作環境

- OS: Windows 10/11（Outlook COM を使用）
- 必須: Microsoft Outlook（送信可能なプロファイルがセットアップ済み）
- Python: 3.11 以上推奨
- ネットワーク: 外部HTTPSへアクセス可能

macOS や Linux でもプログラムは動作しますが、Outlook 送信（COM）は無効化され、ログのみ記録されます。

---

### 5. セットアップ

1) 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

`requirements.txt`

```
streamlit==1.29.0
requests==2.31.0
Pillow==10.3.0
pywin32==306; sys_platform == 'win32'
pyproj==3.6.1
```

2) 設定ファイル（`config.json`）

```json
{
  "location": {
    "name": "三島駅",
    "lat": 35.126474871810345,
    "lon": 138.91109391000256
  },
  "monitoring": {
    "enabled": false,
    "interval_minutes": 3
  },
  "thresholds": {
    "heavy_rain": 30,
    "torrential_rain": 50
  },
  "notification": {
    "email_to": "",
    "enabled": true
  },
  "debug": false
}
```

3) 初回起動

```bash
# UI（任意）: 設定編集・ログ確認・手動実行
streamlit run app.py

# 監視の常駐実行（別ターミナル）
python monitor.py
```

手動チェック: `python monitor.py --once`

デバッグ取得（画像保存・解析強化）: `python monitor.py --debug` 実行後、`debug_images/` を `check_tile.py` で可視化可能。

---

### 6. しきい値とアラート判定

- 降水強度は PNG αチャンネル値（0–65）を mm/h に変換
  - 1–60 → 1–60 mm/h（線形）
  - 61→80, 62→100, 63→150, 64→200, 65→300 mm/h（実装内テーブル）
- 閾値例（`config.json`）
  - heavy_rain: 30 mm/h
  - torrential_rain: 50 mm/h
- 判定: 取得時刻の推定JSTにおける降水量が閾値を超えたら通知

注: 再通知間隔は本実装では固定管理していません。必要なら `monitor.py` に再通知抑止ロジック（最終通知時刻の保持）を追加してください。

---

### 7. Outlook メール送信

- 前提: Outlook がセットアップ済み（送信可能プロファイル）
- 実装: `win32com.client` で MAPI 経由送信（Windows のみ有効）
- macOS/Linux: 送信はスキップされ、ログに記録されます

トラブル時チェック:

- Outlook が資格情報ダイアログを表示 → 一度手動送信して資格情報をキャッシュ
- 組織ポリシーでプログラム送信が制限 → IT 管理者へ許可設定の確認
- オフラインモード → 送信トレイ滞留（オンライン復帰で送信）

---

### 8. Windows タスクスケジューラ（任意）

常駐ではなく 3 分ごと単発実行にする場合:

```powershell
$PY = "C:\\Python312\\python.exe"
$APP = "C:\\rain-monitor\\monitor.py"
schtasks /Create /TN "MishimaRainWatch" /SC MINUTE /MO 3 /TR "$PY $APP --once" /RL HIGHEST /F
```

- 開始フォルダ: リポジトリ直下（`config.json` と `logs/` 参照のため）

---

### 9. ログ／監査

- `logs/monitor.log` に時刻・判定結果・送信結果を追記
- `app.py` から最近のログ閲覧/クリア可
- デバッグ画像は `debug_images/` に保存。`check_tile.py` で統計と可視化（解析PNGも併せて保存）

---

### 10. UI（Streamlit）

- 設定: 複数地点の追加/削除、間隔・閾値・宛先・予測リードタイムの編集/保存
- 手動実行: 「今すぐチェック」ボタン → `python monitor.py --once`
- 状態: 0/15/30/60分先の降水量メトリクス、最新ログ表示

起動: `streamlit run app.py`（既定 `http://localhost:8501`）

---

### 11. 代表的なエラーと対処

| 症状 | 原因 | 対処 |
|---|---|---|
| タイル404/取得失敗 | basetime/validtimeとURLパターン差異 | 自動で複数URLを試行。改善しない場合は再実行/時間をおいて再取得 |
| αチャンネルが0のみ | 降水なし/位置ズレ | `zoom` を10固定、座標が正しいか確認。`--debug` で画像保存し `check_tile.py` で確認 |
| Outlook送信エラー | オフライン/ポリシー制限 | オンライン化/IT管理者へ自動送信許可の確認 |
| 送信されない | macOS/Linux | 設計通り。Windowsで実行、またはSMTP送信の実装を追加 |

---

### 12. 法令・利用条件

- 気象業務法: 社内向け運用通知を目的とし、独自の「予報」を対外配信しない
- クレジット: UIなどに「データ出典：JMA」を明記推奨
- アクセス: 公開APIのレート/利用規約を遵守

---

### 13. 拡張案

- 再通知抑止: 最終通知から一定時間は同レベルの通知を抑止
- 複数地点監視: 地点リストをループ（レート制限に注意）
- 地図可視化: Nowcast PNG を地図に重畳（Leaflet/MapLibre）
- 代替通知: Slack/Teams Webhook や SMTP を追加（Windows外でも通知可能）

---

### 14. ランブック（最短手順）

1. `pip install -r requirements.txt`
2. `config.json` の宛先を設定
3. `streamlit run app.py` で閾値・間隔を確認/保存
4. `python monitor.py --once` で試験実行（必要に応じて `--debug`）
5. 問題なければ `python monitor.py` で常駐、またはタスクスケジューラ登録

---

連絡先/メモ

- 運用相談・閾値調整: （社内担当名を記載）
- 台風・線状降水帯等の可能性が高い場合は閾値を一時的に引き下げて運用可


