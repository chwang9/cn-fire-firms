/**
 * cn-fire-firms 布局回归检测（无头浏览器实测，不做字符串断言）
 *
 * 覆盖三种取数方式 Tab + 省级下拉的各状态：
 *   - 全国视图 / 单省视图
 *   - 方式1 CSV / 方式2 Area API / 方式3 WMS 三个 Tab
 *   - 状态栏多行长文本
 *   - 多种视口尺寸
 *
 * 用法：node scripts/check_layout.cjs <fire_map.html 路径>
 */
const path = require('path');

const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
let chromium;
try {
  ({ chromium } = require(path.join(NODE_PATH, 'playwright')));
} catch (e) {
  console.log('! 未找到 playwright，跳过布局检测（不影响技能功能）');
  process.exit(0);
}

const DEFAULT_HTML = 'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china/fire_map.html';
const htmlPath = process.argv[2] || DEFAULT_HTML;
const VIEWPORTS = [
  [1920, 1080], [1440, 900], [1280, 800], [1024, 768],
  [820, 1180], [640, 900], [1920, 600],
];

const TARGETS = {
  '#topbar': '顶部工具栏',
  '#provSelect': '省份下拉',
  '#tabs': 'Tab 栏',
  '#leftCol': '左侧列',
  '#legend': '图例',
  '.leaflet-control-zoom': '缩放控件',
  '.leaflet-control-layers': '图层控件',
  '.leaflet-control-scale': '比例尺',
  '.leaflet-control-attribution': '版权',
};

async function measure(page) {
  return page.evaluate((targets) => {
    const items = [];
    for (const [sel, name] of Object.entries(targets)) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const b = el.getBoundingClientRect();
      if (!b.width && !b.height) continue;
      items.push({ name, sel, el, x: Math.round(b.left), y: Math.round(b.top),
        w: Math.round(b.width), h: Math.round(b.height),
        r: Math.round(b.right), b: Math.round(b.bottom) });
    }
    const overlaps = [];
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i], b = items[j];
        if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
        const ox = Math.min(a.r, b.r) - Math.max(a.x, b.x);
        const oy = Math.min(a.b, b.b) - Math.max(a.y, b.y);
        if (ox > 0 && oy > 0) overlaps.push({ a: a.name, b: b.name, w: ox, h: oy });
      }
    }
    const oob = items
      .filter(i => i.x < 0 || i.y < 0 || i.r > innerWidth || i.b > innerHeight)
      .map(i => i.name);
    return { vw: innerWidth, vh: innerHeight, items, overlaps, oob };
  }, TARGETS);
}

function report(res, label) {
  const bad = res.overlaps.length || res.oob.length;
  console.log(`${bad ? '✗' : '✓'} ${label}  (${res.vw}x${res.vh})`);
  if (bad) {
    res.items.forEach(i => console.log(`    ${i.name.padEnd(12)} x:${i.x} y:${i.y} w:${i.w} h:${i.h} 右:${i.r} 下:${i.b}`));
  }
  res.overlaps.forEach(o => console.log(`    ✗ 重叠 ${o.a} × ${o.b}  ${o.w}×${o.h}px`));
  res.oob.forEach(n => console.log(`    ✗ 越界 ${n}`));
  return bad === 0;
}

(async () => {
  const url = 'file:///' + htmlPath.replace(/\\/g, '/');
  const browser = await chromium.launch({ headless: true });
  let pass = 0, fail = 0;
  const errors = [];

  for (const [w, h] of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: w, height: h } });
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    if (report(await measure(page), `全国视图 ${w}x${h}`)) pass++; else fail++;

    // 切到某省（会触发 fitBounds，改变各控件位置）
    const has = await page.$('#provSelect');
    if (has) {
      const opt = await page.evaluate(() => {
        const o = Array.from(document.getElementById('provSelect').options).find(x => x.value);
        return o ? o.value : '';
      });
      if (opt) {
        await page.selectOption('#provSelect', opt);
        await page.waitForTimeout(1200);
        if (report(await measure(page), `单省视图(${opt}) ${w}x${h}`)) pass++; else fail++;
      }
    }
    await page.close();
  }

  // 三个 Tab 各测一次（面板高度不同）
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  for (const tab of ['csv', 'area', 'wms']) {
    await page.click(`#tabs .tab[data-tab="${tab}"]`);
    await page.waitForTimeout(600);
    if (report(await measure(page), `Tab=${tab} 1280x900`)) pass++; else fail++;
  }
  // 状态栏多行长文本
  await page.evaluate(() => {
    const el = document.getElementById('wmsStatus');
    if (el) el.textContent = '拉取中… 97.5280,21.1488,106.1890,29.6386 …\n范围较大，可能需 1 分钟以上（建议先在上方选择具体省份）';
  });
  await page.waitForTimeout(400);
  if (report(await measure(page), '状态栏多行长文本 1280x900')) pass++; else fail++;
  await page.close();

  console.log('\n' + '-'.repeat(52));
  console.log(`布局检测：通过 ${pass} / 失败 ${fail}`);
  if (errors.length) console.log('pageerror:', errors);
  if (fail === 0) console.log('✅ 全部视口/状态无重叠、无越界');
  else console.log('❌ 存在重叠或越界，需调整面板排版');
  await browser.close();
  process.exit(fail === 0 ? 0 : 1);
})();
