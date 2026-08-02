# Phase 01: 裝置實測與路線判定

> **✅ 本階段已於 2026-08-02 完成關閉**,全部結果與路線決定見 [verification-results.md](./verification-results.md)。以下為原始執行指引,僅供回顧。

> Depends on: none（第一階段）
> Produces: `docs/verification-results.md`（填寫完成）、路線決定（Gate G1–G4）

本階段無程式碼，純裝置驗證。目標：用最小成本回答 master plan 的四個 Gate，避免把工程投入建立在未驗證的平台行為上。

## Pre-flight

- Thor 已開啟「開發人員選項」與 USB/無線偵錯
- PC 端 `adb devices` 能看到裝置

## Steps

### 1. 記錄裝置基本資料

**File:** `docs/verification-results.md`（MODIFY：填入「裝置資訊」節）

```bash
adb shell getprop ro.product.model
adb shell getprop ro.build.version.release          # Android version
adb shell head -1 /proc/meminfo                     # RAM total → 判定 SKU (8/12/16GB)
adb shell dumpsys display | grep -E "mDisplayId|DisplayDeviceInfo"
```

記錄：SKU/RAM、Android 版本、上/下螢幕各自的 displayId（預期上螢幕 = display 0 主顯示器，請以 dumpsys 實證）。

### 2. 安裝 PlayTranslate

1. 從 <https://github.com/dominostars/playtranslate/releases> 下載最新 APK
2. Play 商店 → 設定 → Play Protect → **暫時**關閉掃描（裝完記得開回來）
3. `adb install <apk>` 或裝置上直接安裝
4. （建議）用 Obtainium 訂閱該 repo，之後自動追更新

### 3. 基準測試（不含模擬器）

在瀏覽器開一段日文文字，測 PlayTranslate 本體：

- [ ] region picker 能框選畫面區域
- [ ] live mode 能自動偵測文字變化並更新翻譯
- [ ] 目標語言可選繁體中文，輸出正常（先用內建本地模型或填一組雲端 API key）
- 記錄體感延遲

### 4. 安裝/設定 Azahar

1. 官方 APK（<https://azahar-emu.org> 或 GitHub releases），版本需 ≥ 2123.3（含雙螢幕掌機修正）
2. 設定 → **Secondary Screen Layout**，確認雙螢幕正常：上螢幕 = 3DS 上畫面、下螢幕 = 3DS 下畫面
3. 順手記錄：此設定切換是否免重啟（Phase 05 方案 B 依賴此行為）

### 5. G1/G2 關鍵測試：兩者同時運作

Azahar 跑任一日文遊戲，啟動 PlayTranslate live mode 框選上螢幕對話區：

- [ ] **G1**：擷取畫面乾淨（非黑屏/破圖）——若黑屏，代表 secure-surface/DRM 阻擋，截圖式方案全滅
- [ ] **G2**：翻譯結果能顯示在**下螢幕**（浮窗或其他形式）
- [ ] 對話變化後自動更新；記錄「對話出現 → 譯文顯示」體感延遲
- [ ] 觀察疊層關係：PlayTranslate 浮窗與 Azahar 的下螢幕畫面誰在上？能否互相切換？（Phase 05 選方案的依據）

### 6. G3：引擎設定檢查

PlayTranslate 設定 → 翻譯引擎：

- [ ] **G3**：OpenAI（相容）引擎是否可自訂 base URL？截圖記錄設定頁
- [ ] 繁體中文是否為該引擎可選的目標語言

### 7. G4：捲動續行行為

找一款「兩行對話框、一次捲一行」的遊戲（多數 JRPG 長對話皆是），觀察：

- [ ] **G4**：是否重複翻譯已顯示過的行？
- [ ] 開啟 v3.0.0 的 context（實驗性）功能後是否改善？改善到什麼程度？

### 8. 輸入路由怪癖

Thor 的手把輸入會路由到「最後被觸碰的螢幕」：

- [ ] 觸碰下螢幕（操作 PlayTranslate）後，手把是否還能控制遊戲？
- [ ] AYN 設定的 **Auto Lock** 開啟後，輸入是否穩定鎖在上螢幕？

### 9. （選）對照組：ThorTranslate

安裝 <https://github.com/magiobus/thortranslate>（MIT），體驗「遊戲在上、翻譯 app 釘在下」架構的操作感，作為 Phase 05 方案 B 的參考。

### 10. 填表與路線判定

完成 `docs/verification-results.md` 的 Gate 判定表，依 [00-master-plan.md](./00-master-plan.md) 的 Decision Gates 寫下路線決定與理由。

## Phase Verification

- [ ] `docs/verification-results.md` 所有欄位已填
- [ ] G1–G4 皆有明確「是/否」+ 證據（截圖或具體描述）
- [ ] 路線決定已寫入該檔

## Regression Tests

本階段無程式碼，略。產出的 `verification-results.md` 即為後續所有階段的決策依據文件。
