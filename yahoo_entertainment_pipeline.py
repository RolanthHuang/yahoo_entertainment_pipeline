#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Yahoo 奇摩娛樂即時新聞 Pipeline
================================
目標：
1. 使用 Selenium 爬取 Yahoo 娛樂即時新聞列表
2. 篩選最近 12 小時新聞
3. 逐篇進入新聞頁抓取正文
4. 使用 LLM 做：
   - 新聞內文摘要
   - NER：找出人名、團體名
   - 判斷是否為「演唱會」相關議題
5. 輸出 CSV / JSON

安裝：
    pip install selenium requests

執行：
    python yahoo_entertainment_pipeline.py

API key：
    你可以直接跑，程式會提示你貼上新的 OpenAI API key。
    或者先在 Terminal 設定：
    export OPENAI_API_KEY="你的新key"
"""

import os
import re
import csv
import json
import time
import getpass
import argparse
import logging
from datetime import datetime, timedelta
from typing import Dict, List

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException


# ---------------------------------------------------------------------------
# 基本設定
# ---------------------------------------------------------------------------

TARGET_URL = "https://tw.news.yahoo.com/entertainment/archive/"

HOURS_WINDOW = 999
MAX_SCROLLS = 80
SCROLL_PAUSE = 1.5
ARTICLE_DELAY = 1.2
HEADLESS = True

OUTPUT_CSV = "output/yahoo_entertainment_latest_result.csv"
OUTPUT_JSON = "output/yahoo_entertainment_latest_result.json"

# 你可以把新的 API key 貼在這裡，但比較安全是留空，讓程式執行時提示你貼
OPENAI_API_KEY = ""

LLM_BASE_URL = "https://api.openai.com/v1"
LLM_MODEL = "gpt-4o-mini"

ARTICLE_URL_PATTERN = re.compile(r"-\d{6,12}\.html")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("yahoo_pipeline")


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

def get_openai_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()

    if not key:
        key = OPENAI_API_KEY.strip()

    if not key:
        try:
            key = getpass.getpass("請貼上新的 OpenAI API key（畫面不會顯示）：").strip()
        except Exception:
            key = input("請貼上新的 OpenAI API key：").strip()

    return key


# ---------------------------------------------------------------------------
# 時間解析
# ---------------------------------------------------------------------------

def parse_relative_time(text: str, now: datetime) -> datetime:
    text = text.strip()

    m = re.match(r"(\d+)\s*秒前", text)
    if m:
        return now - timedelta(seconds=int(m.group(1)))

    m = re.match(r"(\d+)\s*分鐘前", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))

    m = re.match(r"(\d+)\s*小時前", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    if "剛剛" in text or "剛才" in text:
        return now

    m = re.match(r"(\d+)\s*天前", text)
    if m:
        return now - timedelta(days=int(m.group(1)))

    if "昨天" in text:
        return now - timedelta(days=1)

    return now - timedelta(days=999)


# ---------------------------------------------------------------------------
# Selenium 初始化
# ---------------------------------------------------------------------------

def build_driver(headless: bool = True) -> webdriver.Chrome:
    options = Options()

    if headless:
        options.add_argument("--headless=new")

    chrome_bin = os.getenv("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin

    options.add_argument("--window-size=1400,2200")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=zh-TW")
    options.add_argument("--disable-blink-features=AutomationControlled")

    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    chrome_bin = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    
    options.binary_location = chrome_bin
    
    service = Service(executable_path="/usr/bin/chromedriver")
    
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)
    return driver    


def wait_page_ready(driver: webdriver.Chrome, timeout: int = 15):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def dismiss_consent_if_present(driver: webdriver.Chrome):
    candidates = [
        "//button[contains(., '同意')]",
        "//button[contains(., 'Accept')]",
        "//button[contains(., '接受')]",
        "//button[@name='agree']",
    ]

    for xpath in candidates:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            btn.click()
            log.info("已關閉同意彈窗")
            time.sleep(1)
            return
        except TimeoutException:
            continue
        except Exception:
            continue


# ---------------------------------------------------------------------------
# 列表頁抓取
# ---------------------------------------------------------------------------

_EXTRACT_LIST_JS = r"""
const urlPattern = /-\d{6,12}\.html/;
const timePattern = /(剛剛|剛才|昨天|\d+\s*(?:秒|分鐘|小時|天)前)/;
const sepPattern = /[・•·‧]/;

const anchors = Array.from(document.querySelectorAll('a[href]'));
const seen = new Set();
const results = [];

for (const a of anchors) {
  const href = a.href || '';
  if (!href || !urlPattern.test(href)) continue;
  if (seen.has(href)) continue;

  const title = (a.innerText || '').trim();
  if (!title) continue;

  let el = a;
  let blockText = null;

  for (let depth = 0; depth < 10 && el.parentElement; depth++) {
    el = el.parentElement;

    const innerHrefs = new Set(
      Array.from(el.querySelectorAll('a[href]'))
        .map(x => x.href || '')
        .filter(h => urlPattern.test(h))
    );

    if (innerHrefs.size > 1) break;

    const text = el.innerText || '';

    if (sepPattern.test(text) && timePattern.test(text)) {
      blockText = text;
      break;
    }
  }

  if (blockText === null) continue;

  seen.add(href);
  results.push({
    href: href,
    title: title,
    blockText: blockText
  });
}

return results;
"""


def extract_articles_from_list(driver: webdriver.Chrome) -> List[Dict]:
    raw = driver.execute_script(_EXTRACT_LIST_JS) or []

    results = []
    seen_urls = set()

    for item in raw:
        url = item.get("href", "").strip()
        title = item.get("title", "").strip()
        block_text = item.get("blockText", "")

        if not url or not title or url in seen_urls:
            continue

        m = re.search(
            r"([^\n・•·‧]{1,50})\s*[・•·‧]\s*"
            r"((?:剛剛|剛才|昨天|\d+\s*(?:秒|分鐘|小時|天)前))",
            block_text,
        )

        if not m:
            continue

        source = m.group(1).strip()
        time_text = m.group(2).strip()

        if len(source) > 50:
            continue

        seen_urls.add(url)

        results.append(
            {
                "title": title,
                "url": url,
                "source": source,
                "time_text": time_text,
            }
        )

    return results


def click_more_if_exists(driver: webdriver.Chrome) -> bool:
    labels = [
        "顯示更多",
        "查看更多",
        "載入更多",
        "更多",
        "Load more",
    ]

    for label in labels:
        try:
            btn = driver.find_element(
                By.XPATH,
                f"//*[self::button or self::a][contains(normalize-space(.), '{label}')]"
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(2)
            return True
        except NoSuchElementException:
            continue
        except Exception:
            continue

    return False


def force_scroll_page(driver: webdriver.Chrome):
    """
    比單純 scrollTo bottom 更容易觸發 Yahoo lazy-load。
    """
    try:
        body = driver.find_element(By.TAG_NAME, "body")
    except Exception:
        body = None

    last_height = driver.execute_script("return document.body.scrollHeight") or 0

    for _ in range(10):
        driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.85));")
        time.sleep(0.45)

    if body:
        try:
            body.send_keys(Keys.PAGE_DOWN)
            time.sleep(0.4)
            body.send_keys(Keys.PAGE_DOWN)
            time.sleep(0.4)
            body.send_keys(Keys.END)
            time.sleep(1.2)
        except Exception:
            pass

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1.5)

    driver.execute_script("window.dispatchEvent(new Event('scroll'));")
    time.sleep(0.5)

    new_height = driver.execute_script("return document.body.scrollHeight") or 0
    return new_height > last_height


def scroll_and_collect(driver: webdriver.Chrome, hours_window: int) -> List[Dict]:
    now = datetime.now()
    cutoff = now - timedelta(hours=hours_window)

    collected = {}
    oldest_seen = now
    no_growth_rounds = 0

    for i in range(MAX_SCROLLS):
        before_count = len(collected)

        batch = extract_articles_from_list(driver)

        for item in batch:
            ts = parse_relative_time(item["time_text"], now)
            item["parsed_time"] = ts

            if ts < oldest_seen:
                oldest_seen = ts

            if item["url"] not in collected:
                collected[item["url"]] = item

        after_count = len(collected)

        log.info(
            "第 %d 次：目前累積 %d 篇，最舊約 %s",
            i + 1,
            after_count,
            oldest_seen.strftime("%Y-%m-%d %H:%M"),
        )

        if oldest_seen < cutoff - timedelta(minutes=30):
            log.info("已涵蓋最近 %d 小時，停止捲動", hours_window)
            break

        if after_count == before_count:
            no_growth_rounds += 1
        else:
            no_growth_rounds = 0

        clicked = click_more_if_exists(driver)

        if not clicked:
            height_grew = force_scroll_page(driver)
            if height_grew:
                time.sleep(SCROLL_PAUSE)

        if no_growth_rounds >= 10:
            log.warning(
                "連續 %d 輪沒有新增新聞。目前最舊只有 %s，可能 Yahoo 沒再載入更多。",
                no_growth_rounds,
                oldest_seen.strftime("%Y-%m-%d %H:%M"),
            )
            break

    final = [
        item for item in collected.values()
        if item["parsed_time"] >= cutoff
    ]

    final.sort(key=lambda x: x["parsed_time"], reverse=True)

    if final:
        oldest_final = min(x["parsed_time"] for x in final)
        if oldest_final > cutoff + timedelta(hours=1):
            log.warning(
                "警告：目前最舊新聞只有 %s，還沒有真的涵蓋最近 %d 小時。",
                oldest_final.strftime("%Y-%m-%d %H:%M"),
                hours_window,
            )

    return final


# ---------------------------------------------------------------------------
# 新聞內文抓取
# ---------------------------------------------------------------------------

_EXTRACT_BODY_JS = r"""
const selectors = [
  'article p',
  'div.caas-body p',
  'div[data-test-locator="caas-body"] p',
  'div[data-testid="caas-body"] p',
  'main p'
];

let texts = [];

for (const selector of selectors) {
  const nodes = Array.from(document.querySelectorAll(selector));

  for (const node of nodes) {
    const text = (node.innerText || '').trim();
    if (!text) continue;
    texts.push(text);
  }

  if (texts.length >= 3) break;
}

const seen = new Set();
const cleaned = [];

for (let text of texts) {
  text = text.replace(/\s+/g, ' ').trim();

  if (text.length < 10) continue;
  if (seen.has(text)) continue;

  if (/看更多|更多新聞|延伸閱讀|相關新聞|Yahoo奇摩|下載APP|按讚|追蹤|加入為 Google/.test(text)) {
    continue;
  }

  seen.add(text);
  cleaned.push(text);
}

const h1 = document.querySelector('h1');
const detailTitle = h1 ? (h1.innerText || '').trim() : '';

return {
  detailTitle: detailTitle,
  content: cleaned.join('\n')
};
"""


def extract_article_content(driver: webdriver.Chrome, url: str) -> Dict[str, str]:
    try:
        driver.get(url)
        wait_page_ready(driver)
        dismiss_consent_if_present(driver)
        time.sleep(ARTICLE_DELAY)

        driver.execute_script("window.scrollBy(0, 600);")
        time.sleep(0.5)

        data = driver.execute_script(_EXTRACT_BODY_JS) or {}

        return {
            "detail_title": data.get("detailTitle", "").strip(),
            "content": data.get("content", "").strip(),
        }

    except Exception as e:
        log.warning("抓取內文失敗：%s，原因：%s", url, e)
        return {
            "detail_title": "",
            "content": "",
        }


# ---------------------------------------------------------------------------
# LLM 分析
# ---------------------------------------------------------------------------

def extract_json_from_text(text: str) -> Dict:
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError("LLM 回傳不是合法 JSON")


def analyze_with_llm(title: str, content: str, api_key: str) -> Dict:
    if not api_key:
        raise RuntimeError("沒有 OpenAI API key")

    content_for_llm = (content or "")[:7000]

    system_prompt = (
        "你是繁體中文娛樂新聞資料標註助手。"
        "你只能輸出合法 JSON，不要輸出 Markdown，不要加解釋。"
    )

    user_prompt = f"""
請根據以下 Yahoo 娛樂新聞，完成三件事：

1. summary：
   用繁體中文寫 80 字以內摘要。
   摘要要具體，不要只寫「本文報導了某某事件」。

2. entities：
   抽出新聞中出現的「人名」與「團體名」。
   例如：五月天、周杰倫、BLACKPINK、Energy、張惠妹。
   只放真正的人名或團體名，不要放公司名、地名、節目名。
   沒有就回傳空陣列 []。

3. is_concert_related：
   判斷此新聞是否主要和「演唱會」有關。
   只要和開唱、巡演、售票、加場、嘉賓、場館、演出、演唱會事故有明顯關係，就回傳 true。
   否則回傳 false。

請只輸出這種 JSON 格式：

{{
  "summary": "...",
  "entities": ["..."],
  "is_concert_related": true
}}

新聞標題：
{title}

新聞內文：
{content_for_llm}
"""

    url = f"{LLM_BASE_URL}/chat/completions"

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, json=payload, timeout=60)

    if response.status_code >= 400:
        raise RuntimeError(f"LLM API 錯誤：{response.status_code} {response.text}")

    data = response.json()
    text = data["choices"][0]["message"]["content"]

    result = extract_json_from_text(text)

    summary = str(result.get("summary", "")).strip()
    entities = result.get("entities", [])
    is_concert_related = bool(result.get("is_concert_related", False))

    if not isinstance(entities, list):
        entities = []

    entities = [
        str(x).strip()
        for x in entities
        if str(x).strip()
    ]

    return {
        "summary": summary,
        "entities": entities,
        "is_concert_related": is_concert_related,
    }


def analyze_without_llm(title: str, content: str) -> Dict:
    """
    LLM 失敗時的備援。
    正式作業仍然會優先用 LLM。
    """
    text = (content or "").strip()

    text = re.sub(r"加入為 Google 偏好來源", "", text)
    text = re.sub(r"更多新聞.*", "", text)
    text = re.sub(r"延伸閱讀.*", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    summary = text[:120] if text else title[:120]

    concert_keywords = [
        "演唱會",
        "開唱",
        "巡演",
        "售票",
        "加場",
        "場館",
        "小巨蛋",
        "大巨蛋",
        "高雄巨蛋",
        "演出",
        "粉絲見面會",
        "嘉賓",
    ]

    is_concert = any(k in (title + text) for k in concert_keywords)

    return {
        "summary": summary,
        "entities": [],
        "is_concert_related": is_concert,
    }


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------

def save_outputs(rows: List[Dict]):
    os.makedirs("output", exist_ok=True)
    fieldnames = [
        "新聞標題",
        "新聞連結",
        "新聞來源",
        "發布時間文字",
        "推估發布時間",
        "新聞內文",
        "新聞內文摘要",
        "實體(人名/團體)",
        "是否為演唱會",
    ]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            writer.writerow(row)

    log.info("已寫入 CSV：%s，共 %d 筆", OUTPUT_CSV, len(rows))

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    log.info("已寫入 JSON：%s，共 %d 筆", OUTPUT_JSON, len(rows))


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="只測試爬蟲，不呼叫 LLM",
    )
    args = parser.parse_args()

    api_key = ""

    if not args.no_llm:
        api_key = get_openai_api_key()

    driver = build_driver(headless=HEADLESS)

    try:
        log.info("開啟 Yahoo 娛樂即時頁：%s", TARGET_URL)

        driver.get(TARGET_URL)
        wait_page_ready(driver)
        dismiss_consent_if_present(driver)
        time.sleep(2)

        articles = scroll_and_collect(driver, HOURS_WINDOW)

        log.info(
            "列表頁共取得 %d 則最近 %d 小時內的娛樂即時新聞",
            len(articles),
            HOURS_WINDOW,
        )

        rows = []

        for idx, article in enumerate(articles, start=1):
            title = article["title"]
            url = article["url"]

            log.info("[%d/%d] 抓取內文：%s", idx, len(articles), title)

            detail = extract_article_content(driver, url)
            content = detail["content"]

            if not content:
                log.warning("內文為空：%s", url)

            try:
                if args.no_llm:
                    analysis = analyze_without_llm(title, content)
                else:
                    analysis = analyze_with_llm(title, content, api_key)

            except Exception as e:
                log.warning("LLM 分析失敗，改用本地備援摘要：%s，原因：%s", title, e)
                analysis = analyze_without_llm(title, content)

            summary = analysis.get("summary", "").strip()
            entities = analysis.get("entities", [])
            is_concert_related = analysis.get("is_concert_related", False)

            row = {
                "新聞標題": title,
                "新聞連結": url,
                "新聞來源": article["source"],
                "發布時間文字": article["time_text"],
                "推估發布時間": article["parsed_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "新聞內文": content,
                "新聞內文摘要": summary if summary else "Null",
                "實體(人名/團體)": ",".join(entities) if entities else "Null",
                "是否為演唱會": "是" if is_concert_related else "否",
            }

            rows.append(row)

            print("=" * 80)
            print(row["新聞標題"])
            print(row["新聞來源"], row["發布時間文字"])
            print("摘要：", row["新聞內文摘要"])
            print("實體：", row["實體(人名/團體)"])
            print("是否為演唱會：", row["是否為演唱會"])

        save_outputs(rows)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()