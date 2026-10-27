/**
 * 双端一致性检测：Python `area_api_plan()` 与 HTML 内联 JS `planAreaRequests()`
 * 是否是同一套拆段逻辑。
 *
 * 为什么需要它：这两份实现分别服务于 CLI 与网页，但规则完全一致
 * （单次 DAYS 上限 5、DATE 是起始日、>5 天要拆段）。任何一侧被单独修改，
 * 另一侧不会报错、不会失败，只会静默算出不同的请求计划 —— 这正是最难发现的
 * 一类漂移。（v0.7.0 就借此抓到的「Python 用本地日期、JS 用 UTC 日期」，
 * 在每天 UTC 22:00-24:00 两端起始日会整整差一天。）
 *
 * 用法：
 *   node check_plan_parity.cjs <cases.json> <map.html> [out.json]
 *   cases.json = [{"days":7,"date":""}, {"days":10,"date":"2026-10-20"}, ...]
 * 输出：JSON 数组 [{case, js:[...]}]
 */

const fs = require('fs');
const path = require('path');
const { chromium } = require(path.join(
  process.env.PLAYWRIGHT_MODULES || 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules',
  'playwright'));

(async () => {
  const casesPath = process.argv[2];
  const htmlPath = process.argv[3];
  const outPath = process.argv[4];
  const cases = JSON.parse(fs.readFileSync(casesPath, 'utf-8'));

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));

  await page.goto('file:///' + path.resolve(htmlPath).replace(/\\/g, '/'),
    { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);

  const has = await page.evaluate(() => typeof window.planAreaRequests === 'function'
    || typeof planAreaRequests === 'function');
  if (!has) {
    console.error('FAIL 页面里找不到 planAreaRequests()');
    await browser.close();
    process.exit(1);
  }

  const out = [];
  for (const c of cases) {
    const js = await page.evaluate(
      ({ days, date }) => {
        const fn = (typeof window.planAreaRequests === 'function')
          ? window.planAreaRequests : planAreaRequests;
        return fn(days, date || '');
      }, c);
    out.push({ case: c, js: js.map(p => [p.days, p.date || '']) });
  }

  await browser.close();
  if (errs.length) {
    console.error('FAIL 页面存在 JS 错误：' + errs.join(' | '));
    process.exit(1);
  }
  const text = JSON.stringify(out, null, 1);
  if (outPath) { fs.writeFileSync(outPath, text, 'utf-8'); }
  console.log(text);
})().catch(e => { console.error('FAIL ' + e.message); process.exit(1); });
