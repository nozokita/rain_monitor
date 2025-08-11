import streamlit as st, json, subprocess, os, re, sys, signal
from datetime import datetime

st.set_page_config(page_title="気象庁ナウキャスト PNGタイル解析（APIキー不要）", page_icon="🌧️")

# ───────────────────────────────────────────
def load_config():
    if os.path.exists("config.json"):
        with open("config.json", encoding="utf-8") as f:
            cfg = json.load(f)
            # 後方互換: location -> locations への変換を毎回実行
            if "locations" not in cfg or not isinstance(cfg["locations"], list):
                if "location" in cfg and isinstance(cfg["location"], dict):
                    cfg["locations"] = [cfg["location"]]
                else:
                    cfg["locations"] = []
            if not cfg["locations"]:
                cfg["locations"].append({"name": "三島駅", "lat": 35.126474871810345, "lon": 138.91109391000256})
            return cfg
    return {
        "locations": [{"name": "三島駅", "lat": 35.126474871810345, "lon": 138.91109391000256}],
        "monitoring": {"enabled": False, "interval_minutes": 3, "lead_minutes": 60},
        "thresholds": {"heavy_rain": 30, "torrential_rain": 50},
        "notification": {"email_to": "", "enabled": True},
        "heartbeat": {"enabled": True, "times": ["09:00", "17:00"]},
        "debug": False
    }


def save_config(cfg):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ───────────────── UI ───────────────────────
st.title("気象庁ナウキャスト PNGタイル解析（APIキー不要）")
st.caption("気象庁ナウキャスト PNGタイル解析（APIキー不要）")

cfg = load_config()

# ---------- Sidebar ----------
with st.sidebar:
    st.header("監視状態")

    cfg["monitoring"]["enabled"] = st.toggle(
        "監視を有効化", value=cfg["monitoring"]["enabled"]
    )

    # ワーカー管理ユーティリティ
    PID_FILE = "monitor.pid"

    def read_pid():
        try:
            with open(PID_FILE, "r") as f:
                return int(f.read().strip())
        except Exception:
            return None

    def is_alive(pid: int) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def is_worker_running() -> bool:
        pid = read_pid()
        if pid and is_alive(pid):
            return True
        # ステールPIDファイルを掃除
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
        return False

    def start_worker():
        try:
            DEVNULL = open(os.devnull, 'wb')
            subprocess.Popen(
                [sys.executable, "monitor.py"],
                stdout=DEVNULL, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            st.success("ワーカーを起動しました")
        except Exception as e:
            st.error(f"起動に失敗: {e}")

    def stop_worker():
        pid = read_pid()
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGTERM)
            st.info("ワーカーに停止指示を送信")
        except Exception as e:
            st.warning(f"停止指示に失敗: {e}")

    # ↓ ここを修正
    if cfg["monitoring"]["enabled"]:
        # 自動起動
        if not is_worker_running():
            save_config(cfg)  # 設定を保存し、ワーカーが最新状態を読み込めるように
            start_worker()
        st.success("🟢 監視中")
    else:
        # 自動停止
        if is_worker_running():
            save_config(cfg)
            stop_worker()
        st.warning("🔴 停止中")

    st.divider()
    if st.button("🔍 今すぐチェック"):
        with st.spinner("データ取得中..."):
            res = subprocess.run([sys.executable, "monitor.py", "--once"])
            st.success("✅ 完了" if res.returncode == 0 else "❌ エラー")

    st.divider()
    st.subheader("⏰ 監視間隔")
    cfg["monitoring"]["interval_minutes"] = st.slider(
        "確認間隔（分）", 3, 30, cfg["monitoring"]["interval_minutes"]
    )
    st.info(f"1日あたり {24*60//cfg['monitoring']['interval_minutes']} 回")

    cfg["monitoring"]["lead_minutes"] = st.select_slider(
        "予測の参照時刻（分先）", 
        options=[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60], 
        value=cfg.get("monitoring",{}).get("lead_minutes", 60)
    )

    st.divider()
    st.info(
        "0–30分:約250m／35–60分:約1km、5分更新  \n"
        "数値APIは未提供のためPNGから推定"
    )

# ---------- Tabs ----------
tab1, tab2, tab3, tab4 = st.tabs(["📊 現在","⚙️ 設定", "📜 ログ", "ℹ️ 使い方"])

# ----- 現在 -----
with tab1:
    st.header("現在の監視状態")

    # ---- 小さめのCSS（チップ/カード）
    st.markdown("""
    <style>
    .chip { padding:4px 10px; border-radius:999px; color:#fff; display:inline-block; font-size:12px; font-weight:700 }
    .card { border:1px solid #e5e7eb; border-radius:16px; padding:12px; margin-bottom:12px; background:#fff;
            box-shadow:0 1px 3px rgba(0,0,0,.06) }
    .card .title { font-weight:700; margin-bottom:6px; }
    .card .meta  { font-size:14px; line-height:1.6 }
    </style>
    """, unsafe_allow_html=True)

    def chip(text, color="#0ea5e9"):
        return f"<span class='chip' style='background:{color}'>{text}</span>"

    # ---- 監視状態/KPI（参照オフセット・自動更新UIは無し）
    is_enabled   = bool(cfg.get("monitoring", {}).get("enabled", False))
    interval_min = int(cfg.get("monitoring", {}).get("interval_minutes", 3))

    # 最終更新（ログ最終行のタイムスタンプを短縮表示）
    last_updated_raw = None
    if os.path.exists("logs/monitor.log"):
        with open("logs/monitor.log", encoding="utf-8") as f:
            for line in reversed(f.read().splitlines()):
                if line.startswith("[") and "]" in line:
                    last_updated_raw = line.split("]")[0].lstrip("[")
                    break

    # KPI行：最終更新の列幅を少し広めにする
    c0, c1, c2, c3 = st.columns([1, 1, 1, 1.6])
    c0.markdown(chip("監視中", "#16a34a") if is_enabled else chip("停止中", "#dc2626"),
                unsafe_allow_html=True)
    c1.metric("監視地点数", len(cfg.get("locations", [])))
    c2.metric("チェック間隔", f"{interval_min} 分")

    # 最終更新は短い表記＋ヘルプにフル時刻
    if last_updated_raw:
        try:
            ludt = datetime.strptime(last_updated_raw, "%Y-%m-%d %H:%M:%S")
            short_val = ludt.strftime("%m/%d %H:%M")        # 例: 08/11 14:35
            full_tip  = f"{ludt:%Y-%m-%d %H:%M:%S}"         # 例: 2025-08-11 14:35:42
            c3.metric("最終更新", short_val, help=full_tip)
        except Exception:
            c3.metric("最終更新", last_updated_raw)
    else:
        c3.metric("最終更新", "—")

    st.divider()
    st.subheader("📍 監視地点")

    locs = cfg.get("locations", [])
    if not locs:
        st.info("監視地点が設定されていません。config.json の locations を追加してください。")
    else:
        # 2列カードで一覧表示
        cols = st.columns(2)
        for i, loc in enumerate(locs):
            with cols[i % 2]:
                name = loc.get("name", f"地点{i+1}")
                lat  = float(loc.get("lat", 0.0))
                lon  = float(loc.get("lon", 0.0))
                heavy = float(loc.get("heavy_rain",  cfg.get("thresholds", {}).get("heavy_rain", 30)))
                torr  = float(loc.get("torrential_rain", cfg.get("thresholds", {}).get("torrential_rain", 50)))
                notif_on = bool(loc.get("notification_enabled", True)) and bool(loc.get("email_to", ""))

                notif_badge = chip("通知ON", "#16a34a") if notif_on else chip("通知OFF", "#6b7280")
                jma_url = f"https://www.jma.go.jp/bosai/nowc/#zoom:10/lat:{lat}/lon:{lon}/colordepth:normal/elements:hrpns"

                st.markdown(f"""
                <div class="card">
                  <div class="title">🌏 {name}</div>
                  <div class="meta">
                    <div>座標：{lat:.6f}, {lon:.6f}</div>
                    <div>しきい値：大雨 {heavy:.0f} / 豪雨 {torr:.0f} mm/h</div>
                    <div>通知：{notif_badge}</div>
                  </div>
                </div>
                """, unsafe_allow_html=True)

                # JMAの地図へ
                try:
                    st.link_button("地図で開く（JMA）", jma_url, use_container_width=True)
                except Exception:
                    st.markdown(f"<a href='{jma_url}' target='_blank'>地図で開く（JMA）</a>", unsafe_allow_html=True)

    st.divider()
    st.subheader("⏱️ 短時間降水モニタ（代表：2×2最大）— 現在 / ＋15 / ＋30 / ＋60 分")

    # 各地点の最新降水量データ（ログ）を取得・表示（2×2最大のみ）
    if os.path.exists("logs/monitor.log"):
        import re
        with open("logs/monitor.log", encoding="utf-8") as f:
            lines = f.read().splitlines()

        # 「[地点: ○○] MAX2x2: 現在 x.xmm/h(HH:MM), 15分後 ..., 30分後 ..., 60分後 ...」
        # の最新行だけを地点ごとに拾う
        location_max2 = {}
        for ln in reversed(lines):
            if "MAX2x2:" not in ln:
                continue
            m = re.search(r"\[地点:\s*([^\]]+)\].*MAX2x2:\s*(.+)$", ln)
            if m:
                name = m.group(1).strip()
                if name not in location_max2:
                    location_max2[name] = m.group(2).strip()

        def severity_bg(v, heavy=30.0, torrential=50.0):
            # 値に応じて背景色（やさしめ）を返す
            if v is None:
                return "#f3f4f6"   # gray-100
            if v >= torrential:
                return "#fee2e2"   # red-100
            if v >= heavy:
                return "#fef3c7"   # amber-100
            if v >= 1.0:
                return "#e0f2fe"   # sky-100
            return "#f3f4f6"       # gray-100

        def card(container, label, val, tstr, heavy, torrential):
            bg = severity_bg(val, heavy, torrential)
            vtxt = "—" if val is None else f"{val:.1f} mm/h"
            container.markdown(f"""
            <div style="padding:12px;border-radius:14px;background:{bg};
                        box-shadow:0 1px 3px rgba(0,0,0,.08);">
            <div style="font-size:12px;opacity:.75">{label}</div>
            <div style="font-size:26px;font-weight:700;line-height:1.2">{vtxt}</div>
            <div style="font-size:12px;opacity:.75">@ {tstr}</div>
            </div>
            """, unsafe_allow_html=True)

        if location_max2:
            order = ["現在", "15分後", "30分後", "60分後"]
            for loc in cfg["locations"]:
                name = loc.get("name")
                if name not in location_max2:
                    continue

                # 閾値は地点個別 > 全体デフォルトの順で取得
                heavy = float(loc.get("heavy_rain", cfg.get("thresholds",{}).get("heavy_rain", 30)))
                torr  = float(loc.get("torrential_rain", cfg.get("thresholds",{}).get("torrential_rain", 50)))

                # 文字列をパースして {ラベル:(値,時刻)} を作る
                raw = location_max2[name]
                pairs = dict((lab, (None, "—")) for lab in order)
                for lab, val, tstr in re.findall(r"(現在|15分後|30分後|60分後)\s+([0-9.]+)mm/h\((\d{2}:\d{2})\)", raw):
                    pairs[lab] = (float(val), tstr)

                st.markdown(f"### 🌧️ {name}")
                c1, c2, c3, c4 = st.columns(4)
                card(c1, "現在",  *pairs["現在"],  heavy, torr)
                card(c2, "＋15分", *pairs["15分後"], heavy, torr)
                card(c3, "＋30分", *pairs["30分後"], heavy, torr)
                card(c4, "＋60分", *pairs["60分後"], heavy, torr)
                st.caption(f"凡例：背景色＝強度目安（{heavy:.0f}mm/hで大雨、{torr:.0f}mm/hで豪雨）")
        else:
            st.info("まだ実行結果がありません。『今すぐチェック』を押してください。")
    else:
        st.info("まだ実行結果がありません。『今すぐチェック』を押してください。")

    st.divider()
    st.subheader("📝 最新ログ 10 行")
    if os.path.exists("logs/monitor.log"):
        with open("logs/monitor.log", encoding="utf-8") as f:
            for line in reversed(f.readlines()[-10:]):
                st.write(line.rstrip())
    else:
        st.info("ログなし")

# ----- 設定 -----
with tab2:
    st.header("監視設定")
    st.subheader("📍 監視地点（複数可）")

    # rerun ヘルパー（新旧API両対応）
    def safe_rerun():
        try:
            st.rerun()
        except Exception:
            try:
                st.experimental_rerun()
            except Exception:
                pass

    # 編集用 UI
    remove_idx = None
    for idx, loc in enumerate(cfg["locations"]):
        with st.container(border=True):
            # ヘッダーと削除ボタンを横並び
            header_col1, header_col2 = st.columns([4, 1])
            with header_col1:
                st.markdown(f"**地点 #{idx+1}**")
            with header_col2:
                if st.button("削除", key=f"del_{idx}"):
                    remove_idx = idx
            
            # 基本情報
            new_name = st.text_input(f"地点名", value=loc.get("name", f"地点{idx+1}"), key=f"name_{idx}")
            coord_col1, coord_col2 = st.columns(2)
            new_lat = coord_col1.number_input(f"緯度", value=float(loc.get("lat", 0.0)), format="%.9f", key=f"lat_{idx}")
            new_lon = coord_col2.number_input(f"経度", value=float(loc.get("lon", 0.0)), format="%.9f", key=f"lon_{idx}")
            
            # 閾値設定（地点個別）
            st.markdown("**閾値設定 (mm/h)**")
            thresh_col1, thresh_col2 = st.columns(2)
            new_heavy = thresh_col1.number_input("大雨", 10, 100, loc.get("heavy_rain", 30), step=5, key=f"heavy_{idx}")
            new_torrential = thresh_col2.number_input("豪雨", 20, 200, loc.get("torrential_rain", 50), step=10, key=f"torrential_{idx}")
            
            # 通知設定（地点個別）
            st.markdown("**通知設定**")
            notif_col1, notif_col2 = st.columns(2)
            new_email = notif_col1.text_input("メールアドレス", value=loc.get("email_to", ""), key=f"email_{idx}")
            new_enabled = notif_col2.checkbox("通知有効", value=loc.get("notification_enabled", True), key=f"notif_{idx}")
            
            # 値が変更されたら cfg に反映
            cfg["locations"][idx]["name"] = new_name
            cfg["locations"][idx]["lat"] = new_lat
            cfg["locations"][idx]["lon"] = new_lon
            cfg["locations"][idx]["heavy_rain"] = new_heavy
            cfg["locations"][idx]["torrential_rain"] = new_torrential
            cfg["locations"][idx]["email_to"] = new_email
            cfg["locations"][idx]["notification_enabled"] = new_enabled
    
    # 削除処理
    if remove_idx is not None:
        cfg["locations"].pop(remove_idx)
        save_config(cfg)
        safe_rerun()

    # 追加処理
    if st.button("＋ 地点を追加"):
        new_location = {
            "name": f"地点{len(cfg['locations'])+1}", 
            "lat": 35.0, 
            "lon": 135.0,
            "heavy_rain": 30,
            "torrential_rain": 50,
            "email_to": "",
            "notification_enabled": True
        }
        cfg["locations"].append(new_location)
        save_config(cfg)
        safe_rerun()

    st.divider()
    st.subheader("🌧️ 全体設定")
    st.info("各地点の個別設定は上記で行えます。ここでは全体の設定を行います。")
    
    # 死活監視設定
    st.markdown("**死活監視**")
    col_hb = st.columns(2)
    if "heartbeat" not in cfg:
        cfg["heartbeat"] = {"enabled": True, "times": ["09:00", "17:00"]}
    cfg["heartbeat"]["enabled"] = col_hb[0].checkbox("死活監視を有効化", value=cfg["heartbeat"].get("enabled", True))
    
    # 時刻入力（文字列で）
    times_str = col_hb[1].text_input("通知時刻 (HH:MM,HH:MM)", value=",".join(cfg["heartbeat"].get("times", ["09:00", "17:00"])))
    try:
        times_list = [t.strip() for t in times_str.split(",") if t.strip()]
        cfg["heartbeat"]["times"] = times_list
    except:
        pass

    st.divider()
    if st.button("💾 設定を保存", type="primary"):
        save_config(cfg)
        st.success("設定を保存しました")
        st.balloons()
        
# ----- ログ -----
with tab3:
    st.header("動作ログ")
    if os.path.exists("logs/monitor.log"):
        with open("logs/monitor.log", encoding="utf-8") as f:
            log_txt = f.read()
        lines = log_txt.splitlines()
        col1, col2, col3 = st.columns(3)
        col1.metric("行数", len(lines))
        col2.metric("警報", sum("警報" in ln for ln in lines))
        col3.metric("エラー", sum("エラー" in ln for ln in lines))

        st.divider()
        st.text_area("全文", log_txt, height=400)
        if st.button("🗑️ ログをクリア"):
            open("logs/monitor.log", "w").close()
            st.success("クリアしました")
    else:
        st.info("ログファイルなし")

# ----- 使い方 -----
with tab4:
    st.header("使い方")
    st.markdown(
        """
### 🚀 セットアップ
```bash
pip install streamlit requests Pillow pywin32
streamlit run app.py      # UI
python monitor.py         # ワーカー
""")