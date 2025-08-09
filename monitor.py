#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
monitor.py – JMAはポイント数値APIは未提供、PNG解析でmm/h推定
"""

import json, os, sys, time, math, requests
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image
import base64
import atexit
import signal

# Windows Outlook メール送信
try:
    import win32com.client
    WINDOWS_EMAIL = True
except ImportError:
    WINDOWS_EMAIL = False

# α(0-255) → mm/h 変換表
STEP_TO_MM = {a: a for a in range(1, 61)}
STEP_TO_MM.update({61: 80, 62: 100, 63: 150, 64: 200, 65: 300})


class JMANowcastAPI:
    """PNG タイルαチャンネルから降水強度を取得（デバッグ機能付き）"""

    BASE = "https://www.jma.go.jp/bosai/jmatile/data/nowc"

    def __init__(self, zoom: int = 10, debug: bool = False):
        self.zoom = zoom
        self.debug = debug
        self._target_json = f"{self.BASE}/targetTimes_N1.json"
        if self.debug:
            log_message(f"[DEBUG] targetTimes URL: {self._target_json}")

    def _deg2tile(self, lat: float, lon: float):
        """緯度経度 → タイル座標"""
        z = self.zoom
        lat_rad = math.radians(lat)
        n = 2.0**z
        xtile = int((lon + 180.0) / 360.0 * n)
        ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
        return xtile, ytile

    def _pixel_in_tile(self, lat: float, lon: float):
        """タイル内ピクセル位置"""
        z = self.zoom
        lat_rad = math.radians(lat)
        n = 2.0**z
        x_f = (lon + 180.0) / 360.0 * n
        y_f = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        return int((x_f - int(x_f)) * 256), int((y_f - int(y_f)) * 256)

    def _latest_times(self, target_offset_minutes: int = 0):
        """利用可能な時刻から basetime と validtime を決定。
        - target_offset_minutes を 5 分単位に丸めて validtime を決定（0〜60 分にクリップ）。
        - targetTimes_N1.json が {basetime, validtime} を返す場合は最も近い validtime を選択。
        - 文字列リスト（basetime のみ）の場合は「最新の basetime + 丸めたオフセット」で validtime を合成。
        """
        try:
            response = requests.get(self._target_json, timeout=10)
            data = response.json()
            
            if not data:
                raise ValueError("targetTimes_N1.json が空です")
            
            # 現在時刻（JST）
            now_jst = datetime.now()
            # オフセットは 0〜60 に制限、かつ 5 分単位へ丸め
            clamped = max(0, min(60, int(round(target_offset_minutes / 5) * 5)))
            target_jst = now_jst + timedelta(minutes=clamped)
            log_message(f"現在時刻(JST): {now_jst.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 最も現在時刻に近いデータを探す
            best_data = None
            min_diff = float('inf')
            
            if isinstance(data[0], dict):
                # validtime を持つ形式
                for item in data:
                    vt_str = item["validtime"]
                    vt = datetime.strptime(vt_str, "%Y%m%d%H%M%S")
                    vt_jst = vt + timedelta(hours=9)
                    diff = abs((vt_jst - target_jst).total_seconds())
                    if diff < min_diff:
                        min_diff = diff
                        best_data = item
                if isinstance(best_data, dict):
                    bt_str = best_data["basetime"]
                    vt_str = best_data["validtime"]
                else:
                    # 念のためのフォールバック
                    vt_str = best_data
                    bt_str = vt_str
                vt = datetime.strptime(vt_str, "%Y%m%d%H%M%S")
                vt_jst = vt + timedelta(hours=9)
            else:
                # 文字列（basetime のみ）形式 → 最新の basetime を選択し、丸めたオフセットで validtime を合成
                # データは新しい順である前提（異なる場合は max() でも良い）
                bt_str = data[0]
                bt = datetime.strptime(bt_str, "%Y%m%d%H%M%S")
                bt_jst = bt + timedelta(hours=9)
                vt_jst = bt_jst + timedelta(minutes=clamped)
                vt = bt + timedelta(minutes=clamped)
                vt_str = vt.strftime("%Y%m%d%H%M%S")
                min_diff = abs((vt_jst - target_jst).total_seconds())
            
            log_message(f"選択データ: basetime={bt_str}, validtime={vt_str}")
            log_message(f"データ時刻(JST推定): {vt_jst.strftime('%Y-%m-%d %H:%M:%S')}")
            log_message(f"目標: {target_jst.strftime('%Y-%m-%d %H:%M:%S')} / 時差: {min_diff/60:.1f}分")
            
            return bt_str, vt_str
            
        except Exception as e:
            log_message(f"[ERROR] 時刻取得エラー: {e}")
            raise

    def _fetch_tile_png(self, basetime, validtime, x, y):
        """タイルをダウンロード（デバッグ情報付き）"""
        
        # 複数のURLパターンを試行
        url_patterns = [
            f"{self.BASE}/{basetime}/none/{validtime}/surf/hrpns/{self.zoom}/{x}/{y}.png",
            f"{self.BASE}/{basetime}/{validtime}/surf/hrpns/{self.zoom}/{x}/{y}.png",
            f"{self.BASE}/{basetime}/none/{validtime}/surf/rasrf/{self.zoom}/{x}/{y}.png",
        ]
        
        for url in url_patterns:
            try:
                if self.debug:
                    log_message(f"[DEBUG] 試行URL: {url}")
                
                r = requests.get(url, timeout=10)
                
                if r.status_code == 200:
                    img = Image.open(BytesIO(r.content))
                    
                    # デバッグ: 画像情報を出力
                    if self.debug:
                        log_message(f"[DEBUG] 画像サイズ: {img.size}")
                        log_message(f"[DEBUG] 画像モード: {img.mode}")
                        log_message(f"[DEBUG] データサイズ: {len(r.content)} bytes")
                        
                        # 必要に応じて RGBA に変換して統計を取得
                        img_for_stats = img.convert('RGBA') if img.mode != 'RGBA' else img

                        # 画像を保存（デバッグ用）
                        debug_dir = "debug_images"
                        os.makedirs(debug_dir, exist_ok=True)
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        debug_path = f"{debug_dir}/tile_{timestamp}.png"
                        img.save(debug_path)
                        log_message(f"[DEBUG] 画像を保存: {debug_path}")
                        
                        # アルファチャンネルの統計情報（Pモード等でもRGBAに変換して取得）
                        alpha = img_for_stats.getchannel('A')
                        alpha_data = list(alpha.getdata())
                        non_zero = [v for v in alpha_data if v > 0]
                        if non_zero:
                            log_message(f"[DEBUG] アルファ値: min={min(non_zero)}, max={max(non_zero)}, 非ゼロピクセル数={len(non_zero)}")
                        else:
                            log_message(f"[DEBUG] アルファチャンネルは全て0（降水なし）")
                    
                    return img, url
                    
                elif r.status_code == 404:
                    if self.debug:
                        log_message(f"[DEBUG] 404 Not Found: {url}")
                    continue
                else:
                    log_message(f"[WARNING] HTTP {r.status_code}: {url}")
                    
            except Exception as e:
                if self.debug:
                    log_message(f"[DEBUG] 取得エラー: {e}")
                continue
        
        raise requests.exceptions.HTTPError(f"すべてのURLでタイル取得失敗")

    def rainfall_mm(self, lat: float, lon: float, lead_minutes: int = 0):
        """指定地点の降水量を取得"""
        try:
            self.lat = lat
            self.lon = lon
            
            basetime, validtime = self._latest_times(target_offset_minutes=lead_minutes)
            xt, yt = self._deg2tile(lat, lon)
            px, py = self._pixel_in_tile(lat, lon)
            
            log_message(f"座標情報: lat={lat:.6f}, lon={lon:.6f}")
            log_message(f"タイル: zoom={self.zoom}, x={xt}, y={yt}, pixel=({px},{py})")
            
            img, png_url = self._fetch_tile_png(basetime, validtime, xt, yt)
            
            # 強度ステップの取得: JMAの hrpns タイルは通常 'P'（パレット）モードで、
            # ピクセル値が 0..65 のステップを表す。0 は降水なし。
            if img.mode == 'P':
                step = img.getpixel((px, py))  # 0..255 のうち 0..65 を使用
                if self.debug:
                    log_message(f"[DEBUG] パレットインデックス(step)={step}")
            else:
                # フォールバック: RGBA のアルファで有無のみ判断
                if self.debug:
                    log_message("[DEBUG] 非PモードのためRGBAに変換して有無判定")
                img_rgba = img.convert('RGBA')
                a = img_rgba.getchannel('A').getpixel((px, py))
                step = 0 if a == 0 else 1
            
            mmh = STEP_TO_MM.get(step, 0)
            
            vt = datetime.strptime(validtime, "%Y%m%d%H%M%S")
            vt_jst = vt + timedelta(hours=9)  # JST変換
            
            log_message(f"降水強度: step={step} → {mmh} mm/h")
            
            # 確認用のWebページURLも生成
            web_url = f"https://www.jma.go.jp/bosai/nowc/#zoom:{self.zoom}/lat:{lat}/lon:{lon}/colordepth:normal/elements:hrpns"
            log_message(f"確認用URL: {web_url}")
            
            return mmh, vt_jst, png_url
            
        except Exception as e:
            log_message(f"[ERROR] 降水量取得エラー: {e}")
            return 0.0, datetime.now(), "N/A"


def load_config():
    try:
        with open("config.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "location": {
                "name": "三島駅",
                "lat": 35.126474871810345,
                "lon": 138.91109391000256,
            },
            "monitoring": {"enabled": False, "interval_minutes": 3},
            "thresholds": {"heavy_rain": 30, "torrential_rain": 50},
            "notification": {"email_to": "", "enabled": True},
            "heartbeat": {"enabled": True, "times": ["09:00", "17:00"]},
            "debug": False  # デバッグモード設定
        }


def log_message(msg: str):
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with open("logs/monitor.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def send_outlook_email(to_addr, subject, body):
    try:
        outlook = win32com.client.Dispatch("outlook.application")
        mail = outlook.CreateItem(0)
        mail.To, mail.Subject, mail.Body = to_addr, subject, body
        mail.Send()
        log_message(f"メール送信成功: {to_addr}")
        return True
    except Exception as e:
        log_message(f"[ERROR] メール送信エラー: {e}")
        return False


def send_email(to_addr, subject, body):
    if WINDOWS_EMAIL:
        return send_outlook_email(to_addr, subject, body)
    log_message("メール送信スキップ（非Windows環境）")
    return False


def maybe_send_heartbeat(cfg):
    """指定した時刻（HH:MM, JST）に日次の死活通知を送信。1分粒度で判定。"""
    try:
        hb = cfg.get("heartbeat", {})
        if not hb or not hb.get("enabled", False):
            return
        times = hb.get("times", []) or []
        if not (cfg.get("notification", {}).get("enabled") and cfg.get("notification", {}).get("email_to")):
            return
        now = datetime.now()
        current = now.strftime("%H:%M")
        if current in times:
            subj = f"【死活監視】雨監視システム稼働中 - {now:%Y/%m/%d %H:%M}"
            body = (
                f"システムは稼働中です。\n\n"
                f"時刻: {now:%Y/%m/%d %H:%M}\n"
                f"監視地点数: {len(cfg.get('locations') or [cfg.get('location')])}\n"
                f"間隔: {cfg.get('monitoring',{}).get('interval_minutes', 3)} 分\n"
            )
            # 簡易的な重複送信抑止（同分内の多重送信を避ける）
            stamp = now.strftime("%Y%m%d%H%M")
            flag_file = os.path.join("logs", f"heartbeat_{stamp}.flag")
            os.makedirs("logs", exist_ok=True)
            if not os.path.exists(flag_file):
                send_email(cfg["notification"]["email_to"], subj, body)
                open(flag_file, "w").close()
    except Exception as e:
        log_message(f"[ERROR] ハートビート送信エラー: {e}")


def check_and_notify():
    """降水量チェックと通知"""
    try:
        cfg = load_config()
        heavy, torrential = cfg["thresholds"]["heavy_rain"], cfg["thresholds"]["torrential_rain"]
        debug_mode = cfg.get("debug", False)
        lead_minutes = cfg.get("monitoring", {}).get("lead_minutes", 60)

        # 死活通知（必要な分だけ送信）
        maybe_send_heartbeat(cfg)

        # APIインスタンス作成（デバッグモード対応）
        api = JMANowcastAPI(zoom=10, debug=debug_mode)
        log_message(f"予測オフセット: {lead_minutes} 分先を参照")

        # 互換: locations がなければ location を1件として扱う
        locations = []
        if isinstance(cfg.get("locations"), list) and cfg["locations"]:
            locations = cfg["locations"]
        elif isinstance(cfg.get("location"), dict):
            locations = [cfg["location"]]
        else:
            locations = [{"name": "三島駅", "lat": 35.126474871810345, "lon": 138.91109391000256}]

        for loc in locations:
            try:
                loc_name = loc.get("name", "(無名)")
                lat = float(loc["lat"])
                lon = float(loc["lon"])
            except Exception:
                continue

            rain, vt_jst, png_url = api.rainfall_mm(lat, lon, lead_minutes=lead_minutes)

            # 0/15/30/60 分先の一覧をログ出力
            try:
                leads_preview = [0, 15, 30, 60]
                preview_results = []
                for lm in leads_preview:
                    r, t_jst, _ = api.rainfall_mm(lat, lon, lead_minutes=lm)
                    preview_results.append((lm, r, t_jst))
                msg = f"【地点: {loc_name}】時刻別降水量: " + ", ".join(
                    [f"{('現在' if lm==0 else str(lm)+'分後')} {r:.1f}mm/h({t.strftime('%H:%M')})" for lm, r, t in preview_results]
                )
                log_message(msg)
            except Exception:
                pass

            if png_url == "N/A":
                log_message(f"[WARNING] [{loc_name}] データ取得に失敗しました。次回再試行します。")
                continue

            log_message(f"[{loc_name}] タイルURL: {png_url}")
            log_message(f"[{loc_name}] 降水量 ({vt_jst.strftime('%H:%M')} JST): {rain:.1f} mm/h")

            # 閾値判定
            level = "豪雨" if rain >= torrential else "大雨" if rain >= heavy else None
            if not level:
                log_message(f"[{loc_name}] 異常なし")
                continue

            # 通知
            if cfg["notification"]["enabled"] and cfg["notification"]["email_to"]:
                subj = f"【{level}警報】{loc_name}周辺 - {datetime.now():%m/%d %H:%M}"
                body = (
                    f"{loc_name} 周辺で {level} が予測されています。\n\n"
                    f"降水量 ({vt_jst.strftime('%H:%M')} JST): {rain:.1f} mm/h\n"
                    f"警報レベル: {level}\n"
                    f"タイルURL: {png_url}\n"
                    f"確認時刻: {datetime.now():%Y/%m/%d %H:%M}\n\n"
                    "データソース: 気象庁降水ナウキャスト"
                )
                send_email(cfg["notification"]["email_to"], subj, body)
            log_message(f"[{loc_name}] {level}検知（{'通知' if cfg['notification']['enabled'] else '通知なし'}）")
        
    except Exception as e:
        log_message(f"[ERROR] チェック処理エラー: {e}")


def main():
    # コマンドライン引数処理
    if len(sys.argv) > 1:
        if sys.argv[1] == "--once":
            check_and_notify()
            return
        elif sys.argv[1] == "--debug":
            # デバッグモードで1回実行
            cfg = load_config()
            cfg["debug"] = True
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            check_and_notify()
            return

    # PID ファイル作成
    try:
        with open("monitor.pid", "w") as pf:
            pf.write(str(os.getpid()))
    except Exception:
        pass

    def _cleanup_pid(*_):
        try:
            if os.path.exists("monitor.pid"):
                os.remove("monitor.pid")
        except Exception:
            pass

    atexit.register(_cleanup_pid)
    try:
        signal.signal(signal.SIGTERM, _cleanup_pid)
    except Exception:
        pass

    log_message("監視ワーカー起動")
    while True:
        try:
            cfg = load_config()
            if cfg["monitoring"]["enabled"]:
                check_and_notify()
                time.sleep(cfg["monitoring"]["interval_minutes"] * 60)
            else:
                log_message("監視停止中")
                time.sleep(60)
        except KeyboardInterrupt:
            log_message("監視ワーカー終了")
            break
        except Exception as e:
            log_message(f"[ERROR] メインループエラー: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()