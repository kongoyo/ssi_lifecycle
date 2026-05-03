import yaml
import logging
import re
import time
from playwright.sync_api import sync_playwright
from datetime import datetime
import os
import sys

# 強制 stdout 使用 UTF-8 以避免 Windows CP950 錯誤
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# [Harness] Defensive Logging
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger("IBM-SRE")

class IBMLifecycleHarness:
    def __init__(self):
        self.models_data = self._load_config()
        self.results = []
        self.report_path = 'readme.md'

    def _load_config(self):
        combined_data = {}
        # 掃描當前目錄所有 YAML 檔案
        yaml_files = [f for f in os.listdir('.') if f.endswith('.yaml') and f != 'package.json']
        print(f"[*] 偵測到 {len(yaml_files)} 個設定檔: {yaml_files}")
        
        for yf in yaml_files:
            try:
                with open(yf, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    if data:
                        combined_data.update(data)
            except Exception as e:
                print(f"  [!] 讀取 {yf} 失敗: {e}")
        return combined_data

    def normalize_date(self, date_str):
        if not date_str or date_str.upper() == "N/A" or date_str == "-":
            return "N/A"
        
        # 徹底封殺已知的全局腳註錯誤日期 (防禦性措施)
        if "2020-01-15" in date_str: return "N/A"
        
        # 清理腳註或噪聲
        date_str = re.sub(r'\[\d+\]', '', date_str).strip()
        
        months = {
            "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
            "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
            "JANUARY": "01", "FEBRUARY": "02", "MARCH": "03", "APRIL": "04", "MAY": "05", "JUNE": "06",
            "JULY": "07", "AUGUST": "08", "SEPTEMBER": "09", "OCTOBER": "10", "NOVEMBER": "11", "DECEMBER": "12"
        }
        
        # Format: 15-JAN-2020
        m1 = re.match(r'(\d{1,2})-([A-Za-z]{3,9})-(\d{4})', date_str)
        if m1:
            day, mon, year = m1.groups()
            mon_num = months.get(mon.upper(), "01")
            return f"{year}-{mon_num}-{day.zfill(2)}"
        
        # Format: January 15, 2020
        m2 = re.match(r'([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})', date_str)
        if m2:
            mon, day, year = m2.groups()
            mon_num = months.get(mon.upper(), "01")
            return f"{year}-{mon_num}-{day.zfill(2)}"
        
        # Format: 31 January 2026
        m4 = re.match(r'(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})', date_str)
        if m4:
            day, mon, year = m4.groups()
            mon_num = months.get(mon.upper(), "01")
            return f"{year}-{mon_num}-{day.zfill(2)}"

        # Format: 2020-01-15
        m3 = re.search(r'(\d{4}-\d{2}-\d{2})', date_str)
        if m3: return m3.group(1)
        
        return "N/A"

    def process_model(self, model, page):
        print(f"\n[Item] 稽核對象: {model}")
        search_parts = model.split('-')
        search_query = "+".join(search_parts) if len(search_parts) > 1 else model
        
        final_res = {"Model": model, "Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A", "Url": "-"}
        
        # 跨類別搜尋迴圈 (Sales Manual 與 Announcement Letters 是互斥的)
        for s_type in ["salesmanual", "announcement"]:
            if final_res["Announced"] != "N/A" and final_res["Available"] != "N/A":
                break
                
            search_url = f"https://www.ibm.com/docs/en/search/{search_query}?type={s_type}"
            print(f"  [SEARCH] 類別: {s_type} | 策略: {search_query}")
            
            for attempt in range(2):
                try:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                    try: page.wait_for_selector("h3.dw-search-result-title, .dw-search-result-title", timeout=10000)
                    except: pass
                    page.wait_for_timeout(3000)
                    break
                except Exception as e:
                    if attempt == 0:
                        page.wait_for_timeout(5000)
                    else:
                        print(f"  [ERROR] 搜尋頁面加載失敗: {e}")
                        continue

            try:
                all_links = page.locator("a").all()
                if len(all_links) < 15: continue
                
                candidates = []
                prefix = model.split('-')[0]
                model_full = model.upper()
                model_clean = model.replace("-", "").upper()
                
                for i, link in enumerate(all_links):
                    try:
                        title = link.inner_text().strip()
                        href = link.get_attribute("href")
                        if not href: continue
                        
                        if "/announcements/" in href or "salesmanual" in href.lower() or "announcement" in href.lower():
                            if model_full in title.upper() or model_clean in title.upper():
                                candidates.append({"title": title, "url": href})
                            elif prefix.upper() in title.upper() and len(title) > 5:
                                candidates.append({"title": title, "url": href})
                    except: continue

                # 去重
                seen_urls = set()
                unique_candidates = []
                for c in candidates:
                    if c['url'] not in seen_urls:
                        unique_candidates.append(c)
                        seen_urls.add(c['url'])
                candidates = unique_candidates

                if not candidates:
                    # 兜底：嘗試抓取前幾個可能相關的連結
                    for link in all_links:
                        try:
                            href = link.get_attribute("href")
                            title = link.inner_text().strip()
                            if href and "/announcements/" in href and len(title) > 10:
                                candidates.append({"title": title, "url": href})
                                if len(candidates) >= 2: break
                        except: continue

                # 優先級排序：完全匹配型號的排在前面
                candidates.sort(key=lambda x: 1 if (model_full in x['title'].upper() or model_clean in x['title'].upper()) else 2)
                
                for cand in candidates[:8]:
                    full_url = cand['url']
                    if full_url.startswith('/'):
                        full_url = "https://www.ibm.com" + full_url
                    
                    print(f"  [TRACE] 檢查候選公告 ({s_type}): {cand['title']}")
                    
                    try:
                        page.goto(full_url, wait_until="domcontentloaded", timeout=60000)
                        
                        # 核心優化：嘗試切換至 AP 地區以確保數據一致性
                        try:
                            # 尋找地區選擇器 (IBM Docs 常見模式)
                            region_btn = page.locator("button.dw-region-selector-button, .region-selector").first
                            if region_btn.is_visible():
                                region_btn.click()
                                # 優先尋找 AP 或 Asia Pacific
                                ap_opt = page.locator("text=Asia Pacific, text=AP, text=Japan").first
                                if ap_opt.is_visible():
                                    ap_opt.click()
                                    page.wait_for_timeout(2000)
                        except: pass

                        # 自動滾動以觸發動態加載 (Shadow DOM / Lazy Load)
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
                        page.wait_for_timeout(1000)
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        
                        # 核心修復：等待 Shadow DOM 水合 (Hydration)
                        # 等待頁面出現日期模式 (YYYY-MM-DD 或 Month DD, YYYY)
                        try:
                            page.wait_for_function("""
                                () => {
                                    const text = document.body.innerTextContent || document.body.innerText;
                                    return /\\d{4}-\\d{2}-\\d{2}|[A-Za-z]+\\s+\\d{1,2},\\s+\\d{4}|\\d{1,2}\\s+[A-Za-z]+\\s+\\d{4}/.test(text);
                                }
                            """, timeout=8000)
                        except:
                            print(f"    [TRACE] 等待水合超時，嘗試強制提取...")
                        
                        page.wait_for_timeout(2000)
                        
                        try: page.wait_for_selector("table", timeout=5000)
                        except: pass
                        
                        content_upper = page.content().upper()
                        
                        # 檢查頁面內容是否包含型號與關鍵字
                        if model_full not in content_upper and model_clean not in content_upper:
                            if model_full not in cand['title'].upper() and model_clean not in cand['title'].upper():
                                continue

                        tables = page.locator("table").all()
                        for table_idx, table in enumerate(tables):
                            rows = table.locator("tr").all()
                            if not rows: continue
                            
                            headers = []
                            try:
                                for r in rows[:3]:
                                    text = r.inner_text().upper()
                                    if any(k in text for k in ["MODEL", "ANNOUNCED", "AVAILABLE", "WITHDRAWN", "DISCONTINUED", "SUPPORT"]):
                                        h_cells = r.locator("th, td").all()
                                        headers = [h.inner_text().strip().upper() for h in h_cells]
                                        break
                                if not headers:
                                    h_cells = rows[0].locator("th, td").all()
                                    headers = [h.inner_text().strip().upper() for h in h_cells]
                            except: pass

                            for row_idx, row in enumerate(rows):
                                cells = row.locator("td, th").all()
                                if not cells: continue
                                
                                try:
                                    cell0_text = cells[0].inner_text().strip().upper()
                                    if model_full in cell0_text or model_clean in cell0_text or (len(cell0_text) > 3 and cell0_text in model_full):
                                        row_data = [c.inner_text().strip() for c in cells]
                                        current_cand_res = {}
                                        for idx, h in enumerate(headers):
                                            if idx >= len(row_data): break
                                            val = self.normalize_date(row_data[idx])
                                            if val != "N/A":
                                                if "ANNOUNCED" in h or "ANNOUNCE" in h: current_cand_res["Announced"] = val
                                                elif "AVAILABLE" in h or "AVAILABILITY" in h: current_cand_res["Available"] = val
                                                elif "WITHDRAWN" in h or "WITHDRAWAL" in h: current_cand_res["Withdrawn"] = val
                                                elif "DISCONTINUED" in h or "DISCONTINUANCE" in h or "EOS" in h or "SUPPORT" in h or "SERVICE DISCONTINUED" in h or "LEVEL CHANGED" in h: 
                                                    current_cand_res["Discontinued"] = val
                                        
                                        # 靜態索引兜底
                                        if "Announced" not in current_cand_res and len(row_data) > 1:
                                            val = self.normalize_date(row_data[1])
                                            if val != "N/A": current_cand_res["Announced"] = val
                                        if "Available" not in current_cand_res and len(row_data) > 2:
                                            val = self.normalize_date(row_data[2])
                                            if val != "N/A": current_cand_res["Available"] = val
                                        
                                        # 合併數據
                                        for k, v in current_cand_res.items():
                                            if final_res.get(k, "N/A") == "N/A":
                                                final_res[k] = v
                                                final_res["Url"] = full_url
                                        
                                        if current_cand_res:
                                            print(f"    [DATA] 提取結果: {current_cand_res}")
                                except: continue

                        # 文本正則兜底 (加入 Shadow DOM 穿透與 Proximity 檢查)
                        if final_res["Announced"] == "N/A":
                            try:
                                # 使用 JS 深度提取包含 Shadow DOM 的所有文本
                                page_text = page.evaluate("""
                                    () => {
                                        function getDeepText(node) {
                                            let text = "";
                                            if (node.nodeType === Node.TEXT_NODE) {
                                                text += node.textContent + " ";
                                            } else if (node.nodeType === Node.ELEMENT_NODE) {
                                                if (node.tagName !== 'SCRIPT' && node.tagName !== 'STYLE') {
                                                    if (node.shadowRoot) {
                                                        text += getDeepText(node.shadowRoot);
                                                    }
                                                    for (let child of node.childNodes) {
                                                        text += getDeepText(child);
                                                    }
                                                }
                                            } else if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                                                for (let child of node.childNodes) {
                                                    text += getDeepText(child);
                                                }
                                            }
                                            return text;
                                        }
                                        return getDeepText(document.body);
                                    }
                                """)
                                
                                # 尋找型號附近的日期 (距離 800 字元內)
                                model_pos = page_text.upper().find(model_full)
                                if model_pos == -1: model_pos = page_text.upper().find(model_clean)
                                
                                if model_pos != -1:
                                    snippet = page_text[model_pos : model_pos + 1200] # 取後方 1200 字元
                                    dates = re.findall(r'(\d{4}-\d{2}-\d{2})|(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})', snippet)
                                    if dates:
                                        flat_dates = []
                                        for d_tuple in dates:
                                            d_str = d_tuple[0] or d_tuple[1]
                                            norm = self.normalize_date(d_str)
                                            if norm != "N/A": flat_dates.append(norm)
                                        
                                        if len(flat_dates) >= 2:
                                            final_res["Announced"] = flat_dates[0]
                                            final_res["Available"] = flat_dates[1]
                                            final_res["Url"] = full_url
                                            print(f"    [DATA] 深度文本正則提取成功: {flat_dates[:2]}")
                            except Exception as e:
                                print(f"    [TRACE] 深度文本提取失敗: {e}")
                                pass
                        
                        if final_res["Announced"] != "N/A" and final_res["Available"] != "N/A":
                            print(f"  [VERIFIED] 已從 {s_type} 獲得核心數據。")
                            break
                            
                    except Exception as e:
                        print(f"  [TRACE] 存取出錯: {e}")
                        continue
            except Exception as e:
                print(f"  [ERROR] 處理 {s_type} 搜尋時出錯: {e}")
                continue

        # 第三階層 Fallback: IBM Support Lifecycle Site (DataTables)
        if final_res["Announced"] == "N/A" or final_res["Available"] == "N/A":
            print(f"  [FALLBACK] 前兩階層未果，啟動第三階層: IBM Support Lifecycle")
            support_res = self._search_support_lifecycle(model, page)
            for k, v in support_res.items():
                if final_res.get(k, "N/A") == "N/A" and v != "N/A":
                    final_res[k] = v
                    final_res["Url"] = "https://www.ibm.com/support/pages/lifecycle/"

        return final_res

    def _search_support_lifecycle(self, model, page):
        support_url = "https://www.ibm.com/support/pages/lifecycle/"
        res = {"Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"}
        try:
            page.goto(support_url, wait_until="domcontentloaded", timeout=60000)
            # 尋找搜尋框並輸入
            search_input = page.locator("#plc--query")
            search_input.fill(model)
            search_input.press("Enter")
            
            # 等待表格加載 (DataTables)
            try:
                page.wait_for_selector("table.dataTable tbody tr", timeout=10000)
            except:
                print(f"    [TRACE] Support 頁面未發現數據表格。")
                return res
            
            rows = page.locator("table.dataTable tbody tr").all()
            model_clean = model.replace("-", "").upper()
            model_full = model.upper()
            
            for row in rows:
                cells = row.locator("td").all()
                if len(cells) < 7: continue
                
                # 第 2 欄 (Product name) 或 第 5 欄 (PID/MTM)
                prod_name = cells[1].inner_text().strip().upper()
                pid_mtm = cells[4].inner_text().strip().upper()
                
                if model_full in prod_name or model_clean in pid_mtm:
                    print(f"    [MATCH] Support Lifecycle 匹配成功: {prod_name}")
                    # 第 6 欄: GA Date
                    ga_date = self.normalize_date(cells[5].inner_text().strip())
                    # 第 7 欄: EOS Date
                    eos_date = self.normalize_date(cells[6].inner_text().strip())
                    
                    if ga_date != "N/A":
                        res["Announced"] = ga_date
                        res["Available"] = ga_date
                    if eos_date != "N/A":
                        res["Discontinued"] = eos_date
                    
                    return res
        except Exception as e:
            print(f"    [ERROR] Support Lifecycle 查詢失敗: {e}")
            
        return res

    def _null_result(self, model):
        return {"Model": model, "Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A", "Url": "-"}

    def run(self):
        with sync_playwright() as p:
            # 啟動可視化以便調試 (根據 rules: 高級審美與互動)
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            page = context.new_page()
            
            all_results = {}
            for category, models in self.models_data.items():
                print(f"\n{'='*20} 處理分組: {category} {'='*20}")
                category_res = []
                for idx, model in enumerate(models):
                    print(f"進度: [{idx+1}/{len(models)}]")
                    res = self.process_model(model, page)
                    category_res.append(res)
                    # 資源回收
                    if (idx + 1) % 10 == 0:
                        page.close()
                        page = context.new_page()
                all_results[category] = category_res
            
            browser.close()
            self._write_report(all_results)

    def _write_report(self, all_results):
        with open(self.report_path, 'w', encoding='utf-8') as f:
            f.write("# IBM Hardware Lifecycle Report\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
            for category, results in all_results.items():
                f.write(f"## {category}\n\n")
                f.write("| Model | Announced | Available | Withdrawn | Discontinued | Source |\n")
                f.write("|-------|-----------|-----------|-----------|--------------|--------|\n")
                for r in results:
                    url_cell = f"[Link]({r['Url']})" if r['Url'] != "-" else "-"
                    f.write(f"| {r['Model']} | {r['Announced']} | {r['Available']} | {r['Withdrawn']} | {r['Discontinued']} | {url_cell} |\n")
                f.write("\n")
        
        print(f"\n[+] 任務完成，報表已更新: {self.report_path}")

if __name__ == "__main__":
    harness = IBMLifecycleHarness()
    harness.run()
