import yaml
import logging
import glob
from urllib.parse import quote
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class IBMLifecycleUniversalSRE:

    def __init__(self):

        self.results = []



    def parse_table(self, html_content, target_model):

        soup = BeautifulSoup(html_content, 'html.parser')

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

                # 模糊匹配型號，例如 9105-42A

                if len(cols) > idx['model'] and target_model in cols[idx['model']]:

                    return {

                        "Announced": cols[idx['announced']] if idx['announced'] != -1 else "-",

                        "Available": cols[idx['available']] if idx['available'] != -1 else "-",

                        "Withdrawn": cols[idx['withdrawn']] if idx['withdrawn'] != -1 else "-",

                        "Discontinued": cols[idx['discontinued']] if idx['discontinued'] != -1 else "-"

                    }

        return None



    def run_for_file(self, config_path, title, is_first_file):

        self.results = [] # 清空上次結果

        with open(config_path, 'r', encoding='utf-8') as f:

            models = yaml.safe_load(f).get('models', [])



        with sync_playwright() as p:

            browser = p.chromium.launch(headless=True)

            page = browser.new_page()

            for model in models:

                try:

                    # 步驟 1: 使用您要求的搜尋 URL 格式

                    search_url = f"https://www.ibm.com/docs/en/search/Family%20{quote(model)}%2B?type=salesmanual"

                    logger.info(f"正在搜尋型號: {model} from {title}")

                    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

                    

                    # 步驟 2: 點擊第一個公告連結

                    link_locator = page.locator("a[href*='/announcements/']").first

                    link_locator.wait_for(state="attached", timeout=15000)

                    announce_url = link_locator.get_attribute("href")

                    if not announce_url.startswith("http"):

                        announce_url = f"https://www.ibm.com{announce_url}"

                    

                    # 步驟 3: 進入公告頁面並解析

                    logger.info(f"進入正確公告頁: {announce_url}")

                    page.goto(announce_url, wait_until="domcontentloaded", timeout=30000)

                    

                    if model == "9043-MRU":

                        with open("9043-mru.html", "w", encoding="utf-8") as f:

                            f.write(page.content())

                        logger.info("Saved HTML for 9043-MRU")

                    page.wait_for_selector("table", timeout=15000)

                    

                    data = self.parse_table(page.content(), model)

                    self.results.append({"Model": model, **(data if data else {})})

                except Exception as e:

                    logger.error(f"{model} 解析失敗: 找不到對應公告或表格")

                    self.results.append({"Model": model, "Announced": "N/A", "Available": "N/A", "Withdrawn": "N/A", "Discontinued": "N/A"})

            browser.close()

        self.display(title)

        self.write_to_readme(title, is_first_file)



    def run(self):

        yaml_files = sorted(glob.glob('*.yaml'))

        for i, yaml_file in enumerate(yaml_files):

            self.run_for_file(yaml_file, yaml_file, i == 0)



    def write_to_readme(self, title, is_first_file):

        headers = ["Model", "Announced", "Available", "Withdrawn", "Discontinued"]

        col_widths = {h: len(h) for h in headers}



        for r in self.results:

            for h in headers:

                cell_content = str(r.get(h, '-'))

                if len(cell_content) > col_widths[h]:

                    col_widths[h] = len(cell_content)



        md_table = f"## {title}\n\n"

        md_table += "| " + " | ".join([f"{h:<{col_widths[h]}}" for h in headers]) + " |\n"

        md_table += "|-" + "-|-".join(["-" * col_widths[h] for h in headers]) + "-|\n"



        for r in self.results:

            md_table += "| " + " | ".join([f"{str(r.get(h, '-')):<{col_widths[h]}}" for h in headers]) + " |\n"

        

        md_table += "\n"



        mode = 'w' if is_first_file else 'a'

        if is_first_file:

            md_table = "# IBM Power Hardware Lifecycle\n\n" + md_table



        with open('readme.md', mode, encoding='utf-8') as f:

            f.write(md_table)

        logger.info(f"結果已寫入 readme.md ({title})")



    def display(self, title):

        print(f"\n## {title}\n")

        header_str = f"{'Model':<12} | {'Announced':<12} | {'Available':<12} | {'Withdrawn':<12} | {'Discontinued'}"

        print("=" * len(header_str))

        print(header_str)

        print("-" * len(header_str))

        for r in self.results:

            print(f"{r['Model']:<12} | {r.get('Announced','-'):<12} | {r.get('Available','-'):<12} | {r.get('Withdrawn','-'):<12} | {r.get('Discontinued','-')}")

        print("=" * len(header_str))



if __name__ == "__main__":

    lifecycle = IBMLifecycleUniversalSRE()

    lifecycle.run()
