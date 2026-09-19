"""Polite scrape of oilprice.com archive listings (title, timestamp, excerpt) back to 2024-03-01."""
import html, json, re, time
from datetime import datetime
import requests

UA = {"User-Agent": "Mozilla/5.0 (personal research backtest)"}
STOP = datetime(2024, 3, 1)
out = open("news_oilprice.jsonl", "w")


def parse(u):
    h = requests.get(u, headers=UA, timeout=30).text
    arts = []
    for b in h.split('<div class="categoryArticle__content">')[1:]:
        t = re.search(r'categoryArticle__title">(.*?)</h2>', b, re.S)
        m = re.search(r'categoryArticle__meta">(.*?)</p>', b, re.S)
        ex = re.search(r'categoryArticle__excerpt">(.*?)</p>', b, re.S)
        if t and m:
            ts = datetime.strptime(m.group(1).split("|")[0].strip(), "%b %d, %Y at %H:%M")
            arts.append(dict(title=html.unescape(t.group(1).strip()), ts=ts.isoformat(),
                             excerpt=html.unescape(re.sub("<.*?>", "", ex.group(1))).strip() if ex else ""))
    return arts


for section, url in [("crude", "https://oilprice.com/Energy/Crude-Oil/Page-{}.html"),
                     ("world", "https://oilprice.com/Latest-Energy-News/World-News/Page-{}.html")]:
    page = 1
    while True:
        try:
            arts = parse(url.format(page))
        except Exception as e:
            print(section, page, "error", e, flush=True); time.sleep(10); continue
        if not arts:
            print(section, page, "empty; stop", flush=True); break
        for a in arts:
            a["section"] = section
            out.write(json.dumps(a) + "\n")
        out.flush()
        oldest = min(datetime.fromisoformat(a["ts"]) for a in arts)
        if page % 25 == 0:
            print(section, page, oldest, flush=True)
        if oldest < STOP:
            print(section, "reached", oldest, "at page", page, flush=True); break
        page += 1
        time.sleep(1.5)
print("done", flush=True)
