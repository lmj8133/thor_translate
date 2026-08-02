# Phase 02 翻譯後端 — Mac 部署與使用指南

架構（皆在同一 LAN）：

```
Thor (PlayTranslate, Custom URL)
   → http://<mac-ip>:8000/v1        FastAPI 術語注入代理（本目錄）
      → http://localhost:11434/v1   Ollama（Sakura-GalTransl-7B-v3.7）
```

代理的職責：把 PlayTranslate 的請求整段改寫成 Sakura-GalTransl v3.7 官方 prompt 格式，掃描原文、只注入命中的 per-game 術語（gpt_dict 格式），轉發給 Ollama。模型的簡中輸出在**代理出口直接轉台灣正體**（OpenCC `s2twp`，含台灣用語），不依賴 PT 端的顯示轉換設定；PT 回傳的前文 context 則以 `tw2sp` 正規化回簡體再餵給模型。

模型授權：CC BY-NC-SA（禁商用，個人使用無虞）。

## 1. 安裝模型（Mac，一次性）

> **⚠ 陷阱：不要用 `:Q6_K` 標籤，也不要不帶標籤直接 pull。**
> 該 HF repo 內含五個 Q6_K 檔（v1/v1.5/v2/v2.6/v3.7），`:Q6_K` 與預設標籤實際解析到 **GalTransl-7B-v1（舊模型）**。必須用完整檔名作標籤（此檔即 Q6_K 量化，約 6.25 GB）。

```bash
ollama pull hf.co/SakuraLLM/Sakura-GalTransl-7B-v3.7:Sakura-Galtransl-7B-v3.7.gguf
```

建立衍生模型（烘入 num_ctx——Ollama 的 OpenAI 相容 `/v1` 端點**無法**逐請求設定 context 長度，超長 prompt 會被靜默截斷）：

```bash
cat > /tmp/Modelfile.sakura <<'EOF'
FROM hf.co/SakuraLLM/Sakura-GalTransl-7B-v3.7:Sakura-Galtransl-7B-v3.7.gguf

# Baked-in context length: the OpenAI-compatible /v1 endpoint cannot set
# num_ctx per request, and the server default is too small for
# glossary + history + dialogue.
PARAMETER num_ctx 8192
EOF
ollama create sakura-galtransl-v3.7 -f /tmp/Modelfile.sakura
```

驗證（應顯示 family `qwen2`、7.72B 參數、context length 8192）：

```bash
ollama show sakura-galtransl-v3.7
```

## 2. 啟動代理（Mac）

安裝 uv（若尚未安裝）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

把本 repo 複製到 Mac 任意路徑後，在 repo 根目錄：

```bash
uv sync
GLOSSARY_PATH=glossaries/pokemon-oras.txt \
uv run uvicorn server.proxy.main:app --host 0.0.0.0 --port 8000
```

| 環境變數 | 預設 | 說明 |
|----------|------|------|
| `UPSTREAM_URL` | `http://localhost:11434` | Ollama 位址（不必曝露 LAN，由代理承擔） |
| `OLLAMA_MODEL` | `sakura-galtransl-v3.7` | 上游模型名（client 傳什麼 model 都會被覆蓋） |
| `GLOSSARY_PATH` | （未設 = 停用注入） | per-game 術語表檔案 |

**keep_alive**：代理啟動時會自動呼叫 Ollama 原生 API 把模型釘在記憶體（`keep_alive=-1`，兼作預熱），避免閒置 5 分鐘後卸載、下一句冷載入卡頓。**若之後重啟過 Ollama，請重啟代理**（或一勞永逸：`launchctl setenv OLLAMA_KEEP_ALIVE -1` 後重啟 Ollama app；注意此設定重開機後失效）。

## 3. 防火牆與連通驗證

首次啟動時 macOS 會詢問「是否允許連入連線」→ 允許（手動路徑：系統設定 → 網路 → 防火牆）。**勿對公網開放**；外出使用走 Tailscale 等 VPN（不在本階段範圍）。

從 WSL 或其他 LAN 機器驗證：

```bash
curl -s http://<mac-ip>:8000/v1/models -H 'Authorization: Bearer test'

curl -s http://<mac-ip>:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"any","messages":[{"role":"user","content":"ダイゴさんは　どこに　いますか？"}]}'
```

第二條應回含「大吾」的**台灣正體**譯文（術語注入與 s2twp 出口轉換皆生效）。

## 4. Thor 端 PlayTranslate 設定

Settings → Translation services → Add Online Translation Service → **OpenAI** → provider 選 **Custom**：

- **Custom URL**：`http://<mac-ip>:8000/v1`（http 連私網 IP 為 PT 白名單允許）
- **API key**：任意非空字串（如 `thor`；代理只檢查非空，非真正驗證）
- **Model**：任意（代理一律改用 `OLLAMA_MODEL`）
- 目標語言：**Chinese (Traditional, Taiwan)**（zh-Hant-TW）——代理輸出已是台灣正體（PT 再轉一次是恆等操作），但此設定讓瀑布備援的內建引擎也能正確轉繁
- Advanced LLM configuration → **開啟 context**（預設關閉）→ 前文會轉成 Sakura 的「历史翻译」，提升人稱與譯名連貫性
- System prompt / 翻譯 prompt **維持預設即可**（代理會整段改寫成 Sakura 官方格式；PT 的 system prompt 會被忽略）

## 5. 術語表維護

- 格式：每行 `src->dst #note`（note 可省略；`#`、`//` 開頭為註解行）
- **存檔即熱載入**（依 mtime），遊戲中途調整詞條免重啟
- 一款遊戲一檔；換遊戲改 `GLOSSARY_PATH` 重啟代理
- Pokémon 官方繁中譯名來源：[神奇寶貝百科（52poke wiki）](https://wiki.52poke.com/)
- 匹配為單純子字串包含：過短的詞可能誤中其他詞內部，命名時避免一兩個假名的詞條

## 6. 品質與延遲抽測

```bash
uv run python tools/quality_check.py --endpoint http://<mac-ip>:8000/v1
```

逐句送出 `tools/sample_dialogue.jsonl`（模擬 PT 預設請求格式、帶前文 context），印出每句「原文／譯文／耗時」與收尾 p50/p95。驗收目標：LAN 端到端單句 ≤ 3s（50 token 級對話）。

## 7. 疑難排解

| 症狀 | 原因 / 處理 |
|------|-------------|
| 502 `unreachable` | Ollama 沒啟動，或 `UPSTREAM_URL` 錯 |
| 502 `Upstream ... returned 404` | 模型名不存在——對照 `ollama ls` 與 `OLLAMA_MODEL` |
| 譯文尾端被截斷、或前文彷彿失憶 | num_ctx 不足（Ollama server log 會有 truncation 警告）→ 加大 Modelfile 的 `num_ctx` 重新 `ollama create` |
| 第一句特別慢、之後正常 | 模型冷載入。代理啟動時會預熱＋釘住；若 Ollama 重啟過，重啟代理 |
| PT 畫面顯示簡體 | 代理是未含 OpenCC 出口轉換的舊版（`uv sync` 後重啟）；或該句走了 PT 內建備援引擎且目標語言選成 Chinese (Simplified) |
| 專有名詞仍不穩 | 該詞不在術語表——加進 `GLOSSARY_PATH` 檔案存檔即生效 |
