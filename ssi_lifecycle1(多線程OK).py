import yaml
import logging
import glob
import os
import re
import threading
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from datetime import datetime

# 初始化日誌系統
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class IBMLifecycleParallelSRE:
    def __init__(self, max_workers=3):
        self.all_results = []
        self.lock = threading.Lock()  # 確保寫入 results 時的執行緒安全
        self.max_workers = max_workers

    def normalize_date(self, date_str):
        """ SRE 數據規範化邏輯 (維持原邏輯) """
        if not date_str or date_str in ["-", "N/A", "None", "Table Miss", "Timeout"]:
            return date_str
        date_str = date_str.strip()
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return date_str
        try:
            clean_date = re.sub(r'\s+', ' ', date_str).replace(',', '')
            formats = ["%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y", "%Y-%m-%d"]
            for fmt in formats:
                try:
                    dt = datetime.strptime(clean_date, fmt)
                    return dt.strftime('%Y-%m-%d')
                except ValueError: continue
        except Exception: pass
        return date_str

    def parse_table(self, html_content, target_model):
        """ 表格解析邏輯 (維持原邏輯) """
        soup = BeautifulSoup(html_content, 'html.parser')
        target_model_clean = target_model.replace("-", "").upper()
        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if not any('model' in h for h in headers): continue
            idx = {'model': -1, 'announced': -1, 'available': -1, 'withdrawn': -1, 'discontinued': -1}
            for i, h in enumerate(headers):
                if 'model' in h: idx['model'] = i
                elif 'announced' in h: idx['announced'] = i
                elif 'available' in h: idx['available'] = i
                elif 'withdrawn' in h: idx['withdrawn'] = i
                elif 'discontinued' in h: idx['discontinued'] = i

            for tr in table.find_all('tr')[1:]:
                cols = [td.get_text(strip=True) for td in tr.find_all('td')]
                if len(cols) > idx['model']:
                    cell_model = cols[idx['model']].replace("-", "").upper()
                    if target_model_clean in cell_model or cell_model in target_model_clean:
                        return {
                            "Announced": self.normalize_date(cols[idx['announced']]) if idx['announced'] < len(cols) else "-",
                            "Available": self.normalize_date(cols[idx['available']]) if idx['available'] < len(cols) else "-",
                            "Withdrawn": self.normalize_date(cols[idx['withdrawn']]) if idx['withdrawn'] < len(cols) else "-",
                            "Discontinued": self.normalize_date(cols[idx['discontinued']]) if idx['discontinued'] < len(cols) else "-"
                        }
        return None

    def fetch_model_data(self, model, title):
        # 確保 model 鍵值為字串
        res = {"Model": str(model), "Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}        
        # 在多執行緒環境中，每個 thread 必須開啟獨立的 Playwright 實例
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent="IBM-SRE-Parallel-Bot/2.0")
                page = context.new_page()
                page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff2}", lambda route: route.abort())

                search_url = f"https://www.ibm.com/docs/en/search/Family%20{model}+?type=salesmanual"
                logger.info(f"[Thread-{threading.get_ident()}] 正在搜尋: {model}")
                
                page.goto(search_url, wait_until="domcontentloaded", timeout=50000)
                
                try:
                    link_locator = page.locator("a[href*='/announcements/']").first
                    link_locator.wait_for(state="attached", timeout=15000)
                    announce_url = link_locator.get_attribute("href")
                    if not announce_url.startswith("http"):
                        announce_url = f"https://www.ibm.com{announce_url}"
                    
                    page.goto(announce_url, wait_until="domcontentloaded", timeout=50000)
                    page.wait_for_selector("table", timeout=20000)
                    
                    data = self.parse_table(page.content(), model)
                    if data:
                        res.update(data)
                    else:
                        res["Announced"] = "Table Miss"
                except:
                    res["Announced"] = "Timeout"
                
                browser.close()
            except Exception as e:
                logger.error(f"{model} 處理異常: {str(e)}")
        return res

    def run(self):
        yaml_files = sorted(glob.glob('*.yaml'))
        for i, yaml_file in enumerate(yaml_files):
            clean_title = os.path.splitext(yaml_file)[0].replace('_', ' ')
            logger.info(f"== 啟動平行處理類別: {clean_title} ==")
            
            with open(yaml_file, 'r', encoding='utf-8') as f:
                # 關鍵修正：讀取後立即將所有型號強制轉為字串
                raw_models = yaml.safe_load(f).get('models', [])
                models = [str(m) for m in raw_models] 

            current_file_results = []
            
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # 這裡傳入的 model 已經保證是字串
                future_to_model = {executor.submit(self.fetch_model_data, model, clean_title): model for model in models}
                
                for future in as_completed(future_to_model):
                    model_data = future.result()
                    current_file_results.append(model_data)
                    
            # 關鍵修正：排序時再次確保比較對象皆為字串，防止 fetch_model_data 回傳異常類型
            current_file_results.sort(key=lambda x: str(x.get('Model', '')))
            
            self.display(clean_title, current_file_results)
            self.write_to_readme(clean_title, current_file_results, i == 0)

    def write_to_readme(self, title, results, is_first_file):
        headers = ["Model", "Announced", "Available", "Withdrawn", "Discontinued"]
        col_widths = {h: max([len(str(r.get(h, '-'))) for r in results] + [len(h)]) for h in headers}

        md_table = f"## {title}\n\n"
        md_table += "| " + " | ".join([f"{h:<{col_widths[h]}}" for h in headers]) + " |\n"
        md_table += "|-" + "-|-".join(["-" * col_widths[h] for h in headers]) + "-|\n"

        for r in results:
            md_table += "| " + " | ".join([f"{str(r.get(h, '-')):<{col_widths[h]}}" for h in headers]) + " |\n"
        md_table += "\n"

        mode = 'w' if is_first_file else 'a'
        with open('README.md', mode, encoding='utf-8') as f:
            if is_first_file: f.write("# IBM Hardware Lifecycle (Parallel)\n\n")
            f.write(md_table)
        logger.info(f"結果已更新至 README.md ({title})")

    def display(self, title, results):
        print(f"\n## {title} (Parallel Mode)\n")
        header_str = f"{'Model':<12} | {'Announced':<12} | {'Available':<12} | {'Withdrawn':<12} | {'Discontinued'}"
        print("=" * len(header_str))
        print(header_str)
        print("-" * len(header_str))
        for r in results:
            print(f"{r['Model']:<12} | {r.get('Announced','-'):<12} | {r.get('Available','-'):<12} | {r.get('Withdrawn','-'):<12} | {r.get('Discontinued','-')}")
        print("=" * len(header_str))

if __name__ == "__main__":
    # max_workers 建議設為 3-5，避免被 IBM WAF 封鎖 IP
    lifecycle = IBMLifecycleParallelSRE(max_workers=3)
    lifecycle.run()