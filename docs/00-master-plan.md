# AYN Thor 3DS 即時翻譯 — 實作總計畫

> **🏁 2026-08-03 專案完成,全數 phase 關閉**。最終架構**優於原計畫且零 fork**:
>
> - **Phase 01** ✅ 路線 A 定案(2026-08-02)
> - **Phase 02** ✅ 完成後大幅演進:代理(Starlette,純 Python 依賴)跑在 **Thor 自己的 Termux** 裡,PT 連 127.0.0.1;翻譯走 **Gemini 免費層雲端鏈**(3 模型備援,$0)→ Mac 的 Sakura 為在家可選兜底;3,982 條 52poke 全量術語表(PT 模型選單即換遊戲選單)、續句拼接、台灣正體出口、開機自啟、重啟自癒、閒置零耗電
> - **Phase 03** 🔒 不需要(無 fork patch 存留)
> - **Phase 04** 🔒 按需封存(跨框斷句已由代理端解決;逐行捲動遊戲出現才復活)
> - **Phase 05** 🔒 關閉(下螢幕面板降為按需;端到端整合以 Thor 單機形態完成;驗收 p50=1.06s/p95=2.22s ✅)
>
> 日常維運見 `server/README-thor.md`;新遊戲術語庫見 `glossaries/README.md`。

> **⚠ 2026-08-02 Phase 01 定案更新**(細節與證據見 [verification-results.md](./verification-results.md)):
>
> - **路線 A(adopt-and-extend PlayTranslate)正式確認**,備援路線 B/C/D 關閉。G1=是、G2=是(過渡採上螢幕 overlay 配置)、G3=是(免 patch)、G4=本作不觸發。
> - **Phase 02 範圍改定**:推論引擎 = **Ollama on MacBook Pro M4 24GB**(使用者已裝);proxy 職責 = **動態專有名詞術語注入**(GalTransl gpt_dict 模式);**OpenCC 轉換免做**(PT 內建 opencc4j s2tw+台灣詞彙,顯示時轉換)。
> - **Phase 04(捲動合併)降為按需啟動**:ORAS 對話為整框替換,不觸發;遇逐行捲動遊戲再做。
> - **Phase 05 主工作確定**:fork patch #1 = **overlay-hosted 下螢幕譯文面板**(實測 `TYPE_PRESENTATION`=31000 < `TYPE_APPLICATION_OVERLAY`=111000,可行性已證);Phase 05 不再依賴 Phase 04。
> - 各 phase 文件開頭均有對應的修正前言;**與本文件下方原文衝突時,以修正前言與 verification-results.md 為準**。

## Scope

在 AYN Thor（雙螢幕 Android 掌機）上玩 3DS 遊戲（Azahar 模擬器）時，自動偵測上螢幕對話變化 → OCR → 帶上下文的 LLM 翻譯 → 顯示於下螢幕，並可切換下螢幕顯示「遊戲畫面」或「翻譯」。

路線：以現成開源 app **PlayTranslate**（GPLv3，為 Thor 第二螢幕而生）為基底，先實測、再補齊缺口——而非從零自建 app 或 fork Azahar（兩者列為備援路線）。

## Prerequisites

- AYN Thor（SKU/RAM 於 Phase 01 記錄；8GB 機型將排除「裝置端 LLM」選項）
- Windows PC + WSL2（本 repo 所在），`adb` 可用（Windows 或 WSL 側皆可）
- （選用）家用 server / PC GPU：跑自架翻譯模型；無 GPU 則走雲端 API
- （選用）Gemini / OpenAI 等 API key
- Android Studio（Phase 03 建置 fork 時需要，建議裝在 Windows 側）

## Assumptions（明示假設，如與事實不符請先修正計畫）

1. 主要遊戲語言為日文、輸出繁體中文（台灣）；英文遊戲為次要情境。
2. 可接受暫時關閉 Play Protect 以安裝 GitHub APK。
3. 本 repo（`coding/thor`）存放文件與 server 端程式；PlayTranslate fork 另行 clone（Phase 03）。

## File Impact

| Action | File | Purpose |
|--------|------|---------|
| CREATE | docs/00-master-plan.md 等 6 份 | 本計畫 |
| CREATE | docs/verification-results.md | Phase 01 驗證結果紀錄表（範本已建） |
| CREATE | pyproject.toml, server/proxy/ | Phase 02：OpenCC 轉換代理（FastAPI） |
| CREATE | tests/test_proxy.py | Phase 02：代理測試 |
| CREATE | tools/sample_dialogue.jsonl, tools/quality_check.py | Phase 02：品質/延遲抽測 |
| CREATE | docs/playtranslate-architecture-notes.md | Phase 03：fork 源碼導覽筆記 |
| CREATE | （fork 內）LineOverlapMerger + 測試 | Phase 04：捲動續行合併 |
| CREATE | docs/usage.md | Phase 05：日常使用手冊 |

## Phase Order

1. [01-device-verification.md](./01-device-verification.md) — 裝置實測與路線判定（回答 Gate G1–G4）
2. [02-translation-backend.md](./02-translation-backend.md) — 自架翻譯後端（**可與 01 平行**）
3. [03-playtranslate-build.md](./03-playtranslate-build.md) — fork 建置與源碼導覽（依 01 結果）
4. [04-scroll-merge-patch.md](./04-scroll-merge-patch.md) — 捲動續行合併 patch（依 03）
5. [05-display-toggle-integration.md](./05-display-toggle-integration.md) — 下螢幕切換與端到端整合（依 01、04）

## Decision Gates（Phase 01 產出）

| Gate | 問題 | 若「否」的走向 |
|------|------|----------------|
| G1 | Azahar 運行時 PlayTranslate 能擷取乾淨畫面（無黑屏/DRM 阻擋）？ | 整個截圖式方案失效 → 路線 C（fork Azahar，另立計畫） |
| G2 | live mode 翻譯能顯示在**下螢幕**？ | Phase 05 改走方案 B（Screen Launch 釘 app 於下螢幕），或評估路線 B |
| G3 | OpenAI 相容引擎可自訂 base URL？ | Phase 03 加一個小 patch（並考慮 upstream PR） |
| G4 | 存在捲動重複翻譯問題？（預期：是） | 若「否」則 Phase 04 整段略過 |

## 備援路線（僅在 Gate 觸發時展開，勿提前投入）

- **路線 B**：改 fork [overlay-translator](https://github.com/ciddwd/overlay-translator)（Apache-2.0；已內建 manga-ocr ONNX + llama.cpp 裝置端推論，缺第二螢幕輸出與捲動合併）
- **路線 C**：fork Azahar（擷取 hook = `RendererBase::RequestScreenshot` + 小型 JNI 匯出；代價：36 submodules、CI 建置約 51 分鐘、`src/android` 年 164 commits 的 rebase 負擔、無 upstream 路徑）
- **路線 D**（精準特化，可與主路線並存）：Azahar 內建 RPC server（config ini `enable_rpc_server`，UDP 45987 `ReadMemory`）做每遊戲記憶體文字鉤取，零 OCR 誤差，但每款遊戲需逆向文字位址
- **簡易替代**：[ThorTranslate](https://github.com/magiobus/thortranslate)（MIT，極簡）——只需「堪用」而不想維護 fork 時

## Technical Decisions

| Decision | Chosen Approach | Rationale | Alternatives Considered |
|----------|----------------|-----------|------------------------|
| 基底 app | PlayTranslate（GPLv3） | 已實作幾乎全部需求管線（live mode、多螢幕、manga-ocr、context 翻譯、繁中），活躍維護 | 從零自建；fork Azahar；overlay-translator |
| OCR | PlayTranslate 內建（manga-ocr 優先，PaddleOCR 備援） | manga-ocr 為日文遊戲/漫畫字型的社群標準；Tesseract 在此類文字準確率 <30% | ML Kit（英文佳、日文遊戲字型未知） |
| 翻譯後端 | 自架 Sakura-GalTransl-7B-v3.7 + OpenCC `s2twp` 代理 | 遊戲對話特化微調品質；Sakura 系僅輸出簡中，需出口轉換 | 雲端 Gemini Flash-Lite（≈US$2/30h 遊戲，備援）；裝置端 4B 模型（僅 12/16GB 機型可考慮） |
| 捲動續行 | 自寫行重疊合併（LineOverlapMerger，Phase 04） | 市面上無任何工具解決此問題 | 只靠 LLM 上下文（仍會重複翻譯、浪費延遲） |
| 下螢幕切換 | 依 G2：浮窗顯隱（方案 A）或 AYN Screen Launch + Azahar 免重啟版面切換（方案 B） | 避開未文件化的跨 app 視窗 z-order 賭注 | Azahar fork 內建切換（路線 C） |

## Key Constraints

1. **PlayTranslate 為 GPLv3**：修改版若對外散布必須開源；純個人使用無義務。可貢獻的 patch 優先嘗試 upstream PR。
2. **SakuraLLM/GalTransl 為 CC BY-NC-SA**（禁商用）；**megingiard 授權禁止改作散布**——僅可參考概念，不可抄程式碼。
3. Android/gradle 專案**勿放在 `/mnt/c` 下建置**（跨檔案系統 I/O 極慢）；clone 至 WSL 家目錄或 Windows 側。
4. 文件用繁體中文；程式碼、識別字、註解、commit message 一律英文。
5. API key 與自架 server 位址不得寫入版控（用環境變數 / local settings）。

## Full Regression Command

- 本 repo：`uv run pytest -q`
- PlayTranslate fork：`./gradlew test`（於 fork 目錄）

## How to Implement

1. 先讀本 master plan 掌握範圍、Gate 與限制
2. 依序實作各 phase——一次只讀一份 phase 文件（02 可與 01 平行）
3. 每個 phase 開始前跑 Pre-flight（第一階段除外）
4. 每個 phase 結束後跑 Phase Verification 與 Regression Tests
5. 全部完成後跑上方 Full Regression Command
