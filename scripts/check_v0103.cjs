const path = require('path');
const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
const { chromium } = require(path.join(NODE_PATH, 'playwright'));

const FILE = 'file://C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china/fire_map.html';
const PROXY = 'http://127.0.0.1:8765/fire_map.html';

let pass = 0, fail = 0;
function ok(name, cond){ if(cond){pass++;console.log('  ✓ '+name);} else {fail++;console.log('  ✗ '+name);} }

(async () => {
  const b = await chromium.launch({ headless: true });

  // ---- A. 通过本地代理打开（方式2 连通路径）----
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  await p.goto(PROXY, { waitUntil: 'load' });
  await p.waitForFunction(() => document.getElementById('csvListBody') && document.getElementById('csvListBody').children.length > 0, { timeout: 20000 });

  // 标题（请求4）
  const title = await p.title();
  ok('A1 document.title = NASA FIRMS 全球近实时火点', title === 'NASA FIRMS 全球近实时火点');
  // v0.10.5 顶部重设计后品牌区拆为 kicker + title + 版本胶囊，逐节点断言
  const brand = await p.evaluate(() => {
    const q = s => { const e = document.querySelector('#topbar .brand ' + s); return e ? e.textContent.replace(/\s+/g, ' ').trim() : null; };
    return { kicker: q('.kicker'), title: q('.title'), ver: q('.ver') };
  });
  ok('A2a 品牌区 kicker = NASA · FIRMS', brand.kicker === 'NASA · FIRMS');
  ok('A2b 品牌区主标题 = 全球近实时火点', (brand.title || '').includes('全球近实时火点'));
  ok('A2c 品牌区版本胶囊格式 vX.Y.Z', /^v\d+\.\d+\.\d+$/.test(brand.ver || ''));

  // 左列宽度（请求2）
  const lw = await p.evaluate(() => getComputedStyle(document.getElementById('leftCol')).width);
  ok('A3 左列宽度 = 380px（原 340）', lw === '380px');

  // 方式1 来源卡（请求3）
  const meta = await p.evaluate(() => {
    const g = id => { const e = document.getElementById(id); return e ? e.textContent.trim() : null; };
    return {
      prod: g('csvMetaScope') !== null ? document.querySelector('#csvSrcMeta').innerText.replace(/\n/g, ' | ') : null,
      scope: g('csvMetaScope'),
      count: g('csvMetaCount'),
      hasCard: !!document.getElementById('csvSrcMeta'),
    };
  });
  ok('A4 方式1 含「数据来源」元信息卡', meta.hasCard === true);
  ok('A5 来源卡含「FIRMS 火点 · VIIRS S-NPP 375m (C2)」', (meta.prod || '').includes('FIRMS 火点 · VIIRS S-NPP 375m (C2)'));
  ok('A6 来源卡含「24h CSV」', (meta.prod || '').includes('24h CSV'));
  ok('A7 来源卡含「行政区划 / 全国（全部省份）」', (meta.prod || '').includes('全国（全部省份）'));
  ok('A8 来源卡火点总数 = 7739', meta.count === '7739');

  // 切省联动（请求3 动态）
  await p.selectOption('#provSelect', '四川省').catch(() => {});
  await p.waitForTimeout(400);
  const scope2 = await p.evaluate(() => document.getElementById('csvMetaScope').textContent);
  const count2 = await p.evaluate(() => document.getElementById('csvMetaCount').textContent);
  ok('A9 切省后来源卡行政区划联动（= 四川省）', scope2 === '四川省');
  ok('A10 切省后来源卡火点数联动（非 7739、为数字）', count2 !== '7739' && /^\d+$/.test(count2));

  // 方式2 代理发现（请求1：代理已起 → 应发现）
  const disc = await p.evaluate(async () => {
    try { const b = await discoverAreaProxy(); return b; } catch(e){ return 'ERR:'+e.message; }
  });
  ok('A11 方式2 经运行中的本地代理 discoverAreaProxy 命中', typeof disc === 'string' && disc.includes('127.0.0.1:8765'));

  ok('A12 全程无 pageerror', errs.length === 0);
  if (errs.length) console.log('   pageerrors:', JSON.stringify(errs));
  await p.close();

  // ---- B. file:// 打开同样能发现代理（CORS 已开）----
  const p2 = await b.newPage({ viewport: { width: 1440, height: 900 } });
  const errs2 = [];
  p2.on('pageerror', e => errs2.push(e.message));
  await p2.goto(FILE, { waitUntil: 'load' });
  await p2.waitForFunction(() => document.getElementById('csvListBody') && document.getElementById('csvListBody').children.length > 0, { timeout: 20000 });
  const disc2 = await p2.evaluate(async () => { try { return await discoverAreaProxy(); } catch(e){ return 'ERR:'+e.message; } });
  ok('B1 file:// 打开也能发现 127.0.0.1:8765 代理（CORS 生效）', typeof disc2 === 'string' && disc2.includes('127.0.0.1:8765'));
  ok('B2 file:// 全程无 pageerror', errs2.length === 0);
  if (errs2.length) console.log('   pageerrors:', JSON.stringify(errs2));
  await p2.close();

  await b.close();
  console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('FATAL', e); process.exit(2); });
