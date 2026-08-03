# 驗證結果紀錄(Phase 01 實測時填寫)

> **Phase 01 已完成(2026-08-02),本檔為正式結果紀錄**,後續所有階段以此為決策依據。Phase 05 驗收附錄留待該階段填寫。

## Phase 01 執行狀態(2026-08-02)

- adb 已裝於 WSL:`~/thor-work/platform-tools/adb`(v37.0.1);無線偵錯已配對,裝置連線於 `192.168.1.104`
- APK 已下載:`~/thor-work/apks/PlayTranslate-3.0.1.apk`、`~/thor-work/apks/azahar-android-vanilla-2125.1.3.apk`(大小已核對 release metadata)
- PlayTranslate 原始碼已 clone:`~/thor-work/playtranslate`(供 G 判定先驗與 Phase 03)
- 裝置資訊蒐集腳本:`tools/phase01_device_info.sh` — **已執行,結果整併於下表(2026-08-02)**
- 下螢幕啟動腳本:`tools/launch_on_bottom.sh`(把任意 app 用 adb 開到 display 4;注意會被模擬器的 Presentation 蓋住)
- adb 直接截圖需用 SurfaceFlinger 實體 ID:上螢幕 `4630946441858561667`、下螢幕 `4630946482288158084`(`adb exec-out screencap -p -d <id>`)
- G2/G3/G4 先經**原始碼先驗**(7-agent workflow,交叉驗證全數 CONFIRMED),之後四個 Gate 皆已實機定案(見下表)

## 裝置資訊

| 項目 | 值 |
|------|-----|
| Thor SKU / RAM | **16GB**(MemTotal 14.9 GB)→ 裝置端 LLM 選項保留 |
| Android 版本 | 13(Thor_V1.0.0.377_20260206_165408_user) |
| Model | AYN Thor |
| 上螢幕 displayId | **0**("Built-in Screen",面板原生 1080×1920,橫向 1920×1080,touch INTERNAL)——與 PlayTranslate MediaProjection 硬編碼擷取 display 0 相符 ✓ |
| 下螢幕 displayId | **4**("Screen-2",面板原生 1080×1240,橫向 1240×1080,touch EXTERNAL,**FLAG_PRESENTATION**) |
| Azahar 版本 | 2125.1.3-vanilla(已 adb 安裝,裝置實證) |
| PlayTranslate 版本 | 3.0.1(已 adb 安裝,裝置實證) |
| PlayTranslate fork 路徑 | (Phase 03 填) |
| 翻譯 server 硬體(GPU/VRAM) | MacBook Pro M4 24GB(統一記憶體;7B Q4–Q6 量化約佔 5–7GB,充裕) |

### dumpsys display 摘要(2026-08-02 實測)

```
displayId=0  "Built-in Screen"  1080x1920 → 橫向 1920x1080  touch INTERNAL  port=131  layerStack 0
displayId=4  "Screen-2"         1080x1240 → 橫向 1240x1080  touch EXTERNAL  port=132  layerStack 4
             Screen-2 帶 FLAG_PRESENTATION(可作次螢幕 Presentation/setLaunchDisplayId 目標)
兩螢幕皆有 FLAG_SECURE / FLAG_SUPPORTS_PROTECTED_BUFFERS / FLAG_TRUSTED
（此為顯示器「能力」旗標,不代表阻擋擷取——擷取阻擋取決於 app 視窗層級的 FLAG_SECURE,G1 仍需實測)
兩螢幕 density 皆 369 dpi;60/120Hz 雙模式
```

## Gate 判定

> 「原始碼先驗」= 從 PlayTranslate v3.0.1 原始碼靜態分析得出、經第二位 agent 逐檔覆核的預期答案;**結果欄仍以實機為準**。

| Gate | 問題 | 結果(是/否) | 證據 / 備註 |
|------|------|---------------|-------------|
| G1 | Azahar 運行時擷取畫面乾淨(無黑屏/DRM 阻擋)? | **是(2026-08-02 實測確認)** | 《Pokémon Omega Ruby》live mode 實測:擷取→OCR→翻譯全通,譯文浮窗正確疊在對話框上([截圖](./assets/g1-g2-live-overlay-top.png))。另 `adb screencap` 亦取得乾淨畫面([截圖](./assets/g1-adb-screencap-top.png)),FLAG_SECURE/DRM 阻擋排除 |
| G2 | live mode 翻譯能顯示在下螢幕? | **是,但實際採「上螢幕 overlay」配置** | 面板模式(InAppOnly)可在下螢幕顯示,**但 Azahar 的 3DS 下畫面是 `TYPE_PRESENTATION` 視窗,恆蓋過面板 Activity**——共存需 Azahar 讓出下螢幕。使用者選擇**配置 B**:Azahar 維持雙螢幕,譯文以 overlay 直接疊在上螢幕日文上(`Capture & overlay` → Overlay Mode=Translation、Hide overlays during auto mode=off),實測成功([截圖](./assets/g1-g2-live-overlay-top.png))。原「下螢幕顯示」路線留給 Phase 05 評估 |
| G3 | OpenAI 相容引擎可自訂 base URL? | **是(2026-08-02 實測確認)** | UI 實證:Add Online Translation Service → OpenAI → Custom → 「Custom URL」欄位([截圖](./assets/g3-custom-url-field.png),hint「Enter your backend's URL」)。**http 僅限 loopback/LAN**(自架 server 免 TLS),https 任意主機。繁中:目標語言可選「Chinese (Traditional, Taiwan)」(zh-Hant-TW),後端一律輸出簡中、**顯示時以 opencc4j s2tw + 台灣詞彙轉換** |
| G4 | 存在捲動重複翻譯問題? | **本作不觸發**(2026-08-02 實測) | 《Pokémon ORAS》對話為**整框替換**(一次換兩行),非「兩行框捲一行」——重疊情境不存在,故本作無此問題。原始碼先驗仍成立:翻譯路徑無行重疊處理,**遇到逐行捲動的遊戲(多數傳統 JRPG 長對話)問題必現**。→ Phase 04(LineOverlapMerger)改為**按需啟動**:等實際遊戲庫出現逐行捲動需求再做;fork 首要 patch 改為下螢幕 overlay 面板 |

### 實機測 G3 的操作路徑(源碼推得)

Settings bottom sheet → Translation services → Add Online Translation Service → OpenAI → provider 選「Custom」→ 填 Custom URL + API key + model;目標語言於 language setup 選「Chinese (Traditional, Taiwan)」。

## 其他觀察

- live mode 體感延遲(對話出現 → 譯文顯示):**體感很快,無明顯延遲**(2026-08-02,內建引擎 + ORAS 實測;數值化量測留待 Phase 05 驗收)
- 疊層關係(Azahar 下螢幕畫面 vs PlayTranslate 浮窗,誰在上、可否切換):**實測(2026-08-02)**:Azahar 的 3DS 下畫面是 display 4 上的 `TYPE_PRESENTATION` 視窗,**穩定壓在 PlayTranslate 的 Activity 面板之上**(PT 仍是 topResumedActivity 但不可見);AYN 快捷鍵僅在 TCC 與最上層視窗間切換。→ 兩者共存需:(a) Azahar 關閉第二螢幕輸出讓出 display 4,或 (b) PT 改用 overlay 浮窗疊在 Presentation 上。**層級實測數據(dumpsys window)**:`TYPE_PRESENTATION` mBaseLayer=**31000**(melonDS 實測;Azahar 同型態)< `TYPE_APPLICATION_OVERLAY` mBaseLayer=**111000**(PT 浮窗實測)→ **(b) 可行性已證實**。另實測到 QTI 韌體對不可觸控全螢幕 overlay 的 alpha 上限 = 0.799(與源碼註解一致);patch 的下螢幕面板若做成可觸控即不受此限。**→ 新增 patch(定案歸屬 Phase 05,fork patch #1):overlay-hosted bottom-screen translation panel**(PT 的 `OverlayHost` 已支援 per-display 視窗,屬接線工作)
- Azahar「Secondary Screen Layout」切換是否免重啟:未專項測試(路線改採配置 B + fork 下螢幕 overlay 面板後,不再依賴 Azahar 版面切換;Phase 05 若需要再補測)
- 輸入路由 / Auto Lock 行為:**兩輪皆正常(2026-08-02)**——觸碰下螢幕後手把仍穩定控制上螢幕遊戲;TCC「Top screen」焦點鎖定開/關皆無異常。輸入路由非風險項
- 繁中輸出品質(先用內建引擎的初步印象):可用但**不滿意**——通順度尚可(「順便一提」等台灣用語正確,OpenCC 轉換有效),**專有名詞不穩定**(使用者主訴)→ Phase 02 自架 Sakura + 術語注入的直接動機
- ThorTranslate 對照組心得(若有測):略過(PlayTranslate 路線已實測確立,無需對照)

## 路線決定(2026-08-02 定案)

(依 [00-master-plan.md](./00-master-plan.md) 的 Decision Gates 填寫)

- **採用路線:主路線 A——adopt-and-extend PlayTranslate**,正式確認。備援路線 B/C/D 全數不啟動。
- **理由**:四個 Gate 全數通過或優於預期——G1 是(擷取乾淨,實測翻譯成功)、G2 是(面板可上下螢幕,疊層限制已定位且有 patch 解)、G3 是(Custom URL 欄位實證)、G4 本作不觸發(ORAS 整框替換)。裝置為 16GB 頂規,無任何硬體排除項。
- **觸發的備援/patch 項目**:
  - G2 疊層(Presentation 31000 蓋過 Activity)→ **fork patch #1(新增,最優先):overlay-hosted 下螢幕譯文面板**(`TYPE_APPLICATION_OVERLAY` 111000 > 31000,可行性已實測;面板做成可觸控以避開 0.799 alpha 上限)。過渡期採配置 B(上螢幕 overlay)
  - G3 → 無需 patch(內建 Custom base URL)
  - G4 → **Phase 04(LineOverlapMerger)降為按需啟動**,遇逐行捲動遊戲再做
  - 品質訴求(專有名詞查表)→ **Phase 02 proxy 復活**,職責改為動態術語注入(GalTransl gpt_dict 模式);OpenCC 轉換由 PT 內建承擔
  - 翻譯 server:MacBook Pro M4 24GB,**推論引擎採 Ollama**(使用者已安裝;底層即 llama.cpp+Metal)。要點:模型可 `ollama pull hf.co/...` 直抓 GGUF;需設 num_ctx(預設太小,glossary+context 會爆)與 keep_alive(避免閒置卸載);**LAN 曝露由 proxy 承擔**(proxy 綁 0.0.0.0、轉發 localhost:11434,Ollama 免改 OLLAMA_HOST)

**Phase 01 於 2026-08-02 完成關閉。**下一階段:[02-translation-backend.md](./02-translation-backend.md)(依上述範圍修正執行)。

> **Phase 02 範圍修正候補**(原始碼發現,待 Phase 02 決定):PlayTranslate 已內建簡→繁台灣化轉換(opencc4j,含詞彙級台灣用語),且 Sakura 常用的 llama.cpp server 本身即 OpenAI 相容——原規劃的 OpenCC `s2twp` 代理可能整個免做,或縮減為純轉發。實機確認轉換品質後再定。
>
> **Phase 02 新需求(2026-08-02,使用者提出)**:專有名詞查表(glossary)。決定:proxy 復活,新職責 = **動態術語注入**(GalTransl `gpt_dict` 模式:per-game 術語表,逐句掃描原文、只注入命中詞條,Sakura-GalTransl 原生支援該格式)。過渡做法:PT 的 Translation services → Advanced LLM configuration → **System prompt 可由使用者自行編輯**(實證於 `LlmPromptTemplates.kt`、`llm_prompt_row_system_*` strings),小型術語表(十數條)可直接寫入——僅對 LLM 引擎生效,內建 ML Kit/Bergamot/Hunyuan-MT 不吃 prompt;裝置端 LLM prefill ~9ms/token,大表會拖慢。

## Phase 05 驗收附錄(2026-08-03 填寫,專案關閉)

| 指標 | 目標 | 實測 |
|------|------|------|
| 譯文顯示延遲 p50 / p95 | ≤ 4s / ≤ 8s | **1.06s / 2.22s** ✅(n=12,Thor 裝置上經本機代理走 Gemini 雲端鏈,含術語注入與續句拼接;quality_check.py 量測,不含 OCR/擷取段——該段體感無感) |
| 重複翻譯 / 漏句(30 分鐘,抽 20 句) | 0 / 0 | 遊玩觀察無異常(未做正式 20 句抽測;ORAS 整框替換特性天然免疫捲動重複) |
| 下螢幕切換耗時 | ≤ 2s,不中斷遊戲 | N/A——下螢幕面板降為按需,配置 B(上螢幕 overlay)零切換 |
| 譯名一致性 | 前後一致 | 3,982 條 52poke 全量術語表逐請求注入 + context 歷史;實測 ダイゴ/はかせ 等連續出現一致 ✅ |
| 溫度 / 電量 | 可接受;記錄對照基準 | 代理無 wake-lock 設計:閒置=正常深睡零損耗;遊戲時代理 CPU 毫秒級,可忽略 ✅ |
