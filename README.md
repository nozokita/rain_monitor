## 三島駅周辺降水予測通知システム — README

> 目的
> 気象庁「高解像度降水ナウキャスト」の PNG タイル（APIキー不要）を解析し、0/15/30/60分先の降水強度をミニマムな運用コストで監視します。複数地点・地点別の閾値/通知先に対応し、Windows では Outlook を使って閾値超過時にメール通知します。UI は Streamlit。

---

### 1. システム概要

- 複数地点監視: UI から自由に追加/削除・各地点で閾値/宛先/通知ON/OFF を設定
- 監視間隔: 既定 3 分（設定可）。UI トグルでワーカー自動 起動/停止（PID管理）
- 参照先: 0/15/30/60 分先。0分は N1、15/30/60分は N2 の targetTimes を使用（5分刻みで丸め）
- 表示解像度の目安: 0–30分 ≈ 約250m、35–60分 ≈ 約1km（5分更新）
- 解析手法（代表値）: 2×2 最大（メイン表示）、8×8 最大（参考表示）
- 通知: Windowsの Outlook（COM）でメール送信。宛先は複数可（カンマ/セミコロン/空白区切り）
- 死活監視: 既定 9:00/17:00 で稼働通知（時刻は設定可、宛先も複数可）
- デバッグ画像: 解析エリアを重ね描画して `debug_images/` に保存。UI（タブ3）からプレビュー/ダウンロード
- 自動クリーンアップ: `debug_images/` の保持期間・上限ファイル数・総容量を自動で抑制
- ログ制御: `[DEBUG]` は出力抑止済み。`[WARN]` は設定で抑制可

利用データソース（APIキー不要）

- 気象庁 降水ナウキャスト PNG タイル
  - 例: `https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/{validtime}/surf/hrpns/{z}/{x}/{y}.png`
  - 5分更新 / 最大1時間先 / PNGのパレット値（インデックス）で降水強度を表現

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
│    ├ PNGタイル取得 → パレット値（0–65）/RGB から mm/h 推定
│    ├ 0/15/30/60分先の降水量をログ出力（地点名別）
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
│    └ ログ確認 / 0/15/30/60分先のメトリクス（2×2最大）
│       ＋ ログから検出したデバッグ画像をプレビュー/ダウンロード
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

主な依存（詳細は `requirements.txt` を参照）:
- streamlit, requests, Pillow, numpy, matplotlib
- pywin32（Windows のみ）

2) 設定ファイル（`config.json` の例）

```json
{
  "locations": [
    {
      "name": "三島駅",
      "lat": 35.126474871810345,
      "lon": 138.91109391000256,
      "heavy_rain": 30,
      "torrential_rain": 50,
      "email_to": "alert@example.com, ops@example.com",
      "notification_enabled": true
    }
  ],
  "monitoring": { "enabled": false, "interval_minutes": 3, "lead_minutes": 60 },
  "heartbeat": { "enabled": true, "times": ["09:00", "17:00"] },
  "debug_images": { "retention_hours": 12, "max_files": 500, "max_total_mb": 200 },
  "log": { "suppress_warn": false },
  "debug": false
}
```

補足
- メール宛先は複数可（カンマ/セミコロン/空白で区切り）。UI の「？」にも同様のヘルプを表示
- 死活監視メールは、通知有効かつメールが設定されている地点の宛先を集約して送信

3) 初回起動

```bash
# UI（任意）: 設定編集・ログ確認・手動実行
streamlit run app.py

# 監視の常駐実行（別ターミナル）
python monitor.py
```

手動チェック: `python monitor.py --once`

デバッグ取得（画像保存・解析強化）: `python monitor.py --debug` 実行後、`debug_images/` を `check_tile.py` で可視化可能

---

### 6. しきい値・降水推定・アラート判定

- 画像モード
  - Pモード: パレットインデックス（0–65）をそのまま step として利用（α=0 は無降水）
  - 非Pモード: 色が階級と一致しにくいため、有無を 1/0 として扱い
- mm/h への変換（優先度）
  1) RGB 近傍一致で JMA の色階級にマッチ → 代表値（上限側）を採用（例: 5–10 → 10.0）
  2) フォールバックで step→mm/h（上限側丸め: 0–1→1.0, 1–5→5.0, 5–10→10.0, 10–20→20.0, 20–30→30.0, 30–50→50.0, 50–80→80.0, 80+は拡張値）
- 空間集計メソッド
  - single（参考/デバッグ）、max_2x2（メイン）、max_4x4、max_8x8（参考）
  - 中心ピクセル位置に黒い十字、各窓枠は色分け（2×2=マゼンタ、3×3=シアン、4×4=緑、8×8=青、太さ4px）
- 閾値例（`config.json`）
  - heavy_rain: 30 mm/h
  - torrential_rain: 50 mm/h
- 判定: 取得時刻の推定JSTにおける降水量が閾値を超えたら通知

備考（WARN について）
- ログの `[WARN] 色推定とステップ推定に乖離` は、中心色と周辺最大（max系）で評価点が異なるために生じる情報メッセージです
- 判定に使う値は上記優先度で一意に決まるため、WARN が出ても通知判定は変わりません（必要に応じて抑制可）

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
- デバッグ画像は `debug_images/` に保存。タブ3でプレビュー/ダウンロード可、`check_tile.py` で追加解析
- `debug_images` は保持期間/上限ファイル数/総容量を自動で抑制（`debug_images` 設定参照）

---

### 10. UI（Streamlit）

- 設定: 複数地点の追加/削除、間隔・閾値・宛先（複数可）・予測リードタイムの編集/保存
- 手動実行: 「今すぐチェック」ボタン → `python monitor.py --once`
- 状態: 0/15/30/60分先の降水量メトリクス（代表=2×2最大、参考=8×8最大）、最新ログ表示
- ログ: ログ本文 + ログから検出したデバッグ画像のプレビュー/ダウンロード

起動: `streamlit run app.py`（既定 `http://localhost:8501`）

---

### 11. 代表的なエラーと対処

| 症状 | 原因 | 対処 |
|---|---|---|
| タイル404/取得失敗 | basetime/validtimeとURLパターン差異 | 自動で複数URLを試行。改善しない場合は再実行/時間をおいて再取得 |
| αチャンネルが0のみ | 降水なし/位置ズレ | `zoom` を10固定、座標が正しいか確認。`--debug` で画像保存し `check_tile.py` で確認 |
| Outlook送信エラー | オフライン/ポリシー制限 | オンライン化/IT管理者へ自動送信許可の確認 |
| 送信されない | macOS/Linux | 設計通り。Windowsで実行、またはSMTP送信の実装を追加 |
| デバッグ画像が増え続ける | 長期運用 | 既定で自動削除します。保持条件は `debug_images` 設定で調整 |

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


