"""Tools. Each returns a ToolResult with explicit status, sources and confidence.

Real data sources (all public):
  - US Census Geocoder (TIGER street-range interpolation)
  - Nominatim (OSM) as geocode fallback
  - City of Ithaca ArcGIS Online: Municipal_Boundary, Tax_Parcels, City_Zoning, Historic_Districts
  - FEMA NFHL MapServer (effective flood zones) + Esri Living Atlas mirror
  - TCAT GTFS static feed
  - City of Ithaca "Zoning Charts – April 2026" PDF (+ zoning-rewrite RFP for currency)
"""
from __future__ import annotations

import csv
import math
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .core import Trace, Span, http_get, ToolHTTPError

DATA = Path(__file__).resolve().parent.parent / "data"
COI = "https://services5.arcgis.com/R1JbITZvSQHJsl5r/arcgis/rest/services"
FEMA_NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
ESRI_FLOOD = "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0"
CHART_URL = "https://www.cityofithacany.gov/DocumentCenter/View/20523/COI-Zoning-Charts_0426?bidId="
RFP_URL = "https://www.cityofithacany.gov/DocumentCenter/View/20155/Zoning-Rewrite-RFP_Final?bidId=223"
TOMPKINS_FLOOD_URL = "https://www.tompkinscountyny.gov/Government-Resources/Flood-Information"
GTFS_URL = "https://s3.amazonaws.com/tcat-gtfs/tcat-ny-us.zip"
OVERLAYS = {"SLAUOZ": "街道层活跃用途叠加区 (§325-8(D)(3))", "SHOD": "South Hill 叠加区 (§325-8(E))",
            "Adult Us": "成人用途叠加区 (§325-29.1)"}
TODAY = date(2026, 10, 27)
_BREAKER: dict = {}


@dataclass
class ToolResult:
    status: str                      # ok | empty | error | degraded | unverified
    data: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    confidence: float = 1.0
    note: str = ""
    failure_modes: list = field(default_factory=list)


def _src(sid, title, url, as_of=None):
    return {"id": sid, "title": title, "url": url,
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "data_as_of": as_of}


def _arc_point(trace, span, layer_url, lon, lat, fields="*", distance=None, geom=False, fault_key=None, timeout=15):
    p = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
         "spatialRel": "esriSpatialRelIntersects", "outFields": fields, "returnGeometry": str(geom).lower(),
         "outSR": 4326, "f": "json"}
    if distance:
        p.update(distance=distance, units="esriSRUnit_Meter")
    d = http_get(trace, span, layer_url + "/query", p, timeout=timeout, fault_key=fault_key)
    return d.get("features", [])


def _layer_edit_date(trace, span, layer_url):
    try:
        d = http_get(trace, span, layer_url, {"f": "json"}, timeout=10)
        ms = (d.get("editingInfo") or {}).get("dataLastEditDate")
        return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat() if ms else None
    except ToolHTTPError:
        return None


# ------------------------------------------------------------------ geocode
def geocode(trace: Trace, span: Span, address: str) -> ToolResult:
    srcs = []
    try:
        d = http_get(trace, span, "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                     {"address": address, "benchmark": "Public_AR_Current", "format": "json"}, fault_key="geocode")
        m = d.get("result", {}).get("addressMatches", [])
        srcs.append(_src("census_geocoder", "US Census Geocoder (Public_AR_Current)",
                         "https://geocoding.geo.census.gov/geocoder/"))
        if len(m) > 1:
            return ToolResult("degraded", {"candidates": [x["matchedAddress"] for x in m][:5]}, srcs, 0.3,
                              "多个候选地址，需要用户确认", ["FM-19"])
        if m:
            c = m[0]["coordinates"]
            return ToolResult("ok", {"lon": c["x"], "lat": c["y"], "matched": m[0]["matchedAddress"],
                                     "precision": "street_range_interpolated", "provider": "census"}, srcs, 0.7,
                              "人口普查地理编码按门牌区间在街道中心线上插值，点位可能落在道路上", [])
    except ToolHTTPError as e:
        span.warnings.append(f"census geocoder failed: {e.kind}")
    # fallback: Nominatim
    try:
        d = http_get(trace, span, "https://nominatim.openstreetmap.org/search",
                     {"q": address, "format": "json", "limit": 3, "countrycodes": "us"}, timeout=8, retries=0,
                     fault_key="geocode_fallback")
        srcs.append(_src("nominatim", "OpenStreetMap Nominatim", "https://nominatim.openstreetmap.org/"))
        if d:
            return ToolResult("ok", {"lon": float(d[0]["lon"]), "lat": float(d[0]["lat"]), "matched": d[0]["display_name"],
                                     "precision": "osm", "provider": "nominatim"}, srcs, 0.6, "备用地理编码", ["FM-02"])
    except ToolHTTPError as e:
        span.warnings.append(f"nominatim fallback failed: {e.kind}")
        return ToolResult("empty", {}, srcs, 0.0, f"两个地理编码器都没有结果（备用源：{e.kind}）", ["FM-09", "FM-03"])
    return ToolResult("empty", {}, srcs, 0.0, "两个地理编码器都没有匹配结果", ["FM-09"])


# ------------------------------------------------------------------ jurisdiction
def jurisdiction(trace, span, lon, lat) -> ToolResult:
    f = _arc_point(trace, span, f"{COI}/Municipal_Boundary/FeatureServer/0", lon, lat, fields="OBJECTID")
    s = [_src("coi_boundary", "City of Ithaca – Municipal Boundary (ArcGIS)", f"{COI}/Municipal_Boundary/FeatureServer")]
    if f:
        return ToolResult("ok", {"in_city": True}, s, 0.95)
    return ToolResult("ok", {"in_city": False}, s, 0.9,
                      "点位不在伊萨卡市（City）边界内——可能属于 Town of Ithaca 等其他辖区，分区规则完全不同", ["FM-10"])


# ------------------------------------------------------------------ parcel snap
def _house_no(addr: str):
    m = re.match(r"\s*(\d+)", addr or "")
    return int(m.group(1)) if m else None


def _addr_match(target: str, cand: str) -> bool:
    n = _house_no(target)
    if n is None or not cand:
        return False
    rng = re.match(r"\s*(\d+)(?:-(\d+))?", cand)
    if not rng:
        return False
    lo = int(rng.group(1)); hi = int(rng.group(2)) if rng.group(2) else lo
    if rng.group(2) and hi < lo:              # "105-07" style
        hi = int(str(lo)[:-len(rng.group(2))] + rng.group(2))
    street_t = re.sub(r"[^a-z]", "", target.lower().split(",")[0].split(" ", 1)[-1])[:5]
    street_c = re.sub(r"[^a-z]", "", cand.lower())
    toks = [t for t in re.findall(r"[a-z]+", target.lower().split(",")[0]) if t not in ("e", "w", "n", "s", "st", "rd", "ave")]
    if rng.group(2) and (lo % 2) == (hi % 2) and (n % 2) != (lo % 2):
        return False  # US address ranges are single-sided: odd and even numbers sit on opposite sides
    return lo <= n <= hi and any(t[:5] in street_c for t in toks) if toks else lo <= n <= hi


def _centroid(geom):
    ring = (geom or {}).get("rings", [[]])[0]
    if not ring:
        return None
    xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def snap_parcel(trace, span, lon, lat, address) -> ToolResult:
    layer = f"{COI}/Tax_Parcels/FeatureServer/0"
    as_of = _layer_edit_date(trace, span, layer)
    s = [_src("coi_parcels", "City of Ithaca – Tax Parcels (ArcGIS)", layer.replace("/0", ""), as_of)]
    fields = "PRINTKEY,LOCADDR,PrimaryZone,SecZones,OverlayZone,CALCACRES,Shape__Area,PROPCLASS,DESCRIPTIO,FZONE,FIRMPANEL,HISTPROP,ILPCREVIEW"
    for radius in (0, 15, 35):
        fs = _arc_point(trace, span, layer, lon, lat, fields=fields, distance=radius or None, geom=True, fault_key="parcel")
        if not fs:
            continue
        exact = [f for f in fs if _addr_match(address, f["attributes"].get("LOCADDR", ""))]
        pick = exact[0] if exact else None
        if pick:
            a = pick["attributes"]; c = _centroid(pick.get("geometry"))
            fm = [] if radius == 0 else ["FM-08"]
            note = "点位直接落在宗地内" if radius == 0 else f"原始点位落在道路上，已在 {radius} 米半径内按门牌号吸附到宗地"
            return ToolResult("ok", {"parcel": a, "centroid": c, "snap_radius_m": radius,
                                     "lot_area_sqft": round(a["Shape__Area"] * 10.7639) if a.get("Shape__Area") else None},
                              s, 0.95 if radius == 0 else 0.85, note, fm)
        if radius == 35:
            cands = [f["attributes"].get("LOCADDR") for f in fs][:6]
            return ToolResult("degraded", {"candidates": cands}, s, 0.3,
                              f"35 米内有 {len(fs)} 个宗地，但没有一个门牌号与输入匹配；拒绝随便选一个", ["FM-08", "FM-21"])
    return ToolResult("empty", {}, s, 0.0, "35 米内没有任何宗地", ["FM-08"])


_DIRS = {"e": "E", "w": "W", "n": "N", "s": "S", "east": "E", "west": "W", "north": "N", "south": "S"}
_SUF = {"street": "ST", "st": "ST", "road": "RD", "rd": "RD", "avenue": "AVE", "ave": "AVE", "place": "PL", "pl": "PL",
        "drive": "DR", "dr": "DR", "lane": "LN", "ln": "LN", "court": "CT", "ct": "CT"}


def _assessor_street(address):
    """'130 E Clinton St, Ithaca' -> (130, 'CLINTON ST E') — assessor rolls put the direction last."""
    head = address.split(",")[0].strip().split()
    if not head or not head[0].isdigit():
        return None, None
    n, rest = int(head[0]), head[1:]
    d = _DIRS.get(rest[0].lower()) if rest else None
    if d:
        rest = rest[1:]
    if rest and rest[-1].lower().strip(".") in _SUF:
        rest = rest[:-1] + [_SUF[rest[-1].lower().strip(".")]]
    street = " ".join(w.upper() for w in rest) + (f" {d}" if d else "")
    return n, street


def parcel_by_address(trace, span, address, geo_lon, geo_lat) -> ToolResult:
    """Fallback when spatial snap fails: look the address up in the assessor attribute table."""
    layer = f"{COI}/Tax_Parcels/FeatureServer/0"
    s = [_src("coi_parcels", "City of Ithaca – Tax Parcels (ArcGIS) · 属性反查", layer.replace("/0", ""))]
    n, street = _assessor_street(address)
    if not street:
        return ToolResult("empty", {}, s, 0.0, "地址无法解析出门牌号与街道名", ["FM-09"])
    fields = "PRINTKEY,LOCADDR,PrimaryZone,SecZones,OverlayZone,CALCACRES,Shape__Area,PROPCLASS,DESCRIPTIO,FZONE,FIRMPANEL,HISTPROP,ILPCREVIEW"
    d = http_get(trace, span, layer + "/query", {"where": f"LOCADDR LIKE '%{street.replace(chr(39), '')}'", "outFields": fields,
                                                  "returnGeometry": "true", "outSR": 4326, "f": "json"}, fault_key="parcel")
    fs = [f for f in d.get("features", []) if _addr_match(address, f["attributes"].get("LOCADDR", ""))]
    if not fs:
        return ToolResult("empty", {"street_query": street}, s, 0.0, f"宗地表中没有 {n} {street}", ["FM-09"])
    a = fs[0]["attributes"]; c = _centroid(fs[0].get("geometry"))
    off = round(_hav(geo_lat, geo_lon, c[1], c[0])) if c else None
    fm = ["FM-08"] + (["FM-21"] if off and off > 60 else [])
    return ToolResult("ok", {"parcel": a, "centroid": c, "snap_radius_m": None, "geocode_offset_m": off,
                             "lot_area_sqft": round(a["Shape__Area"] * 10.7639) if a.get("Shape__Area") else None},
                      s, 0.85, f"空间吸附失败，改用宗地属性表按地址反查成功；地理编码点距宗地中心 {off} 米"
                      + ("（偏差过大：若直接用原始点位，下游所有空间查询都会错）" if off and off > 60 else ""), fm)


# ------------------------------------------------------------------ zoning
def zoning(trace, span, lon, lat, parcel_attrs: dict | None) -> ToolResult:
    layer = f"{COI}/City_Zoning/FeatureServer/0"
    as_of = _layer_edit_date(trace, span, layer)
    s = [_src("coi_zoning", "City of Ithaca – City Zoning layer (ArcGIS)", layer.replace("/0", ""), as_of)]
    fs = _arc_point(trace, span, layer, lon, lat, fields="ZONE", fault_key="zoning")
    zones = [f["attributes"]["ZONE"] for f in fs]
    base = [z for z in zones if z not in OVERLAYS]
    over = [z for z in zones if z in OVERLAYS]
    fm, notes, conf = [], [], 0.9
    p_zone = None
    if parcel_attrs:
        p_zone = [z for z in [parcel_attrs.get("PrimaryZone")] + (parcel_attrs.get("SecZones") or "").split(";") if z]
    if not zones:
        return ToolResult("empty", {"base_zones": [], "overlays": [], "overlay_desc": [], "layer_zones": [], "parcel_zone_attr": p_zone}, s, 0.0, "分区图层在该点没有要素（可能是道路/水面）", ["FM-01", "FM-08"])
    if over and not base:
        fm.append("FM-11"); conf = 0.4
        notes.append(f"该点只命中叠加区 {over}，基础分区缺失——叠加区不能代替基础分区")
        if p_zone:
            base = p_zone; notes.append(f"改用宗地属性中的分区 {p_zone} 作为基础分区"); conf = 0.7
    if p_zone and base and set(p_zone) != set(base):
        fm.append("FM-12"); conf = min(conf, 0.6)
        notes.append(f"跨源不一致：分区图层={base}，宗地属性={p_zone}")
    if p_zone and len(p_zone) > 1:
        base = list(dict.fromkeys(base + p_zone))   # evaluate every part of a split-zoned parcel
        fm.append("FM-12"); conf = min(conf, 0.65)
        notes.append(f"宗地跨越多个分区 {p_zone}（split-zoned），各部分适用不同规则")
    if as_of and (TODAY - date.fromisoformat(as_of)).days > 365:
        fm.append("FM-14"); conf = min(conf, 0.8)
        notes.append(f"分区图层最后编辑于 {as_of}，晚于此后的分区修订可能未反映")
    status = "ok" if not fm else "degraded"
    return ToolResult(status, {"base_zones": base, "overlays": over, "overlay_desc": [OVERLAYS[o] for o in over],
                               "layer_zones": zones, "parcel_zone_attr": p_zone}, s, conf, "；".join(notes), fm)


def historic(trace, span, lon, lat) -> ToolResult:
    layer = f"{COI}/Historic_Districts/FeatureServer/0"
    s = [_src("coi_historic", "City of Ithaca – Historic Districts (ArcGIS)", layer.replace("/0", ""))]
    fs = _arc_point(trace, span, layer, lon, lat, fields="DISTRICT,DESIGNATION", fault_key="historic")
    if fs:
        return ToolResult("ok", {"districts": [f["attributes"] for f in fs]}, s, 0.9,
                          "位于历史街区：外立面/新建需经 ILPC（地标保护委员会）审查")
    return ToolResult("ok", {"districts": []}, s, 0.75, "该点未命中历史街区图层（不排除单体历史建筑，见宗地 HISTPROP 字段）")


# ------------------------------------------------------------------ flood (the showcase fallback chain)
def flood(trace, span, lon, lat, parcel_attrs: dict | None) -> ToolResult:
    srcs, fm, chain = [], [], []
    # 1) FEMA NFHL authoritative — behind a process-wide circuit breaker
    try:
        if _BREAKER.get("fema_open_until", 0) > time.time() and not trace.faults.get("flood"):
            raise ToolHTTPError("circuit_open", f"熔断中：FEMA 最近连续失败 {_BREAKER.get('fema_fails')} 次，跳过直至冷却", False)
        try:
            fs = _arc_point(trace, span, FEMA_NFHL, lon, lat, fields="FLD_ZONE,ZONE_SUBTY,SFHA_TF", fault_key="flood", timeout=12)
            _BREAKER["fema_fails"] = 0
        except ToolHTTPError as e:
            if e.kind != "circuit_open":
                _BREAKER["fema_fails"] = _BREAKER.get("fema_fails", 0) + 1
                if _BREAKER["fema_fails"] >= 2:
                    _BREAKER["fema_open_until"] = time.time() + 300
            raise
        srcs.append(_src("fema_nfhl", "FEMA National Flood Hazard Layer (effective)", FEMA_NFHL.rsplit("/", 1)[0]))
        chain.append("FEMA NFHL: 成功")
        if fs:
            a = fs[0]["attributes"]
            sub = a.get("ZONE_SUBTY") or ""
            note, fmx, conf = f"FEMA 有效洪水分区 {a.get('FLD_ZONE')} {sub}".strip(), [], 0.95
            if parcel_attrs and parcel_attrs.get("FZONE") == "YES" and a.get("SFHA_TF") == "F":
                note += f"；与宗地旧字段 FZONE=YES（旧 FIRM 面板 {parcel_attrs.get('FIRMPANEL')}）冲突——以 2025 年生效的 FEMA 新图为准，但贷款方可能仍按旧记录要求保险"
                fmx = ["FM-12", "FM-14"]; conf = 0.85
            return ToolResult("ok", {"zone": a.get("FLD_ZONE"), "subtype": sub, "sfha": a.get("SFHA_TF"), "chain": chain,
                                     "legacy_parcel_flag": parcel_attrs.get("FZONE") if parcel_attrs else None}, srcs, conf, note, fmx)
        # empty from the authoritative source still needs a coverage check
        chain.append("FEMA NFHL: 该点无多边形")
    except ToolHTTPError as e:
        chain.append(f"FEMA NFHL: {e.kind}"); fm.append("FM-02")
    # 2) Esri mirror
    try:
        fs = _arc_point(trace, span, ESRI_FLOOD, lon, lat, fields="*", fault_key="flood_mirror")
        srcs.append(_src("esri_flood", "Esri Living Atlas – USA Flood Hazard Areas (mirror)", ESRI_FLOOD.rsplit("/", 1)[0]))
        if fs:
            a = fs[0]["attributes"]
            chain.append("Esri 镜像: 命中")
            return ToolResult("degraded", {"zone": a.get("FLD_ZONE") or a.get("esri_symbology"), "chain": chain}, srcs, 0.7,
                              "来自二手镜像，非 FEMA 官方接口", fm + ["FM-04"])
        chain.append("Esri 镜像: 该点为空 → 做覆盖度探测")
        # 3) coverage probe: is the mirror empty everywhere around here?
        d = http_get(trace, span, ESRI_FLOOD + "/query",
                     {"geometry": f"{lon-0.06},{lat-0.04},{lon+0.06},{lat+0.04}", "geometryType": "esriGeometryEnvelope",
                      "inSR": 4326, "where": "1=1", "returnCountOnly": "true", "f": "json"}, timeout=15)
        n = d.get("count", 0)
        chain.append(f"覆盖度探测: 周边约 10km×9km 内要素数 = {n}")
        if n == 0:
            fm += ["FM-01", "FM-04"]
    except ToolHTTPError as e:
        chain.append(f"Esri 镜像: {e.kind}"); fm.append("FM-02")
    # 4) parcel attribute (legacy FIRM panel)
    legacy = None
    if parcel_attrs and parcel_attrs.get("FZONE"):
        legacy = {"FZONE": parcel_attrs.get("FZONE"), "FIRMPANEL": parcel_attrs.get("FIRMPANEL")}
        chain.append(f"宗地属性 FZONE={legacy['FZONE']}（引用旧版 FIRM 面板 {legacy['FIRMPANEL']}）")
        fm.append("FM-14")
    srcs.append(_src("tompkins_flood", "Tompkins County – Flood Information（新版 FIRM 于 2025-06-18 生效）", TOMPKINS_FLOOD_URL))
    return ToolResult("unverified", {"zone": None, "legacy_parcel_flag": legacy, "chain": chain}, srcs, 0.2,
                      "无法从权威源确认洪水分区。空结果≠不在洪水区：镜像在伊萨卡无覆盖，宗地字段基于 1980 年代旧图。"
                      "需在 FEMA FIRM Viewer 人工核对。", sorted(set(fm)))


# ------------------------------------------------------------------ transit (local GTFS)
_GTFS: dict[str, Any] = {}


def _load_gtfs():
    if "feed" in _GTFS:
        return _GTFS
    g = DATA / "tcat"
    rd = lambda n: list(csv.DictReader(open(g / n, encoding="utf-8-sig")))
    _GTFS["stops"] = rd("stops.txt")
    _GTFS["feed"] = rd("feed_info.txt")[0]
    trips = {t["trip_id"]: t for t in rd("trips.txt")}
    cal = {c["service_id"]: c for c in rd("calendar.txt")}
    per_stop = defaultdict(lambda: {"routes": set(), "weekday_trips": 0})
    for st in csv.DictReader(open(g / "stop_times.txt", encoding="utf-8-sig")):
        t = trips.get(st["trip_id"])
        if not t:
            continue
        c = cal.get(t["service_id"], {})
        ps = per_stop[st["stop_id"]]
        ps["routes"].add(t["route_id"])
        if c.get("wednesday") == "1":
            ps["weekday_trips"] += 1
    _GTFS["per_stop"] = per_stop
    return _GTFS


def _hav(lat1, lon1, lat2, lon2):
    r = 6371000; p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1; dl = math.radians(lon2 - lon1)
    return 2 * r * math.asin(math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))


def transit(trace, span, lon, lat) -> ToolResult:
    g = _load_gtfs()
    fe = g["feed"]["feed_end_date"]
    s = [_src("tcat_gtfs", f"TCAT GTFS（有效期 {g['feed']['feed_start_date']}–{fe}）", GTFS_URL, g["feed"]["feed_version"])]
    fm, conf, note = [], 0.85, "步行距离 = 直线距离 × 1.3 绕行系数（近似，非路网计算）"
    end = datetime.strptime(fe, "%Y%m%d").date()
    if end < TODAY:
        fm.append("FM-22"); conf = 0.3; note += "；GTFS 已过期"
    elif (end - TODAY).days < 60:
        note += f"；GTFS 将于 {end} 到期，需要刷新"
    if trace.faults.get("transit") == "stale":
        fm.append("FM-22"); conf = 0.3; note += "；[注入] GTFS 已过期"
    near = []
    for st in g["stops"]:
        d = _hav(lat, lon, float(st["stop_lat"]), float(st["stop_lon"])) * 1.3
        if d <= 800:
            ps = g["per_stop"].get(st["stop_id"], {"routes": set(), "weekday_trips": 0})
            near.append({"name": st["stop_name"], "walk_m": round(d), "routes": sorted(ps["routes"]),
                         "weekday_trips": ps["weekday_trips"]})
    near.sort(key=lambda x: x["walk_m"])
    in400 = [x for x in near if x["walk_m"] <= 400]
    routes400 = sorted({r for x in in400 for r in x["routes"]})
    trips400 = sum(x["weekday_trips"] for x in in400)
    if not near:
        return ToolResult("empty", {"stops_800m": 0}, s, conf, "800 米内无公交站——这是真实的“无”，因为 GTFS 是全量数据", fm)
    return ToolResult("ok" if not fm else "degraded",
                      {"nearest": near[:5], "stops_400m": len(in400), "routes_400m": routes400,
                       "weekday_stop_events_400m": trips400, "stops_800m": len(near)}, s, conf, note, fm)


# ------------------------------------------------------------------ planning documents
PAGE_SPECS = {
    # page: (kind, header columns in order)
    4: ("use", ["B-1A", "B-1B", "B-2A", "B-2C", "B-2D", "B-4", "B-5", "CBD"]),
    5: ("bulk", ["B-1A", "B-1B", "B-2A", "B-2D", "B-4", "B-5", "CBD"]),
    12: ("use", ["CR-1", "CR-2", "CR-3", "CR-4", "MU-1", "MU-2"]),
    13: ("bulk", ["CR-1", "CR-2", "CR-3", "CR-4", "MU-1", "MU-2"]),
}
USE_ROWS = {"restaurant": r"Restaurant, fast food", "retail": r"Retail store or service", "office": r"Business or professional office",
            "multifamily": r"Multiple family dwelling", "hotel": r"Hotel"}
BULK_ROWS = {"max_stories": r"Number of stories", "max_height_ft": r"Height in feet", "max_lot_coverage": r"Percent of lot area"}


_PDF: dict[int, str] = {}   # v2 bug: this cache used to share the _GTFS dict, so `if _GTFS: return` skipped loading the feed


def _page_text(page):
    key = page
    if key not in _PDF:
        _PDF[key] = subprocess.run(["pdftotext", "-layout", "-f", str(page), "-l", str(page),
                                     str(DATA / "docs/zoning_charts_2026-04.pdf"), "-"],
                                    capture_output=True, text=True).stdout
    return _PDF[key]


def _col_for(zone):
    z = zone.upper()
    if z.startswith("CBD"):
        return "CBD"
    return z


def _row_cells(text, label_re):
    for line in text.splitlines():
        if re.search(label_re, line, re.I):
            rest = line[re.search(label_re, line, re.I).end():]
            parts = re.split(r"\s{2,}", rest)
            cells = [c.strip() for c in parts[1:] if c.strip()]   # parts[0] = rest of label (+footnote)
            return line, cells
    return None, None


def zoning_rules(trace, span, zone: str, uses: list[str]) -> ToolResult:
    col = _col_for(zone)
    src = [_src("chart_2026", "City of Ithaca Zoning Charts (April 2026)", CHART_URL, "2026-04")]
    out, fm, conf, notes = {"zone": zone, "uses": {}, "bulk": {}, "citations": []}, [], 0.9, []
    if trace.faults.get("docs") == "empty":
        return ToolResult("empty", out, src, 0.0, "[注入] 文档检索为空", ["FM-15"])
    pages = [p for p, (k, cols) in PAGE_SPECS.items() if col in cols]
    if not pages:
        return ToolResult("degraded", out, src, 0.2,
                          f"分区 {zone} 不在本 Agent 支持解析的页面中（仅支持商业区与 Collegetown 页）；请人工查阅图表", ["FM-15"])
    for p in pages:
        kind, cols = PAGE_SPECS[p]
        txt = _page_text(p)
        rows = {u: USE_ROWS[u] for u in uses if u in USE_ROWS} if kind == "use" else BULK_ROWS
        for key, label in rows.items():
            line, cells = _row_cells(txt, label)
            if line is None:
                notes.append(f"p{p} 未找到行 {label}"); fm.append("FM-15"); continue
            if trace.faults.get("docs") == "misaligned" and kind == "bulk":
                cells = cells[1:]
            aligned = len(cells) == len(cols)
            val = cells[cols.index(col)] if aligned else None
            foot = None
            if val:
                mm = re.fullmatch(r"(.+?)\s+(\d{1,2})", val)
                if mm and not re.fullmatch(r"\d+", val):
                    val, foot = mm.group(1), mm.group(2)
            rec = {"value": val, "footnote": foot, "page": p, "row": label.replace("\\", ""), "aligned": aligned,
                   "raw": re.sub(r"\s+", " ", line.strip())[:160]}
            if not aligned:
                fm.append("FM-13"); conf = min(conf, 0.45)
                notes.append(f"p{p}「{label}」行解析出 {len(cells)} 个单元格，表头有 {len(cols)} 列——列对齐不可信，不采用该值")
            (out["uses"] if kind == "use" else out["bulk"])[key] = rec
            out["citations"].append(f"{CHART_URL}#page={p}")
    # rule-based cross-check for CBD heights (note 5 on p5)
    if col == "CBD":
        m = re.match(r"CBD-(\d+)", zone.upper())
        if m:
            h = int(m.group(1))
            prev = out["bulk"].get("max_height_ft", {}).get("value")
            out["bulk"]["max_height_ft"] = {"value": f"{h} ft", "page": 5, "row": "Note (5): 高度=分区后缀",
                                            "aligned": True, "raw": "Each of the CBD sub-districts have a maximum building height that corresponds with its suffix",
                                            "table_cell": prev}
            st = out["bulk"].get("max_stories", {})
            if st.get("value") and not re.fullmatch(r"\d+", st["value"]):
                fm.append("FM-13"); notes.append(f"CBD 列最大层数单元格为「{st['value']}」，疑似抽取噪声，已丢弃")
                st["value"] = None; st["aligned"] = False
            if h == 140:
                out["bulk"]["max_stories"] = {"value": "12", "page": 5, "row": "Note (4)", "aligned": True, "raw": "CBD-140 has a maximum building height of 12 stories."}
    if col in ("CR-1", "CR-2", "CR-3", "CR-4", "MU-1", "MU-2"):
        notes.append("Collegetown 为形态准则分区（§325-45），图表外还有立面/激活要求")
    out["currency"] = {"doc": "Zoning Charts April 2026",
                       "warning": "市政府已发布分区条例全面重写 RFP，计划废止并替换 Chapter 325；结论需以申请时有效版本为准",
                       "rfp": RFP_URL}
    src.append(_src("rfp", "City of Ithaca – Zoning Code Update RFP", RFP_URL))
    fm_set = sorted(set(fm))
    return ToolResult("ok" if not fm_set else "degraded", out, src, conf, "；".join(notes), fm_set)


# ------------------------------------------------------------------ metrics (pure compute)
def metrics(parcel: dict | None, rules: dict | None, tr: dict | None) -> dict:
    m: dict[str, Any] = {}
    if parcel and parcel.get("lot_area_sqft"):
        m["lot_area_sqft"] = parcel["lot_area_sqft"]
    b = (rules or {}).get("bulk", {})

    def num(k):
        v = (b.get(k) or {}).get("value")
        if not v or not (b.get(k) or {}).get("aligned"):
            return None
        mm = re.search(r"\d+", v)
        return int(mm.group()) if mm else None
    cov, stories, hft = num("max_lot_coverage"), num("max_stories"), num("max_height_ft")
    if stories is None and hft:
        stories_est = max(1, hft // 12)   # ~12 ft floor-to-floor; labelled as estimate
        m["stories_estimated_from_height"] = stories_est
        stories = stories_est
    if m.get("lot_area_sqft") and cov and stories:
        m["envelope_gfa_sqft"] = round(m["lot_area_sqft"] * cov / 100 * stories)
        m["envelope_formula"] = f"宗地面积 {m['lot_area_sqft']} × 覆盖率 {cov}% × {stories} 层"
    if tr:
        # simple transit score 0-100: frequency within 400m, capped
        m["transit_score"] = min(100, round(tr.get("weekday_stop_events_400m", 0) / 15))
        m["transit_score_formula"] = "min(100, 400 米内站点工作日停靠班次 / 15)"
    return m
