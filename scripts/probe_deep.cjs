// 深度交互回归探针（数据集无关）：加载生成的 HTML，全程捕获 console/page 错误，
// 并遍历每条交互路径，断言不变量。可在任意数据集（含 selftest 合成数据）上运行。
// 用法：node probe_deep.cjs <html-path>
const { chromium } = require('playwright');

const HTML = process.argv[2];
if (!HTML) { console.error('usage: node probe_deep.cjs <html-path>'); process.exit(2); }

const fails = [];
const log = (...a) => console.log(...a);
function check(cond, msg) { if (!cond) { fails.push(msg); log('  ✗ ' + msg); } else { log('  ✓ ' + msg); } }

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ acceptDownloads: true });
  const page = await ctx.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', e => pageErrors.push(e.message));

  log('Loading ' + HTML);
  await page.goto('file://' + HTML, { waitUntil: 'load' });
  await page.waitForTimeout(1200);

  // 1. 初始化
  log('\n[1] 初始化');
  const init = await page.evaluate(() => ({
    rendered: window.__fireRendered, builds: window.__csvBuilds,
    items: window.__csvItems, csvCount: window.__csvCount,
    tab: window.__activeTab, prov: window.__province,
  }));
  const NATIONAL = init.rendered;   // 全国火点总数，后续校验切回全国应恢复
  log('  ' + JSON.stringify(init));
  check(NATIONAL > 0, '方式1 初始渲染火点 > 0');
  check(init.builds === 1, '方式1 初始仅构建 1 次图层');
  check(init.tab === 'csv', '初始 Tab 为 csv');
  check(init.prov === '', '初始为全国视图');
  check(pageErrors.length === 0, '初始化无 pageerror');

  // 2. 切到一个真实存在的省份
  log('\n[2] 切换省份');
  const provVal = await page.$$eval('#provSelect option', els => {
    const o = els.find(e => e.value);
    return o ? o.value : '';
  });
  if (provVal) {
    await page.selectOption('#provSelect', provVal);
    await page.waitForTimeout(700);
    const after = await page.evaluate(() => ({ builds: window.__csvBuilds, prov: window.__province, csvCount: window.__csvCount }));
    log('  ' + JSON.stringify(after));
    check(after.prov === provVal, '省份切换生效: ' + provVal);
    check(after.csvCount > 0, '该省有火点数 (>0)');
    // 切回全国
    await page.selectOption('#provSelect', '');
    await page.waitForTimeout(500);
    const back = await page.evaluate(() => ({ builds: window.__csvBuilds, csvCount: window.__csvCount }));
    log('  ' + JSON.stringify(back));
    check(back.csvCount === NATIONAL, '切回全国恢复 ' + NATIONAL + ' 点');
  } else {
    log('  (无可选省份，跳过)');
  }

  // 3. Tab 互斥 + 不重建
  log('\n[3] Tab 互斥 + 不重建');
  const before = await page.evaluate(() => window.__csvBuilds);
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(350);
  const ls1 = await page.evaluate(() => window.__layerState());
  await page.click('#tabs .tab[data-tab="wms"]');
  await page.waitForTimeout(350);
  const ls2 = await page.evaluate(() => window.__layerState());
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(350);
  const afterAll = await page.evaluate(() => window.__csvBuilds);
  log('  before=' + before + ' afterTabCycle=' + afterAll + ' ls(area)=' + JSON.stringify(ls1) + ' ls(wms)=' + JSON.stringify(ls2));
  check(before === afterAll, '切 Tab 不触发 csv 图层重建 (builds 不变)');
  check(ls1.csv === false, '方式2 下 csv 图层已隐藏');
  check(ls2.csv === false && ls2.wms === false, '方式3 下 csv/wms 图层均未挂（wms 未应用时）');

  // 4. 列表排序 + 显示更多（数据集可能小于一屏，按钮或已禁用，需兼容）
  log('\n[4] 火点列表排序 / 显示更多');
  await page.evaluate(() => { const s = document.getElementById('csvListSort'); s.value = 'frp'; s.dispatchEvent(new Event('change')); });
  await page.waitForTimeout(250);
  const topFrps = await page.$$eval('#csvListBody .fpItem .frp', els => els.slice(0, 5).map(e => parseFloat(e.textContent)));
  log('  top frp: ' + JSON.stringify(topFrps));
  check(topFrps.length >= 2 && topFrps[0] >= topFrps[topFrps.length - 1], '按 FRP 降序排列');
  const moreDisabled0 = await page.evaluate(() => document.getElementById('csvListMore').disabled);
  const cnt0 = await page.evaluate(() => document.querySelectorAll('#csvListBody .fpItem').length);
  if (moreDisabled0) {
    log('  (数据集 <= 一屏，已显示全部 ' + cnt0 + ' 条，无需展开)');
    check(cnt0 >= 1, '列表已显示全部条目');
  } else {
    await page.click('#csvListMore');
    await page.waitForTimeout(250);
    const cnt1 = await page.evaluate(() => document.querySelectorAll('#csvListBody .fpItem').length);
    log('  list items: ' + cnt0 + ' -> ' + cnt1);
    check(cnt1 > cnt0, '「显示更多」追加条目');
  }

  // 5. 列表点击定位
  log('\n[5] 列表点击定位');
  await page.click('#csvListBody .fpItem');
  await page.waitForTimeout(500);
  const focused = await page.evaluate(() => window.__focusedFire);
  log('  ' + JSON.stringify(focused));
  check(focused && typeof focused.lat === 'number', '点击列表项成功定位到火点');

  // 6. 自由绘制（注入显式坐标，验证图层与跨 Tab）
  log('\n[6] 自由绘制（注入形状，验证图层与跨 Tab）');
  await page.evaluate(() => {
    const ll = [{ lat: 30, lng: 100 }, { lat: 40, lng: 100 }, { lat: 40, lng: 110 }, { lat: 30, lng: 110 }];
    window.setShape('rect', ll);
  });
  await page.waitForTimeout(250);
  const shp = await page.evaluate(() => ({ state: window.__shapeState(), ls: window.__layerState() }));
  log('  ' + JSON.stringify(shp));
  check(shp.state && shp.state.type === 'rect', '矩形形状已建立');
  await page.click('#tabs .tab[data-tab="wms"]');
  await page.waitForTimeout(250);
  const lsWmsShape = await page.evaluate(() => window.__layerState());
  log('  wms tab ls.shape=' + lsWmsShape.shape);
  check(lsWmsShape.shape === false, '非方式2 Tab 下绘制形状被隐藏');
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(250);
  await page.evaluate(() => window.clearShape());
  await page.waitForTimeout(150);
  const shpCleared = await page.evaluate(() => window.__shapeState());
  check(shpCleared === null, 'clearShape 后形状清除');

  // 7. WMS 应用（占位 key，验证不抛 JS 错误）
  log('\n[7] 方式3 WMS 应用');
  await page.click('#tabs .tab[data-tab="wms"]');
  await page.waitForTimeout(250);
  await page.fill('#firmsKeyWms', 'TESTKEY123');
  await page.click('#wmsApply');
  await page.waitForTimeout(700);
  const wmsState = await page.evaluate(() => ({ ls: window.__layerState(), status: document.getElementById('wmsStatusWms').textContent }));
  log('  ' + JSON.stringify(wmsState));
  check(pageErrors.length === 0, 'WMS 应用过程无 pageerror');

  // 8. 控制台错误扫描
  log('\n[8] 控制台错误扫描');
  log('  console.error 数: ' + consoleErrors.length);
  consoleErrors.slice(0, 10).forEach(e => log('    • ' + e));
  check(consoleErrors.length === 0, '无 console.error 输出');
  check(pageErrors.length === 0, '全程无 pageerror');

  await browser.close();
  log('\n==== 结果 ====');
  if (fails.length) { log('发现 ' + fails.length + ' 个问题：'); fails.forEach(f => log(' - ' + f)); process.exit(1); }
  else { log('✅ 深度探针全绿，未发现运行时 BUG'); }
})().catch(e => { console.error('探针异常：' + e); process.exit(3); });
