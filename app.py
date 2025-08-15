import os, json, math, textwrap, datetime
from typing import List, Dict, Any, Tuple, Optional

import streamlit as st
import requests
import folium
from streamlit_folium import st_folium

# =========================
# Robust HTTP + caching
# =========================
_UA = {"User-Agent": "RouteForge/1.1 (no-keys; contact: https://github.com/vinabi)"}

@st.cache_data(ttl=3600, show_spinner=False)
def _get(url: str, params: dict = None, timeout: int = 20):
    r = requests.get(url, params=params or {}, headers=_UA, timeout=timeout)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=3600, show_spinner=False)
def _post(url: str, data: str, timeout: int = 90):
    r = requests.post(url, data=data, headers=_UA, timeout=timeout)
    r.raise_for_status()
    return r.json()

# =========================
# Geocoding (Nominatim + Photon + city-bias)
# =========================
@st.cache_data(ttl=3600, show_spinner=False)
def geocode_nominatim(q: str, limit=1) -> Optional[Tuple[float,float,str]]:
    try:
        data = _get("https://nominatim.openstreetmap.org/search",
                    {"q": q, "format": "json", "limit": limit})
        if data:
            lat = float(data[0]["lat"]); lon = float(data[0]["lon"])
            disp = data[0].get("display_name", q)
            return lat, lon, disp
    except Exception:
        pass
    return None

@st.cache_data(ttl=3600, show_spinner=False)
def geocode_photon(q: str) -> Optional[Tuple[float,float,str]]:
    try:
        data = _get("https://photon.komoot.io/api", {"q": q, "limit": 1})
        feats = data.get("features") or []
        if feats:
            c = feats[0]["geometry"]["coordinates"]  # [lon, lat]
            props = feats[0].get("properties", {})
            lat, lon = float(c[1]), float(c[0])
            label = props.get("name") or props.get("city") or q
            return lat, lon, label
    except Exception:
        pass
    return None

def _bbox(lat: float, lon: float, box_km: float = 12.0):
    dlat = box_km / 111.0
    dlon = box_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

@st.cache_data(ttl=3600, show_spinner=False)
def geocode_in_city(fragment: str, city_center: Tuple[float,float], box_km: float = 12.0):
    latc, lonc = city_center
    lon_min, lat_min, lon_max, lat_max = _bbox(latc, lonc, box_km)
    try:
        data = _get("https://nominatim.openstreetmap.org/search", {
            "q": fragment, "format": "json", "limit": 1,
            "viewbox": f"{lon_min},{lat_min},{lon_max},{lat_max}", "bounded": 1
        })
        if data:
            lat = float(data[0]["lat"]); lon = float(data[0]["lon"])
            disp = data[0].get("display_name", fragment)
            return lat, lon, disp
    except Exception:
        pass
    return None

def geocode_best(q: str, bias_city: Optional[Tuple[float,float]] = None):
    """Try Nominatim → city-bias → Photon. Returns (lat, lon, label) or None."""
    hit = geocode_nominatim(q)
    if hit: return hit
    if bias_city:
        hit = geocode_in_city(q, bias_city, box_km=15.0)
        if hit: return hit
    hit = geocode_photon(q)
    if hit: return hit
    return None

# =========================
# Overpass (multi-endpoint)
# =========================
_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]

def overpass_query(query: str) -> dict:
    last_err = None
    for ep in _OVERPASS_ENDPOINTS:
        try:
            return _post(ep, query, timeout=90)
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("Overpass failed")

def overpass_places(lat: float, lon: float, radius_m: int, kind: str) -> List[Dict[str,Any]]:
    if kind == "restaurant":
        q = f"""
        [out:json][timeout:60];
        node(around:{radius_m},{lat},{lon})[amenity=restaurant];
        out center 120;
        """
    else:
        q = f"""
        [out:json][timeout:60];
        (
          node(around:{radius_m},{lat},{lon})[tourism=attraction];
          node(around:{radius_m},{lat},{lon})[amenity=park];
          node(around:{radius_m},{lat},{lon})[leisure=park];
        );
        out center 150;
        """
    try:
        data = overpass_query(q)
        out = []
        for e in data.get("elements", []):
            tags = e.get("tags", {}) or {}
            plat = e.get("lat"); plon = e.get("lon")
            if plat is None or plon is None:
                c = (e.get("center") or {})
                plat, plon = c.get("lat"), c.get("lon")
            if plat is None or plon is None:
                continue
            out.append({
                "name": tags.get("name") or "Unnamed",
                "lat": float(plat), "lon": float(plon),
                "category": kind,
                "address": tags.get("addr:full",""),
                "url": f"https://www.openstreetmap.org/{e.get('type','node')}/{e.get('id')}"
            })
        return out
    except Exception:
        return []

# =========================
# Specific-need resolver
# =========================
def find_specific(center_xy: Tuple[float,float], radius_m: int, query_text: str) -> List[Dict[str,Any]]:
    if not query_text.strip():
        return []
    latc, lonc = center_xy

    # 1) Try resolving the text as a named place in the city (bias)
    hit = geocode_in_city(query_text, center_xy, box_km=radius_m/1000.0 * 1.5)
    if hit:
        plat, plon, label = hit
        return [{
            "name": label, "lat": plat, "lon": plon, "category": "specific",
            "address": label, "url": f"https://www.openstreetmap.org/?mlat={plat}&mlon={plon}"
        }]

    # 2) Try Overpass amenity guesses
    txt = query_text.lower()
    amenity = None
    if any(w in txt for w in ["pharmacy","chemist"]):
        amenity = "pharmacy"
    elif any(w in txt for w in ["restroom","toilet","washroom","bathroom"]):
        amenity = "toilets"
    elif any(w in txt for w in ["cafe","coffee","chai"]):
        amenity = "cafe"
    elif "restaurant" in txt:
        amenity = "restaurant"

    if amenity:
        over = f"""
        [out:json][timeout:60];
        (
          node(around:{radius_m},{latc},{lonc})[amenity="{amenity}"];
          way(around:{radius_m},{latc},{lonc})[amenity="{amenity}"];
          relation(around:{radius_m},{latc},{lonc})[amenity="{amenity}"];
        );
        out center 150;
        """
        try:
            data = overpass_query(over)
            out = []
            for e in data.get("elements", []):
                tags = e.get("tags", {}) or {}
                plat = e.get("lat") or (e.get("center") or {}).get("lat")
                plon = e.get("lon") or (e.get("center") or {}).get("lon")
                if plat is None or plon is None:
                    continue
                out.append({
                    "name": tags.get("name") or amenity.title(),
                    "lat": float(plat), "lon": float(plon),
                    "category": "specific", "address": tags.get("addr:full",""),
                    "url": f"https://www.openstreetmap.org/{e.get('type','node')}/{e.get('id')}"
                })
            if out:
                return out
        except Exception:
            pass

    # 3) Last resort: best-effort geocode anywhere
    hit2 = geocode_best(query_text)
    if hit2:
        plat, plon, label = hit2
        return [{
            "name": label, "lat": plat, "lon": plon, "category": "specific",
            "address": label, "url": f"https://www.openstreetmap.org/?mlat={plat}&mlon={plon}"
        }]

    return []

# =========================
# Routing (OSRM)
# =========================
@st.cache_data(ttl=120, show_spinner=False)
def osrm_table(coords: List[Tuple[float,float]], mode="driving") -> Dict[str, Any]:
    base = f"https://router.project-osrm.org/table/v1/{mode}/"
    path = ";".join([f"{lon},{lat}" for lat,lon in coords])
    r = requests.get(base + path, params={"annotations":"duration,distance"}, headers=_UA, timeout=60)
    r.raise_for_status()
    return r.json()

def osrm_route_geometry(coords: List[Tuple[float,float]], mode="driving") -> Optional[List[Tuple[float,float]]]:
    base = f"https://router.project-osrm.org/route/v1/{mode}/"
    path = ";".join([f"{lon},{lat}" for lat,lon in coords])
    try:
        r = requests.get(base + path, params={"overview":"full","geometries":"geojson"}, headers=_UA, timeout=60)
        r.raise_for_status()
        js = r.json()
        routes = js.get("routes") or []
        if routes:
            coords = routes[0]["geometry"]["coordinates"]  # [lon,lat]
            return [(c[1], c[0]) for c in coords]
    except Exception:
        pass
    return None

def haversine_km(a: Tuple[float,float], b: Tuple[float,float]) -> float:
    R = 6371.0
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1 = map(radians, a)
    lat2, lon2 = map(radians, b)
    dlat = lat2 - lat1; dlon = lon2 - lon1
    h = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 2*R*asin(sqrt(h))

def plan_route(origin: Tuple[float,float], dest: Tuple[float,float], stops: List[Dict[str,Any]], mode="driving") -> Dict[str,Any]:
    coords = [origin] + [(p["lat"], p["lon"]) for p in stops] + [dest]
    try:
        table = osrm_table(coords, mode=mode)
        dist = table.get("distances"); dur = table.get("durations")
    except Exception:
        dist = dur = None
    # fallback to haversine if OSRM fails
    if not dist or not dur:
        n = len(coords)
        dist = [[0]*n for _ in range(n)]
        dur = [[0]*n for _ in range(n)]
        speed_kmh = 40 if mode=="driving" else (5 if mode=="walking" else 15)
        for i in range(n):
            for j in range(n):
                if i==j: continue
                d = haversine_km(coords[i], coords[j])
                dist[i][j] = d*1000
                dur[i][j]  = (d/speed_kmh)*3600
    # greedy route: 0 -> visit all -> n-1
    n = len(coords); must_end = n-1
    unvisited = set(range(1, n-1))
    route = [0]; curr = 0
    while unvisited:
        nxt = min(unvisited, key=lambda j: dist[curr][j])
        route.append(nxt); unvisited.remove(nxt); curr = nxt
    route.append(must_end)
    legs, total_m, total_s = [], 0.0, 0.0
    for i in range(len(route)-1):
        a, b = route[i], route[i+1]
        total_m += dist[a][b]; total_s += dur[a][b]
        legs.append({"from_index": a, "to_index": b, "distance_m": dist[a][b], "duration_s": dur[a][b]})
    return {"order": route, "legs": legs, "total_distance_m": total_m, "total_duration_s": total_s}

# =========================
# Picking & Markdown
# =========================
def score_and_pick(places: List[Dict[str,Any]], center: Tuple[float,float], top_k: int, force_specific: bool) -> List[Dict[str,Any]]:
    for p in places:
        p["dist_km"] = round(haversine_km(center, (p["lat"], p["lon"])), 2)
    attractions = sorted([p for p in places if p.get("category")!="restaurant"], key=lambda x: x["dist_km"])
    restaurants = sorted([p for p in places if p.get("category")=="restaurant"], key=lambda x: x["dist_km"])
    specifics = [p for p in places if p.get("category")=="specific"]
    picks = []
    if force_specific and specifics:
        specifics.sort(key=lambda x: x.get("dist_km", 1e9))
        picks.append(specifics[0])
    i=j=0
    while len(picks) < max(2, top_k) and (i < len(attractions) or j < len(restaurants)):
        if i < len(attractions):
            cand = attractions[i]; i+=1
            if all(haversine_km((cand["lat"],cand["lon"]), (p["lat"],p["lon"])) > 0.01 for p in picks):
                picks.append(cand)
        if len(picks) >= top_k: break
        if j < len(restaurants):
            cand = restaurants[j]; j+=1
            if all(haversine_km((cand["lat"],cand["lon"]), (p["lat"],p["lon"])) > 0.01 for p in picks):
                picks.append(cand)
    return picks

def make_markdown(inputs: Dict[str,Any], ordered_nodes: List[Dict[str,Any]], total_km: float, total_hr: float, cost_est: float) -> str:
    lines = []
    lines.append(f"# Trip Plan: {inputs['origin']} → {inputs['final_destination']}")
    lines.append("")
    lines.append("## Overview")
    lines.append(f"- Mode: **{inputs['mode']}**")
    lines.append(f"- Stops before destination: **{inputs['top_k']}** (auto-selected by proximity & diversity)")
    lines.append(f"- Search radius: **{inputs['radius_m']} m**")
    lines.append(f"- Total distance: **{total_km:.1f} km**, total time: **{total_hr:.1f} hr**, est. cost: **{cost_est:.2f}**")
    if inputs.get("specific_need"):
        lines.append(f"- Specific request honored: **{inputs['specific_need']}**")
    lines.append("")
    lines.append("## Ordered Stops")
    for idx, n in enumerate(ordered_nodes):
        label = "Origin" if idx==0 else ("Destination" if idx==len(ordered_nodes)-1 else f"Stop {idx}")
        url = n.get("url","")
        extra = f" — {n.get('category','')}" if n.get("category") else ""
        if url:
            lines.append(f"- **{label}:** [{n['name']}]({url}) ({n['lat']:.5f}, {n['lon']:.5f}){extra}")
        else:
            lines.append(f"- **{label}:** {n['name']} ({n['lat']:.5f}, {n['lon']:.5f}){extra}")
    lines.append("")
    lines.append("## Budget (Simple Model)")
    lines.append(f"- Transport cost = distance_km × cost_per_km + hours × time_value_per_hr")
    lines.append(f"- Using: cost_per_km = {inputs['cost_per_km']}, time_value_per_hr = {inputs['time_value_per_hr']}")
    lines.append(f"- **Estimated total: {cost_est:.2f}**")
    lines.append("")
    lines.append("## Notes")
    lines.append("- Places sourced from OpenStreetMap; treat details as approximate.")
    lines.append("- OSRM public server estimates travel time; traffic not included.")
    return "\n".join(lines)

# =========================
# Streamlit UI (session_state to persist results)
# =========================
st.set_page_config(page_title="RouteForge — AI Travel Agent", page_icon="🧭", layout="wide")
st.title("🧭 RouteForge — AI Research & Planning Agent")
st.caption("Minimize cost. Maximize fun. Honor your specific stops.")

if "run" not in st.session_state:
    st.session_state.run = False

with st.form("rf_form"):
    col1, col2 = st.columns(2)
    with col1:
        origin = st.text_input("Origin (address or city)",
                               "Bahria Town, Lahore, Pakistan")
        final_destination = st.text_input("Final destination (address or city)",
                                          "DHA Phase 7, Islamabad, Pakistan")
        city_for_guides = st.text_input("City/Area to explore (press Enter to reuse destination)",
                                        "").strip() or final_destination
        specific_need = st.text_input("Anything specific to add? (e.g., pharmacy, coffee, store, restroom, or a named place)", "")
    with col2:
        mode = st.selectbox("Transport mode", ["driving","walking","cycling"], index=0)
        top_k = st.number_input("How many stops before the final destination?", min_value=1, max_value=12, value=6, step=1)
        radius_m = st.number_input("Search radius (meters)", min_value=500, max_value=10000, value=4000, step=250)
        cost_per_km = st.number_input("Cost per km (fuel/fare)", min_value=0.0, value=0.25, step=0.05)
        time_value_per_hr = st.number_input("Your time value per hour", min_value=0.0, value=5.0, step=0.5)

    do_run = st.form_submit_button("Plan my route", use_container_width=True)
    if do_run:
        st.session_state.run = True
        # keep latest inputs in state (for re-renders)
        st.session_state.inputs = {
            "origin": origin, "final_destination": final_destination, "city": city_for_guides,
            "mode": mode, "top_k": int(top_k), "radius_m": int(radius_m),
            "cost_per_km": float(cost_per_km), "time_value_per_hr": float(time_value_per_hr),
            "specific_need": specific_need.strip()
        }

st.button("↺ Reset", on_click=lambda: st.session_state.update(run=False), help="Clear the current plan")

if st.session_state.run:
    with st.spinner("Planning your route..."):
        # Use inputs from state so UI re-renders don’t clear results
        inputs = st.session_state.inputs

        # Geocoding with robust fallbacks
        g3 = geocode_best(inputs["city"]) or geocode_best(inputs["final_destination"])
        if not g3:
            st.error("Could not geocode the exploration city/area or destination. Try a clearer city name.")
            st.stop()
        center_xy = (g3[0], g3[1])

        g1 = geocode_best(inputs["origin"], bias_city=center_xy)
        g2 = geocode_best(inputs["final_destination"], bias_city=center_xy)

        # Detailed error if any missing
        missing = []
        if not g1: missing.append("origin")
        if not g2: missing.append("destination")
        if missing:
            st.error(f"Could not geocode: {', '.join(missing)}. Try including city & country (e.g., 'Bahria Town, Lahore, Pakistan').")
            st.stop()

        origin_xy = (g1[0], g1[1]); dest_xy = (g2[0], g2[1])

        # Discover places (general)
        places = overpass_places(center_xy[0], center_xy[1], int(inputs["radius_m"]), "attraction") + \
                 overpass_places(center_xy[0], center_xy[1], int(inputs["radius_m"]), "restaurant")

        # Specific need (free-form)
        specific_found = []
        if inputs["specific_need"]:
            specific_found = find_specific(center_xy, int(inputs["radius_m"]), inputs["specific_need"])

        # Merge and de-dup
        merged = (specific_found or []) + (places or [])
        uniq, seen = [], set()
        for p in merged:
            try:
                key = (p.get("name",""), round(float(p.get("lat",0) or 0),5), round(float(p.get("lon",0) or 0),5))
            except Exception:
                continue
            if key not in seen:
                seen.add(key); uniq.append(p)
        places = uniq

        # Score/pick with specific-stop guarantee
        picks = score_and_pick(places, dest_xy, int(inputs["top_k"]), force_specific=bool(inputs["specific_need"]))

        # Plan route
        route = plan_route(origin_xy, dest_xy, picks, mode=inputs["mode"])
        total_km = route["total_distance_m"]/1000.0
        total_hr = route["total_duration_s"]/3600.0
        cost_est = total_km*inputs["cost_per_km"] + total_hr*inputs["time_value_per_hr"]

        # Ordered nodes for map/report
        all_nodes = [{"name":"Origin","lat":origin_xy[0],"lon":origin_xy[1]}] + picks + [{"name":"Destination","lat":dest_xy[0],"lon":dest_xy[1]}]
        ordered_nodes = [ all_nodes[i] for i in route["order"] ]

        # MAP
        mid_lat = sum(n["lat"] for n in ordered_nodes)/len(ordered_nodes)
        mid_lon = sum(n["lon"] for n in ordered_nodes)/len(ordered_nodes)
        fmap = folium.Map(location=[mid_lat, mid_lon], zoom_start=11, control_scale=True)
        geom = osrm_route_geometry([(n["lat"], n["lon"]) for n in ordered_nodes], mode=inputs["mode"])
        if geom:
            folium.PolyLine(geom, weight=4, opacity=0.8, color="#2E86AB").add_to(fmap)
        for idx, n in enumerate(ordered_nodes):
            label = "Origin" if idx==0 else ("Destination" if idx==len(ordered_nodes)-1 else f"Stop {idx}")
            popup = f"{label}: {n['name']}<br>({n['lat']:.5f}, {n['lon']:.5f})"
            folium.Marker([n["lat"], n["lon"]], tooltip=label, popup=popup).add_to(fmap)
        st_folium(fmap, width=None, height=560)

        # SUMMARY
        st.subheader("Summary")
        st.write(f"**Mode:** {inputs['mode']}  |  **Stops:** {len(picks)}  |  **Radius:** {int(inputs['radius_m'])} m")
        st.write(f"**Distance:** {total_km:.1f} km  |  **Time:** {total_hr:.1f} hr  |  **Estimated Cost:** {cost_est:.2f}")
        if inputs["specific_need"]:
            st.info(f"Specific request honored: **{inputs['specific_need']}**")

        # DOWNLOADS
        md = make_markdown(inputs, ordered_nodes, total_km, total_hr, cost_est)
        json_payload = {
            "inputs": inputs,
            "geocodes": {"origin": g1, "destination": g2, "center": g3},
            "specific_candidates": specific_found,
            "all_candidates": places,
            "selected_stops": picks,
            "route": route,
            "totals": {"distance_km": round(total_km,2), "duration_hr": round(total_hr,2), "cost_est": round(cost_est,2)},
            "generated_at": datetime.datetime.utcnow().isoformat()+"Z"
        }
        st.download_button("↓ Download Itinerary.md", data=md.encode("utf-8"),
                           file_name="Itinerary.md", mime="text/markdown")
        st.download_button("↓ Download trip_plan.json",
                           data=json.dumps(json_payload, ensure_ascii=False, indent=2).encode("utf-8"),
                           file_name="trip_plan.json", mime="application/json")

st.markdown("---")
st.caption("Built with OpenStreetMap, Overpass (multi-endpoint), and OSRM public endpoints. No keys required.")
