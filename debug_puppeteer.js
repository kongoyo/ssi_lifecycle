/**
 * ==============================================================================
 * [SRE] IBM Lifecycle 稽核報表系統 (Early Exit & Self-Healing & Persistence)
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
    // 1. 讀取 YAML 任務清單
    let allModels = [];
    try {
        const files = fs.readdirSync('./');
        files.filter(f => f.endsWith('.yaml') || f.endsWith('.yml')).forEach(file => {
            const doc = yaml.load(fs.readFileSync(path.join('./', file), 'utf8'));
            if (doc?.models) allModels = [...allModels, ...doc.models];
        });
    } catch (err) {
        console.error(`[FATAL] YAML 讀取失敗: ${err.message}`);
        return;
    }

    // 2. [斷點續傳邏輯] 讀取現有 readme.md，解析已存數據
    let finishedModels = new Set();
    let finalSummary = [];
    const readmePath = 'readme.md';
    
    if (fs.existsSync(readmePath)) {
        const content = fs.readFileSync(readmePath, 'utf8');
        const lines = content.split('\n');
        lines.forEach(line => {
            if (line.includes('|') && !line.includes('Model') && !line.includes('---')) {
                const parts = line.split('|').map(p => p.trim());
                if (parts[1]) {
                    // 僅 ✅ PASS 被視為完成，下次跳過
                    if (line.includes('✅ PASS')) {
                        finishedModels.add(parts[1]);
                    }
                    // 將所有記錄（含 FAIL）恢復到陣列中，維持報表完整性
                    finalSummary.push({
                        'Model': parts[1],
                        'Announced': parts[2],
                        'Available': parts[3],
                        'Withdrawn': parts[4],
                        'Discontinued': parts[5],
                        'Status': parts[6]
                    });
                }
            }
        });
        console.log(`[CHECKPOINT] 偵測到斷點，已跳過 ${finishedModels.size} 筆成功項目。`);
    }

    const launchBrowser = async () => {
        return await puppeteer.launch({ 
            headless: "new", 
            args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--js-flags="--max-old-space-size=1024"'] 
        });
    };

    let browser = await launchBrowser();

    for (const targetModel of allModels) {
        // [斷點續傳] 跳過已 PASS 項目
        if (finishedModels.has(targetModel)) continue;

        if (!browser.connected) {
            console.warn(`[RECOVERY] 偵測到連線中斷，正在重新啟動瀏覽器實例...`);
            browser = await launchBrowser();
        }

        console.log(`\n[INIT] 啟動 ${targetModel} 稽核任務 (優化模式: 尋獲即止)...`);
        
        let page;
        try {
            page = await browser.newPage();
            page.setDefaultNavigationTimeout(90000); 
            let finalResult = null;

            const searchUrl = `https://www.ibm.com/docs/en/search/${targetModel}?type=salesmanual`;
            console.log(`[SEARCH] 網址: ${searchUrl}`);
            await page.goto(searchUrl, { waitUntil: 'domcontentloaded' });
            
            try {
                await page.waitForSelector('.accordion-blue-title', { timeout: 45000 });
            } catch (e) {
                console.error(`[FAIL] 搜尋結果超時或無結果 (Model: ${targetModel})`);
                finalResult = { 'Model': targetModel, 'Announced': '-', 'Available': '-', 'Withdrawn': '-', 'Discontinued': '-', 'Status': '❌ FAIL' };
            }

            if (!finalResult) {
                const searchResults = await page.evaluate(() => {
                    return Array.from(document.querySelectorAll('.accordion-blue-title')).map(row => {
                        const link = row.querySelector('a');
                        const parent = row.closest('.bx--search-result');
                        const lastUpdated = parent ? parent.innerText.match(/Last Updated:\s*([^\s|]+)/)?.[1] || 'N/A' : 'N/A';
                        return { title: link ? link.innerText.trim() : 'N/A', href: link ? "https://www.ibm.com" + link.getAttribute('href') : '', lastUpdated };
                    }).filter(r => r.href);
                });

                for (const res of searchResults) {
                    const detailPage = await browser.newPage();
                    try {
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
                                        if (match) { lifecycle = { model: target, ann: match[1], avl: match[2], wdr: match[3], dsc: match[4] }; }
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
                            await detailPage.close().catch(() => {});
                            break; 
                        }
                    } catch (e) {
                        console.error(`[ERR] 處理分頁異常: ${res.title}`);
                    } finally {
                        if (detailPage && !detailPage.isClosed()) { await detailPage.close().catch(() => {}); }
                    }
                }
            }

            // [斷點續傳] 更新結果陣列並寫入硬碟
            if (!finalResult) {
                finalResult = { 'Model': targetModel, 'Announced': '-', 'Available': '-', 'Withdrawn': '-', 'Discontinued': '-', 'Status': '❌ FAIL' };
            }

            // 若原本已有 FAIL 記錄，先移除舊的再推入新的
            finalSummary = finalSummary.filter(item => item.Model !== targetModel);
            finalSummary.push(finalResult);
            console.table([finalResult]);
            updateReadme(finalSummary);

        } catch (err) {
            console.error(`[FATAL] 型號 ${targetModel} 處理崩潰: ${err.message}`);
        } finally {
            if (page && !page.isClosed()) { await page.close().catch(() => {}); }
        }
    }

    console.log(`\n[FINISH] 稽核任務全數處理完成。`);
    if (browser && browser.connected) await browser.close();
}

function updateReadme(dataArray) {
    const mdHeader = `| Model | Announced | Available | Withdrawn | Discontinued | Status |\n| :--- | :--- | :--- | :--- | :--- | :--- |\n`;
    const mdRows = dataArray.map(r => `| ${r.Model} | ${r.Announced} | ${r.Available} | ${r.Withdrawn} | ${r.Discontinued} | ${r.Status} |`).join('\n');
    fs.writeFileSync('readme.md', `# IBM Lifecycle Audit Report\n\nGenerated at: ${new Date().toLocaleString()}\n\n${mdHeader}${mdRows}`);
}

runAuditReport();