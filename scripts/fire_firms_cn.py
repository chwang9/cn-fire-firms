#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cn-fire-firms —— 中国省份火点获取与可视化（FIRMS 零 Key）

流程：
  1. 从 NASA FIRMS 公开目录下载 24h 全球火点 CSV（默认 VIIRS S-NPP 375m，免 key）
  2. 从阿里 DataV 获取省份行政边界（GCJ-02）
  3. 将边界反算为 WGS-84，对火点做「点在多边形内」裁切
  4. 输出火点 GeoJSON（WGS-84，可直接进 QGIS）
  5. 生成自包含 HTML：Leaflet + 高德卫星影像底图，火点按 FRP 着色、点击弹窗

仅依赖 Python 标准库（csv / json / urllib / subprocess）。
"""

import argparse
import concurrent.futures
import csv
import datetime
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# 技能版本：必须与 SKILL.md frontmatter / _user_meta.json / README.md 四处一致
# （selftest 有「版本一致性守卫」对拍，不一致直接失败）
SKILL_VERSION = "2.2.2"

# ---------------------------------------------------------------------------
# FIRMS 公开目录（24h 全球文件，免申请 key）
# ---------------------------------------------------------------------------
FIRMS_PRODUCTS = {
    "suomi-npp-viirs-c2": {
        "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_24h.csv",
        "label": "VIIRS S-NPP 375m (C2)",
        "wms_layer": "fires_viirs_snpp_24",
        "area_source": "VIIRS_SNPP_NRT",
    },
    "noaa-20-viirs-c2": {
        "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/NOAA20_VIIRS_C2_Global_24h.csv",
        "label": "VIIRS NOAA-20 375m (C2)",
        "wms_layer": "fires_viirs_noaa20_24",
        "area_source": "VIIRS_NOAA20_NRT",
    },
    "viirs-noaa-21": {
        "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/viirs-noaa-21/csv/VIIRS_NOAA21_C2_Global_24h.csv",
        "label": "VIIRS NOAA-21 375m (C2)",
        "wms_layer": "fires_viirs_noaa21_24",
        "area_source": "VIIRS_NOAA21_NRT",
    },
    "modis-c6.1": {
        "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/modis-c6.1/csv/MODIS_C6_1_Global_24h.csv",
        "label": "MODIS C6.1 1km",
        "wms_layer": "fires_modis_24",
        "area_source": "MODIS_NRT",
    },
}

# FIRMS WMS 需要免费的 MAP_KEY，嵌入 URL 路径；申请：
# https://firms.modaps.eosdis.nasa.gov/mapserver/wms-info/#firms-mapkey
FIRMS_WMS_BASE = "https://firms.modaps.eosdis.nasa.gov/mapserver/wms/fires/"

# ---------------------------------------------------------------------------
# FIRMS Area API（方式 2：按包围盒拉数据）
#   https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/{SOURCE}/{BBOX}/{DAYS}[/{DATE}]
# 实测约束（2026-10-26，用公开示例 key 实测确认，非文档转述）：
#   * MAP_KEY 在 URL 路径里；无效 key 返回 400 + 纯文本 "Invalid MAP_KEY."（非 JSON）
#   * BBOX = west,south,east,north，也可写 'world'；必须 4 段，否则 400 "Invalid area."
#   * DAYS 取值 1..5（实测 6 被拒："Invalid day range. Expects [1..5]."）
#   * DATE 是可选的路径段，格式 YYYY-MM-DD（实测 20241001 / 2024-10 均被判 "Invalid date format."）
#   * SOURCE 合法值（官方 /api/area 页面 + 实测 200 双确认）：
#     MODIS_NRT / MODIS_SP / VIIRS_SNPP_NRT / VIIRS_SNPP_SP /
#     VIIRS_NOAA20_NRT / VIIRS_NOAA20_SP / VIIRS_NOAA21_NRT / VIIRS_NOAA21_SP / LANDSAT_NRT
#   * 限频 5000 次 / 10 分钟；响应体无 Access-Control-Allow-Origin（浏览器直连必被 CORS 拦）
# ---------------------------------------------------------------------------
FIRMS_AREA_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
FIRMS_AREA_SOURCES = [
    "VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT",
    "VIIRS_SNPP_SP", "VIIRS_NOAA20_SP", "VIIRS_NOAA21_SP",
    "MODIS_NRT", "MODIS_SP", "LANDSAT_NRT",
]
# 单次请求的 DAYS 上限（实测 6 被拒："Invalid day range. Expects [1..5]."）
FIRMS_AREA_MAX_DAYS = 5
# 对外支持的「回溯窗口」上限：>5 天由 area_api_plan() 拆成多段 <=5 天的请求再合并。
# 拆段依据（2026-10-26 实测）：DATE 是**起始日**，窗口 = [DATE, DATE+DAYS-1] ——
#   5/2026-10-17 → 10-17..10-21 ；5/2026-10-22 → 10-22..10-26 ，两段拼接正好 10 天。
FIRMS_AREA_MAX_WINDOW = 10

DATAV_PROVINCES = "https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json"

# ---------------------------------------------------------------------------
# 本地缓存 —— 区分「技能自带静态数据」与「运行时下载缓存」
#
# 为什么必须分开：早期版本把两者都写在技能安装目录的 data/ 下，结果
#   data/ 每跑一天就新增一份 5-7MB 的 FIRMS 全球 24h 文件（实测两天就到 12MB），
#   技能包体积随时间线性膨胀、根本无法分发。
#   · data/        ：随技能分发的静态数据（离线兜底），只读，体积恒定
#   · 用户缓存目录 ：运行时下载的火点文件，自动按日期轮转清理（默认留最近 3 天）
# ---------------------------------------------------------------------------
CACHE_DIR_OVERRIDE = None    # 由 --cache-dir 注入
CACHE_KEEP_DAYS = 3          # 由 --cache-keep 注入
_CACHE_RESOLVED = False


def _builtin_dir():
    """技能自带静态数据目录（随技能分发。**只读用途**，绝不在此写运行时数据）。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".wtest")
        with open(probe, "w") as fh:
            fh.write("1")
        os.remove(probe)
        return True
    except OSError:
        # 这里的静默是刻意的：cache_dir() 会依次试多个候选目录，
        # 中途不可写属正常流程；若**全部**不可写，cache_dir() 会明确告警，
        # 兜底分支因此在上层是可观测的，不会变成无人知晓的失败。
        return False


def cache_dir():
    """运行时下载缓存目录。

    优先顺序：--cache-dir > 环境变量 CN_FIRE_CACHE > 系统用户缓存目录 > 技能 data/cache。

    注意最后一档回退**不能**用 tempfile.mkdtemp()：临时目录每次进程都新建，
    缓存复用会完全失效（每天重新下载 7MB 却不报任何错，属于典型的静默失败）。
    宁可写在技能目录的 cache/ 子目录，也要保证跨进程可复用。
    """
    global _CACHE_RESOLVED
    cands = []
    if CACHE_DIR_OVERRIDE:
        cands.append(CACHE_DIR_OVERRIDE)
    env = os.environ.get("CN_FIRE_CACHE")
    if env:
        cands.append(env)
    appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if appdata:
        cands.append(os.path.join(appdata, "cn-fire-firms"))
    cands.append(os.path.join(os.path.expanduser("~"), ".cache", "cn-fire-firms"))
    cands.append(os.path.join(_builtin_dir(), "cache"))

    for d in cands:
        if _writable(d):
            if not _CACHE_RESOLVED:
                _CACHE_RESOLVED = True
                _migrate_legacy_cache(d)
            return d
    d = tempfile.mkdtemp(prefix="cnfire_cache_")
    print(f"  [warn] 所有候选缓存目录都不可写，本次回退到临时目录 {d}："
          f"跨进程缓存复用失效，下次运行会重新下载。可用 --cache-dir 指定可写路径")
    return d


def _migrate_legacy_cache(new_dir):
    """把历史版本残留在技能 data/ 下的火点 CSV 迁到新缓存目录，并清掉原文件。

    这是技能包瘦身的收尾：否则老用户升级后，data/ 里的旧文件永远不会消失。
    """
    legacy = _builtin_dir()
    try:
        names = os.listdir(legacy)
    except OSError:
        return 0
    moved = 0
    for n in names:
        if not (n.startswith("fires_") and n.endswith(".csv")):
            continue
        src, dst = os.path.join(legacy, n), os.path.join(new_dir, n)
        try:
            if os.path.exists(dst):
                os.remove(src)
            else:
                os.replace(src, dst)
            moved += 1
        except OSError as e:
            print(f"  [warn] 旧缓存 {n} 迁移失败（已跳过，可手动删除）：{e}")
    if moved:
        print(f"  ✓ 已将技能包内 {moved} 份历史火点缓存迁出到 {new_dir}（保持技能包体积恒定）")
    return moved


# 需要按文件名里的日期轮转清理的缓存文件
_DATED_CACHE = re.compile(r"^fires_(\d{4}-\d{2}-\d{2})\.csv$")


def prune_cache(d=None, keep=None, quiet=False):
    """清理按日期命名的火点缓存，只保留最近 keep 份。

    FIRMS 24h 文件每日一更，历史文件除了占空间没有复用价值；不清理的话
    缓存目录会随使用天数无限增长（这正是早期版本把技能包撑到几十 MB 的原因）。
    """
    d = d or cache_dir()
    keep = CACHE_KEEP_DAYS if keep is None else keep
    if keep <= 0:
        return 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    dated = [(m.group(1), n) for n in names for m in [_DATED_CACHE.match(n)] if m]
    if len(dated) <= keep:
        return 0
    dated.sort()
    removed = freed = 0
    for _, n in dated[:len(dated) - keep]:
        p = os.path.join(d, n)
        try:
            freed += os.path.getsize(p)
            os.remove(p)
            removed += 1
        except OSError as e:
            # 删不掉要留下痕迹：缓存清理失败会让磁盘持续增长，
            # 而它既不报错也不影响出结果，是最容易被忽略的一类问题
            print(f"  [warn] 过期缓存 {n} 删除失败：{e}")
    if removed and not quiet:
        print(f"  ✓ 已清理 {removed} 份过期火点缓存（释放 {freed / 1048576:.1f} MB），"
              f"保留最近 {keep} 天")
    return removed


def clear_cache(d=None):
    """一键清空运行时缓存目录内的全部文件（--clear-cache），清完即退出。

    与 prune_cache 的分工：prune 只按日期轮转火点 CSV（保留最近 N 天），
    本函数把目录里**所有**条目清空（含 cn_provinces.json 运行时副本、
    .bad 损坏存档、历史残留子目录），用于「彻底重置」场景。

    「删了缓存还能用」的保证链（删的只是可再生的运行时数据）：
      · 省界：技能 data/cn_provinces.json 内置离线兜底副本不受影响（只读静态数据）；
      · 火点：缓存缺失时 load_fires_csv() 会直接重新下载，无缓存也能出图；
      · 边界：本函数只动「运行时缓存目录」这一个目录，绝不触及技能安装目录
        与用户输出目录。
    """
    d = d or cache_dir()
    try:
        names = os.listdir(d)
    except OSError as e:
        print(f"  [warn] 缓存目录 {d} 无法读取（可能不存在或从未生成过缓存），无需清理：{e}")
        return 0
    count = freed = 0
    for n in names:
        p = os.path.join(d, n)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)   # 历史上可能残留的分块临时子目录
            else:
                freed += os.path.getsize(p)
                os.remove(p)
            count += 1
        except OSError as e:
            # 删不掉必须留痕：静默失败会让用户以为清干净了，实际还在占空间
            print(f"  [warn] 缓存条目 {n} 删除失败：{e}")
    print(f"  ✓ 已清空运行时缓存 {d}：删除 {count} 项，释放 {freed / 1048576:.1f} MB")
    print("    技能自带离线数据（data/）不受影响；下次运行将按需重新下载数据，不影响使用。")
    return count


def load_provinces(refresh=False, dest=None):
    """获取 DataV 全国省界。

    查找顺序：缓存目录副本 → **技能内置副本**（离线兜底，随技能分发）→ 在线下载。
    内置副本是只读的：即便技能安装目录不可写也能正常工作，也不会因为
    --refresh 把运行时数据写回技能包。
    """
    cache = os.path.join(cache_dir(), "cn_provinces.json")
    builtin = os.path.join(_builtin_dir(), "cn_provinces.json")
    dest = dest or cache
    # 命中缓存也必须「先验证后使用」。v0.9.0：过去只判 size>1000，而一份被 CTRL-C
    # 掐断的半截 JSON 往往远超 1000 字节 —— 于是 features 少一半、省份下拉框缺
    # 项，全链路无一处报错。体积达标不等于内容完整，必须真读一次。
    if (not refresh) and _cache_is_valid(dest, 200_000, None, tail=_json_file_ok):
        return _load_json(dest, "省份边界缓存")
    if os.path.exists(builtin) and os.path.getsize(builtin) > 1000:
        with open(builtin, encoding="utf-8") as fh:
            return json.load(fh)
    # 火点 CSV 至少是 MB 级；要求首行表头含 date/acq_date 与经纬度，
    # 可挡住「把代理错误页当数据」导致的空白地图（见 _payload_ok 实证）。
    ok, err = download(DATAV_PROVINCES, dest, min_size=200_000, tail=_json_file_ok)
    if not ok:
        raise RuntimeError(f"省份边界下载失败：{err}")
    try:
        with open(dest, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError(f"省份边界文件不是合法 JSON（疑似收到错误页）：{e}")


def _load_json(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError(f"{label}不是合法 JSON（疑似收到错误页）：{e}")


def _cache_is_valid(path, min_size, sniff, tail=None, bad_suffix=".bad"):
    """缓存命中前的完整性校验（v0.9.0 新增）。

    通过则返回 True；否则把坏文件改名存档并**明确告知**——这是最后一处能把「残缺数据」
    挡在门外的地方。改名而不是删除，理由：既保证后续不再误用，也留下现场供排查，
    同时 `prune_cache` 的文件名规则（`^fires_\\d{4}-\\d{2}-\\d{2}\\.csv$`）对它不生效，
    不会破坏「保留最近 3 天」的语义。
    """
    ok, why = _payload_ok(path, min_size, sniff, tail)
    if ok:
        return True
    name = os.path.basename(path)
    print(f"  [warn] 缓存 {name} 已损坏，弃用并重新下载：{why}")
    try:
        os.replace(path, path + bad_suffix)
    except OSError as e:
        print(f"  [warn] 损坏缓存 {name} 改名失败（若不移除会持续被误用）：{e}")
    return False


def load_fires_csv(url, refresh=False, dest=None):
    """获取 FIRMS 全球 24h 文件；按日期缓存（24h 文件每日更新）。

    文件落在**运行时缓存目录**（不再是技能安装目录），并在写之前先做轮转清理，
    保证占用磁盘与使用频次无关。下载失败时若还有旧副本则明确提示降级原因。
    """
    d = cache_dir()
    prune_cache(d, quiet=True)   # 每次调用顺带轮转，不必依赖用户记得清理
    today = datetime.date.today().isoformat()
    dest = dest or os.path.join(d, f"fires_{today}.csv")
    # FIRMS 全球 24h 文件约 5-8 MB；要求首行表头含经纬度与日期列，
    # 可挡住「把代理错误页当数据」导致的 0 火点空白地图（见 _payload_ok 实证）。
    # v0.9.0：命中缓存同样要做这套校验，且必须带 min_size —— 少了下限校验的话，
    # 一份 2KB 的网关错误页只要恰好含 latitude/longitude 字样就能蒙混过关。
    # tail=_csv_tail_ok 是关键：只看表头拦不住「上半截」式截断（表头完好、体积达标）。
    _csv_sniff = _sniff_csv("latitude", "longitude", "acq_date")
    if (not refresh) and _cache_is_valid(dest, 100_000, _csv_sniff, tail=_csv_tail_ok):
        return dest
    ok, err = download(url, dest, min_size=100_000, sniff=_csv_sniff,
                       tail=_csv_tail_ok)
    if not ok:
        if os.path.exists(dest) and os.path.getsize(dest) > 100_000:
            print(f"  [warn] 本次下载失败，回退到上一次的缓存副本：{err}")
            return dest
        raise RuntimeError(f"FIRMS 火点文件下载失败：{err}")
    return dest


# ---------------------------------------------------------------------------
# 几何包围盒（用于裁切前的快速剔除，避免对全部全球点做点在多边形判定）
# ---------------------------------------------------------------------------
def _bbox_coords(coords):
    if len(coords) >= 2 and isinstance(coords[0], (int, float)):
        x, y = coords[0], coords[1]
        return x, y, x, y
    xs, ys = [], []
    for c in coords:
        x0, y0, x1, y1 = _bbox_coords(c)
        xs += [x0, x1]
        ys += [y0, y1]
    return min(xs), min(ys), max(xs), max(ys)


def geometry_bbox(geom):
    return _bbox_coords(geom["coordinates"])

# ---------------------------------------------------------------------------
# GCJ-02 <-> WGS-84 坐标变换（近似逆变换，足够裁切/对齐使用）
# ---------------------------------------------------------------------------
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lng, lat):
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(lng, lat):
    ret = (-100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat
           + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng)))
    ret += (20.0 * math.sin(6.0 * lng * math.pi)
            + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi)
            + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi)
            + 320.0 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(lng, lat):
    ret = (300.0 + lng + 2.0 * lat + 0.1 * lng * lng
           + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng)))
    ret += (20.0 * math.sin(6.0 * lng * math.pi)
            + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * math.pi)
            + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * math.pi)
            + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng, lat):
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / ((_A / sqrtmagic) * math.cos(radlat) * math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng, lat):
    # 单步近似逆变换（误差 < 几米，裁切足够）
    glng, glat = wgs84_to_gcj02(lng, lat)
    return lng * 2 - glng, lat * 2 - glat


def _conv_coords(coords):
    if (len(coords) >= 2 and isinstance(coords[0], (int, float))
            and isinstance(coords[1], (int, float))):
        return list(gcj02_to_wgs84(coords[0], coords[1]))
    return [_conv_coords(c) for c in coords]


def gcj02_geom_to_wgs84(geom):
    return {"type": geom["type"], "coordinates": _conv_coords(geom["coordinates"])}


# ---------------------------------------------------------------------------
# 点在多边形内判定（支持 Polygon / MultiPolygon / GeometryCollection）
# ---------------------------------------------------------------------------
def _in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _in_polygon(x, y, poly):
    if not _in_ring(x, y, poly[0]):
        return False
    for hole in poly[1:]:
        if _in_ring(x, y, hole):
            return False
    return True


def point_in_geometry(x, y, geom):
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Polygon":
        return _in_polygon(x, y, c)
    if t == "MultiPolygon":
        return any(_in_polygon(x, y, p) for p in c)
    if t == "GeometryCollection":
        return any(point_in_geometry(x, y, g) for g in geom.get("geometries", []))
    return False


# ---------------------------------------------------------------------------
# 省份名匹配（容忍「省/市/自治区/特别行政区」后缀与简称）
# ---------------------------------------------------------------------------
def _norm_name(s):
    s = (s or "").strip()
    for suf in ["维吾尔自治区", "壮族自治区", "回族自治区", "自治区",
                "特别行政区", "省", "市"]:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


def find_province(geo, name):
    feats = geo.get("features", [])
    for f in feats:
        if f["properties"].get("name") == name:
            return f
    t = _norm_name(name)
    for f in feats:
        if _norm_name(f["properties"].get("name", "")) == t:
            return f
    for f in feats:
        n = _norm_name(f["properties"].get("name", ""))
        if t and (t in n or n in t):
            return f
    return None


# ---------------------------------------------------------------------------
# 下载（分块并行 + 失败分块递归二分重试，规避网关限速/截断；
#       失败回退单连接 curl，再回退 urllib）
# ---------------------------------------------------------------------------
def _content_length(url, timeout, _probe_note=None):
    """探测远端文件长度；失败时返回 None，并把原因记进 _probe_note（调用方可打印）。

    _probe_note 存在的意义：探测失败会让 download() 退化到「单连接整文件」分支，
    在限速网关下这条分支比并发分块慢一个数量级。不记录就等于隐藏了性能根因。
    """
    _probe_note = _probe_note if _probe_note is not None else []
    try:
        out = subprocess.run(
            ["curl", "-sI", "--max-time", str(timeout), url],
            capture_output=True, text=True, check=False,
        ).stdout
        for line in out.splitlines():
            low = line.lower()
            # 状态码必须先校验。实证：本机走代理时被限流/不可达的目标会返回
            # 「HTTP/1.1 502 Bad Gateway + Content-Length: 70」的错误页——它同样带
            # Content-Length，只看长度会把错误页当成文件大小，进而把错误页当数据。
            if low.startswith("http/"):
                code = line.split()[1] if len(line.split()) > 1 else ""
                if not code.startswith("2"):
                    _probe_note.append(f"curl -I 返回 {line.strip()}")
                    return None
            if low.startswith("content-length:"):
                return int(line.split(":")[1].strip())
        _probe_note.append("curl -I 未返回 Content-Length")
    except Exception as e:
        # 必须留下痕迹：探测失败会让下载走另一条分支，静默 pass 会让排障无法下手
        _probe_note.append(f"curl -I 探测异常：{type(e).__name__}: {e}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"},
                                     method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # urllib 对 4xx/5xx 会抛 HTTPError 走 except，这里必然是 2xx
            cl = r.headers.get("Content-Length")
            if cl:
                return int(cl)
        _probe_note.append("HEAD 请求未返回 Content-Length")
    except Exception as e:
        _probe_note.append(f"HEAD 探测异常：{type(e).__name__}: {e}")
    return None


def _download_range(url, start, end, dest, timeout, depth=0,
                    min_size=200_000, max_depth=7):
    """下载字节区间 [start,end]；失败时递归二分直到成功或低于最小块。"""
    size = end - start + 1
    for _ in range(3):
        try:
            rc = subprocess.run(
                ["curl", "-sL", "--max-time", str(timeout),
                 "-r", f"{start}-{end}", url, "-o", dest],
                check=False,
            )
            if rc.returncode == 0 and os.path.getsize(dest) == size:
                return True
            if rc.returncode != 0:
                print(f"  [warn] 分块 {start}-{end} 下载失败（curl rc={rc.returncode}），准备重试/二分")
        except Exception as e:
            print(f"  [warn] 分块 {start}-{end} 异常：{type(e).__name__}: {e}")
    if size > min_size and depth < max_depth:
        mid = (start + end) // 2
        d1, d2 = dest + ".a", dest + ".b"
        if (_download_range(url, start, mid, d1, timeout, depth + 1)
                and _download_range(url, mid + 1, end, d2, timeout, depth + 1)):
            try:
                with open(d1, "rb") as a, open(d2, "rb") as b, open(dest, "wb") as out:
                    out.write(a.read())
                    out.write(b.read())
                return os.path.getsize(dest) == size
            except Exception:
                return False
    return False


def _single_curl(url, dest, timeout, retries):
    for _ in range(retries):
        rc = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), url, "-o", dest],
            check=False,
        )
        if rc.returncode == 0 and os.path.getsize(dest) > 100:
            return True, ""
    return False, "curl 失败"


def _payload_ok(dest, min_size=0, sniff=None, tail=None):
    """校验落盘内容真的是数据，而不是网关错误页。

    实证（本机环境）：经过代理访问被限流/不可达的目标时，会收到
    「HTTP/1.1 502 Bad Gateway / Content-Length: 70」的纯文本错误页。它同样有
    Content-Length、体积极可能超过「>100 字节」这个旧判据 —— 于是错误页会被
    当成数据落盘，下游解析出 0 条火点，**地图一片空白却不报任何错**。

    tail 是 v0.9.0 加的第二类判据（sniff 只读头部）。理由见 _csv_tail_ok。
    """
    try:
        size = os.path.getsize(dest)
    except OSError as e:
        return False, f"无法读取下载结果：{e}"
    if size < min_size:
        return False, (f"下载结果仅 {size} 字节，小于预期下限 {min_size} 字节"
                       f"（很可能是网关返回的错误页）")
    if sniff is not None:
        try:
            with open(dest, "rb") as fh:
                head = fh.read(4096)
        except OSError as e:
            return False, f"读取下载结果失败：{e}"
        ok, why = sniff(head)
        if not ok:
            return False, (f"下载结果不是预期的数据文件（{why}）；"
                           f"前 80 字节 = {head[:80]!r}")
    if tail is not None:
        return tail(dest)
    return True, ""


def _csv_tail_ok(path, probe=8192):
    """CSV **尾部**完整性校验 —— v0.9.0 的核心判据。

    为什么只看表头和体积不够：连接被掐时留在磁盘上的往往是文件的**前半截**
    （实测 272,123 字节的文件留下 136,123 字节、正好 50%）。它表头完好、
    体积远超任何合理的 min_size，`_sniff_csv` 全部通过 —— 于是静默少一半数据。
    反过来，一个完整 CSV 必然以换行符结束，且末行列数必然等于表头列数；
    这两个条件都不成立时，几乎只可能是截断。**「最后一行的换行符」是这里唯一
    廉价且强力的证据**，代价仅一次 8KB 的尾部读取。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(4096)
            if size > probe:
                fh.seek(size - probe)
                tail = fh.read(probe)
            else:
                tail = head
    except OSError as e:
        return False, f"读取待校验文件失败：{e}"
    head_txt = head.decode("utf-8", "replace")
    first = head_txt.splitlines()[0] if head_txt.strip() else ""
    ncol = first.count(",") + 1
    tail_txt = tail.decode("utf-8", "replace")
    lines = tail_txt.splitlines()
    if not tail_txt.endswith(("\n", "\r")):
        last = lines[-1] if lines else ""
        return False, (f"文件不以换行结束，最后一行为半截数据（…{last[-48:]!r}）；"
                       f"通常是下载被中途掐断留下的残片")
    last = lines[-1] if lines else ""
    got = last.count(",") + 1
    if last and ncol > 1 and got != ncol:
        return False, f"末行只有 {got} 列，而表头有 {ncol} 列（疑似截断）"
    return True, ""


def _json_file_ok(path):
    """JSON 完整性校验：整份 parse 一遍，且 features 必须非空。

    比只看前 4KB 更可靠 —— 截断的 JSON 几乎必然在 features 数组中间断掉，
    parse 会直接失败；而只看头部标记会把这类文件判为「看起来是 GeoJSON」。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (ValueError, UnicodeDecodeError) as e:
        return False, f"不是合法 JSON（{e}）；通常是下载被掐断留下的残片"
    if not isinstance(obj, dict) or not obj.get("features"):
        return False, "JSON 中没有 features 数组（内容不完整）"
    return True, ""


def _sniff_csv(*required_cols):
    """生成 CSV 嗅探器：要求首行（表头）包含全部指定列名。"""
    def _f(head):
        try:
            first = head.decode("utf-8", "replace").splitlines()[0] if head.strip() else ""
        except Exception as e:  # noqa: BLE001
            return False, f"无法解码：{e}"
        missing = [c for c in required_cols if c not in first]
        if missing:
            return False, f"表头缺少列 {'/'.join(missing)}"
        return True, ""
    return _f


def _discard(path):
    """删除半成品文件；失败也要留痕（删不掉会持续污染下一次的缓存命中判定）。"""
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError as e:
        print(f"  [warn] 无法删除半成品文件 {os.path.basename(path)}：{e}")
        return False
    return True


def download(url, dest, timeout=120, retries=3, chunk=1_000_000,
             min_size=0, sniff=None, tail=None):
    """下载文件。min_size / sniff 用于校验「下载到的确实是数据」，
    避免把代理返回的错误页当成数据收下（详见 _payload_ok 的实证说明）。

    **原子写（v0.9.0 修复）**：所有内容先落到 `<dest>.part`，通过 _payload_ok
    校验后才 `os.replace()` 到 dest。

    取证（本地起 HTTP 服务，只发一半就断连）：改前 download() 把字节直接写进 dest，
    一旦中途被掐（网关截断 / 超时 / Ctrl-C），dest 里就留下一份 **136,123 字节、正好
    50%** 的半截 CSV；而下一轮的缓存命中判据只看 `size > 1000` —— 半截文件照样通过，
    于是「火点少一半」这件事**既不报错也不告警**，出来的图看着还很正常。
    """
    part = dest + ".part"

    def _finish():
        ok, why = _payload_ok(part, min_size, sniff, tail)
        if not ok:
            # 关键：校验不通过必须删掉半成品。留着它会被下一轮的缓存命中当成有效数据
            _discard(part)
            return False, why
        try:
            os.replace(part, dest)   # 原子替换：dest 要么是完整的新文件，要么保持旧样
            return True, ""
        except OSError as e:
            _discard(part)
            return False, f"无法写入目标文件：{e}"

    # 开始之前先清掉上一轮可能残留的半成品，避免它干扰本次的大小判定
    _discard(part)

    # 1) 分块并行 + 失败二分重试
    notes = []
    total = _content_length(url, timeout, notes)
    if not total:
        # 拿不到长度意味着走不了并发分块，只能单连接下载 —— 在限速网关下会明显更慢，
        # 必须让用户知道「慢」的原因，而不是干等。
        print("  [warn] 未能探测到文件大小（%s），退化为单连接下载（限速网关下会明显更慢）"
              % ("；".join(notes) or "未知原因"))
    if total and total > chunk:
        ranges = [(i, min(i + chunk - 1, total - 1)) for i in range(0, total, chunk)]
        tmp = tempfile.mkdtemp(prefix="cnfire_")
        parts = [os.path.join(tmp, f"p{i:04d}") for i in range(len(ranges))]
        starts = [s for s, _ in ranges]
        ends = [e for _, e in ranges]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                res = list(ex.map(_download_range,
                                  [url] * len(ranges), starts, ends, parts,
                                  [timeout] * len(ranges)))
            if all(res):
                try:
                    with open(part, "wb") as out:
                        for p in parts:
                            with open(p, "rb") as f:
                                out.write(f.read())
                except OSError as e:
                    print(f"  [warn] 分块拼接写入失败：{e}")
                else:
                    if os.path.getsize(part) == total:
                        return _finish()
                    print("  分块拼接大小不符，回退单连接")
            else:
                print("  部分分块失败，回退单连接")
        finally:
            # v0.9.0：这个目录以前从不回收 —— 分块下载一份 7MB 的全球文件会在系统临时
            # 目录里留下同样 7MB 的分块文件，且**永不删除**（实测 Temp 下积压 11 个目录
            # / 33 个文件 / 25.0 MB）。失败时的 `.a`/`.b` 二分残片同理。
            shutil.rmtree(tmp, ignore_errors=True)
    # 2) 单连接 curl
    ok, err = _single_curl(url, part, timeout, retries)
    if ok:
        return _finish()
    # 3) urllib 兜底
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (cn-fire-firms)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if len(data) > 100:
            with open(part, "wb") as fh:
                fh.write(data)
            return _finish()
        err = "urllib 响应过小"
    except Exception as e:
        err = f"urllib 失败: {e}"
    _discard(part)
    return False, err


# ---------------------------------------------------------------------------
# 解析 FIRMS CSV
# ---------------------------------------------------------------------------
def parse_fires(csv_path):
    """解析 FIRMS 全局 CSV。返回 (pts, skipped)。

    v0.9.0：skipped 是**必需的返回值**，不是可选装饰。原先坏行被 `continue` 直接吞掉，
    连计数都没有 —— 实测一份「2 行正常 + 2 行经纬度缺失」的文件会静默丢掉一半，
    返回的长度看着正常，无人知道有过丢弃。缺列/非数字在现实数据里确实存在（截断、
    版本升级导致的列名变化），必须让它浮出水面。
    """
    pts = []
    skipped = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                lat = float(row["latitude"])
                lng = float(row["longitude"])
            except (KeyError, ValueError, TypeError):
                skipped += 1
                continue
            pts.append((lng, lat, row))
    return pts, skipped


def _report_parse_skips(skipped, kept, label):
    """汇报解析期丢弃的行数 —— 让「静默丢数据」不可能再发生。

    超过 20% 单独加一句结语：普通脏数据不可能达到这个比例，一旦达到基本可以断定
    是**下载被截断**（半截 CSV 的最后一行正好断在中间，[A] 类故障的后半段），
    光说「跳过了 N 行」用户未必会联想到缓存。
    """
    if not skipped:
        return
    total = skipped + kept
    pct = skipped / total * 100 if total else 0.0
    msg = f"  [warn] {label}：{skipped}/{total} 行因经纬度缺失或非法被跳过（{pct:.1f}%）"
    if pct > 20:
        msg += "。占比过高，该文件很可能是残缺下载，建议用 --refresh-fires 重新获取"
    print(msg)


def _brightness(row):
    """亮度温度字段名因产品而异：MODIS 为 brightness/bright_t31，
    VIIRS C2 为 bright_ti4/bright_ti5。统一取第一个非空值。"""
    for k in ("brightness", "bright_ti4", "bright_ti5", "bright_t31"):
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


_CONF_MAP = {"l": "low", "n": "nominal", "h": "high",
             "low": "low", "nominal": "nominal", "high": "high"}


def _norm_confidence(v):
    """Area API 的 confidence 用单字母 l/n/h，全局 CSV 用 low/nominal/high，统一到后者。"""
    if v is None:
        return None
    s = str(v).strip()
    return _CONF_MAP.get(s.lower(), s)


def _norm_acq_time(v):
    """acq_time 为 UTC HHMM。Area API 会省略前导零（'542' 实为 05:42），统一补齐 4 位。"""
    if v is None:
        return None
    s = str(v).strip()
    if s.isdigit() and len(s) < 4:
        s = s.zfill(4)
    return s


def to_feature(lng, lat, row):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(lng, 5), round(lat, 5)]},
        "properties": {
            "longitude": row.get("longitude"),
            "latitude": row.get("latitude"),
            "brightness": _brightness(row),
            "frp": row.get("frp"),
            "confidence": _norm_confidence(row.get("confidence")),
            "acq_date": row.get("acq_date"),
            "acq_time": _norm_acq_time(row.get("acq_time")),
            "daynight": row.get("daynight"),
            "satellite": row.get("satellite"),
        },
    }


# ---------------------------------------------------------------------------
# FIRMS Area API（方式 2：按包围盒拉数据）
# ---------------------------------------------------------------------------
def validate_area_days(days):
    """对外（CLI / 界面）校验回溯窗口天数：1..FIRMS_AREA_MAX_WINDOW。

    注意区分两个上限：
      * FIRMS_AREA_MAX_DAYS   —— **单次请求**的 DAYS 上限 5（API 硬限制）
      * FIRMS_AREA_MAX_WINDOW —— 本技能支持的**回溯窗口**上限 10，
        >5 天由 area_api_plan() 拆成多段请求再合并，对用户是透明的
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError(f"DAYS 必须为整数，收到：{days!r}")
    if not (1 <= days <= FIRMS_AREA_MAX_WINDOW):
        raise ValueError(
            f"DAYS 只能取 1..{FIRMS_AREA_MAX_WINDOW}（超过 {FIRMS_AREA_MAX_DAYS} 天会自动拆成多段请求），收到：{days}")
    return days


def area_api_plan(days=1, date=None, today=None):
    """把 N 天回溯窗口切成若干段 <=5 天的请求。

    返回 [(days, date_or_None), ...]，date 为 None 表示该段不带 DATE 路径段。

    实测依据（2026-10-26）：DATE 是**起始日**，窗口 = [DATE, DATE+DAYS-1]。
      * 给了 date：从该日起往后切（历史回溯）
      * 未给 date：窗口为 [今天-(N-1), 今天]；N<=5 时沿用「不带 DATE」的原行为
    """
    days = validate_area_days(days)
    if days <= FIRMS_AREA_MAX_DAYS and not date:
        return [(days, None)]
    today = today or datetime.date.today()
    if date:
        try:
            start = datetime.date.fromisoformat(str(date).strip())
        except ValueError:
            raise ValueError(f"DATE 必须为 YYYY-MM-DD，收到：{date!r}")
    else:
        start = today - datetime.timedelta(days=days - 1)
    plan, left, cur = [], days, start
    while left > 0:
        d = min(FIRMS_AREA_MAX_DAYS, left)
        plan.append((d, cur.isoformat()))
        cur += datetime.timedelta(days=d)
        left -= d
    return plan


def area_api_url(mapkey, bbox, source="VIIRS_SNPP_NRT", days=1, date=None):
    """拼**单次** Area API URL：/api/area/csv/{KEY}/{SOURCE}/{BBOX}/{DAYS}[/{DATE}]

    days 受 FIRMS_AREA_MAX_DAYS(5) 限制；更长的窗口请用 area_api_plan() 拆段。
    """
    bbox = bbox.strip()
    parts = bbox.split(",")
    if len(parts) != 4:
        raise ValueError(f"BBOX 必须为 west,south,east,north 4 段，收到：{bbox!r}")
    try:
        w, s, e, n = (float(x) for x in parts)
    except ValueError:
        raise ValueError(f"BBOX 必须为 4 个数字（west,south,east,north），收到：{bbox!r}")
    if w > e:
        w, e = e, w
    if s > n:
        s, n = n, s
    if not (-180 <= w <= 180 and -90 <= s <= 90 and -180 <= e <= 180 and -90 <= n <= 90):
        raise ValueError(f"BBOX 超出 [-180,-90,180,90] 范围：{bbox!r}")
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError(f"DAYS 必须为整数，收到：{days!r}")
    if not (1 <= days <= FIRMS_AREA_MAX_DAYS):
        raise ValueError(f"DAYS 只能取 1..{FIRMS_AREA_MAX_DAYS}（实测 6 被 API 拒），收到：{days}")
    url = f"{FIRMS_AREA_BASE}{mapkey}/{source}/{w:g},{s:g},{e:g},{n:g}/{days}"
    if date:
        url += "/" + date.strip()
    return url


def fetch_area_csv(mapkey, bbox, source="VIIRS_SNPP_NRT", days=1, date=None,
                   timeout=120, retries=3):
    """调用 FIRMS Area API 拉取包围盒内火点 CSV。

    错误处理要点：FIRMS 用 HTTP 400 + 纯文本返回原因（如 "Invalid MAP_KEY."），
    不是 JSON —— 必须原样透出，否则用户只看到「请求失败」而无从排查。
    """
    url = area_api_url(mapkey, bbox, source, days, date)
    last = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if len(data) > 10:
                return data.decode("utf-8", "replace"), url
            last = "响应过小"
        except urllib.error.HTTPError as e:
            body = e.read(400).decode("utf-8", "replace").strip()
            # 4xx 是请求本身的问题（key 无效/bbox 非法），重试无意义，直接抛出可读原因
            if 400 <= e.code < 500:
                raise RuntimeError(f"FIRMS Area API 拒绝请求（HTTP {e.code}）：{body or '无响应体'}")
            last = f"HTTP {e.code}: {body[:120]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        # 5xx / 网络错误才重试，且 short-backoff：实测失败耗时方差极大，
        # 退避给 1.5s×attempt 即可，固定长 sleep 会显著拖慢多段请求
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"FIRMS Area API 请求失败（{retries} 次重试）：{last}")


def merge_area_csv(texts):
    """合并多段 Area API 响应为一份 CSV：按「经纬+日期+时间+卫星」去重，列取并集。

    拆段请求必然产生重叠边界（例如 5/10-17 与 5/10-22 都含 10-22 附近数据），
    不去重会在地图上叠出重复点。
    """
    rows, cols, seen = [], [], set()
    for t in texts or []:
        for r in csv.DictReader(io.StringIO(t)):
            for k in r:
                if k and k not in cols:
                    cols.append(k)
            kk = (r.get("latitude"), r.get("longitude"), r.get("acq_date"),
                  r.get("acq_time"), r.get("satellite"))
            if kk in seen:
                continue
            seen.add(kk)
            rows.append(r)
    if not cols:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def fetch_area_csv_multi(mapkey, bbox, source="VIIRS_SNPP_NRT", days=1, date=None,
                         timeout=120, retries=3, verbose=False, plan=None):
    """按回溯窗口拉 Area API：>5 天自动拆成多段请求并合并去重。

    返回 (csv_text, [urls])，urls 供日志/界面展示（key 需自行脱敏）。

    plan 可复用外部已算好的拆段计划 —— 调用方常常已经算过一次（CLI 要先打印
    段数再发起请求），传进来即可，不必把同一套日期切分再算一遍。
    """
    plan = plan or area_api_plan(days, date)
    texts, urls = [], []
    for d, dt in plan:
        txt, url = fetch_area_csv(mapkey, bbox, source, d, dt,
                                  timeout=timeout, retries=retries)
        urls.append(url)
        texts.append(txt)
        if verbose and len(plan) > 1:
            print(f"    · 分段 {dt or '(默认今日)'} × {d} 天 → "
                  f"{max(0, len(txt.strip().splitlines()) - 1)} 行")
    return merge_area_csv(texts), urls


def parse_area_csv(text):
    """解析 Area API 返回的 CSV 文本为火点要素列表。

    Area API 的列名随 SOURCE 变化（VIIRS 用 bright_ti4/bright_ti5，
    MODIS 用 brightness/bright_t31），统一走 to_feature 的多列名兼容。
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    pts = []
    skipped = 0
    for row in rows:
        try:
            lat = float(row["latitude"])
            lng = float(row["longitude"])
        except (KeyError, ValueError, TypeError):
            skipped += 1
            continue
        pts.append((lng, lat, row))
    return pts, skipped


# ---------------------------------------------------------------------------
# HTML 生成（自包含，Leaflet + 高德卫星影像）
# ---------------------------------------------------------------------------
# 注意：必须是 raw string（r"""..."""）。
# 模板里大量 JS 转义（/\r?\n/、'\n'、/\//g 等）需要**原样**输出给浏览器；
# 若用普通三引号串，Python 会把 \r \n 解析成真实换行，把 JS 字符串截断成语法错误。
HTML_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>%%TITLE%%</title>
%%LEAFLET_CSS%%
<style>
  /* ===== 设计变量：颜色 / 圆角 / 阴影 / 动效统一收敛，避免散落的硬编码值 ===== */
  :root{
    --brand:#1565c0; --brand-500:#1e88e5; --brand-600:#0d47a1; --brand-50:#e8f0fe;
    --ink:#111827; --ink-2:#374151; --muted:#6b7280; --faint:#9ca3af;
    --line:#e5e7eb; --line-2:#d1d5db; --line-3:#b6bdc9;
    --surface-2:#f8fafc;
    --ok:#15803d; --err:#c62828;
    --r:12px; --r-sm:8px; --r-xs:6px;
    --sh-1:0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.08);
    --sh-2:0 4px 6px -1px rgba(16,24,40,.08), 0 2px 4px -2px rgba(16,24,40,.06);
    --sh-3:0 10px 24px -4px rgba(16,24,40,.14), 0 4px 8px -4px rgba(16,24,40,.08);
    --ease:cubic-bezier(.4,0,.2,1);
  }
  html,body{height:100%;margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
    "Microsoft YaHei",sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased}
  #map{position:absolute;inset:0;background:#dfe6ec}
  /* 数字统一等宽，切省/切 Tab 时不会左右抖动 */
  .tabular{font-variant-numeric:tabular-nums}

  /* ===== 顶部工具栏 ===== */
  /* Leaflet 控件默认 z-index 1000，工具栏 1100；两者纵向错开避免压盖 */
  /* 科研级/克制：近实色白底 + 极轻磨砂（仅够让地图色块不透过文字），去掉原 170% 饱和
     的重玻璃感；底边一条 1px 细线收口，不堆装饰阴影。 */
  #topbar{position:absolute;z-index:1100;top:0;left:0;right:0;display:flex;align-items:center;
    gap:14px;padding:9px 18px;background:rgba(255,255,255,.96);
    -webkit-backdrop-filter:blur(8px) saturate(138%);backdrop-filter:blur(8px) saturate(138%);
    border-bottom:1px solid var(--line);box-shadow:var(--sh-1);flex-wrap:wrap}
  #topbar .brand{display:flex;align-items:center;gap:10px;white-space:nowrap;flex:0 0 auto}
  #topbar .logo{width:30px;height:30px;border-radius:9px;display:flex;align-items:center;
    justify-content:center;background:linear-gradient(135deg,#fb8c00,#e53935);flex:0 0 auto}
  #topbar .logo svg{width:17px;height:17px;display:block}
  #topbar .brandTxt{display:flex;flex-direction:column;gap:1px;line-height:1.06;min-width:0}
  #topbar .brandTxt .kicker{font-size:10px;font-weight:700;letter-spacing:1px;
    color:var(--brand);text-transform:uppercase}
  #topbar .brandTxt .title{display:flex;align-items:center;gap:7px;font-size:15px;font-weight:700;
    letter-spacing:.2px;color:var(--ink)}
  #topbar .brandTxt .ver{font-style:normal;font-size:10.5px;font-weight:600;color:var(--muted);
    background:#f1f5f9;border:1px solid var(--line);padding:1px 6px;border-radius:999px;
    letter-spacing:0;white-space:nowrap}
  #provWrap{display:flex;align-items:center;gap:8px;font-size:13px}
  #provWrap label{color:var(--muted);white-space:nowrap;font-size:12.5px}
  /* v2.1.0：行政区划是方式1（内嵌分省 CSV）的概念，方式2/3 按视野取数，
     下拉只在方式1 显示。用 body 级 class 一条 CSS 收口，switchTab 只切 class。 */
  body:not(.tab-csv) #provWrap label,
  body:not(.tab-csv) #provSelect{display:none}
  #provSelect{appearance:none;-webkit-appearance:none;padding:6px 30px 6px 10px;font-size:13px;
    border:1px solid var(--line-2);border-radius:var(--r-sm);outline:none;cursor:pointer;
    color:var(--ink);background:#fff no-repeat right 9px center/12px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2.5 4.5L6 8l3.5-3.5' fill='none' stroke='%236b7280' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
    width:186px;max-width:40vw;font-family:inherit;
    transition:border-color .16s var(--ease),box-shadow .16s var(--ease)}
  #provSelect:hover{border-color:var(--line-3)}
  #provSelect:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(21,101,192,.14)}
  /* 顶部统计必须带「来源」前缀：三个方式的点数口径完全不同（方式1 是内嵌的 24h CSV
     快照，方式2 是实时拉取，方式3 是服务端栅格、根本没有点计数），只显示一个裸数字
     会被误读成「当前 Tab 的火点数」。chip 化后仍需可收缩 + 省略号，避免窄屏把工具栏
     撑换行；用 inline-block 而非 inline-flex，否则内层 span 不会触发省略号。 */
  #provStat{font-size:12px;color:var(--muted);white-space:nowrap;flex:0 1 auto;min-width:0;
    overflow:hidden;text-overflow:ellipsis;display:inline-block;max-width:180px;line-height:1.5;
    padding:4px 10px;border-radius:999px;background:#f1f5f9;border:1px solid var(--line)}
  #provStat .src{color:var(--faint)}
  #provStat .num{color:var(--brand);font-weight:600;font-variant-numeric:tabular-nums}

  /* ===== 三方式 Tab（分段控件） ===== */
  #tabs{display:flex;gap:3px;margin-left:auto;background:#eef2f7;padding:3px;border-radius:10px;
    box-shadow:inset 0 1px 2px rgba(16,24,40,.07)}
  #tabs .tab{padding:6px 13px;font-size:12.5px;font-weight:500;border:none;background:transparent;
    color:var(--ink-2);border-radius:var(--r-sm);cursor:pointer;white-space:nowrap;
    display:flex;align-items:center;gap:6px;font-family:inherit;
    transition:background .18s var(--ease),color .18s var(--ease),box-shadow .18s var(--ease)}
  #tabs .tab:hover{background:rgba(21,101,192,.08);color:var(--brand)}
  #tabs .tab.on{background:#fff;color:var(--brand);font-weight:600;box-shadow:var(--sh-1)}
  #tabs .tab .dot{width:6px;height:6px;border-radius:50%;background:var(--faint);flex:0 0 auto;
    transition:background .18s var(--ease),box-shadow .18s var(--ease)}
  #tabs .tab:hover .dot{background:var(--brand)}
  #tabs .tab.on .dot{background:var(--brand);box-shadow:0 0 0 3px rgba(21,101,192,.18)}

  /* ===== 左侧面板列 ===== */
  /* top 不写死：一行/两行工具栏都按实测高度 --topbar-h 自适应（1024px 宽时工具栏
     会换行成两行，写死 60px 会把左列压进工具栏里——实测 280x36px 重叠） */
  /* v0.8.0：max-height 也必须跟着 --topbar-h 走，并给左下角的图形比例尺留出
     走廊。原先写死 `- 74px`，只在工具栏一行高时才成立；面板内容短时 height 撑不到
     max-height，问题被掩盖；加了火点列表后内容首次顶到 max-height，
     立刻在 1280x900 / 1920x600 上压住比例尺（实测重叠 109x6px）。
     38px = 左列下边距 12 + 比例尺高度 13 + 其底部留白 ~13。 */
  #leftCol{position:absolute;z-index:1000;top:calc(var(--topbar-h, 60px) + 12px);
    left:12px;width:380px;max-width:calc(100vw - 24px);
    display:flex;flex-direction:column;gap:10px;
    max-height:calc(100vh - var(--topbar-h, 60px) - 38px);
    overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;padding-right:2px;
    scrollbar-color:rgba(15,23,42,.2) transparent}
  #leftCol::-webkit-scrollbar{width:6px}
  #leftCol::-webkit-scrollbar-track{background:transparent}
  #leftCol::-webkit-scrollbar-thumb{background:rgba(15,23,42,.18);border-radius:3px}
  #leftCol::-webkit-scrollbar-thumb:hover{background:rgba(15,23,42,.32)}
  /* 玻璃拟态：面板浮在卫星影像上，压暗又透光，比实色底更有层次 */
  .panel{background:rgba(255,255,255,.9);
    -webkit-backdrop-filter:blur(16px) saturate(165%);backdrop-filter:blur(16px) saturate(165%);
    border:1px solid rgba(255,255,255,.6);border-radius:var(--r);padding:12px 13px;
    box-shadow:var(--sh-3);font-size:13px;line-height:1.6;
    transition:box-shadow .2s var(--ease)}
  .panel:hover{box-shadow:0 14px 30px -6px rgba(16,24,40,.2), 0 6px 10px -6px rgba(16,24,40,.1)}
  /* 范围指标卡（容器始终可见；v2.0.0 起内容随 Tab 换口径——三个方式各显自己的统计） */
  .stats{margin-top:10px;padding-top:9px;border-top:1px solid var(--line);
    display:flex;flex-direction:column;gap:7px}
  .stats .row{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
  .stats .k{font-size:11.5px;color:var(--muted);white-space:nowrap;flex:0 0 auto}
  .stats .v{font-size:12.5px;font-weight:600;color:var(--ink);text-align:right;
    font-variant-numeric:tabular-nums;min-width:0;overflow:hidden;text-overflow:ellipsis}
  .stats .v.big{font-size:14px;color:var(--brand)}

  /* ===== 方式面板（Tab 内容） ===== */
  .tabPane{display:none}
  .tabPane.on{display:block;animation:panelIn .22s var(--ease)}
  @keyframes panelIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
  .hint{font-size:11px;color:var(--muted);line-height:1.55;margin-top:5px}
  .methodTitle{display:flex;align-items:center;gap:7px;font-size:13.5px;font-weight:700;
    color:var(--ink);margin-bottom:3px;line-height:1.4}
  .methodTitle::before{content:"";width:3px;height:13px;border-radius:2px;background:var(--brand);
    flex:0 0 auto}
  .methodDesc{font-size:11.5px;color:var(--muted);line-height:1.55;margin:0 0 9px 10px}
  .methodDesc code{background:#f1f3f7;padding:1px 4px;border-radius:3px;font-size:10.5px;
    color:var(--ink-2)}

  /* 方式1 数据来源元信息卡（火点产品 / 版本 / 行政区划 / 数据 / 火点总数） */
  .srcMeta{border:1px solid var(--line);border-radius:10px;background:#f8fafc;
    padding:8px 10px;margin:2px 0 11px}
  .srcMeta .smRow{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
    font-size:11.5px;line-height:1.75;min-width:0}
  .srcMeta .smK{color:var(--muted);flex:0 0 auto}
  .srcMeta .smV{color:var(--ink);font-weight:600;text-align:right;font-variant-numeric:tabular-nums;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  /* 方式面板内的控件。选择器作用域必须跟 DOM 走：v0.5.0 把面板 id 改成 pane-* 后，
     旧的 #wmsPanel 规则全部落空、控件退回浏览器默认样式（实测输入框只有 177px 而非撑满
     面板的 242px），故统一改为 .tabPane 作用域。 */
  .tabPane input,.tabPane select{width:100%;box-sizing:border-box;margin-top:8px;padding:7px 10px;
    font-size:12.5px;border:1px solid var(--line-2);border-radius:var(--r-sm);outline:none;
    background:#fff;color:var(--ink);font-family:inherit;
    transition:border-color .16s var(--ease),box-shadow .16s var(--ease)}
  .tabPane input::placeholder{color:#b3bac6}
  .tabPane input:hover,.tabPane select:hover{border-color:var(--line-3)}
  .tabPane input:focus,.tabPane select:focus{border-color:var(--brand);
    box-shadow:0 0 0 3px rgba(21,101,192,.14)}
  .tabPane select{appearance:none;-webkit-appearance:none;padding-right:30px;cursor:pointer;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2.5 4.5L6 8l3.5-3.5' fill='none' stroke='%236b7280' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 9px center;background-size:12px}
  .tabPane .btns{display:flex;gap:8px;margin-top:10px}
  .tabPane button{flex:1 1 0;min-width:0;padding:8px 6px;font-size:12.5px;font-weight:600;
    cursor:pointer;background:linear-gradient(180deg,var(--brand-500),var(--brand));color:#fff;
    border:none;border-radius:var(--r-sm);white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;font-family:inherit;box-shadow:0 1px 2px rgba(21,101,192,.35);
    transition:background .18s var(--ease),box-shadow .18s var(--ease),transform .12s var(--ease)}
  .tabPane button:hover{background:linear-gradient(180deg,#1976d2,var(--brand-600));
    box-shadow:0 3px 8px rgba(21,101,192,.34)}
  .tabPane button:active{transform:translateY(1px);box-shadow:0 1px 2px rgba(21,101,192,.3)}
  .tabPane button.ghost{background:#fff;color:var(--brand);border:1px solid #bfd4f5;
    box-shadow:none}
  .tabPane button.ghost:hover{background:var(--brand-50);border-color:var(--brand)}
  .tabPane button:disabled{background:#f1f5f9;color:#aab2bf;border:1px solid var(--line);
    box-shadow:none;cursor:not-allowed;transform:none}
  .tabPane .row2{display:flex;gap:8px;align-items:center;margin-top:8px}
  .tabPane .row2 select{flex:1 1 0;min-width:0;margin-top:0}
  .tabPane .row2 input{flex:0 0 66px;width:66px;margin-top:0}
  .tabPane .row2 input[type=date]{flex:1 1 0;width:auto;min-width:0}
  .tabPane .row2 label{font-size:11.5px;color:var(--muted);white-space:nowrap}
  .tabPane .dlrow{margin-top:10px;padding-top:9px;border-top:1px dashed var(--line)}
  .tabPane .dlrow .btns{margin-top:0}
  /* 状态区：带语义色（成功/失败/进行中），不再一律灰字 */
  #wmsStatus,#wmsStatusWms{font-size:11.5px;color:var(--muted);margin-top:9px;min-height:15px;
    line-height:1.55;white-space:pre-wrap;padding:7px 9px;border-radius:var(--r-xs);
    background:var(--surface-2);border-left:3px solid var(--line-2)}
  #wmsStatus:empty,#wmsStatusWms:empty{display:none}
  #wmsStatus.busy,#wmsStatusWms.busy{color:var(--brand);background:var(--brand-50);
    border-left-color:var(--brand)}
  #wmsStatus.ok,#wmsStatusWms.ok{color:var(--ok);background:#f0fdf4;border-left-color:#22c55e}
  #wmsStatus.err,#wmsStatusWms.err{color:var(--err);background:#fef2f2;border-left-color:#ef4444}
  /* 方式2：切换火点产品（卫星）时给出的一句简介，随选择实时更新 */
  .srcInfo{font-size:11.5px;line-height:1.55;color:var(--muted);background:rgba(15,23,42,.03);
    border-left:2px solid var(--brand);padding:7px 9px;border-radius:6px;margin-top:8px}
  .srcInfo b{color:var(--ink);font-weight:600;margin-right:2px}
  .srcInfo:empty{display:none}
  #areaReqUrl{font-size:10px;color:#8a94a6;margin-top:8px;line-height:1.45;word-break:break-all;
    border-top:1px dashed rgba(0,0,0,.12);padding-top:6px}
  #areaReqUrl:empty{display:none}
  #proxyHelp{display:none;margin-top:9px;padding-top:8px;border-top:1px dashed rgba(0,0,0,.14)}
  #proxyHelp .err{font-size:11.5px;color:var(--err);line-height:1.55;margin-bottom:7px}
  #proxyCmdBox{display:none;margin-top:7px;font-size:10px;color:var(--ink-2);line-height:1.5;
    background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-xs);
    padding:7px 8px;word-break:break-all}

  /* ===== 火点列表 ===== */
  /* 列表自带独立滚动容器：#leftCol 本身已经 overflow-y:auto，若列表不加限高，
     几百条火点会把「下载」「状态」等控件顶出一屏之外，用户得先滚过列表才能操作。 */
  .fpList{margin-top:11px;padding-top:10px;border-top:1px dashed var(--line)}
  .fpHd{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:700;color:var(--ink)}
  .fpHd .fpMeta{margin-left:auto;font-size:11px;font-weight:400;color:var(--muted);
    font-variant-numeric:tabular-nums;white-space:nowrap}
  /* 排序下拉要「窄成一枚 chip」，不能沿用 .tabPane select 的 width:100%。
     这里靠选择器特异性（.tabPane .fpHd select = 0,2,1 > .tabPane select = 0,1,1）
     覆盖，不需要 !important。 */
  .tabPane .fpHd select{width:auto;margin-top:0;padding:2px 20px 2px 6px;font-size:11px;
    font-weight:500;flex:0 0 auto;background-size:10px;background-position:right 5px center}
  .fpScroll{margin-top:7px;max-height:232px;overflow-y:auto;overflow-x:hidden;
    border:1px solid var(--line);border-radius:var(--r-sm);
    background:rgba(255,255,255,.72);scrollbar-width:thin}
  .fpScroll::-webkit-scrollbar{width:6px}
  .fpScroll::-webkit-scrollbar-thumb{background:rgba(15,23,42,.18);border-radius:3px}
  .fpItem{display:flex;align-items:flex-start;gap:7px;padding:6px 8px;cursor:pointer;
    border-bottom:1px solid rgba(15,23,42,.05);line-height:1.4}
  .fpItem:last-child{border-bottom:none}
  .fpItem:hover{background:var(--brand-50)}
  .fpItem.on{background:#fff8e1;box-shadow:inset 3px 0 0 var(--brand)}
  .fpItem .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto;margin-top:3px;
    border:1px solid rgba(0,0,0,.2)}
  .fpItem .main{min-width:0;flex:1 1 auto;display:flex;flex-direction:column;gap:1px}
  .fpItem .l1{display:flex;align-items:center;gap:6px}
  .fpItem .frp{font-size:11.5px;font-weight:700;color:var(--ink);
    font-variant-numeric:tabular-nums;white-space:nowrap}
  .fpItem .tag{font-style:normal;font-size:10px;color:var(--muted);background:#eef2f7;
    border-radius:3px;padding:0 4px;white-space:nowrap}
  .fpItem .l2{font-size:10.5px;color:var(--faint);white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;font-variant-numeric:tabular-nums}
  .fpEmpty{padding:16px 8px;text-align:center;font-size:11.5px;color:var(--faint)}
  .fpMore{margin-top:7px}
  .fpNote{margin-top:6px;font-size:10.5px;color:var(--muted);line-height:1.5}

  /* ===== 自由绘制 ===== */
  /* 绘制中要盖掉 Leaflet 的 grab 光标，用户才知道「现在是在画」而不是在拖地图 */
  body.map-draw .leaflet-grab,body.map-draw .leaflet-container{cursor:crosshair !important}
  .drawHint{font-size:11px;color:var(--brand);background:var(--brand-50);line-height:1.5;
    border-left:3px solid var(--brand);padding:6px 8px;border-radius:var(--r-xs);margin-top:8px}
  .drawHint:empty{display:none}
  #areaShapeHint{font-size:11px;color:var(--muted);line-height:1.5;margin-top:7px;
    padding:5px 8px;border-radius:var(--r-xs);background:var(--surface-2)}
  #areaShapeHint.has{color:var(--brand);background:var(--brand-50)}

  /* ===== 图例：右下角 ===== */
  #legend{position:absolute;z-index:1000;bottom:34px;right:12px;width:184px;padding:11px 12px}
  #legend .lgTitle{font-size:12px;font-weight:700;color:var(--ink);margin-bottom:6px}
  #legend .bar{height:7px;border-radius:4px;
    background:linear-gradient(90deg,#ffd54f,#ff9800,#e53935,#b71c1c);
    box-shadow:inset 0 0 0 1px rgba(0,0,0,.08)}
  #legend .barLbl{display:flex;justify-content:space-between;font-size:10px;color:var(--faint);
    margin-top:3px}
  #legend .row{display:flex;align-items:center;gap:7px;margin-top:5px;font-size:11.5px;
    color:var(--ink-2)}
  #legend .dot{width:11px;height:11px;border-radius:50%;display:inline-block;
    border:1.5px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.22);flex:0 0 auto}
  #legend .sep{height:1px;background:var(--line);margin:9px 0 7px}
  #legend .note{margin-top:7px;font-size:10.5px;color:var(--muted);line-height:1.5}

  /* ===== Leaflet 控件美化（默认是方角浅灰，与整体卡片风格不统一） ===== */
  .leaflet-bar{border:none;box-shadow:var(--sh-2);border-radius:var(--r-sm);overflow:hidden}
  .leaflet-bar a,.leaflet-bar a:hover{width:30px;height:30px;line-height:30px;background:#fff;
    color:var(--ink-2);border-bottom:1px solid var(--line);font-size:16px;font-weight:500}
  .leaflet-bar a:hover{background:var(--brand-50);color:var(--brand)}
  .leaflet-bar a:last-child{border-bottom:none}
  .leaflet-control-layers-toggle{background-image:none;width:auto;height:auto;padding:5px 11px;
    font-size:12px;font-weight:600;color:var(--ink-2);background:#fff;border-radius:var(--r-sm);
    box-shadow:var(--sh-2)}
  .leaflet-control-layers-toggle::after{content:"图层"}
  .leaflet-control-layers-expanded{padding:10px 13px;font-size:12.5px;line-height:1.85;
    border-radius:var(--r-sm);border:1px solid var(--line);
    background:rgba(255,255,255,.96);box-shadow:var(--sh-3);color:var(--ink)}
  .leaflet-control-scale-line{border:1px solid rgba(15,23,42,.34);border-top:none;
    background:rgba(255,255,255,.86);font-size:10.5px;color:var(--ink-2);padding:0 5px;
    border-radius:0 0 3px 3px}
  .leaflet-control-attribution{background:rgba(255,255,255,.82);font-size:10px;color:var(--muted);
    padding:1px 7px;border-radius:4px 0 0 0}
  .leaflet-popup-content-wrapper{border-radius:var(--r);box-shadow:var(--sh-3);padding:2px}
  .leaflet-popup-content{margin:10px 12px;font-size:12.5px;line-height:1.65}
  .leaflet-popup-content b{color:#111}
  .leaflet-tooltip.fire-tip{background:rgba(20,20,20,.85);color:#fff;border:none;
    border-radius:5px;font-size:11px;padding:3px 7px;box-shadow:0 2px 6px rgba(0,0,0,.32)}
  .leaflet-tooltip.fire-tip::before{display:none}
  /* Leaflet 控件整体下移，让开顶部工具栏（否则缩放/图层控件会被工具栏压住）。
     偏移量由 JS 按工具栏实测高度写入 --topbar-h（窄屏换行时高度会变），不写死。 */
  .leaflet-top.leaflet-right,.leaflet-top.leaflet-left{margin-top:var(--topbar-h,56px)}

  @media (max-width:860px){
    #topbar{gap:9px;padding:8px 12px}
    #topbar .brandTxt .kicker{display:none}
    #tabs{margin-left:0;width:100%;order:3}
    #tabs .tab{flex:1 1 0;justify-content:center;padding:6px 4px;font-size:12px}
  }
  @media (max-width:640px){
    #provSelect{width:120px}
    #provStat{max-width:150px}
    #topbar .brandTxt .ver{display:none}
    #legend{width:152px;font-size:12px;padding:9px 10px}
    #legend .note{display:none}
  }
  @media (max-width:420px){
    #legend{display:none}
  }
  @media (prefers-reduced-motion:reduce){
    *{animation:none !important;transition:none !important}
  }
</style>
</head>
<body class="tab-csv">
<div id="map"></div>

<!-- ===== 顶部工具栏 ===== -->
<div id="topbar">
  <div class="brand">
    <span class="logo" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 2.4c3.1 3.5 5.6 6 5.6 9.6a5.6 5.6 0 1 1-11.2 0c0-3.6 2.5-6.1 5.6-9.6z" fill="#fff" opacity=".96"/><path d="M12 13c1.5 1.8 2.5 2.9 2.5 4.2a2.5 2.5 0 1 1-5 0c0-1.3 1-2.4 2.5-4.2z" fill="#ff8a3d"/></svg></span>
    <span class="brandTxt">
      <span class="kicker">NASA · FIRMS</span>
      <span class="title">全球近实时火点 <i class="ver">v%%VERSION_TXT%%</i></span>
    </span>
  </div>
  <div id="provWrap">
    <label for="provSelect">行政区划</label>
    <select id="provSelect" title="筛选中国省级行政区"></select>
    <span id="provStat"></span>
  </div>
  <div id="tabs">
    <button class="tab on" data-tab="csv"><span class="dot"></span>方式1 CSV</button>
    <button class="tab" data-tab="area"><span class="dot"></span>方式2 Area API</button>
    <button class="tab" data-tab="wms"><span class="dot"></span>方式3 WMS</button>
  </div>
</div>

<!-- ===== 左侧面板列 ===== -->
<div id="leftCol">

  <!-- 方式1：免 Key CSV -->
  <div class="panel tabPane on" id="pane-csv">
    <div class="methodTitle">方式1 · 免 Key 直下 CSV</div>
    <div class="methodDesc">
      数据来自 FIRMS 公开目录的 24h 全球文件，已离线裁切到中国各省并内嵌在本页。
      切换上方「行政区划」即时查看各省火点，无需联网、无需 Key。
    </div>
    <div class="srcMeta" id="csvSrcMeta">
      <div class="smRow"><span class="smK">火点产品</span><span class="smV">FIRMS 火点 · %%PRODUCT_TXT%%</span></div>
      <div class="smRow"><span class="smK">版本</span><span class="smV">v%%VERSION_TXT%%</span></div>
      <div class="smRow"><span class="smK">行政区划</span><span class="smV" id="csvMetaScope">全国（全部省份）</span></div>
      <div class="smRow"><span class="smK">数据</span><span class="smV">24h CSV</span></div>
      <div class="smRow"><span class="smK">火点总数</span><span class="smV" id="csvMetaCount">—</span></div>
    </div>
    <div class="dlrow">
      <div class="btns">
        <button id="csvDlCsv" class="ghost" title="下载当前范围的火点 CSV">下载 CSV</button>
        <button id="csvDlGeo" class="ghost" title="下载当前范围的火点 GeoJSON">下载 GeoJSON</button>
      </div>
      <div class="hint">按上方「行政区划」的当前范围下载（WGS-84 坐标，CSV 带 BOM 可直接用 Excel 打开）。</div>
    </div>
    <div id="csvDlStat" class="hint" style="color:#2e7d32"></div>
    <div class="fpList">
      <div class="fpHd">
        <span>火点列表</span>
        <select id="csvListSort" title="列表排序方式">
          <option value="frp">按 FRP 降序</option>
          <option value="time">按时间降序</option>
        </select>
        <span class="fpMeta" id="csvListMeta">—</span>
      </div>
      <div class="fpScroll" id="csvListBody"></div>
      <div class="fpMore">
        <button id="csvListMore" class="ghost" disabled title="每次多显示 60 条">显示更多</button>
      </div>
      <div class="fpNote">默认按 FRP 从强到弱排序；点击任一条可在地图上定位并弹出详情。</div>
    </div>
  </div>

  <!-- 方式2：Area API -->
  <div class="panel tabPane" id="pane-area">
    <div class="methodTitle">方式2 · Area API 按视野拉取</div>
    <div class="methodDesc">
      需免费 MAP_KEY。默认按<b>当前地图视野</b>实时向 FIRMS 请求火点；也可<b>自己在地图上画一个范围</b>
      （矩形或多边形），拉取时按所画区域过滤（新拉的点以青色描边叠加）。
      <br/>接口：<code>/api/area/csv/{KEY}/{SOURCE}/{BBOX}/{DAYS}[/{DATE}]</code>
    </div>
    <!-- 自由绘制：矩形（拖框）与多边形（逐点勾），绘制结果同时用于请求 bbox 与结果过滤。
         不引第三方 Draw 插件：本页面要保持「单文件、零外部依赖」，而真正需要的
         只是「取得形状包络盒 + 多边形内点判定」两件事。 -->
    <div class="btns">
      <button id="areaDrawRect" class="ghost" title="按住鼠标在地图上拖出一个矩形作为拉取范围">绘制矩形</button>
      <button id="areaDrawPoly" class="ghost" title="在地图上依次点击勾出多边形，双击或点回起点闭合">绘制多边形</button>
      <button id="areaClearShape" class="ghost" title="清除已绘制的范围，回到「地图视野」模式">清除区域</button>
    </div>
    <div id="areaShapeHint">当前范围：地图视野 ∩ 中国范围</div>
    <div class="drawHint" id="areaDrawHint"></div>
    <input id="firmsKey" type="text" placeholder="粘贴 FIRMS MAP_KEY" autocomplete="off" spellcheck="false"/>
    <div class="row2">
      <select id="areaSource" title="Area API 数据源"></select>
      <input id="areaDays" type="number" min="1" max="%%AREA_MAX_WINDOW%%" value="1"
             title="回溯天数 1-%%AREA_MAX_WINDOW%%（>%%AREA_MAX_SINGLE%% 天会自动拆成多段请求）"/>
    </div>
    <div id="areaSourceInfo" class="srcInfo"></div>
    <div class="row2">
      <label for="areaDate">起始日</label>
      <input id="areaDate" type="date" title="历史回溯起始日（可选；留空 = 最近 N 天）"/>
      <button id="areaDateClear" class="ghost" title="清空起始日，回到「最近 N 天」">清空</button>
    </div>
    <div class="hint" id="areaPlanHint"></div>
    <div class="btns">
      <button id="areaApply">拉取视野火点</button>
    </div>
    <div class="dlrow">
      <div class="btns">
        <button id="areaDlCsv" class="ghost" disabled title="下载已拉取的火点 CSV">下载 CSV</button>
        <button id="areaDlGeo" class="ghost" disabled title="下载已拉取的火点 GeoJSON">下载 GeoJSON</button>
      </div>
      <div class="hint">下载本次拉取到的火点（先点「拉取视野火点」；切换省份会清空已拉数据）。</div>
    </div>
    <div class="fpList">
      <div class="fpHd">
        <span>火点列表</span>
        <select id="areaListSort" title="列表排序方式">
          <option value="frp">按 FRP 降序</option>
          <option value="time">按时间降序</option>
        </select>
        <span class="fpMeta" id="areaListMeta">—</span>
      </div>
      <div class="fpScroll" id="areaListBody"></div>
      <div class="fpMore">
        <button id="areaListMore" class="ghost" disabled title="每次多显示 60 条">显示更多</button>
      </div>
      <div class="fpNote">列出本次拉取到的火点；点击任一条可在地图上定位并弹出详情。</div>
    </div>
    <div id="wmsStatus"></div>
    <div id="areaReqUrl"></div>
    <div id="proxyHelp">
      <div class="err">未找到本地代理服务。FIRMS 不允许浏览器跨域直连 Area API，必须经本地代理转发。</div>
      <div class="btns" style="margin-top:0">
        <button id="proxyCopy" title="复制下方命令，粘贴到终端执行">复制启动命令</button>
        <button id="proxyRetry" class="ghost" title="我已启动代理，重新检测">重新检测</button>
      </div>
      <div id="proxyCmdBox"></div>
    </div>
  </div>

  <!-- 方式3：WMS -->
  <div class="panel tabPane" id="pane-wms">
    <div class="methodTitle">方式3 · WMS 官方栅格叠加</div>
    <div class="methodDesc">
      叠加 FIRMS 官方火点栅格服务（瓦片方式，样式为官方配色）。
      <br/>接口：<code>/mapserver/wms/fires/{KEY}/</code>
    </div>
    <input id="firmsKeyWms" type="text" placeholder="粘贴 FIRMS MAP_KEY" autocomplete="off" spellcheck="false"/>
    <div class="btns">
      <button id="wmsApply">叠加 WMS 栅格</button>
    </div>
    <div id="wmsStatusWms"></div>
    <div class="hint" style="margin-top:6px">
      WMS 走瓦片（&lt;img&gt;），浏览器可直连，<b>无需本地代理</b>；QGIS 里同样可直接加此服务。
    </div>
  </div>
</div>

<div id="legend" class="panel">
  <div class="lgTitle">火点强度 · FRP</div>
  <div class="bar"></div>
  <div class="barLbl"><span>弱</span><span>强</span></div>
  <div class="row"><span class="dot" style="background:#b71c1c"></span>&gt; 100 MW</div>
  <div class="row"><span class="dot" style="background:#e53935"></span>50 – 100 MW</div>
  <div class="row"><span class="dot" style="background:#ff9800"></span>10 – 50 MW</div>
  <div class="row"><span class="dot" style="background:#ffd54f"></span>≤ 10 MW</div>
  <div class="sep"></div>
  <!-- Area API 的点只有方式2 才有；表里如果常驻这一行，切到方式1/3 会让人
       以为图上的圆点也是实时拉的，故随 Tab 显隐（switchTab 里控制） -->
  <!-- v0.10.1：默认 display:none。初始化走的是 __activeTab 直赋而非 switchTab()，
       若靠 HTML 默认可见，初始「方式1」下图例会串台显示 Area 行（实测截图+computed
       display:flex 证实）。默认藏、由 switchTab 按需打开，与 lgShapeRow 同款默认安全。 -->
  <div class="row" id="lgAreaRow" style="display:none"><span class="dot" style="background:#4fc3f7;border-color:#00e5ff"></span>Area API 实时点</div>
  <div class="row" id="lgShapeRow" style="display:none"><span class="dot" style="background:transparent;border-color:#ff8f00;border-style:dashed"></span>已绘制的关注区域</div>
  <div class="note">圆点越大、越偏红，火强度越高</div>
</div>

%%LEAFLET_JS%%
<script>
const PROVINCES = %%PROVINCES%%;   // [{name, adcode, geometry(GCJ-02), bbox(WGS-84)}]
const FIRES = %%FIRES%%;           // {省名: [[lng,lat,frp,bright,confIdx,dateIdx,acqTime,dnIdx], ...]}
const DATES = %%DATES%%;           // 日期字典
const CONF = %%CONF%%;             // 置信度字典 ['low','nominal','high']
const META = %%META%%;
const PRODUCT = %%PRODUCT%%;
const WMS_LAYER = %%WMS_LAYER%%;
const FIRMS_WMS_BASE = 'https://firms.modaps.eosdis.nasa.gov/mapserver/wms/fires/';
const AREA_BASE = %%AREA_BASE%%;
const AREA_PROXY_CANDIDATES = %%AREA_PROXY_CANDIDATES%%;
const SERVE_PY = %%SERVE_PY%%;
const MAP_DIR = %%MAP_DIR%%;
const PYTHON_EXE = %%PYTHON_EXE%%;
const AREA_PROXY_PORT = %%PROXY_PORT%%;
const INITIAL_KEY = %%WMS_KEY%%;
const AREA_SOURCES = %%AREA_SOURCES%%;
const DEFAULT_PROVINCE = %%DEFAULT_PROVINCE%%;
const AREA_MAX_SINGLE = %%AREA_MAX_SINGLE%%;   // Area API 单次请求 DAYS 上限（实测 6 被拒）
const AREA_MAX_WINDOW = %%AREA_MAX_WINDOW%%;   // 本页支持的回溯窗口上限（>5 自动拆段）
const SKILL_VERSION = %%SKILL_VERSION%%;

// WGS-84 -> GCJ-02（用于对齐高德底图）
function wgs84togcj02(lng, lat){
  if(lng<73.66||lng>135.05||lat<3.86||lat>53.55) return [lng,lat];
  const pi=Math.PI, a=6378245, ee=0.00669342162296594323;
  const tlat=x=>{let r=-100+2*x+3*lat+0.2*lat*lat+0.1*x*lat+0.2*Math.sqrt(Math.abs(x));
    r+=(20*Math.sin(6*x*pi)+20*Math.sin(2*x*pi))*2/3;
    r+=(20*Math.sin(lat*pi)+40*Math.sin(lat/3*pi))*2/3;
    r+=(160*Math.sin(lat/12*pi)+320*Math.sin(lat*pi/30))*2/3;return r;};
  const tlng=x=>{let r=300+x+2*lat+0.1*x*x+0.1*x*lat+0.1*Math.sqrt(Math.abs(x));
    r+=(20*Math.sin(6*x*pi)+20*Math.sin(2*x*pi))*2/3;
    r+=(20*Math.sin(x*pi)+40*Math.sin(x/3*pi))*2/3;
    r+=(150*Math.sin(x/12*pi)+300*Math.sin(x/30*pi))*2/3;return r;};
  let dlat=tlat(lng-105), dlng=tlng(lng-105);
  const radlat=lat/180*pi, m=Math.sin(radlat); let mm=1-ee*m*m, sm=Math.sqrt(mm);
  dlat=(dlat*180)/((a*(1-ee))/(mm*sm)*pi);
  dlng=(dlng*180)/((a/sm)*Math.cos(radlat)*pi);
  return [lng+dlng, lat+dlat];
}

// GCJ-02 -> WGS-84（近似逆变换），用于把地图当前视野的 GCJ-02 bbox
// 换算回 Area API 需要的 WGS-84 bbox（Area API 只认 WGS-84）
function gcj02towgs84(lng, lat){
  if(lng<73.66||lng>135.05||lat<3.86||lat>53.55) return [lng,lat];
  const pi=Math.PI, a=6378245, ee=0.00669342162296594323;
  const tlat=x=>{let r=-100+2*x+3*lat+0.2*lat*lat+0.1*x*lat+0.2*Math.sqrt(Math.abs(x));
    r+=(20*Math.sin(6*x*pi)+20*Math.sin(2*x*pi))*2/3;
    r+=(20*Math.sin(lat*pi)+40*Math.sin(lat/3*pi))*2/3;
    r+=(160*Math.sin(lat/12*pi)+320*Math.sin(lat*pi/30))*2/3;return r;};
  const tlng=x=>{let r=300+x+2*lat+0.1*x*x+0.1*x*lat+0.1*Math.sqrt(Math.abs(x));
    r+=(20*Math.sin(6*x*pi)+20*Math.sin(2*x*pi))*2/3;
    r+=(20*Math.sin(x*pi)+40*Math.sin(x/3*pi))*2/3;
    r+=(150*Math.sin(x/12*pi)+300*Math.sin(x/30*pi))*2/3;return r;};
  let dlat=tlat(lng-105), dlng=tlng(lng-105);
  const radlat=lat/180*pi, m=Math.sin(radlat); let mm=1-ee*m*m, sm=Math.sqrt(mm);
  dlat=(dlat*180)/((a*(1-ee))/(mm*sm)*pi);
  dlng=(dlng*180)/((a/sm)*Math.cos(radlat)*pi);
  return [lng-dlng, lat-dlat];
}

function frpColor(v){const v2=parseFloat(v)||0; return v2>100?'#b71c1c':v2>50?'#e53935':v2>10?'#ff9800':'#ffd54f';}
function frpRadius(v){const v2=parseFloat(v)||0; return Math.max(5,Math.min(20,4+Math.sqrt(v2)*1.3));}
// 小数位统一，切省/切 Tab 时列表数字不会左右抖
function frpText(v){ return (parseFloat(v)||0).toFixed(1); }

// ===== 图层显隐：统一入口 =====
//
// v0.8.0 修复：原先 switchTab() 只处理了 areaLayer / wmsLayer，唯独没管 csvLayer，
// 于是切到方式2/3 后方式1 的火点仍留在图上，两堆不同口径的点混在一起无法分辨
// （无头浏览器取证：tab=area / tab=wms 时 map.eachLayer 仍能枚举到 csvLayer）。
//
// 顺带把「切换 = 销毁重建」改成「切换 = 增删图层」：全国版有 7739 个 circleMarker，
// 若每次切回方式1 都重建一遍，切换会有肉眼可见的卡顿（实测重建是秒级）。
function activeTab(){ return window.__activeTab || 'csv'; }
function showLayer(l){ if(l && !map.hasLayer(l)) l.addTo(map); }
function hideLayer(l){ if(l && map.hasLayer(l)) map.removeLayer(l); }
// 自检钩子：external=e2e 脚本据此判断某个方式的图层是否真的在图上
window.__layerState = function(){
  return {csv: !!(csvLayer && map.hasLayer(csvLayer)),
          area: !!(areaLayer && map.hasLayer(areaLayer)),
          wms: !!(firmsWmsLayer && map.hasLayer(firmsWmsLayer)),
          shape: !!(areaShapeLayer && map.hasLayer(areaShapeLayer))};  // v0.10.1 绘制形状
};

const map = L.map('map', {zoomControl:false});
L.control.zoom({position:'topright'}).addTo(map);
map.setView([28.0, 101.0], 4);  // 先建立有效视图，避免矢量渲染器 _bounds 未初始化
L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scale=1&style=6',
  {subdomains:'1234', maxZoom:18, attribution:'© 高德地图'}).addTo(map);
L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scale=1&style=8',
  {subdomains:'1234', maxZoom:18, opacity:0.75}).addTo(map);

const fireRenderer = L.canvas({padding:0.5});
L.control.scale({position:'bottomleft', imperial:false, metric:true, maxWidth:160}).addTo(map);

// ===== 数据图层 =====
let boundaryLayer = null;   // 省界（当前选中省）
let countryLayer = null;    // 全国省界（上下文参考）
let csvLayer = null;        // 方式1 火点
let csvItems = [];          // 方式1：[{row, marker, prov}]，与列表一一对应
let areaLayer = null;       // 方式2 Area API 火点
let areaItems = [];         // 方式2：[{row, marker}]
let firmsWmsLayer = null, wmsCtl = null;
let areaProxyBase = null;
let currentProvince = '';   // '' = 全国
// 方式2 已拉取的数据缓存：切 Tab / 切省时不该让已拉到的点凭空消失或留在错误的省上
let areaRowsCache = null;   // 去重后的 CSV 行对象数组
let areaMeta = null;        // {source, days, date, bbox, urls}

// ===== 通用：CSV / GeoJSON 导出与下载 =====
function csvEscape(v){
  const s = (v === null || v === undefined) ? '' : String(v);
  return /[",\r\n]/.test(s) ? ('"' + s.replace(/"/g, '""') + '"') : s;
}
function rowsToCsv(rows){
  const cols = ['longitude','latitude','brightness','frp','confidence','acq_date','acq_time',
                'daynight','satellite','province'];
  const has = cols.filter(c => rows.some(r => r[c] !== undefined && r[c] !== null && r[c] !== ''));
  return [has.join(',')].concat(rows.map(r => has.map(c => csvEscape(r[c])).join(','))).join('\r\n');
}
function rowsToGeoJSON(rows){
  return JSON.stringify({type:'FeatureCollection', features: rows.map(r=>({
    type:'Feature',
    geometry:{type:'Point', coordinates:[Number(r.longitude), Number(r.latitude)]},
    properties:{longitude:Number(r.longitude), latitude:Number(r.latitude),
      brightness:r.brightness, frp:r.frp, confidence:r.confidence,
      acq_date:r.acq_date, acq_time:r.acq_time, daynight:r.daynight,
      satellite:r.satellite, province:r.province}
  }))}, null, 1);
}
function safeName(s){
  return String(s || '').replace(/[^\w\u4e00-\u9fa5-]+/g, '_').replace(/^_+|_+$/g, '') || 'firms';
}
function stampNow(){
  const d = new Date(), p = n => String(n).padStart(2, '0');
  return '' + d.getFullYear() + p(d.getMonth()+1) + p(d.getDate())
       + '_' + p(d.getHours()) + p(d.getMinutes());
}
// 加 BOM：否则 Excel 打开 UTF-8 CSV 会显示中文乱码
function saveFile(name, text, mime){
  const blob = new Blob(['\ufeff' + text], {type: mime});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name; a.style.display = 'none';
  document.body.appendChild(a); a.click();
  setTimeout(()=>{ URL.revokeObjectURL(url); a.remove(); }, 2000);
}
function downloadRows(rows, base, kind){
  const nm = safeName('firms_' + PRODUCT.replace(/\s*\(.*\)/, '') + '_' + base) + '_' + stampNow();
  if(kind === 'geo') saveFile(nm + '.geojson', rowsToGeoJSON(rows), 'application/geo+json;charset=utf-8');
  else saveFile(nm + '.csv', rowsToCsv(rows), 'text/csv;charset=utf-8');
  return nm;
}

// ===== 方式1：按当前省/全国导出 =====
function csvRowsForScope(){
  const out = [];
  const push = (r, pname) => out.push({
    longitude:r[0], latitude:r[1], frp:r[2], brightness:r[3],
    confidence:(CONF[r[4]] || ''), acq_date:(DATES[r[5]] || ''),
    acq_time:String(r[6]).padStart(4, '0'), daynight:(r[7] === 0 ? 'D' : 'N'),
    satellite:'', province:pname});
  if(currentProvince){ (FIRES[currentProvince] || []).forEach(r => push(r, currentProvince)); }
  else { for(const k in FIRES){ (FIRES[k] || []).forEach(r => push(r, k)); } }
  return out;
}

// 省界（GCJ-02，直接对齐底图）。关键：interactive:false，否则其交互 SVG 层
// 会盖在火点 Canvas 之上吞掉点击/悬停事件，导致「点火点没反应」。
function drawBoundaries(provName){
  if(boundaryLayer){ map.removeLayer(boundaryLayer); boundaryLayer = null; }
  // v2.1.0：全国省界上下文层只建一次。原先每次切省都 remove+重建全部 34 省
  // GeoJSON（几何量大、每次重新投影），而它根本不随省变化——只有高亮的
  // boundaryLayer 变。与 v0.8.0「切 Tab 不重建」同一哲学；__countryBuilds
  // 计数器供守卫断言「反复切省不再重建」。
  if(!countryLayer){
    window.__countryBuilds = (window.__countryBuilds || 0) + 1;
    const all = {type:'FeatureCollection', features: PROVINCES.map(p=>({
      type:'Feature', geometry:p.geometry, properties:{name:p.name}}))};
    countryLayer = L.geoJSON(all, {interactive:false,
      style:{color:'#94a3b8', weight:0.8, fill:false}});
    countryLayer.addTo(map);
  }
  if(provName){
    const p = PROVINCES.find(x=>x.name===provName);
    if(p){
      boundaryLayer = L.geoJSON({type:'Feature', geometry:p.geometry, properties:{name:p.name}},
        {interactive:false, style:{color:'#ff5252', weight:1.8, fill:true,
          fillColor:'#ff5252', fillOpacity:0.07}});
      boundaryLayer.addTo(map);
    }
  }
}

// 方式1 火点：内嵌数据，按省筛选。
// 图层**始终构建**（省/全国切换时才重建），但是否挂到地图上取决于当前 Tab ——
// 三个方式的点必须互斥显示，否则图上两堆不同口径的火点无法分辨。
function drawCsvFires(provName){
  if(csvLayer){ hideLayer(csvLayer); csvLayer = null; }
  const rows = provName ? (FIRES[provName]||[]) : ALL_ROWS;
  csvLayer = L.layerGroup();
  csvItems = [];
  for(let i=0;i<rows.length;i++){
    const r = rows[i];
    const g = wgs84togcj02(r[0], r[1]);
    const m = L.circleMarker([g[1],g[0]], {renderer:fireRenderer, radius:frpRadius(r[2]),
      color:'#ffffff', weight:1.4, fillColor:frpColor(r[2]), fillOpacity:0.92})
     .bindPopup('<b>火点</b><br>经度 '+r[0]+'<br>纬度 '+r[1]+
       '<br>亮度温度 '+r[3]+' K<br>FRP '+r[2]+' MW<br>置信度 '+(CONF[r[4]]||'—')+
       '<br>采集日期 '+(DATES[r[5]]||'—')+'<br>采集时间 '+fmtAqTime(r[6])+' UTC（分钟级）'+
       '<br>昼/夜 '+(r[7]===0?'昼':'夜'))
     .bindTooltip('FRP '+r[2]+' MW · '+(r[7]===0?'昼':'夜'), {direction:'top', offset:[0,-4], className:'fire-tip'});
    m.addTo(csvLayer);
    // 列表要能反查 marker：全国视图下行来自不同省，省名必须随行带走，
    // 否则列表里「哪一省」这一列拿不到（ALL_ROWS 只存了紧凑行本身）
    csvItems.push({row:r, marker:m, prov:(provName || PROV_OF_ROW.get(r) || '')});
  }
  // 先装满再一次性 addTo(map)：逐个 addTo 会触发逐次重绘，7739 个点会明显卡
  if(activeTab()==='csv') showLayer(csvLayer);
  window.__fireRendered = rows.length;
  window.__csvItems = csvItems.length;
  // 可观测的重建计数：v0.8.0 把「切 Tab = 销毁重建」改成「切 Tab = 摘/挂图层」
  // 之后，切回方式1 不该再重建这 7739 个 marker。自检据此断言计数不增长，
  // 否则性能回归会悄无声息地回来（页面照样能看，只是切换明显卡顿）。
  window.__csvBuilds = (window.__csvBuilds || 0) + 1;
  // 数据集版本自增：列表排序缓存据此判断「数据是否真的变了」，避免每次展开都重排。
  window.__csvVersion = (window.__csvVersion || 0) + 1;
  renderCsvList();
  return rows.length;
}

// 全国火点 = 所有省合并。
// PROV_OF_ROW：行 → 所属省 的反查表。ALL_ROWS 是跨省合并出来的，行本身不带
// 省信息；列表要显示「哪一省」，就得在构建期一次性建表，渲染时 O(1) 取用
// （不能每渲染一次列表就重扫一遍 35 个省的数组）。
const PROV_OF_ROW = new Map();
const ALL_ROWS = (function(){
  const out = [];
  for(const k in FIRES){
    for(const r of FIRES[k]){ out.push(r); if(!PROV_OF_ROW.has(r)) PROV_OF_ROW.set(r, k); }
  }
  return out;
})();

// ===== 火点列表（方式1 / 方式2 共用一套渲染器）=====
//
// 为什么需要列表：几千个点叠在图上时，靠肉眼找不到「FRP 最强的那几个」，也没法
// 按时间排序；列表给出可排序、可逐个定位的入口，地图则负责看空间分布。
//
// 渲染策略：一次只渲染窗口内的前 60 条，点「显示更多」再追加 60。
// 全国版 7739 条若一次性建成 DOM，首屏与每次切省都会明显变慢。
const LIST_STEP = 60;
const listStates = {
  csv:  {sort:'frp', limit:LIST_STEP, cur:-1, items:[]},
  area: {sort:'frp', limit:LIST_STEP, cur:-1, items:[]},
};
function csvTimeKey(r){
  const d = String(DATES[r[5]] || '').replace(/-/g, '');
  return (parseInt(d, 10) || 0) * 10000 + (parseInt(r[6], 10) || 0);
}
function areaTimeKey(r){
  const d = String(r.acq_date || '').replace(/-/g, '');
  return (parseInt(d, 10) || 0) * 10000 + (parseInt(r.acq_time, 10) || 0);
}
// FIRMS 源 acq_time 为 UTC「HHMM」（分钟级），无秒。统一补成「HH:MM:00」便于阅读；
// 秒位恒为 00，并非真实精度 —— 弹窗里额外标注「（分钟级）」说明这一点。
function fmtAqTime(hhmm){
  const t = String(hhmm || '').padStart(4, '0');
  return t.slice(0,2) + ':' + t.slice(2,4) + ':00';
}
function fpRowHtml(kind, it, i){
  let frp, lat, lng, tm, prov, tag;
  if(kind === 'csv'){
    const r = it.row;
    frp = r[2]; lng = r[0]; lat = r[1];
    tm = (DATES[r[5]] || '—') + ' ' + fmtAqTime(r[6]);
    tag = (r[7] === 0 ? '昼' : '夜');
    prov = it.prov || '';
  }else{
    const r = it.row;
    frp = r.frp; lng = parseFloat(r.longitude); lat = parseFloat(r.latitude);
    tm = (r.acq_date || '—') + ' ' + fmtAqTime(r.acq_time);
    tag = (r.daynight === 'D' ? '昼' : '夜');
    prov = r.satellite || '';
  }
  const tail = [(isNaN(lng) ? '' : lng.toFixed(3)) + ', ' + (isNaN(lat) ? '' : lat.toFixed(3)),
                tm, prov].filter(x => x && x !== '—').join(' · ');
  return '<div class="fpItem" data-kind="' + kind + '" data-i="' + i + '">'
    + '<span class="dot" style="background:' + frpColor(frp) + '"></span>'
    + '<span class="main"><span class="l1"><b class="frp">' + frpText(frp) + ' MW</b>'
    + '<i class="tag">' + tag + '</i></span>'
    + '<span class="l2">' + tail + '</span></span></div>';
}
function renderFireList(kind, items, resetLimit){
  const st = listStates[kind];
  // v0.9.0：**默认回到首屏窗口**，只有「显示更多」这一个场景显式传 false。
  // 改前的写法是「由每个调用点各自记得重置」，结果 drawCsvFires() 就没重置 ——
  // 实测在全国点 4 次「显示更多」把窗口撑到 300 后，切省再切回来仍会一次性渲染
  // 300 条 DOM，恰好复踩了 v0.8.0 想避开的「一次性建大量 DOM」性能坑。
  // 把安全行为设成默认值，忘记处理的调用点就自动是安全的。
  if(resetLimit !== false) st.limit = LIST_STEP;
  const body = document.getElementById(kind + 'ListBody');
  const meta = document.getElementById(kind + 'ListMeta');
  const more = document.getElementById(kind + 'ListMore');
  if(!body) return 0;
  const sortEl = document.getElementById(kind + 'ListSort');
  st.sort = sortEl ? sortEl.value : 'frp';
  st.cur = -1;
  // 仅在「排序维度变化」或「数据集版本变化（切省/切 Tab 重建了底层 items）」时才重新排序。
  // 全国视图 7739 条若每次「显示更多」都 slice+sort 一遍是 O(N log N) 的纯浪费——
  // 复用已排好的数组即可，N 大时（数万点）差异明显且零风险（排序结果确定、只读不写）。
  // 版本号由 drawCsvFires / drawAreaRows 在真正重建数据时自增，可稳定区分「数据变了」与
  // 「只是展开更多窗口」两种情形（后者传入的 items 就是上次的排序结果，引用相等但语义相同）。
  const version = (kind === 'csv') ? (window.__csvVersion || 0) : (window.__areaVersion || 0);
  const needResort = (st._cacheVer !== version) || (st._sortKey !== st.sort);
  if(needResort){
    st._sorted = items.slice();
    const frpOf = (kind === 'csv') ? (it => parseFloat(it.row[2]) || 0)
                                   : (it => parseFloat(it.row.frp) || 0);
    const tOf = (kind === 'csv') ? (it => csvTimeKey(it.row)) : (it => areaTimeKey(it.row));
    st._sorted.sort((a, b) => (st.sort === 'time') ? (tOf(b) - tOf(a)) : (frpOf(b) - frpOf(a)));
    st._cacheVer = version; st._sortKey = st.sort;
  }
  st.items = st._sorted;
  const n = st.items.length;
  if(meta) meta.textContent = n + ' 条';
  if(!n){
    body.innerHTML = '<div class="fpEmpty">'
      + (kind === 'csv' ? '当前范围没有火点' : '尚未拉取，先点上方「拉取视野火点」') + '</div>';
    if(more){ more.disabled = true; more.textContent = '显示更多'; }
    window['__' + kind + 'ListCount'] = 0;
    return 0;
  }
  const lim = Math.min(st.limit, n);
  let html = '';
  for(let i = 0; i < lim; i++) html += fpRowHtml(kind, st.items[i], i);
  body.innerHTML = html;
  if(more){
    more.disabled = lim >= n;
    more.textContent = (lim >= n) ? ('已显示全部 ' + n + ' 条') : ('显示更多（' + lim + '/' + n + '）');
  }
  window['__' + kind + 'ListCount'] = n;
  return n;
}
function renderCsvList(){ return renderFireList('csv', csvItems); }
function renderAreaList(){ return renderFireList('area', areaItems); }

// 列表 → 地图定位：跳跃 + 开弹窗 + 高亮当前行。
// activeTab 守卫不是多余的：口径互斥要求「别的方式的点不许出现在图上」，
// 万一将来有别的入口在错误的 Tab 下调到它，也不能把图层挂回去。
function focusFire(kind, i){
  const st = listStates[kind];
  const it = st.items[i];
  if(!it || activeTab() !== kind) return;
  st.cur = i;
  const body = document.getElementById(kind + 'ListBody');
  if(body){
    Array.prototype.forEach.call(body.querySelectorAll('.fpItem'), el => {
      el.classList.toggle('on', parseInt(el.dataset.i, 10) === i);
    });
  }
  const m = it.marker;
  if(!m) return;
  showLayer(kind === 'csv' ? csvLayer : areaLayer);
  map.setView(m.getLatLng(), Math.max(map.getZoom(), 10), {animate:true});
  m.openPopup();
  window.__focusedFire = {kind:kind, i:i, lat:m.getLatLng().lat, lng:m.getLatLng().lng};
}
function bindFireList(kind){
  const body = document.getElementById(kind + 'ListBody');
  if(body){
    body.addEventListener('click', e => {
      const el = (e.target.closest) ? e.target.closest('.fpItem') : null;
      if(!el) return;
      focusFire(kind, parseInt(el.dataset.i, 10));
    });
  }
  const sortEl = document.getElementById(kind + 'ListSort');
  if(sortEl) sortEl.addEventListener('change', () => {
    if(kind === 'csv') renderCsvList(); else renderAreaList();   // 换排序维度 = 重新看排行榜，回到首屏
  });
  const more = document.getElementById(kind + 'ListMore');
  if(more) more.addEventListener('click', () => {
    listStates[kind].limit += LIST_STEP;
    renderFireList(kind, listStates[kind].items, false);   // false = 保留已展开的窗口
  });
}

// ===== 界面：省份下拉 =====
function initProvinceSelect(){
  const sel = document.getElementById('provSelect');
  const names = PROVINCES.map(p=>p.name).sort();
  let html = '<option value="">全国（全部省份）</option>';
  names.forEach(n=>{
    const c = (FIRES[n]||[]).length;
    html += '<option value="'+n+'">'+n+(c?('（'+c+'）'):'（0）')+'</option>';
  });
  sel.innerHTML = html;
  sel.value = DEFAULT_PROVINCE || '';
}

// 方式2 的图层绘制 / 清空。缓存与图层分离：
// 切 Tab 只是隐藏图层（数据还在），切省才是真的作废数据。
function drawAreaRows(){
  hideLayer(areaLayer);
  areaLayer = null; areaItems = [];
  if(!areaRowsCache || !areaRowsCache.length){
    window.__areaFetched = 0;
    renderProvStat(); renderAreaList();
    return 0;
  }
  areaLayer = L.layerGroup();
  let n = 0;
  areaRowsCache.forEach(r=>{
    const lat = parseFloat(r.latitude), lng = parseFloat(r.longitude);
    if(isNaN(lat) || isNaN(lng)) return;
    const g = wgs84togcj02(lng, lat);
    const frp = r.frp, br = r.bright_ti4 || r.brightness || r.bright_t31;
    const m = L.circleMarker([g[1],g[0]], {renderer:fireRenderer, radius:frpRadius(frp),
      color:'#00e5ff', weight:1.6, fillColor:frpColor(frp), fillOpacity:0.9})
     .bindPopup('<b>火点（Area API 实时）</b><br>经度 '+lng+'<br>纬度 '+lat+
       '<br>亮度温度 '+br+' K<br>FRP '+frp+' MW<br>置信度 '+(r.confidence||'—')+
       '<br>采集日期 '+r.acq_date+'<br>采集时间 '+fmtAqTime(r.acq_time)+' UTC（分钟级）'+
       '<br>卫星 '+(r.satellite||'—'))
     .bindTooltip('FRP '+frp+' MW · '+r.acq_date, {direction:'top', offset:[0,-4], className:'fire-tip'});
    m.addTo(areaLayer);
    areaItems.push({row:r, marker:m});
    n++;
  });
  if(activeTab()==='area') showLayer(areaLayer);
  window.__areaFetched = n;
  // 数据集版本自增：列表排序缓存据此判断「数据是否真的变了」（切 Tab 不重建，但拉取新数据要重排）。
  window.__areaVersion = (window.__areaVersion || 0) + 1;
  renderProvStat();
  renderAreaList();
  return n;
}
function setAreaDlEnabled(n){
  const on = !!(n && n > 0);
  const a = document.getElementById('areaDlCsv'), b = document.getElementById('areaDlGeo');
  if(a) a.disabled = !on;
  if(b) b.disabled = !on;
}
function clearAreaData(){
  hideLayer(areaLayer); areaLayer = null; areaItems = [];
  areaRowsCache = null; areaMeta = null;
  window.__areaFetched = 0;
  const ru = document.getElementById('areaReqUrl'); if(ru) ru.textContent = '';
  setAreaDlEnabled(0);
  renderProvStat();
  renderAreaList();   // v0.8.0：列表必须与图层同生命周期，否则换省后列表还留着上个省的点
}

function onProvinceChange(){
  currentProvince = document.getElementById('provSelect').value;
  drawBoundaries(currentProvince);
  const n = drawCsvFires(currentProvince);
  updateInfo(currentProvince, n);
  // 已拉的方式2 数据属于「上一个范围」，换省必须作废，否则图上会留着别省的点
  clearAreaData();
  // 视野跟随所选省（仅在有该省界时）
  if(currentProvince && boundaryLayer){
    try{ map.fitBounds(boundaryLayer.getBounds(), {padding:[24,24]}); }catch(e){}
  }else{
    map.setView([28.0, 101.0], 4);
  }
  window.__province = currentProvince;
}

// ===== 顶部统计（随当前 Tab 切换口径）=====
// v0.6.1 修复：原先 provStat 只在「切省」时被写入方式1 的内嵌点数，切到方式2/3 后
// 数字纹丝不动，看着像「当前 Tab 的火点数」，实际永远属于方式1。
function provStatHtml(){
  const tab = window.__activeTab || 'csv';
  const src = (s)=>'<span class="src">'+s+'</span>';
  const num = (s)=>'<span class="num">'+s+'</span>';
  if(tab === 'area'){
    const n = window.__areaFetched || 0;
    return {html: src('Area API')+' · '+(n ? num(n+' 个火点') : '未拉取'),
            tip: n ? ('方式2：Area API 实时拉取 '+n+' 个火点（当前视野/绘制形状 ∩ 中国范围）')
                   : '方式2：尚未拉取。填入 MAP_KEY 并点「拉取视野火点」'};
  }
  if(tab === 'wms'){
    return {html: src('WMS 栅格')+' · 瓦片叠加',
            tip: '方式3：官方 WMS 栅格瓦片，由服务端渲染、不返回点计数；'+
                 '各方式的火点图层互斥显示，此时图上不会有方式1 的内嵌点'};
  }
  const n = window.__csvCount || 0;
  return {html: src('24h CSV')+' · '+num(n+' 个火点'),
          tip: '方式1：FIRMS 公开目录 24h 全球 CSV，已离线裁切内嵌；当前范围 '+n+' 个火点'};
}
function renderProvStat(){
  const el = document.getElementById('provStat');
  if(!el) return;
  const s = provStatHtml();
  el.innerHTML = s.html;
  el.title = s.tip;
}

function updateInfo(provName, n){
  // v2.0.1：信息面板统计卡整卡移除后，本函数只剩「方式1 数据来源卡」与全局计数的刷新。
  window.__csvCount = n;
  document.getElementById('csvMetaScope').textContent = provName || '全国（全部省份）';
  document.getElementById('csvMetaCount').textContent = n;
  renderProvStat();
}

// ===== Tab 切换 =====
function switchTab(tab){
  document.querySelectorAll('#tabs .tab').forEach(b=>b.classList.toggle('on', b.dataset.tab===tab));
  document.querySelectorAll('.tabPane').forEach(p=>p.classList.toggle('on', p.id==='pane-'+tab));
  window.__activeTab = tab;
  // v2.1.0：行政区划下拉只在方式1 显示（方式2/3 按视野取数，与省无关）
  document.body.classList.toggle('tab-csv', tab==='csv');

  // 三个方式的火点必须互斥出现在图上。v0.8.0 修复：此处原先只处理了 area/wms，
  // 唯独漏了 csvLayer —— 切到方式2/3 后方式1 的几千个点仍在图上，与实时拉的
  // 点混作一团，用户根本分不清哪些是当前方式的结果（无头浏览器取证：tab=area
  // 与 tab=wms 时 map.eachLayer 仍能枚举到 csvLayer）。
  // 注意是「摘图层」而不是「销毁重建」：全国版 7739 个 marker，重建是秒级开销。
  if(tab==='csv') showLayer(csvLayer); else hideLayer(csvLayer);
  // 方式2：数据仍在缓存里，切回来重新挂上即可（不要再 Destroy，否则白拉一次）
  if(tab==='area') showLayer(areaLayer);
  else { hideLayer(areaLayer); if(drawState) cancelDraw(); }
  // 方式3：连同图层控件里的勾选项一起摘掉，避免用户又在别的 Tab 下把它勾回来
  if(tab==='wms'){
    if(firmsWmsLayer) showLayer(firmsWmsLayer);
  }else if(firmsWmsLayer){
    hideLayer(firmsWmsLayer);
    if(wmsCtl) wmsCtl.removeLayer(firmsWmsLayer);
    firmsWmsLayer = null;
  }
  const lgArea = document.getElementById('lgAreaRow');
  if(lgArea) lgArea.style.display = (tab==='area') ? '' : 'none';
  // v0.10.1：绘制形状是方式2的视觉元素，别的 Tab 不许留在图上。实测：方式2画完
  // 矩形切到方式3，橙色虚线框仍显示在地图上（overlay 截图证实）——因为形状是独立
  // layer 且 setShape 直接 addTo(map)，switchTab 此前只摘 areaLayer 没摘它。
  // 摘挂而不销毁：areaShape 数据保留，切回方式2 原样恢复，不用重画。
  if(areaShapeLayer) (tab==='area') ? showLayer(areaShapeLayer) : hideLayer(areaShapeLayer);
  const lgShape = document.getElementById('lgShapeRow');
  if(lgShape) lgShape.style.display = (tab==='area' && areaShape) ? '' : 'none';
  renderProvStat();   // 切 Tab 必须换口径：三个方式的点数不是一回事
}

// ===== 方式2：自由绘制区域（矩形 / 多边形）=====
//
// 为什么自己写而不用 Leaflet.Draw：本页面要求「单文件、零外部依赖」，
// 为一个「画个范围」的需求引入整套插件（还有它自带的 CSS 与图标资源）不划算。
// 而 Area API 真正需要的其实只有两件事：形状的**包络盒**（拼进请求 bbox）
// 与**点在多边形内判定**（把返回的点筛到区域内）——都在下面几十行里。
let drawState = null;        // null | 'rect' | 'poly'
let drawPreview = null;      // 绘制过程中的预览图形
let areaShape = null;        // {type:'rect'|'poly', latlngs:[L.LatLng]}（GCJ-02 显示坐标）
let areaShapeLayer = null;
let _rectStart = null, _polyPts = [], _polyTimer = null;

function setDrawHint(t){ const el = document.getElementById('areaDrawHint'); if(el) el.textContent = t || ''; }
function shapeStyle(){ return {color:'#ff8f00', weight:2, dashArray:'6 4',
  fillColor:'#ff8f00', fillOpacity:0.08, interactive:false}; }
function clearDrawPreview(){ if(drawPreview){ map.removeLayer(drawPreview); drawPreview = null; } }

// 双击会先派发两次 click，再派发 dblclick —— Leaflet 没有「二者互斥」的开关，
// 故用短延时把被双击吞掉的第二次 click 取消掉，否则闭合时会多出两个顶点。
// 200ms 是实测够用的阈值：两次真实点击间隔通常远大于此，双击的第二次则远小于此。
const DBLCLICK_GUARD_MS = 200;

function rectFromBounds(b){
  return [b.getSouthWest(), b.getNorthWest(), b.getNorthEast(), b.getSouthEast()];
}
function _rectDown(e){
  _rectStart = e.latlng;
  clearDrawPreview();
  drawPreview = L.polygon(rectFromBounds(L.latLngBounds(_rectStart, _rectStart)), shapeStyle());
  drawPreview.addTo(map);
}
function _rectMove(e){
  if(!_rectStart || !drawPreview) return;
  drawPreview.setLatLngs(rectFromBounds(L.latLngBounds(_rectStart, e.latlng)));
}
function _rectUp(e){
  if(!_rectStart) return;
  const b = L.latLngBounds(_rectStart, e.latlng || _rectStart);
  const ll = rectFromBounds(b);
  _rectStart = null;
  clearDrawPreview();
  // 手滑点一下几乎不产生面积，别生成退化形状，否则拉取会得到一个空区
  if(b.getNorth() - b.getSouth() < 0.002 || b.getEast() - b.getWest() < 0.002){
    _endDraw('矩形太小已忽略，请按住鼠标拖出一个范围（或改用「绘制多边形」）');
    return;
  }
  setShape('rect', ll);
  _endDraw('已锁定矩形区域，点「拉取视野火点」拉取（Esc 或「清除区域」可撤销）');
}
function redrawPolyPreview(){
  clearDrawPreview();
  drawPreview = L.polygon(_polyPts.concat([_polyPts[0]]), shapeStyle());
  drawPreview.addTo(map);
}
function _polyClick(e){
  if(_polyTimer) clearTimeout(_polyTimer);
  const ll = e.latlng;
  _polyTimer = setTimeout(()=>{
    _polyTimer = null;
    // 点回起点也算闭合：屏幕 12px 内判定，避免必须双击（双击在触摸板上不好按）
    if(_polyPts.length >= 3){
      const p0 = map.latLngToContainerPoint(_polyPts[0]);
      const pc = map.latLngToContainerPoint(ll);
      if(Math.abs(p0.x - pc.x) < 12 && Math.abs(p0.y - pc.y) < 12){ closePoly(); return; }
    }
    _polyPts.push(ll);
    redrawPolyPreview();
  }, DBLCLICK_GUARD_MS);
}
function _polyMove(e){
  if(!_polyPts.length || !drawPreview) return;
  drawPreview.setLatLngs(_polyPts.concat([e.latlng]));
}
function _polyDbl(){
  if(_polyTimer){ clearTimeout(_polyTimer); _polyTimer = null; }
  closePoly();
}
function closePoly(){
  if(_polyPts.length < 3){
    setDrawHint('至少需要 3 个顶点才能闭合，请继续点击（Esc 取消）');
    return;
  }
  const ll = _polyPts.slice();
  _polyPts = [];
  clearDrawPreview();
  setShape('poly', ll);
  _endDraw('已锁定多边形区域，点「拉取视野火点」拉取（Esc 或「清除区域」可撤销）');
}
function _endDraw(hint){
  map.off('mousedown', _rectDown); map.off('mousemove', _rectMove); map.off('mouseup', _rectUp);
  map.off('click', _polyClick); map.off('mousemove', _polyMove); map.off('dblclick', _polyDbl);
  if(_polyTimer){ clearTimeout(_polyTimer); _polyTimer = null; }
  drawState = null; _rectStart = null; _polyPts = [];
  document.body.classList.remove('map-draw');
  map.dragging.enable();
  map.doubleClickZoom.enable();
  clearDrawPreview();
  setDrawHint(hint || '');
}
function cancelDraw(){ _endDraw(''); }
function startDrawRect(){
  cancelDraw();
  drawState = 'rect'; _rectStart = null;
  document.body.classList.add('map-draw');
  map.dragging.disable();       // 否则按住拖动会变成平移地图
  map.on('mousedown', _rectDown); map.on('mousemove', _rectMove); map.on('mouseup', _rectUp);
  setDrawHint('按住鼠标左键拖出矩形，松开即完成（Esc 取消）');
}
function startDrawPoly(){
  cancelDraw();
  drawState = 'poly'; _polyPts = [];
  document.body.classList.add('map-draw');
  map.doubleClickZoom.disable();
  map.on('click', _polyClick); map.on('mousemove', _polyMove); map.on('dblclick', _polyDbl);
  setDrawHint('依次点击勾出顶点；双击或点回起点闭合（至少 3 点，Esc 取消）');
}
function setShape(type, latlngs){
  hideLayer(areaShapeLayer); areaShapeLayer = null;
  areaShape = {type:type, latlngs:latlngs.map(ll=>L.latLng(ll.lat, ll.lng)), wgsRing:null};
  areaShapeLayer = L.polygon(areaShape.latlngs, shapeStyle());
  showLayer(areaShapeLayer);   // v0.10.1：统一走 show/hide 入口，不再裸 addTo(map)
  const lg = document.getElementById('lgShapeRow'); if(lg) lg.style.display = '';
  updateShapeHint();
}
function clearShape(){
  hideLayer(areaShapeLayer); areaShapeLayer = null; areaShape = null;
  updateShapeHint();
  const lg = document.getElementById('lgShapeRow'); if(lg) lg.style.display = 'none';
  setDrawHint('已清除绘制区域，拉取范围回到「地图视野」');
}
function updateShapeHint(){
  const el = document.getElementById('areaShapeHint');
  if(!el) return;
  if(!areaShape){
    el.className = '';
    el.textContent = '当前范围：地图视野 ∩ 中国范围';
    return;
  }
  // 提示里给 WGS-84：Area API 认的是这套坐标，给 GCJ-02 会让人对不上请求 URL
  const b = L.latLngBounds(areaShape.latlngs);
  const nw = gcj02towgs84(b.getWest(), b.getNorth());
  const se = gcj02towgs84(b.getEast(), b.getSouth());
  el.className = 'has';
  el.textContent = '当前范围：' + (areaShape.type === 'rect' ? '矩形' : '多边形')
    + ' ' + nw[0].toFixed(3) + ',' + se[1].toFixed(3)
    + ' ~ ' + se[0].toFixed(3) + ',' + nw[1].toFixed(3) + '（WGS-84）∩ 中国范围';
}
// 射线法：判断 WGS-84 经纬点是否落在绘制区域内（无孔多边形，够用）
function shapeContains(lng, lat){
  if(!areaShape) return true;
  if(!areaShape.wgsRing){
    areaShape.wgsRing = areaShape.latlngs.map(ll => gcj02towgs84(ll.lng, ll.lat));
  }
  const ring = areaShape.wgsRing;
  let inside = false;
  for(let i = 0, j = ring.length - 1; i < ring.length; j = i++){
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    if((yi > lat) !== (yj > lat) && lng < (xj - xi) * (lat - yi) / (yj - yi) + xi){
      inside = !inside;
    }
  }
  return inside;
}
window.__shapeState = function(){
  if(!areaShape) return null;
  const b = L.latLngBounds(areaShape.latlngs);
  return {type:areaShape.type, n:areaShape.latlngs.length,
          west:b.getWest(), south:b.getSouth(), east:b.getEast(), north:b.getNorth()};
};

// ===== 方式2：Area API =====
function mapDir(){
  try{
    if(location.protocol === 'file:'){
      var p = decodeURIComponent(location.pathname).replace(/^\/([A-Za-z]:)/, '$1');
      var i = p.lastIndexOf('/');
      if(i > 0) return p.slice(0, i).replace(/\//g, '\\');
    }
  }catch(e){}
  return MAP_DIR;
}
function startCmd(){
  return '"' + PYTHON_EXE + '" "' + SERVE_PY + '" --dir "' + mapDir() + '" --port ' + AREA_PROXY_PORT + ' --open';
}
function showProxyHelp(show){
  const box = document.getElementById('proxyHelp');
  if(!box) return;
  box.style.display = show ? 'block' : 'none';
  if(!show) return;
  const cb = document.getElementById('proxyCmdBox');
  cb.style.display = 'block';
  cb.textContent = '最省事：双击本地图所在文件夹里的 启动火点地图.cmd（Windows）或 启动火点地图.sh（macOS/Linux）。\n或在终端执行：\n' + startCmd();
}
async function copyCmd(){
  const cmd = startCmd();
  let ok = false;
  try{ await navigator.clipboard.writeText(cmd); ok = true; }catch(e){ ok = false; }
  if(!ok){
    try{
      const ta = document.createElement('textarea');
      ta.value = cmd; ta.style.position='fixed'; ta.style.opacity='0';
      document.body.appendChild(ta); ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    }catch(e2){ ok = false; }
  }
  const btn = document.getElementById('proxyCopy');
  btn.textContent = ok ? '已复制 ✓' : '请手动复制';
  setTimeout(()=>{ btn.textContent = '复制启动命令'; }, 2200);
}
async function probeProxy(base){
  try{
    const r = await fetch(base.replace(/\/$/, '') + '/api/fires/health', {cache:'no-store'});
    if(!r.ok) return false;
    const j = await r.json();
    return !!(j && j.ok);
  }catch(e){ return false; }
}
async function discoverAreaProxy(){
  if(areaProxyBase) return areaProxyBase;
  // 探测失败不缓存：用户常是「先点按钮 → 看到提示 → 去起代理 → 再点一次」
  try{
    const q = new URLSearchParams(location.search).get('proxy');
    if(q){
      const base = q.replace(/\/api(\/.*)?$/, '').replace(/\/$/, '');
      if(await probeProxy(base)){ areaProxyBase = base; return areaProxyBase; }
    }
  }catch(e){}
  if(location.protocol !== 'file:'){
    const base = location.origin;
    if(await probeProxy(base)){ areaProxyBase = base; return areaProxyBase; }
  }
  for(const u of AREA_PROXY_CANDIDATES){
    const b = u.replace(/\/$/, '');
    if(await probeProxy(b)){ areaProxyBase = b; return areaProxyBase; }
  }
  return null;
}
function parseCsv(txt){
  const lines = txt.trim().split(/\r?\n/); if(!lines.length) return [];
  const head = lines[0].split(','); const out = [];
  for(let i=1;i<lines.length;i++){
    const c = lines[i].split(','), o = {};
    for(let j=0;j<head.length;j++) o[head[j].trim()] = (c[j]||'').trim();
    out.push(o);
  }
  return out;
}
// 回溯窗口 1..AREA_MAX_WINDOW 天 → 若干段「<=5 天」的请求。
// 实测依据：DATE 是**起始日**，窗口 = [DATE, DATE+DAYS-1]；DAYS 单次上限 5（6 被拒）。
//   5/2026-10-17 → 10-17..10-21 ；5/2026-10-22 → 10-22..10-26 ，两段拼接正好 10 天。
// 本地日期格式化。**不能用 toISOString()**：它先把本地时刻转成 UTC 再输出，
// 对 UTC+8 而言「本地 00:00」会被写成前一天的 16:00Z → 日期整整早一天。
function ymdLocal(d){
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0')
       + '-' + String(d.getDate()).padStart(2,'0');
}
function planAreaRequests(days, date){
  days = Math.max(1, Math.min(AREA_MAX_WINDOW, parseInt(days, 10) || 1));
  if(days <= AREA_MAX_SINGLE && !date) return [{days:days, date:''}];
  let start;
  if(date){
    start = new Date(date + 'T00:00:00');
    if(isNaN(start.getTime())) return [{days:Math.min(days, AREA_MAX_SINGLE), date:''}];
  }else{
    // 必须用**本地日期**构造：Python 侧的基准是 datetime.date.today()（本地），
    // 若这里用 UTC 日期，在每天 UTC 22:00-24:00（即北京时间次日 06:00-08:00）
    // 两端起始日会整整差一天 —— 实测同一时刻 Python 得 10-21、JS 得 10-20。
    const now = new Date();
    start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    start.setDate(start.getDate() - (days - 1));
  }
  const out = []; let left = days;
  while(left > 0){
    const d = Math.min(AREA_MAX_SINGLE, left);
    out.push({days:d, date:ymdLocal(start)});
    start = new Date(start.getFullYear(), start.getMonth(), start.getDate() + d);
    left -= d;
  }
  return out;
}
function planHint(days, date){
  const plan = planAreaRequests(days, date);
  if(plan.length <= 1){
    return '回溯 ' + days + ' 天' + (date ? ('（起始 ' + date + '）') : '（最近）') + ' → 1 次请求。';
  }
  return '回溯 ' + days + ' 天 > 单次上限 ' + AREA_MAX_SINGLE + ' 天 → 拆为 ' + plan.length +
    ' 次请求：' + plan.map(p=>p.date+'×'+p.days).join('、') + '（结果自动去重合并）。';
}
// 多段响应去重：相邻段在边界日期上会重复返回同一批火点
function mergeCsvRows(texts){
  const out = [], seen = new Set();
  (texts||[]).forEach(t=>{
    parseCsv(t).forEach(r=>{
      const k = [r.latitude, r.longitude, r.acq_date, r.acq_time, r.satellite].join('|');
      if(seen.has(k)) return;
      seen.add(k); out.push(r);
    });
  });
  return out;
}

async function pullAreaFires(){
  const key = (document.getElementById('firmsKey').value||'').trim();
  const st = document.getElementById('wmsStatus');
  // 语义色走 class（busy 蓝 / ok 绿 / err 红 / null 中性灰），不再用内联 style 写死颜色
  const setS = (t, ok)=>{ st.textContent=t;
    st.className = ok===false?'err':(ok===true?'ok':(ok===null?'':'busy')); };
  if(!key){ setS('请先输入 FIRMS MAP_KEY', false); return; }
  setS('正在连接本地代理…');
  const base = await discoverAreaProxy();
  if(!base){
    showProxyHelp(true);
    setS('未找到本地代理服务（FIRMS 不允许浏览器跨域直连 Area API，必须经本地代理转发）', false);
    return;
  }
  showProxyHelp(false);
  // 拉取范围（GCJ-02 显示坐标 → WGS-84 bbox）：
  //   有绘制形状时用它取代地图视野 —— 视野是矩形且随缩放漂移，很难稳定复现
  //   同一块研究区；手画的形状则把「关注区」显式固定下来了。
  const selBounds = areaShape ? L.latLngBounds(areaShape.latlngs) : map.getBounds();
  const nw = gcj02towgs84(selBounds.getWest(), selBounds.getNorth());
  const se = gcj02towgs84(selBounds.getEast(), selBounds.getSouth());
  let bx0 = nw[0], by0 = se[1], bx1 = se[0], by1 = nw[1];
  // 与中国范围求交兜底。v2.1.0 起方式2 不再联动行政区划：行政区划是方式1
  // （内嵌分省 CSV）的概念，方式2 的范围只由「地图视野或绘制形状」决定。
  // 此前「视野 ∩ 所选省」是隐藏耦合——省选在方式1 的下拉里，却悄悄改变
  // 方式2 的请求 bbox（且切到方式2 后下拉已隐藏，用户无从得知/更改）。
  // 大范围拉取慢的对策不变：缩小视野或用「绘制区域」框定研究区。
  bx0 = Math.max(bx0, 73); by0 = Math.max(by0, 3);
  bx1 = Math.min(bx1, 136); by1 = Math.min(by1, 54);
  if(bx1 <= bx0 || by1 <= by0){ setS('当前视野不在中国范围，请平移回中国后再拉取', false); return; }
  const bbox = [bx0, by0, bx1, by1].map(v=>v.toFixed(4)).join(',');
  const days = Math.max(1, Math.min(AREA_MAX_WINDOW,
    parseInt(document.getElementById('areaDays').value, 10) || 1));
  const dateVal = (document.getElementById('areaDate').value || '').trim();
  const src = document.getElementById('areaSource').value;
  const plan = planAreaRequests(days, dateVal);
  window.__areaPlan = plan;
  const tag = (plan.length > 1 ? ('（第 1/' + plan.length + ' 段）') : '');
  setS('拉取中… ' + bbox + ' …' + tag);
  // 超大范围会非常慢：给出预期提示（实测 20°x25° 约 12s，40°x35° 约 68s）
  const slowTimer = setTimeout(()=>{
    setS('拉取中… ' + bbox + ' …\n范围较大，可能需 1 分钟以上（建议缩小视野，或用「绘制区域」框定研究区）');
  }, 8000);
  try{
    const texts = [], urls = [];
    for(let i = 0; i < plan.length; i++){
      if(plan.length > 1) setS('拉取中… ' + bbox + ' …（第 ' + (i+1) + '/' + plan.length +
        ' 段：' + plan[i].date + ' × ' + plan[i].days + ' 天）');
      const url = base + AREA_BASE + '/' + encodeURIComponent(key)
                + '/' + encodeURIComponent(src)
                + '/' + encodeURIComponent(bbox)
                + '/' + plan[i].days
                + (plan[i].date ? ('/' + plan[i].date) : '');
      const ctrl = new AbortController();
      const abortTimer = setTimeout(()=>ctrl.abort(), 180000);
      try{
        const res = await fetch(url, {signal: ctrl.signal});
        clearTimeout(abortTimer);
        const txt = await res.text();
        if(!res.ok){ clearTimeout(slowTimer); setS('Area API 失败：' + txt.slice(0,140), false); return; }
        texts.push(txt); urls.push(url);
      }catch(e){ clearTimeout(abortTimer); throw e; }
    }
    clearTimeout(slowTimer);
    const fetched = mergeCsvRows(texts);
    // 绘制的是多边形时要再做一次内点判定：Area API 只接受 bbox，返回的是
    // 包络盒里的全部点；不筛就相当于按矩形算，画的形状失去意义。
    const rows = fetched.filter(r => shapeContains(parseFloat(r.longitude),
                                                   parseFloat(r.latitude)));
    areaRowsCache = rows;
    areaMeta = {source:src, days:days, date:dateVal, bbox:bbox, urls:urls,
                shape: areaShape ? areaShape.type : 'view'};
    const n = drawAreaRows();
    window.__areaUrl = urls[urls.length - 1] || '';
    const shapeNote = (areaShape && fetched.length !== n)
      ? ('；按绘制' + (areaShape.type === 'rect' ? '矩形' : '多边形') + '从返回 ' + fetched.length + ' 个中筛出 ' + n + ' 个') : '';
    setS(n > 0
      ? ('已拉取 ' + n + ' 个火点（' + src + '，' + days + ' 天'
         + (plan.length > 1 ? ('，' + plan.length + ' 段请求已去重合并') : '')
         + (areaShape ? ('，' + (areaShape.type === 'rect' ? '矩形' : '多边形') + '区域') : '视野')
         + shapeNote + '）')
      // v0.9.0 修复：这里原先传 `n > 0`，n=0 时即 false → className='err' →
      // 「该范围内无火点」被渲染成**红色报错**（实测 color: rgb(198,40,40)），
      // 与注释「不该显示成红色报错」的意图完全相反 —— 用户会以为接口坏了。
      // setS 的契约是 false=错误 / true=成功 / null 或不传=中性或进行中，此处用 null。
      // 顺带提示「把天数调大」：VIIRS NRT 的当日过境常晚于当前时刻，days=1 拉空
      // 是常态而非故障（实测同一 bbox days=1 返回 0 条、days=3 返回数据）。
      : ('该范围内无火点'
         + (days < 3 ? '（可把「天数」调到 3–5 天：当日过境数据常晚于当前时刻）' : '')),
        n > 0 ? true : null);
    setAreaDlEnabled(n);
    document.getElementById('areaReqUrl').textContent =
      urls.map(u => '请求：' + u.replace(encodeURIComponent(key), '<MAP_KEY>')).join('\n');
  }catch(err){
    clearTimeout(slowTimer);
    const msg = (err && err.message) ? err.message : String(err);
    setS('拉取失败：' + msg + '\n请确认本地代理仍在运行（双击 启动火点地图.cmd）；范围过大时可先在上方选择具体省份', false);
  }
}

// ===== 方式3：WMS 栅格 =====
function applyFirmsKey(){
  const key = (document.getElementById('firmsKeyWms').value||'').trim();
  const st = document.getElementById('wmsStatusWms');
  const setS = (t, ok)=>{ st.textContent=t;
    st.className = ok===false?'err':(ok===true?'ok':(ok===null?'':'busy')); };
  if(!WMS_LAYER){ setS('当前产品无 WMS 图层', false); return; }
  if(!key){ setS('请先输入 FIRMS MAP_KEY', false); return; }
  if(firmsWmsLayer){ if(wmsCtl) wmsCtl.removeLayer(firmsWmsLayer); map.removeLayer(firmsWmsLayer); firmsWmsLayer=null; }
  firmsWmsLayer = L.tileLayer.wms(FIRMS_WMS_BASE + key + '/',
    {layers: WMS_LAYER, format:'image/png', transparent:true,
     opacity:0.85, version:'1.1.1', maxZoom:18,
     errorTileUrl: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAAQ='});
  firmsWmsLayer.addTo(map);
  if(!wmsCtl){ wmsCtl = L.control.layers(null, null, {position:'topright'}).addTo(map); }
  wmsCtl.addOverlay(firmsWmsLayer, 'FIRMS WMS 火点（官方栅格）');
  setS('已叠加 WMS：' + WMS_LAYER + '（若无显示请检查 MAP_KEY 是否有效）', true);
}

// ===== 初始化 =====
(function(){
  // 工具栏实际高度写入 CSS 变量：Leaflet 控件与左列都据此下移，
  // 窄屏换行时高度会变，动态测量才不会写死数值导致重叠。
  function syncTopbarHeight(){
    const h = document.getElementById('topbar').offsetHeight;
    document.documentElement.style.setProperty('--topbar-h', h + 'px');
  }
  syncTopbarHeight();
  if(window.ResizeObserver){
    new ResizeObserver(syncTopbarHeight).observe(document.getElementById('topbar'));
  }else{
    window.addEventListener('resize', syncTopbarHeight);
  }

  initProvinceSelect();
  document.querySelectorAll('#tabs .tab').forEach(b=>{
    b.addEventListener('click', ()=>switchTab(b.dataset.tab));
  });
  const sel = document.getElementById('areaSource');
  AREA_SOURCES.forEach(s=>{ const o=document.createElement('option'); o.value=s; o.textContent=s; sel.appendChild(o); });
  sel.value = AREA_SOURCES.indexOf(%%AREA_SOURCE_JSON%%)>=0 ? %%AREA_SOURCE_JSON%% : AREA_SOURCES[0];

  // 方式2：切换火点产品（卫星）时，给出该产品的一句简介，帮助用户快速区分分辨率/重访/时效
  const PRODUCT_INFO = {
    'VIIRS_SNPP_NRT':  {label:'VIIRS S-NPP · 近实时', desc:'Suomi NPP 星载 VIIRS，火点像元 375m；NRT 过境后约 3 小时入库，每日约 2 次过境。'},
    'VIIRS_SNPP_SP':   {label:'VIIRS S-NPP · 标准',   desc:'同上卫星，标准产品（完整科学算法再处理），质量更高、延迟数天。'},
    'VIIRS_NOAA20_NRT':{label:'VIIRS NOAA-20 · 近实时', desc:'NOAA-20 星载 VIIRS，375m；与 S-NPP 互补，可把每日过境提到约 4 次。'},
    'VIIRS_NOAA20_SP': {label:'VIIRS NOAA-20 · 标准',   desc:'NOAA-20 标准产品，延迟数天。'},
    'VIIRS_NOAA21_NRT':{label:'VIIRS NOAA-21 · 近实时', desc:'NOAA-21 星载 VIIRS，375m；较新平台，覆盖仍在完善。'},
    'VIIRS_NOAA21_SP': {label:'VIIRS NOAA-21 · 标准',   desc:'NOAA-21 标准产品，延迟数天。'},
    'MODIS_NRT':       {label:'MODIS · 近实时', desc:'Terra/Aqua 双星 MODIS，火点像元 1km；分辨率较低但历史长、全球一致，每日约 4 次过境。'},
    'MODIS_SP':        {label:'MODIS · 标准',   desc:'MODIS 标准产品，延迟数天。'},
    'LANDSAT_NRT':     {label:'Landsat 热异常 · 近实时', desc:'Landsat 8/9 热红外，30m 分辨率极高，但重访约 16 天、覆盖稀疏，适合定点精细核查。'}
  };
  const srcInfoEl = document.getElementById('areaSourceInfo');
  function updateSrcInfo(){
    const s = document.getElementById('areaSource').value;
    const info = PRODUCT_INFO[s];
    srcInfoEl.innerHTML = info ? ('<b>'+info.label+'</b>' + info.desc) : '';
  }
  document.getElementById('areaSource').addEventListener('change', updateSrcInfo);
  updateSrcInfo();

  // 方式2：天数 / 起始日 变化时实时提示「会拆成几次请求」，避免用户以为卡住
  const refreshPlanHint = ()=>{
    const el = document.getElementById('areaPlanHint');
    if(!el) return;
    const d = Math.max(1, Math.min(AREA_MAX_WINDOW,
      parseInt(document.getElementById('areaDays').value, 10) || 1));
    el.textContent = planHint(d, (document.getElementById('areaDate').value || '').trim());
  };
  document.getElementById('areaDays').addEventListener('input', refreshPlanHint);
  document.getElementById('areaDate').addEventListener('change', refreshPlanHint);
  document.getElementById('areaDateClear').addEventListener('click', ()=>{
    document.getElementById('areaDate').value = ''; refreshPlanHint();
  });
  refreshPlanHint();

  // —— 火点列表：两个方式各一份，委托到滚动容器上，只绑一次 ——
  bindFireList('csv');
  bindFireList('area');
  renderAreaList();   // 初始要在方式2 列表里显示「尚未拉取」，而不是空面板

  // —— 自由绘制：三个按钮 + Esc 取消 ——
  document.getElementById('areaDrawRect').addEventListener('click', e => {
    e.target.blur(); startDrawRect();
  });
  document.getElementById('areaDrawPoly').addEventListener('click', e => {
    // 主动失焦：否则按钮吃焦点，之后按 Enter 会重复触发它而不是做别的事
    e.target.blur(); startDrawPoly();
  });
  document.getElementById('areaClearShape').addEventListener('click', e => {
    e.target.blur(); cancelDraw(); clearShape();
  });
  document.addEventListener('keydown', e => {
    if(e.key !== 'Escape') return;
    // 输入框里按 Esc 走原生行为（不抢焦点），只处理地图区
    if(e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if(drawState){ cancelDraw(); }            // 取消正在进行的绘制手势
    else if(areaShape){ clearShape(); }       // 撤销已锁定的形状（与「清除区域」等价）
  }, true);  // 捕获阶段：先于 Leaflet 的键盘处理，避免被 stopPropagation 拦截

  // 方式1 下载
  const dlScopeName = ()=> currentProvince || '全国';
  document.getElementById('csvDlCsv').addEventListener('click', ()=>{
    const rows = csvRowsForScope();
    if(!rows.length){ document.getElementById('csvDlStat').textContent = '当前范围没有火点可下载'; return; }
    const nm = downloadRows(rows, dlScopeName(), 'csv');
    document.getElementById('csvDlStat').textContent = '已导出 ' + rows.length + ' 条 → ' + nm + '.csv';
  });
  document.getElementById('csvDlGeo').addEventListener('click', ()=>{
    const rows = csvRowsForScope();
    if(!rows.length){ document.getElementById('csvDlStat').textContent = '当前范围没有火点可下载'; return; }
    const nm = downloadRows(rows, dlScopeName(), 'geo');
    document.getElementById('csvDlStat').textContent = '已导出 ' + rows.length + ' 个要素 → ' + nm + '.geojson';
  });

  // 方式2 下载（导出的是本次拉取的原始行，字段与 FIRMS 返回一致）
  const areaRowsForDownload = ()=> (areaRowsCache || []).map(r=>({
    longitude:r.longitude, latitude:r.latitude,
    brightness:(r.bright_ti4 || r.brightness || r.bright_t31), frp:r.frp,
    confidence:r.confidence, acq_date:r.acq_date,
    acq_time:String(r.acq_time || '').padStart(4, '0'),
    daynight:r.daynight, satellite:r.satellite
  }));
  const onAreaDl = (kind)=>{
    const rows = areaRowsForDownload();
    if(!rows.length) return;
    const nm = downloadRows(rows, 'area_' + (areaMeta ? areaMeta.source : 'firms'), kind);
    document.getElementById('areaReqUrl').textContent =
      (document.getElementById('areaReqUrl').textContent + '\n已导出 ' + rows.length + ' 条 → ' + nm
       + (kind === 'geo' ? '.geojson' : '.csv')).trim();
  };
  document.getElementById('areaDlCsv').addEventListener('click', ()=>onAreaDl('csv'));
  document.getElementById('areaDlGeo').addEventListener('click', ()=>onAreaDl('geo'));

  document.getElementById('areaApply').addEventListener('click', pullAreaFires);
  document.getElementById('proxyCopy').addEventListener('click', copyCmd);
  document.getElementById('proxyRetry').addEventListener('click', ()=>{ pullAreaFires(); });
  document.getElementById('wmsApply').addEventListener('click', applyFirmsKey);
  document.getElementById('firmsKey').addEventListener('keydown', e=>{ if(e.key==='Enter') pullAreaFires(); });
  document.getElementById('provSelect').addEventListener('change', onProvinceChange);
  document.getElementById('firmsKeyWms').addEventListener('keydown', e=>{ if(e.key==='Enter') applyFirmsKey(); });

  if(INITIAL_KEY){
    document.getElementById('firmsKey').value = INITIAL_KEY;
    document.getElementById('firmsKeyWms').value = INITIAL_KEY;
  }
  window.__activeTab = 'csv';   // 必须在 onProvinceChange 之前：统计口径按当前 Tab 渲染
  onProvinceChange();
  window.__province = currentProvince;
})();
</script>
</body>
</html>
"""



def build_html(out_html, product_label, provinces, fires_by_province,
               dates, conf_list, total_fires,
               mapkey=None, wms_layer=None, area_base="/api/area/csv",
               area_source=None, proxy_port=8765, default_province=""):
    """生成自包含 HTML：三种取数方式（Tab 切换）+ 中国省级行政区下拉筛选。

    数据内嵌，故切换省份是**纯前端**操作（无需重新下载、无需代理）。
    """
    # 生成期注入真实路径：界面提示里的启动命令要能直接复制执行
    serve_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve_fires.py")
    map_dir = os.path.dirname(os.path.abspath(out_html))
    python_exe = sys.executable or "python"
    # 端口必须与候选探测列表一致：提示里给的启动命令用这个端口，
    # 页面也必须能探测到它，否则「按提示启动」后依然连不上。
    proxy_port = int(proxy_port or 8765)
    area_proxy_candidates = [
        f"http://127.0.0.1:{proxy_port}", "http://127.0.0.1:8790", "http://127.0.0.1:8080",
        "http://127.0.0.1:8000", "http://127.0.0.1:8888", f"http://localhost:{proxy_port}",
    ]

    # 内联 Leaflet（优先本地文件，缺失则回退 CDN），避免预览/webview 拦截外部脚本
    # 注意：文件必须平铺在 scripts/ 下（根/scripts/文件 = 两级）——Skill 包上传
    # 仅支持两级目录结构，曾经放在 scripts/lib/ 下会导致整包上传被拒。
    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    lf_js = os.path.join(_scripts_dir, "leaflet.js")
    lf_css = os.path.join(_scripts_dir, "leaflet.css")
    if os.path.exists(lf_js) and os.path.exists(lf_css):
        with open(lf_css, encoding="utf-8") as fh:
            leaflet_css = f"<style>{fh.read()}</style>"
        with open(lf_js, encoding="utf-8") as fh:
            leaflet_js = f"<script>{fh.read()}</script>"
    else:
        leaflet_css = '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>'
        leaflet_js = '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'

    # 省界（GCJ-02，直接对齐高德底图）。坐标压到 4 位：约 11m 精度，体积减半。
    prov_json = [{
        "name": p["name"],
        "adcode": p["adcode"],
        "geometry": _round_geom(p["geometry"], 4),
        "bbox": [round(v, 4) for v in p["bbox"]],
    } for p in provinces]

    # 火点紧凑行：{省名: [[lng,lat,frp,bright,confIdx,dateIdx,acqTime,dnIdx], ...]}
    fires_json = {name: rows["rows"] for name, rows in fires_by_province.items()
                  if rows["rows"]}

    meta = {"product": product_label, "total": total_fires,
            "provinces": len(provinces), "default_province": default_province}

    html = (HTML_TPL
            .replace("%%TITLE%%", "NASA FIRMS 全球近实时火点")
            .replace("%%PRODUCT%%", json.dumps(product_label, ensure_ascii=False))
            .replace("%%PRODUCT_TXT%%", product_label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            .replace("%%VERSION_TXT%%", SKILL_VERSION)
            .replace("%%SKILL_VERSION%%", json.dumps(SKILL_VERSION))
            .replace("%%LEAFLET_CSS%%", leaflet_css)
            .replace("%%LEAFLET_JS%%", leaflet_js)
            .replace("%%PROVINCES%%", json.dumps(prov_json, ensure_ascii=False,
                                                  separators=(",", ":")))
            .replace("%%FIRES%%", json.dumps(fires_json, ensure_ascii=False,
                                              separators=(",", ":")))
            .replace("%%DATES%%", json.dumps(dates, ensure_ascii=False))
            .replace("%%CONF%%", json.dumps(conf_list, ensure_ascii=False))
            .replace("%%WMS_LAYER%%", json.dumps(wms_layer or ""))
            .replace("%%WMS_KEY%%", json.dumps(mapkey or ""))
            .replace("%%AREA_BASE%%", json.dumps(area_base.rstrip("/")))
            .replace("%%AREA_PROXY_CANDIDATES%%", json.dumps(area_proxy_candidates))
            .replace("%%SERVE_PY%%", json.dumps(serve_py))
            .replace("%%MAP_DIR%%", json.dumps(map_dir))
            .replace("%%PYTHON_EXE%%", json.dumps(python_exe))
            .replace("%%PROXY_PORT%%", json.dumps(str(proxy_port)))
            .replace("%%AREA_SOURCES%%", json.dumps(FIRMS_AREA_SOURCES))
            .replace("%%AREA_SOURCE_JSON%%", json.dumps(area_source or FIRMS_AREA_SOURCES[0]))
            .replace("%%AREA_MAX_SINGLE%%", json.dumps(FIRMS_AREA_MAX_DAYS))
            .replace("%%AREA_MAX_WINDOW%%", json.dumps(FIRMS_AREA_MAX_WINDOW))
            .replace("%%DEFAULT_PROVINCE%%", json.dumps(default_province or ""))
            .replace("%%META%%", json.dumps(meta, ensure_ascii=False)))
    with open(out_html, "w", encoding="utf-8") as fh:
        fh.write(html)

    # 模板占位符若漏加引号，会生成 `const X = VIIRS S-NPP...` 这类非法 JS。
    # 这里做一次 node --check 级别的自检：只查「占位符残留」与明显的未加引号赋值。
    _assert_no_placeholder_leak(html)

    # 在输出目录放一个可双击的启动器（Windows .cmd / macOS-Linux .sh）：
    # 双击即启动本地代理并打开浏览器 —— 用户不需要知道命令、也不需要开终端敲字。
    _write_launchers(map_dir, serve_py, python_exe, proxy_port)


def _round_geom(g, nd):
    """坐标按小数位压缩（省界体积可减半，精度约 11m，足够制图）。"""
    def rc(c):
        if isinstance(c[0], (int, float)):
            return [round(c[0], nd), round(c[1], nd)]
        return [rc(x) for x in c]
    return {"type": g["type"], "coordinates": rc(g["coordinates"])}


def _assert_no_placeholder_leak(html):
    """生成后自检：占位符必须全部替换，且 JS 常量赋值不得漏引号。

    这类错误**不会抛 Python 异常**，只会在浏览器里报 SyntaxError 导致整页脚本不执行
    （表现为「下拉框空、按钮全无反应」），极难靠肉眼看代码定位，故在此拦截。
    """
    import re as _re
    left = set(_re.findall(r"%%[A-Z_]+%%", html))
    if left:
        raise RuntimeError(f"HTML 中残留未替换的占位符：{sorted(left)}")
    # 形如 `const X = VIIRS S-NPP 375m (C2);` —— 值未加引号
    bad = _re.findall(r"^const [A-Z_]+ = [^'\"\[{\d\-tfn][^\n;]*;$", html, _re.M)
    bad = [b for b in bad if "AREA_SOURCE" not in b]
    if bad:
        raise RuntimeError(f"HTML 中 JS 常量赋值未加引号：{bad[:3]}")


def _write_launchers(out_dir, serve_py, python_exe, proxy_port):
    """生成双击即用的启动器脚本。失败不影响主流程（只是少个便捷入口）。

    Windows 编码要点：.cmd 必须用**系统 ANSI 代码页**（中文 Windows 为 cp936/GBK）写入，
    且**不能加 chcp 65001** —— cmd 会在读到 chcp 后用新代码页重新解析后续行，
    导致文件本身是 GBK 却按 UTF-8 解读，含中文的路径会被拆坏。
    """
    esc = lambda p: str(p).replace('"', '""')
    try:
        # Windows：.cmd（mbcs = 系统 ANSI 代码页，兼容中文路径）
        cmd = (
            "@echo off\r\n"
            "rem 启动 FIRMS 火点地图本地服务（Area API 代理）并打开浏览器\r\n"
            f'"{esc(python_exe)}" "{esc(serve_py)}" --dir "{esc(out_dir)}" '
            f"--port {proxy_port} --open\r\n"
            "if errorlevel 1 pause\r\n"
        )
        cmd_path = os.path.join(out_dir, "启动火点地图.cmd")
        try:
            with open(cmd_path, "w", encoding="mbcs", errors="replace") as fh:
                fh.write(cmd)
        except (LookupError, UnicodeError):
            # 非 Windows 或无 mbcs：退回 UTF-8，并显式切代码页
            with open(cmd_path, "w", encoding="utf-8") as fh:
                fh.write("@echo off\r\nchcp 65001 >nul\r\n" + cmd.split("\r\n", 1)[1])
        # macOS / Linux
        sh = (
            "#!/usr/bin/env bash\n"
            "# 启动 FIRMS 火点地图本地服务（Area API 代理）并打开浏览器\n"
            f'"{esc(python_exe)}" "{esc(serve_py)}" --dir "{esc(out_dir)}" '
            f"--port {proxy_port} --open\n"
        )
        p_sh = os.path.join(out_dir, "启动火点地图.sh")
        with open(p_sh, "w", encoding="utf-8") as fh:
            fh.write(sh)
        try:
            os.chmod(p_sh, 0o755)
        except OSError as e:
            # 非 Windows 上 chmod 失败会让 .sh 不可执行；这里不算致命，
            # 但要让用户知道得手动 chmod +x，而不是双击了没反应
            print(f"  [warn] .sh 启动器未加上可执行权限（请手动 chmod +x）：{e}")
    except OSError as e:
        print(f"  ! 启动器脚本生成失败（不影响使用）：{e}")


# ---------------------------------------------------------------------------
# 多省数据构建（供 HTML 界面按省切换）
# ---------------------------------------------------------------------------
def build_province_dataset(geojson_provinces, all_pts, geo_lookup=None):
    """把全球火点按省归类，并整理成前端可直接消费的紧凑结构。

    返回 (provinces, fires_by_province, total)：
      provinces      : [{name, adcode, feature}]，feature 为 GCJ-02 省界
      fires_by_province: {省名: [紧凑行, ...]}
      total          : 归入各省的火点总数

    紧凑行格式（体积约为对象式的 1/4，便于整份内嵌进 HTML）：
      [lng, lat, frp, brightness, conf_idx, date_idx, acq_time, daynight_idx]
    """
    provs = []
    for f in geojson_provinces.get("features", []):
        name = f["properties"].get("name", "")
        if not name:
            continue
        g = f["geometry"]
        wgs = gcj02_geom_to_wgs84(g)
        minx, miny, maxx, maxy = geometry_bbox(wgs)
        provs.append({
            "name": name,
            "adcode": f["properties"].get("adcode"),
            "geometry": g,                 # GCJ-02，直接对齐高德底图
            "wgs": wgs,                   # WGS-84，供 Area API bbox 用
            "bbox": [minx, miny, maxx, maxy],
        })

    # 国家级外包框 = 所有省界 bbox 的并集。
    # 必要性实测（2026-10-27 的 VIIRS 全球文件）：92,002 个全球点里只有 11.2%
    # 落在中国境内，若直接逐点遍历 35 个省的 bbox 就是 322 万次比较，其中近九成
    # 纯属浪费。先用整体外框筛一遍，绝大多数点一次比较即可淘汰。
    _BOX = None
    if provs:
        _bs = [p["bbox"] for p in provs]
        _BOX = (min(b[0] for b in _bs), min(b[1] for b in _bs),
                max(b[2] for b in _bs), max(b[3] for b in _bs))

    # 一次遍历完成归类：国家外框粗筛 → 逐省 bbox 再筛 → 精确点在多边形判定
    fires = {p["name"]: [] for p in provs}
    dates = set()
    raw = []
    for lng, lat, row in all_pts:
        if _BOX is not None and (
                lng < _BOX[0] or lng > _BOX[2] or lat < _BOX[1] or lat > _BOX[3]):
            continue
        hit = None
        for p in provs:
            minx, miny, maxx, maxy = p["bbox"]
            if lng < minx or lng > maxx or lat < miny or lat > maxy:
                continue
            if point_in_geometry(lng, lat, p["wgs"]):
                hit = p
                break
        if hit is None:
            continue
        d = row.get("acq_date")
        if d:
            dates.add(d)
        raw.append((hit["name"], lng, lat, row))

    # 守卫：若所有行都缺 acq_date，date_list 会是空表，下游 dates[idx] 会 IndexError
    date_list = sorted(dates) or [""]
    date_idx = {d: i for i, d in enumerate(date_list)}
    conf_list = ["low", "nominal", "high"]
    conf_idx = {c: i for i, c in enumerate(conf_list)}

    for name, lng, lat, row in raw:
        c = _norm_confidence(row.get("confidence"))
        fires[name].append([
            round(lng, 4), round(lat, 4),
            float(row.get("frp") or 0),
            # 必须用 _brightness()：亮度列名随产品而异（VIIRS 是 bright_ti4，
            # 直接取 row["brightness"] 会得到 None → 亮度显示 0 K
            round(float(_brightness(row) or 0), 1),
            conf_idx.get(c, 0),
            date_idx.get(row.get("acq_date"), 0),
            int(row.get("acq_time") or 0),
            0 if row.get("daynight") == "D" else 1,
        ])

    # 紧凑行里的 conf 索引用字符串数组还原太绕，改为直接把 conf_list 一起输出
    out = {name: {"rows": fires[name]} for name in fires}
    return provs, out, date_list, conf_list, len(raw)


def _write_points_csv(path, export, dates, conf_list):
    """把 (省名, 紧凑行) 导出为 CSV。utf-8-sig 带 BOM，Excel 打开中文不乱码。"""
    cols = ["longitude", "latitude", "brightness", "frp", "confidence",
            "acq_date", "acq_time", "daynight", "province"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for pname, r in export:
            w.writerow([r[0], r[1], r[3], r[2], conf_list[r[4]], dates[r[5]],
                        f"{r[6]:04d}", "D" if r[7] == 0 else "N", pname])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="中国省份火点获取与可视化（FIRMS 三种取数方式）",
        epilog="省份名可省略：省略则输出全国版地图，界面里用「行政区划」下拉切换各省。")
    ap.add_argument("province", nargs="?", default=None,
                    help="省份名，如 云南省 / 新疆 / 北京；省略则输出全国版（界面内切换省份）")
    ap.add_argument("--product", default="suomi-npp-viirs-c2",
                    choices=list(FIRMS_PRODUCTS.keys()),
                    help="FIRMS 火点产品（默认 VIIRS S-NPP 375m）")
    ap.add_argument("--output", default=None, help="输出目录，默认 ./<省份>_fire 或 ./china_fire")
    ap.add_argument("--no-html", action="store_true", help="只输出 GeoJSON，不生成 HTML")
    ap.add_argument("--boundary", default=None, help="自定义省界 GeoJSON（Feature/FeatureCollection），跳过 DataV 下载")
    ap.add_argument("--refresh-fires", action="store_true", help="忽略缓存，重新下载 FIRMS 全球文件")
    ap.add_argument("--refresh-boundary", action="store_true", help="忽略缓存，重新下载 DataV 省界")
    ap.add_argument("--mapkey", default=os.environ.get("FIRMS_MAP_KEY"),
                    help="FIRMS MAP_KEY（免费申请：mapserver/wms-info）。用于方式2 Area API 与方式3 WMS 栅格")
    ap.add_argument("--wms-layer", default=None,
                    help="WMS 图层名，默认按 --product 选（fires_viirs_snpp_24 / fires_viirs_noaa20_24 / fires_viirs_noaa21_24 / fires_modis_24）")
    # —— 方式2：Area API（按包围盒拉数据）——
    ap.add_argument("--method", default="csv", choices=["csv", "area"],
                    help="数据获取方式：csv=方式1 免 Key 直下 FIRMS 公开目录（默认）；area=方式2 Area API 按包围盒拉（需 MAP_KEY）")
    ap.add_argument("--bbox", default=None,
                    help="方式2 的包围盒 west,south,east,north（默认取省界包围盒，略放大）")
    ap.add_argument("--days", type=int, default=1,
                    help=f"方式2 回溯天数，1..{FIRMS_AREA_MAX_WINDOW}"
                         f"（单次请求实测上限 {FIRMS_AREA_MAX_DAYS} 天，"
                         f"> {FIRMS_AREA_MAX_DAYS} 天会自动拆成多段请求并去重合并）")
    ap.add_argument("--date", default=None,
                    help="方式2 历史起始日期 YYYY-MM-DD（作为 URL 路径段）")
    ap.add_argument("--area-source", default=None,
                    help="方式2 的 SOURCE，默认按 --product 选；可选：%s" % "/".join(FIRMS_AREA_SOURCES))
    ap.add_argument("--api-base", default="/api/area/csv",
                    help="HTML 内「拉取视野火点」按钮调用的路径形态，与官方接口同构："
                         "{base}/api/area/csv/{MAP_KEY}/{SOURCE}/{BBOX}/{DAYS}"
                         "（代理只换主机名；配合 scripts/serve_fires.py 使用）")
    ap.add_argument("--proxy-port", type=int, default=8765,
                    help="本地代理端口（默认 8765）。必须与 scripts/serve_fires.py 实际使用的端口一致，"
                         "否则页面探测不到代理；界面提示里给出的启动命令会带上该端口")
    ap.add_argument("--serve", action="store_true",
                    help="生成后直接启动本地代理并打开浏览器（一条命令搞定方式2 的 Area API 实时拉取，"
                         "无需再手动开终端敲命令）")
    ap.add_argument("--version", action="version", version=f"cn-fire-firms {SKILL_VERSION}")
    ap.add_argument("--cache-dir", default=None,
                    help="运行时下载缓存目录（默认取系统用户缓存目录）。FIRMS 全球 24h 文件"
                         "每日一份约 5-7MB，刻意**不**放技能安装目录，以免技能包随使用天数膨胀")
    ap.add_argument("--cache-keep", type=int, default=3,
                    help="保留最近 N 天的火点缓存（默认 3，0 表示不清理）")
    ap.add_argument("--clear-cache", action="store_true",
                    help="清空运行时缓存目录内的**全部**文件后退出（技能自带 data/ 离线数据"
                         "不受影响；下次运行按需重新下载，不影响使用）。配合 --cache-dir 可定向清理")
    args = ap.parse_args()

    global CACHE_DIR_OVERRIDE, CACHE_KEEP_DAYS
    CACHE_DIR_OVERRIDE = args.cache_dir
    CACHE_KEEP_DAYS = args.cache_keep

    # --clear-cache 是纯维护动作：清完即退出，不进入「下载→裁切→出图」主流程
    if args.clear_cache:
        clear_cache(args.cache_dir)
        return 0

    out_dir = args.output or (f"./{args.province}_fire" if args.province else "./china_fire")
    os.makedirs(out_dir, exist_ok=True)
    geojson_path = os.path.join(out_dir, "fire_points.geojson")
    html_path = os.path.join(out_dir, "fire_map.html")
    prod = FIRMS_PRODUCTS[args.product]

    # 1) 省份边界（优先复用本地缓存；--boundary 直接用自定义文件）
    print("[1/4] 获取省份边界 ...")
    if args.boundary:
        with open(args.boundary, encoding="utf-8") as fh:
            custom = json.load(fh)
        # 自定义边界可能是三种形态：FeatureCollection / Feature / 裸 geometry
        if isinstance(custom, dict) and custom.get("type") == "FeatureCollection":
            provinces = custom
        elif isinstance(custom, dict) and custom.get("type") == "Feature":
            provinces = {"type": "FeatureCollection", "features": [custom]}
        elif isinstance(custom, dict) and custom.get("type"):
            provinces = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": custom,
                 "properties": {"name": args.province or "自定义", "adcode": ""}}]}
        else:
            raise SystemExit(f"✗ 自定义边界格式不支持：{args.boundary}")
        print(f"  ✓ 自定义边界：{len(provinces['features'])} 个要素")
    else:
        try:
            provinces = load_provinces(refresh=args.refresh_boundary)
        except RuntimeError as e:
            print(f"  ✗ {e}")
            sys.exit(2)
        print(f"  ✓ 全国省界 {len(provinces['features'])} 个要素（adcode 来自 DataV）")

    # 2) 取火点数据
    #    方式1 csv ：免 Key 直下 FIRMS 公开目录 24h 全球文件（默认）
    #    方式2 area：Area API 按 --bbox 拉（需 MAP_KEY）
    if args.method == "area":
        if not args.mapkey:
            print("  ✗ 方式2（Area API）需要 MAP_KEY：请用 --mapkey 传入，或设环境变量 FIRMS_MAP_KEY")
            print("    免费申请：https://firms.modaps.eosdis.nasa.gov/api/map_key")
            sys.exit(2)
        source = args.area_source or prod.get("area_source") or FIRMS_AREA_SOURCES[0]
        bbox = args.bbox
        sel_feat_cache = None      # 同一省份只查一次（下面导出环节会复用）
        if not bbox:
            if args.province:
                sel_feat_cache = find_province(provinces, args.province)
                if not sel_feat_cache:
                    print(f"  ✗ 未找到省份「{args.province}」")
                    sys.exit(2)
                minx, miny, maxx, maxy = geometry_bbox(
                    gcj02_geom_to_wgs84(sel_feat_cache["geometry"]))
            else:
                minx, miny, maxx, maxy = 73.0, 3.0, 136.0, 54.0
            bbox = f"{minx - 0.1:.4f},{miny - 0.1:.4f},{maxx + 0.1:.4f},{maxy + 0.1:.4f}"
        # plan 算一次：下面要打印段数、fetch_area_csv_multi 又要按它发请求，
        # 让它内部再算一遍纯属重复（2026 年版两套日期切分逻辑曾各跑一次）
        plan = area_api_plan(args.days, args.date)
        print(f"[2/4] 方式2 Area API：source={source} days={args.days}"
              f"{(' 起始日=' + args.date) if args.date else ''}"
              f" 请求段数={len(plan)} bbox={bbox} ...")
        try:
            csv_text, api_urls = fetch_area_csv_multi(
                args.mapkey, bbox, source, args.days, args.date, plan=plan, verbose=True)
        except (RuntimeError, ValueError) as e:
            print(f"  ✗ {e}")
            sys.exit(2)
        print(f"  ✓ Area API 返回 {len(csv_text)} 字节（{len(api_urls)} 次请求，已去重合并）")
        for u in api_urls:
            print(f"    URL: {u.replace(args.mapkey, '<MAP_KEY>')}")
        all_pts, skipped = parse_area_csv(csv_text)
        _report_parse_skips(skipped, len(all_pts), "Area API 响应")
        src_desc = f"Area API {source}"
    else:
        print(f"[2/4] 方式1 免 Key：获取 FIRMS {prod['label']} 24h 全球文件 ...")
        try:
            csv_path = load_fires_csv(prod["url"], refresh=args.refresh_fires)
        except RuntimeError as e:
            print(f"  ✗ {e}")
            sys.exit(2)
        print(f"  ✓ 火点数据：{csv_path}")
        all_pts, skipped = parse_fires(csv_path)
        _report_parse_skips(skipped, len(all_pts), os.path.basename(csv_path))
        src_desc = f"公开目录 {prod['label']} 24h"

    # 3) 按省归类（一次遍历：bbox 粗筛 + 点在多边形精确判定）
    print("[3/4] 按省界归类火点 ...")
    prov_list, fires_by_prov, dates, conf_list, total_in = build_province_dataset(
        provinces, all_pts)
    print(f"  全球火点 {len(all_pts)} 个 → 落省 {total_in} 个 → 覆盖 {len(prov_list)} 个省级行政区")

    # 落盘的 GeoJSON / CSV：指定了省份则只出该省，否则出全国
    if args.province:
        # 方式2 分支已经查过一次，这里直接复用；重复调用 find_province 会做一遍
        # 「精确 → 去后缀 → 子串」三轮全表扫描，纯属浪费（35 个省，两轮即可感知）
        feat = sel_feat_cache or find_province(provinces, args.province)
        if not feat:
            print(f"  ✗ 未找到省份「{args.province}」")
            sys.exit(2)
        sel_name = feat["properties"]["name"]
        sel_rows = fires_by_prov.get(sel_name, {}).get("rows", [])
        export = [(sel_name, r) for r in sel_rows]
        default_province = sel_name
        print(f"  ✓ {sel_name}：{len(sel_rows)} 个火点")
    else:
        export = []
        for pname, obj in fires_by_prov.items():
            for r in obj.get("rows", []):
                export.append((pname, r))
        default_province = ""
        print(f"  ✓ 全国版：{len(export)} 个火点")
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [r[0], r[1]]},
         "properties": {
             "longitude": r[0], "latitude": r[1],
             "brightness": r[3], "frp": r[2],
             "confidence": conf_list[r[4]],
             "acq_date": dates[r[5]], "acq_time": f"{r[6]:04d}",
             "daynight": "D" if r[7] == 0 else "N",
             "province": pname}}
        for pname, r in export]}
    with open(geojson_path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False, indent=1)
    print(f"  ✓ GeoJSON：{geojson_path}")
    # CSV 同步落盘（带 BOM，Excel 直接打开不乱码）
    csv_out = os.path.join(out_dir, "fire_points.csv")
    _write_points_csv(csv_out, export, dates, conf_list)
    print(f"  ✓ CSV：{csv_out}")

    # 4) HTML
    if not args.no_html:
        print("[4/4] 生成 Leaflet + 高德卫星影像地图（三方式 Tab + 省级筛选）...")
        build_html(html_path, prod["label"], prov_list, fires_by_prov,
                   dates, conf_list, total_in,
                   mapkey=args.mapkey, wms_layer=args.wms_layer or prod.get("wms_layer"),
                   area_base=args.api_base,
                   area_source=args.area_source or prod.get("area_source"),
                   proxy_port=args.proxy_port, default_province=default_province)
        size_kb = os.path.getsize(html_path) // 1024
        print(f"  ✓ HTML：{html_path}（{size_kb} KB）")
        print("  · 顶部「行政区划」下拉切换各省（数据已内嵌，切换即时生效、无需联网）")
        print("  · 顶部三个 Tab 切换取数方式；方式2 需经本地代理（双击 启动火点地图.cmd），方式3 浏览器直连即可")
    print(f"完成（数据源：{src_desc}）。")

    # 5) 可选：直接启动本地代理并打开浏览器（一条命令搞定，无需手动敲命令）
    if args.serve:
        if args.no_html:
            print("✗ --serve 需要生成 HTML（不能与 --no-html 同用）")
            sys.exit(2)
        serve_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve_fires.py")
        print(f"\n启动本地代理（方式2 Area API 实时拉取）…")
        print(f"  端口 {args.proxy_port}；保持本窗口开着，代理随窗口关闭而退出。")
        try:
            import subprocess as _sp
            rc = _sp.call([sys.executable, serve_py, "--dir", out_dir,
                           "--file", "fire_map.html", "--port", str(args.proxy_port),
                           "--open"])
            sys.exit(rc)
        except KeyboardInterrupt:
            print("\n已退出。")
            sys.exit(0)


if __name__ == "__main__":
    main()
