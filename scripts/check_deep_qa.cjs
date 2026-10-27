const path = require('path');
const fs = require('fs');
const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
const { chromium } = require(path.join(NODE_PATH, 'playwright'));

const HTML = 'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china/fire_map.html';
const SHOTS = 'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china/qa_shots';
fs.mkdirSync(SHOTS, { recursive: true });

const log = (...a) => console.log(...a);
const errors = [];

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE.ERR: ' + m.text()); });

  const t0 = Date.now();
  await page.goto('file://' + HTML, { waitUntil: 'load' });
  // 等待方式1列表首屏渲染
  await page.waitForFunction(() => {
    const b = document.getElementById('csvListBody');
    return b && b.children.length > 0;
  }, { timeout: 20000 }).catch(() => errors.push('TIMEOUT: csvListBody 未在 20s 内出现'));
  const loadMs = Date.now() - t0;

  // ---- 方式1 默认态 ----
  const csv = await page.evaluate(() => {
    const lc = document.getElementById('leftCol');
    const body = document.getElementById('csvListBody');
    const first = body && body.children[0];
    const firstTime = first ? (first.querySelector('.fpTime') ? first.querySelector('.fpTime').textContent : first.textContent.slice(0, 60)) : null;
    return {
      tab: window.__activeTab,
      leftColW: lc ? getComputedStyle(lc).width : null,
      csvRows: body ? body.children.length : -1,
      csvMeta: (document.getElementById('csvListMeta') || {}).textContent,
      firstTime,
      mapCanvas: document.querySelectorAll('#map canvas').length,
    };
  });
  await page.screenshot({ path: SHOTS + '/shot1_csv.png' });
  log('[方式1]', JSON.stringify(csv));

  // 点击列表首行，验证弹窗含时分秒格式
  let popupTime = null;
  try {
    await page.click('#csvListBody > div:first-child');
    await page.waitForTimeout(400);
    popupTime = await page.evaluate(() => {
      const p = document.querySelector('.leaflet-popup-content');
      return p ? p.textContent : null;
    });
  } catch (e) { errors.push('CLICK_ROW: ' + e.message); }
  log('[方式1弹窗]', popupTime ? popupTime.replace(/\s+/g, ' ').slice(0, 120) : '(无弹窗)');

  // ---- 切到方式2 ----
  await page.click('button.tab[data-tab="area"]');
  await page.waitForTimeout(500);
  const areaOpts = await page.evaluate(() => {
    const sel = document.getElementById('areaSource');
    return sel ? Array.from(sel.options).map(o => o.value) : [];
  });
  log('[方式2] 产品数=', areaOpts.length, areaOpts.join(','));

  // 遍历每个产品，记录简介
  const prodInfo = {};
  for (const v of areaOpts) {
    await page.selectOption('#areaSource', v);
    await page.waitForTimeout(120);
    const info = await page.evaluate(() => {
      const el = document.getElementById('areaSourceInfo');
      return el ? el.textContent.trim() : '(空)';
    });
    prodInfo[v] = info;
  }
  const missingInfo = Object.entries(prodInfo).filter(([k, v]) => !v || v.length < 4);
  log('[方式2] 缺简介产品=', JSON.stringify(missingInfo));
  await page.screenshot({ path: SHOTS + '/shot2_area.png' });

  // ---- 自由绘制：矩形 ----
  let drawResult = {};
  try {
    await page.click('#areaDrawRect');
    await page.waitForTimeout(150);
    const before = await page.evaluate(() => document.getElementById('areaShapeHint').textContent);
    const box = await page.$eval('#map', el => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    });
    // 在地图中心拖一个矩形
    const x0 = box.x + box.w * 0.3, y0 = box.y + box.h * 0.3;
    const x1 = box.x + box.w * 0.7, y1 = box.y + box.h * 0.7;
    await page.mouse.move(x0, y0); await page.mouse.down();
    await page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, { steps: 5 });
    await page.mouse.move(x1, y1, { steps: 5 }); await page.mouse.up();
    await page.waitForTimeout(300);
    const after = await page.evaluate(() => ({
      hint: document.getElementById('areaShapeHint').textContent,
      drawHint: document.getElementById('areaDrawHint').textContent,
      rectOnMap: document.querySelectorAll('#map .leaflet-overlay-pane path, #map .leaflet-overlay-pane rect').length,
    }));
    drawResult = { before, after };
  } catch (e) { errors.push('DRAW_RECT: ' + e.message); }
  log('[自由绘制矩形]', JSON.stringify(drawResult));
  await page.screenshot({ path: SHOTS + '/shot2b_draw.png' });

  // ---- 切到方式3（不联网，仅切tab）----
  await page.click('button.tab[data-tab="wms"]');
  await page.waitForTimeout(400);
  const wms = await page.evaluate(() => ({
    tab: window.__activeTab,
    hasPane: !!document.getElementById('pane-wms'),
    csvCanvasStill: document.querySelectorAll('#map canvas').length,
  }));
  await page.screenshot({ path: SHOTS + '/shot3_wms.png' });
  log('[方式3]', JSON.stringify(wms));

  // ---- 回到方式1，测试列表翻页 ----
  await page.click('button.tab[data-tab="csv"]');
  await page.waitForTimeout(300);
  let moreInfo = {};
  try {
    for (let i = 0; i < 5; i++) {
      const disabled = await page.evaluate(() => document.getElementById('csvListMore').disabled);
      if (disabled) { moreInfo.stoppedAt = i; break; }
      await page.click('#csvListMore');
      await page.waitForTimeout(150);
    }
    moreInfo = { ...moreInfo, finalRows: await page.evaluate(() => document.getElementById('csvListBody').children.length) };
  } catch (e) { errors.push('PAGING: ' + e.message); }
  log('[列表翻页]', JSON.stringify(moreInfo));

  // ---- 性能 ----
  log('[性能] csvListBody 首屏出现耗时 ≈', loadMs, 'ms');

  log('\\n===== ERRORS (' + errors.length + ') =====');
  errors.forEach(e => log(' - ' + e));

  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
