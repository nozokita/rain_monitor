#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_tile.py - 保存されたタイル画像を確認するツール
"""

import os
import sys
import json
import glob
from io import BytesIO
from datetime import datetime

import requests
from PIL import Image
import numpy as np
import matplotlib.pyplot as plt

# α値→降水量変換表
STEP_TO_MM = {a: a for a in range(1, 61)}
STEP_TO_MM.update({61: 80, 62: 100, 63: 150, 64: 200, 65: 300})


def check_latest_image():
    """最新の保存画像を確認"""
    
    # debug_imagesフォルダの画像を検索
    pattern = "debug_images/tile_*.png"
    files = glob.glob(pattern)
    
    if not files:
        print("❌ デバッグ画像が見つかりません")
        print("   まず `python monitor.py --debug` を実行してください")
        return
    
    # 最新のファイルを取得
    latest_file = max(files, key=os.path.getctime)
    print(f"📁 確認ファイル: {latest_file}")
    
    # 画像を開く
    img = Image.open(latest_file)
    print(f"📐 画像サイズ: {img.size}")
    print(f"🎨 画像モード: {img.mode}")
    
    if img.mode != 'RGBA':
        print("ℹ️ 画像を RGBA に変換して解析します（元モード: %s）" % img.mode)
        img = img.convert('RGBA')
    
    # アルファチャンネルを確認
    alpha = np.array(img.getchannel('A'))
    
    print("\n📊 アルファチャンネル統計:")
    print(f"  最小値: {alpha.min()}")
    print(f"  最大値: {alpha.max()}")
    print(f"  平均値: {alpha.mean():.2f}")
    print(f"  非ゼロピクセル数: {np.count_nonzero(alpha)} / {alpha.size}")
    print(f"  非ゼロピクセル率: {np.count_nonzero(alpha) / alpha.size * 100:.2f}%")
    
    # 降水量の分布を確認
    non_zero = alpha[alpha > 0]
    if len(non_zero) > 0:
        print("\n🌧️ 降水量分布:")
        unique_values = np.unique(non_zero)
        for val in unique_values[:10]:  # 最初の10個まで表示
            mm = STEP_TO_MM.get(int(val), 0)
            count = np.sum(alpha == val)
            print(f"  ステップ {val:3d} → {mm:3d} mm/h : {count:5d} ピクセル")
        if len(unique_values) > 10:
            print(f"  ... 他 {len(unique_values)-10} 種類の値")
    else:
        print("\n☀️ 降水なし（全ピクセルが0）")
    
    # 可視化
    visualize_tile(img, alpha, latest_file)


def visualize_tile(img, alpha, filename):
    """タイル画像を可視化"""
    
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    
    # 1. 元画像（RGB）
    ax = axes[0, 0]
    rgb = img.convert('RGB')
    ax.imshow(rgb)
    ax.set_title('元画像 (RGB)')
    ax.grid(True, alpha=0.3)
    
    # 2. アルファチャンネル
    ax = axes[0, 1]
    im = ax.imshow(alpha, cmap='viridis', vmin=0, vmax=65)
    ax.set_title('アルファチャンネル（降水強度）')
    plt.colorbar(im, ax=ax, label='ステップ値')
    ax.grid(True, alpha=0.3)
    
    # 3. 降水量マップ (mm/h)
    ax = axes[1, 0]
    # ステップ値を降水量に変換
    rainfall = np.vectorize(lambda x: STEP_TO_MM.get(int(x), 0))(alpha)
    im = ax.imshow(rainfall, cmap='Blues', vmin=0, vmax=50)
    ax.set_title('降水量 (mm/h)')
    plt.colorbar(im, ax=ax, label='mm/h')
    ax.grid(True, alpha=0.3)
    
    # 4. 降水エリアのハイライト
    ax = axes[1, 1]
    # 降水がある場所を赤でマーク
    highlight = np.zeros((*alpha.shape, 4))
    highlight[alpha > 0] = [1, 0, 0, 0.5]  # 赤色、半透明
    ax.imshow(rgb)
    ax.imshow(highlight)
    ax.set_title('降水エリア（赤色）')
    ax.grid(True, alpha=0.3)
    
    # 三島駅の位置をマーク（config.jsonから読み込み）
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # ピクセル位置を計算（monitor.pyと同じロジック）
        # ここでは簡易的に中心点をマーク
        for ax in axes.flat:
            ax.plot(31, 42, 'r*', markersize=15, label='三島駅')
            ax.legend()
    except:
        pass
    
    plt.suptitle(f'タイル画像解析: {os.path.basename(filename)}', fontsize=14)
    plt.tight_layout()
    
    # 保存
    output_file = filename.replace('.png', '_analysis.png')
    plt.savefig(output_file, dpi=100, bbox_inches='tight')
    print(f"\n💾 解析画像を保存: {output_file}")
    
    plt.show()


def download_and_check(url):
    """URLから直接タイルをダウンロードして確認"""
    
    print(f"📥 ダウンロード中: {url}")
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"❌ ダウンロード失敗: HTTP {response.status_code}")
            return
        
        img = Image.open(BytesIO(response.content))
        
        # 一時保存
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs("debug_images", exist_ok=True)
        filename = f"debug_images/direct_{timestamp}.png"
        img.save(filename)
        print(f"💾 保存: {filename}")
        
        # 解析
        if img.mode == 'RGBA':
            alpha = np.array(img.getchannel('A'))
            print(f"\n📊 画像情報:")
            print(f"  サイズ: {img.size}")
            print(f"  モード: {img.mode}")
            print(f"  非ゼロピクセル: {np.count_nonzero(alpha)} / {alpha.size}")
            
            visualize_tile(img, alpha, filename)
        else:
            print("⚠️ アルファチャンネルがありません")
            
    except Exception as e:
        print(f"❌ エラー: {e}")


def main():
    """メイン処理"""
    
    print("=" * 60)
    print("🔍 気象庁タイル画像確認ツール")
    print("=" * 60)
    
    if len(sys.argv) > 1:
        # URLが指定された場合
        url = sys.argv[1]
        download_and_check(url)
    else:
        # 保存済み画像を確認
        check_latest_image()
        
        print("\n" + "=" * 60)
        print("💡 使い方:")
        print("  保存済み画像確認: python check_tile.py")
        print("  URL指定:         python check_tile.py [URL]")
        print("\n例:")
        print('  python check_tile.py "https://www.jma.go.jp/bosai/jmatile/data/nowc/20250806213500/none/20250806213500/surf/hrpns/10/907/405.png"')


if __name__ == "__main__":
    # 必要なライブラリチェック
    try:
        import matplotlib
        import numpy
        import requests
    except ImportError as e:
        print(f"❌ 必要なライブラリがありません: {e}")
        print("   pip install matplotlib numpy requests pillow")
        sys.exit(1)
    
    main()