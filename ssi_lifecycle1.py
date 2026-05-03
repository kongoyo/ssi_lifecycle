import yaml
import logging
import glob
import os
import re
import threading
import random
import time
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright, Page
from bs4 import BeautifulSoup
from datetime import datetime

# 初始化日誌系統
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class IBMLifecycleParallelSRE:
    def __init__(self, max_workers: int=3):
        self.all_results = []
        self.lock = threading.Lock()
        self.max_workers = max_workers
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        ]

    def format_model_standard(self, model: str):
        clean = str(model).replace("-", "").upper()
        if len(clean) == 7:
            return f"{clean[:4]}-{clean[4:]}"
        return model

    def normalize_date(self, date_str):
        """ 增強的日期規範化 """
        if not date_str or date_str in ["-", "N/A", "None", "Table Miss", "Timeout", ""]:
            return "N/A"
        
        date_str = str(date_str).strip()
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return date_str
        
        try:
            clean_date = re.sub(r'\s+', ' ', date_str).replace(',', '').strip()
            formats = ["%Y-%m-%d", "%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y", "%Y/%m/%d", "%d-%b-%Y"]
            for fmt in formats:
                try:
                    dt = datetime.strptime(clean_date, fmt)
                    return dt.strftime('%Y-%m-%d')
                except ValueError: continue
        except Exception: pass
        return date_str

    def parse_table(self, html_content, target_model):
        """ 針對 IBM Sales Manuals 的表格解析 """
        soup = BeautifulSoup(html_content, 'html.parser')
        def norm(v): return re.sub(r'[^A-Z0-9]', '', str(v).upper())
        target_clean = norm(target_model)
        
        final_res = {"Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}
        has_valid_date = False

        for table in soup.find_all('table'):
            rows = table.find_all('tr')
            if len(rows) < 2: continue
            
            headers = [cell.get_text(separator=' ', strip=True).lower() for cell in rows[0].find_all(['th', 'td'])]
            idx = {'model': -1, 'ann': -1, 'ava': -1, 'wit': -1, 'dis': -1}
            for i, h in enumerate(headers):
                if any(k in h for k in ['model', 'type', 'mtm']): idx['model'] = i
                elif 'announced' in h: idx['ann'] = i
                elif 'available' in h: idx['ava'] = i
                elif 'withdrawn' in h: idx['wit'] = i
                elif 'discontinued' in h or 'service' in h: idx['dis'] = i
            
            for tr in rows[1:]:
                cols = [cell.get_text(separator=' ', strip=True) for cell in tr.find_all(['th', 'td'])]
                if not cols: continue
                
                is_match = False
                if idx['model'] != -1 and idx['model'] < len(cols):
                    cell_clean = norm(cols[idx['model']])
                    if target_clean in cell_clean or cell_clean in target_clean: is_match = True
                
                if is_match:
                    for k, field in [('ann', 'Announced'), ('ava', 'Available'), ('wit', 'Withdrawn'), ('dis', 'Discontinued')]:
                        if idx[k] != -1 and idx[k] < len(cols):
                            val = self.normalize_date(cols[idx[k]])
                            if val != "N/A":
                                final_res[field] = val
                                has_valid_date = True
                    if has_valid_date: return final_res
        return final_res if has_valid_date else None

    def parse_text_fallback(self, html_content, target_model):
        """ 文字回退解析 """
        soup = BeautifulSoup(html_content, 'html.parser')
        text = " ".join(soup.get_text(separator=' ', strip=True).split())
        patterns = {
            "Announced": [r"Announcement\s+date[:\s]+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", r"Announced[:\s]+(\d{4}-\d{2}-\d{2})"],
            "Available": [r"Planned\s+availability[:\s]+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", r"Available[:\s]+(\d{4}-\d{2}-\d{2})"],
            "Withdrawn": [r"Withdrawn from Market[:\s]+(\d{4}-\d{2}-\d{2})", r"Withdrawal[:\s]+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})"],
            "Discontinued": [r"End of Support[:\s]+(\d{4}-\d{2}-\d{2})", r"Service\s+[Dd]iscontinued[:\s]+(\d{4}-\d{2}-\d{2})"]
        }
        res = {"Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}
        found = False
        for key, p_list in patterns.items():
            for p in p_list:
                match = re.search(p, text, re.IGNORECASE)
                if match:
                    res[key] = self.normalize_date(match.group(1))
                    found = True
                    break
        return res if found else None

    def _fetch_tier1_sales_manual(self, page, model):
        """ Tier 1: Sales Manual Search """
        try:
            search_url = f"https://www.ibm.com/docs/en/search/Family%20{model}+?type=salesmanual"
            page.goto(search_url, wait_until="load", timeout=60000)
            target_selector = "a[href*='/announcements/']"
            page.wait_for_selector(target_selector, state="visible", timeout=15000)
            
            link_locator = page.locator(target_selector).first
            target_url = link_locator.get_attribute("href")
            if not target_url.startswith("http"):
                target_url = f"https://www.ibm.com{target_url}"
            
            page.goto(target_url, wait_until="load", timeout=60000)
            html = page.content()
            data = self.parse_table(html, model)
            if not data:
                data = self.parse_text_fallback(html, model)
            return data
        except Exception as e:
            logger.debug(f"[Tier1] {model} 失敗: {str(e)}")
            return None

    def _fetch_tier2_support_lifecycle(self, page, model):
        """ Tier 2: Support Lifecycle Search """
        res = {"Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}
        try:
            search_url = f"https://www.ibm.com/support/pages/lifecycle/search?q={model}"
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            
            # 抓取搜尋結果表格
            try:
                table_selector = "table#plc--search-results-table"
                page.wait_for_selector(table_selector, timeout=15000)
                rows = page.locator(f"{table_selector} tbody tr")
                for i in range(rows.count()):
                    row_text = rows.nth(i).inner_text().upper()
                    if model.replace("-","").upper() in row_text.replace("-",""):
                        cols = rows.nth(i).locator("td")
                        res["Announced"] = self.normalize_date(cols.nth(1).inner_text())
                        res["Available"] = res["Announced"]
                        res["Discontinued"] = self.normalize_date(cols.nth(2).inner_text())
                        break
            except: pass

            # 進入詳情頁抓取 EOM
            try:
                detail_link = page.locator("a#search-result-0")
                if detail_link.count() > 0:
                    detail_link.click()
                    page.wait_for_load_state("networkidle", timeout=20000)
                    content = page.content()
                    eom_match = re.search(r'(Withdrawn from Market|Withdrawal from Marketing)[:\s]+(\d{4}-\d{2}-\d{2}|\d{1,2}-[A-Za-z]{3}-\d{4})', content)
                    if eom_match:
                        res["Withdrawn"] = self.normalize_date(eom_match.group(2))
            except: pass
            
            return res if any(v != "N/A" for v in res.values()) else None
        except Exception as e:
            logger.debug(f"[Tier2] {model} 失敗: {str(e)}")
            return None

    def fetch_model_data(self, model, title):
        final_res = {"Model": str(model), "Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}
        
        with sync_playwright() as p:
            browser = None
            try:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=random.choice(self.user_agents))
                page = context.new_page()
                page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff2}", lambda r: r.abort())

                # Tier 1
                logger.info(f"[{model}] Tier 1 搜尋中...")
                t1_data = self._fetch_tier1_sales_manual(page, model)
                if t1_data:
                    final_res.update(t1_data)
                
                # Check for Fallback
                missing = any(final_res[k] == "N/A" for k in ["Announced", "Withdrawn", "Discontinued"])
                if missing:
                    logger.info(f"[{model}] 觸發 Tier 2 補位...")
                    t2_data = self._fetch_tier2_support_lifecycle(page, model)
                    if t2_data:
                        for k in ["Announced", "Available", "Withdrawn", "Discontinued"]:
                            if t2_data[k] != "N/A":
                                final_res[k] = t2_data[k]

            except Exception as e:
                logger.error(f"[{model}] 處理異常: {str(e)}")
            finally:
                if browser: browser.close()
        return final_res

    def run(self):
        yaml_files = sorted(glob.glob('*.yaml'))
        for i, yaml_file in enumerate(yaml_files):
            clean_title = os.path.splitext(yaml_file)[0].replace('_', ' ')
            logger.info(f"== 啟動處理類別: {clean_title} ==")
            
            with open(yaml_file, 'r', encoding='utf-8') as f:
                raw_models = yaml.safe_load(f).get('models', [])
            
            groups = []
            current_group_name = "Uncategorized"
            current_models = []
            for m in raw_models:
                m_str = str(m)
                if "-" not in m_str and (m_str.startswith("Power") or len(m_str) < 10):
                    if current_models:
                        groups.append((current_group_name, current_models))
                        current_models = []
                    current_group_name = m_str
                else: current_models.append(m_str)
            if current_models: groups.append((current_group_name, current_models))

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                final_grouped_results = []
                for group_name, models in groups:
                    future_to_model = {executor.submit(self.fetch_model_data, m, clean_title): m for m in models}
                    group_results = [future.result() for future in as_completed(future_to_model)]
                    group_results.sort(key=lambda x: str(x['Model'])[:4])
                    final_grouped_results.append((group_name, group_results))
            
            self.display(clean_title, final_grouped_results)
            self.write_to_readme(clean_title, final_grouped_results, i == 0)

    def write_to_readme(self, title, grouped_results, is_first_file):
        md_content = ""
        for group_name, results in grouped_results:
            md_content += f"## {group_name}\n\n"
            md_content += "| Model | Announced | Available | Withdrawn | Discontinued |\n"
            md_content += "|-------|-----------|-----------|-----------|--------------|\n"
            for r in results:
                md_content += f"| {r['Model']} | {r['Announced']} | {r['Available']} | {r['Withdrawn']} | {r['Discontinued']} |\n"
            md_content += "\n"

        mode = 'w' if is_first_file else 'a'
        with open('README.md', mode, encoding='utf-8') as f:
            if is_first_file:
                f.write("# IBM Hardware Lifecycle Dates\n\n")
                f.write(f"> Last Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(md_content)
        logger.info(f"結果已更新至 README.md")

    def display(self, title, grouped_results):
        print(f"\n# {title}\n")
        for group_name, results in grouped_results:
            print(f"## {group_name}")
            if not results: continue
            header = f"{'Model':<12} | {'Announced':<12} | {'Available':<12} | {'Withdrawn':<12} | {'Discontinued'}"
            print("-" * len(header))
            print(header)
            print("-" * len(header))
            for r in results:
                print(f"{r['Model']:<12} | {r['Announced']:<12} | {r['Available']:<12} | {r['Withdrawn']:<12} | {r['Discontinued']}")

if __name__ == "__main__":
    lifecycle = IBMLifecycleParallelSRE(max_workers=3)
    lifecycle.run()