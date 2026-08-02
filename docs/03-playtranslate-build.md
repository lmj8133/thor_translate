# Phase 03: PlayTranslate fork 建置與源碼導覽

> **⚠ 2026-08-02 修正前言(依 Phase 01 結果,優先於下方原文)**:
>
> 1. **G3=是 → Step 5(自訂 base URL patch)整段略過**,內建 Custom preset 已實證可用。
> 2. **`docs/playtranslate-architecture-notes.md` 已於 Phase 01 先行建立**(7-agent 源碼調查產物,含 G2/G3/G4 相關類別與行號、管線總覽、patch 插入點)。Step 4 改為「驗證與擴充」該檔,勿重建。
> 3. 上游原始碼已 shallow clone 於 `~/thor-work/playtranslate`(v3.0.1,唯讀參考用);fork 仍照 Step 1 另行 clone 到建置位置。
> 4. **本階段完成後的首要 patch 是 Phase 05 的 overlay-hosted 下螢幕譯文面板**(非 Phase 04);見 verification-results.md 路線決定。
> Produces: 可安裝的 debug APK、`docs/playtranslate-architecture-notes.md`、（視 G3）自訂 endpoint patch

目標：能從原始碼建出與 release 行為一致的 APK，並摸清管線落點，為 Phase 04/05 的 patch 鋪路。

## Pre-flight

- Phase 01 完成，`docs/verification-results.md` 判定走路線 A
- Android Studio 或 command-line SDK 可用（建議 Windows 側）

## Steps

### 1. Fork 與 clone

1. GitHub 上 fork `dominostars/playtranslate` 至個人帳號
2. Clone 至 **WSL 家目錄**或 Windows 側（勿放 `/mnt/c` 下建置，gradle I/O 極慢）：

```bash
git clone git@github.com:<you>/playtranslate.git ~/src/playtranslate
cd ~/src/playtranslate
git remote add upstream https://github.com/dominostars/playtranslate.git
```

3. 在 `docs/verification-results.md` 附註 fork 的實際路徑；本 repo 若日後 `git init`，勿將 fork 巢狀納入版控

### 2. 建置環境與首次建置

1. 讀 fork 的 README / CONTRIBUTING / build 說明（**以其為準**——本文件不臆測其 gradle 結構與 module 名）
2. 依其指示建置 debug APK（典型為 `./gradlew assembleDebug`）
3. `adb install` 至 Thor

### 3. 對照驗證（排除建置差異）

用 debug 版重跑 Phase 01 的 Step 5（G1/G2 測試），確認行為與官方 release APK 一致。不一致就先解決（簽章、flavor、缺 secrets 等），否則後續 patch 的實測結果不可信。

### 4. 源碼導覽

**File:** `docs/playtranslate-architecture-notes.md`（CREATE）

以關鍵字搜尋定位，逐項記錄「檔案路徑 + 類別名 + 一句話職責」：

| 區塊 | 搜尋關鍵字 |
|------|-----------|
| 螢幕擷取 | `MediaProjection`, `ImageReader`, `VirtualDisplay` |
| live mode / 變化偵測 | `live`, `stabil`, `hash`, `diff`, `interval` |
| OCR 管線 | `Paddle`, `MangaOcr`, `Meiki`, `MNN` |
| 翻譯引擎介面 | `OpenAI`, `baseUrl`, `endpoint`, `Gemini`, `apiKey` |
| 多螢幕輸出 | `Presentation`, `DisplayManager`, `TYPE_APPLICATION_OVERLAY`, `addView` |
| 設定儲存 | `DataStore`, `SharedPreferences` |
| context 翻譯（v3.0.0） | `context`, `history`, `previous` |

另外畫出 live mode 的處理流程（擷取 → 偵測 → OCR → 翻譯 → 顯示 的類別串接順序），標出 Phase 04 merger 的插入點與 Phase 05 顯示切換的落點。

### 5. （條件：G3=否）自訂 base URL patch

若 OpenAI 相容引擎不能自訂 endpoint：

1. 依 Step 4 找到的引擎設定介面，加一個 base URL 欄位（預設值維持官方 URL；遵循該 codebase 既有的設定 UI 模式）
2. 實測指向 Phase 02 的代理，翻譯成功回繁中
3. Commit（英文訊息）；此 patch 通用性高，考慮直接開 upstream PR

### 6. 建立測試基線

```bash
./gradlew test
```

記錄專案原有測試的通過狀態，作為之後所有 patch 的 regression 基線。若原專案測試本來就有紅的，記下清單，後續只要求「不新增紅測」。

## Phase Verification

- [ ] debug APK 於 Thor 正常運作（G1/G2 重測通過）
- [ ] `playtranslate-architecture-notes.md` 完成：上表七個區塊皆有「檔案 + 類別」落點
- [ ] （若執行 Step 5）PlayTranslate 經自架代理翻譯成功，輸出繁中

## Regression Tests

`./gradlew test`（fork 目錄；含專案原有測試 + 本階段基線紀錄）
