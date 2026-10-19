---
name: moneyprinterturbo-storybook
description: Use this skill when the user wants a hand-drawn animated video (knowledge explainer / 科普影片, picture-book story, product or concept explanation) made with their deployed MoneyPrinterTurbo storybook service, or asks to operate, check, or troubleshoot that system (task status, failed generation, video size, Claude login, restarting containers). The service turns a narration script into a code-drawn watercolor/ink animation with voice, subtitles, BGM and a size-capped MP4.
---

# MoneyPrinterTurbo 手繪故事動畫

已部署的服務會把一份旁白稿做成手繪動畫影片：分鏡 → 逐句配音 → Claude 用程式碼畫出 canvas 動畫 →
無頭 Chromium 錄製 → 燒錄字幕、混背景音樂 → 壓到檔案上限以內。

- WebUI：http://192.168.9.6:8501（本機 http://127.0.0.1:8501）
- API：http://192.168.9.6:8080（本機 http://127.0.0.1:8080）
- 部署目錄：`~/src/MoneyPrinterTurbo`，`docker compose -f docker-compose.storybook.yml`
- 成片：`storage/tasks/<task_id>/final-1.mp4`，網址 `http://192.168.9.6:8080/tasks/<task_id>/final-1.mp4`

## 做一支科普影片

1. **自己寫稿**（最重要，見下一節）。存成 UTF-8 文字檔，一行一句。使用者給的是主題時由你寫；
   使用者給了稿就照用，只修正事實錯誤並告知。
2. **提交並等待**：以 `SKILL_DIR`（本檔所在目錄）為工作目錄，前景執行，timeout 至少 150 分鐘：

   ```bash
   MPT_PUBLIC=http://192.168.9.6:8080 python3 mpt_storybook.py run \
     --subject "傅利葉轉換是什麼" --script-file /path/to/script.txt --genre explainer
   ```

   helper 自己會輪詢並印出進度，不要另外用 sleep/ps/tail 輪詢。結束時印出 `DONE {...}`，
   內含網址、檔案大小、分鏡和動畫頁面路徑。
3. **檢查**：`python3 mpt_storybook.py preview <task_id>` 產生 3×3 截圖拼圖，用 Read 看一遍：
   畫面是否在講旁白講的東西、字卡文字有沒有錯字（分鏡模型偶爾會改字）、字幕是否可讀。
4. **交付**：給使用者網址、長度、大小，並如實說明看到的瑕疵。你聽不到聲音，要說明沒有試聽。

只給主題、不想寫稿時，可以省略 `--script-file`，改用 `--duration 120`（必要時加 `--prompt` 補充要點），
系統會用 explainer 模式自動寫稿（定義 → 問題 → 舊方法 → 一個貫穿的例子 → 2–4 個術語 → 應用 → 總結，
並以新手和專家角度各審一次）。品質不如親手寫的稿穩定。

## 寫稿規則（從實際失敗學到的）

第一版 Transformer 影片用「療癒、有傳播力」的故事寫法，觀眾看完仍不懂。科普稿要這樣寫：

- **第一句就給白話定義**：它是什麼、做什麼。最後一句用同一個定義總結。
- **先講它解決什麼問題**，用生活例子；再講舊方法為什麼不夠。
- **一個具體例子貫穿全片**，每一步都在同一個例子上「演」出來，給數字或結果，不喊口號。
- **術語最多 2–4 個**，先給直覺、再給名字（「這個『看』，叫做注意力」）。
- **每句都要教到東西**，不要角色拌嘴、不要情緒鋪陳。一行一句，句子短。
- **寫出畫面能畫的東西**：字卡、箭頭、長條圖、比例、疊層。圖上要出現的字要和旁白用字完全一致。
- 事實要正確；簡化可以，但不能說錯，必要時加一句「實際上還會…」。
- 用台灣慣用語（影片、程式、資訊、網路），語言 `zh-TW`。

**長度**：Edge 台灣女聲約每秒 4.5 個中文字。稿長（字數）÷ 4.5 ≈ 秒數。
20 MB 上限下：約 100 秒內保持 1080p，約 100–250 秒會降到 720p，再長會降到 540p。
科普片建議 120–200 秒。

## 參數（mpt_storybook.py）

| 參數 | 預設 | 說明 |
|---|---|---|
| `--genre` | explainer | `explainer` 科普 / `story` 療癒繪本故事 |
| `--style` | watercolor | `watercolor` 水彩繪本、`ink_wash` 水墨神話、`gongbi_scroll` 工筆長卷、`night_picture_book` 夜晚繪本 |
| `--aspect` | 16:9 | 直式短影音用 `9:16` |
| `--voice` | zh-TW-HsiaoChenNeural-Female | Edge 語音；男聲 `zh-TW-YunJheNeural-Male` |
| `--doubao` | 關 | 改用豆包配音（需 `.env` 的 `DOUBAO_TTS_API_KEY`） |
| `--max-mb` | 20 | 檔案上限，0 = 不壓縮 |
| `--bgm-volume` | 0.12 | 0 = 不要背景音樂 |

## 時間預期（2 核 CPU）

寫稿 1–3 分鐘、配音 < 1 分鐘、Claude 畫動畫 10–25 分鐘、錄製約影片長度 × 7、合成約影片長度 × 3、
壓縮數分鐘。3 分鐘的片約 70 分鐘。

## 疑難排解

- **`Not logged in`**：`.env` 的 `CLAUDE_CODE_OAUTH_TOKEN` 失效。請使用者在自己的電腦跑
  `claude setup-token`，更新 `.env` 後 `docker compose -f docker-compose.storybook.yml up -d`。
  驗證：`echo 'say ok' | docker exec -i moneyprinterturbo-webui claude -p --model haiku`
- **task not found**：任務狀態存在記憶體，容器重啟就會消失（檔案還在）。**有任務在跑時不要重啟容器。**
  helper 會在成片檔存在時直接回報完成，否則要重新提交。
- **失敗時**：看 `docker logs --since 30m moneyprinterturbo-api | grep -E 'storybook|ERROR'`。
  中間產物在 `storage/tasks/<id>/storybook/`：`story.json`（分鏡）、`timeline.json`、`index.html`
  （可直接用瀏覽器開來看動畫）、`preview-*.jpg`。
- **主機上直接跑程式出現 PermissionError: config.toml**：容器存設定時會把它改成 root 擁有，
  `sudo chown $USER config.toml` 即可。
- **改了程式碼**：程式碼是掛載進容器的，改完要 `docker compose -f docker-compose.storybook.yml restart`
  （注意上面「不要在任務進行中重啟」）。
- 不要印出 `.env` 或 `config.toml` 的內容（含金鑰與 token）。
