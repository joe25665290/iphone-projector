# Projector

用一條 USB 線，把 iPhone 的畫面和聲音即時投到 Windows 視窗，OBS 和 Discord 都能直接擷取。

- 手機上**不用裝任何 App**。它用的是 Mac 的 QuickTime 錄 iPhone 螢幕時那套 USB 擷取協定，由手機的硬體編碼器處理，負擔很小。
- 實測（iPhone 15 / iOS 18.2.1）：1180×2556、最高 60 fps 的畫面，48 kHz 立體聲，聲音延遲約 165 ms。

## 需求

- Windows 10 / 11，以及一條能傳輸資料的 USB 線
- Python 3.12（透過 `py` 啟動器）
- **這台電腦已經信任過這支 iPhone**：裝過 Apple 裝置 App 或 iTunes，接上時在 iPhone 上按過「信任」。
  程式會用 Apple 留在 `C:\ProgramData\Apple\Lockdown` 的配對紀錄，所以這一步要在換驅動**之前**完成。

## 安裝（只需一次）

### 1. 換驅動（Zadig）

Windows 預設的 Apple 驅動不允許 iPhone 切換到擷取模式，所以要把**這支 iPhone**的驅動換成 libusb-win32。

> 換了之後，iTunes、Apple 裝置 App、備份、照片匯入都會看不到這支 iPhone，直到換回來為止。

1. 從 <https://zadig.akeo.ie> 下載 Zadig（免安裝）。
2. iPhone 接上 USB 並解鎖。
3. 開 Zadig，選單 **Options**：
   - 勾選 **List All Devices**
   - **取消勾選** Ignore Hubs or Composite Parents
4. 下拉選單選 **iPhone**（USB ID `05AC 12A8`，左邊 Driver 欄位是 `usbccgp`）。
   - Zadig 顯示的是 iPhone 自己回報的名稱，不是裝置管理員裡的「Apple Mobile Device USB Composite Device」。
   - 不要選 **Apple USB Multiplexor** 或 **PTP**。那是單一介面，只換其中一個切不了擷取模式。
   - 看不到 **iPhone** 的話，代表 Ignore Hubs or Composite Parents 還勾著。
5. 右邊選 **libusb-win32**，按 **Replace Driver**。

**換回 Apple 驅動：** 裝置管理員 → 找到 iPhone（在 libusb-win32 devices 底下）→ 右鍵「解除安裝裝置」→ 勾選「嘗試移除此裝置的驅動程式」→ 拔掉 iPhone 再插回去。

### 2. 安裝 Python 套件

套件只會裝在專案的 `.venv` 資料夾裡，不會動到系統的 Python。

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## 使用

1. iPhone 接上 USB 並解鎖。
2. 雙擊 **`Projector.bat`**（或執行 `.venv\Scripts\python app.py`）。
3. 「Projector - iPhone」視窗出現畫面就可以用了。iPhone 轉成橫向時，視窗會跟著轉。
4. 結束時**關閉 Projector 視窗**（不是主控台），程式會先讓 iPhone 正常結束擷取。

擷取期間：
- iPhone 狀態列會顯示「9:41、滿電」，動態島也會出現擷取提示，這些都是正常的。
- 聲音改由電腦播放，iPhone 喇叭不會出聲，iPhone 的音量鍵也沒有作用。
- 擷取一開始，YouTube 這類 App 會**自動暫停**，再按一次播放即可。
- 拔掉 iPhone 後，視窗會顯示「等待 iPhone 連接」，接回去會自動重新連線。

### 調整音量

在 Projector 視窗上操作，畫面上方會出現音量條：

| 操作 | 作用 |
|---|---|
| 上／下方向鍵、滑鼠滾輪、`+`／`-` | 音量 ±10%，範圍 0–200%（超過 100% 會放大） |
| `M` | 靜音／取消靜音 |

- 音量會存在 `%APPDATA%\Projector\settings.json`，下次開啟沿用。

### 參數

| 參數 | 用途 |
|---|---|
| `--volume 0–200` | 起始音量（%，預設沿用上次的設定） |
| `--list-audio-devices` | 列出聲音輸出裝置 |
| `--audio-device 名稱或編號` | 指定聲音輸出裝置（預設是系統預設裝置） |
| `--serial 序號` | 接了多支 iPhone 時，指定要用哪一支 |
| `--log 路徑` | 紀錄檔位置（預設 `out\app.log`） |
| `--seconds N --screenshot 路徑` | 自我測試：收到畫面 N 秒後截圖並結束 |

## 疑難排解

| 狀況 | 處理方式 |
|---|---|
| 一直顯示「等待 iPhone 連接」 | 確認 USB 線有接好、iPhone 已解鎖，而且 Zadig 換的是整台 **iPhone**（見安裝步驟 1）。 |
| 顯示「iPhone 沒有回應」 | iPhone 的擷取服務卡住了，通常是之前的擷取沒有正常結束。拔插沒有用，要**重開 iPhone**。 |
| 有畫面但沒有聲音 | 先確認 iPhone 上的影片真的在播放（擷取開始時常被自動暫停）。如果還是沒聲音，可能是 iPhone 連線前的音量為 0：iPhone 送出來的就是靜音，Projector 再怎麼放大也聽不到。請拔掉 USB，把 iPhone 音量調高，再接上。 |
| 錯誤訊息提到配對紀錄或 StartSession | iPhone 不信任這台電腦了。暫時換回 Apple 驅動，接上並在 iPhone 按「信任」，再換回 libusb-win32。 |
| 按 Ctrl+C 或程式出錯 | 程式會盡量讓 iPhone 正常結束擷取。之後若出現「iPhone 沒有回應」，就重開 iPhone。 |

回報問題時，請附上 `out\app.log`。**重新開啟 Projector 會覆蓋這個檔案**，所以要先把它存起來。

## 運作方式

1. 透過 usbmux（iPhone 和電腦之間的多工通道），用配對紀錄建立受信任連線（StartSession + TLS），向 iPhone 證明這台電腦是它信任的主機。
2. 送出一個廠商請求，讓 iPhone 多出第 5 組 USB 設定（擷取模式）。切換過去後，再建立一次受信任連線。
3. 在擷取介面上回應 iPhone 的握手和時鐘同步，接收 H.264 畫面和 LPCM 聲音。
4. 結束時請 iPhone 停止傳送，等它確認釋放之後，再重設 USB。

沒有受信任連線的話，iPhone 只會送一個 PING，之後就不再傳送。如果擷取沒有正常結束，iPhone 的擷取服務會卡住，要重開 iPhone 才會恢復。

## 開發

### 測試腳本

不開視窗，直接錄一段畫面和聲音，用來確認 USB 這一段是否正常：

```powershell
.venv\Scripts\python spike.py --list   # 只列出 USB 看到的 iPhone，不改任何東西
.venv\Scripts\python spike.py          # 收到畫面後錄 30 秒，輸出到 out\
```

輸出 `video.h264`（原始畫面）、`audio.wav`（聲音）和 `spike.log`（完整紀錄）。成功時，最後會顯示「畫面 OK，聲音 OK」。

`tools\bench_decode.py out\...\video.h264` 會把錄下來的畫面解出一張截圖，並測量各種顯示方式的速度。

### 單元測試

用真實封包樣本測試協定，不需要接手機：

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

`tests/` 目前被 `.gitignore` 的 `test*` 規則排除，沒有納入版本控制。

### 專案結構

| 路徑 | 用途 |
|---|---|
| `app.py`、`Projector.bat` | App：視窗、按鍵、狀態顯示 |
| `projector/capture.py` | 擷取引擎：連線順序、自動重連、正常結束 |
| `projector/protocol.py` | 擷取協定：封包、握手、時鐘、畫面／聲音樣本解析 |
| `projector/usb_transport.py` | 透過 libusb0 切換擷取模式並收送 USB 資料 |
| `projector/usbmux.py` | usbmux 協定（iPhone 服務的多工通道） |
| `projector/lockdown.py` | 用配對紀錄建立受信任連線 |
| `projector/video.py` | H.264 低延遲解碼，並縮放到視窗大小 |
| `projector/audio.py` | 聲音播放、緩衝與音量 |
| `projector/settings.py` | 使用者設定（音量） |
| `projector/sinks.py` | 測試腳本用：寫出 .h264／.wav 並統計 |
| `spike.py` | 測試腳本 |
| `tools/bench_decode.py` | 解碼速度測試 |
| `tests/` | 協定單元測試（不在 git 裡） |

## 致謝

協定移植自 [danielpaulus/quicktime_video_hack](https://github.com/danielpaulus/quicktime_video_hack)（MIT），
並參考 [chotgpt/quicktime_video_hack_windows](https://github.com/chotgpt/quicktime_video_hack_windows)（MIT）的 Windows 做法。
`tests/fixtures/qvh/` 裡的封包樣本來自 qvh，授權見同一資料夾的 `LICENSE-qvh`。
