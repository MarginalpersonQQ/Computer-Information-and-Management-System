# 永暘電腦維修管理系統

適合手機瀏覽器使用的內部維修工作台，以 Python、SQLite 與原生 HTML/CSS/JavaScript 實作。部署於店內 Ubuntu，店外透過 WireGuard 連線。

## 功能

- 個人／公司客戶、對接人、電話、地址、LINE 名稱與選填 ID。
- 電腦配置、機身序號、AnyDesk 編號與永久設備 QR Code。
- 每台電腦的照片、封面縮圖、拍照／相簿上傳。
- 階層式維修項目，可新增、改名及刪除大項與細項；歷史紀錄保留原名稱。
- 維修問題、備註、總金額、收款及維修進度。
- 「已完成待送回」進度，以及依未結案案件計算的總表。
- Windows 登入密碼與自訂帳號密碼，以 Fernet 加密保存。
- 管理員、維修人員與唯讀角色，客戶／電腦封存及確認刪除流程。
- 完整資料庫備份及一般資料 JSON 匯出。

總表沒有未結案案件時顯示「無待修案件」；有案件時顯示最新未結案案件的進度，多筆未結案會顯示件數。

## 程式結構

```text
server.py                 HTTP API、權限、資料庫與備份
vault.py                  設備帳密的加密與解密
photos.py                 圖片驗證、壓縮及移除 EXIF
static/                   網頁、樣式與前端程式
tests/                    使用暫存資料的功能及狀態測試
yongyang.service          systemd 使用者服務範本
yongyang-backup.service   完整備份服務
yongyang-backup.timer     每日備份計時器
requirements.txt          Python 執行依賴
```

原有的 [架構圖](Archetecture%20Diagram.drawio) 亦保留在 repository 中。

核心程式及前端檔案取自正式系統。systemd 範本改用虛擬環境執行，預設只監聽本機；安裝時再依環境調整。

## 本機啟動

需要 Python 3.10 以上。以下以 Ubuntu 為例：

```sh
sudo apt update
sudo apt install python3 python3-venv
git clone https://github.com/MarginalpersonQQ/Computer-Information-and-Management-System.git ~/yongyang
cd ~/yongyang
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py --init-admin
HOST=127.0.0.1 PORT=8080 BASE_URL=http://127.0.0.1:8080 .venv/bin/python server.py
```

開啟 `http://127.0.0.1:8080`。建立管理員時會在終端機互動輸入帳號與密碼；沒有預設密碼。已有帳號時初始化會被拒絕，請從管理介面新增人員。

Windows 可使用 `.venv\Scripts\python.exe` 取代 `.venv/bin/python`，並以 PowerShell 的 `$env:HOST`、`$env:PORT`、`$env:BASE_URL` 設定環境變數。

### 環境變數

| 變數 | 用途 |
| --- | --- |
| `HOST` | 監聽位址，未設定時為 `127.0.0.1` |
| `PORT` | HTTP 埠，預設 `8080` |
| `BASE_URL` | 使用者可到達的入口網址，用於 QR Code |
| `DATA_DIR` | 資料儲存目錄，預設為程式旁的 `data/` |

正式程式保留既有內部網路的 `BASE_URL` 預設值；新安裝請明確設定自己的 `BASE_URL`。程式不會自動讀取 `.env`。

## Ubuntu 自動啟動

服務範本以程式位於 `~/yongyang`、虛擬環境位於 `~/yongyang/.venv` 為前提。其他目錄請同步修改服務檔。

1. 先完成安裝、建立管理員及本機測試。
2. 編輯 `yongyang.service` 的 `HOST` 與 `BASE_URL`，改成實際的 LAN／VPN 入口。
3. 安裝服務：

```sh
mkdir -p ~/.config/systemd/user
cp yongyang.service yongyang-backup.service yongyang-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now yongyang.service yongyang-backup.timer
sudo loginctl enable-linger "$USER"
```

```sh
systemctl --user status yongyang
journalctl --user -u yongyang -n 100 --no-pager
systemctl --user restart yongyang
```

本系統目前供可信任的店內網路及 WireGuard 使用。請僅允許實際 LAN／VPN 網段連線；不要直接開放至公網。若要對外提供服務，需先規劃 HTTPS、對外存取權限及部署方式。

QR Code 內包含 `BASE_URL`。請固定伺服器的內部 IP；日後變更網址需維持舊網址轉址，或重新列印貼紙。

## 備份與還原

執行程式後會建立：

- `data/repair.sqlite3`：客戶、電腦、維修、照片及加密設備帳密。
- `data/credentials.key`：解密設備帳密所需的金鑰。

完整備份使用 SQLite 備份 API，可在服務運作時執行：

```sh
mkdir -p backups
.venv/bin/python server.py --backup "backups/repair-$(date +%Y%m%d-%H%M%S).sqlite3"
```

會產生配對的 `.sqlite3` 和 `.key`，必須一起保存。照片已包含在資料庫備份內；一般 JSON 匯出不包含照片、設備帳密、使用者或登入狀態，不能取代完整備份。

每日計時器預設在伺服器當地時間 03:00 執行；舊備份不會自動刪除。請定期將配對備份複製到其他保管位置。

還原時先停止服務並備份現況，再將選定的 `.sqlite3` 放回 `data/repair.sqlite3`，將配對 `.key` 放回 `data/credentials.key`，最後啟動服務。還原會回到該備份的時間點。

```sh
chmod 600 data/credentials.key
systemctl --user start yongyang
```

金鑰不在 repository 中；僅有程式碼無法還原既有客戶資料或解密原本的設備帳密。

## 權限與刪除

管理員可管理人員、維修分類及封存／刪除操作。維修人員可管理客戶、電腦、維修、照片及設備帳密；唯讀人員可查看一般紀錄與照片，不能修改，也無法取得設備帳密。

刪除客戶會先顯示名下電腦、維修、照片及帳密資料數量，要求輸入完整客戶名稱並再次確認；刪除會包含已封存設備，原 QR Code 隨之失效。確認期間資料變動時，操作會被拒絕。單獨刪除電腦仍限沒有維修紀錄的設備。

密碼以加密形式存於資料庫，但金鑰位於同一台主機，不能防止擁有主機管理權限的人讀取。只保管取得客戶同意的資料。

## 測試

先安裝 `requirements.txt`。後端測試只使用暫存資料，不連線正式系統：

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

前端狀態測試另需 Node.js，執行服務本身不需要 Node.js：

```sh
node tests/test_work_status.cjs
node --check static/app.js
```

## 不應提交的檔案

`.gitignore` 已排除客戶資料目錄、照片／上傳目錄、備份、資料庫、加密金鑰、環境檔、SSH／WireGuard 憑證及本機虛擬環境。此 repository 僅包含程式、部署範本、說明與測試用的虛構資料。
