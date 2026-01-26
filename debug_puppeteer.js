/**
 * ==============================================================================
 * [SRE] IBM Lifecycle 稽核報表系統 (Early Exit & Persistent Module)
 * ==============================================================================
 */

const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const yaml = require('js-yaml');

// --- 輔助工具：日期轉 ISO 格式 ---
function toISODate(dateStr) {
    if (!dateStr || dateStr === '-' || dateStr.length < 4) return '-';
    try {
        const cleaned = dateStr.replace(/[()]/g, '').replace(/\//g, '-').trim();
        const d = new Date(cleaned);
        return isNaN(d.getTime()) ? dateStr : d.toISOString().split('T')[0];
    } catch (e) { return dateStr; }
}

async function runAuditReport() {
    // [模組] YAML 自動識別系統
    let allModels = [];
    try {
        const files = fs.readdirSync('./');
        files.filter(f => f.endsWith('.yaml') || f.endsWith('.yml')).forEach(file => {
            const doc = yaml.load(fs.readFileSync(path.join('./', file), 'utf8'));
            if (doc?.models) allModels = [...allModels, ...doc.models];
        });
        console.log(`[INIT] 已載入 YAML 型號，共計 ${allModels.length} 筆。`);
    } catch (err) {
        console.error(`[FATAL] YAML 讀取失敗: ${err.message}`);
        return;
    }

    const browser = await puppeteer.launch({ 
        headless: "new", 
        args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] 
    });

    const finalSummary = [];

    for (const targetModel of allModels) {
        console.log(`\n[INIT] 啟動 ${targetModel} 稽核任務 (優化模式: 尋獲即止)...`);
        const page = await browser.newPage();
        page.setDefaultNavigationTimeout(90000); 
        let finalResult = null;

        try {
            // --- 1. 搜尋階段 ---
            const searchUrl = `https://www.ibm.com/docs/en/search/${targetModel}?type=salesmanual`;
            console.log(`[SEARCH] 網址: ${searchUrl}`);
            await page.goto(searchUrl, { waitUntil: 'domcontentloaded' });
            
            try {
                await page.waitForSelector('.accordion-blue-title', { timeout: 45000 });
            } catch (e) {
                console.error(`[FAIL] 搜尋結果超時或無結果 (Model: ${targetModel})`);
                await page.close();
                continue;
            }

            const searchResults = await page.evaluate(() => {
                return Array.from(document.querySelectorAll('.accordion-blue-title')).map(row => {
                    const link = row.querySelector('a');
                    const parent = row.closest('.bx--search-result');
                    const lastUpdated = parent ? parent.innerText.match(/Last Updated:\s*([^\s|]+)/)?.[1] || 'N/A' : 'N/A';
                    return {
                        title: link ? link.innerText.trim() : 'N/A',
                        href: link ? "https://www.ibm.com" + link.getAttribute('href') : '',
                        lastUpdated
                    };
                }).filter(r => r.href);
            });

            // --- 2. 穿透式驗證階段 (尋獲即止) ---
            for (const res of searchResults) {
                const detailPage = await browser.newPage();
                try {
                    // [優化] 雙錨點切換與重試機制 (1次為限)
                    let auditUrl = `${res.href}#h2-smlcg`;
                    await detailPage.goto(auditUrl, { waitUntil: 'networkidle2', timeout: 60000 });

                    let hasData = await detailPage.evaluate(() => {
                        return !!document.querySelector('table') || !!document.querySelector('pre.pre');
                    });

                    if (!hasData) {
                        auditUrl = `${res.href}#lcg__title__1`;
                        console.log(`[TRACE] 頁面未發現表格，切換備援錨點重試: ${auditUrl}`);
                        await detailPage.goto(auditUrl, { waitUntil: 'networkidle2', timeout: 60000 });
                    }

                    const audit = await detailPage.evaluate((target) => {
                        const html = document.body.innerHTML;
                        const hasLabel = !!document.querySelector('.sales-manual-label');
                        const hasJsonLabel = html.includes('"content.salesManualLabel":"Sales%20manual"');
                        const smPass = hasLabel || hasJsonLabel;
                        let lifecycle = null;

                        // [優化選擇器] 軌道 A: 現代 Table (優先尋找 ID，若無 ID 則尋找包含 Type Model 的 table)
                        let table = document.querySelector('table[id="smlcg__lcg"]');
                        if (!table) {
                            table = Array.from(document.querySelectorAll('table')).find(t => t.innerText.includes('Type Model'));
                        }

                        if (table) {
                            const trs = Array.from(table.querySelectorAll('tr'));
                            for (let tr of trs) {
                                const cells = Array.from(tr.querySelectorAll('td')).map(c => c.innerText.trim());
                                if (cells[0] === target) {
                                    lifecycle = { model: cells[0], ann: cells[1], avl: cells[2], wdr: cells[3], dsc: cells[4] };
                                    break;
                                }
                            }
                        }

                        if (!lifecycle) {
                            const pres = Array.from(document.querySelectorAll('pre.pre'));
                            for (let pre of pres) {
                                const text = pre.innerText;
                                if (text.includes(target)) {
                                    const regex = new RegExp(`${target}\\s+([\\d/\\-]+)\\s+([\\d/\\-]+)\\s+([\\d/\\-]+)\\s+([\\d/\\-]+)`, "i");
                                    const match = text.match(regex);
                                    if (match) {
                                        lifecycle = { model: target, ann: match[1], avl: match[2], wdr: match[3], dsc: match[4] };
                                    }
                                }
                            }
                        }
                        return { smPass, lifecycle };
                    }, targetModel);

                    if (audit.smPass && audit.lifecycle) {
                        finalResult = {
                            'Model': audit.lifecycle.model,
                            'Announced': toISODate(audit.lifecycle.ann),
                            'Available': toISODate(audit.lifecycle.avl),
                            'Withdrawn': toISODate(audit.lifecycle.wdr),
                            'Discontinued': toISODate(audit.lifecycle.dsc),
                            'Status': '✅ PASS'
                        };
                        console.log(`[SUCCESS] 已找到精準匹配項目。`);
                        await detailPage.close();
                        break; 
                    }
                } catch (e) {
                    console.error(`[ERR] 處理頁面異常: ${res.title}`);
                } finally {
                    if (!detailPage.isClosed()) await detailPage.close();
                }
            }

            if (finalResult) {
                finalSummary.push(finalResult);
                console.table([finalResult]);
            } else {
                console.error(`[RESULT] ${targetModel} 未能找到規範資料。`);
            }

        } catch (err) {
            console.error(`[FATAL] 稽核崩潰: ${err.message}`);
        } finally {
            await page.close();
        }
    }

    // --- 3. 寫入 readme.md ---
    const mdHeader = `| Model | Announced | Available | Withdrawn | Discontinued | Status |\n| :--- | :--- | :--- | :--- | :--- | :--- |\n`;
    const mdRows = finalSummary.map(r => `| ${r.Model} | ${r.Announced} | ${r.Available} | ${r.Withdrawn} | ${r.Discontinued} | ${r.Status} |`).join('\n');
    fs.writeFileSync('readme.md', `# IBM Lifecycle Audit Report\n\nGenerated at: ${new Date().toLocaleString()}\n\n${mdHeader}${mdRows}`);
    
    console.log(`\n[FINISH] 報表已寫入 readme.md`);
    await browser.close();
}

runAuditReport();