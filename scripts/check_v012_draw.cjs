const path = require('path');
const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
const { chromium } = require(path.join(NODE_PATH, 'playwright'));

const HTML = 'file://' + (process.argv[2] || 'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china/fire_map.html');
// 安全内区（避开：左列 x<360、顶栏 y<70、右下图例、左下比例尺、右上控件）
const A = {x: 460, y: 180}, B = {x: 760, y: 240}, C = {x: 600, y: 520};
let pass = 0, fail = 0;
function ok(name, cond){ console.log((cond?'  PASS':'  FAIL')+' '+name); cond?pass++:fail++; }

(async()=>{
  const b = await chromium.launch({ headless: true });
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  await p.goto(HTML, { waitUntil: 'load' });
  await p.waitForFunction(()=>document.getElementById('csvListBody') && document.getElementById('csvListBody').children.length>0, { timeout: 20000 });
  await p.click('button.tab[data-tab="area"]'); await p.waitForTimeout(300);

  const shapeState = () => p.evaluate(()=>window.__layerState ? window.__layerState().shape : (typeof areaShape!=='undefined' && !!areaShape));
  const hint = () => p.$eval('#areaShapeHint', el=>el.textContent);

  // B1: 矩形拖拽中按 Esc 取消（不应锁定）
  await p.click('#areaDrawRect'); await p.waitForTimeout(150);
  await p.mouse.move(A.x, A.y); await p.mouse.down();
  await p.mouse.move(B.x, B.y, { steps: 4 });
  await p.keyboard.press('Escape'); await p.mouse.up(); await p.waitForTimeout(200);
  ok('B1 矩形拖拽中 Esc 取消后无锁定形状', (await shapeState()) === false);

  // B2: 画一个矩形并锁定
  await p.click('#areaDrawRect'); await p.waitForTimeout(150);
  await p.mouse.move(A.x, A.y); await p.mouse.down();
  await p.mouse.move(B.x, B.y, { steps: 6 }); await p.mouse.up();
  await p.waitForTimeout(300);
  ok('B2 矩形锁定后 shape=true', (await shapeState()) === true);
  ok('B2 锁定后提示含「已锁定矩形」', /矩形/.test(await hint()));

  // B3: 锁定后按 Esc 撤销（新修复）
  await p.keyboard.press('Escape'); await p.waitForTimeout(300);
  ok('B3 锁定后 Esc 撤销 → shape=false', (await shapeState()) === false);
  ok('B3 撤销后提示回到「地图视野」', /地图视野/.test(await hint()));

  // B4: 多边形只点 2 点后双击不应闭合
  await p.click('#areaDrawPoly'); await p.waitForTimeout(150);
  await p.mouse.click(A.x, A.y); await p.waitForTimeout(250);
  await p.mouse.click(B.x, B.y); await p.waitForTimeout(250);
  await p.mouse.dblclick(C.x, C.y); await p.waitForTimeout(300);
  ok('B4 多边形仅 2 点双击不闭合', (await shapeState()) === false);

  // B5: 补足 3 点后双击闭合
  await p.mouse.click(C.x, C.y); await p.waitForTimeout(250);
  await p.mouse.dblclick(C.x, C.y); await p.waitForTimeout(300);
  ok('B5 多边形满 3 点双击闭合 → shape=true', (await shapeState()) === true);

  // B6: 输入框里按 Esc 不应误清形状（回归守卫）
  await p.click('#firmsKey'); await p.keyboard.type('TESTKEY'); await p.keyboard.press('Escape');
  await p.waitForTimeout(200);
  ok('B6 输入框 Esc 不误清已锁定多边形', (await shapeState()) === true);

  ok('pageerror=0', errs.length === 0);
  if(errs.length) console.log('  pageerrors:', JSON.stringify(errs));
  console.log('\n结果: '+pass+' 通过, '+fail+' 失败');
  await b.close();
  process.exit(fail ? 1 : 0);
})().catch(e=>{ console.error('FATAL', e); process.exit(2); });
