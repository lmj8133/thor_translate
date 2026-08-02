# PlayTranslate 源碼導覽筆記(preliminary)

> Phase 01 先驗調查的副產物,Phase 03 正式導覽時擴充。
> 對象:v3.0.1(single-commit shallow clone @ `~/thor-work/playtranslate`);行號以該 checkout 為準,可能漂移。

## Gradle 模組與建置

- `settings.gradle.kts` 四模組:`:app`(主體)、`:mnn` 與 `:bergamot`(包 vendored 原生碼的 library module)、`:build-tools`。Kotlin DSL;foojay resolver 自動抓 JDK 17;JitPack(KOMORAN 韓文形態素)。
- `app/build.gradle.kts`:applicationId `com.playtranslate`,minSdk 29,target/compileSdk 36,viewBinding(無 Compose),ABI = arm64-v8a + armeabi-v7a(`:mnn` 僅 arm64;32-bit 以 `OnDeviceLlmBackend.supportsRequiredAbi()` 隱藏 MNN 後端)。DeepL key 從 `local.properties` 注入 BuildConfig。Release 用 debug keystore 簽名。
- 特別處:KOMORAN/HanLP 字典從 classpath 剝除,改走可下載 language packs(`app/src/main/assets/langpack_catalog.json`、`scripts/build_latin_dict.py`、`scripts/build_zh_dict.py`)。vendored 原生樹:`mnn/third-party/MNN`(MNN_BUILD_LLM=ON、CPU-only)、`bergamot/third-party`;根目錄 `build.sh`/`BUILD.md`。

## 管線總覽(`app/src/main/java/com/playtranslate/`)

| 段 | 位置 | 重點 |
|----|------|------|
| 擷取 | `capture/` | `CaptureBackend` ← `MediaProjectionCaptureBackend` / `AccessibilityCaptureBackend`,由 `CaptureBackendResolver` 決定;`CaptureService.kt`(~3.5k 行)+ `CaptureSession.kt` 統籌。MediaProjection 固定鏡射 `Display.DEFAULT_DISPLAY`(`capture/MediaProjectionController.kt:264`);accessibility 後端可 `takeScreenshot(displayId)` 指定任意螢幕 |
| OCR | `ocr/` | `OcrPipeline.kt`;引擎註冊於 `ocr/registry/OcrEngineRegistry.kt`。內建:ML Kit、PaddleOCR(`ocr/paddle/`)、manga-ocr(`ocr/mangaocr/`)、Meiki(`ocr/meiki/`);後三者走 MNN/native bridge |
| 翻譯 | `translation/` | `TranslationBackend` + `TranslationBackendRegistry.kt`;線上後端經 `OnlineServiceStore`/`OnlineBackendFactory` 動態增減。後端:MlKit、Bergamot、DeepL、Gemini、**OpenAi**、Lingva、HyMt、裝置端 MNN LLM(Qwen/Gemma) |
| 顯示 | `overlay/OverlayHost.kt`、`ui/` | 雙軌:per-display overlay 視窗(a11y 模式 `TYPE_ACCESSIBILITY_OVERLAY` / MP 模式 `TYPE_APPLICATION_OVERLAY`,經 `createDisplayContext`)與 in-app 結果面板(`MainActivity`/`ui/TranslationResultActivity.kt`,`OverlayUiController.kt:1943-1946` 以 `setLaunchDisplayId` 指定螢幕)。雙螢幕機的「InAppOnly」flavor 於 `CaptureService.kt:1718-1719` 決定,`PanelPresenter.kt:27` `rendersOverlays=false`。無 `android.app.Presentation` |
| 設定 | `Prefs.kt`(~1700 行 SharedPreferences 包裝) | 另有 per-feature store 如 `translation/OnlineServiceStore.kt`;UI 在 `ui/*Activity.kt` |

## G3 相關:OpenAI 相容引擎與繁中輸出

- `translation/OpenAiBackend.kt:38-99`:泛用 chat-completions client(文件自述支援 OpenRouter/DeepSeek/LM Studio 等)。
- `translation/OnlineServiceInstance.kt:76`:`enum OpenAiPreset { OPENAI, DEEPSEEK, MISTRAL, GROQ, OPENROUTER, CUSTOM }`;`:105-107` `baseUrl` 僅 preset==CUSTOM 時採用。可建多實例,清單順序即翻譯瀑布優先序。
- `ui/LlmBackendSettingsActivity.kt:198-215/242-249/337-340`:CUSTOM 時 `etBaseUrl` 可編輯,存檔時驗證;`ui/LlmBackendConfig.kt:81` `allowsBaseUrl = type == ServiceType.OPENAI`。
- `net/CustomEndpointPolicy.kt:21-59`:https 任意主機;**http 僅 loopback/私網/link-local**(自架 LAN server 免 TLS);`net/PtHttp.kt` interceptor 於請求邊界二次強制。
- `translation/OnlineBackendFactory.kt:124-130`:`CUSTOM -> instance.baseUrl.ifBlank { DEFAULT_OPENAI_BASE_URL }`。
- 繁中:`language/ChineseScriptVariant.kt:17-21`(zh-Hans/zh-Hant/zh-Hant-TW/zh-Hant-HK);後端一律輸出簡中(cache 也存簡中,鍵為 "zh"),顯示時 `translation/ChineseScriptConverter.kt` 以 opencc4j s2t/s2tw/s2hk 轉換,含詞彙級台灣用語(软件→軟體、鼠标→滑鼠)。`ui/LanguageSetupActivity.kt:354-395`:目標語言把 "zh" 展開為四變體列。
- 舊制單實例 pref:`Prefs.kt` `KEY_OPENAI_BASE_URL="openai_base_url"`(遷移後以 OnlineServiceStore 多實例為準,實機確認)。

## G4 相關:live mode 與去重(Phase 04 主戰場)

- 主迴圈:`LiveCycleEngine.kt:102-136/187-236` — pace delay → frame-delivery sequence gate → 單次 cycle;reconciler 層不做像素 diff(`ReconcilerLiveMode.kt` 僅 `isAllBlack:610-626`),OCR 後在文字空間比對。舊制 `PinholeOverlayMode.kt` 才有像素 gate(仍活躍,負責 occluded-stream 情境)。
- 比對:`ScanlineReconciler.kt:188-343` — 幾何配對 + bag-of-chars 變化測試;`Region` 帶 `replacesBox`(舊框+舊 sourceText,`:105-120`)。
- 既有三層去重(捲動皆失效):`OverlayToolkit.kt:28-46` bag-of-chars 容差(3 字/30%)KEEP;`TranslationCache.kt:20-43` 整段精確 LRU 500(查詢 `CaptureService.kt:3029-3034`);`TypewriterGate.kt:785-799` 前綴增長(`isEvolvingText` 要求嚴格變長,捲動不符)。
- `LogWriteGate.kt:294-312` 有包含/取代判定,但**只**用於 History/context 紀錄,不在翻譯派發路徑。
- context 功能:`ContextRing.kt`(RING_CAP=8;讀取上限 3 對/600 字/3 分鐘)→ `LlmPromptTemplates.kt:269-270` 注入 `{context}` 區塊;僅 `OpenAiBackend.kt:164/239` 與 `GeminiBackend.kt:129/221`;live 停止與語言對切換時清空(`TranslationLogRecorder.kt:275/491`)。
- **LineOverlapMerger 插入點**:`ScanlineReconciler.reconcile` 的 paired-box RETRANSLATE 分支(已有 `replacesBox` 舊文字可比對),或 `ReconcilerLiveMode.runCycle` 中 `reconcile()`(`:433`)與 `typewriterGate.filterVerdicts()` 之間——TypewriterGate 即現成的「重疊替換」機制先例。
- 注意:若 OCR 把兩行框成**separate groups**,平移行的舊文字會命中 per-group TranslationCache(`OverlayToolkit.kt:803`),重譯範圍縮小——實機觀察分組行為後再定合併策略。

## G2 相關:雙螢幕顯示

- 明確多螢幕:使用者選定 `gameDisplayIds` 集合、DisplayManager listener、per-display region/overlay/loop、`capturableDisplays()` picker。
- z-order:兩種 overlay 型別皆高於他 app 的一般 Activity 視窗;`TYPE_ACCESSIBILITY_OVERLAY` 高於他 app 的 `TYPE_APPLICATION_OVERLAY`;**in-app 面板是普通 Activity,會被他 app(如 Azahar)在同螢幕 resume 的 Activity 蓋住**——Phase 05 切換設計的核心事實。
- `MainActivity` 有 `dumpDisplayState` 可在執行期確認螢幕拓撲。
- 附註:QTI BSP 對不可觸控全螢幕 overlay 有 ~0.79 alpha 上限(源碼註解引述的實測行為,非平台保證)。
