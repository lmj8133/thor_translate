# Thor 單機模式 — 代理跑在掌機上（Termux）

讓 Thor 不依賴 Mac：代理直接跑在 Thor 的 Termux 裡，PT 連本機 loopback，翻譯走多供應商雲端端點鏈（Gemini／Groq／Cloudflare／Z.ai）。出門只要有網路（含 iPhone 熱點）就能玩。

```
Thor 單機：PT → http://127.0.0.1:8000/v1（Termux 代理）→ 雲端端點鏈（CLOUD_ENDPOINTS）
```

**金鑰只住在這裡**：雲端端點鏈的所有 key 都設定在 Thor 端的 `env.sh`；Mac 端的代理是純 Sakura 兜底、刻意不設金鑰（見 README.md 架構節）。

注意：此模式下「本地 Sakura 兜底」自然停用（Thor 上沒有 Ollama；啟動 log 會有一行 pin 失敗的 WARNING，屬預期）。斷網 = 無翻譯；在家想要兜底就繼續用 Mac 模式（兩種模式可在 PT 裡並存，見 §6）。

## 1. 安裝 Termux（一次性，Thor 上）

- 從 **F-Droid** 安裝 Termux（**不要用 Play 商店版**——功能閹割且簽名不同）：https://f-droid.org/packages/com.termux/

## 2. 推送檔案（WSL 端，adb）

**首次安裝**時手動推送：

```bash
adb shell mkdir -p /sdcard/thor-proxy
adb push server /sdcard/thor-proxy/
adb push glossaries /sdcard/thor-proxy/
```

**之後的所有更新**（程式碼或術語表）一律用一鍵部署腳本——推送、同步、重啟、健康檢查全包：

```bash
bash tools/deploy_thor.sh
```

（前提：Thor 的無線 adb 連著。**重開機、甚至換網路（外出用熱點）都不用查埠號**——deploy 腳本斷線時自動呼叫 `tools/connect_thor.sh`：依「THOR_IP 環境變數 → 上次成功 IP → 家中預設 IP → 全子網掃描」逐層探索重連；配對關係跨重啟、跨網路皆有效。唯一的手動步驟：「無線偵錯」**重開機後必定被關閉**（此 ROM 實測確認），要部署前在 Thor 上重新開啟——**快速設定最後一頁有「無線偵錯」磁貼：此 ROM 上點按切換是壞的，但長按可直達設定頁**，進去開一下即可（約 5 秒）。翻譯本身不需要 adb，這只影響「部署程式」；整條重啟鏈（開機自啟代理、phantom 設定持久化、自動掃埠重連）已於 2026-08-03 實機重啟驗證全數存活）

## 3. Termux 內初始化（一次性，Thor 上）

開啟 Termux，執行：

```bash
termux-setup-storage        # 跳權限視窗 → 允許
bash /sdcard/thor-proxy/server/thor/setup-termux.sh
```

金鑰設定檔 `env.sh` 在**電腦端 repo 根目錄**維護（含全部供應商的 key，已被 gitignore 保護；格式見 README.md「多供應商端點鏈」），用 adb 推送進 Termux home：

```bash
adb push env.sh /sdcard/thor-proxy/env.sh
adb shell "run-as com.termux sh -c 'cp /storage/emulated/0/thor-proxy/env.sh files/home/thor-proxy/env.sh'"
adb shell rm /sdcard/thor-proxy/env.sh    # /sdcard 對其他 app 可讀，不留金鑰
```

## 4. 系統防護設定（一次性）

1. **電池**：設定 → 應用程式 → Termux → 電池 → **不受限制**
2. **Phantom process killer**（Android 13 會殺 Termux 子程序，症狀 `signal 9`）——由 WSL 端 adb 執行：

   ```bash
   adb shell "settings put global settings_enable_monitor_phantom_procs false"
   adb shell "settings get global settings_enable_monitor_phantom_procs"   # 應回 false
   ```

   還原方法：`adb shell "settings delete global settings_enable_monitor_phantom_procs"`
3. 不要從最近任務清單把 Termux 滑掉

## 5. 啟動

```bash
bash ~/thor-proxy/server/thor/run-proxy.sh
```

啟動 log 應見 `Cloud key1/gemini-3.1-flash-lite: OK` 等各端點的煙霧測試行（Ollama pin WARNING 屬預期；Thor 啟動腳本預設 `STARTUP_SMOKE=0` 時則無煙霧測試行）。

> **開機不會自動啟動**（刻意的）：Termux:Boot 會在 Wi-Fi 連上之前就把代理拉起來，結果是「連得上但翻不動」，而 PT 首波請求失敗就會把服務打入約 30 分鐘冷卻——反而更糟。**每次重開機後手動開 Termux 輸入 `proxy` ↵ 即可**（一個字的別名），網路此時早已就緒。

### 何時可以開遊戲——開 status 頁

Thor 的瀏覽器開 **`http://127.0.0.1:8000/status`**（建議加書籤），頁面每 5 秒自動刷新，最上方那條綠色橫幅就是就緒狀態：

| 橫幅內容 | 意義 |
|----------|------|
| `Starting…` | 程式起來了，正在釘本地模型／做端點煙霧測試 |
| `Waiting for network… — do NOT start the game yet` | **還不能開遊戲**：開機時連接埠開得比 Wi-Fi 早，此時翻譯會失敗（還會害 PT 把服務打入約 30 分鐘冷卻） |
| `✅ Ready — N cloud endpoints` | 雲端已連得上，**現在開遊戲第一句就會成功** |
| `Ready — local Sakura only` | 沒設雲端金鑰的部署（如 Mac 端），本地模式待命中 |

橫幅下方還有即時狀態：**現在哪個模型在翻**、單句耗時、今天已翻幾句、每個端點是 leader／ready／cooling（冷卻中會顯示剩餘時間）、快取命中率。

> 註：狀態一律看這個頁面。曾嘗試改用 Android 通知列，但本機 Termux:API（GitHub 0.53）的 `termux-notification` 不會真的更新通知、進程永不結束且無法 kill（實測幾分鐘堆積 14 個殭屍進程），該功能已整個移除。

## 6. PT 端設定

Translation services 裡把 Custom URL 改為（或**新增第二個** OpenAI Custom 服務並排在第一位）：

- **Custom URL**：`http://127.0.0.1:8000/v1`（PT 白名單允許 http 連 loopback）
- API key 任意非空、目標語言 **Chinese (Simplified)**（同 Mac 模式，PT 不做轉換）
- **Model = 遊戲選擇器**：在 Translation services **清單**（不是編輯頁），服務開關開啟後其下方的小字子列 → 點開就是術語庫選單（pokemon-oras 等）。之後玩別款：建 `glossaries/<名字>.txt` → `deploy_thor.sh` 推上去 → 在這個選單點它——**換遊戲不用碰 Termux 也不用重啟**
- 術語庫**只由這個選單決定**（沒選 = 不注入，啟動 log 會提示）；若想要「沒選時的預設表」，在 `~/thor-proxy/env.sh` 加 `export GLOSSARY_PATH=glossaries/pokemon-oras.txt`

保留原本指向 Mac 的服務作第二位 → 在家時 Thor 代理掛了還有 Mac 撐著；出門時只有第一位生效。

## 6.5 自救指南（代理掛了怎麼辦）

先判斷死活：**開 `http://127.0.0.1:8000/status`**——綠色橫幅顯示 `✅ Ready` ＝可以玩；顯示 `Waiting for network…` ＝先確認 Wi-Fi；**整頁打不開**＝代理掛了（見下表）。（下拉通知列有 Termux 常駐通知也可佐證 Termux 服務還活著。）

| 情境 | 自救動作 |
|------|----------|
| 重開機後 | 開 Termux → 輸入 **`proxy`** ↵ → 開 `/status` 確認綠色 `✅ Ready`（開機不自動啟動，見 §5） |
| 誤把 Termux 從最近任務滑掉／翻譯突然切到備援 | 開 Termux → 輸入 **`proxy`** ↵（一個字的別名）→ 回 PT 把本機服務開關切一下清除冷卻（或等它自動重試）。之後**用 HOME 鍵離開 Termux**，別再滑掉 |
| 電腦在旁邊 | 一行搞定：`bash tools/deploy_thor.sh`（會自動確保 Termux 服務存在再重啟代理） |
| 完全不確定狀態 | 同上跑 deploy 腳本——它的健康檢查會直接告訴你答案 |

沒自救期間也不會開天窗：在家 PT 自動退到 Mac 服務；在外退到 PT 內建引擎（品質降級但仍可玩）。

## 7. 疑難排解

| 症狀 | 處理 |
|------|------|
| `[Process completed (signal 9)]` | Phantom killer 又殺程序——確認 §4-2 的 settings 值仍為 false（某些系統更新會重置） |
| 代理無聲死亡（log 無錯誤戛然而止）＋ PT 顯示 Connection failed 切到備援 | Android 空進程清理殺了整個 Termux uid——**Termux 服務必須保持存活**：保持 Termux app 開著且**不要從最近任務滑掉**（用 HOME 鍵離開）；logcat 可見 `Killing com.termux (adj 985): empty` 佐證 |
| 重開機後譯文變**簡體亂翻**（如 欢迎来到Bokket怪物） | **開機競速**：PT 擷取起得比代理早，首波請求失敗 → 兩個自訂服務被打入 ~30 分鐘冷卻 → 落到內建引擎。**修復：Translation services 把兩個 Custom 服務開關各切一次**（清冷卻）。**預防：重開機後先手動啟動代理（`proxy` ↵）、看到 `/status` 綠色 `✅ Ready` 再開遊戲** |
| 螢幕關閉後翻譯停止 | **這是刻意設計**：翻譯只在螢幕亮時發生（PT 擷取需要螢幕），代理隨裝置休眠、閒置零耗電；螢幕一亮立即恢復 |
| 啟動極慢／pip 編譯錯誤 | 確認裝的是 F-Droid 版 Termux；依賴全為純 Python，**不應**出現編譯——若出現，貼錯誤訊息回報 |
| 雲端三行全 unreachable | Thor 沒網路，或 DNS 問題——瀏覽器開任意網站確認連線 |
| 電量消耗 | 代理**不持有 wake-lock**，閒置＝正常深度休眠、零額外耗電；遊戲時螢幕耗電遠大於代理 |
