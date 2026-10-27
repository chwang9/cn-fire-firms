/**
 * cn-fire-firms 端到端实测 —— 数据下载 + 方式2 时间范围（1~10 天）+ 面板样式作用域
 *
 * 覆盖 v0.6.0 / v0.6.1 新增与修复的行为：
 *   1) 面板控件样式生效（v0.5.0 遗留：#wmsPanel 规则落空，输入框没撑满面板）
 *   2) 方式2 天数 1..10 + 起始日 → 自动拆成多段请求（单次上限 5）
 *   3) 方式2 数据缓存：切 Tab 回来数据还在、切省清空
 *   4) 方式1 / 方式2 的 CSV 与 GeoJSON 下载（真实拦截下载内容校验）
 *
 * 用法：node scripts/check_download_e2e.cjs [fire_map.html 路径]
 * 需要 MAP_KEY：环境变量 FIRMS_MAP_KEY（缺省用公开示例 key）
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
let chromium;
try {
  ({ chromium } = require(path.join(NODE_PATH, 'playwright')));
} catch (e) {
  console.log('! 未找到 playwright，跳过下载端到端实测');
  process.exit(0);
}

const PY = process.env.FIRMS_PY || 'c:/Users/wangch/.workbuddy/binaries/python/versions/3.13.12/python.exe';
const SERVE = path.join(__dirname, 'serve_fires.py');
const KEY = process.env.FIRMS_MAP_KEY || '243e05a6877257980f3aa8cfce4996e4';
const DEFAULT_HTML = 'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china/fire_map.html';
const htmlPath = process.argv[2] || DEFAULT_HTML;

let fail = 0;
const check = (cond, msg) => { console.log((cond ? '  ✓ ' : '  ✗ ') + msg); if (!cond) fail++; };

async function grabDownload(page, btnSel) {
  const [dl] = await Promise.all([
    page.waitForEvent('download', { timeout: 20000 }),
    page.click(btnSel),
  ]);
  const p = await dl.path();
  const txt = fs.readFileSync(p, 'utf8');
  return { name: dl.suggestedFilename(), text: txt };
}

(async () => {
  const mapDir = path.dirname(path.resolve(htmlPath));
  const fileName = path.basename(htmlPath);
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  await page.goto('file:///' + path.resolve(htmlPath).replace(/\\/g, '/'), { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);

  console.log('1) 面板控件样式作用域（v0.5.0 遗留 bug 回归）');
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(500);
  const style = await page.evaluate(() => {
    const pane = document.querySelector('.tabPane.on');
    const pb = pane.getBoundingClientRect();
    const cs = getComputedStyle(pane);
    const inner = pb.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
      - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth);
    const k = document.getElementById('firmsKey').getBoundingClientRect();
    const b = document.getElementById('areaApply').getBoundingClientRect();
    return { inner: Math.round(inner), keyW: Math.round(k.width), btnW: Math.round(b.width) };
  });
  check(Math.abs(style.keyW - style.inner) <= 2,
    `MAP_KEY 输入框撑满面板：${style.keyW}px / 可用 ${style.inner}px`);
  check(style.btnW > style.inner * 0.8, `按钮 flex 平分：${style.btnW}px / ${style.inner}px`);

  console.log('\n2) 方式2 时间范围：1..10 天自动拆段');
  const plan = await page.evaluate(() => ({
    single: AREA_MAX_SINGLE, win: AREA_MAX_WINDOW,
    p1: planAreaRequests(1, ''), p5: planAreaRequests(5, ''),
    p6: planAreaRequests(6, ''), p10: planAreaRequests(10, ''),
    pd: planAreaRequests(10, '2026-10-17'),
  }));
  check(plan.single === 5 && plan.win === 10, `拆段上限常量：单段 ${plan.single} 天 / 窗口 ${plan.win} 天`);
  check(plan.p1.length === 1 && plan.p5.length === 1, '1、5 天各 1 次请求');
  check(plan.p6.length === 2, `6 天拆为 ${plan.p6.length} 次`);
  check(plan.p10.length === 2 && plan.p10[0].days === 5 && plan.p10[1].days === 5,
    `10 天拆为 2 段：${plan.p10.map(p => p.date + '×' + p.days).join('、')}`);
  check(plan.pd[0].date === '2026-10-17' && plan.pd[1].date === '2026-10-22',
    `指定起始日后按起始日切段：${plan.pd.map(p => p.date).join(' → ')}`);
  const hint = await page.evaluate(() => {
    document.getElementById('areaDays').value = '10';
    document.getElementById('areaDays').dispatchEvent(new Event('input'));
    return document.getElementById('areaPlanHint').textContent;
  });
  check(/拆为 2 次请求/.test(hint), '界面提示拆段次数：' + hint);

  console.log('\n3) 方式2 实拉（10 天 / 2 段）');
  const PORT = await page.evaluate(() => AREA_PROXY_PORT);
  const srv = spawn(PY, [SERVE, '--dir', mapDir, '--file', fileName, '--port', String(PORT)], { stdio: 'ignore' });
  await new Promise(r => setTimeout(r, 2500));
  const health = await page.evaluate(async (port) => {
    try { const r = await fetch(`http://127.0.0.1:${port}/api/fires/health`, { cache: 'no-store' }); return r.ok; }
    catch (e) { return false; }
  }, PORT);
  check(health, `本地代理在 ${PORT} 端口就绪`);
  if (!health) { srv.kill(); await browser.close(); process.exit(1); }

  // 选一个省 → fitBounds 把视野对准该省（v2.1.0 起 bbox=视野∩中国，选省即缩视野，效果不变）
  const target = await page.evaluate(() => {
    const opts = Array.from(document.getElementById('provSelect').options).filter(o => o.value);
    let best = null, bestN = -1;
    for (const o of opts) {
      const n = parseInt(o.textContent.replace(/[^0-9]/g, ''), 10) || 0;
      if (n > bestN) { bestN = n; best = o.value; }
    }
    return best || '';
  });
  // v2.1.0：行政区划下拉只在方式1 显示——先切回方式1 选省（fitBounds 缩视野），再进方式2
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(300);
  if (target) { await page.selectOption('#provSelect', target); await page.waitForTimeout(900); }
  await page.click('#tabs .tab[data-tab="area"]');
  await page.fill('#firmsKey', KEY);
  await page.evaluate(() => { document.getElementById('areaDays').value = '10'; });
  await page.click('#areaApply');
  await page.waitForFunction(() => {
    const t = document.getElementById('wmsStatus').textContent;
    return t.length > 0 && !t.includes('拉取中') && !t.includes('正在连接');
  }, null, { timeout: 180000 });
  await page.waitForTimeout(500);
  const area = await page.evaluate(() => ({
    status: document.getElementById('wmsStatus').textContent.split('\n')[0],
    fetched: window.__areaFetched,
    urls: document.getElementById('areaReqUrl').textContent,
    dlEnabled: !document.getElementById('areaDlCsv').disabled,
  }));
  console.log('     ' + area.status);
  check(area.fetched > 0, `拉到 ${area.fetched} 个火点`);
  check(/去重合并/.test(area.status), '状态栏告知多段已去重合并');
  const segs = (area.urls.match(/请求：/g) || []).length;
  check(segs === 2, `实际发出 2 段请求（URL 区显示 ${segs} 条）`);
  check(area.dlEnabled, '方式2 下载按钮已启用');

  console.log('\n4) 方式2 数据缓存：切 Tab 回来不丢');
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(600);
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(600);
  const back = await page.evaluate(() => window.__areaFetched);
  check(back === area.fetched, `切回方式2 数据仍在：${back} 个（原 ${area.fetched}）`);

  // 注意：必须先做下载再测「切省清空」——切省会把缓存清空并禁用按钮，
  // 顺序颠倒会点在一个 disabled 按钮上（表现为等待下载事件超时，而非明确的失败）。
  console.log('\n5) 顶部统计随 Tab 切换口径（v0.6.1：原先永远显示方式1 的点数）');
  const stat = {};
  for (const t of ['csv', 'area', 'wms']) {
    await page.click(`#tabs .tab[data-tab="${t}"]`);
    await page.waitForTimeout(400);
    stat[t] = await page.evaluate(() => ({
      txt: document.getElementById('provStat').textContent.trim(),
      tip: document.getElementById('provStat').title,
      csv: window.__csvCount, area: window.__areaFetched,
    }));
    console.log(`     [${t}] ${stat[t].txt}`);
  }
  check(/24h CSV/.test(stat.csv.txt) && stat.csv.txt.indexOf(String(stat.csv.csv)) >= 0,
    `方式1 标注入来源且计数一致（${stat.csv.csv}）`);
  check(/Area API/.test(stat.area.txt) && stat.area.txt.indexOf(String(area.fetched)) >= 0,
    `方式2 标注来源且计数一致（${area.fetched}）`);
  check(/WMS 栅格/.test(stat.wms.txt), '方式3 标注为栅格、不伪装成点数');
  check(new Set([stat.csv.txt, stat.area.txt, stat.wms.txt]).size === 3,
    '三个 Tab 的统计文本互不相同（不再是同一个数）');
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(400);

  console.log('\n6) 数据下载：方式2 CSV / GeoJSON');
  if (area.fetched > 0) {
    const d1 = await grabDownload(page, '#areaDlCsv');
    check(/\.csv$/.test(d1.name), 'CSV 文件名：' + d1.name);
    check(/^longitude,latitude/.test(d1.text.replace(/^\ufeff/, '')), 'CSV 表头正确');
    const lines = d1.text.replace(/^\ufeff/, '').trim().split(/\r?\n/);
    check(lines.length - 1 === area.fetched, `CSV 行数 ${lines.length - 1} = 火点数 ${area.fetched}`);
    const d2 = await grabDownload(page, '#areaDlGeo');
    let gj = null;
    try { gj = JSON.parse(d2.text.replace(/^\ufeff/, '')); } catch (e) {}
    check(/\.geojson$/.test(d2.name), 'GeoJSON 文件名：' + d2.name);
    check(!!gj && gj.type === 'FeatureCollection', 'GeoJSON 结构合法');
    check(!!gj && gj.features.length === area.fetched, `要素数 ${gj ? gj.features.length : 0} 与火点数一致`);
  } else {
    check(false, '无数据可下载（跳过）');
  }

  console.log('\n7) 切省应清空方式2 数据（v2.1.0：换省在方式1 进行，方式2 数据随 onProvinceChange 作废）');
  const before = await page.evaluate(() => window.__areaFetched);
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(300);
  await page.selectOption('#provSelect', '');
  await page.waitForTimeout(900);
  const cleared = await page.evaluate(() => ({
    fetched: window.__areaFetched,
    dlEnabled: !document.getElementById('areaDlCsv').disabled,
  }));
  check(before > 0 && cleared.fetched === 0, `切省清空：${before} → ${cleared.fetched}`);
  check(!cleared.dlEnabled, '清空后下载按钮已禁用');

  console.log('\n8) 数据下载：方式1 按当前省范围');
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(500);
  if (target) { await page.selectOption('#provSelect', target); await page.waitForTimeout(900); }
  const scopeN = await page.evaluate(() => window.__csvCount);
  const stat2 = await page.evaluate(() => document.getElementById('provStat').textContent.trim());
  check(stat2.indexOf(String(scopeN)) >= 0, `切省后顶部统计同步为 ${scopeN}（${stat2}）`);
  const c1 = await grabDownload(page, '#csvDlCsv');
  const rows = c1.text.replace(/^\ufeff/, '').trim().split(/\r?\n/);
  check(rows[0].indexOf('province') >= 0, 'CSV 含 province 列');
  check(rows.length - 1 === scopeN, `导出 ${rows.length - 1} 行 = 当前范围火点数 ${scopeN}`);
  check(rows.length > 1 && rows[1].indexOf(target) >= 0, `行内 province 标注为 ${target}`);
  const c2 = await grabDownload(page, '#csvDlGeo');
  let g2 = null;
  try { g2 = JSON.parse(c2.text.replace(/^\ufeff/, '')); } catch (e) {}
  check(!!g2 && g2.features.length === scopeN, `GeoJSON 要素数 ${g2 ? g2.features.length : 0} = ${scopeN}`);

  srv.kill();
  console.log('\npageerrors:', errors.length ? errors : 'none');
  if (errors.length) fail++;
  console.log(fail === 0 ? '✅ 下载 / 时间范围 / 样式回归 端到端全部通过' : `❌ ${fail} 项未通过`);
  await browser.close();
  process.exit(fail === 0 ? 0 : 1);
})();
