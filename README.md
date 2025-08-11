# Auto-SEO
# RSS 排名器（rss_ranker）

`rss_ranker.py` 是一個用來評估並排列多個 RSS Feed 品質的 Python 指令工具。程式會抓取各 Feed 的文章數量、最近更新時間、發布頻率與規律性，依照權重計算出最終分數，幫助你快速找到值得關注的來源。

## 依賴套件

執行前請先安裝以下套件：

```bash
pip install pandas requests feedparser python-dateutil
```

## 使用方式

```bash
python3 rss_ranker.py --input INPUT.csv [--output OUTPUT.csv] \
    [--header-row-index 2] [--lookback-days 90] [--workers 10] \
    [--min-interval-days 0.5] [--ignore-interval-below-minutes 1]
```

### 參數說明
- `--input`：輸入的 CSV 檔案路徑。檔案需包含 `Main_site`, `Sub_site`, `URL`, `RSS_URL` 欄位，表頭所在列以 `--header-row-index` 指定（預設為第 3 列，0-based）。
- `--output`：輸出的 CSV 檔案路徑，預設為 `rss_ranked.csv`。
- `--header-row-index`：表頭所在的 0-based 列索引，預設為 `2`。
- `--lookback-days`：計算近期文章量的回顧天數，預設 `90` 天。
- `--workers`：並行抓取 RSS 的執行緒數量，預設為 `10`。
- `--min-interval-days`：文章平均間隔天數的下限，預設 `0.5` 天。
- `--ignore-interval-below-minutes`：忽略相鄰文章間隔小於此分鐘數的資料，預設 `1` 分鐘。

## 評分指標與權重
程式會針對每個 Feed 計算下列指標：

- **量**（Volume）：指定回顧天數內的文章數量。
- **新鮮度**（Recency）：最近一篇文章與現在的時間差。
- **頻率**（Frequency）：發布的平均頻率。
- **規律性**（Regularity）：發布間隔的標準差。

預設權重為：量 `30%`、新鮮度 `60%`、頻率 `5%`、規律性 `5%`。可在原始碼中調整，若希望權重總和為 `1`，可將 `NORMALIZE_WEIGHTS` 設為 `True`。

## 輸出
完成後會產生含各項指標與最終分數的 CSV 檔，並依分數與最近更新時間排序，方便後續分析或挑選。

## 注意
請確保你擁有合法抓取各 RSS 來源的權利，並遵守網站使用規範。

