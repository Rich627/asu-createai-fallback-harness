# ASU Codex Bridge

讓 Codex 在主供應商額度不足時，透過本機代理改用 ASU CreateAI Chat Completions。切換發生在模型請求層，保留同一個 Codex session、對話輸入與已完成的工具結果。

**狀態：本機模擬與 Codex CLI 整合測試通過；真實 ASU／主供應商連線及桌面 App 整合尚未驗證。** 目前不會修改你的 Codex 設定、登入、桌面 App 或背景服務。

## 你需要完成的步驟

1. 在 CreateAI Builder 專案中開啟 **Profile → API Keys → Request API Key**。
2. **Key Type** 選 `Service`；**Key Name** 填 `codex-fallback`。
3. **Intended API Use** 可填以下實際用途，然後按 Next 完成申請：

   > I would like to use CreateAI as a backup model provider for my local Codex coding assistant. A local API compatibility bridge will translate Codex requests into CreateAI chat completions. If my primary provider reaches its usage limit, the assistant should continue the interrupted coding task using my CreateAI project, including conversation context and tool calls. The service token will remain private on my machine and will not be embedded in shared applications.

4. 在 Builder 專案選擇你要的模型。預設 `defaults` 使用該專案的模型；不要猜 GPT-6 的 API ID。
5. 取得 Service token 後，在 Terminal 執行下方檢查。程式會隱藏輸入 token，只保留於記憶體；不要貼到聊天、原始碼或 shell 命令列。

```sh
cd ~/Developer/asu-codex-bridge
python3 codex_asu.py --doctor
```

這會列出此 token 可用的 model ID，並進行兩個小型模型請求，驗證串流、function call、工具結果與接續回覆。會消耗少量 ASU 額度。如果專案在 beta 或 POC，使用 `--environment beta` 或 `--environment poc`。

若遇到 HTTP 500，先做分段診斷：

```sh
python3 ~/Developer/asu-codex-bridge/codex_asu.py --diagnose
```

依序獨立檢查模型清單、最小文字請求、最小串流請求，以及原生 Responses。模型清單失敗不會阻止後續檢查；每次最多三個小型模型請求。不會輸出 token、回應原文或伺服器內部錯誤內容。請提供各步 PASS/FAIL 資訊。HTTP 500 本身不足以判定 token 有效、模型相容，或服務全面故障。

2026-09-11 查閱的 ASU 文件已新增 `/v1/responses` 支援，和最初開發時的文件不同；此診斷會確認你所使用環境的實際支援狀態。基本 Responses 測試成功仍不代表完整 Codex 工具或自動接續已通過。

## Codex CLI 使用方式

需要 Python 3.10+ 與已安裝的 Codex CLI。無第三方 Python 套件。

先測 ASU 模式：

```sh
python3 ~/Developer/asu-codex-bridge/codex_asu.py -- -C /path/to/project
```

主供應商使用 ChatGPT 訂閱登入、額度不足時自動切換：

```sh
python3 ~/Developer/asu-codex-bridge/codex_asu.py --auto -- -C /path/to/project
```

主模型預設 `gpt-6-astra`，ASU 模型預設 `defaults`。可明確指定：

```sh
python3 ~/Developer/asu-codex-bridge/codex_asu.py \
  --auto --primary-model gpt-6-astra --model 'EXACT_ASU_MODEL_ID' \
  -- -C /path/to/project
```

若你使用的是 OpenAI API 計費，且 Codex 已設定 API key 登入，使用 `--primary api`。主供應商認證由 Codex 正常登入流程提供；本程式不讀取 `auth.json` 或 Keychain。CLI 與 App 的登入是否共用，需要依你安裝的版本確認。

一般 Codex 參數放在 `--` 後，例如 `exec` 或 `resume SESSION_ID`。已有 session 若包含無法轉換的伺服器端狀態，會明確報錯；不能保證任意舊 session 可跨供應商恢復。新啟動的 auto session 不需要重啟 Codex 就能在辨識出的額度錯誤後切換。

也可以由外部安全工具提供 `ASU_CREATEAI_TOKEN` 環境變數。啟動器會從 Codex 子程序環境移除此變數，避免工具程序繼承 ASU token。

## 自動切換的範圍

- 主供應商 HTTP 402／429 回傳 `insufficient_quota`、`usage_limit_reached`、`quota_exceeded` 或 `billing_hard_limit_reached`，或在尚未輸出內容時回傳同類串流錯誤，會切到 ASU。
- 切換後直到本次啟動結束都使用 ASU，不在每一步反覆嘗試已用完額度的主供應商。
- 已完成的工具結果包含在下一次請求中，不主動重跑先前工具。
- 一般網路故障、401／403、短暫 rate limit、任務本身出錯不會被當成額度耗盡。
- 若主供應商已輸出部分內容／工具呼叫後才遇到額度錯誤，本次請求不自動重送；下一次請求才走 ASU。無法保證在程序崩潰或所有種類中斷後無縫恢復。
- ASU token 失效、額度用完或模型不相容時會停止，沒有第二個隱藏付費後端。

## 相容性限制

這是 Responses 的部分相容實作，支援文字串流、一般 function calls、namespace 工具、以 JSON 包裝的 custom/freeform tools，以及基本使用者圖片輸入。工具呼叫會等待整個 ASU 串流完成且參數解析成功後才交回 Codex。

啟動器在本次執行停用原生網頁搜尋、Apps、plugins、computer/browser use、image generation、code mode 與請求壓縮，以符合此相容層的範圍。Codex 原本的沙箱與審批規則維持由 Codex 管理。這些旗標只作用於本次啟動；不會改寫 `~/.codex/config.toml`。

不支援伺服器端 conversation IDs、Responses hosted tools、遠端 compact endpoint、加密 compaction state、檔案 ID 或完整原生多媒體工具。ASU 不接收原供應商的 opaque reasoning；可見訊息與工具結果仍保留。custom tool 的 grammar 只作為模型說明傳送，ASU 不會原生強制該 grammar。

ASU 自動接手時會收到這個 task 的對話、程式碼片段與工具輸出；資料會依 ASU 專案的設定處理。原供應商 token 只轉送到指定的 OpenAI／ChatGPT 主端點，不轉送 ASU。

## 桌面 App

目前尚未安裝桌面 App 的路由設定。需要先確認你要求的是哪個桌面版本、驗證它會採用自訂 provider，再以可回復方式設定固定本機端點與服務。**CLI 的測試通過不代表目前這個 App 視窗已具備 fallback。** 不應在正在執行的 task 中途改動 provider 或重啟 App。

## 開發與驗證

```sh
python3 -m unittest -v
RUN_CODEX_INTEGRATION=1 python3 -m unittest -v
```

第二個命令會以本機模擬模型及臨時目錄執行真正的 Codex CLI。測試使用假的認證與 `--ignore-user-config`，不使用真實模型 API；驗證工具結果接續，以及主模型工具執行後發生額度錯誤時，ASU 在同一個 thread 完成回覆。

程式僅監聽 `127.0.0.1`，每次啟動產生本機認證 token，拒絕有 Origin 的瀏覽器請求，不記錄請求內容或認證。關閉 Codex 時會停止代理。

## macOS 自動啟動與桌面 App

以下安裝器會將 ASU Service token 存入 macOS Keychain，執行 CreateAI 基本 API 與工具接續檢查，接著才備份並更新 `~/.codex/config.toml`。它會安裝使用者層級 LaunchAgent，在你登入 macOS 後啟動背景 bridge；不需要每次貼 token。ASU token 不會寫進 Codex 設定或 LaunchAgent plist。

```sh
python3 ~/Developer/asu-codex-bridge/setup_macos.py install
```

安裝器只會要求輸入一次 ASU CreateAI Service token，透過 macOS 原生 Keychain API 儲存，然後立刻在記憶體中比對讀回內容。Token 不會出現在程序參數或 shell history。若安裝前測試回傳 403，Codex 設定不會被修改；直接重跑安裝並更新 Keychain 內容即可。

安裝成功後，完整結束並重開 ChatGPT/Codex。確認狀態：

```sh
python3 ~/Developer/asu-codex-bridge/setup_macos.py status
```

完整移除並還原安裝前的 provider 設定：

```sh
python3 ~/Developer/asu-codex-bridge/setup_macos.py uninstall
```

解除安裝預設也刪除 Keychain token；加上 `--keep-token` 可保留。原始設定備份留在 `~/.codex/config.toml.asu-backup-*`，不包含 ASU token。

這是「登入後自動啟動」，因為使用者 Keychain 在登入前不可用。LaunchAgent 若異常退出會由 macOS 重新啟動。固定本機端點為 `127.0.0.1:41117`，只接受沒有瀏覽器 Origin 的 Bearer-authenticated 請求。

## 參考文件

- [ASU OpenAI-compatible API](https://docs.aiml.asu.edu/openai-compatible.md)
- [ASU API access](https://ai.asu.edu/ai-tools/createai-platform)
- [ASU rate limits](https://docs.aiml.asu.edu/limits.md)
- [Codex custom providers](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
