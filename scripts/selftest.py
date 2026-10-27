#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cn-fire-firms 离线自检（selftest）

不依赖任何网络：用技能内置的「海南省」真实边界（GCJ-02）配合合成火点，
验证整条管线 —— 裁切（包围盒 + 点在多边形）→ GeoJSON → 自包含 HTML → JS 语法校验。

运行：python selftest.py  （退出码 0 表示通过）
"""

import datetime
import glob
import json
import os
import random
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SKILL_ROOT = os.path.dirname(HERE)

import fire_firms_cn as F  # noqa: E402

NODE = r"C:/Users/wangch/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"

# 让 node 子进程能解析 playwright，而不依赖调用方 shell 是否 export 过 NODE_PATH。
# 受管 node 的第三方模块装在 <node>/../workspace/node_modules；写进环境后，
# 所有 subprocess.run([NODE, ...]) 都能继承到，selftest 在任意干净环境都能跑通。
_NODE_MODULES = os.path.abspath(
    os.path.join(os.path.dirname(NODE), "..", "..", "workspace", "node_modules"))
os.environ["NODE_PATH"] = _NODE_MODULES



def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _cli_options(script):
    """从 --help 的**实际输出**解析选项：对拍真实 CLI，避免文档靠手维护产生漂移。"""
    r = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True)
    return set(re.findall(r"--[a-z][a-z0-9-]*", r.stdout))


def _doc_options(skill_md):
    """只取 SKILL.md「## 输入」章节的参数表。

    不能全文扫：变更记录里会提到历史选项名（如已更名的 --area-proxy），
    正文里也会有 CSS 变量名（--topbar-h），全文匹配会产生大量假告警。
    """
    m = re.search(r"^##\s*输入\s*$(.*?)^##\s", skill_md, re.S | re.M)
    if not m:
        raise AssertionError("SKILL.md 缺少「## 输入」章节（文档同步守卫无法定位参数表）")
    return set(re.findall(r"`(--[a-z][a-z0-9-]*)`", m.group(1)))


def check_version_and_docs():
    """版本一致性守卫 + 文档同步守卫（上传规范：SKILL.md / _user_meta.json / README 三处版本必须一致）。"""
    skill_md = _read(os.path.join(SKILL_ROOT, "SKILL.md"))
    readme_p = os.path.join(SKILL_ROOT, "README.md")
    assert os.path.exists(readme_p), "缺少 README.md（上传规范要求技能自带 README）"
    rd = _read(readme_p)
    with open(os.path.join(SKILL_ROOT, "_user_meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)

    m = re.search(r'^version:\s*"?([0-9][0-9.]*)"?\s*$', skill_md, re.M)
    assert m, "SKILL.md frontmatter 缺少 version 字段"
    mv = re.search(r"版本[^\n]{0,16}?(\d+\.\d+\.\d+)", rd)
    assert mv, "README.md 未标注版本号"
    vers = {
        "SKILL.md frontmatter": m.group(1),
        "_user_meta.json": str(meta.get("version", "")),
        "fire_firms_cn.SKILL_VERSION": F.SKILL_VERSION,
        "README.md": mv.group(1),
    }
    uniq = set(vers.values())
    assert len(uniq) == 1, f"版本号不一致（四处必须对拍）：{vers}"
    ver = uniq.pop()
    print(f"✓ 版本一致性：SKILL.md / _user_meta.json / README.md / 脚本常量 均为 {ver}")

    # 文档同步守卫 ①：主脚本 CLI 选项 ⟷ SKILL.md「## 输入」参数表，双向校验
    cli = _cli_options(os.path.join(HERE, "fire_firms_cn.py")) - {"--help"}
    doc = _doc_options(skill_md) - {"--help"}
    missing = sorted(cli - doc)
    ghost = sorted(doc - cli)
    assert not missing, f"SKILL.md 参数表缺少实际存在的选项：{missing}"
    assert not ghost, f"SKILL.md 参数表存在不存在的「幽灵选项」：{ghost}"

    # 文档同步守卫 ②：代理脚本的选项写在「本地服务」章节而非参数表，
    # 故只做单向校验 —— 改名后若文档未同步（正文还留着旧名）能被抓出来
    srv = _cli_options(os.path.join(HERE, "serve_fires.py")) - {"--help"}
    srv_missing = sorted(o for o in srv if o not in skill_md)
    assert not srv_missing, f"SKILL.md 未提及 serve_fires.py 的选项：{srv_missing}"
    print(f"✓ 文档同步守卫：主脚本 {len(cli)} 个选项与参数表双向一致；"
          f"代理脚本 {len(srv)} 个选项在文档中均有记载")


def check_package_layout():
    """Skill 包目录层级守卫：仅支持「根目录/二级目录/文件」，三级嵌套会被上传校验拒绝。

    实证：scripts/lib/leaflet.js（根/scripts/lib/文件 = 三级）导致整包上传报
    「目录层级超限」。本守卫在打包前就能抓住同类问题（含 __pycache__ 等构建产物）。
    """
    bad = []
    for root, dirs, files in os.walk(SKILL_ROOT):
        rel = os.path.relpath(root, SKILL_ROOT)
        if rel == ".":
            continue
        depth = len(rel.split(os.sep))   # 二级目录=1，三级目录=2
        if depth >= 2:
            bad.append(rel.replace(os.sep, "/"))
            dirs[:] = []                 # 三级内部不必再走
    assert not bad, (
        f"存在三级及更深目录（Skill 包仅支持两级结构，上传会被拒），需平移或删除：{bad}")
    print("✓ 包层级守卫：全部条目 ≤ 两级（根/二级目录/文件），无嵌套子目录、无 __pycache__ 残留")


def main():
    # —— 0) 版本一致性与文档同步守卫 + 包层级守卫 ——
    check_version_and_docs()
    check_package_layout()

    # —— 1) 加载内置真实省界（海南） ——
    bpath = os.path.join(SKILL_ROOT, "data", "test_boundary_hainan.json")
    if not os.path.exists(bpath):
        print("✗ 缺少内置边界文件：", bpath)
        return 2
    with open(bpath, encoding="utf-8") as fh:
        feat = json.load(fh)
    boundary_gcj02 = feat["geometry"]
    boundary_wgs = F.gcj02_geom_to_wgs84(boundary_gcj02)
    minx, miny, maxx, maxy = F.geometry_bbox(boundary_wgs)

    # —— 2) 合成火点：3 个在海南境内、3 个在境外，均带真实字段 ——
    rows = [
        # 海南境内
        {"latitude": "19.20", "longitude": "109.70", "brightness": "320", "frp": "50",
         "confidence": "high", "acq_date": "2026-10-26", "acq_time": "0300", "daynight": "N", "satellite": "N"},
        {"latitude": "20.00", "longitude": "110.30", "brightness": "310", "frp": "10",
         "confidence": "nominal", "acq_date": "2026-10-26", "acq_time": "0305", "daynight": "N", "satellite": "N"},
        {"latitude": "18.30", "longitude": "109.50", "brightness": "340", "frp": "200",
         "confidence": "high", "acq_date": "2026-10-26", "acq_time": "0310", "daynight": "N", "satellite": "N"},
        # 境外（应被剔除）
        {"latitude": "39.90", "longitude": "116.40", "brightness": "300", "frp": "5",
         "confidence": "low", "acq_date": "2026-10-26", "acq_time": "0315", "daynight": "D", "satellite": "N"},  # 北京
        {"latitude": "43.80", "longitude": "87.60", "brightness": "300", "frp": "5",
         "confidence": "low", "acq_date": "2026-10-26", "acq_time": "0320", "daynight": "D", "satellite": "N"},  # 乌鲁木齐
        {"latitude": "45.00", "longitude": "126.00", "brightness": "300", "frp": "5",
         "confidence": "low", "acq_date": "2026-10-26", "acq_time": "0325", "daynight": "D", "satellite": "N"},  # 哈尔滨
    ]

    # 包围盒预剔除 + 精确点在多边形（与主流程同一套函数）
    pts = [(float(r["longitude"]), float(r["latitude"]), r) for r in rows]
    box_reject = 0
    features = []
    for lng, lat, row in pts:
        if lng < minx or lng > maxx or lat < miny or lat > maxy:
            box_reject += 1
            continue
        if F.point_in_geometry(lng, lat, boundary_wgs):
            features.append(F.to_feature(lng, lat, row))

    assert len(features) == 3, f"期望保留 3 个海南火点，实际 {len(features)}（包围盒剔除 {box_reject}）"
    # 验证亮度温度字段兼容（合成数据用 brightness，应原样保留）
    assert all(f["properties"]["brightness"] for f in features), "亮度温度字段缺失"
    print(f"✓ 裁切：6 个合成点 → 包围盒剔除 {box_reject} → 落海南 {len(features)} 个")

    # —— 3) GeoJSON ——
    fc = {"type": "FeatureCollection", "features": features}
    gj = os.path.join(tempfile.gettempdir(), "cnfire_selftest.geojson")
    with open(gj, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False)
    assert os.path.getsize(gj) > 100
    print(f"✓ GeoJSON：{gj} ({os.path.getsize(gj)} 字节)")

    # —— 4) 自包含 HTML（含内联 Leaflet，无 CDN 依赖）——
    html = os.path.join(tempfile.gettempdir(), "cnfire_selftest.html")
    provs0, fires0, dates0, conf0, total0 = F.build_province_dataset(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": boundary_gcj02,
             "properties": {"name": "海南省", "adcode": "460000"}}]},
        [(float(r["longitude"]), float(r["latitude"]), r) for r in rows])
    F.build_html(html, "VIIRS S-NPP 375m (C2)", provs0, fires0,
                 dates0, conf0, total0, wms_layer="fires_viirs_snpp_24")
    h = open(html, encoding="utf-8").read()
    assert "unpkg.com" not in h, "不应依赖外部 CDN"
    assert "wgs84togcj02" in h and "circleMarker" in h
    for f in features:
        # 内嵌的火点按 4 位小数压缩，故比对压缩后的值
        assert str(round(float(f["properties"]["longitude"]), 4)) in h, "火点经度未写入 HTML"
        assert str(round(float(f["properties"]["latitude"]), 4)) in h, "火点纬度未写入 HTML"
    print(f"✓ 自包含 HTML：{html} ({os.path.getsize(html)} 字节，内联 Leaflet，无 CDN)")

    # —— 5) 提取业务脚本做 JS 语法校验（node --check）——
    scripts = re.findall(r"<script>(.*?)</script>", h, re.S)
    js = scripts[-1]
    js_path = os.path.join(tempfile.gettempdir(), "cnfire_selftest.js")
    with open(js_path, "w", encoding="utf-8") as fh:
        fh.write(js)
    if os.path.exists(NODE):
        r = subprocess.run([NODE, "--check", js_path], capture_output=True, text=True)
        assert r.returncode == 0, f"JS 语法错误：{r.stderr}"
        print("✓ JS 语法校验（node --check）通过")
    else:
        print("! 未找到 node，跳过 JS 语法校验（仅做字符串断言）")

    # —— 6) 三方式 Tab + 省级筛选 的接线与结构 ——
    # 6-0) 多省测试夹具：海南（真实边界）+ 云南（合成矩形），各放 1 个火点
    yn_box = {"type": "Polygon", "coordinates": [[
        [97.5, 21.1], [106.2, 21.1], [106.2, 29.6], [97.5, 29.6], [97.5, 21.1]]]}
    provinces_geo = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": boundary_gcj02,
         "properties": {"name": "海南省", "adcode": "460000"}},
        {"type": "Feature", "geometry": yn_box,
         "properties": {"name": "云南省", "adcode": "530000"}},
    ]}
    synth_rows = [
        {"latitude": "19.20", "longitude": "109.70", "bright_ti4": "320", "frp": "50",
         "confidence": "high", "acq_date": "2026-10-26", "acq_time": "0300",
         "daynight": "N", "satellite": "N"},
        {"latitude": "25.00", "longitude": "102.00", "bright_ti4": "350", "frp": "20",
         "confidence": "n", "acq_date": "2026-10-26", "acq_time": "0542",
         "daynight": "D", "satellite": "N"},
        {"latitude": "45.00", "longitude": "126.00", "bright_ti4": "300", "frp": "5",
         "confidence": "l", "acq_date": "2026-10-26", "acq_time": "0325",
         "daynight": "D", "satellite": "N"},
    ]

    # 6a) Area API URL 构造与参数校验（纯离线，不联网）
    u = F.area_api_url("KEY", "116,31,118,33", "VIIRS_SNPP_NRT", 1)
    assert u.endswith("/VIIRS_SNPP_NRT/116,31,118,33/1"), f"Area API URL 不符：{u}"
    ud = F.area_api_url("KEY", "116,31,118,33", "VIIRS_SNPP_NRT", 3, "2026-10-20")
    assert ud.endswith("/3/2026-10-20"), f"Area API date 路径段不符：{ud}"
    for bad_bbox, bad_days in [("116,31,118", 1), ("a,b,c,d", 1), ("116,31,118,33", 6)]:
        try:
            F.area_api_url("KEY", bad_bbox, "VIIRS_SNPP_NRT", bad_days)
            raise AssertionError(f"非法参数未被拒绝：{bad_bbox} days={bad_days}")
        except ValueError:
            pass
    assert F._norm_confidence("n") == "nominal" and F._norm_confidence("h") == "high"
    assert F._norm_acq_time("542") == "0542" and F._norm_acq_time("52") == "0052"
    # 6a-2) 回溯窗口 1..10：>5 天必须拆成多段（单次上限 5，DATE 为起始日）
    T = datetime.date(2026, 10, 26)
    assert F.area_api_plan(1, today=T) == [(1, None)], "1 天不应拆段"
    assert F.area_api_plan(5, today=T) == [(5, None)], "5 天不应拆段"
    p10 = F.area_api_plan(10, today=T)
    assert p10 == [(5, "2026-10-17"), (5, "2026-10-22")], f"10 天拆段错误：{p10}"
    # 拆段必须首尾相接、覆盖满 10 天：10-17..10-21 + 10-22..10-26
    assert sum(d for d, _ in p10) == 10, f"拆段总天数应为 10：{p10}"
    assert F.area_api_plan(6, today=T) == [(5, "2026-10-21"), (1, "2026-10-26")]
    assert F.area_api_plan(10, "2026-10-20") == [(5, "2026-10-20"), (5, "2026-10-25")]
    for bad in (0, 11, -1):
        try:
            F.area_api_plan(bad)
            raise AssertionError(f"非法天数未被拒绝：{bad}")
        except ValueError:
            pass
    try:
        F.area_api_plan(3, "2026/10/20")
        raise AssertionError("非法日期格式未被拒绝")
    except ValueError:
        pass
    # 合并去重：两段在边界重复同一行时只保留一条
    hdr = "latitude,longitude,frp,acq_date,acq_time,satellite\n"
    merged = F.merge_area_csv([hdr + "1.0,2.0,3,2026-10-22,100,N\n",
                               hdr + "1.0,2.0,3,2026-10-22,100,N\n1.0,2.1,4,2026-10-23,200,N\n"])
    assert merged.strip().splitlines()[0].startswith("latitude"), "合并后缺表头"
    assert len(merged.strip().splitlines()) == 3, f"去重失败：{merged!r}"
    assert F.merge_area_csv([]) == "", "空输入应返回空串"
    print("✓ Area API 工具：URL 构造 / BBOX+DAYS 校验 / 1..10 天拆段 / 多段去重合并 / 字段归一")

    # 6b) 多省数据集构建：按省归类 + 字段归一
    provs, fires_by_prov, dates, conf_list, total_in = F.build_province_dataset(
        provinces_geo, [(float(r["longitude"]), float(r["latitude"]), r) for r in synth_rows])
    assert len(provs) >= 2, f"应解析出多个省，实际 {len(provs)}"
    assert total_in == 2, f"应有 2 个火点落省，实际 {total_in}"
    hn_rows = fires_by_prov["海南省"]["rows"]
    assert len(hn_rows) == 1, f"海南应有 1 个火点，实际 {len(hn_rows)}"
    r0 = hn_rows[0]
    assert len(r0) == 8, f"紧凑行应有 8 列，实际 {len(r0)}"
    # 亮度必须用 _brightness() 取（VIIRS 是 bright_ti4，直接取 brightness 会得 0）
    assert r0[3] == 320.0, f"亮度温度取值错误：{r0[3]}（应为 320.0）"
    assert conf_list[r0[4]] == "high", f"置信度索引映射错误：{r0[4]}"
    assert r0[6] == 300, f"acq_time 应为 300：{r0[6]}"
    # Area API 的单字母 confidence（l/n/h）须归一为 low/nominal/high
    yn_rows = fires_by_prov["云南省"]["rows"]
    assert len(yn_rows) == 1, f"云南应有 1 个火点，实际 {len(yn_rows)}"
    assert conf_list[yn_rows[0][4]] == "nominal", \
        f"单字母 confidence 未归一：{yn_rows[0][4]}（n 应映射为 nominal）"
    assert yn_rows[0][6] == 542, f"acq_time 应为 542：{yn_rows[0][6]}"
    print("✓ 多省数据集：按省归类 / 亮度列名兼容 / 置信度与时间归一")

    # 6c) HTML 结构：三 Tab + 省级下拉 + 两处 MAP_KEY 输入
    html_wms = os.path.join(tempfile.gettempdir(), "cnfire_selftest_wms.html")
    F.build_html(html_wms, "VIIRS S-NPP 375m (C2)", provs, fires_by_prov,
                 dates, conf_list, total_in,
                 mapkey="DEMO_KEY", wms_layer="fires_viirs_snpp_24")
    hw = open(html_wms, encoding="utf-8").read()
    # 模板里若混入 U+FFFD（替换字符），页面会显示成豆腐块且不抛任何异常，
    # 只能靠扫描字符发现——实测模板里「切换」曾被写成「切\uFFFD\uFFFD」。
    assert "\ufffd" not in hw, "生成的 HTML 含 U+FFFD 替换字符（模板源文件编码损坏）"
    # 三 Tab
    assert hw.count('class="tab') >= 3, "缺少三个方式 Tab"
    for tab in ("csv", "area", "wms"):
        assert f'data-tab="{tab}"' in hw, f"缺少 {tab} Tab"
        assert f'id="pane-{tab}"' in hw, f"缺少 {tab} 面板"
    assert "switchTab" in hw, "缺少 Tab 切换函数"
    # 省级筛选
    assert 'id="provSelect"' in hw, "缺少省级行政区下拉"
    assert "initProvinceSelect" in hw and "onProvinceChange" in hw, "缺少省份切换逻辑"
    assert "PROVINCES" in hw and "FIRES" in hw, "未内嵌省界与火点数据"
    # 两处 MAP_KEY 输入（方式2 与方式3 各一）
    assert 'id="firmsKey"' in hw and 'id="firmsKeyWms"' in hw, "两处 MAP_KEY 输入框缺失"
    assert 'id="wmsApply"' in hw and 'id="areaApply"' in hw, "两处按钮缺失"
    assert 'id="areaSource"' in hw and 'id="areaDays"' in hw, "Area API 控件缺失"
    # 方式2 / 方式3 的接线
    assert "FIRMS_WMS_BASE" in hw and "mapserver/wms/fires/" in hw, "WMS 基础端点常量缺失"
    assert 'INITIAL_KEY = "DEMO_KEY"' in hw, "WMS 初始 KEY 未注入"
    assert "fires_viirs_snpp_24" in hw, "WMS 图层名未内嵌"
    assert "FIRMS WMS 火点（官方栅格）" in hw, "WMS 图层控制项缺失"
    for s in ("VIIRS_SNPP_NRT", "MODIS_NRT", "VIIRS_NOAA21_NRT"):
        assert s in hw, f"Area API SOURCE 列表缺 {s}"
    assert "gcj02towgs84" in hw, "缺少 GCJ-02→WGS-84 逆变换（视野 bbox 无法转 WGS-84）"
    assert "pullAreaFires" in hw, "缺少方式2 拉取函数"
    # file:// 打开时相对路径会解析成 file:///…，必须能探测本机代理
    assert "discoverAreaProxy" in hw, "缺少代理自动发现（file:// 打开将 Failed to fetch）"
    assert "api/fires/health" in hw, "缺少代理健康检查探测端点"
    assert "127.0.0.1:8765" in hw, "缺少本机代理候选地址"
    assert "serve_fires.py" in hw, "缺少「未找到代理」时的可执行指引"
    # v2.1.0：方式2 与行政区划解耦——bbox 只由视野/形状 ∩ 中国范围决定，
    # 断言反转：不得再按所选省收敛（否则隐藏耦合回来了）
    assert "pv.bbox" not in hw and "p0[0]" not in hw, \
        "v2.1.0 起方式2 不应再按所选省收敛 bbox（检测到残留耦合）"
    # 守卫：占位符必须全部被替换。漏替换会让页面拿到字面量 '%AREA_PROXY%'，
    # 浏览器请求 /%AREA_PROXY%?... 直接 404，且不会抛 JS 异常 —— 极易漏测。
    leftovers = re.findall(r"%%[A-Z_]+%%|%[A-Z_]{4,}%", hw)
    assert not leftovers, f"HTML 中残留未替换的占位符：{set(leftovers)}"

    # 6c-2) 方式2 时间范围：天数 1..10 + 历史起始日 + 拆段提示
    assert 'max="10"' in hw, "方式2 天数上限应为 10"
    assert 'id="areaDate"' in hw and 'type="date"' in hw, "缺少历史回溯起始日输入"
    assert 'id="areaPlanHint"' in hw, "缺少「将拆为 N 次请求」提示"
    assert "planAreaRequests" in hw and "mergeCsvRows" in hw, "缺少多段请求与去重合并逻辑"
    assert "AREA_MAX_SINGLE" in hw and "AREA_MAX_WINDOW" in hw, "缺少拆段上限常量"
    # 6c-3) 数据下载（方式1 + 方式2 各两个按钮）
    for bid in ("csvDlCsv", "csvDlGeo", "areaDlCsv", "areaDlGeo"):
        assert f'id="{bid}"' in hw, f"缺少下载按钮 {bid}"
    assert "rowsToCsv" in hw and "rowsToGeoJSON" in hw, "缺少 CSV/GeoJSON 序列化"
    assert "saveFile" in hw and "URL.createObjectURL" in hw, "缺少浏览器端下载实现"
    assert "\\ufeff" in hw, "CSV 未加 BOM（Excel 打开中文会乱码）"
    assert "csvRowsForScope" in hw, "方式1 未按当前范围导出"
    # 6c-4) 方式2 数据缓存：切 Tab 不应丢数据、切省应清空
    assert "areaRowsCache" in hw and "clearAreaData" in hw, "缺少方式2 数据缓存/清理"
    assert "drawAreaRows" in hw, "缺少方式2 图层重绘（切 Tab 回来数据会丢）"
    # 6c-5) 面板控件样式作用域必须跟 DOM 走（v0.5.0 遗留：11 条 #wmsPanel 规则全部落空）
    # 只查真正的 CSS 规则：先剥掉注释（注释里会引用旧选择器名作为历史说明）
    css = re.sub(r"/\*.*?\*/", "", hw, flags=re.S)
    assert not re.search(r"#wmsPanel\s*[.{ ]", css), "存在落空的 #wmsPanel 选择器（面板已改名 pane-*）"
    assert re.search(r"\.tabPane\s+input", css), "面板控件样式未改为 .tabPane 作用域"
    # 6c-6) 顶部统计必须按 Tab 切换口径（v0.6.1 修复：原先永远显示方式1 的点数）
    assert "renderProvStat" in hw and "provStatHtml" in hw, "顶部统计未按当前 Tab 渲染"
    # 6c-7) 视觉规范（v0.6.2）：设计 token / 左列动态定位 / 指标上移 / 状态语义色
    assert ":root{" in hw and "--brand:" in hw and "--sh-3:" in hw, \
        "缺少设计变量 token（颜色/圆角/阴影又散落成硬编码）"
    assert re.search(r"#leftCol\{[^}]*top:calc\(var\(--topbar-h", hw), \
        "左列 top 写死（工具栏换行成两行时会压住左列，实测 280x36px 重叠）"
    assert 'id="info"' not in hw and 'id="scopeName"' not in hw, "v2.0.1 起信息卡应整卡移除，检测到残留节点"
    assert re.search(r"#wmsStatus\.ok,?#wmsStatus", hw) or "#wmsStatus.busy" in hw, \
        "状态区缺少语义色（busy/ok/err）"
    assert "panelIn" in hw and "prefers-reduced-motion" in hw, "缺少面板入场动画与 reduced-motion 降级"
    sw = hw[hw.index("function switchTab("):]
    sw = sw[:sw.index("\nfunction ", 10)] if "\nfunction " in sw[10:] else sw
    # 6c-8) v0.8.0：三个方式的火点图层必须互斥（切 Tab = 摘挂图层，不重建）
    # 这是用户直接反馈的 bug：切到方式2/3 后方式1 的几千个点仍在图上。
    # 只做静态断言不够（这类问题 Chromium 里不报错、页面照常显示），
    # 真正的守卫在文末第 11 步的无头浏览器实测里；这里防的是「代码被改没了」。
    assert "activeTab" in hw and "showLayer" in hw and "hideLayer" in hw, \
        "缺少图层显隐的统一入口（各方式自行 addTo/removeLayer 就会重新发散）"
    assert "if(tab==='csv') showLayer(csvLayer); else hideLayer(csvLayer);" in sw, \
        "switchTab() 未按当前 Tab 隔离方式1 图层（方式2/3 下会残留方式1 的火点）"
    assert "window.__csvBuilds" in hw, \
        "缺少 __csvBuilds 计数：切 Tab 是否退化为「销毁重建」将无法被观测"
    assert "renderProvStat" in sw, "切 Tab 未刷新顶部统计（会一直显示方式1 的点数）"
    for fn in ("renderFireList", "focusFire", "bindFireList"):
        assert fn in hw, f"缺少火点列表函数 {fn}（方式1/方式2 列表失效）"
    for bid in ("csvListBody", "csvListSort", "csvListMeta", "csvListMore",
                "areaListBody", "areaListSort", "areaListMeta", "areaListMore"):
        assert f'id="{bid}"' in hw, f"缺少火点列表控件 {bid}"
    assert "LIST_STEP" in hw, "火点列表缺少分批渲染上限（几千条一次建 DOM 会卡）"
    for fn in ("startDrawRect", "startDrawPoly", "clearShape", "setShape",
               "shapeContains", "updateShapeHint", "cancelDraw"):
        assert fn in hw, f"缺少自由绘制函数 {fn}"
    assert f'id="areaDrawRect"' in hw and f'id="areaDrawPoly"' in hw \
        and f'id="areaClearShape"' in hw, "缺少自由绘制按钮"
    assert f'id="areaShapeHint"' in hw, "缺少「当前范围」提示（用户看不出拉取范围已变成所画区域）"
    assert "DBLCLICK_GUARD_MS" in hw, \
        "缺少双击防抖守卫（Leaflet 双击会先派发两次 click，闭合时多边形会多两个顶点）"
    assert "Escape" in hw, "缺少 Esc 取消绘制（画了一半无法退出）"
    assert 'id="lgShapeRow"' in hw, "图例缺少「已绘制的关注区域」条目"
    # 左列 max-height 必须给左下角比例尺留走廊（加了列表后首次触发重叠）
    m_lc = re.search(r"#leftCol\{(.*?)\}", hw, re.S)
    assert m_lc, "缺少 #leftCol 规则"
    lc = m_lc.group(1).replace("\n", " ").replace("  ", "")
    assert re.search(r"max-height:calc\(100vh - var\(--topbar-h[^)]*\) - 3\d px?\)", lc) \
        or "max-height:calc(100vh - var(--topbar-h, 60px) - 38px)" in lc, \
        f"#leftCol 的 max-height 未为左下角比例尺留出空间（重叠回归风险）：{lc}"

    # 6d) 布局结构守卫：面板走 flex 流式；工具栏高度用 CSS 变量动态测量
    assert "24h CSV" in hw and "Area API" in hw and "WMS 栅格" in hw, "顶部统计未标注数据来源"
    assert re.search(r"#provStat\{[^}]*text-overflow:ellipsis", hw), \
        "#provStat 加了来源前缀后必须可收缩，否则窄屏会把工具栏撑换行"

    # 6d) 布局结构守卫：面板走 flex 流式；工具栏高度用 CSS 变量动态测量
    assert 'id="leftCol"' in hw, "缺少左侧面板列容器 #leftCol"
    assert "#leftCol{" in hw and "display:flex" in hw, "左列未使用 flex 排版"
    assert re.search(r"#wmsPanel\s*\{[^}]*top:", hw) is None, "#wmsPanel 不应使用固定 top 定位"
    assert "syncTopbarHeight" in hw, "工具栏高度未动态测量（窄屏换行会压住 Leaflet 控件）"
    assert "--topbar-h" in hw, "未使用 --topbar-h 变量控制 Leaflet 控件偏移"
    assert 'class="tab-csv"' in hw and "body:not(.tab-csv) #provSelect" in hw, \
        "v2.1.0 起行政区划下拉应只在方式1 显示（body.tab-csv + CSS 收口缺失）"
    print("✓ 界面结构：三 Tab / 省级下拉 / 两处 MAP_KEY 输入 / 动态工具栏高度")

    # —— 7) 布局实测（无头浏览器；缺 playwright 则跳过）——
    # 用真实 getBoundingClientRect() 检出浮动面板重叠/越界。
    # 纯字符串断言抓不到「两个面板压在一起」这类问题，故此处必须真渲染。
    layout_js = os.path.join(HERE, "check_layout.cjs")
    if os.path.exists(NODE) and os.path.exists(layout_js):
        r = subprocess.run([NODE, layout_js, html], capture_output=True, text=True)
        tail = [l for l in r.stdout.strip().splitlines() if l.strip()][-2:]
        assert r.returncode == 0, "布局检测未通过（面板重叠或越界）：\n" + r.stdout[-1500:]
        print("✓ 布局实测：" + " / ".join(t.strip() for t in tail))
    else:
        print("! 未找到 node 或 check_layout.cjs，跳过布局实测")

    # —— 8) 方式2 端到端实测（真实起代理 → 拉取 → 渲染）——
    # 覆盖「无代理 → 起代理 → 点重新检测 → 成功」这条曾经坏掉的链路。
    e2e_js = os.path.join(HERE, "check_area_e2e.cjs")
    if os.path.exists(NODE) and os.path.exists(e2e_js):
        r = subprocess.run([NODE, e2e_js, html], capture_output=True, text=True)
        assert r.returncode == 0, (
            "Area API 端到端实测未通过：\n" + r.stdout[-1500:] + "\n--- stderr ---\n" + r.stderr[-1500:])
        print("✓ Area API 端到端：省级筛选 → 三方式 Tab → Area API 拉取 → WMS 叠加 → 弹窗全通")
    else:
        print("! 未找到 node 或 check_area_e2e.cjs，跳过 Area API 端到端实测")

    # —— 9) 数据下载 / 时间范围 1..10 / 面板样式作用域 端到端 ——
    # 覆盖 v0.6.0 新增能力：真实拦截浏览器下载并校验内容，纯字符串断言抓不到。
    dl_js = os.path.join(HERE, "check_download_e2e.cjs")
    if os.path.exists(NODE) and os.path.exists(dl_js):
        r = subprocess.run([NODE, dl_js, html], capture_output=True, text=True)
        assert r.returncode == 0, (
            "下载 / 时间范围端到端实测未通过：\n" + r.stdout[-2000:] + "\n--- stderr ---\n" + r.stderr[-1500:])
        print("✓ 下载与时间范围端到端：面板样式 / 1..10 天拆段 / 切Tab缓存 / 切省清空 / CSV+GeoJSON 导出")
    else:
        print("! 未找到 node 或 check_download_e2e.cjs，跳过下载端到端实测")

    # —— 9b) v0.10.0 体验增强守卫：左列宽度 / 方式2 产品简介 / 时间时分秒 ——
    feat_js = os.path.join(HERE, "check_v010_features.cjs")
    if os.path.exists(NODE) and os.path.exists(feat_js):
        r = subprocess.run([NODE, feat_js, html], capture_output=True, text=True)
        assert r.returncode == 0, (
            "v0.10.0 体验增强守卫未通过：\n" + r.stdout[-1500:] + "\n--- stderr ---\n" + r.stderr[-1500:])
        print("✓ v0.10.0 守卫：左列 380px / 方式2 产品简介切换 / 时间时分秒")
    else:
        print("! 未找到 node 或 check_v010_features.cjs，跳过 v0.10.0 守卫")

    # —— 9c) v0.10.1 两处视觉 bug 守卫：初始图例串台 / 形状跨 Tab 残留 ——
    bugs_js = os.path.join(HERE, "check_v011_bugs.cjs")
    if os.path.exists(NODE) and os.path.exists(bugs_js):
        r = subprocess.run([NODE, bugs_js, html], capture_output=True, text=True)
        assert r.returncode == 0, (
            "v0.10.1 bug 守卫未通过：\n" + r.stdout[-1500:] + "\n--- stderr ---\n" + r.stderr[-1500:])
        print("✓ v0.10.1 守卫：初始图例隐藏 / 形状跨 Tab 摘挂 / pageerror=0")
    else:
        print("! 未找到 node 或 check_v011_bugs.cjs，跳过 v0.10.1 守卫")

    # —— 9d) v0.10.2 自由绘制边界守卫：Esc 取消/撤销 / 多边形最小点数 ——
    draw_js = os.path.join(HERE, "check_v012_draw.cjs")
    if os.path.exists(NODE) and os.path.exists(draw_js):
        r = subprocess.run([NODE, draw_js, html], capture_output=True, text=True)
        assert r.returncode == 0, (
            "v0.10.2 绘制边界守卫未通过：\n" + r.stdout[-1500:] + "\n--- stderr ---\n" + r.stderr[-1500:])
        print("✓ v0.10.2 守卫：Esc 取消/撤销 / 多边形 ≥3 点闭合 / 输入框 Esc 不误清")
    else:
        print("! 未找到 node 或 check_v012_draw.cjs，跳过 v0.10.2 守卫")

    # —— 9e) 深度交互回归 + 对抗交互回归（数据集无关，覆盖全交互路径与边界情形）——
    # 这两支探针对拍真实运行时行为：初始化 / 切省 / Tab 互斥不重建 / 列表排序分页 /
    # 列表定位 / 自由绘制跨 Tab 摘挂 / WMS 应用 / 空白 Esc / 绘制中切 Tab / 列表耗尽，
    # 全程断言无 console.error 与 pageerror。任何静默回归（如重建泄漏、图层未摘、残留
    # 绘制态）都会被它们抓住——它们接受任意数据集（含下方合成数据），故可固化进 selftest。
    deep_js = os.path.join(HERE, "probe_deep.cjs")
    if os.path.exists(NODE) and os.path.exists(deep_js):
        r = subprocess.run([NODE, deep_js, html], capture_output=True, text=True)
        assert r.returncode == 0, (
            "深度交互回归未通过：\n" + r.stdout[-1500:] + "\n--- stderr ---\n" + r.stderr[-1500:])
        print("✓ 深度交互回归：初始化/切省/Tab 互斥/列表排序分页/定位/绘制跨 Tab/WMS 全绿")
    else:
        print("! 未找到 node 或 probe_deep.cjs，跳过深度交互回归")
    adv_js = os.path.join(HERE, "probe_adversarial.cjs")
    if os.path.exists(NODE) and os.path.exists(adv_js):
        r = subprocess.run([NODE, adv_js, html], capture_output=True, text=True)
        assert r.returncode == 0, (
            "对抗交互回归未通过：\n" + r.stdout[-1500:] + "\n--- stderr ---\n" + r.stderr[-1500:])
        print("✓ 对抗交互回归：空白 Esc/绘制中切 Tab/反复切省/列表耗尽/产品简介 全绿")
    else:
        print("! 未找到 node 或 probe_adversarial.cjs，跳过对抗交互回归")

    # —— 9f) v2.0.0 信息面板统计卡守卫已在 v2.0.1 随整卡移除而撤销（卡片不存在了）——

    # —— 10) 双端一致性：Python area_api_plan() ⟷ HTML 内联 JS planAreaRequests() ——
    # 这两份实现必须同构。任何一侧被单独改都不会报错，只会静默算出不同的请求计划。
    parity_js = os.path.join(HERE, "check_plan_parity.cjs")
    if os.path.exists(NODE) and os.path.exists(parity_js):
        cases = [
            {"days": 1, "date": ""}, {"days": 3, "date": ""}, {"days": 5, "date": ""},
            {"days": 6, "date": ""}, {"days": 7, "date": ""}, {"days": 10, "date": ""},
            {"days": 1, "date": "2026-10-20"}, {"days": 5, "date": "2026-10-20"},
            {"days": 6, "date": "2026-10-20"}, {"days": 10, "date": "2026-10-20"},
            {"days": 7, "date": "2026-02-26"},      # 跨月
            {"days": 10, "date": "2025-12-28"},     # 跨年
        ]
        cases_path = os.path.join(tempfile.gettempdir(), "cnfire_parity_cases.json")
        out_path = os.path.join(tempfile.gettempdir(), "cnfire_parity_js.json")
        with open(cases_path, "w", encoding="utf-8") as fh:
            json.dump(cases, fh)
        r = subprocess.run([NODE, parity_js, cases_path, html, out_path],
                           capture_output=True, text=True)
        assert r.returncode == 0, (
            "双端一致性检测未通过：\n" + r.stdout[-1500:] + "\n--- stderr ---\n" + r.stderr[-1500:])
        with open(out_path, encoding="utf-8") as fh:
            got = json.load(fh)
        diff = []
        for c, got_item in zip(cases, got):
            py = [[d, dt or ""] for d, dt in F.area_api_plan(c["days"], c["date"] or None)]
            if py != got_item["js"]:
                diff.append((c, py, got_item["js"]))
        assert not diff, "双端拆段计划不一致：\n" + "\n".join(
            f"  days={c['days']} date={c['date'] or '(空)'}  python={p}  js={j}"
            for c, p, j in diff)
        print(f"✓ 双端一致性：Python area_api_plan ⟷ JS planAreaRequests "
              f"{len(cases)} 组输入输出完全一致（含跨月 / 跨年 / 无起始日）")
    else:
        print("! 未找到 node 或 check_plan_parity.cjs，跳过双端一致性检测")

    # —— 10b) 「无火点」不得显示成红色错误态（v0.9.0）——
    # E2E 里只在「首拉恰好为空」时才跑得到，数据正常时那条守卫会**空转**。
    # 静态守卫恒被执行：less，两边一起，确保任何时候都不会把「成功但空」说成故障。
    assert "n > 0 ? true : null" in hw, (
        "「该范围内无火点」必须给 setS 传中性状态（null）；传 false 会被渲染成红色报错")
    assert "调到 3–5 天" in hw, (
        "「无火点」文案必须提示把天数调大：days=1 拉空是 NRT 延迟导致的常态，"
        "不给补救建议用户会以为功能坏了")

    # —— 10c) 下载健壮性（v0.9.0）：残片污染 / 临时目录泄漏 / 丢行静默 ——
    # 这三类故障都「不抛异常、产物看着正常」，只有对照式断言能抓住。
    import io
    import contextlib
    with tempfile.TemporaryDirectory(prefix="cnfire_st_dl_") as td:
        _hdr = b"latitude,longitude,bright_ti4,acq_date,acq_time,frp,daynight\n"
        _row = b"19.1,109.7,320.0,2026-10-27,0300,5.0,D\n"
        _body = _hdr + _row * 40
        good_p = os.path.join(td, "good.csv")
        half_p = os.path.join(td, "half.csv")
        open(good_p, "wb").write(_body)
        open(half_p, "wb").write(_body[: int(len(_body) * 0.6)])   # 模拟中途断连的残片

        ok_good, why_good = F._csv_tail_ok(good_p)
        assert ok_good, f"完整 CSV 被误判为残缺：{why_good}"
        ok_half, why_half = F._csv_tail_ok(half_p)
        assert not ok_half, "截断 CSV 未被拦截 —— 残片会污染下一次的缓存命中"
        print(f"✓ CSV 完整性判据：完整文件放行 / 60% 截断被拦截（{why_half[:34]}…）")

        # 缓存命中必须经得起同一套判据
        bad = os.path.join(td, "fires_trunc.csv")
        open(bad, "wb").write(_body[: int(len(_body) * 0.6)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            valid = F._cache_is_valid(bad, 10, None, tail=F._csv_tail_ok)
        assert not valid, "残缺缓存竟然通过了命中校验"
        assert os.path.exists(bad + ".bad"), "损坏缓存未改名存档（会被反复误用）"
        assert "已损坏" in buf.getvalue(), "弃用损坏缓存时必须给出可见的告警"
        print("✓ 缓存命中校验：残缺缓存被拦下并改名存档，且有 [warn] 输出")

        # 坏行必须被**计数**，不能静默丢
        mixed = os.path.join(td, "mixed.csv")
        open(mixed, "w", encoding="utf-8").write(
            "latitude,longitude,frp,daynight\n"
            "19.1,109.7,5.0,D\n"
            ",,5.0,D\n"          # 经纬度为空
            "abc,xyz,5.0,D\n"    # 非数字
            "19.2,109.8,6.0,D\n")
        pts, skipped = F.parse_fires(mixed)
        assert (len(pts), skipped) == (2, 2), f"解析计数不符：pts={len(pts)} skipped={skipped}"
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            F._report_parse_skips(skipped, len(pts), "mixed.csv")
        assert "[warn]" in buf2.getvalue(), "丢弃坏行时必须有可见告警（静默丢数据不可接受）"
        assert "残缺下载" in buf2.getvalue(), "丢弃率 >20% 时应直接点出「可能是残缺下载」"
        print("✓ 坏行可观测：2/4 行被丢弃时有 [warn]，并提示可能是残缺下载")

        # JSON 侧：截断的省界必须 parse 失败而不是「看起来像 GeoJSON」
        gj_ok = os.path.join(td, "ok.json")
        raw = ('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"name":"X"},'
               '"geometry":{"type":"Polygon","coordinates":[[[1,1],[2,2],[1,2],[1,1]]]}}]}')
        open(gj_ok, "w", encoding="utf-8").write(raw)
        gj_bad = os.path.join(td, "bad.json")
        open(gj_bad, "w", encoding="utf-8").write(raw[: len(raw) // 2])
        assert F._json_file_ok(gj_ok)[0], "完整省界 JSON 被误判为损坏"
        assert not F._json_file_ok(gj_bad)[0], "截断省界 JSON 未被拦截"
        print("✓ 省界 JSON 判据：完整放行 / 截断拦截")

        # ④ 临时目录不泄漏（v0.9.0）：download() 的分块临时目录必须回收。
        #    以前每次下载都在系统 Temp 留下一份**同大小**的副本且永不删除
        #    （实测积压 11 个目录 / 33 个文件 / 25.0 MB）。
        #    这条必须**真的跑一次分块路径**：只有 total > chunk 才会创建那个目录，
        #    所以要把 chunk 调到小于数据量，否则守卫会静默空转。
        import http.server

        import threading as _th
        payload = _body * 20
        class _H(http.server.BaseHTTPRequestHandler):
            # 必须支持 Range：**不支持 Range 的服务器对 `-r start-end` 会返回整个文件**，
            # 于是每个分块的大小都对不上（4096 ≠ 35760）→ 全部判定失败 → 走单连接回退。
            # 那样守卫测到的就不是分块路径，清理逻辑的正反两面都覆盖不到。
            def do_GET(self):
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes=") and "-" in rng:
                    a, _, b = rng[6:].partition("-")
                    s = int(a)
                    e = int(b) if b else len(payload) - 1
                    e = min(e, len(payload) - 1)
                    body = payload[s:e + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {s}-{e}/{len(payload)}")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def log_message(self, *a):
                pass
        # 必须用**多线程** server：download() 用 8 并发请求各个分块，单线程的
        # TCPServer 会让大部分分块排队超时 → 走「回退单连接」分支，测的就不是分块路径了。
        _srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        _port = _srv.server_address[1]
        _th.Thread(target=_srv.serve_forever, daemon=True).start()
        _before = set(glob.glob(os.path.join(tempfile.gettempdir(), "cnfire_*")))
        ok_dl, err_dl = F.download(f"http://127.0.0.1:{_port}/x.csv",
                                   os.path.join(td, "dl.csv"),
                                   timeout=30, retries=1, chunk=4096)
        _after = set(glob.glob(os.path.join(tempfile.gettempdir(), "cnfire_*")))
        _srv.shutdown()
        assert ok_dl, f"下载未成功，本条守卫失去意义：{err_dl}"
        leak = _after - _before
        assert not leak, f"分块临时目录泄漏 {len(leak)} 个：{sorted(leak)[:3]}"
        print("✓ 临时目录回收：分块下载后系统 Temp 无 cnfire_* 残留")

    # —— 10d) --clear-cache（v2.2.0）：清空运行时缓存，且清完后技能仍可正常使用 ——
    # 「保证使用」是本守卫的核心：删的必须只是**可再生**的运行时数据，
    # 技能自带离线数据（data/）与代码路径一丝一毫都不能被波及。
    with tempfile.TemporaryDirectory(prefix="cnfire_st_cc_") as td:
        fake = os.path.join(td, "cache")
        os.makedirs(fake)
        for _n, _sz in [("fires_2026-01-01.csv", 4096), ("fires_2026-01-02.csv", 2048),
                        ("cn_provinces.json", 1024), ("fires_trunc.csv.bad", 512)]:
            with open(os.path.join(fake, _n), "wb") as _fh:
                _fh.write(b"x" * _sz)
        os.makedirs(os.path.join(fake, "cnfire_part"))
        open(os.path.join(fake, "cnfire_part", "chunk.a"), "wb").write(b"x")

        r = subprocess.run([sys.executable, os.path.join(HERE, "fire_firms_cn.py"),
                            "--clear-cache", "--cache-dir", fake],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"--clear-cache 退出码非 0：stderr={r.stderr[-400:]}"
        assert os.listdir(fake) == [], f"清空后仍有残留：{os.listdir(fake)}"
        assert "已清空" in r.stdout and "MB" in r.stdout, f"输出缺少清理摘要：{r.stdout[-300:]}"

        # 目录不存在 / 为空时也必须礼貌退出，不能抛异常
        r2 = subprocess.run([sys.executable, os.path.join(HERE, "fire_firms_cn.py"),
                             "--clear-cache", "--cache-dir", os.path.join(td, "no_such")],
                            capture_output=True, text=True)
        assert r2.returncode == 0, "缓存目录不存在时 --clear-cache 不应失败"
        assert "无需清理" in r2.stdout, f"目录不存在时缺少可见提示：{r2.stdout[-300:]}"

        # 「保证使用」①：技能自带离线数据原封不动（省界兜底 + 自检边界）
        _builtin = os.path.join(SKILL_ROOT, "data", "cn_provinces.json")
        _selftest_b = os.path.join(SKILL_ROOT, "data", "test_boundary_hainan.json")
        assert os.path.exists(_builtin) and os.path.getsize(_builtin) > 100_000, \
            "清缓存后技能内置省界丢失 —— 离线兜底被误删，无法保证继续可用"
        assert os.path.exists(_selftest_b), "清缓存后自检边界文件丢失"
        # 「保证使用」②：缓存缺失 → 重新下载的代码路径必须真实存在（静态断言，
        # 不发真请求；联网行为已由 E2E 覆盖）
        _src = _read(os.path.join(HERE, "fire_firms_cn.py"))
        assert "refresh" in _src and "_cache_is_valid(dest" in _src, \
            "load_* 缺少 refresh / 缓存校验分支 —— 清缓存后无法按需重下"
        print("✓ --clear-cache：4 文件 + 1 子目录全清、目录缺失礼貌退出、"
              "data/ 离线兜底原封不动、重下路径在位")

    # —— 11) 图层隔离 / 火点列表 / 自由绘制 端到端（v0.8.0）——
    # 需要真浏览器：这三类都是运行时状态（地图图层成员、DOM、鼠标拖拽），
    # 静态断言抓不到「图层还挂在 map 上」这种不报错、页面也照常显示的缺陷。
    #
    # 夹具必须 >= 150 个火点：列表窗口是 60 条，点数太少时会同时通过
    # 「分页」与「不分页」两种实现，守卫等于空转。
    lay_js = os.path.join(HERE, "check_layers_e2e.cjs")
    if os.path.exists(NODE) and os.path.exists(lay_js):
        html_list = os.path.join(tempfile.gettempdir(), "cnfire_list.html")
        rng_rows, pts150, seed = [], [], 20261027
        rnd = random.Random(seed)
        while len(pts150) < 150:
            lng = rnd.uniform(minx, maxx)
            lat = rnd.uniform(miny, maxy)
            if not F.point_in_geometry(lng, lat, boundary_wgs):
                continue
            r = {"latitude": f"{lat:.4f}", "longitude": f"{lng:.4f}",
                 "bright_ti4": str(rnd.randint(300, 400)),
                 "frp": f"{rnd.uniform(0.5, 300):.1f}",
                 "confidence": rnd.choice(["l", "n", "h"]),
                 "acq_date": rnd.choice(["2026-10-26", "2026-10-25"]),
                 "acq_time": f"{rnd.randint(0, 23):02d}{rnd.randint(0, 59):02d}",
                 "daynight": rnd.choice(["D", "N"]), "satellite": "N"}
            rng_rows.append(r)
            pts150.append((lng, lat, r))
        provs3, fires3, dates3, conf3, tot3 = F.build_province_dataset(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": boundary_gcj02,
                 "properties": {"name": "海南省", "adcode": "460000"}}]}, pts150)
        assert tot3 >= 150, f"列表夹具火点不足：{tot3}（需 >= 150 才能验证 60 条分页窗口）"
        F.build_html(html_list, "VIIRS S-NPP 375m (C2)（自检夹具）", provs3, fires3,
                     dates3, conf3, tot3, wms_layer="fires_viirs_snpp_24")
        r = subprocess.run([NODE, lay_js, html_list], capture_output=True, text=True)
        assert r.returncode == 0, (
            "图层隔离 / 火点列表 / 自由绘制 端到端未通过：\n"
            + r.stdout[-2500:] + "\n--- stderr ---\n" + r.stderr[-1200:])
        print("✓ 图层隔离 / 火点列表 / 自由绘制 端到端：三方式图层互斥 / 切Tab不重建 / "
              "列表排序分页点击定位 / 矩形拖框+多边形勾点+Esc取消")
    else:
        print("! 未找到 node 或 check_layers_e2e.cjs，跳过图层隔离端到端实测")

    print("\n✅ selftest 全部通过：版本与文档守卫 / 裁切 / GeoJSON+CSV / 自包含 HTML / JS 语法 / "
          "方式2 Area API（1..10 天）/ 方式3 WMS / 数据下载 / 布局 / 端到端 / 双端一致性")
    return 0


if __name__ == "__main__":
    sys.exit(main())
