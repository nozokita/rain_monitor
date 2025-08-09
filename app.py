import streamlit as st, json, subprocess, os, re, sys, signal
from datetime import datetime

st.set_page_config(page_title="気象庁ナウキャスト PNGタイル解析（APIキー不要）", page_icon="🌧️")

# ───────────────────────────────────────────
def load_config():
    if os.path.exists("config.json"):
        with open("config.json", encoding="utf-8") as f:
            return json.load(f)
    return {
        "location": {"name": "三島駅", "lat": 35.126474871810345, "lon": 138.91109391000256},
        "monitoring": {"enabled": False, "interval_minutes": 3},
        "thresholds": {"heavy_rain": 30, "torrential_rain": 50},
        "notification": {"email_to": "", "enabled": True},
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
    st.info(
        "0–30分:約250m／35–60分:約1km、5分更新  \n"
        "数値APIは未提供のためPNGから推定"
    )

# ---------- Tabs ----------
tab1, tab2, tab3, tab4 = st.tabs(["⚙️ 設定", "📊 現在", "📜 ログ", "ℹ️ 使い方"])

# ----- 設定 -----
with tab1:
    st.header("監視設定")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📍 監視地点（複数可）")

        # 後方互換: location -> locations
        if "locations" not in cfg or not isinstance(cfg["locations"], list):
            if "location" in cfg and isinstance(cfg["location"], dict):
                cfg["locations"] = [cfg["location"]]
            else:
                cfg["locations"] = []
        if not cfg["locations"]:
            cfg["locations"].append({"name": "三島駅", "lat": 35.126474871810345, "lon": 138.91109391000256})

        # 編集用 UI
        remove_idx = None
        for idx, loc in enumerate(cfg["locations"]):
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    loc["name"] = st.text_input(f"地点名 #{idx+1}", value=loc.get("name", f"地点{idx+1}"), key=f"name_{idx}")
                    loc["lat"] = st.number_input(f"緯度 #{idx+1}", value=float(loc.get("lat", 0.0)), format="%.9f", key=f"lat_{idx}")
                    loc["lon"] = st.number_input(f"経度 #{idx+1}", value=float(loc.get("lon", 0.0)), format="%.9f", key=f"lon_{idx}")
                with c2:
                    if st.button("削除", key=f"del_{idx}"):
                        remove_idx = idx
        if remove_idx is not None:
            cfg["locations"].pop(remove_idx)
            st.experimental_rerun()

        if st.button("＋ 地点を追加"):
            cfg["locations"].append({"name": f"地点{len(cfg['locations'])+1}", "lat": 35.0, "lon": 135.0})
            st.experimental_rerun()

    with col2:
        st.subheader("⏰ 間隔(分)")
        cfg["monitoring"]["interval_minutes"] = st.slider(
            "確認間隔", 3, 30, cfg["monitoring"]["interval_minutes"]
        )
        st.info(f"1日あたり {24*60//cfg['monitoring']['interval_minutes']} 回")

        cfg["monitoring"]["lead_minutes"] = st.select_slider(
            "予測の参照時刻（分先）", options=[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60], value=cfg.get("monitoring",{}).get("lead_minutes", 60)
        )

    st.divider()
    st.subheader("🌧️ 閾値(mm/h)")
    col3, col4 = st.columns(2)
    cfg["thresholds"]["heavy_rain"] = col3.number_input(
        "大雨", 10, 100, cfg["thresholds"]["heavy_rain"], step=5
    )
    cfg["thresholds"]["torrential_rain"] = col4.number_input(
        "豪雨", 20, 200, cfg["thresholds"]["torrential_rain"], step=10
    )

    st.divider()
    st.subheader("📧 通知")
    col5, col6 = st.columns(2)
    cfg["notification"]["email_to"] = col5.text_input("宛先メール", cfg["notification"]["email_to"])
    cfg["notification"]["enabled"] = col6.checkbox(
        "メール通知を有効化", value=cfg["notification"]["enabled"]
    )

    st.divider()
    if st.button("💾 設定を保存", type="primary"):
        save_config(cfg)
        st.success("設定を保存しました")
        st.balloons()

# ----- 現在 -----
with tab2:
    st.header("現在の監視状態")
    cols = st.columns(3)
    cols[0].metric("地点", cfg["location"]["name"])
    cols[1].metric("間隔", f"{cfg['monitoring']['interval_minutes']} 分")
    cols[2].metric("状態", "監視中" if cfg["monitoring"]["enabled"] else "停止中")

    st.divider()
    st.subheader("🕒 0/15/30/60分先の降水量")
    # monitor.py がログに書く "時刻別降水量" を拾って最新をメトリクス表示
    latest_preview = None
    if os.path.exists("logs/monitor.log"):
        with open("logs/monitor.log", encoding="utf-8") as f:
            lines = f.read().splitlines()
        for ln in reversed(lines):
            if "時刻別降水量:" in ln:
                latest_preview = ln
                break
    if latest_preview:
        # 例: 時刻別降水量: 現在 0.0mm/h(13:20), 15分後 0.0mm/h(13:35), 30分後 0.0mm/h(13:50), 60分後 1.0mm/h(14:20)
        try:
            part = latest_preview.split("時刻別降水量:", 1)[1].strip()
            chunks = [c.strip() for c in part.split(",")]

            def parse_metric(chunk: str, base_label: str):
                m = re.search(r"([0-9.]+)mm/h\((\d{2}:\d{2})\)", chunk)
                if m:
                    value = f"{m.group(1)} mm/h"
                    label = f"{base_label} ({m.group(2)})"
                else:
                    value = chunk
                    label = base_label
                return label, value

            c0 = chunks[0] if len(chunks) > 0 else ""
            c15 = chunks[1] if len(chunks) > 1 else ""
            c30 = chunks[2] if len(chunks) > 2 else ""
            c60 = chunks[3] if len(chunks) > 3 else ""

            mcols = st.columns(4)
            lbl, val = parse_metric(c0, "現在")
            mcols[0].metric(lbl, val)
            lbl, val = parse_metric(c15, "15分後")
            mcols[1].metric(lbl, val)
            lbl, val = parse_metric(c30, "30分後")
            mcols[2].metric(lbl, val)
            lbl, val = parse_metric(c60, "60分後")
            mcols[3].metric(lbl, val)
        except Exception:
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