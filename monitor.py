#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
monitor.py – JMAはポイント数値APIは未提供、PNG解析でmm/h推定
  - 透明(α=0)=無降水の厳密判定
  - JMA配色階級に丸める jma_bins 変換
  - RGB→階級の色マッチ（±2）を追加（将来変更に強い）
  - targetTimesの60秒キャッシュ
  - requests.Session + リトライ/UA
  - プレビュー(0/15/30/60分)は同一の basetime/validtime セットで固定
  - メッシュサイズは緯度補正した m/pixel を表示
"""

import json, os, sys, time, math
from datetime import datetime, timedelta
from io import BytesIO
from typing import Tuple, Optional, Dict, Any

import requests
from requests.adapters import HTTPAdapter
try:
    # urllib3>=1.26
    from urllib3.util.retry import Retry
except Exception:
    Retry = None

from PIL import Image, ImageDraw
import atexit
import signal

# ───────────────────────────────────────────
# Windows Outlook メール送信
try:
    import win32com.client
    WINDOWS_EMAIL = True
except ImportError:
    WINDOWS_EMAIL = False

# ───────────────────────────────────────────
# ステップ値→mm/h 変換（ベース定義）
# identity: 1..60 → そのままmm/h, 61..65は拡張値
STEP_TO_MM_IDENTITY = {a: a for a in range(1, 61)}
STEP_TO_MM_IDENTITY.update({61: 80, 62: 100, 63: 150, 64: 200, 65: 300})

def convert_step_to_mmh(step: int, mapping_mode: str = "identity") -> float:
    """ステップ値(0..65)を mm/h に変換。
    mapping_mode:
      - identity: 1..60 → そのままmm/h, 61..65 → {80,100,150,200,300}
      - jma_bins: 気象庁の色階級に丸める（代表値）
          透明/0以下 → 0.0
          0–1 → 0.5
          1–5 → 3.0
          5–10 → 7.5
          10–20 → 15.0
          20–30 → 25.0
          30–50 → 40.0
          50–80 → 65.0
          80以上 → ステップ拡張値（80/100/150/200/300）を維持
    """
    if step is None or step <= 0:
        return 0.0

    mmh_identity = float(STEP_TO_MM_IDENTITY.get(step, 0.0))
    if mapping_mode == "identity":
        return mmh_identity

    m = mmh_identity
    if m <= 0.0:  return 0.0
    if m <= 1.0:  return 1.0
    if m <= 5.0:  return 5.0
    if m <= 10.0: return 10.0
    if m <= 20.0: return 20.0
    if m <= 30.0: return 30.0
    if m <= 50.0: return 50.0
    if m <= 80.0: return 80.0
    # 80以上は情報保持
    return m

# ───────────────────────────────────────────
# JMA配色（RGB厳密値）。±2の許容でマッチング
JMA_COLOR_BINS = {
    (242,242,255): (0.0, 1.0, 1.0),
    (160,210,255): (1.0, 5.0, 5.0),
    (33,140,255):  (5.0,10.0,10.0),
    (0,65,255):    (10.0,20.0,20.0),
    (250,245,0):   (20.0,30.0,30.0),
    (255,153,0):   (30.0,50.0,50.0),
    (255,40,0):    (50.0,80.0,80.0),
    (180,0,104):   (80.0, None, None),  # 80以上帯
}

def match_color_to_bin(r:int,g:int,b:int, tol:int=2) -> Optional[float]:
    """RGBをJMA配色に近傍一致させ、代表値(mm/h)を返す。無該当ならNone"""
    for (cr,cg,cb),(lo,hi,rep) in JMA_COLOR_BINS.items():
        if abs(r-cr)<=tol and abs(g-cg)<=tol and abs(b-cb)<=tol:
            # 80以上帯は代表値を返さず None にして step→mm/h に委ねるか、80代表値を返す
            return 80.0 if rep is None else rep
    return None

# ───────────────────────────────────────────
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
            "monitoring": {"enabled": False, "interval_minutes": 3, "lead_minutes": 0},
            "thresholds": {"heavy_rain": 30, "torrential_rain": 50},
            "notification": {"email_to": "", "enabled": True},
            "heartbeat": {"enabled": True, "times": ["09:00", "17:00"]},
            "debug_images": {"retention_hours": 12, "max_files": 500, "max_total_mb": 200},
            "debug": False
        }

def log_message(msg: str):
    # [DEBUG]で始まるメッセージは除外
    if msg.startswith("[DEBUG]"):
        return
    
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with open("logs/monitor.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def prune_debug_images(retention_hours: int = 12, max_files: int = 500, max_total_mb: int = 200):
    """debug_images の古い/多すぎるファイルを削除して容量を抑制する。
    - retention_hours: 何時間前より古いものを優先的に削除
    - max_files: 最大ファイル数
    - max_total_mb: 合計MB上限
    """
    try:
        folder = "debug_images"
        if not os.path.isdir(folder):
            return
        entries = []
        now = time.time()
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
                entries.append({
                    "path": path,
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                })
            except Exception:
                continue

        if not entries:
            return

        # 1) 保持期間を超えた古いファイルを削除
        cutoff = now - (retention_hours * 3600)
        for e in sorted(entries, key=lambda x: x["mtime"])[:]:
            if e["mtime"] < cutoff:
                try:
                    os.remove(e["path"])
                except Exception:
                    pass
                entries.remove(e)

        # 2) 総ファイル数が多い場合、古い順に削除
        if len(entries) > max_files:
            overflow = len(entries) - max_files
            for e in sorted(entries, key=lambda x: x["mtime"])[:overflow]:
                try:
                    os.remove(e["path"])
                except Exception:
                    pass
                entries.remove(e)

        # 3) 合計サイズが上限を超える場合、古い順に削除
        total_bytes = sum(e["size"] for e in entries)
        limit_bytes = max_total_mb * 1024 * 1024
        if total_bytes > limit_bytes:
            for e in sorted(entries, key=lambda x: x["mtime"]):
                try:
                    os.remove(e["path"])
                except Exception:
                    pass
                total_bytes -= e["size"]
                if total_bytes <= limit_bytes:
                    break
    except Exception:
        # ログは肥大させないためエラーは黙殺
        pass

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

# ───────────────────────────────────────────
class JMANowcastAPI:
    """PNG タイルから降水強度を取得（αと色で判定／デバッグ機能付き）"""

    BASE = "https://www.jma.go.jp/bosai/jmatile/data/nowc"

    def __init__(self, zoom: int = 10, debug: bool = False):
        self.zoom = zoom
        self.debug = debug

        # HTTPセッション
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "rain-monitor/1.0 (+github) python-requests"})
        if Retry is not None:
            retries = Retry(total=3, backoff_factor=0.5, status_forcelist=(429,500,502,503,504))
            adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

        # targetTimesキャッシュ
        self._times_cache: Dict[str, Dict[str, Any]] = {}  # {"N1": {"ts":dt, "data":[...]}, "N2":{...}}

        if self.debug:
            log_message(f"[DEBUG] init zoom={self.zoom}")

    # ── 地理座標計算 ──
    def _deg2tile(self, lat: float, lon: float) -> Tuple[int,int]:
        z = self.zoom
        lat_rad = math.radians(lat)
        n = 2.0**z
        xtile = int((lon + 180.0) / 360.0 * n)
        ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
        return xtile, ytile

    def _pixel_in_tile(self, lat: float, lon: float) -> Tuple[int,int]:
        z = self.zoom
        lat_rad = math.radians(lat)
        n = 2.0**z
        x_f = (lon + 180.0) / 360.0 * n
        y_f = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        return int((x_f - int(x_f)) * 256), int((y_f - int(y_f)) * 256)

    def _mpp(self, lat: float) -> float:
        # meters per pixel at latitude
        return 156543.03392 * math.cos(math.radians(lat)) / (2**self.zoom)

    # ── 画像ユーティリティ ──
    @staticmethod
    def _alpha_at(img: Image.Image, x:int, y:int) -> int:
        return (img if img.mode=='RGBA' else img.convert('RGBA')).getchannel('A').getpixel((x,y))

    @staticmethod
    def _rgb_at(img: Image.Image, x:int, y:int) -> Tuple[int,int,int]:
        return (img if img.mode=='RGB' else img.convert('RGB')).getpixel((x,y))

    # ── targetTimes 取得（60秒キャッシュ） ──
    def _get_target_times(self, kind: str):
        # kind: "N1" or "N2"
        url = f"{self.BASE}/targetTimes_{kind}.json"
        now = datetime.utcnow()
        entry = self._times_cache.get(kind)
        if entry and (now - entry["ts"]).total_seconds() < 60:
            return entry["data"]
        if self.debug:
            log_message(f"[DEBUG] fetch targetTimes {kind}: {url}")
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        self._times_cache[kind] = {"ts": now, "data": data}
        return data

    def _latest_times(self, target_offset_minutes: int = 0) -> Tuple[str, str]:
        """basetime と validtime を決定。0分→N1、>0分→N2"""
        try:
            clamped = max(0, min(60, int(round(target_offset_minutes / 5) * 5)))
            kind = "N1" if clamped == 0 else "N2"
            data = self._get_target_times(kind)
            if not data:
                raise ValueError(f"targetTimes_{kind}.json が空です")

            target_jst = datetime.now() + timedelta(minutes=clamped)

            if isinstance(data[0], dict):
                # {"basetime","validtime"} の配列から target に最も近い要素を選ぶ
                def jst_dt(item):
                    return datetime.strptime(item["validtime"], "%Y%m%d%H%M%S") + timedelta(hours=9)
                best = min(data, key=lambda it: abs((jst_dt(it) - target_jst).total_seconds()))
                bt_str = best["basetime"]
                vt_str = best["validtime"]
            else:
                # ["basetime", ...] 形式
                bt_str = data[0]  # 最新想定
                bt = datetime.strptime(bt_str, "%Y%m%d%H%M%S")
                vt = bt + timedelta(minutes=clamped)
                vt_str = vt.strftime("%Y%m%d%H%M%S")

            if self.debug:
                vt_dbg = datetime.strptime(vt_str, "%Y%m%d%H%M%S") + timedelta(hours=9)
                log_message(f"[DEBUG] basetime={bt_str}, validtime={vt_str} (JST {vt_dbg:%Y-%m-%d %H:%M:%S}) target={target_jst:%H:%M}")
            return bt_str, vt_str

        except Exception as e:
            log_message(f"[ERROR] 時刻取得エラー: {e}")
            raise

    # ── PNGタイル取得 ──
    def _fetch_tile_png(self, basetime, validtime, x, y) -> Tuple[Image.Image, str]:
        url_patterns = [
            f"{self.BASE}/{basetime}/none/{validtime}/surf/hrpns/{self.zoom}/{x}/{y}.png",
            f"{self.BASE}/{basetime}/{validtime}/surf/hrpns/{self.zoom}/{x}/{y}.png",
            f"{self.BASE}/{basetime}/none/{validtime}/surf/rasrf/{self.zoom}/{x}/{y}.png",
        ]
        last_err = None
        for url in url_patterns:
            try:
                if self.debug:
                    log_message(f"[DEBUG] 試行URL: {url}")
                r = self.session.get(url, timeout=10)
                if r.status_code == 200:
                    img = Image.open(BytesIO(r.content))
                    if self.debug:
                        img_for_stats = img.convert('RGBA') if img.mode != 'RGBA' else img
                        alpha = img_for_stats.getchannel('A')
                        alpha_data = list(alpha.getdata())
                        non_zero = [v for v in alpha_data if v > 0]
                        if non_zero:
                            log_message(f"[DEBUG] 画像: mode={img.mode} size={img.size} α(min,max,count)={min(non_zero)},{max(non_zero)},{len(non_zero)}")
                        else:
                            log_message(f"[DEBUG] 画像: αすべて0（降水なし）")
                    return img, url
                elif r.status_code == 404:
                    if self.debug:
                        log_message(f"[DEBUG] 404 Not Found: {url}")
                    continue
                else:
                    last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
                if self.debug:
                    log_message(f"[DEBUG] 取得エラー: {e}")
                continue
        raise requests.exceptions.HTTPError(f"すべてのURLでタイル取得失敗: {last_err}")

    # ── 共通コア ──
    def _calc_step_at(self, img:Image.Image, x:int, y:int) -> int:
        """中心ピクセルの step を返す。α=0→0。Pモードはパレットindex、非Pは有無で1/0。"""
        a = self._alpha_at(img, x, y)
        if a == 0:
            return 0
        if img.mode == 'P':
            return int(img.getpixel((x,y)))
        # 非Pモードは階級情報が失われている前提 → 1 (有降水)
        return 1

    def _calc_step_in_window(self, img:Image.Image, px:int, py:int, size:int) -> int:
        """中心(px,py)にできるだけ揃えた size×size の最大stepを返す（α=0は0）。
        偶数サイズの場合は中心が画素間になるため、(px,py) の±(size//2-1, size//2)で
        可能な限り対称に近い窓を取る。端でははみ出さないように調整。
        """
        w, h = img.width, img.height
        half = size // 2
        start_x = px - (half - 1)
        start_y = py - (half - 1)
        # はみ出し補正
        start_x = max(0, min(start_x, w - size))
        start_y = max(0, min(start_y, h - size))
        steps = []
        for dx in range(size):
            for dy in range(size):
                x = start_x + dx
                y = start_y + dy
                steps.append(self._calc_step_at(img, x, y))
        return max(steps) if steps else 0

    def _calc_color_mmh_at(self, img:Image.Image, x:int, y:int) -> Optional[float]:
        """中心ピクセルのRGBからJMA階級代表値(mm/h)を返す。α=0は0.0"""
        a = self._alpha_at(img, x, y)
        if a == 0:
            return 0.0
        r,g,b = self._rgb_at(img, x, y)
        return match_color_to_bin(r,g,b, tol=2)

    # ── 外部API ──
    def rainfall_mm_at(self, lat: float, lon: float, basetime: str, validtime: str,
                       method: str = "single") -> Tuple[float, datetime, str]:
        """指定 basetime/validtime で降水量取得"""
        # タイル座標とピクセル座標
        xt, yt = self._deg2tile(lat, lon)
        px, py = self._pixel_in_tile(lat, lon)
        mpp = self._mpp(lat)
        mesh_size = f"約{round(mpp)}m/pixel"
            
        if self.debug:
            log_message(f"座標: lat={lat:.6f}, lon={lon:.6f} zoom={self.zoom} x={xt} y={yt} px={px} py={py} {mesh_size}")

        img, png_url = self._fetch_tile_png(basetime, validtime, xt, yt)
            
        # step算出
        original_zoom = self.zoom
        try:
            if method == "high_zoom":
                self.zoom += 1
                xt, yt = self._deg2tile(lat, lon)
                px, py = self._pixel_in_tile(lat, lon)
                img, png_url = self._fetch_tile_png(basetime, validtime, xt, yt)
                step = self._calc_step_at(img, px, py)
            elif method == "average_2x2":
                # 2x2平均→四捨五入
                steps = []
                for dx in (0,1):
                    for dy in (0,1):
                        steps.append(self._calc_step_at(img, min(px+dx, img.width-1), min(py+dy, img.height-1)))
                step = round(sum(steps)/len(steps)) if steps else 0
            elif method == "max_2x2":
                step = self._calc_step_in_window(img, px, py, 2)
            elif method == "max_3x3":
                # 中心-1..+1 の範囲
                steps = []
                for dx in (-1,0,1):
                    for dy in (-1,0,1):
                        x = min(max(px+dx,0), img.width-1)
                        y = min(max(py+dy,0), img.height-1)
                        steps.append(self._calc_step_at(img, x, y))
                step = max(steps) if steps else 0
            elif method == "max_4x4":
                step = self._calc_step_in_window(img, px, py, 4)
            elif method == "max_8x8":
                step = self._calc_step_in_window(img, px, py, 8)
            else:
                step = self._calc_step_at(img, px, py)
        finally:
            # ズームは必ず復帰
            self.zoom = original_zoom

        # 変換（色→階級優先、フォールバックでstep→mm/h）
        mmh_color = self._calc_color_mmh_at(img, px, py)
        mmh_step  = convert_step_to_mmh(step, mapping_mode="jma_bins")
        mmh = mmh_color if (mmh_color is not None) else mmh_step

        vt = datetime.strptime(validtime, "%Y%m%d%H%M%S") + timedelta(hours=9)

        if self.debug:
            log_message(f"[DEBUG] step={step} → stepConv={mmh_step:.1f} mm/h, colorConv={mmh_color if mmh_color is not None else 'None'} → use={mmh:.1f}")
            # デバッグ可視化
            try:
                overlay_img = img.convert('RGBA')
                draw = ImageDraw.Draw(overlay_img)
                line_thickness = 4; cross_half = 6
                def cross(cx,cy,c=(0,0,0,255)):
                    draw.line((cx-cross_half,cy,cx+cross_half,cy), fill=c, width=line_thickness)
                    draw.line((cx,cy-cross_half,cx,cy+cross_half), fill=c, width=line_thickness)
                cross(px,py)
                info = f"{method} px={px},py={py} step={step} ({mmh:.1f}mm/h)"
                draw.rectangle((0,0,min(overlay_img.width, 360), 16), fill=(0,0,0,160))
                draw.text((4,2), info, fill=(255,255,255,255))

                # 窓枠の可視化（中心に揃える）
                w,h = overlay_img.width, overlay_img.height
                def rect_centered(px:int, py:int, size:int, color):
                    half = size // 2
                    sx = max(px - (half - 1), 0)
                    sy = max(py - (half - 1), 0)
                    ex = min(sx + size - 1, w-1)
                    ey = min(sy + size - 1, h-1)
                    draw.rectangle((sx, sy, ex, ey), outline=color, width=line_thickness)

                if method == 'average_2x2' or method == 'max_2x2':
                    rect_centered(px, py, 2, (255, 0, 255, 255))  # マゼンタ
                elif method == 'max_3x3':
                    rect_centered(px, py, 3, (0, 255, 255, 255))  # シアン
                elif method == 'max_4x4':
                    rect_centered(px, py, 4, (0, 255, 0, 255))    # 緑
                elif method == 'max_8x8':
                    rect_centered(px, py, 8, (0, 0, 255, 255))    # 青
                os.makedirs('debug_images', exist_ok=True)
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                debug_overlay_path = f"debug_images/tile_{ts}_{validtime}_{method}_z{self.zoom}_x{xt}_y{yt}_px{px}_py{py}.png"
                overlay_img.save(debug_overlay_path)
                log_message(f"[DEBUG] 判定可視化を保存: {debug_overlay_path}")
            except Exception as _e:
                log_message(f"[DEBUG] 可視化保存失敗: {_e}")

            web_url = f"https://www.jma.go.jp/bosai/nowc/#zoom:{self.zoom}/lat:{lat}/lon:{lon}/colordepth:normal/elements:hrpns"
            log_message(f"確認用URL: {web_url}")
            
        # 乖離チェック
        if (mmh_color is not None) and abs(mmh_color - mmh_step) >= 10.0:
            log_message(f"[WARN] 色推定とステップ推定に乖離: color={mmh_color:.1f} stepConv={mmh_step:.1f} mm/h @ ({px},{py})")

        return mmh, vt, png_url

    def rainfall_mm(self, lat: float, lon: float, lead_minutes: int = 0, method: str = "single"):
        """lead_minutes を指定して降水量取得（内部で targetTimes を決定）"""
        bt, vt = self._latest_times(target_offset_minutes=lead_minutes)
        return self.rainfall_mm_at(lat, lon, bt, vt, method=method)

# ───────────────────────────────────────────
def maybe_send_heartbeat(cfg):
    """指定した時刻（HH:MM, JST）に日次の死活通知を送信。1分粒度で判定。"""
    try:
        hb = cfg.get("heartbeat", {})
        if not hb or not hb.get("enabled", False):
            return
        times = hb.get("times", []) or []

        now = datetime.now()
        current = now.strftime("%H:%M")
        if current not in times:
            return

        recipients = set()
        locations = cfg.get("locations", [])
        if not locations and cfg.get("location"):
            if cfg.get("notification", {}).get("enabled") and cfg.get("notification", {}).get("email_to"):
                recipients.add(cfg["notification"]["email_to"])
        else:
            for loc in locations:
                if loc.get("notification_enabled") and loc.get("email_to"):
                    recipients.add(loc["email_to"])

        if not recipients:
            return

        subj = f"【死活監視】雨監視システム稼働中 - {now:%Y/%m/%d %H:%M}"
        body = (
            f"システムは稼働中です。\n\n"
            f"時刻: {now:%Y/%m/%d %H:%M}\n"
            f"監視地点数: {len(locations or [cfg.get('location')])}\n"
            f"間隔: {cfg.get('monitoring',{}).get('interval_minutes', 3)} 分\n"
        )

        stamp = now.strftime("%Y%m%d%H%M")
        flag_file = os.path.join("logs", f"heartbeat_{stamp}.flag")
        os.makedirs("logs", exist_ok=True)
        if not os.path.exists(flag_file):
            for email in recipients:
                send_email(email, subj, body)
            log_message(f"死活監視通知を {len(recipients)} 件送信: {', '.join(recipients)}")
            open(flag_file, "w").close()
    except Exception as e:
        log_message(f"[ERROR] ハートビート送信エラー: {e}")

# ───────────────────────────────────────────
def check_and_notify():
    """降水量チェックと通知"""
    try:
        cfg = load_config()
        debug_mode = cfg.get("debug", False)
        lead_minutes = int(cfg.get("monitoring", {}).get("lead_minutes", 0))

        maybe_send_heartbeat(cfg)

        api = JMANowcastAPI(zoom=10, debug=debug_mode)
        log_message(f"予測オフセット: {lead_minutes} 分先を参照")

        # 互換: locations がなければ location を1件として扱う
        locations = []
        if isinstance(cfg.get("locations"), list) and cfg["locations"]:
            locations = cfg["locations"]
        elif isinstance(cfg.get("location"), dict):
            old_loc = cfg["location"].copy()
            old_loc["heavy_rain"] = cfg.get("thresholds", {}).get("heavy_rain", 30)
            old_loc["torrential_rain"] = cfg.get("thresholds", {}).get("torrential_rain", 50)
            old_loc["email_to"] = cfg.get("notification", {}).get("email_to", "")
            old_loc["notification_enabled"] = cfg.get("notification", {}).get("enabled", True)
            locations = [old_loc]
        else:
            locations = [{
                "name": "三島駅",
                "lat": 35.126474871810345,
                "lon": 138.91109391000256,
                "heavy_rain": 30,
                "torrential_rain": 50,
                "email_to": "",
                "notification_enabled": True
            }]

        for loc in locations:
            try:
                loc_name = loc.get("name", "(無名)")
                lat = float(loc["lat"])
                lon = float(loc["lon"])
                heavy = float(loc.get("heavy_rain", 30))
                torrential = float(loc.get("torrential_rain", 50))
                email_to = loc.get("email_to", "")
                notification_enabled = bool(loc.get("notification_enabled", True))
            except Exception:
                continue

            # 0/15/30/60 分用の basetime/validtime を先に固定
            leads_preview = [0, 15, 30, 60]
            times_fixed: Dict[int, Tuple[str,str]] = {}
            for lm in leads_preview:
                times_fixed[lm] = api._latest_times(target_offset_minutes=lm)

            # 現在の lead_minutes でメイン値
            bt_main, vt_main = api._latest_times(target_offset_minutes=lead_minutes)
            rain, vt_jst, png_url = api.rainfall_mm_at(lat, lon, bt_main, vt_main)
            
            # デバッグ画像のファイル名を生成
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            xt, yt = api._deg2tile(lat, lon)
            px, py = api._pixel_in_tile(lat, lon)
            debug_filename = f"tile_{ts}_{vt_main}_max_2x2_z{api.zoom}_x{xt}_y{yt}_px{px}_py{py}.png"

            # プレビュー（固定時刻での比較）
            try:
                preview_results = []
                for lm in leads_preview:
                    bt, vt = times_fixed[lm]
                    r, t_jst, _ = api.rainfall_mm_at(lat, lon, bt, vt)
                    preview_results.append((lm, r, t_jst))
                msg = f"【地点: {loc_name}】時刻別降水量: " + ", ".join(
                    [f"{('現在' if lm==0 else str(lm)+'分後')} {r:.1f}mm/h({t.strftime('%H:%M')})" for lm, r, t in preview_results]
                )
                log_message(msg)
            except Exception:
                pass

            # MAX窓の比較（固定時刻）
            try:
                def summarize(method: str):
                    vals = []
                    for lm in leads_preview:
                        bt, vt = times_fixed[lm]
                        r, t_jst, _ = api.rainfall_mm_at(lat, lon, bt, vt, method=method)
                        vals.append(f"{('現在' if lm==0 else str(lm)+'分後')} {r:.1f}mm/h({t_jst.strftime('%H:%M')})")
                    return ", ".join(vals)
                log_message(f"[地点: {loc_name}] MAX2x2: {summarize('max_2x2')}")
                log_message(f"[地点: {loc_name}] MAX4x4: {summarize('max_4x4')}")
                log_message(f"[地点: {loc_name}] MAX8x8: {summarize('max_8x8')}")
            except Exception:
                pass

            # デバッグ: 精度比較
            if debug_mode:
                try:
                    log_message(f"[{loc_name}] === 精度比較実験 ===")
                    methods = [
                        ("single", "1px"),
                        ("high_zoom", "zoom+1"),
                        ("average_2x2", "2x2平均")
                    ]
                    for method, label in methods:
                        r_exp, _, _ = api.rainfall_mm_at(lat, lon, bt_main, vt_main, method=method)
                        log_message(f"[{loc_name}] {label}: {r_exp:.1f} mm/h")
                    log_message(f"[{loc_name}] === 比較終了 ===")
                except Exception as e:
                    log_message(f"[{loc_name}] 精度比較エラー: {e}")
        
            if png_url == "N/A":
                log_message(f"[WARNING] [{loc_name}] データ取得失敗。次回再試行します。")
                continue

            log_message(f"[{loc_name}] デバッグ画像: {debug_filename}")
            log_message(f"[{loc_name}] 降水量 ({vt_jst.strftime('%H:%M')} JST): {rain:.1f} mm/h")

            level = "豪雨" if rain >= torrential else "大雨" if rain >= heavy else None
            if not level:
                log_message(f"[{loc_name}] 異常なし（閾値: 大雨{heavy}mm/h, 豪雨{torrential}mm/h）")
                continue

            if notification_enabled and email_to:
                subj = f"【{level}警報】{loc_name}周辺 - {datetime.now():%m/%d %H:%M}"
                body = (
                    f"{loc_name} 周辺で {level} が予測されています。\n\n"
                    f"降水量 ({vt_jst.strftime('%H:%M')} JST): {rain:.1f} mm/h\n"
                    f"警報レベル: {level} (閾値: 大雨{heavy}mm/h, 豪雨{torrential}mm/h)\n"
                    f"デバッグ画像: {debug_filename}\n"
                    f"確認時刻: {datetime.now():%Y/%m/%d %H:%M}\n\n"
                    "データソース: 気象庁 高解像度降水ナウキャスト"
                )
                send_email(email_to, subj, body)
                log_message(f"[{loc_name}] {level}検知 → {email_to} に通知送信")
            else:
                log_message(f"[{loc_name}] {level}検知（通知設定なし: enabled={notification_enabled}, email='{email_to}'）")

            # デバッグ画像の自動クリーンアップ
            dbg = cfg.get("debug_images", {}) if isinstance(cfg, dict) else {}
            prune_debug_images(
                retention_hours=int(dbg.get("retention_hours", 12)),
                max_files=int(dbg.get("max_files", 500)),
                max_total_mb=int(dbg.get("max_total_mb", 200)),
            )
        
    except Exception as e:
        log_message(f"[ERROR] チェック処理エラー: {e}")

# ───────────────────────────────────────────
def main():
    # コマンドライン引数処理
    if len(sys.argv) > 1:
        if sys.argv[1] == "--once":
            check_and_notify()
            return
        elif sys.argv[1] == "--debug":
            cfg = load_config()
            cfg["debug"] = True
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            check_and_notify()
            return
        elif sys.argv[1] == "--experiment":
            log_message("=== 精度比較実験モード ===")
            cfg = load_config()
            cfg["debug"] = True

            locations = cfg.get("locations", [])
            if not locations and cfg.get("location"):
                locations = [cfg["location"]]

            api = JMANowcastAPI(zoom=10, debug=True)
            for loc in locations:
                try:
                    loc_name = loc.get("name", "地点")
                    lat = float(loc["lat"])
                    lon = float(loc["lon"])

                    log_message(f"\n=== [{loc_name}] 精度比較実験 開始 ===")
                    bt, vt = api._latest_times(target_offset_minutes=0)
                    methods = [
                        ("single", "1px"),
                        ("high_zoom", "zoom+1"),
                        ("average_2x2", "2x2平均")
                    ]
                    results = {}
                    for method, label in methods:
                        rain, vt_jst, url = api.rainfall_mm_at(lat, lon, bt, vt, method=method)
                        results[method] = rain
                        log_message(f"[{loc_name}] {label}: {rain:.1f} mm/h")

                    values = list(results.values())
                    if max(values) - min(values) > 0:
                        log_message(f"[{loc_name}] 差異: 最大{max(values):.1f} - 最小{min(values):.1f} = {max(values)-min(values):.1f} mm/h")
                    else:
                        log_message(f"[{loc_name}] 全パターン同値")
                    log_message(f"=== [{loc_name}] 精度比較実験 終了 ===\n")
                except Exception as e:
                    log_message(f"[{loc_name}] 実験エラー: {e}")

            log_message("=== 全地点の精度比較実験 完了 ===")
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

# ───────────────────────────────────────────
if __name__ == "__main__":
    main()
