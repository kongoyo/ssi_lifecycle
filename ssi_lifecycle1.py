import yaml
import logging
import glob
import os
import re
import threading
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright, Page
from bs4 import BeautifulSoup
from datetime import datetime

# 初始化日誌系統
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class IBMLifecycleParallelSRE:
    def __init__(self, max_workers: int=2):
        self.all_results = []
        self.lock = threading.Lock()
        self.max_workers = max_workers

    def format_model_standard(self, model: str):
        clean = str(model).replace("-", "").upper()
        if len(clean) == 7:
            return f"{clean[:4]}-{clean[4:]}"
        return model

    def _execute_scraping_flow(self, page , model: str):
        try:
            search_url = f"https://www.ibm.com/docs/en/search/Family%20{str(model)}+?type=salesmanual"
            page.goto(search_url, wait_until="load", timeout=90000)
            # target_selector = "a[href*='/announcements/']"
            target_selector = "a[href*='/announcements/']"
            try:
                page.wait_for_selector(target_selector, state="visible", timeout=15000)
            except:
                logger.warning(f"[{model}] 搜尋結果清單超時未出現")
                return None

            link_locator = page.locator(target_selector).first
            if link_locator.count() == 0: return None
            
            target_url = link_locator.get_attribute("href")
            print(target_url)
            if not target_url.startswith("http"):
                target_url = f"https://www.ibm.com{target_url}"
            
            logger.info(f"[{model}] 成功定位公告頁面: {target_url}")
            
            # === 修正 2: 公告頁面也延長超時 ===
            page.goto(target_url, wait_until="load", timeout=90000)
            
            # === 修正 3: 等待特定的 IBM Carbon Design 表格 ===
            try:
                page.wait_for_selector("table.cds--data-table", timeout=50000)
                logger.info(f"[{model}] 表格已載入")
            except:
                logger.warning(f"[{model}] 表格載入超時,嘗試繼續解析")

            html = page.content()
            data = self.parse_table(html, model)
            
            if not data:
                logger.warning(f"[{model}] 表格解析失敗,嘗試文字解析")
                data = self.parse_text_fallback(html, model)
            else:
                logger.info(f"[{model}] 成功解析: {data}")
            
            return data
        except Exception as e:
            logger.error(f"[{model}] Flow 執行異常: {str(e)}")
            return None

    def normalize_date(self, date_str):
        """ 增強的日期規範化 """
        if not date_str or date_str in ["-", "N/A", "None", "Table Miss", "Timeout", ""]:
            return "N/A"
        
        date_str = str(date_str).strip()
        
        # 已經是標準格式 (IBM 表格直接提供 YYYY-MM-DD)
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return date_str
        
        try:
            clean_date = re.sub(r'\s+', ' ', date_str).replace(',', '').strip()
            
            formats = [
                "%Y-%m-%d",      # 2011-10-21 (IBM 標準格式)
                "%d %B %Y",      # 21 October 2011
                "%B %d %Y",      # October 21 2011
                "%d %b %Y",      # 21 Oct 2011
                "%b %d %Y",      # Oct 21 2011
                "%Y/%m/%d",      # 2011/10/21
            ]
            
            for fmt in formats:
                try:
                    dt = datetime.strptime(clean_date, fmt)
                    return dt.strftime('%Y-%m-%d')
                except ValueError:
                    continue
        except Exception as e:
            logger.debug(f"日期轉換失敗: {date_str} - {e}")
        
        return date_str

    def parse_table(self, html_content, target_model):
        """
        針對 IBM Carbon Design System 表格的專用解析器
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        
        def norm(v): 
            return re.sub(r'[^A-Z0-9]', '', str(v).upper())
        
        target_clean = norm(target_model)
        
        final_res = {
            "Announced": "N/A", 
            "Available": "N/A", 
            "Withdrawn": "N/A", 
            "Discontinued": "N/A"
        }
        has_valid_date = False

        # === 修正 4: 針對 IBM Carbon Design 表格結構 ===
        # 尋找所有表格,包括 class="cds--data-table" 的表格
        tables = soup.find_all('table')
        
        for table_idx, table in enumerate(tables):
            rows = table.find_all('tr')
            if len(rows) < 2: 
                continue
            
            # === 修正 5: 從 <div class="cds--table-header-label"> 提取表頭 ===
            header_row = rows[0]
            headers = []
            for cell in header_row.find_all(['th', 'td']):
                # 優先從 div 中提取文字
                label_div = cell.find('div', class_='cds--table-header-label')
                if label_div:
                    text = label_div.get_text(strip=True).lower()
                else:
                    text = cell.get_text(separator=' ', strip=True).lower()
                headers.append(text)
            
            # 欄位索引映射
            idx = {'model': -1, 'ann': -1, 'ava': -1, 'wit': -1, 'dis': -1}
            
            for i, h in enumerate(headers):
                if any(k in h for k in ['model', 'type', 'mtm', 'machine']):
                    idx['model'] = i
                elif any(k in h for k in ['announced', 'announce']):
                    idx['ann'] = i
                elif any(k in h for k in ['available', 'availability']):
                    idx['ava'] = i
                elif any(k in h for k in ['withdrawn', 'marketing']):
                    idx['wit'] = i
                elif any(k in h for k in ['discontinued', 'discontin', 'service']):
                    idx['dis'] = i
            
            # 遍歷資料行
            for row_idx, tr in enumerate(rows[1:], start=1):
                cols = [cell.get_text(separator=' ', strip=True) 
                       for cell in tr.find_all(['th', 'td'])]
                
                if not cols:
                    continue
                
                # 判斷是否匹配目標型號
                is_match = False
                
                # 策略 A: 精確匹配 model 欄位
                if idx['model'] != -1 and idx['model'] < len(cols):
                    cell_clean = norm(cols[idx['model']])
                    if target_clean == cell_clean:
                        is_match = True
                        logger.info(f"[{target_model}] ✓ 精確匹配: {cols[idx['model']]}")
                
                # 策略 B: 部分匹配
                if not is_match and idx['model'] != -1 and idx['model'] < len(cols):
                    cell_clean = norm(cols[idx['model']])
                    if target_clean in cell_clean or cell_clean in target_clean:
                        is_match = True
                        logger.info(f"[{target_model}] ✓ 部分匹配: {cols[idx['model']]}")
                
                # 策略 C: 全行掃描
                if not is_match:
                    full_row = norm("".join(cols))
                    if target_clean in full_row:
                        is_match = True
                        logger.info(f"[{target_model}] ✓ 全行匹配")
                
                if is_match:
                    # 提取日期
                    if idx['ann'] != -1 and idx['ann'] < len(cols):
                        val = self.normalize_date(cols[idx['ann']])
                        if val != "N/A":
                            final_res["Announced"] = val
                            has_valid_date = True
                    
                    if idx['ava'] != -1 and idx['ava'] < len(cols):
                        val = self.normalize_date(cols[idx['ava']])
                        if val != "N/A":
                            final_res["Available"] = val
                            has_valid_date = True
                    
                    if idx['wit'] != -1 and idx['wit'] < len(cols):
                        val = self.normalize_date(cols[idx['wit']])
                        if val != "N/A":
                            final_res["Withdrawn"] = val
                            has_valid_date = True
                    
                    if idx['dis'] != -1 and idx['dis'] < len(cols):
                        val = self.normalize_date(cols[idx['dis']])
                        if val != "N/A":
                            final_res["Discontinued"] = val
                            has_valid_date = True
                    
                    # 找到匹配後可以提前返回
                    if has_valid_date:
                        return final_res
        
        return final_res if has_valid_date else None

    def parse_text_fallback(self, html_content, target_model):
        """
        文字回退解析
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        text = " ".join(soup.get_text(separator=' ', strip=True).split())
        
        patterns = {
            "Announced": [
                r"Announcement\s+date[:\s]+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
                r"dated\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
                r"Announced[:\s]+(\d{4}-\d{2}-\d{2})",
            ],
            "Available": [
                r"Planned\s+availability[:\s]+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
                r"Available[:\s]+(\d{4}-\d{2}-\d{2})",
            ],
            "Withdrawn": [
                r"Marketing\s+[Ww]ithdrawn[:\s]+(\d{4}-\d{2}-\d{2})",
                r"Withdrawal[:\s]+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
            ],
            "Discontinued": [
                r"Service\s+[Dd]iscontinued[:\s]+(\d{4}-\d{2}-\d{2})",
                r"discontinuance[:\s]+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
            ]
        }
        
        res = {
            "Announced": "N/A", 
            "Available": "N/A", 
            "Withdrawn": "N/A", 
            "Discontinued": "N/A"
        }
        
        for key, p_list in patterns.items():
            for p in p_list:
                match = re.search(p, text, re.IGNORECASE)
                if match:
                    res[key] = self.normalize_date(match.group(1))
                    break
        
        return res if any(v != "N/A" for v in res.values()) else None

    def fetch_model_data(self, model, title):
        current_model = str(model)
        is_retried = False
        final_res = {
            "Model": current_model, 
            "Announced": "N/A", 
            "Available": "N/A", 
            "Withdrawn": "N/A", 
            "Discontinued": "N/A"
        }

        for attempt in [1, 2]:
            with sync_playwright() as p:
                browser = None
                try:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                    page = context.new_page()
                    
                    # === 修正 6: 禁用不必要的資源載入 ===
                    page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2}", lambda r: r.abort())

                    data = self._execute_scraping_flow(page, current_model)

                    if data and data.get("Announced") not in ["N/A", "Full Miss"]:
                        final_res.update(data)
                        if is_retried:
                            final_res["Model"] = f"{model} (Retry)"
                        return final_res

                    if attempt == 1:
                        formatted = self.format_model_standard(current_model)
                        if formatted != current_model:
                            logger.warning(f"型號 {current_model} 查無資料,嘗試修正為 {formatted} 重試...")
                            current_model = formatted
                            is_retried = True
                            continue
                    
                    final_res["Announced"] = "Full Miss"
                    
                except Exception as e:
                    logger.error(f"{current_model} 處理異常: {str(e)}")
                    final_res["Announced"] = "Error"
                finally:
                    if browser: 
                        browser.close()
        
        return final_res

    def run(self):
        yaml_files = sorted(glob.glob('*.yaml'))
        for i, yaml_file in enumerate(yaml_files):
            clean_title = os.path.splitext(yaml_file)[0].replace('_', ' ')
            logger.info(f"== 啟動平行處理類別: {clean_title} ==")
            
            with open(yaml_file, 'r', encoding='utf-8') as f:
                raw_models = yaml.safe_load(f).get('models', [])
                models = [str(m) for m in raw_models]

            current_file_results = []
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_model = {
                    executor.submit(self.fetch_model_data, m, clean_title): m 
                    for m in models
                }
                for future in as_completed(future_to_model):
                    current_file_results.append(future.result())
            
            current_file_results.sort(key=lambda x: str(x['Model']).replace(" (Retry)", ""))
            self.display(clean_title, current_file_results)
            self.write_to_readme(clean_title, current_file_results, i == 0)

    def write_to_readme(self, title, results, is_first_file):
        """ 仿照 kongoyo/ssi-life-cycle-dates 的極簡 Markdown 格式 """
        
        # 1. 數據清洗：輸出前將 N/A 或 Full Miss 轉為 '-' 增加視覺整潔度
        clean_results = []
        for r in results:
            row = {
                "Model": r.get("Model", "-"),
                "Announced": r.get("Announced") if r.get("Announced") not in ["N/A", "Full Miss", "Error"] else "-",
                "Available": r.get("Available") if r.get("Available") not in ["N/A", "Full Miss", "Error"] else "-",
                "Withdrawn": r.get("Withdrawn") if r.get("Withdrawn") not in ["N/A", "Full Miss", "Error"] else "-",
                "Discontinued": r.get("Discontinued") if r.get("Discontinued") not in ["N/A", "Full Miss", "Error"] else "-"
            }
            clean_results.append(row)

        # 2. 構建表格字串 (緊湊型，無多餘空格)
        md_table = f"## {title}\n\n"
        md_table += "| Model | Announced | Available | Withdrawn | Discontinued |\n"
        md_table += "|-------|-----------|-----------|-----------|--------------|\n"

        for r in clean_results:
            md_table += f"| {r['Model']} | {r['Announced']} | {r['Available']} | {r['Withdrawn']} | {r['Discontinued']} |\n"
        md_table += "\n"

        # 3. 檔案寫入邏輯
        mode = 'w' if is_first_file else 'a'
        with open('readme.md', mode, encoding='utf-8') as f:
            if is_first_file:
                # 寫入主標題與生成日期 (符合 SRE 規範)
                f.write("# IBM Hardware Lifecycle Dates\n\n")
                f.write(f"> Last Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(md_table)
            
        logger.info(f"報表格式已更新至 readme.md ({title})")

    def display(self, title, results):
        print(f"\n## {title} (Parallel Mode)\n")
        m_width = max([len(str(r['Model'])) for r in results] + [12])
        header_str = f"{'Model':<{m_width}} | {'Announced':<12} | {'Available':<12} | {'Withdrawn':<12} | {'Discontinued'}"
        
        print("=" * len(header_str))
        print(header_str)
        print("-" * len(header_str))
        for r in results:
            m = f"{r['Model']:<{m_width}}"
            ann = f"{r.get('Announced','-'):<12}"
            ava = f"{r.get('Available','-'):<12}"
            wit = f"{r.get('Withdrawn','-'):<12}"
            dis = f"{r.get('Discontinued','-')}"
            print(f"{m} | {ann} | {ava} | {wit} | {dis}")
        print("=" * len(header_str))

if __name__ == "__main__":
    lifecycle = IBMLifecycleParallelSRE(max_workers=3)
    lifecycle.run()