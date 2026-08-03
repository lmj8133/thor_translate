# Phase 04: 捲動續行合併（LineOverlapMerger）

> **🔒 2026-08-03 關閉(維持按需,觸發條件未出現)**。另註:原痛點的「跨框句子斷裂」面向已由**代理端續句拼接**(server/proxy/sakura.py `join_continuation`,2026-08-03 實戰驗證)解決;殘餘的「捲動重複翻譯去重」面向仍屬 PT 端,**只在遇到逐行捲動的遊戲時**才需要復活本階段(屆時 Phase 03 一併復活)。
>
> **⚠ 2026-08-02 狀態:降為按需啟動(暫緩)**。Phase 01 實測:ORAS 對話為整框替換(一次換兩行),無「捲一行」重疊情境,本作不觸發此問題。原始碼先驗仍成立(翻譯路徑無行重疊處理),**遇到逐行捲動的遊戲時再啟動本階段**。Phase 05 已改為不依賴本階段。精確插入點已記於 `playtranslate-architecture-notes.md`(`ScanlineReconciler.reconcile` RETRANSLATE 分支,`Region.replacesBox` 帶舊文字;`TypewriterGate` 為現成先例)。
> Produces: fork 內 `LineOverlapMerger` + 單元測試 + live-mode 整合

解決本專案的核心痛點：「兩行對話框每次捲一行 → 重複翻譯、文句不通順」。做法：新舊 OCR 結果做**行重疊比對**，只翻譯新增的行，舊行併入滾動歷史供 LLM 上下文。此邏輯市面上無現成解，是本專案最主要的自寫程式碼。

## Pre-flight

```bash
cd <fork>; ./gradlew test    # 必須維持 Phase 03 基線
```

## Steps

### 1. 純邏輯類別

**File:** fork 內新檔（套件位置依 codebase 慣例，置於 OCR 後處理相近的 package，如 `.../ocr/LineOverlapMerger.kt`）（CREATE）

```kotlin
enum class MergeKind { NEW_DIALOGUE, CONTINUATION, UNCHANGED }

data class MergeResult(
    val kind: MergeKind,
    val deltaLines: List<String>,   // lines that still need translation
    val mergedText: String,         // full merged dialogue for LLM context
)

class LineOverlapMerger(private val similarityThreshold: Double = 0.85) {
    fun merge(previousLines: List<String>, currentLines: List<String>): MergeResult
}
```

實作要點（English comments）：

- **normalize**：trim、全形/半形空白摺疊、去除行尾游標符號（`▼▽►▶➤` 等常見 continue 提示）
- **重疊偵測**：找最大 `k`，使 previous 的最後 `k` 行 ≈ current 的最前 `k` 行；「≈」= 逐行 normalized Levenshtein 相似度 ≥ threshold（容忍 OCR 噪音）
- 判定：`k == currentLines.size` → `UNCHANGED`；`k > 0` → `CONTINUATION`（`deltaLines = current.drop(k)`）；`k == 0` → `NEW_DIALOGUE`
- 純函式、無內部狀態；前次行與歷史由呼叫端保存

### 2. 單元測試

**File:** fork 內對應測試路徑 `.../LineOverlapMergerTest.kt`（CREATE）

| 案例 | 輸入 | 預期 |
|------|------|------|
| 核心捲動情境 | prev=[A,B], curr=[B,C] | CONTINUATION, delta=[C] |
| 畫面未變 | prev=[A,B], curr=[A,B] | UNCHANGED |
| 全新對話 | prev=[A,B], curr=[C,D] | NEW_DIALOGUE |
| OCR 噪音容忍 | 「勇者は剣を取った▼」vs「勇者は剣を取った」 | 視為同一行 |
| current 為空 | prev=[A,B], curr=[] | UNCHANGED（不觸發翻譯） |
| 單行框連續推進 | prev=[A], curr=[B] | NEW_DIALOGUE（無重疊即新句） |

### 3. 整合進 live-mode 管線

依 architecture-notes 的插入點，在「OCR 結果 → 翻譯請求」之間接上 merger（維持既有程式風格）：

- `CONTINUATION`：只送 `deltaLines` 翻譯；prompt 的 context 帶 `mergedText` 與前次譯文
- `UNCHANGED`：直接跳過，不發翻譯請求（省延遲與 token）
- `NEW_DIALOGUE`：照原流程；同時將前次 `mergedText` 推入滾動歷史（上限 5 則，接上 v3.0.0 既有 context 機制，勿重複造輪子）
- 加一個設定開關（預設開；沿用其設定 UI 模式），出問題可一鍵退回原始行為

### 4. 裝置實測

用 Phase 01 Step 7 的同一款遊戲重測：

- [ ] 捲動一行時只翻譯新行，下螢幕不出現重複句
- [ ] 譯文前後連貫（LLM 有收到合併上下文）
- [ ] 快速連點跳過對話時不崩潰、不漏句（壓力測試）

## Phase Verification

- [ ] `./gradlew test` 全綠（含新測試，不低於 Phase 03 基線）
- [ ] 裝置實測三項全過

## Regression Tests

`LineOverlapMergerTest` 併入 `./gradlew test`，永久保留。
