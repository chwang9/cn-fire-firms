# 国内省份遥感火点获取与可视化（FIRMS）

[![版本](https://img.shields.io/badge/版本-v2.2.2-blue)](./SKILL.md)

从 NASA FIRMS 获取中国火点数据，**省份可选**——不输入省份默认出全国版地图（34 个省级行政区
数据内嵌、页面内即点即切），输入省份则聚焦该省。输出 **WGS-84 火点 GeoJSON + CSV**，并生成一个
**自包含 HTML 地图**（Leaflet + 高德卫星影像）。三种取数方式在一个页面里用 Tab 切换，
支持省级行政区筛选与时间范围回溯（1–10 天）。

> **零注册、纯标准库**：方式1 完全不需要 API key；方式2/3 才需要免费 MAP_KEY，且可只在网页界面里填。

---

## 快速开始

```bash
# 方式1（默认）：免 Key，出全国版地图，页面内可切换 34 个省级行政区
python scripts/fire_firms_cn.py

# 方式1：只看某个省
python scripts/fire_firms_cn.py 云南省

# 一条命令：生成地图 + 启动本地代理 + 自动开浏览器（方式2 实时拉取）
python scripts/fire_firms_cn.py 云南省 --serve

# 方式2：Area API 回溯最近 10 天（>5 天会自动拆成多段请求并去重合并）
python scripts/fire_firms_cn.py 云南省 --method area --mapkey <你的KEY> --days 10

# 方式2：指定历史起始日
python scripts/fire_firms_cn.py 云南省 --method area --mapkey <你的KEY> \
    --days 5 --date 2026-10-20

# 查看版本 / 全部参数
python scripts/fire_firms_cn.py --version
python scripts/fire_firms_cn.py --help
```

打开输出目录里的 `fire_map.html` 即可（也可双击 `启动火点地图.cmd` / `启动火点地图.sh`）。

---

## 三种取数方式

| 方式 | 手段 | 需要 MAP_KEY | 时间范围 | 说明 |
|---|---|---|---|---|
| **方式1 CSV** | FIRMS 公开目录 24h 全球文件 | **不需要** | 最近 24h | 数据离线裁切到各省并**内嵌进 HTML**，切省即时生效、零联网。带**火点列表**（FRP/时间排序、点击跳点），支持 CSV / GeoJSON 下载 |
| **方式2 Area API** | 按包围盒实时请求 | 需要 | **1–10 天**（可指定历史起始日） | 可**自己在地图上画**矩形/多边形作为关注区（也可用「视野 ∩ 所选省」），新点以青色描边叠加。带**火点列表**，支持 CSV / GeoJSON 下载 |
| **方式3 WMS** | 官方栅格瓦片叠加 | 需要 | 由图层决定（24h/48h） | 瓦片走 `<img>`，**浏览器直连即可，不需要本地代理**；QGIS 里同样可直接加此服务 |

> **三个方式的火点图层互斥**：切 Tab 只显示当前方式的点。方式2 未拉取、方式3 是服务端栅格时，
> 图上就不会有方式1 的残留点（v0.8.0 修复了「切过去后方式1 的几千个点还在」的问题）。

方式2 的接口形态（与官方文档逐字一致）：

```
https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/{SOURCE}/{BBOX}/{DAYS}[/{DATE}]
```

实测约束（2026-10-26 用真实 key 验证，非文档转述）：

- `DAYS` **单次请求上限 5**（填 6 会被拒：`Invalid day range. Expects [1..5].`）
- 本技能支持 **1–10 天**：超过 5 天会自动拆成多段请求再合并去重
  （例如 10 天 → `5/2026-10-17` + `5/2026-10-22`）
- `DATE` 是**路径段**、格式 `YYYY-MM-DD`，语义为 **起始日**：窗口 = `[DATE, DATE+DAYS-1]`
- 响应**不带 `Access-Control-Allow-Origin`** → 浏览器直连必被 CORS 拦，界面端需经本地代理

---

## 参数速查

| 参数 | 说明 |
|---|---|
| `province` | 省份名（位置参数，可省略 → 输出全国版） |
| `--product` | FIRMS 产品：`suomi-npp-viirs-c2`（默认 375m）/ `noaa-20-viirs-c2` / `viirs-noaa-21` / `modis-c6.1`（1km） |
| `--method` | `csv`（默认，方式1）/ `area`（方式2） |
| `--mapkey` | FIRMS 免费 MAP_KEY（也可只填在网页界面里） |
| `--days` | 方式2 回溯天数 **1..10**（>5 自动拆段） |
| `--date` | 方式2 历史起始日 `YYYY-MM-DD` |
| `--bbox` | 方式2 包围盒 `west,south,east,north`（默认取省界包围盒） |
| `--area-source` | 方式2 数据源，9 种：`VIIRS_SNPP_NRT` / `VIIRS_NOAA20_NRT` / `VIIRS_NOAA21_NRT` / `*_SP` / `MODIS_NRT` / `MODIS_SP` / `LANDSAT_NRT` |
| `--wms-layer` | 方式3 WMS 图层名（默认按 `--product` 选） |
| `--output` | 输出目录 |
| `--no-html` | 只出数据，不生成 HTML |
| `--boundary` | 自定义省界 GeoJSON（Feature / FeatureCollection 均可） |
| `--refresh-fires` / `--refresh-boundary` | 忽略缓存重新下载 |
| `--cache-dir` | 运行时缓存目录（默认系统用户缓存目录，**不写技能包**） |
| `--cache-keep` | 保留最近 N 天缓存（默认 3，`0` = 不清理） |
| `--clear-cache` | **一键清空运行时缓存目录后退出**（技能自带 `data/` 离线数据不受影响，下次运行按需重下，不影响使用；可配 `--cache-dir` 定向清理） |
| `--api-base` | 界面「拉取视野火点」的请求路径形态（默认 `/api/area/csv`） |
| `--proxy-port` | 本地代理端口（默认 8765） |
| `--serve` | 生成后直接启动代理并打开浏览器 |
| `--version` | 打印版本号 |

完整说明见 [SKILL.md](./SKILL.md)。

---

## 输出产物

| 文件 | 说明 |
|---|---|
| `fire_map.html` | 自包含地图（内联 Leaflet，无 CDN 依赖），含省级筛选 + 三方式 Tab + 火点列表 + 自由绘制 |
| `fire_points.geojson` | 火点要素集，**WGS-84**，可直接拖进 QGIS |
| `fire_points.csv` | 同数据 CSV（**带 BOM**，Excel 直接打开不乱码） |
| `启动火点地图.cmd` / `.sh` | 双击即启动本地代理并打开浏览器 |

**数据下载**（界面内）：方式1 可按当前「行政区划」范围导出，方式2 可导出本次拉取到的点，
两种方式都支持 **CSV / GeoJSON** 两种格式，文件名形如
`firms_VIIRS_S-NPP_375m_云南省_20261026_2340.csv`。

---

## 离线自检

```bash
python scripts/selftest.py
```

全程不联网（用内置海南真实边界 + 合成火点），覆盖 24 项：
裁切 → GeoJSON → 自包含 HTML → JS 语法 → 三方式接线 → 布局实测（真实浏览器量
`getBoundingClientRect()` 两两求交集）→ Area API 端到端（真实起代理 → 拉取 → 渲染）
→ 数据下载端到端 → **双端一致性**（Python `area_api_plan()` ⟷ 页面里的
`planAreaRequests()`，12 组输入输出在真实浏览器里对拍）→ **图层隔离 / 火点列表 / 自由绘制
端到端**（临时生成 150 个火点的夹具，35 项断言在真实浏览器里跑）→ **下载健壮性**
（CSV 完整 vs 60% 截断 / 缓存命中拦截 / 坏行计数 / JSON 截断 / 临时目录回收）。

另含三个**守卫**：

- **版本一致性守卫**：`SKILL.md` frontmatter / `_user_meta.json` / `README.md` / 脚本常量四处版本号必须对拍
- **文档同步守卫**：`--help` 实际输出与 `SKILL.md` 参数表**双向**校验，缺项或幽灵项直接失败
- **双端一致性守卫**：Python 与内联 JS 的 Area API 拆段计划必须对拍。这两份实现同规则，任何一侧
  被单独修改都不会报错、只会静默算出不同的请求计划（v0.7.0 就借此抓到了本地/UTC 日期基准差一天）

**下载健壮性**这一组针对的是最难发现的一类问题——「部分成功」：下载到一半被掐会留下一份半截
CSV，它表头完好、体积达标，会被下一次的缓存命中当成有效数据，于是**火点少一半却不报任何错**。
防守分三层：原子写（`<dest>.part` → 校验通过才 replace）、尾部完整性判据（末行必须有换行、列数
须与表头一致）、缓存命中前同样校验（失败则改名 `.bad` 存档并重下）。这些守卫本身也都做了敏感性
验证——例如手动把临时目录的清理逻辑换成 no-op 后，守卫必须立刻报出泄漏，两头都绿的断言等于没有。

单独跑布局检测与端到端：

```bash
node scripts/check_layout.cjs <fire_map.html>
node scripts/check_area_e2e.cjs <fire_map.html>
node scripts/check_layers_e2e.cjs <fire_map.html>    # 图层互斥 / 列表 / 绘制
```

---

## 常见问题

**Q：界面点「拉取视野火点」提示「未找到本地代理服务」？**
FIRMS Area API 不带 CORS 头，浏览器不能直连。三种起法任选其一：
① 双击输出目录里的 `启动火点地图.cmd`；② 主脚本加 `--serve`；
③ `python scripts/serve_fires.py --dir <输出目录> --open`。
代理只监听 `127.0.0.1`，MAP_KEY 只在请求时转发，不落盘、不写日志。

**Q：为什么方式2 拉取很慢？**
Area API 对大包围盒响应极慢（实测全国视野约 123s，一省 1–2s）。本技能已把请求范围
收敛为「**当前视野 ∩ 所选省**」，建议先在顶部选好省份再拉。

**Q：方式2 显示「该范围内无火点」，是坏了吗？**
大概率不是。VIIRS NRT 的当日过境常晚于当前时刻，实测同一包围盒 `天数=1` 返回 0 条、
`天数=3` 返回 119 条 —— 把「天数」调到 3–5 天再拉通常就有。这条提示是**成功但空**的
中性状态（不会显示成红色报错）；界面也会直接给出这个建议。

**Q：Suomi NPP 还能用多久？**
FIRMS 官方公告该产品将于 **2026-11-02** 停止交付，届时请用
`--product viirs-noaa-21` 或 `noaa-20-viirs-c2`。

---

## 数据字段

| 字段 | 含义 |
|---|---|
| `latitude` / `longitude` | 纬度 / 经度（WGS-84） |
| `brightness` | 亮度温度（K）。VIIRS C2 取 `bright_ti4`，MODIS 取 `brightness` |
| `frp` | 火辐射功率 FRP（MW） |
| `confidence` | 置信度 `low` / `nominal` / `high`（Area API 的单字母 `l/n/h` 已归一） |
| `acq_date` / `acq_time` | 采集日期 / 时间（UTC，已补齐 4 位） |
| `daynight` | 昼(D) / 夜(N) |
| `satellite` | 卫星标识 |
| `province` | 所属省级行政区 |

---

## 许可与用途

FIRMS 数据用于科研/业务用途，商用请遵循 NASA 使用条款。
本技能仅依赖 Python 标准库（`csv` / `json` / `urllib` / `subprocess` / `http.server`），无需 `pip install`。
