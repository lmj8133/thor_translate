# Thor 單機模式 — 代理跑在掌機上（Termux）

讓 Thor 不依賴 Mac：代理直接跑在 Thor 的 Termux 裡，PT 連本機 loopback，翻譯走 Gemini 雲端。出門只要有網路（含 iPhone 熱點）就能玩。

```
Thor 單機：PT → http://127.0.0.1:8000/v1（Termux 代理）→ Gemini 雲端備援鏈
```

注意：此模式下「本地 Sakura 兜底」自然停用（Thor 上沒有 Ollama；啟動 log 會有一行 pin 失敗的 WARNING，屬預期）。斷網 = 無翻譯；在家想要兜底就繼續用 Mac 模式（兩種模式可在 PT 裡並存，見 §6）。

## 1. 安裝 Termux（一次性，Thor 上）

- 從 **F-Droid** 安裝 Termux（**不要用 Play 商店版**——功能閹割且簽名不同）：https://f-droid.org/packages/com.termux/
- （選用，開機自啟）同一來源加裝 **Termux:Boot**，裝完**開啟過一次**即可

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
echo 'export GEMINI_API_KEY="AIza你的key"' > ~/thor-proxy/env.sh
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

啟動 log 應見 `Cloud model gemini-3.5-flash-lite: OK` 等三行（Ollama pin WARNING 屬預期）。裝了 Termux:Boot 的話，重開機會自動啟動（log 寫到 `~/thor-proxy/proxy.log`）。

## 6. PT 端設定

Translation services 裡把 Custom URL 改為（或**新增第二個** OpenAI Custom 服務並排在第一位）：

- **Custom URL**：`http://127.0.0.1:8000/v1`（PT 白名單允許 http 連 loopback）
- API key 任意非空、目標語言 **Chinese (Simplified)**（同 Mac 模式，PT 不做轉換）
- **Model = 遊戲選擇器**：在 Translation services **清單**（不是編輯頁），服務開關開啟後其下方的小字子列 → 點開就是術語庫選單（pokemon-oras 等）。之後玩別款：建 `glossaries/<名字>.txt` → `deploy_thor.sh` 推上去 → 在這個選單點它——**換遊戲不用碰 Termux 也不用重啟**
- 術語庫**只由這個選單決定**（沒選 = 不注入，啟動 log 會提示）；若想要「沒選時的預設表」，在 `~/thor-proxy/env.sh` 加 `export GLOSSARY_PATH=glossaries/pokemon-oras.txt`

保留原本指向 Mac 的服務作第二位 → 在家時 Thor 代理掛了還有 Mac 撐著；出門時只有第一位生效。

## 6.5 自救指南（代理掛了怎麼辦）

先判斷死活：**下拉通知列——有「Termux」常駐通知＝護體在、代理活著**；沒有＝掛了。

| 情境 | 自救動作 |
|------|----------|
| 重開機後 | **全自動**，什麼都不用做（Termux:Boot 以前景腳本啟動，服務自帶護體）。想確認就看通知列 |
| 誤把 Termux 從最近任務滑掉／翻譯突然切到備援 | 開 Termux → 輸入 **`proxy`** ↵（一個字的別名）→ 回 PT 把本機服務開關切一下清除冷卻（或等它自動重試）。之後**用 HOME 鍵離開 Termux**，別再滑掉 |
| 電腦在旁邊 | 一行搞定：`bash tools/deploy_thor.sh`（會自動確保 Termux 服務存在再重啟代理） |
| 完全不確定狀態 | 同上跑 deploy 腳本——它的健康檢查會直接告訴你答案 |

沒自救期間也不會開天窗：在家 PT 自動退到 Mac 服務；在外退到 PT 內建引擎（品質降級但仍可玩）。

## 7. 疑難排解

| 症狀 | 處理 |
|------|------|
| `[Process completed (signal 9)]` | Phantom killer 又殺程序——確認 §4-2 的 settings 值仍為 false（某些系統更新會重置） |
| 代理無聲死亡（log 無錯誤戛然而止）＋ PT 顯示 Connection failed 切到備援 | Android 空進程清理殺了整個 Termux uid——**Termux 服務必須保持存活**：開機路徑由前景常駐的 boot 腳本保障；手動啟動時保持 Termux app 開過且**不要從最近任務滑掉**；logcat 可見 `Killing com.termux (adj 985): empty` 佐證 |
| 重開機後譯文變**簡體亂翻**（如 欢迎来到Bokket怪物） | **開機競速**：PT 擷取起得比 Wi-Fi／代理早，首波請求失敗 → 兩個自訂服務被打入 ~30 分鐘冷卻 → 落到內建引擎。**修復：Translation services 把兩個 Custom 服務開關各切一次**（清冷卻）。**預防：開機後等通知列出現 Termux 通知再開遊戲** |
| 螢幕關閉後翻譯停止 | **這是刻意設計**：翻譯只在螢幕亮時發生（PT 擷取需要螢幕），代理隨裝置休眠、閒置零耗電；螢幕一亮立即恢復 |
| 啟動極慢／pip 編譯錯誤 | 確認裝的是 F-Droid 版 Termux；依賴全為純 Python，**不應**出現編譯——若出現，貼錯誤訊息回報 |
| 雲端三行全 unreachable | Thor 沒網路，或 DNS 問題——瀏覽器開任意網站確認連線 |
| 電量消耗 | 代理**不持有 wake-lock**，閒置＝正常深度休眠、零額外耗電；遊戲時螢幕耗電遠大於代理 |
