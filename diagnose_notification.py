"""診斷腳本：檢查為什麼沒收到通知"""
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

STATE_PATH = Path("data/state.json")
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

def diagnose():
    print("=" * 60)
    print("🔍 Threads 通知診斷報告")
    print("=" * 60)
    print()
    
    # 1. 檢查當前時間
    now_utc = datetime.now(timezone.utc)
    now_taipei = now_utc.astimezone(TAIPEI_TZ)
    print(f"📅 當前時間:")
    print(f"  UTC:   {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  台北:  {now_taipei.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # 2. 讀取 state.json
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
    
    print(f"📊 各來源狀態:")
    print()
    
    for source_id, data in state.items():
        print(f"  【{source_id}】")
        
        # 最後檢查時間
        last_checked = data.get("last_checked_at")
        if last_checked:
            dt = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
            dt_taipei = dt.astimezone(TAIPEI_TZ)
            time_diff = now_utc - dt
            hours_ago = time_diff.total_seconds() / 3600
            
            print(f"    最後檢查: {dt_taipei.strftime('%Y-%m-%d %H:%M:%S')} (台北)")
            print(f"    距離現在: {hours_ago:.1f} 小時前")
            
            if hours_ago > 2:
                print(f"    ⚠️  警告：超過 2 小時未檢查！")
        else:
            print(f"    最後檢查: 從未執行")
        
        # 最後成功時間
        last_success = data.get("last_success_at")
        if last_success:
            dt = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
            dt_taipei = dt.astimezone(TAIPEI_TZ)
            print(f"    最後成功: {dt_taipei.strftime('%Y-%m-%d %H:%M:%S')} (台北)")
        
        # 錯誤訊息
        last_error = data.get("last_error_message")
        if last_error:
            print(f"    ❌ 錯誤: {last_error}")
        
        # 已通知貼文數量
        notified_count = len(data.get("notified_posts", []))
        print(f"    已通知貼文: {notified_count} 則")
        print()
    
    print("=" * 60)
    print("📋 建議檢查:")
    print("=" * 60)
    print("1. 前往 GitHub Actions 查看最近的執行記錄")
    print("2. 確認 GitHub Actions 是否正常每小時執行")
    print("3. 檢查是否有錯誤訊息")
    print("4. 確認 Threads 帳號是否確實有新貼文")
    print()
    print("🔗 GitHub Actions 網址:")
    print("   https://github.com/你的帳號/test/actions")
    print()

if __name__ == "__main__":
    diagnose()
