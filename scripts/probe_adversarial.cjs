// 对抗交互回归探针（数据集无关）：专攻绘制/Esc/Tab 切换的边界情形，确保无残留状态、无 pageerror。
// 用法：node probe_adversarial.cjs <html-path>
const { chromium } = require('playwright');
const HTML = process.argv[2];
const fails = [];
const log = (...a) => console.log(...a);
function check(c, m) { if (!c) { fails.push(m); log('  ✗ ' + m); } else log('  ✓ ' + m); }

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  await page.goto('file://' + HTML, { waitUntil: 'load' });
  await page.waitForTimeout(1100);

  const NATIONAL = await page.evaluate(() => window.__fireRendered);
  log('[A] 空白 Esc 不应报错');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(150);
  check(pageErrors.length === 0, '空白 Esc 无 pageerror');

  log('[B] 画矩形 → Esc 取消（捕获阶段监听）');
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(150);
  await page.click('#areaDrawRect');
  await page.waitForTimeout(150);
  let drawing = await page.evaluate(() => document.body.classList.contains('map-draw'));
  check(drawing === true, '进入矩形绘制态（body.map-draw）');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(150);
  drawing = await page.evaluate(() => document.body.classList.contains('map-draw'));
  let lgShape = await page.evaluate(() => document.getElementById('lgShapeRow').style.display);
  check(drawing === false, 'Esc 后退出绘制态');
  check(lgShape === 'none', 'Esc 后形状图例隐藏');

  log('[C] 多边形 2 点 → Esc 取消');
  await page.click('#areaDrawPoly');
  await page.waitForTimeout(150);
  const box = await page.$eval('#map', el => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; });
  await page.mouse.click(box.x + box.w * 0.4, box.y + box.h * 0.4);
  await page.waitForTimeout(220);
  await page.mouse.click(box.x + box.w * 0.5, box.y + box.h * 0.5);
  await page.waitForTimeout(220);
  drawing = await page.evaluate(() => document.body.classList.contains('map-draw'));
  check(drawing === true, '进入多边形绘制态且记录顶点');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(150);
  drawing = await page.evaluate(() => document.body.classList.contains('map-draw'));
  check(drawing === false, '多边形 Esc 取消成功');

  log('[D] 绘制中切换 Tab 应取消绘制；行政区划下拉只在方式1 显示');
  await page.click('#areaDrawRect');
  await page.waitForTimeout(120);
  await page.click('#tabs .tab[data-tab="wms"]');
  await page.waitForTimeout(180);
  drawing = await page.evaluate(() => document.body.classList.contains('map-draw'));
  check(drawing === false, '切 Tab 后绘制态被取消');
  // v2.1.0：行政区划（方式1 概念）在方式2/3 下应隐藏
  const selHidden = await page.evaluate(() => {
    const el = document.getElementById('provSelect');
    return el.offsetParent === null;
  });
  check(selHidden === true, '方式3 下行政区划下拉已隐藏');
  const countryB0 = await page.evaluate(() => window.__countryBuilds);

  log('[E] 反复切换省份不应泄漏/崩坏（行政区划属于方式1，先切回方式1）');
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(200);
  const selShown = await page.evaluate(() => document.getElementById('provSelect').offsetParent !== null);
  check(selShown === true, '切回方式1 后行政区划下拉恢复显示');
  const provs = await page.$$eval('#provSelect option', els => els.map(e => e.value).filter(Boolean));
  const b0 = await page.evaluate(() => window.__csvBuilds);
  const seq = (provs.length ? provs : ['']).slice(0, 4);
  for (const p of seq) { await page.selectOption('#provSelect', p); await page.waitForTimeout(120); }
  await page.selectOption('#provSelect', '');
  await page.waitForTimeout(200);
  const b1 = await page.evaluate(() => window.__csvBuilds);
  const natl = await page.evaluate(() => window.__csvCount);
  const countryB1 = await page.evaluate(() => window.__countryBuilds);
  check(countryB1 === countryB0, '反复切省不再重建全国省界层（countryBuilds ' + countryB0 + ' → ' + countryB1 + '）');
  log('  builds: ' + b0 + ' -> ' + b1 + ' / national=' + NATIONAL + ' got=' + natl);
  check(natl === NATIONAL, '反复切换后全国仍 ' + NATIONAL + ' 点');

  log('[F] 列表「显示更多」耗尽后停住');
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(150);
  await page.selectOption('#provSelect', '');
  await page.waitForTimeout(150);
  let guard = 0;
  while (guard++ < 500) {
    const dis = await page.evaluate(() => document.getElementById('csvListMore').disabled);
    if (dis) break;
    await page.click('#csvListMore');
    await page.waitForTimeout(30);
  }
  const fin = await page.evaluate(() => ({
    count: document.querySelectorAll('#csvListBody .fpItem').length,
    meta: document.getElementById('csvListMeta').textContent,
    more: document.getElementById('csvListMore').textContent,
    moreDisabled: document.getElementById('csvListMore').disabled,
  }));
  log('  ' + JSON.stringify(fin) + ' (clicks=' + guard + ')');
  check(fin.count === NATIONAL, '列表最终渲染全部 ' + NATIONAL + ' 条');
  check(fin.moreDisabled === true && /已显示全部/.test(fin.more), '按钮禁用并显示「已显示全部」');

  log('[G] 方式2 切换火点产品（卫星）显示简介');
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(150);
  const sources = await page.$$eval('#areaSource option', els => els.map(e => e.value));
  if (sources.length > 1) {
    await page.selectOption('#areaSource', sources[sources.length - 1]);
    await page.waitForTimeout(150);
  }
  const info = await page.textContent('#areaSourceInfo');
  log('  info=' + (info || '').slice(0, 50));
  check(info && info.trim().length > 0, '产品切换显示简介');

  log('[H] 全程 pageerror 计数: ' + pageErrors.length);
  pageErrors.slice(0, 8).forEach(e => log('  • ' + e));
  check(pageErrors.length === 0, '全程无 pageerror');

  await browser.close();
  log('\n==== 结果 ====');
  if (fails.length) { log('发现 ' + fails.length + ' 个问题：'); fails.forEach(f => log(' - ' + f)); process.exit(1); }
  else log('✅ 对抗探针全绿');
})().catch(e => { console.error('探针异常：' + e); process.exit(3); });
