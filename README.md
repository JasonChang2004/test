# Threads Discord Monitor Bot

自動監控 Threads 帳號的新貼文，並透過 Discord Webhook 發送通知。

## 功能特色

- 🔄 定期監控指定的 Threads 帳號
- 📢 新貼文自動推送至 Discord 頻道
- 💾 智能去重，避免重複通知
- ⏱️ 可自訂檢查間隔時間
- 🤖 支援 GitHub Actions 自動化執行

## 環境需求

- Python 3.11+
- Playwright（自動安裝 Chromium 瀏覽器）
- Discord Webhook URL

## 本地開發設定

### 1. 安裝依賴

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 設定環境變數

複製 `.env.example` 並重命名為 `.env`，填入您的 Discord Webhook URL：

```bash
cp .env.example .env
```

編輯 `.env`：
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_webhook_url
```

### 3. 設定監控來源

編輯 `config/sources.json`，新增或修改要監控的 Threads 帳號：

```json
[
  {
    "id": "unique_source_id",
    "platform": "threads",
    "name": "顯示名稱",
    "url": "https://www.threads.com/@username",
    "enabled": true,
    "check_interval_minutes": 60,
    "parser_type": "threads_public_profile"
  }
]
```

### 4. 執行程式

```bash
python app.py
```

## GitHub Actions 部署

### 1. 設定 Secret

在您的 GitHub repository 設定中：
1. 前往 **Settings** → **Secrets and variables** → **Actions**
2. 點擊 **New repository secret**
3. 名稱：`DISCORD_WEBHOOK_URL`
4. 值：貼上您的 Discord Webhook URL
5. 點擊 **Add secret**

### 2. 啟用 Workflow

程式碼推送到 GitHub 後，workflow 會自動啟用：
- 預設每小時執行一次（整點）
- 可手動觸發執行：**Actions** → **Threads Monitor Bot** → **Run workflow**

### 3. 調整執行頻率

編輯 `.github/workflows/monitor.yml` 中的 cron 表達式：

```yaml
schedule:
  - cron: '0 * * * *'  # 每小時
  # - cron: '*/30 * * * *'  # 每 30 分鐘
  # - cron: '0 */2 * * *'  # 每 2 小時
```

## 設定說明

### sources.json 參數

- `id`: 唯一識別碼
- `platform`: 固定填 `"threads"`
- `name`: 來源顯示名稱（會顯示在 Discord 通知中）
- `url`: Threads 帳號的完整 URL
- `enabled`: `true` 啟用 / `false` 停用
- `check_interval_minutes`: 檢查間隔（分鐘）
- `parser_type`: 固定填 `"threads_public_profile"`

## 專案結構

```
.
├── app.py                      # 主程式
├── requirements.txt            # Python 依賴
├── .env                        # 環境變數（不會提交到 Git）
├── .env.example                # 環境變數範例
├── config/
│   └── sources.json           # 監控來源設定
├── data/
│   └── state.json             # 運行狀態（自動生成）
└── .github/
    └── workflows/
        └── monitor.yml        # GitHub Actions 設定
```

## 運作原理

1. 讀取 `config/sources.json` 取得要監控的帳號列表
2. 檢查每個來源是否到達檢查間隔時間
3. 使用 Playwright 抓取 Threads 公開頁面
4. 解析 HTML 提取貼文 ID、內容、發布時間
5. 比對 `data/state.json` 過濾已通知的貼文
6. 將新貼文透過 Discord Webhook 發送通知
7. 更新狀態檔案記錄已通知的貼文

## 注意事項

- Threads 可能有速率限制，建議檢查間隔設定為 30 分鐘以上
- GitHub Actions 每次執行時間約 15-30 秒
- 首次執行會將最近的貼文全部通知一次
- state.json 會記錄最近 50 則已通知的貼文

## 故障排除

### Discord 通知失敗
- 檢查 Webhook URL 是否正確
- 確認 Webhook 沒有被刪除或停用

### 無法抓取貼文
- 確認 Threads 帳號是公開的
- 檢查網路連線是否正常
- Threads 頁面結構可能有更新，需要調整解析邏輯

### GitHub Actions 執行失敗
- 檢查 Secret 是否設定正確
- 查看 Actions 執行日誌找出錯誤原因

## 授權

MIT License
