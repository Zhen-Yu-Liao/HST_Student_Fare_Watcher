# THSR Auto Booker

這個專案是一個單腳本版本的台灣高鐵自動查票與自動訂位工具。
目前唯一正式入口是 `thsrc_auto_booker.py`。

功能維持原本腳本的流程：

- 使用 Playwright 開啟高鐵訂位頁面
- 自動處理驗證碼
- 反覆查詢指定班次與折扣
- 命中後自動進入訂位流程
- 寄送 Gmail 通知

## 安裝

1. 安裝 Python 3.10 以上版本。
2. 安裝套件：

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## `.env` 設定

先複製 `.env.example` 成 `.env`：

```bash
copy .env.example .env
```

`.env` 內容如下：

```ini
# 高鐵訂位資料
THSRC_IDNO=
THSRC_PHONE=
THSRC_EMAIL=

# Gmail 通知設定
GMAIL_SENDER=
GMAIL_APP_PASSWORD=
EMAIL_TO=
```

欄位說明：

- `THSRC_IDNO`：訂位使用的身分證字號
- `THSRC_PHONE`：手機號碼
- `THSRC_EMAIL`：訂位 email
- `GMAIL_SENDER`：寄信用 Gmail 帳號
- `GMAIL_APP_PASSWORD`：Gmail 應用程式密碼，不是一般登入密碼
- `EMAIL_TO`：通知收件人，可用逗號分隔多個 email

注意：

- 主腳本會先讀取 `CONFIG`，再用 `.env` 覆蓋對應敏感欄位。
- `THSRC_IDNO` 是必要欄位，空白時程式會在啟動前停止，不會等選到票後才送出空白取票識別碼。
- `THSRC_PHONE` 與 `THSRC_EMAIL` 不是程式啟動的硬性阻擋條件，但空白時可能收不到高鐵簡訊或 Email。
- 如果你手上的 `.env` 是舊版格式，只有 `LINE_*`、`SEARCH_ARGS`、`TARGET_KEYWORD` 這類欄位，請不要整份覆蓋，直接把上面三個 `THSRC_*` 欄位加進現有 `.env`。
- 如果你把 [thsrc_auto_booker.py](D:/User/桌面/Nathan/side_project/高鐵五折票/HST_Student_Fare_Watcher/thsrc_auto_booker.py) 內 `notify.enabled` 設成 `False`，就不一定要填 Gmail 相關欄位。

## `CONFIG` 需要改的欄位

搜尋條件仍然維持在 `thsrc_auto_booker.py` 內的 `CONFIG`：

- `CONFIG["search"]["origin"]`：出發站
- `CONFIG["search"]["dest"]`：到達站
- `CONFIG["search"]["date"]`：日期，格式 `YYYY-MM-DD`
- `CONFIG["search"]["time"]`：出發時間
- `CONFIG["search"]["adult"]`：全票張數
- `CONFIG["search"]["student"]`：學生票張數
- `CONFIG["search"]["discount_key"]`：想要命中的折扣文字，例如 `學生5折`
- `CONFIG["watch"]["interval_min"]` / `interval_max`：輪詢間隔
- `CONFIG["watch"]["until"]`：監看截止時間
- `CONFIG["browser"]["headless"]`：是否無頭執行

提醒：

- 學生折扣只會出現在學生票情境，所以 `student` 通常要大於 `0`。
- 如果你的目標是學生折扣，`discount_key` 應該填像 `學生5折`、`學生75折`、`學生88折` 這種文字。

## 執行方式

```bash
python thsrc_auto_booker.py
```

## 成功判定與本機紀錄

程式現在只有在完成頁面解析到 `8` 碼訂位代號時，才會輸出 `結果：booked`。

成功時終端機會印出：

```text
結果：booked
訂位代號：12345678
車次：817
```

同時會把完成頁保存到 `booking_logs/`，包含 `.html`、`.txt` 與 `.png`，方便後續確認付款/取票資料。這個資料夾可能包含個資，已加入 `.gitignore`，不要上傳。

如果程式送出後沒有解析到訂位代號，會輸出 `結果：confirmation_missing` 並停止自動重試，避免重複訂位。此時請先檢查 `booking_logs/` 內保存的頁面，再用高鐵官方網站或 T-EX App 查詢狀態。

以下狀態屬於本輪可重試失敗，程式會等待下一輪，不會整個中斷：

- `captcha_failed`：驗證碼多次辨識或重抓失敗。
- `submit_failed`：查詢送出失敗或等待結果超時。
- `sold_out`：送出訂位資料時，高鐵回覆座位或優惠座位已額滿。
- `round_exception`：還沒送出訂位資料前發生的頁面例外。

若已進入送出訂位資料後才發生未確認狀態，程式仍會停止，避免重複訂位。

## Gmail 應用程式密碼申請教學

如果你要開啟 Gmail 通知，`GMAIL_APP_PASSWORD` 必須填的是 **Google App Password**，不是一般登入密碼。

官方連結：

- Google 官方說明：[Sign in with app passwords](https://support.google.com/accounts/answer/185833?hl=en)
- 兩步驟驗證入口：[myaccount.google.com/signinoptions/two-step-verification](https://myaccount.google.com/signinoptions/two-step-verification)
- App Passwords 入口：[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)

申請步驟：

1. 先登入你的 Google 帳號。
2. 到兩步驟驗證頁面，開啟 `2-Step Verification`。
3. 開啟後，再進入 `App Passwords` 頁面。
4. 建立一組新的應用程式密碼。
5. 你會拿到一組 16 碼密碼，把它填進 `.env` 的 `GMAIL_APP_PASSWORD`。

常見失敗原因：

- 沒有開 `2-Step Verification`，就不會看到 `App Passwords`
- 如果你改過 Google 帳號密碼，舊的 App Password 可能失效，需要重新建立
- 你若把一般 Gmail 密碼填進 `GMAIL_APP_PASSWORD`，SMTP 會回 `535 5.7.8 Username and Password not accepted`

## 常見風險

- 高鐵網站結構、欄位名稱、驗證碼行為可能隨時改版，腳本可能因此失效。
- 無頭模式不一定比有頭模式穩定，遇到問題時先切回 `headless=False`。
- 自動訂位涉及個資與實際交易，第一次建議先用可接受的測試條件驗證流程。
- 通知寄信失敗不代表查票一定失敗，但會讓你收不到狀態回報。
