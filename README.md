# 學校財務查詢（EDB Finance Q&A）

教職員在文字欄輸入問題，網頁即時從教育局「財務管理」三個網頁及其連結的 PDF／Word 文件中
找出相關**原文段落**，附出處（文件名稱、頁數、所屬網頁及連結）。

- **沒有 AI／LLM 呼叫**，零 API 成本。檢索用 BM25（中文字二元組 + 英文單詞）在瀏覽器本機完成。
- 資料庫是一個靜態檔 `site/data/index.js`，由 `build_index.py` 每月重建。
- 整個網站是純靜態檔案，任何網頁伺服器、學校內聯網、GitHub Pages，甚至直接雙擊 `index.html` 都能用。

## 資料來源

```
https://www.edb.gov.hk/tc/sch-admin/fin-management/about-fin-management/index.html
https://www.edb.gov.hk/tc/sch-admin/fin-management/subsidy-info/index.html
https://www.edb.gov.hk/tc/sch-admin/fin-management/notes-sch-fin/index.html
```

爬蟲由這三頁出發，跟隨同一「財務管理」欄目下的子頁（最多三層），下載所有 `.pdf` / `.doc` / `.docx`
（Excel 表格範本不納入）。目前約 14 個網頁、87 份文件、2,100 段原文，索引約 0.6 MB。

## 目錄

```
build_index.py            爬蟲 + 文字抽取 + 分段 + 產生索引
requirements.txt          pypdf
site/index.html           查詢網頁（單一檔案，含檢索邏輯）
site/data/index.js        資料庫（window.EDB_INDEX = {...}）
site/data/index.json      同一資料的純 JSON
cache/                    下載的文件及 ETag／Last-Modified（下次只抓有更新的）
scripts/update.sh         重建索引並寫入 logs/
scripts/com.school.edb-finance-qa.plist   macOS launchd 每月排程
.github/workflows/update.yml              GitHub Pages 每月自動重建
```

## 首次建立

```bash
cd /Users/chad/edb-finance-qa
python3 -m pip install --user -r requirements.txt
python3 build_index.py          # 約 5 分鐘（首次需下載全部文件）
open site/index.html            # 或放到任何靜態伺服器
```

macOS 以內建 `textutil` 讀取 .doc/.docx；Linux 伺服器請安裝 `antiword`（.docx 有內建後備解析）。

## 每月更新

任選其一：

**A. 這部 Mac（launchd）**
```bash
cp scripts/com.school.edb-finance-qa.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.school.edb-finance-qa.plist
launchctl start com.school.edb-finance-qa      # 立即試跑一次
```
每月 1 日 03:30 執行；關機時會在下次開機後補跑。更新後把 `site/` 複製到網頁伺服器即可
（或直接把伺服器目錄指向 `site/`）。

**B. Linux 伺服器（cron）**
```
30 3 1 * * /bin/bash /path/to/edb-finance-qa/scripts/update.sh
```

**C. GitHub Pages（免伺服器）**
推送到 GitHub，Settings → Pages → Source 選「GitHub Actions」。`update.yml` 每月 1 日重建並部署，
亦可在 Actions 頁按「Run workflow」手動更新。

## 檢索與整合邏輯（`site/index.html`）

**第一步：找段落（BM25）**
1. 去除問句中的疑問詞／語氣詞（請問、如何、可否、幾時、嗎…），保留實質字眼；中文數字（五萬元）同時轉成文件慣用的 50,000。
2. 中文按相鄰兩字切成二元組，英文／數字按單詞；對全部段落建立 BM25 倒排索引（載入時在瀏覽器完成，約 2,000 段，毫秒級）。
3. 問句中連續兩字以上的片語若原文完整出現，額外加分；出現在文件標題也加分。每份文件最多取兩段。

**第二步：整合成「綜合答案」（抽取式，非生成式）**
4. 把最相關的 12 段拆成句子；常見問題文件中的「問：」句用作評分、但引用其後的「答：」句。
5. 每句按與問題的字詞覆蓋率、片語命中、規範性字眼（須／不得／上限／截止…）、所屬段落排名評分；
   問「幾時」偏好含日期的句子，問「幾多／上限」偏好含金額的句子。
6. 剔除雜訊：目錄、導航路徑、網址、標題行、表格數字行、「請參閱附件」類互相引用。
7. 以字詞重疊率去重（>50% 視為重複），最多選 5 句；相關度跌至首句 55% 以下即停。
8. 每句附引用編號，連到下方收合的原文段落及出處（文件名稱、頁數、PDF 直接開到該頁）。

所有句子均為文件原文，沒有改寫，因此不會「作答」出文件沒有的內容；代價是答案不會跨句歸納。

## 已知限制

- 掃描版 PDF（純圖片）無法抽取文字，建索引時會記錄 `EMPTY` 並略過。
- 「綜合答案」是按相關度挑選的原文句子，不會跨文件歸納或推論；使用者需自行閱讀出處確認。
- 若問題字眼與文件用語相差太遠（例如口語「畀錢」對文件「付款」），可能找不到；請用文件常用詞。
