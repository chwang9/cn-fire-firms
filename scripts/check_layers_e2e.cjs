/**
 * cn-fire-firms 图层隔离 / 火点列表 / 自由绘制 端到端实测
 *
 * 覆盖 v0.8.0 的三件事：
 *   1) 三个方式的火点图层必须**互斥**（切到方式2/3 后方式1 的点不许还在图上）
 *   2) 切 Tab 不许重建 marker（只摘挂图层）——用 __csvBuilds 计数断言
 *   3) 火点列表：排序、分页、点击定位；自由绘制：矩形拖框、多边形勾点、清除
 *
 * 为什么必须真开浏览器：这些都是运行时状态（地图图层成员、DOM、鼠标事件），
 * 静态字符串断言完全抓不到 —— 尤其是「图层还挂在 map 上」这种既不报错、
 * 页面也照看的现象。
 *
 * 用法：node scripts/check_layers_e2e.cjs [fire_map.html 路径]
 */
const path = require('path');

const NODE_PATH = 'C:/Users/wangch/.workbuddy/binaries/node/workspace/node_modules';
let chromium;
try {
  ({ chromium } = require(path.join(NODE_PATH, 'playwright')));
} catch (e) {
  console.log('! 未找到 playwright，跳过图层隔离实测');
  process.exit(0);
}

const htmlPath = process.argv[2] || path.join(HERE_DEFAULT(), 'fire_map.html');
function HERE_DEFAULT() {
  return 'C:/Users/wangch/WorkBuddy/2026-10-26-17-15-58/fire_china';
}

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

  // 选一个火点较多的省，列表才有足够数据验证分页/排序
  const target = await page.evaluate(() => {
    const opts = Array.from(document.getElementById('provSelect').options).filter(o => o.value);
    let best = '', n = -1;
    for (const o of opts) {
      const c = parseInt(o.textContent.replace(/[^0-9]/g, ''), 10) || 0;
      if (c > n) { n = c; best = o.value; }
    }
    return { name: best, count: n };
  });
  await page.selectOption('#provSelect', target.name);
  await page.waitForTimeout(1200);
  console.log(`用例省份：${target.name}（${target.count} 个火点）\n`);

  const layers = () => page.evaluate(() => window.__layerState());
  const builds = () => page.evaluate(() => window.__csvBuilds || 0);

  console.log('1) 三个方式的火点图层互斥');
  const baseBuilds = await builds();
  let st = await layers();
  check(st.csv === true, `Tab=csv：方式1 图层在图上（csv=${st.csv}）`);

  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(400);
  st = await layers();
  check(st.csv === false && st.area === false && st.wms === false,
    `Tab=area：方式1 图层已摘除（csv=${st.csv} area=${st.area} wms=${st.wms}）`);

  await page.click('#tabs .tab[data-tab="wms"]');
  await page.waitForTimeout(400);
  st = await layers();
  check(st.csv === false, `Tab=wms：方式1 图层已摘除（csv=${st.csv}）`);

  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(400);
  st = await layers();
  check(st.csv === true, `切回 Tab=csv：方式1 图层恢复（csv=${st.csv}）`);

  console.log('\n2) 切 Tab 不重建 marker（只摘挂图层）');
  const afterBuilds = await builds();
  check(afterBuilds === baseBuilds,
    `往返切换 3 次后 __csvBuilds 未增长（${baseBuilds} → ${afterBuilds}）`);
  await page.selectOption('#provSelect', '全国（全部省份）').catch(() => page.selectOption('#provSelect', ''));
  await page.waitForTimeout(1500);
  const natBuilds = await builds();
  const natCount = await page.evaluate(() => window.__csvItems);
  check(natBuilds > afterBuilds,
    `切省确实触发重建（${afterBuilds} → ${natBuilds}），可观测性成立；当前 ${natCount} 个点`);
  await page.selectOption('#provSelect', target.name);
  await page.waitForTimeout(1500);

  console.log('\n3) 火点列表（方式1）');
  const list0 = await page.evaluate(() => ({
    meta: document.getElementById('csvListMeta').textContent,
    items: document.querySelectorAll('#csvListBody .fpItem').length,
    count: window.__csvListCount,
    rendered: window.__fireRendered,
    moreTxt: document.getElementById('csvListMore').textContent,
    firstFrp: (document.querySelector('#csvListBody .fpItem .frp') || {}).textContent,
  }));
  check(list0.count === list0.rendered,
    `列表条数与地图一致：${list0.count} = 渲染 ${list0.rendered}`);
  check(list0.items === Math.min(60, list0.count),
    `首屏只渲染 60 条窗口（实际 ${list0.items} 条，共 ${list0.count}）`);
  check(/MW/.test(list0.firstFrp || ''), `列表首项带 FRP：${list0.firstFrp}`);

  // 默认按 FRP 降序：第一条应 >= 第二条
  const frpSeq = await page.evaluate(() => Array.from(
    document.querySelectorAll('#csvListBody .fpItem .frp')).slice(0, 5)
    .map(e => parseFloat(e.textContent)));
  const desc = frpSeq.every((v, i) => i === 0 || frpSeq[i - 1] >= v);
  check(desc, `默认按 FRP 降序：前 5 项 ${frpSeq.join(' ≥ ')}`);

  // 切到「按时间降序」后顺序应变化，且时间单调不增
  // 只看前 5 项是**无效断言**：同一分钟里常常就有十几个点，前 5 项时间天然相同，
  // 怎么排序都通过。必须取整个渲染窗口来判断单调性。
  const readList = () => page.evaluate(() => Array.from(
    document.querySelectorAll('#csvListBody .fpItem'))
    .map(el => {
      const t = el.querySelector('.l2').textContent;
      const m = t.match(/(\d{4}-\d{2}-\d{2}) (\d{4})/);
      return {key: m ? Number(m[1].replace(/-/g, '') + m[2]) : -1,
              frp: parseFloat(el.querySelector('.frp').textContent),
              cx: el.querySelector('.l2').textContent};
    }));
  const byFrp = await readList();
  const frpMono = byFrp.map(x => x.frp);
  check(frpMono.every((v, i) => i === 0 || frpMono[i - 1] >= v),
    `FRP 降序：整个窗口 ${frpMono.length} 项单调不增`);

  await page.selectOption('#csvListSort', 'time');
  await page.waitForTimeout(400);
  const byTime = await readList();
  const tKeys = byTime.map(x => x.key);
  check(tKeys.every((v, i) => i === 0 || tKeys[i - 1] >= v),
    `时间降序：整个窗口 ${tKeys.length} 项单调不增（${tKeys[0]} → ${tKeys[tKeys.length - 1]}）`);
  const sameOrder = JSON.stringify(byFrp.map(x => x.cx)) === JSON.stringify(byTime.map(x => x.cx));
  check(!sameOrder, '两种排序结果确实不同（排序是生效的，不是同一份顺序换个标签）');
  await page.selectOption('#csvListSort', 'frp');
  await page.waitForTimeout(400);

  // 分页
  const before = list0.items;
  await page.click('#csvListMore');
  await page.waitForTimeout(400);
  const after = await page.evaluate(() =>
    document.querySelectorAll('#csvListBody .fpItem').length);
  check(after > before || before >= list0.count,
    `「显示更多」追加 60 条（${before} → ${after}）`);

  // v0.9.0 守卫：分页窗口必须在**切换范围后回到首屏 60 条**。
  // 改前 listStates.csv.limit 只在换排序时重置，切省/切回全国都照旧 —— 在全国点几次
  // 「显示更多」把窗口撑到 300 后再切回来，会一次性建 300 条 DOM，
  // 正好复踩 v0.8.0 想避开的坑。断言必须「先撑大再切」，否则拿不到敏感性。
  const expanded = after;
  // 挑一个火点数 >= 60 的省 —— 只有目标范围的行数超过窗口，断言才有鉴别力：
  // 目标只有 30 条时 dom=30，无论是否重置都成立，守卫就空转了。
  const prov = await page.evaluate(() => {
    for (const o of Array.from(document.getElementById('provSelect').options)) {
      if (!o.value) continue;
      const n = parseInt(o.textContent.replace(/[^0-9]/g, ''), 10) || 0;
      if (n >= 60) return { name: o.value, count: n };
    }
    return { name: '', count: 0 };
  });
  check(prov.name !== '', `夹具中有火点数 ≥60 的省（${prov.name} ${prov.count} 个），分页守卫不会空转`);
  await page.selectOption('#provSelect', prov.name);
  await page.waitForTimeout(900);
  await page.selectOption('#provSelect', '');
  await page.waitForTimeout(1500);
  const back = await page.evaluate(() => ({
    limit: listStates.csv.limit,
    dom: document.querySelectorAll('#csvListBody .fpItem').length,
  }));
  check(expanded > 60 && back.dom === 60 && back.limit === 60,
    `切范围后分页窗口回到首屏 60 条（曾展开到 ${expanded} → 切 ${prov.name} 再切回全国后 ${back.dom}）`);

  // 点击列表项 → 定位 + 弹窗 + 高亮
  await page.click('#csvListBody .fpItem');
  await page.waitForTimeout(900);
  const foc = await page.evaluate(() => ({
    focused: window.__focusedFire || null,
    hasPopup: !!document.querySelector('.leaflet-popup-content'),
    highlighted: !!document.querySelector('#csvListBody .fpItem.on'),
    zoom: map.getZoom(),
  }));
  check(foc.focused && typeof foc.focused.lat === 'number',
    `点击列表项已定位：lat=${foc.focused && foc.focused.lat.toFixed(4)} zoom=${foc.zoom}`);
  check(foc.hasPopup, '点击列表项弹出详情');
  check(foc.highlighted, '列表当前行高亮');

  console.log('\n4) 自由绘制（方式2）');
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(400);

  // 矩形：拖框
  const box = await page.evaluate(() => {
    const el = document.getElementById('map').getBoundingClientRect();
    return { x: el.left, y: el.top, w: el.width, h: el.height };
  });
  const x0 = box.x + box.w * 0.35, y0 = box.y + box.h * 0.35;
  const x1 = box.x + box.w * 0.62, y1 = box.y + box.h * 0.62;
  await page.click('#areaDrawRect');
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  await page.mouse.move(x0 + (x1 - x0) / 2, y0 + (y1 - y0) / 2, { steps: 6 });
  await page.mouse.move(x1, y1, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(500);
  const rect = await page.evaluate(() => ({
    shape: window.__shapeState(),
    hint: document.getElementById('areaShapeHint').textContent,
    lgShown: getComputedStyle(document.getElementById('lgShapeRow')).display !== 'none',
    cursor: getComputedStyle(document.querySelector('#map')).cursor,
  }));
  check(rect.shape && rect.shape.type === 'rect', `拖拽生成矩形（顶点 ${rect.shape && rect.shape.n}）`);
  check(rect.shape && rect.shape.east > rect.shape.west && rect.shape.north > rect.shape.south,
    `矩形包络有效：${rect.shape && rect.shape.west.toFixed(3)},${rect.shape && rect.shape.south.toFixed(3)} ~ `
    + `${rect.shape && rect.shape.east.toFixed(3)},${rect.shape && rect.shape.north.toFixed(3)}`);
  check(/矩形/.test(rect.hint) && /WGS-84/.test(rect.hint), `范围提示已更新：${rect.hint}`);
  check(rect.lgShown, '图例新增「已绘制的关注区域」一行');
  check(rect.cursor !== 'crosshair', `绘制结束光标已复位（${rect.cursor}）`);

  // 多边形：4 次单击 + 双击闭合
  await page.click('#areaClearShape');
  await page.waitForTimeout(300);
  const cleared = await page.evaluate(() => window.__shapeState());
  check(cleared === null, '「清除区域」已清空形状');

  await page.click('#areaDrawPoly');
  const pts = [[0.40, 0.40], [0.62, 0.38], [0.66, 0.60], [0.42, 0.62]];
  for (const [px, py] of pts) {
    await page.mouse.click(box.x + box.w * px, box.y + box.h * py);
    await page.waitForTimeout(320);
  }
  await page.mouse.dblclick(box.x + box.w * 0.42, box.y + box.h * 0.62);
  await page.waitForTimeout(400);
  const poly = await page.evaluate(() => ({
    shape: window.__shapeState(),
    hint: document.getElementById('areaShapeHint').textContent,
  }));
  // 双击守卫必须生效：4 个顶点就是 4 个，不能被双击多塞两点
  check(poly.shape && poly.shape.type === 'poly' && poly.shape.n === 4,
    `多边形闭合且顶点数正确（type=${poly.shape && poly.shape.type} n=${poly.shape && poly.shape.n}，双击未多塞顶点）`);
  check(/多边形/.test(poly.hint), `范围提示切换为多边形：${poly.hint}`);

  // Esc 取消绘制
  await page.click('#areaDrawPoly');
  await page.waitForTimeout(200);
  const drawing = await page.evaluate(() =>
    document.body.classList.contains('map-draw'));
  check(drawing, '绘制进行中标记生效（body.map-draw）');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  const cancelled = await page.evaluate(() => ({
    drawing: document.body.classList.contains('map-draw'),
    drag: !!(map.dragging && map.dragging.enabled()),
    shape: window.__shapeState(),
  }));
  check(!cancelled.drawing, 'Esc 取消绘制（map-draw 已移除）');
  check(cancelled.drag, 'Esc 后地图拖拽已恢复');
  check(cancelled.shape && cancelled.shape.type === 'poly', 'Esc 不会误删已画好的区域');

  console.log('\n5) 切 Tab 时绘制状态收敛');
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(400);
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(400);
  const kept = await page.evaluate(() => window.__shapeState());
  check(kept && kept.type === 'poly', '已画好的区域在 Tab 往返后仍在（不该被误清）');

  await page.click('#areaClearShape');
  await page.waitForTimeout(300);

  console.log('\n6) 切省时列表与图层同步清理');
  await page.click('#tabs .tab[data-tab="csv"]');
  await page.waitForTimeout(300);
  await page.selectOption('#provSelect', '');
  await page.waitForTimeout(1500);
  await page.click('#tabs .tab[data-tab="area"]');
  await page.waitForTimeout(400);
  const afterProv = await page.evaluate(() => ({
    layers: window.__layerState(),
    areaCount: window.__areaListCount,
    emptyText: (document.querySelector('#areaListBody .fpEmpty') || {}).textContent,
  }));
  check(afterProv.layers.csv === false, '切到方式2 后方式1 图层仍保持摘除');
  check(afterProv.areaCount === 0, `方式2 列表随切省清空（count=${afterProv.areaCount}）`);
  check(/尚未拉取/.test(afterProv.emptyText || ''), `空列表有明确引导文案：${afterProv.emptyText}`);

  console.log('\npageerrors:', errors.length ? errors : 'none');
  if (errors.length) fail++;
  console.log(fail === 0 ? '✅ 图层隔离 / 火点列表 / 自由绘制 端到端全部通过'
    : `❌ ${fail} 项未通过`);
  await browser.close();
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error('FAIL', e); process.exit(1); });
