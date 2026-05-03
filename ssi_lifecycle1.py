import requests
import re
import yaml
import logging
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class IBMLifecycleParallelSRE:
    def __init__(self, config_path='models.yaml'):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        self.base_url = "https://www.ibm.com/common/ssi/ShowDoc.wss?docURL=/common/ssi/rep_sm/s/{model_short}/index.html"

    def normalize_date(self, date_str):
        if not date_str or date_str.upper() == "N/A": return "N/A"
        # 徹底封殺已知的全局腳註錯誤日期
        if "2020-01-15" in date_str: return "N/A"
        
        date_str = date_str.strip()
        months = {"JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
                  "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"}
        m = re.match(r'(\d{1,2})-([A-Za-z]{3})-(\d{4})', date_str)
        if m:
            day, mon, year = m.groups()
            return f"{year}-{months.get(mon.upper(), '01')}-{day.zfill(2)}"
        m_iso = re.search(r'(\d{4}-\d{2}-\d{2})', date_str)
        return m_iso.group(1) if m_iso else "N/A"

    def fetch_tier1_sales_manual(self, model):
        try:
            model_short = model.split('-')[1] if '-' in model else model
            url = self.base_url.format(model_short=model_short)
            response = requests.get(url, timeout=10)
            if response.status_code != 200: return None
            html = response.text
            data = {"Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}
            m_ga = re.search(r'Planned Availability Date:.*?(\d{4}-\d{2}-\d{2})', html, re.S)
            if m_ga: data["Announced"] = data["Available"] = m_ga.group(1)
            m_wd = re.search(r'Withdrawal from Marketing:.*?(\d{4}-\d{2}-\d{2})', html, re.S)
            if m_wd: data["Withdrawn"] = m_wd.group(1)
            m_eos = re.search(r'Service Discontinued:.*?(\d{4}-\d{2}-\d{2})', html, re.S)
            if m_eos: data["Discontinued"] = m_eos.group(1)
            return data if any(v != "N/A" for v in data.values()) else None
        except: return None

    def _fetch_tier2_support_lifecycle(self, page, model):
        """ Tier 2: Definitive precision scraper """
        res = {"Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}
        try:
            search_url = f"https://www.ibm.com/support/pages/lifecycle/search?q={model}"
            page.goto(search_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(5000)
            
            rows = page.locator("table#plc--search-results-table tbody tr").all()
            m_parts = re.findall(r'[A-Z0-9]+', model.upper())
            
            for row in rows:
                cells = row.locator("td").all()
                row_text = row.inner_text().upper().replace("-","")
                
                # 型號匹配
                if all(p in row_text for p in m_parts):
                    # 1. 表格優先 (Index 2=GA, Index 3=EOS)
                    if len(cells) >= 4:
                        res["Announced"] = self.normalize_date(cells[2].inner_text())
                        res["Available"] = res["Announced"]
                        res["Discontinued"] = self.normalize_date(cells[3].inner_text())
                    
                    # 2. 只有在表格抓不到時才去詳情頁抓 Withdrawn/EOS
                    try:
                        link = row.locator("a").first
                        link.click()
                        page.wait_for_load_state("networkidle", timeout=15000)
                        page.wait_for_timeout(3000)
                        detail = page.content()
                        
                        # 擷取 Withdrawn (表格中沒有)
                        m_wd = re.search(r'(No longer available for order|Withdrawn from Market|Marketing Withdrawal).*?(\d{4}-\d{2}-\d{2}|\d{1,2}-[A-Z]{3}-\d{4})', detail, re.I | re.S)
                        if m_wd: res["Withdrawn"] = self.normalize_date(m_wd.group(2))
                        
                        # 擷取 EOS (如果表格裡是空的)
                        if res["Discontinued"] == "N/A":
                            m_eos = re.search(r'(Transition to End of Support Services|End of Support|Service Discontinued).*?(\d{4}-\d{2}-\d{2}|\d{1,2}-[A-Z]{3}-\d{4})', detail, re.I | re.S)
                            if m_eos: res["Discontinued"] = self.normalize_date(m_eos.group(2))
                    except: pass
                    
                    return res
            return None
        except: return None

    def fetch_model_data(self, model):
        res = {"Model": str(model), "Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}
        t1 = self.fetch_tier1_sales_manual(model)
        if t1:
            for k in t1: res[k] = t1[k]
        
        if res["Announced"] == "N/A" or res["Discontinued"] == "N/A":
            logger.info(f"[{model}] 執行 Tier 2...")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_context().new_page()
                t2 = self._fetch_tier2_support_lifecycle(page, model)
                browser.close()
                if t2:
                    for k in t2:
                        if t2[k] != "N/A": res[k] = t2[k]
                    logger.info(f"[{model}] 資料已補全")
        return res

    def run(self):
        category_results = {}
        for category, models in self.config.items():
            logger.info(f"== 處理 {category} ==")
            with ThreadPoolExecutor(max_workers=3) as executor:
                results = list(executor.map(self.fetch_model_data, models))
                category_results[category] = results
        self.generate_report(category_results)

    def generate_report(self, category_results):
        with open('readme.md', 'w', encoding='utf-8') as f:
            f.write("# IBM Hardware Lifecycle Report\n\n")
            for cat, results in category_results.items():
                f.write(f"## {cat}\n\n")
                f.write("| Model | Announced | Available | Withdrawn | Discontinued |\n")
                f.write("|-------|-----------|-----------|-----------|--------------|\n")
                for r in results:
                    f.write(f"| {r['Model']} | {r['Announced']} | {r['Available']} | {r['Withdrawn']} | {r['Discontinued']} |\n")
                f.write("\n")

if __name__ == "__main__":
    sre = IBMLifecycleParallelSRE()
    sre.run()