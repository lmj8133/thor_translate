# Phase 05: 下螢幕切換與端到端整合

> **🔒 2026-08-03 關閉**。兩個組成部分的最終處置:
>
> 1. **fork patch #1(下螢幕譯文面板)→ 降為按需**:配置 B(上螢幕 overlay)已達成「零切換」的原始核心需求;且 ORAS 下螢幕為 PokéNav 觸控介面,可觸控面板會攔截操作反成障礙;fork 維運成本(Phase 03 工具鏈+隨上游 rebase)不划算。可行性證據(z-order 實測 31000<111000、OverlayHost 接線點)完整在案,遇到「上螢幕 overlay 礙眼」或「下螢幕閒置」的遊戲再復活。
> 2. **端到端整合 → 已以優於原計畫的形態完成**:Thor 單機模式(代理跑 Termux、PT 連 127.0.0.1、Gemini 雲端鏈),含開機自啟、重啟自癒、per-game 術語表選單,均實機驗證。
>
> **驗收數據(2026-08-03,裝置上實測)**:翻譯延遲 p50=**1.06s**、p95=**2.22s**(n=12,含術語注入與續句拼接,目標 ≤4s/≤8s ✅);譯名一致性由 3,982 條術語表+context 保障;閒置零耗電(無 wake-lock 設計)。詳見 verification-results.md 附錄。

> **⚠ 2026-08-02 修正前言(依 Phase 01 結果,優先於下方原文)**:
>
> 1. **依賴改為 Phase 03**(Phase 04 已降為按需,不再是前置)。
> 2. **主工作定案 = fork patch #1:overlay-hosted 下螢幕譯文面板**。實測數據:模擬器第二螢幕(Azahar/melonDS 皆為 `TYPE_PRESENTATION`)mBaseLayer=**31000** < `TYPE_APPLICATION_OVERLAY`=**111000** → 浮窗必疊在 3DS 下畫面之上,Step 1 的「方案 A」可行性已證,免再評估方案 B。實作要點:PT 的 `OverlayHost` 已支援 per-display 視窗(`createDisplayContext`),把譯文面板改掛 overlay 視窗於 display 4;**面板做成可觸控**以避開 QTI 韌體對不可觸控全螢幕 overlay 的 0.799 alpha 上限(實測)。
> 3. **使用者明確需求:不要模式切換**——遊戲維持完整雙螢幕,譯文常駐疊在下畫面上(可加顯示/隱藏鈕,但非必要)。過渡期使用者現行採「配置 B」= 譯文 overlay 疊在上螢幕對話框上(Capture & overlay → Hide overlays during auto mode=off)。
> 4. **Step 2 輸入焦點已提前驗證無虞**:觸碰下螢幕後手把仍穩定控制遊戲,TCC「Top screen」焦點鎖定開關皆正常(2026-08-02 實測)。
> 5. Thor 實用資訊:上螢幕=display 0、下螢幕=display 4;TCC=下螢幕下方 AYN 實體鍵呼出;`tools/launch_on_bottom.sh` 可把任意 app 開到下螢幕(adb)。
> Produces: 下螢幕「遊戲 / 翻譯」切換機制、`docs/usage.md`（日常使用手冊）、端到端驗收紀錄

## Pre-flight

```bash
cd <fork>; ./gradlew test          # fork 測試全綠
uv run pytest -q                   # 本 repo 測試全綠（於 coding/thor）
```

## Steps

### 1. 選定切換方案（依 verification-results.md 的 G2 與疊層觀察）

**方案 A**（G2=是，且 PlayTranslate 浮窗能穩定疊在 Azahar 下螢幕畫面之上）：

- 在 PlayTranslate 加「顯示/隱藏翻譯面板」切換：下螢幕角落浮動小按鈕；隱藏時即露出 Azahar 的 3DS 下畫面
- 依 architecture-notes 的多螢幕輸出落點實作，遵循既有 Presentation/浮窗管理模式

**方案 B**（G2=否，或疊層不穩定）：

- Azahar「Secondary Screen Layout」平時設 Bottom Screen（遊戲下畫面）；要看翻譯時切為僅上畫面，並用 AYN 的「Screen Launch → Bottom」把 PlayTranslate 釘在下螢幕
- 記錄實際切換步數；若超過 3 步，評估用 AYN Task 分頁或系統捷徑縮短流程

### 2. 輸入焦點防護

Thor 的手把輸入會跟著「最後被觸碰的螢幕」跑：

- 啟用 AYN 的 **Auto Lock**，驗證觸碰下螢幕切換後手把仍控制遊戲
- 方案 A 的浮動按鈕加 `FLAG_NOT_FOCUSABLE`（不取焦點），並實測是否還會搶手把
- 若仍會搶焦點：切換改綁「長按浮動按鈕」等低誤觸操作，並在 usage.md 註明

### 3. 每次遊玩的啟動流程固化

- MediaProjection 授權每個 session 需重新同意：量測「開機 → 開玩 + 翻譯就緒」的實際步數與秒數
- 查 architecture-notes：PlayTranslate 是否已有快速重啟擷取的機制（例如常駐前景服務）；有就用、沒有先接受手動流程（優化列入 backlog，不擋驗收）

### 4. 端到端驗收（30 分鐘實玩）

結果記入 `verification-results.md` 的「Phase 05 驗收附錄」：

- [ ] 對話出現 → 譯文顯示：p50 ≤ 4s、p95 ≤ 8s（抽 20 句手動計時）
- [ ] 30 分鐘內無重複翻譯、無漏句（對照 Phase 04 遊戲段落抽查 20 句）
- [ ] 下螢幕「遊戲 ↔ 翻譯」切換 ≤ 2s，且不中斷遊戲、不搶手把
- [ ] 譯名/人稱前後一致（context 機制生效）
- [ ] 機身溫度與風扇體感可接受；記錄電量消耗並對照純遊戲基準

### 5. 使用手冊

**File:** `docs/usage.md`（CREATE）

- 一次性設定：server 啟動、PlayTranslate 引擎/endpoint、Auto Lock、Screen Launch
- 每次遊玩 SOP：一行版 + 完整版（含 MediaProjection 授權步驟）
- 疑難排解：黑屏（G1 失效時的檢查順序）、引擎逾時（切雲端引擎降級）、OCR 亂碼（切換 OCR 引擎、調整框選區域）

## Phase Verification

- [ ] Step 4 驗收指標全過，結果已記入 verification-results.md
- [ ] `usage.md` 完成，且照著文件能從零走通一次

## Regression Tests

- `./gradlew test`（fork）與 `uv run pytest -q`（本 repo）仍全綠
- 端到端以 Step 4 檢核表為準（手動驗收，無自動化）
