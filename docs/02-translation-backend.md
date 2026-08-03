# Phase 02: 自架翻譯後端（GalTransl + OpenCC 代理）

> **✅ 2026-08-02 實作完成紀錄(優先於下方一切原文與修正前言)**
>
> 程式側已完成並通過審查(多 agent 對抗式覆核 + 25 項測試全綠)。交付物:`server/proxy/`(main.py 路由/轉發、sakura.py prompt 構建與 PT 請求解析、glossary.py 術語表)、`tests/test_proxy.py` + `tests/test_glossary.py`、`tools/sample_dialogue.jsonl` + `tools/quality_check.py`、`glossaries/pokemon-oras.txt`(52poke 逐條驗證)、**`server/README.md`(Mac 部署指南,使用者照抄執行)**。
>
> 與修正前言的差異(以本紀錄為準):
>
> 1. **pull 標籤陷阱**:`:Q6_K` 與不帶標籤實際解析到 **GalTransl-7B-v1(舊模型)**(該 repo 有五個 Q6_K 檔)。正確指令:`ollama pull hf.co/SakuraLLM/Sakura-GalTransl-7B-v3.7:Sakura-Galtransl-7B-v3.7.gguf`(此檔即 Q6_K,6.25GB;經 HF manifest API 實證)。
> 2. **Ollama `/v1` 端點不接受 `options`/`keep_alive`**:num_ctx 以 Modelfile 烘入衍生模型 `sakura-galtransl-v3.7`(num_ctx 8192);keep_alive 由**代理啟動時打原生 `/api/generate` 釘住(keep_alive=-1,兼預熱)**。
> 3. **代理設計 = 整段改寫**(非僅注入):抽出 PT 請求的原文與 context → 重建為 Sakura-GalTransl v3.7 官方 prompt(系統/使用者模板逐字採 GalTransl `Prompts.py`;取樣 temperature 0.3 / top_p 0.8 / frequency_penalty 0.1;PT context 譯文側轉為「历史翻译」)。PT 的 system prompt 被忽略,PT 端維持預設模板即可。
> 4. **batch 路徑**:PT 多區域請求(帶 response_format)→ 依 GalTransl 慣例逐行合批;模型回行數不符時回 **400,PT 會自動退回逐句重送**(實證於 PT 源碼 TranslationBackendRegistry)。
> 5. 預設 port **8000**;`GET /v1/models` 要求非空 Bearer(讓 PT 的 key 驗證顯示正常)。
> 6. `tools/quality_check.py` 模擬 **PT 預設請求格式**(非原文寫的 GalTransl 格式)——因 Sakura prompt 已由代理構建,端到端測的就是真實路徑。
> 7. 術語表修正:ミツル 官方譯名 = **滿充**(「小勝」是劍盾主角マサル,完全不同角色);主角 ハルカ=小遙、ユウキ=小悠。
> 8. Phase Verification 第一項應回**簡體中文**(含術語表繁體詞條),繁化由 PT 顯示端負責;`uv run pytest -q` 已全綠(25);**LAN 端到端延遲留待 Mac 實機部署時量測**(照 server/README.md §6)。
>
> **2026-08-02 部署完成:使用者已完成 server 部署與 Thor 端 PT 設定(OpenAI → Custom → 代理 URL),實機端到端確認可用 → Phase 02 正式關閉。**量化延遲(p50/p95)可隨時以 `uv run python tools/quality_check.py --endpoint http://<server-ip>:8000/v1` 補測,亦可留待 Phase 05 驗收一併量。
>
> **2026-08-03 品質升級 #3:雲端優先備援鏈(cloud mode)**。動機:三個實錘誤譯(はかせ→大師、したがめん→結算鍵、ひつよう→不需要極性翻反)證明全假名兒童向文本超出 VN 特化 Sakura-7B 能力,前處理 A/B(去全形空格)僅換錯型無法救。方案:代理新增 Gemini OpenAI 相容端點的 cloud 模式,每句依序試 gemini-3.5-flash-lite(免費 500/天)→ gemini-3.1-flash-lite(500/天)→ gemma-4-26b-a4b-it(14,400/天;id 於 2026-08-03 實測修正,原猜測 gemma-4-26b-it 為 404)→ 本地 Sakura;任一層失敗(429/斷網/行數不符)自動下滑,翻譯不中斷。免費層 key 無付款方式、額度盡即 429 硬停,實質 $0(實測使用者帳號免費額度:flash-lite 各 500 RPD/15 RPM,Gemma 14.4K RPD/30 RPM)。雲端直接輸出台灣正體(免 OpenCC);術語表/續句拼接/context 全保留(雲端 prompt 為一般指令格式,見 server/proxy/cloud.py);本地兜底路徑行為不變。啟動時對各雲端模型煙霧測試並記 log。GEMINI_API_KEY 未設 = 純本地(原行為)。測試 35 綠。
>
> **2026-08-03 修正 #4:PT 端二次轉換與術語保護**。實機發現「畫面→畫麵」:代理輸出已是台灣正體,PT 目標語言若設 Traditional (TW) 會再跑一次 opencc4j——其詞庫以簡體為鍵,繁體輸入退化成逐字轉而過度轉換(先前「PT 再轉是恆等」的判斷**錯誤**,以此為準)。**修正:PT 目標語言改選 Chinese (Simplified)(= PT 不轉換,劇本由代理全權負責)**;代價僅「內建引擎備援句顯示簡體」(代理全掛才發生)。另修本地路徑術語被自家 s2twp 詞彙映射改寫的問題(模型回簡體 项目 → s2twp 誤映射成 專案):輸出後對命中詞條做保護還原(_protect_glossary_terms)。測試 36 綠。
>
> **2026-08-03 品質修正 #2:跨框長句拼接(continuation join)**。使用者提出「一句話講不完」情境:日文動詞在句尾,長句拆框後前半框單獨翻譯必然彆扭。對策(純代理端,單句路徑):前一框原文非句末標點收尾 → 判定未完句 → 原文接進當前輸入一起翻、其譯文移出历史翻译;可連鎖多框(受 PT context 3 對上限約束)。誤判代價=前框語意在當前框重複顯示(不會翻錯);`CONTINUATION_JOIN=0` 可關(句末不加標點的遊戲用)。同日術語修正:はかせ→大師 誤譯(全假名歧義實例)以 ポケモンはかせ/はかせ/チャンピオン 三詞條解決,實機重現+驗證。測試 31 綠。Phase 04 的「捲動重疊去重」仍屬 PT 端、維持按需。
>
> **2026-08-02 品質修正(部署後)**:實機出現簡體漏轉(「哪里」)。根因:PT 的簡繁轉換取決於 `targetChineseVariant` 偏好,且該偏好 fail-safe 到 Simplified(變體選錯/重置即整段不轉)。修正:**OpenCC 回歸代理**——出口 `s2twp`(簡→台灣正體+台灣用語)、context 入口 `tw2sp`(PT 回傳的繁體前文正規化回簡體再進模型);自此不依賴 PT 端設定(PT 對已繁化文本再轉為恆等)。測試 27 綠。「生硬不自然」問題待此修正上線後重新評估,再決定雲端模型 vs Sakura-14B(候補方向,連同「假名為主文本對 Sakura 屬域外資料」的分析,見對話紀錄)。

> **⚠ 2026-08-02 修正前言(依 Phase 01 結果,優先於下方原文)**:
>
> 1. **推論引擎改用 Ollama**(MacBook Pro M4 24GB,使用者已安裝並指定)。取代 Step 1 的 llama.cpp:模型倉庫確認為 **`huggingface.co/SakuraLLM/Sakura-GalTransl-7B-v3.7`**(GGUF:Q6_K 與 IQ4_XS;24GB 統一記憶體選 **Q6_K**),以 `ollama pull hf.co/SakuraLLM/Sakura-GalTransl-7B-v3.7:Q6_K`(或 Modelfile FROM GGUF)取得;**必設 num_ctx**(預設值太小,術語+context 會被截斷)與 **keep_alive**(避免閒置卸載);Ollama 維持預設只聽 localhost:11434。模型授權 CC BY-NC-SA(禁商用,個人使用無虞)。
> 2. **OpenCC `s2twp` 整段免做**:PlayTranslate 內建 opencc4j s2tw+台灣詞彙、顯示時轉換(源碼實證,見 verification-results.md)。**代理輸出維持簡中**,由 PT 端轉繁。
> 3. **代理的新核心職責 = 動態專有名詞術語注入**(使用者需求):per-game 術語表(日文詞 → 台灣譯名),逐請求掃描原文、只注入命中詞條,採 GalTransl `gpt_dict` 格式(Sakura-GalTransl 原生支援)。Pokémon 官方繁中譯名可自神奇寶貝百科(52poke)整批建表。
> 4. 架構:Thor(PT Custom URL,http 連 LAN 已驗證可行)→ **代理綁 0.0.0.0**(如 :8000)→ 轉發 localhost:11434/v1(Ollama 免曝露 LAN)。
> 5. Step 3/4 的代理實作與測試,將 OpenCC 相關項替換為術語注入(命中注入、未命中不注入、glossary 檔缺失的錯誤處理);其餘轉發/錯誤處理/串流限制照原文。
> 6. Mac 端指令由 Claude 提供、使用者複製執行(開發環境在 WSL,Mac 為部署目標)。

> Depends on: none（可與 Phase 01 平行）
> Produces: `pyproject.toml`、`server/proxy/`（FastAPI 代理）、`tests/test_proxy.py`、`tools/sample_dialogue.jsonl`、`tools/quality_check.py`

目標：提供一個 **OpenAI 相容 endpoint**，內部由 Sakura-GalTransl-7B（日→簡中、遊戲對話特化）翻譯，出口經 OpenCC `s2twp` 轉台灣正體。任何 client（PlayTranslate、curl、之後的其他工具）都能直接填這個 endpoint。

## Pre-flight

- `uv --version` 可用
- 確認 server 主機硬體（GPU VRAM），依下表選型：

| 硬體 | 模型 | 備註 |
|------|------|------|
| GPU ≥ 6GB VRAM | Sakura-GalTransl-7B-v3.7 IQ4_XS | 建議起點；單句 2–5s |
| GPU ≥ 12GB VRAM | Sakura-14B IQ4_XS | 品質更佳 |
| 無 GPU | 跳過本地模型；client 直連雲端（Gemini Flash-Lite，30h 遊戲約 US$2）或代理指向雲端 | 品質仍佳、延遲 <1s |

## Steps

### 1. 部署 llama.cpp server（於 server 主機）

```bash
# Download GGUF from HuggingFace: SakuraLLM/Sakura-GalTransl-7B-v3.7 (IQ4_XS)
llama-server -m Sakura-GalTransl-7B-v3.7-IQ4_XS.gguf -c 4096 -ngl 99 --host 0.0.0.0 --port 8080
curl http://localhost:8080/v1/models    # smoke test
```

### 2. 建立 repo scaffolding

**File:** `pyproject.toml`（CREATE）

```bash
uv init --name thor-translation
uv add fastapi uvicorn httpx opencc
uv add --dev pytest pytest-asyncio
```

### 3. 實作代理

**File:** `server/proxy/__init__.py`（CREATE，空檔）
**File:** `server/proxy/main.py`（CREATE）

FastAPI app，English docstrings/comments：

- `POST /v1/chat/completions`：讀取請求 JSON → 以 `httpx.AsyncClient` 轉發至 `{UPSTREAM_URL}/v1/chat/completions` → 對回應的 `choices[*].message.content` 套 `OpenCC("s2twp").convert()` → 回傳
  - v1 僅支援非串流：請求含 `"stream": true` 時強制改為 `false` 再轉發（程式內以 comment 註明此限制）
- `GET /v1/models`：透傳上游
- 設定走環境變數：`UPSTREAM_URL`（預設 `http://localhost:8080`）、`PROXY_PORT`（預設 `8081`）
- 錯誤處理：上游逾時/非 2xx → 回 502，body 帶上游狀態碼與截斷後的上游訊息（可行動的錯誤內容，不吞例外）
- 啟動：`uv run uvicorn server.proxy.main:app --host 0.0.0.0 --port 8081`

### 4. 測試

**File:** `tests/test_proxy.py`（CREATE）

用 `httpx.ASGITransport` 打 app、monkeypatch 攔截對上游的呼叫：

- happy path：mock 上游回簡中（如「这个软件不能用了」）→ 代理輸出含台灣用語轉換（「軟體」），驗證 `s2twp` 詞彙級轉換生效
- edge：上游回 500 → 代理回 502 且錯誤訊息含上游狀態碼
- edge：`content` 為空字串 → 原樣通過、不噴例外

### 5. 品質與延遲抽測

**File:** `tools/sample_dialogue.jsonl`（CREATE）— 10 句日文遊戲對話測試集，需包含：一組「兩行捲一行」連續樣本（供 Phase 04 對照）、一組人名反覆出現樣本（驗證譯名一致性）

**File:** `tools/quality_check.py`（CREATE）— 逐句 POST 至代理（system prompt 採 GalTransl 官方建議格式，並附前文作 context），印出「原文 / 譯文 / 耗時」，結尾印 p50/p95 延遲。

```bash
uv run python tools/quality_check.py --endpoint http://localhost:8081/v1
```

人工評估譯文品質（連貫性、譯名一致、台灣用語）。

### 6. 讓掌機可達

- 防火牆開 8081；Thor 與 server 同 LAN，於 Thor 瀏覽器或 termux `curl` 驗證連通
- 安全：**不對公網開放**；外出使用需求走 Tailscale 等 VPN（不在本階段範圍）

## Phase Verification

- [ ] `curl -X POST http://<server>:8081/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"local","messages":[{"role":"user","content":"..."}]}'` 回繁體中文
- [ ] `uv run pytest -q` 全綠
- [ ] LAN 端到端單句延遲 ≤ 3s（50 token 級對話）；記錄 p50/p95

## Regression Tests

`uv run pytest -q`（`tests/test_proxy.py` 永久保留，後續階段不得弄壞）
