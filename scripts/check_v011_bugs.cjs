/**
 * cn-fire-firms v0.10.1 两项 bug 修复 · 端到端守卫
 * Bug A：初始「方式1」下图例不得显示「Area API 实时点」（初始化不走 switchTab 导致串台）
 * Bug B：方式2 绘制形状切到方式1/3 后不得残留在地图上；切回方式2 原样恢复
 * 两个探针的检测函数均在修复前的坏状态下实测返回过坏值（display:flex / shape:true），
 * 即守卫灵敏度已被反向证实，不是空转断言。
 */
const path = require('path');
const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
let chromium;
try { ({ chromium } = require(path.join(NODE_PATH, 'playwright'))); }
catch (e) { console.log('! 未找到 playwright，跳过'); process.exit(0); }

const HTML = process.argv[2] || 'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china/fire_map.html';
let pass = 0, fail = 0;
const ok = (name, cond) => { cond ? pass++ : fail++; console.log((cond ? '  ✓ ' : '  ✗ ') + name); };

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errs = [];
  page.on('pageerror', e => errs.push(e.message));
  await page.goto('file://' + HTML, { waitUntil: 'load' });
  await page.waitForFunction(() => document.getElementById('csvListBody') && document.getElementById('csvListBody').children.length > 0, { timeout: 20000 });

  // ---- Bug A：初始态图例 ----
  const a = await page.evaluate(() => {
    const el = document.getElementById('lgAreaRow');
    return { display: getComputedStyle(el).display, visible: el.offsetWidth > 0 && el.offsetHeight > 0 };
  });
  ok('A1 初始 lgAreaRow display=none', a.display === 'none');
  ok('A2 初始 lgAreaRow 不可见', a.visible === false);

  // ---- Bug B：绘制形状跨 Tab ----
  await page.click('button.tab[data-tab="area"]'); await page.waitForTimeout(300);
  await page.click('#areaDrawRect'); await page.waitForTimeout(150);
  const box = await page.$eval('#map', el => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; });
  await page.mouse.move(box.x + box.w * 0.3, box.y + box.h * 0.3); await page.mouse.down();
  await page.mouse.move(box.x + box.w * 0.7, box.y + box.h * 0.7, { steps: 5 }); await page.mouse.up();
  await page.waitForTimeout(300);
  const s1 = await page.evaluate(() => window.__layerState());
  ok('B1 方式2绘制后 shape=true', s1.shape === true);

  await page.click('button.tab[data-tab="wms"]'); await page.waitForTimeout(400);
  const s2 = await page.evaluate(() => ({
    st: window.__layerState(),
    lgShape: document.getElementById('lgShapeRow').style.display,
    lgArea: document.getElementById('lgAreaRow').style.display,
  }));
  ok('B2 切方式3后 shape=false（不残留）', s2.st.shape === false);
  ok('B3 切方式3后图例「已绘制的关注区域」隐藏', s2.lgShape === 'none');
  ok('B4 切方式3后图例「Area API 实时点」隐藏', s2.lgArea === 'none');

  await page.click('button.tab[data-tab="area"]'); await page.waitForTimeout(400);
  const s3 = await page.evaluate(() => ({
    st: window.__layerState(),
    lgShape: document.getElementById('lgShapeRow').style.display,
    hint: document.getElementById('areaShapeHint').textContent,
  }));
  ok('B5 切回方式2形状恢复 shape=true', s3.st.shape === true);
  ok('B6 切回方式2图例行恢复', s3.lgShape !== 'none');
  ok('B7 绘制范围数据保留（hint 仍含「矩形」）', s3.hint.indexOf('矩形') >= 0);

  await page.click('button.tab[data-tab="csv"]'); await page.waitForTimeout(300);
  const s4 = await page.evaluate(() => window.__layerState());
  ok('B8 切方式1后 shape=false', s4.shape === false);
  ok('B9 切方式1后 csv=true', s4.csv === true);

  ok('E1 全程 pageerror=0', errs.length === 0);
  errs.forEach(e => console.log('  ! pageerror:', e));

  console.log('\n结果: ' + pass + ' 通过 / ' + fail + ' 失败');
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('FATAL', e); process.exit(1); });
