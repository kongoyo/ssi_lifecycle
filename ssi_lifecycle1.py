import yaml, logging, glob, os, re
from urllib.parse import quote
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class IBMLifecycleUniversalSRE:
    def __init__(self):
        self.results = []
        self.col_map = {'announced': 'Announced', 'available': 'Available', 'withdrawn': 'Withdrawn', 'discontinued': 'Discontinued'}

    def normalize_date(self, date_str):
        if not date_str or any(x in date_str for x in ["-", "N/A", "Miss", "Timeout"]): return date_str
        date_str = re.sub(r'\s+', ' ', date_str.strip()).replace(',', '')
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str): return date_str
        
        fmts = ["%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y", "%Y-%m-%d"]
        for fmt in fmts:
            try: return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
            except ValueError: continue
        return date_str

    def parse_table(self, html, target_model):
        soup = BeautifulSoup(html, 'html.parser')
        target = target_model.replace("-", "").upper()
        
        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if 'model' not in headers: continue
            
            idx = {k: headers.index(next(h for h in headers if k in h)) for k in ['model'] + list(self.col_map.keys()) if any(k in h for h in headers)}
            
            for tr in table.find_all('tr')[1:]:
                cols = [td.get_text(strip=True) for td in tr.find_all('td')]
                if len(cols) > idx.get('model', -1):
                    if target in cols[idx['model']].replace("-", "").upper():
                        return {v: self.normalize_date(cols[idx[k]]) if k in idx and idx[k] < len(cols) else "-" for k, v in self.col_map.items()}
        return None

    def run_for_file(self, config_path, title, is_first):
        self.results = []
        with open(config_path, 'r', encoding='utf-8') as f:
            models = yaml.safe_load(f).get('models', [])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0 SRE-Bot/1.0")
            page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff2}", lambda r: r.abort())

            for model in models:
                res = {"Model": model, "Announced": "N/A", "Available": "-", "Withdrawn": "-", "Discontinued": "-"}
                try:
                    logger.info(f"搜尋 {model} (來自 {title})")
                    page.goto(f"https://www.ibm.com/docs/en/search/Family%20{quote(model)}%2B?type=salesmanual", wait_until="domcontentloaded", timeout=50000)
                    
                    link = page.locator("a[href*='/announcements/']").first
                    link.wait_for(state="attached", timeout=15000)
                    url = link.get_attribute("href")
                    url = url if url.startswith("http") else f"https://www.ibm.com{url}"
                    
                    page.goto(url, wait_until="domcontentloaded", timeout=50000)
                    page.wait_for_selector("table", timeout=20000)
                    
                    data = self.parse_table(page.content(), model)
                    if data: res.update(data)
                    else: res["Announced"] = "Table Miss"
                except Exception as e:
                    logger.warning(f"{model} 失敗: {str(e)[:50]}")
                    res["Announced"] = "Timeout" if "timeout" in str(e).lower() else "N/A"
                self.results.append(res)
            browser.close()
        self.write_readme(title, is_first)

    def write_readme(self, title, is_first):
        hdrs = ["Model", "Announced", "Available", "Withdrawn", "Discontinued"]
        widths = {h: max([len(str(r.get(h, ''))) for r in self.results] + [len(h)]) for h in hdrs}
        
        content = f"## {title}\n\n| " + " | ".join([f"{h:<{widths[h]}}" for h in hdrs]) + " |\n"
        content += "|-" + "-|-".join(["-" * widths[h] for h in hdrs]) + "-|\n"
        for r in self.results:
            content += "| " + " | ".join([f"{str(r.get(h, '-')):<{widths[h]}}" for h in hdrs]) + " |\n"
        
        mode, prefix = ('w', "# IBM Hardware Lifecycle\n\n") if is_first else ('a', "")
        with open('README.md', mode, encoding='utf-8') as f:
            f.write(f"{prefix}{content}\n")
        logger.info(f"已更新 README: {title}")

    def run(self):
        files = sorted(glob.glob('*.yaml'))
        for i, f in enumerate(files):
            self.run_for_file(f, os.path.splitext(f)[0].replace('_', ' '), i == 0)

if __name__ == "__main__":
    IBMLifecycleUniversalSRE().run()