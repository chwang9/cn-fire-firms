/**
 * cn-fire-firms v0.10.0 三项体验改动 · 端到端实测
 *
 *   1) 左侧面板加宽：#leftCol 计算宽度应为 380px（v0.10.3 由 340 加宽）
 *   2) 方式2 切换火点产品（卫星）实时显示简介：#areaSourceInfo 随选择变化
 *   3) 采集时间精确到「时分秒」：列表时间含 ':00'、弹窗时间含 '（分钟级）'
 *
 * 为什么必须真开浏览器：这些都是运行时 DOM / 样式 / 交互，静态字符串断言抓不到。
 * 用法：node scripts/check_v010_features.cjs [fire_map.html 路径]
 */
const path = require('path');

const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
let chromium;
try {
  ({ chromium } = require(path.join(NODE_PATH, 'playwright')));
} catch (e) {
  console.log('! 未找到 playwright，跳过 v0.10.0 实测');
  process.exit(0);
}

const htmlPath = process.argv[2] || path.join(
  'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china', 'fire_map.html');

let fail = 0;
const check = (cond, msg) => { console.log((cond ? '  ✓ ' : '  ✗ ') + msg); if (!cond) fail++; };

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('file:///' + path.resolve(htmlPath).replace(/\\/g, '/'),
    { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);

  console.log('\n[1] 左侧面板加宽');
  const leftW = await page.evaluate(() => {
    const el = document.getElementById('leftCol');
    return getComputedStyle(el).width;
  });
  check(leftW === '380px', `左列计算宽度 = ${leftW}（期望 380px）`);

  console.log('\n[2] 方式2 切换火点产品（卫星）时显示简介');
  // 切到方式2 面板
  await page.click('[data-tab="area"]');
  await page.waitForTimeout(300);
  const hasInfo = await page.evaluate(() => !!document.getElementById('areaSourceInfo'));
  check(hasInfo, '方式2 面板存在 #areaSourceInfo 容器');

  // 初始（默认产品）应已渲染简介
  const initInfo = await page.evaluate(() =>
    (document.getElementById('areaSourceInfo').textContent || '').trim());
  check(initInfo.length > 0, `初始产品简介非空：「${initInfo.slice(0, 40)}…」`);

  async function switchSourceAndRead(val) {
    await page.evaluate((v) => {
      const sel = document.getElementById('areaSource');
      sel.value = v;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }, val);
    await page.waitForTimeout(150);
    return page.evaluate(() =>
      (document.getElementById('areaSourceInfo').textContent || '').trim());
  }

  const modis = await switchSourceAndRead('MODIS_NRT');
  check(modis.includes('MODIS') && modis.includes('1km'),
    `切到 MODIS_NRT → 简介含「MODIS」与「1km」：「${modis.slice(0, 36)}…」`);

  const noaa21 = await switchSourceAndRead('VIIRS_NOAA21_NRT');
  check(noaa21.includes('NOAA-21') && noaa21.includes('375m'),
    `切到 VIIRS_NOAA21_NRT → 简介含「NOAA-21」与「375m」：「${noaa21.slice(0, 36)}…」`);

  const viirs = await switchSourceAndRead('VIIRS_SNPP_NRT');
  check(viirs.includes('Suomi NPP') || viirs.includes('S-NPP'),
    `切回 VIIRS_SNPP_NRT → 简介含平台名：「${viirs.slice(0, 36)}…」`);

  console.log('\n[3] 采集时间精确到「时分秒」');
  // 切回方式1（默认）看火点列表
  await page.click('[data-tab="csv"]');
  await page.waitForTimeout(400);
  const listTime = await page.evaluate(() => {
    const it = document.querySelector('.fpItem .l2');
    return it ? it.textContent : '';
  });
  check(listTime.includes(':00'), `火点列表时间含秒位「:00」：「${listTime.slice(0, 48)}」`);

  // 点击列表首项，开弹窗，检查弹窗时间口径
  let popupOk = false, popupTxt = '';
  try {
    await page.click('.fpItem');
    await page.waitForSelector('.leaflet-popup-content', { timeout: 4000 });
    popupTxt = await page.evaluate(() =>
      document.querySelector('.leaflet-popup-content').textContent || '');
    popupOk = popupTxt.includes('采集时间') && popupTxt.includes(':00')
           && popupTxt.includes('（分钟级）');
  } catch (e) { /* 弹窗可能未出现 */ }
  check(popupOk,
    `弹窗时间含「采集时间 / :00 / （分钟级）」：${popupTxt ? '「' + popupTxt.replace(/\s+/g, ' ').slice(0, 60) + '…」' : '未出现弹窗'}`);

  console.log('\n[0] 运行期错误');
  check(errors.length === 0, `pageerror = ${errors.length}` + (errors.length ? ' → ' + errors.join(' | ') : ''));

  await browser.close();
  console.log(`\n结果：${fail === 0 ? '全部通过 ✓' : fail + ' 项失败 ✗'}`);
  process.exit(fail === 0 ? 0 : 1);
})();
