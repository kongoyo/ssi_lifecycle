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
        if m3: 
            norm = m3.group(1)
            year = int(norm.split('-')[0])
            if 1980 < year < 2050:
                return norm
            else:
                return "N/A"
        
        # 最終校驗：若回傳結果包含年份，檢查是否在合理區間
        year_match = re.search(r'(\d{4})', date_str)
        if year_match:
            year = int(year_match.group(1))
            if not (1980 < year < 2050):
                return "N/A"

        return "N/A"

    def process_model(self, model, page):
        print(f"\n[Item] 稽核對象: {model}")
        search_parts = model.split('-')
        search_query = "+".join(search_parts) if len(search_parts) > 1 else model
        
        final_res = {"Model": model, "Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "EOS_Std": "N/A", "EOS_Full": "N/A", "Url": "-"}
        
        # (3) 第一優先查詢: IBM Support Lifecycle (DataTables)
        print(f"  [STEP 1] 啟動第一優先查詢: IBM Support Lifecycle")
        support_res = self._search_support_lifecycle(model, page)
        for k, v in support_res.items():
            if v != "N/A":
                final_res[k] = v
        if support_res.get("Url") != "N/A":
            final_res["Url"] = support_res["Url"]
        elif any(v != "N/A" for k, v in support_res.items() if k != "Url"):
            final_res["Url"] = "https://www.ibm.com/support/pages/lifecycle/"
        
        # (1) & (2) 次要與補充查詢: IBM Docs (Sales Manual / Announcement)
        if final_res["Announced"] == "N/A" or final_res["Available"] == "N/A" or final_res["Withdrawn"] == "N/A" or final_res["EOS_Std"] == "N/A":
            print(f"  [STEP 2] 資料尚有缺失，啟動次要查詢: IBM Docs")
            for s_type in ["salesmanual", "announcement"]:
                # 如果核心資料已齊備，則提早結束
                if final_res["Announced"] != "N/A" and final_res["Available"] != "N/A" and final_res["Withdrawn"] != "N/A" and final_res["EOS_Std"] != "N/A":
                    break
                    
                search_url = f"https://www.ibm.com/docs/en/search/{search_query}?type={s_type}"
                print(f"    [SEARCH] 類別: {s_type} | 策略: {search_query}")
                
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
                            print(f"    [ERROR] 搜尋頁面加載失敗: {e}")
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
                        
                        print(f"      [TRACE] 檢查候選公告 ({s_type}): {cand['title']}")
                        
                        # [優化] 排除顯然不是該型號主體的頁面
                        bad_keywords = ["CONVERSION", "UPGRADE", "FROM ", "TO ", "REPLACEMENT"]
                        if any(k in cand['title'].upper() for k in bad_keywords):
                            print(f"        [SKIP] 候選標題包含疑似非主體關鍵字: {cand['title']}")
                            continue

                        try:
                            page.goto(full_url, wait_until="domcontentloaded", timeout=60000)
                            
                            try:
                                h1_text = page.locator("h1").first.inner_text().upper()
                                if model_full not in h1_text and model_clean not in h1_text:
                                    print(f"        [WARN] 頁面 H1 不包含目標型號: {h1_text}")
                                    if s_type == "announcement":
                                        continue
                            except: pass

                            is_ap = False
                            ap_indicators = ["ASIA PACIFIC", "AP", "JAPAN", "AUSTRALIA", "CHINA", "KOREA", "INDIA", "ASEAN"]
                            
                            try:
                                page_content = page.content().upper()
                                if any(ind in page_content for ind in ap_indicators):
                                    is_ap = True
                                
                                if not is_ap:
                                    region_btn = page.locator("button.dw-region-selector-button, .region-selector").first
                                    if region_btn.is_visible():
                                        region_btn.click()
                                        ap_opt = page.locator("text=Asia Pacific, text=AP, text=Japan").first
                                        if ap_opt.is_visible():
                                            ap_opt.click()
                                            page.wait_for_timeout(2000)
                                            is_ap = True
                            except: pass

                            page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
                            page.wait_for_timeout(1000)
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            
                            try:
                                page.wait_for_function("""
                                    () => {
                                        const text = document.body.innerTextContent || document.body.innerText;
                                        return /\\d{4}-\\d{2}-\\d{2}|[A-Za-z]+\\s+\\d{1,2},\\s+\\d{4}|\\d{1,2}\\s+[A-Za-z]+\\s+\\d{4}/.test(text);
                                    }
                                """, timeout=8000)
                            except:
                                pass
                            
                            page.wait_for_timeout(2000)
                            try: page.wait_for_selector("table", timeout=5000)
                            except: pass
                            
                            content_upper = page.content().upper()
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
                                        if any(k in text for k in ["MODEL", "ANNOUNCED", "AVAILABLE", "WITHDRAWN", "DISCONTINUED"]):
                                            if "SUPPORT LEVEL" in text: continue
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
                                        if "CONVERSION" in cell0_text or "UPGRADE" in cell0_text:
                                            continue

                                        if model_full in cell0_text or model_clean in cell0_text or (len(cell0_text) > 3 and cell0_text in model_full):
                                            row_data = [c.inner_text().strip() for c in cells]
                                            current_cand_res = {}
                                            for idx, h in enumerate(headers):
                                                if idx >= len(row_data): break
                                                val = self.normalize_date(row_data[idx])
                                                if val != "N/A":
                                                    # 根據來源類型 (s_type) 決定權重
                                                    if "ANNOUNCED" in h or "ANNOUNCE" in h:
                                                        if s_type == "salesmanual": current_cand_res["Announced"] = val
                                                        elif final_res.get("Announced", "N/A") == "N/A":
                                                            current_cand_res["Announced"] = val
                                                    
                                                    elif "AVAILABLE" in h or "AVAILABILITY" in h:
                                                        if final_res.get("Available", "N/A") == "N/A":
                                                            current_cand_res["Available"] = val
                                                            
                                                    elif "WITHDRAWN" in h or "WITHDRAWAL" in h:
                                                        # EOM 使用 Product lifecycle 或 salesmanual
                                                        current_cand_res["Withdrawn"] = val
                                                        
                                                    elif "SUPPORT LEVEL CHANGED" in h:
                                                        if final_res.get("EOS_Std", "N/A") == "N/A":
                                                            current_cand_res["EOS_Std"] = val
                                                            
                                                    elif "SERVICE DISCONTINUED" in h or "SERVICE DISCONTINUANCE" in h:
                                                        # EOS 強制使用 Service Discontinued
                                                        current_cand_res["EOS_Full"] = val
                                                        
                                                    elif "DISCONTINUED" in h or "DISCONTINUANCE" in h or "EOS" in h:
                                                        if "SUPPORT LEVEL" in h: 
                                                            if final_res.get("EOS_Std", "N/A") == "N/A":
                                                                current_cand_res["EOS_Std"] = val
                                                        else: 
                                                            if final_res.get("EOS_Full", "N/A") == "N/A":
                                                                current_cand_res["EOS_Full"] = val
                                            
                                            # 合併數據
                                            for k, v in current_cand_res.items():
                                                if final_res.get(k, "N/A") == "N/A":
                                                    final_res[k] = v
                                            
                                            # Source Link 使用 Product lifecycle (如果已經有了就不蓋掉)
                                            # [Harness] 僅在目前為空或為通用搜尋頁面時才允許更新
                                            weak_urls = ["-", "N/A", "https://www.ibm.com/support/pages/lifecycle/"]
                                            if final_res.get("Url") in weak_urls:
                                                final_res["Url"] = full_url
                                            
                                            if current_cand_res:
                                                print(f"        [DATA] 提取結果: {current_cand_res}")
                                    except: continue

                            # 文本正則兜底
                            if final_res["Announced"] == "N/A":
                                try:
                                    page_text = page.evaluate("() => document.body.innerText")
                                    anchors = [model_full, model_clean, "PLANNED AVAILABILITY DATE", "ANNOUNCEMENT DATE"]
                                    found_dates = []
                                    for anchor in anchors:
                                        pos = page_text.upper().find(anchor)
                                        if pos != -1:
                                            start = max(0, pos - 200)
                                            snippet = page_text[start : pos + 1500]
                                            dates = re.findall(r'(\d{4}-\d{2}-\d{2})|([A-Za-z]+\s+\d{1,2},\\s+\d{4})|(\d{1,2}\\s+[A-Za-z]+\\s+\d{4})', snippet)
                                            for d_tuple in dates:
                                                d_str = d_tuple[0] or d_tuple[1] or d_tuple[2]
                                                norm = self.normalize_date(d_str)
                                                if norm != "N/A" and norm not in found_dates:
                                                    found_dates.append(norm)
                                    
                                    if len(found_dates) >= 2:
                                        sorted_dates = sorted(found_dates)
                                        if final_res["Announced"] == "N/A": final_res["Announced"] = sorted_dates[0]
                                        if final_res["Available"] == "N/A": final_res["Available"] = sorted_dates[1]
                                        
                                        # [Harness] 正則補位時也應遵守 URL 保護原則
                                        weak_urls = ["-", "N/A", "https://www.ibm.com/support/pages/lifecycle/"]
                                        if final_res.get("Url") in weak_urls:
                                            final_res["Url"] = full_url
                                except: pass
                            
                            if final_res["Announced"] != "N/A" and final_res["Available"] != "N/A":
                                print(f"      [VERIFIED] 已從 {s_type} 獲得核心數據。")
                                break
                                
                        except Exception as e:
                            print(f"      [TRACE] 存取出錯: {e}")
                            continue
                except Exception as e:
                    print(f"    [ERROR] 處理 {s_type} 搜尋時出錯: {e}")
                    continue

        # 新增規則：EOS (Full) 不可比 EOS (Standard) 小，發生則視為無效，顯示 N/A
        if final_res["EOS_Std"] != "N/A" and final_res["EOS_Full"] != "N/A":
            if final_res["EOS_Full"] < final_res["EOS_Std"]:
                print(f"    [VALIDATION] EOS_Full ({final_res['EOS_Full']}) 早於 EOS_Std ({final_res['EOS_Std']})，將其設為 N/A")
                final_res["EOS_Full"] = "N/A"
        
        return final_res

    def _search_support_lifecycle(self, model, page):
        support_url = "https://www.ibm.com/support/pages/lifecycle/"
        res = {"Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "EOS_Std": "N/A", "EOS_Full": "N/A", "Url": "N/A"}
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
                    # 第 6 欄: GA Date -> 映射為 Available (GA)
                    ga_date = self.normalize_date(cells[5].inner_text().strip())
                    if ga_date != "N/A":
                        res["Available"] = ga_date
                    
                    # 第 7 欄: Transition to Extended -> 映射為 EOS_Std
                    eos_std = self.normalize_date(cells[6].inner_text().strip())
                    if eos_std != "N/A":
                        res["EOS_Std"] = eos_std
                    
                    # 第 8 欄: Extended Support Complete -> 映射為 EOS_Full
                    try:
                        if len(cells) >= 8:
                            eos_full = self.normalize_date(cells[7].inner_text().strip())
                            if eos_full != "N/A": res["EOS_Full"] = eos_full
                    except: pass
                    
                    # 提取具體產品頁面連結
                    try:
                        link_loc = cells[1].locator("a").first
                        if link_loc.is_visible():
                            href = link_loc.get_attribute("href")
                            if href:
                                if href.startswith('/'): res["Url"] = "https://www.ibm.com" + href
                                else: res["Url"] = href
                    except: pass
                    
                    return res
        except Exception as e:
            print(f"    [ERROR] Support Lifecycle 查詢失敗: {e}")
            
        return res

    def _null_result(self, model):
        return {"Model": model, "Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "EOS_Std": "N/A", "EOS_Full": "N/A", "Url": "-"}

    def run(self):
        with sync_playwright() as p:
            # 啟動可視化以便調試 (根據 rules: 高級審美與互動)
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            page = context.new_page()
            
            all_results = {}
            try:
                for category, models in self.models_data.items():
                    print(f"\n{'='*20} 處理分組: {category} {'='*20}")
                    category_res = []
                    for idx, model in enumerate(models):
                        print(f"進度: [{idx+1}/{len(models)}]")
                        try:
                            res = self.process_model(model, page)
                            category_res.append(res)
                        except Exception as e:
                            print(f"    [CRITICAL] 處理型號 {model} 時發生嚴重錯誤: {e}")
                            category_res.append(self._null_result(model))
                            
                        # 資源回收
                        if (idx + 1) % 10 == 0:
                            page.close()
                            page = context.new_page()
                    all_results[category] = category_res
            finally:
                browser.close()
                self._write_report(all_results)

    def _write_report(self, all_results):
        with open(self.report_path, 'w', encoding='utf-8') as f:
            f.write("# IBM Hardware Lifecycle Report\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
            for category, results in all_results.items():
                f.write(f"## {category}\n\n")
                f.write("| Model | Announced | Available | Withdrawn | EOS (Standard) | EOS (Full) | Source |\n")
                f.write("|-------|-----------|-----------|-----------|----------------|------------|--------|\n")
                for r in results:
                    url_cell = f"[Link]({r['Url']})" if r['Url'] != "-" else "-"
                    f.write(f"| {r['Model']} | {r['Announced']} | {r['Available']} | {r['Withdrawn']} | {r['EOS_Std']} | {r['EOS_Full']} | {url_cell} |\n")
                f.write("\n")
        
        print(f"\n[+] 任務完成，報表已更新: {self.report_path}")

if __name__ == "__main__":
    harness = IBMLifecycleHarness()
    harness.run()
