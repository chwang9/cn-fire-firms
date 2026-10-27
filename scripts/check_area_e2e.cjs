/**
 * cn-fire-firms 端到端实测 —— 覆盖三种取数方式 + 省级筛选
 *
 * 1) 方式1：内嵌 CSV 数据，按省切换 → 火点数量随之变化（纯前端，无需联网）
 * 2) 方式2：Area API 经本地代理 → 真实拉取并渲染
 * 3) 方式3：WMS 栅格叠加 → 图层控件出现
 *
 * 用法：node scripts/check_area_e2e.cjs [fire_map.html 路径]
 * 需要 MAP_KEY：环境变量 FIRMS_MAP_KEY（缺省用公开示例 key）
 */
const path = require('path');
const { spawn } = require('child_process');

const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
let chromium;
try {
  ({ chromium } = require(path.join(NODE_PATH, 'playwright')));
} catch (e) {
  console.log('! 未找到 playwright，跳过端到端实测');
  process.exit(0);
}

const PY = process.env.FIRMS_PY || 'c:/Users/wangch/.workbuddy/binaries/python/versions/3.13.12/python.exe';
const SERVE = path.join(__dirname, 'serve_fires.py');
const KEY = process.env.FIRMS_MAP_KEY || '243e05a6877257980f3aa8cfce4996e4';
const DEFAULT_HTML = 'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china/fire_map.html';
const htmlPath = process.argv[2] || DEFAULT_HTML;

let fail = 0;
const check = (cond, msg) => { console.log((cond ? '  ✓ ' : '  ✗ ') + msg); if (!cond) fail++; };

(async () => {
  const mapDir = path.dirname(path.resolve(htmlPath));
  const fileName = path.basename(htmlPath);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  const fileUrl = 'file:///' + path.resolve(htmlPath).replace(/\\/g, '/');
  await page.goto(fileUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);

  console.log('1) 方式1：内嵌 CSV + 省级筛选');
  const optCount = await page.evaluate(() => document.getElementById('provSelect').options.length);
  check(optCount >= 2, `省级下拉有 ${optCount} 项`);

  // 逐省切换，验证火点数随省变化且与下拉标注一致
  const provs = await page.evaluate(() => Array.from(document.getElementById('provSelect').options)
    .filter(o => o.value).slice(0, 6).map(o => ({ v: o.value, label: o.textContent })));
  for (const p of provs) {
    await page.selectOption('#provSelect', p.v);
    await page.waitForTimeout(700);
    const r = await page.evaluate(() => ({
      rendered: window.__fireRendered,
      count: window.__csvCount,
      scope: window.__province,
    }));
    const expect = parseInt(p.label.replace(/[^0-9]/g, ''), 10);
    const ok = String(r.rendered) === String(expect) && r.scope === p.v;
    check(ok, `${p.v}: 渲染 ${r.rendered} 个（标注 ${expect}），范围=${r.scope}`);
    if (!ok) break;
  }

  // 切回全国
  await page.selectOption('#provSelect', '');
  await page.waitForTimeout(700);
  const nat = await page.evaluate(() => window.__fireRendered);
  check(nat >= 0, `全国视图渲染 ${nat} 个火点`);

  console.log('\n2) 方式2：Area API（经本地代理）');
  // 用页面内置的探测端口：提示里的启动命令也用它，二者天然一致。
  // 若该端口被占用（TIME_WAIT / 其它进程），起代理会失败 —— 故先做就绪探测并明确报错。
  const PORT_USED = await page.evaluate(() => AREA_PROXY_PORT);
  console.log('     页面探测端口:', PORT_USED);
  const srv = spawn(PY, [SERVE, '--dir', mapDir, '--file', fileName,
    '--port', String(PORT_USED)], { stdio: 'ignore' });
  await new Promise(r => setTimeout(r, 2500));
  const health = await page.evaluate(async (port) => {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/api/fires/health`, { cache: 'no-store' });
      return r.ok;
    } catch (e) { return false; }
  }, PORT_USED);
  check(health, `本地代理已在 ${PORT_USED} 端口就绪`);
  if (!health) {
    console.log('❌ 代理未启动（端口可能被占用），后续 Area API 测试跳过');
    srv.kill();
    await browser.close();
    process.exit(1);
  }

  // 选火点最多的省 → fitBounds 缩视野（v2.1.0 起 bbox=视野∩中国，选省即缩视野）
  const target = await page.evaluate(() => {
    const opts = Array.from(document.getElementById('provSelect').options).filter(o => o.value);
    let best = null, bestN = -1;
    for (const o of opts) {
      const n = parseInt(o.textContent.replace(/[^0-9]/g, ''), 10) || 0;
      if (n > bestN) { bestN = n; best = o.value; }
    }
    return best || '';
  });
  if (target) { await page.selectOption('#provSelect', target); await page.waitForTimeout(900); }
  await page.click('#tabs .tab[data-tab="area"]');
  await page.fill('#firmsKey', KEY);
  const readArea = () => page.evaluate(() => ({
    status: document.getElementById('wmsStatus').textContent.split('\n')[0],
    fetched: window.__areaFetched,
    url: window.__areaUrl || '',
    cls: document.getElementById('wmsStatus').className,
    days: document.getElementById('areaDays').value,
  }));
  const waitIdle = () => page.waitForFunction(() => {
    const t = document.getElementById('wmsStatus').textContent;
    return t.length > 0 && !t.includes('拉取中') && !t.includes('正在连接');
  }, null, { timeout: 120000 });
  await page.click('#areaApply');
  await waitIdle();
  await page.waitForTimeout(400);
  let area = await readArea();
  console.log('     days=' + area.days + ' → ' + area.status);

  // v0.9.0：days=1 拉空是**常态而非故障**（VIIRS NRT 当日过境常晚于当前时刻，
  // 实测同一 bbox days=1 返回 0 条、days=3 返回数据）。让自检因外部数据波动随机
  // 失败是最糟的结果 —— 它训练使用者忽略红色。故先自动放宽到 3 天重试一次；
  // **3 天仍为空才判定为真实故障**（那才是代理/接口层面的问题）。
  if (area.fetched === 0) {
    check(area.cls !== 'err', `「无火点」不得显示为红色错误态（实测 class=${area.cls || '(空)'}）`);
    await page.fill('#areaDays', '3');   // input[type=number]，不是 <select>
    await page.click('#areaApply');
    await waitIdle();
    await page.waitForTimeout(400);
    area = await readArea();
    console.log('     days=' + area.days + '（放宽重试）→ ' + area.status);
  }
  check(area.fetched > 0, `Area API 拉到 ${area.fetched} 个火点（days=${area.days}）`);
  check(area.url.includes('/api/area/csv/'), '请求路径与官方接口同构');

  console.log('\n3) 方式3：WMS 栅格');
  await page.click('#tabs .tab[data-tab="wms"]');
  await page.fill('#firmsKeyWms', KEY);
  await page.click('#wmsApply');
  await page.waitForTimeout(1500);
  const wms = await page.evaluate(() => ({
    status: document.getElementById('wmsStatusWms').textContent,
    ctrl: !!document.querySelector('.leaflet-control-layers'),
  }));
  console.log('     ' + wms.status);
  check(wms.ctrl, 'WMS 图层控件已加入');

  console.log('\n4) 回归：点击火点弹窗');
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(600);
  const pt = await page.evaluate(() => {
    const rows = FIRES[window.__province] || ALL_ROWS;
    for (const r of rows) {
      const g = wgs84togcj02(r[0], r[1]);
      const p = map.latLngToContainerPoint([g[1], g[0]]);
      if (p.x > 20 && p.y > 20 && p.x < 1400 && p.y < 850) return { x: p.x, y: p.y };
    }
    return null;
  });
  if (pt) {
    await page.mouse.click(pt.x, pt.y);
    await page.waitForTimeout(600);
    const pop = await page.$eval('.leaflet-popup-content', e => e.innerText.replace(/\n/g, ' | ')).catch(() => null);
    check(!!pop, '弹窗: ' + (pop || '(无)'));
  } else {
    check(false, '未找到可点击的火点');
  }

  srv.kill();
  console.log('\npageerrors:', errors.length ? errors : 'none');
  console.log(fail === 0 ? '✅ 端到端实测全部通过' : `❌ ${fail} 项未通过`);
  await browser.close();
  process.exit(fail === 0 ? 0 : 1);
})();
