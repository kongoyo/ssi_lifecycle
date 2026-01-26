/**
 * ==============================================================================
 * [SRE] IBM Announcement Raw Data 擷取工具 - 強化容錯版
 * ==============================================================================
 */

const puppeteer = require('puppeteer');
const fs = require('fs');

async function exportRawDataWithRetry() {
    const targetUrl = 'https://www.ibm.com/docs/en/announcements/disk-array-subsystem-model-480?region=AP#h2-smlcg';
    let browser;
    
    try {
        browser = await puppeteer.launch({ 
            headless: "new",
            args: [
                '--no-sandbox', 
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu'
            ] 
        });

        const page = await browser.newPage();

        // [模組優化] 封鎖非文字資源以避免 Timeout
        await page.setRequestInterception(true);
        page.on('request', (req) => {
            if (['image', 'stylesheet', 'font'].includes(req.resourceType())) {
                req.abort();
            } else {
                req.continue();
            }
        });

        console.log(`[TRACE] 嘗試開啟網址: ${targetUrl}`);

        // [修復邏輯] 使用較寬鬆的加載判定條件
        await page.goto(targetUrl, { 
            waitUntil: 'domcontentloaded', 
            timeout: 90000 // 增加至 90 秒
        });

        // 確保動態表格內容有時間渲染
        await new Promise(r => setTimeout(r, 8000));

        const rawData = await page.evaluate(() => {
            return {
                html: document.documentElement.innerHTML,
                text: document.body.innerText,
                stats: {
                    tableCount: document.querySelectorAll('table').length,
                    timestamp: new Date().toISOString()
                }
            };
        });

        // 寫入檔案
        fs.writeFileSync('raw_full_inner_html.html', rawData.html);
        fs.writeFileSync('raw_inner_text.txt', rawData.text);
        
        console.log(`[SUCCESS] Raw Data 擷取成功。`);
        console.log(`- HTML 檔已產生: raw_full_inner_html.html`);
        console.log(`- 表格計數: ${rawData.stats.tableCount}`);

    } catch (err) {
        console.error(`[FATAL] 擷取失敗: ${err.message}`);
        // 若失敗，建議檢查網路或改用更輕量的 headless 模式
    } finally {
        if (browser) await browser.close();
    }
}

exportRawDataWithRetry();