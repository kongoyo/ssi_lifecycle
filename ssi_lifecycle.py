import yaml
import pandas as pd
import os
from datetime import datetime

def run_lifecycle_automation(yaml_file, csv_file, output_file):
    # 1. 讀取 YAML 設定檔
    print(f"[*] 正在讀取設定檔: {yaml_file}")
    with open(yaml_file, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    target_models = config.get('models', [])
    if not target_models:
        print("[!] YAML 中未定義型號，任務終止。")
        return

    # 2. 讀取 CSV 數據
    print(f"[*] 正在載入資產清單: {csv_file}")
    df = pd.read_csv(csv_file)
    df['MTM_CLEAN'] = df['MTM'].astype(str).str.strip()

    # 標題映射 (縮短長度以符合整齊需求)
    header_map = {
        'General Availability #': 'GA #',
        'Withdrawn from Marketing': 'Withdrawn',
        'Transition to Extended/Sustained, End of Support': 'EOS Date',
        'Transition to End of Support Announce #': 'EOS Ann#',
        'Last modified': 'Modified'
    }

    # 3. 查詢與資料轉換
    results = []
    for model in target_models:
        search_mtm = model.replace("-", "")
        match = df[df['MTM_CLEAN'] == search_mtm]
        
        row_data = {"Model": model}
        if not match.empty:
            item = match.iloc[0]
            for full_h, short_h in header_map.items():
                val = item.get(full_h, "-")
                if pd.isna(val) or str(val).lower() == 'n/a':
                    row_data[short_h] = "-"
                else:
                    clean_val = str(val).split()[0]
                    # 日期格式化處理 (處理 Last modified 等格式)
                    try:
                        if "-" in clean_val:
                            dt = pd.to_datetime(clean_val)
                            clean_val = dt.strftime('%Y-%m-%d')
                    except: pass
                    row_data[short_h] = clean_val
        else:
            for short_h in header_map.values(): row_data[short_h] = "-"
        results.append(row_data)

    final_df = pd.DataFrame(results)

    # 4. 列印結果報表
    print("\n--- 查詢結果報表 ---")
    markdown_table = final_df.to_markdown(index=False)
    print(markdown_table)

    # 5. 寫入 readme.md
    print(f"\n[*] 正在寫入結果至 {output_file}")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"# IBM 硬體生命週期自動化報表\n\n")
        f.write(f"**生成時間:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**來源設定:** `{yaml_file}`\n\n")
        f.write(markdown_table)
        f.write("\n\n---\n*註解: GA#: 產品上市 | EOS Date: 終止支援日*")
    
    print("[+] 任務完成。")

if __name__ == "__main__":
    run_lifecycle_automation('test.yaml', 'ibm_product_lifecycle_list.csv', 'readme.md')