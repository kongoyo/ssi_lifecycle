import yaml
import logging
import glob
import os
import re
from urllib.parse import quote
from bs4 import BeautifulSoup
from datetime import datetime
import re
from playwright.sync_api import sync_playwright

# 初始化日誌系統
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class IBMLifecycleUniversalSRE:

    def __init__(self):
        self.results = []

    def validate_dates_logic(self, extracted_data):
        """ 透過程式邏輯檢查抽取的日期是否合理 (格式與時間軸順序) """
        
        # 1. 檢查年份格式合理性 (過濾像是 1024, 2099, 或版號)
        year_pattern = re.compile(r'^20[0-9]{2}')
        valid_dates = {}
        
        for key in ['Announced', 'Available', 'Withdrawn', 'Discontinued']:
            val = extracted_data.get(key, '-')
            if val and val != '-':
                # 如果前四碼不是 20xx，當成異常
                if not year_pattern.match(val):
                    return f"❌ 異常年份格式: {key}={val}"
                try:
                    valid_dates[key] = datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    pass
        
        # 2. 檢查時間軸邏輯 (Announced <= Available <= Withdrawn <= Discontinued)
        if 'Announced' in valid_dates and 'Available' in valid_dates:
            if valid_dates['Announced'] > valid_dates['Available']:
                return "❌ 邏輯錯誤: 發布日期晚於上市日期"
                
        if 'Available' in valid_dates and 'Withdrawn' in valid_dates:
            if valid_dates['Available'] > valid_dates['Withdrawn']:
                return "❌ 邏輯錯誤: 上市日期晚於下市日期"
                
        if 'Withdrawn' in valid_dates and 'Discontinued' in valid_dates:
            if valid_dates['Withdrawn'] > valid_dates['Discontinued']:
                return "❌ 邏輯錯誤: 下市日期晚於終止支援日期"

        # 若都沒抓到有效日期
        if not valid_dates:
            return "⚠️ 無有效日期"
            
        return "✅ 本地邏輯驗證通過"

    def normalize_date(self, date_str):
        """ SRE 數據規範化：將多種日期格式統一為 YYYY-MM-DD """
        if not date_str or date_str in ["-", "N/A", "None", "Table Miss", "Timeout"]:
            return date_str
        
        date_str = date_str.strip()
        
        # 模式 1: 已經是 2024-06-30 格式
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return date_str
        
        # 模式 2: 處理自然語言日期如 "31 December 2024", "Dec 31, 2024", 或 "28-Oct-2016"
        try:
            # 清除多餘空格與特殊字元
            clean_date = re.sub(r'\s+', ' ', date_str).replace(',', '')
            
            # 嘗試多種常見的 IBM 公告日期格式 (英文語系)
            formats = [
                "%d %B %Y",  # 31 December 2024
                "%B %d %Y",  # December 31 2024
                "%d %b %Y",  # 31 Dec 2024
                "%b %d %Y",  # Dec 31 2024
                "%d-%b-%Y",  # 28-Oct-2016 (IBM Support Node format)
                "%d-%B-%Y",  # 28-October-2016
                "%Y-%m-%d"   # 2024-06-30
            ]
            
            for fmt in formats:
                try:
                    dt = datetime.strptime(clean_date, fmt)
                    return dt.strftime('%Y-%m-%d')
                except ValueError:
                    continue
        except Exception:
            pass

        return "-"  # 若解析失敗則回傳原始值，確保數據不遺失

    def parse_table(self, html_content, target_model):
        soup = BeautifulSoup(html_content, 'html.parser')
        target_model_clean = target_model.replace("-", "").upper()
        
        for table in soup.find_all('table'):
            # IBM Sales Manuals sometimes use th or td for header columns
            headers = [th.get_text(strip=True).lower() for th in table.find_all(['th', 'td'])]
            
            # 確保這是一個生命週期表格
            if not any('model' in h for h in headers) and not any('version' in h for h in headers): 
                continue
            
            idx = {'model': -1, 'announced': -1, 'available': -1, 'withdrawn': -1, 'discontinued': -1}
            for i, h in enumerate(headers):
                if 'model' in h or 'product' in h or 'version' in h: idx['model'] = i
                elif 'announced' in h or 'announcement' in h: idx['announced'] = i
                elif 'available' in h or 'availability' in h: idx['available'] = i
                elif 'withdrawn' in h or 'marketing' in h: idx['withdrawn'] = i
                elif 'discontinued' in h or 'service' in h: idx['discontinued'] = i

            # Skip if we couldn't even map model column
            if idx['model'] == -1: continue

            # Extract data from rows
            for tr in table.find_all('tr')[1:]:
                # Data rows might be all td or mix
                cols = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
                if len(cols) > idx['model']:
                    cell_model = cols[idx['model']].replace("-", "").upper()
                    # 如果該列包含了目標機種 (e.g. `9119-MHE` => `9119MHE` or `MHE`)
                    if target_model_clean in cell_model or cell_model in target_model_clean:
                        # 在此處調用 normalize_date 進行數據清洗
                        return {
                            "Announced": self.normalize_date(cols[idx['announced']]) if idx['announced'] < len(cols) else "-",
                            "Available": self.normalize_date(cols[idx['available']]) if idx['available'] < len(cols) else "-",
                            "Withdrawn": self.normalize_date(cols[idx['withdrawn']]) if idx['withdrawn'] < len(cols) else "-",
                            "Discontinued": self.normalize_date(cols[idx['discontinued']]) if idx['discontinued'] < len(cols) else "-"
                        }
        return None

    def search_ibm_support(self, page, model):
        """ 透過 IBM Support Lifecycle Search 尋找型號 """
        search_url = f"https://www.ibm.com/support/pages/lifecycle/search?q={model}"
        logger.info(f"嘗試備用查詢 (IBM Support): {search_url}")
        
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
            
            # 給合約和表格一點載入時間，IBM Support 是前端框架渲染
            try:
                page.wait_for_selector("table", timeout=20000)
                page.wait_for_timeout(3000)  # 等待 JS 渲染完成
            except:
                pass 
            
            # 解析搜尋結果頁面
            soup = BeautifulSoup(page.content(), 'html.parser')
            target_model_clean = model.replace("-", "").upper()
            target_node_url = None
            grid_dates = None
            
            # 第一優先：在搜尋結果的表格裡找 (IBM Data Table)
            for table in soup.find_all('table'):
                for tr in table.find_all('tr'):
                    cols = [td.get_text(separator=" ", strip=True).upper() for td in tr.find_all(['th', 'td'])]
                    row_text = " ".join(cols)
                    
                    if target_model_clean in row_text.replace("-", ""):
                        # 如果該行包含目標型號，尋找該行內的 node 連結
                        a_tag = tr.find('a', href=re.compile(r'/node/'))
                        if a_tag:
                            target_node_url = a_tag['href']
                            logger.info(f"成功透過 Table Row 比對找到 Node 連結: {target_node_url}")
                            
                        # 若這是一個標準的 IBM Lifecycle Data Grid (含有 8 欄以上)，順便蒐集表格上的日期作為最後備案
                        if len(cols) >= 8:
                            grid_dates = {
                                "Available": self.normalize_date(cols[5]) if cols[5] else "-",
                                "Withdrawn": self.normalize_date(cols[6]) if cols[6] else "-",
                                "Discontinued": self.normalize_date(cols[7]) if cols[7] else "-"
                            }
                        
                        if target_node_url:
                            break
                if target_node_url:
                    break
                    
            # 第二優先：如果上面的行比對沒找到 Node，退回原本檢查所有連結標題的模糊比對方式
            if not target_node_url:
                links = soup.find_all('a', href=re.compile(r'/node/'))
                for link in links:
                    text = link.get_text(strip=True).upper()
                    if model.upper() in text or target_model_clean in text.replace("-", ""):
                        target_node_url = link['href']
                        break
                        
            # 如果依然沒有 Node 連結，但我們剛剛從 Grid 攔截到日期，直接回傳 Grid 日期
            if not target_node_url:
                if grid_dates and (grid_dates.get('Available', '-') != '-' or grid_dates.get('Withdrawn', '-') != '-' or grid_dates.get('Discontinued', '-') != '-'):
                    logger.info(f"從搜尋列表網格成功擷取 {model} 週期資料")
                    return grid_dates
                
                logger.warning(f"IBM Support 備用查詢未找到精確符合 {model} 的結果")
                return None
                
            if not target_node_url.startswith("http"):
                target_node_url = f"https://www.ibm.com{target_node_url}"
                
            logger.info(f"進入 Support Node 頁面: {target_node_url}")
            page.goto(target_node_url, wait_until="domcontentloaded", timeout=40000)
            
            # 給合約和表格一點載入時間
            try:
                page.wait_for_selector("div.ibm-container", timeout=15000)
            except:
                pass 
                
            soup = BeautifulSoup(page.content(), 'html.parser')
            target_model_clean = model.replace("-", "").upper()
            
            support_dates = {"Announced": "-", "Available": "-", "Withdrawn": "-", "Discontinued": "-"}
            found_any_date = False
            
            # 第一種方式：針對 IBM Support Node 常見的純文字列表提取
            text_nodes = [t.strip() for t in soup.stripped_strings if t.strip()]
            for i, text in enumerate(text_nodes):
                for term, key in [
                    ('General Availability', 'Available'), 
                    ('Withdrawal from Marketing', 'Withdrawn'),
                    ('Withdrawn from Market', 'Withdrawn'), 
                    ('End of Support', 'Discontinued'),
                    ('Transition to End of Support Services', 'Discontinued')
                ]:
                    if term in text and i+1 < len(text_nodes):
                        # 擷取標準日期格式 31-Dec-2024 等
                        match = re.search(r'(\d{1,2}-[a-zA-Z]{3}-\d{4}|\d{4}-\d{2}-\d{2})', text_nodes[i+1])
                        if match:
                            support_dates[key] = self.normalize_date(match.group(1))
                            found_any_date = True
            
            if found_any_date:
                return support_dates

            # 第二種方式：Fallback 仍針對表格解析
            for table in soup.find_all('table'):
                headers = [th.get_text(strip=True).lower() for th in table.find_all(['th', 'td'])]
                
                # Support 網頁表格通常不一定叫 Model，也可能有 Date 關鍵字
                idx = {'model': -1, 'version': -1, 'available': -1, 'withdrawn': -1, 'discontinued': -1}
                for i, h in enumerate(headers):
                    if 'model' in h or 'product' in h: idx['model'] = i
                    elif 'version' in h or 'release' in h: idx['version'] = i
                    elif 'available' in h or 'availability' in h: idx['available'] = i
                    elif 'withdrawn' in h or 'marketing' in h: idx['withdrawn'] = i
                    elif 'discontinued' in h or 'support' in h: idx['discontinued'] = i
                
                for tr in table.find_all('tr')[1:]:
                    cols = [td.get_text(strip=True) for td in tr.find_all(['th', 'td'])]
                    # 有些網頁僅列 Version
                    check_idx = idx['model'] if idx['model'] != -1 else idx['version']
                    
                    if check_idx != -1 and len(cols) > check_idx:
                        cell_text = cols[check_idx].replace("-", "").upper()
                        if target_model_clean in cell_text or cell_text in target_model_clean or len(cols) >= 3:
                            return {
                                "Announced": "-",  # Support網頁通常沒有 Announced
                                "Available": self.normalize_date(cols[idx['available']]) if idx['available'] != -1 and idx['available'] < len(cols) else "-",
                                "Withdrawn": self.normalize_date(cols[idx['withdrawn']]) if idx['withdrawn'] != -1 and idx['withdrawn'] < len(cols) else "-",
                                "Discontinued": self.normalize_date(cols[idx['discontinued']]) if idx['discontinued'] != -1 and idx['discontinued'] < len(cols) else "-"
                            }
            return None
            
        except Exception as e:
            logger.warning(f"IBM Support 備用查詢發生錯誤: {e}")
            return None 
                
            soup = BeautifulSoup(page.content(), 'html.parser')
            target_model_clean = model.replace("-", "").upper()
            
            for table in soup.find_all('table'):
                headers = [th.get_text(strip=True).lower() for th in table.find_all(['th', 'td'])]
                
                # Support 網頁表格通常不一定叫 Model，也可能有 Date 關鍵字
                idx = {'model': -1, 'version': -1, 'available': -1, 'withdrawn': -1, 'discontinued': -1}
                for i, h in enumerate(headers):
                    if 'model' in h or 'product' in h: idx['model'] = i
                    elif 'version' in h or 'release' in h: idx['version'] = i
                    elif 'available' in h or 'availability' in h: idx['available'] = i
                    elif 'withdrawn' in h or 'marketing' in h: idx['withdrawn'] = i
                    elif 'discontinued' in h or 'support' in h: idx['discontinued'] = i
                
                for tr in table.find_all('tr')[1:]:
                    cols = [td.get_text(strip=True) for td in tr.find_all(['th', 'td'])]
                    # 有些網頁僅列 Version
                    check_idx = idx['model'] if idx['model'] != -1 else idx['version']
                    
                    if check_idx != -1 and len(cols) > check_idx:
                        cell_text = cols[check_idx].replace("-", "").upper()
                        if target_model_clean in cell_text or cell_text in target_model_clean or len(cols) >= 3:
                            return {
                                "Announced": "-",  # Support網頁通常沒有 Announced
                                "Available": self.normalize_date(cols[idx['available']]) if idx['available'] != -1 and idx['available'] < len(cols) else "-",
                                "Withdrawn": self.normalize_date(cols[idx['withdrawn']]) if idx['withdrawn'] != -1 and idx['withdrawn'] < len(cols) else "-",
                                "Discontinued": self.normalize_date(cols[idx['discontinued']]) if idx['discontinued'] != -1 and idx['discontinued'] < len(cols) else "-"
                            }
            return None
            
        except Exception as e:
            logger.warning(f"IBM Support 備用查詢發生錯誤: {e}")
            return None

    def run_for_file(self, config_path, title, is_first_file):
        self.results = []
        with open(config_path, 'r', encoding='utf-8') as f:
            models = yaml.safe_load(f).get('models', [])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            # 效能優化：過濾靜態資源以免載入卡死
            page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff2}", lambda route: route.abort())

            for model in models:
                row_data = {"Model": model}
                sm_data = None
                sp_data = None
                
                try:
                    # --- 1. 查詢 Sales Manual ---
                    search_url = f"https://www.ibm.com/docs/en/search/Family%20{model}+?type=salesmanual"
                    logger.info(f"正在搜尋型號: {model} (Sales Manual)")
                    
                    page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
                    
                    announce_found = False
                    try:
                        link_locator = page.locator("a[href*='/announcements/']").first
                        link_locator.wait_for(state="attached", timeout=12000)
                        announce_found = True
                    except:
                        logger.warning(f"{model} 在 Sales Manual 未找到公告")

                    if announce_found:
                        announce_url = link_locator.get_attribute("href")
                        if not announce_url.startswith("http"):
                            announce_url = f"https://www.ibm.com{announce_url}"
                        
                        logger.info(f"進入 SM 公告頁: {announce_url}")
                        page.goto(announce_url, wait_until="domcontentloaded", timeout=40000)
                        try:
                            # 針對某些沒有 table 也能快速略過的頁面加上 timeout
                            page.wait_for_selector("table", timeout=12000)
                            sm_data = self.parse_table(page.content(), model)
                        except:
                            logger.warning(f"{model} SM 公告頁無可用表格")
                    
                    # --- 2. 查詢 IBM Support 生命週期 ---
                    logger.info(f"正在搜尋型號: {model} (IBM Support)")
                    sp_data = self.search_ibm_support(page, model)
                    
                    # --- 3. 合併結果 ---
                    # --- 3. 合併結果 (以SP為主，若缺則用SM補) ---
                    final_dates = {
                        "Announced": "-",
                        "Available": "-",
                        "Withdrawn": "-",
                        "Discontinued": "-"
                    }
                    
                    if sm_data:
                        logger.info(f"{model} [SM] 成功: Ann={sm_data.get('Announced')}, Avb={sm_data.get('Available')}, Wth={sm_data.get('Withdrawn')}, Dis={sm_data.get('Discontinued')}")
                        final_dates["Announced"] = self.normalize_date(sm_data.get("Announced", "-"))
                        final_dates["Available"] = self.normalize_date(sm_data.get("Available", "-"))
                        final_dates["Withdrawn"] = self.normalize_date(sm_data.get("Withdrawn", "-"))
                        final_dates["Discontinued"] = self.normalize_date(sm_data.get("Discontinued", "-"))
                    else:
                        logger.info(f"{model} [SM] 無資料")
                    
                    if sp_data:
                        logger.info(f"{model} [SP] 成功: Avb={sp_data.get('Available')}, Wth={sp_data.get('Withdrawn')}, Dis={sp_data.get('Discontinued')}")
                        sp_avail = self.normalize_date(sp_data.get("Available", "-"))
                        sp_withd = self.normalize_date(sp_data.get("Withdrawn", "-"))
                        sp_disc = self.normalize_date(sp_data.get("Discontinued", "-"))
                        
                        if sp_avail not in ["-", "N/A"]:
                            final_dates["Available"] = sp_avail
                        if sp_withd not in ["-", "N/A"]:
                            final_dates["Withdrawn"] = sp_withd
                        if sp_disc not in ["-", "N/A"]:
                            final_dates["Discontinued"] = sp_disc
                    else:
                        logger.info(f"{model} [SP] 無資料")
                        
                    row_data.update(final_dates)
                    
                    # 邏輯驗證標籤
                    val = self.validate_dates_logic(final_dates)
                    row_data["Validation"] = "✅" if "✅" in val else "❌" if "❌" in val else "⚠️"
                    
                    self.results.append(row_data)

                except Exception as e:
                    logger.error(f"{model} 處理異常: {str(e)}")
                    self.results.append({
                        "Model": model, 
                        "Announced": "Error", "Available": "Error", "Withdrawn": "Error", "Discontinued": "Error",
                        "Validation": "❌ 例外"
                    })
            
            browser.close()
        self.display(title)
        self.write_to_readme(title, is_first_file)

    def run(self):
        yaml_files = sorted(glob.glob('*.yaml'))
        for i, yaml_file in enumerate(yaml_files):
            clean_title = os.path.splitext(yaml_file)[0].replace('_', ' ')
            logger.info(f"正在處理類別: {clean_title}")
            self.run_for_file(yaml_file, clean_title, i == 0)

    def write_to_readme(self, title, is_first_file):
        headers = [
            "Model", 
            "Announced", "Available", "Withdrawn", "Discontinued", "Validation"
        ]
        col_widths = {h: len(h) for h in headers}

        for r in self.results:
            for h in headers:
                cell_content = str(r.get(h, '-'))
                # 處理換行字元避免 markdown 表格破版
                cell_content = cell_content.replace('\n', ' <br> ')
                if len(cell_content) > col_widths[h]:
                    col_widths[h] = len(cell_content)

        md_table = f"## {title}\n\n"
        md_table += "| " + " | ".join([f"{h:<{col_widths[h]}}" for h in headers]) + " |\n"
        md_table += "|-" + "-|-".join(["-" * col_widths[h] for h in headers]) + "-|\n"

        for r in self.results:
            row_data = []
            for h in headers:
                val = str(r.get(h, '-')).replace('\n', ' <br> ')
                row_data.append(f"{val:<{col_widths[h]}}")
            md_table += "| " + " | ".join(row_data) + " |\n"
        md_table += "\n"

        mode = 'w' if is_first_file else 'a'
        if is_first_file:
            md_table = "# IBM Hardware Lifecycle\n\n" + md_table

        with open('README.md', mode, encoding='utf-8') as f:
            f.write(md_table)
        logger.info(f"結果已寫入 README.md ({title})")

    def display(self, title):
        print(f"\n## {title}\n")
        
        headers = [
            "Model", "Announced", "Available", "Withdrawn", "Discontinued", "Validation"
        ]
        # 設定固定欄寬
        col_widths = [12, 12, 12, 12, 14, 12]
        
        header_str = " | ".join([f"{h:<{w}}" for h, w in zip(headers, col_widths)])
        print("=" * len(header_str))
        print(header_str)
        print("-" * len(header_str))
        
        for r in self.results:
            row_str = " | ".join([f"{str(r.get(h, '-')).replace(chr(10), ' '):<{w}}" for h, w in zip(headers, col_widths)])
            print(row_str)
            
        print("=" * len(header_str))

if __name__ == "__main__":
    lifecycle = IBMLifecycleUniversalSRE()
    lifecycle.run()