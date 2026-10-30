#!/usr/bin/env python3
"""One-off generator: Natural Earth 110m TopoJSON -> Robinson-projected SVG partial.

Inputs:  /tmp/countries-110m.json (world-atlas@2 countries-110m)
Output:  dashboard/templates/_world_map.html  (per-country <path id="ISO2">)
Run only when regenerating the map; result is committed like any vendored asset.
"""
import json
import math
from pathlib import Path

SRC = "/tmp/countries-110m.json"
OUT = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "_world_map.html"

# name in world-atlas (Natural Earth) -> ISO2 used by COUNTRY_ISO in db.py
NAME_ISO2 = {
    "United States of America": "US", "United Kingdom": "GB", "Germany": "DE",
    "India": "IN", "Canada": "CA", "South Korea": "KR", "China": "CN", "Japan": "JP",
    "Australia": "AU", "Israel": "IL", "Taiwan": "TW", "Italy": "IT", "Austria": "AT",
    "Netherlands": "NL", "France": "FR", "Poland": "PL", "Greece": "GR",
    "Denmark": "DK", "Belgium": "BE", "Finland": "FI", "Czechia": "CZ",
    "New Zealand": "NZ", "Switzerland": "CH", "Sweden": "SE", "Spain": "ES",
    "Portugal": "PT", "Norway": "NO", "Latvia": "LV", "Ireland": "IE", "Iran": "IR",
    "Cyprus": "CY", "Chile": "CL", "Bulgaria": "BG", "Brazil": "BR",
    "Saudi Arabia": "SA", "N. Cyprus": "CY",
}

RX = [1.0000, 0.9986, 0.9954, 0.9900, 0.9822, 0.9730, 0.9600, 0.9427, 0.9216,
      0.8962, 0.8679, 0.8350, 0.7986, 0.7597, 0.7186, 0.6732, 0.6213, 0.5722, 0.5322]
RY = [0.0000, 0.0620, 0.1240, 0.1860, 0.2480, 0.3100, 0.3720, 0.4340, 0.4958,
      0.5571, 0.6176, 0.6769, 0.7346, 0.7903, 0.8435, 0.8936, 0.9394, 0.9761, 1.0000]


def robinson(lon: float, lat: float) -> tuple[float, float]:
    i = min(int(abs(lat) / 5), 17)
    frac = (abs(lat) - i * 5) / 5
    rx = RX[i] + (RX[i + 1] - RX[i]) * frac
    ry = RY[i] + (RY[i + 1] - RY[i]) * frac
    return 0.8487 * rx * math.radians(lon), (ry if lat >= 0 else -ry)


def main() -> None:
    topo = json.load(open(SRC))
    tr = topo["transform"]
    sx, sy = tr["scale"]
    tx, ty = tr["translate"]
    decoded = []
    for arc in topo["arcs"]:
        pts, x, y = [], 0, 0
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        decoded.append(pts)

    def ring(arc_idxs):
        pts = []
        for i in arc_idxs:
            a = decoded[i] if i >= 0 else list(reversed(decoded[~i]))
            pts.extend(a[1:] if pts and pts[-1] == a[0] else a)
        return pts

    paths = []
    bounds = [math.inf, math.inf, -math.inf, -math.inf]  # xmin ymin xmax ymax
    for g in topo["objects"]["countries"]["geometries"]:
        name = g["properties"]["name"]
        if name == "Antarctica":
            continue
        iso = NAME_ISO2.get(name, "")
        dom = g["arcs"] if g["type"] == "MultiPolygon" else [g["arcs"]]
        d = []
        for poly in dom:
            for ring_i, ring_idxs in enumerate(poly):
                raw = ring(ring_idxs)
                # Unwrap longitudes so rings crossing the antimeridian stay
                # continuous (Fiji, Russia Chukotka) instead of tearing across
                # the whole map. lon jumps > 180 between neighbours -> shift.
                unwrapped = []
                shift = 0.0
                prev = None
                for lo, la in raw:
                    if prev is not None and lo - prev > 180:
                        shift -= 360.0
                    elif prev is not None and lo - prev < -180:
                        shift += 360.0
                    unwrapped.append((lo + shift, la))
                    prev = lo
                # Bring the ring's mean longitude into [-180, 180]: a ring
                # crossing the dateline then renders hugging the map edge on
                # its true side instead of a full revolution away.
                ring_lons = [lo for lo, _ in unwrapped]
                mean = sum(ring_lons) / len(ring_lons)
                # Keep the ring inside [-190, 190]: rings hugging the dateline
                # (Wrangel I., Fiji, Chukotka fringe) stay on the side where
                # their neighbours are; only full-revolution drift is undone.
                while mean > 190:
                    ring_lons = [lo - 360 for lo in ring_lons]
                    mean -= 360
                while mean < -190:
                    ring_lons = [lo + 360 for lo in ring_lons]
                    mean += 360
                proj = [robinson(min(max(lo, -270.0), 270.0), min(max(la, -89.9), 89.9))
                        for lo, la in zip(ring_lons, (la for _, la in unwrapped))]
                # drop degenerate slivers
                if len(proj) < 4:
                    continue
                pts = [(x * 190, -y * 190) for x, y in proj]
                for px, py in pts:
                    bounds[0] = min(bounds[0], px); bounds[1] = min(bounds[1], py)
                    bounds[2] = max(bounds[2], px); bounds[3] = max(bounds[3], py)
                d.append("M" + "L".join(f"{px:.1f},{py:.1f}" for px, py in pts) + "Z")
        if not d:
            continue
        title = name.replace("N. Cyprus", "Cyprus")
        pid = iso or "x-" + title.lower().replace(" ", "-")
        if pid in {p_id for p_id, _ in paths}:  # e.g. Cyprus + N. Cyprus both -> CY
            pid = "x-" + title.lower().replace(" ", "-")
        paths.append((pid, f'<path id="{pid}" '
                           f'class="c" d="{"".join(d)}"><title>{title}</title></path>'))

    # viewBox: compute from projected extents (x symmetric +-2.6352, y in [0, ~0.97])
    W, H = 1000, 360  # aspect ~2.78 (5.3 proj units wide, 1.92 tall)
    body = "\n".join(markup for _, markup in paths)
    mx = (bounds[2] - bounds[0]) * 0.02  # 2% margin
    my = (bounds[3] - bounds[1]) * 0.02
    vb = (f"{bounds[0] - mx:.0f} {bounds[1] - my:.0f} "
          f"{bounds[2] - bounds[0] + 2 * mx:.0f} {bounds[3] - bounds[1] + 2 * my:.0f}")
    svg = (
        f'<svg id="world-map" viewBox="{vb}" preserveAspectRatio="xMidYMid meet" '
        'class="w-full h-full block select-none" role="img" aria-label="World map">\n'
        '<style>\n'
        '#world-map .c{fill:rgb(var(--c-surface-2));stroke:rgb(var(--c-bg));stroke-width:0.7;transition:fill .15s}\n'
        '#world-map .c.link{cursor:pointer}\n'
        '#world-map .c.link:hover{fill:rgb(var(--c-accent))}\n'
        '</style>\n'
        f"{body}\n"
        "</svg>"
    )
    OUT.write_text(svg)
    print(f"wrote {OUT} ({len(svg) // 1024} KB, {len(paths)} countries)")


if __name__ == "__main__":
    main()
