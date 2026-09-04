from __future__ import annotations

import colorsys
import math
from collections import Counter, deque
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageFilter

from app.services.processing.pixels import pixel_data

MAX_VECTOR_DIM = 768
MIN_LAYER_SVG_BYTES = 1024
MAX_OUTPUT_COLORS = 6


def _is_white(color: tuple[int, int, int]) -> bool:
    return color[0] >= 245 and color[1] >= 245 and color[2] >= 245


def _is_neutral(color: tuple[int, int, int]) -> bool:
    return max(color) - min(color) < 18


def _is_near_black(color: tuple[int, int, int]) -> bool:
    return color[0] <= 28 and color[1] <= 28 and color[2] <= 28


def _hsv(color: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = color
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)


def _hue_distance_deg(h1: float, h2: float) -> float:
    d = abs((h1 - h2) * 360.0)
    return min(d, 360.0 - d)


def _can_merge_colors(candidate: tuple[int, int, int], base: tuple[int, int, int]) -> bool:
    if _is_white(candidate) and _is_white(base):
        return True
    if _is_near_black(candidate) or _is_near_black(base):
        return _is_near_black(candidate) and _is_near_black(base)
    if _is_neutral(candidate) != _is_neutral(base):
        return False

    if _is_neutral(candidate):
        return (
            abs(candidate[0] - base[0]) <= 24
            and abs(candidate[1] - base[1]) <= 24
            and abs(candidate[2] - base[2]) <= 24
        )

    c_h, c_s, _c_v = _hsv(candidate)
    b_h, b_s, _b_v = _hsv(base)
    hue_dist = _hue_distance_deg(c_h, b_h)
    sat_dist = abs(c_s - b_s)
    rgb_dist = math.dist(candidate, base)

    # Wider merge window for warm palette shades (reds/oranges/yellows),
    # where generated anti-alias often creates many near-duplicate tones.
    warm_candidate = c_h <= (70.0 / 360.0) or c_h >= (320.0 / 360.0)
    warm_base = b_h <= (70.0 / 360.0) or b_h >= (320.0 / 360.0)
    if warm_candidate and warm_base:
        return hue_dist <= 30.0 and sat_dist <= 0.45 and rgb_dist <= 90.0

    # Wider merge window for cool palette shades (blue/cyan family).
    c_deg = c_h * 360.0
    b_deg = b_h * 360.0
    cool_candidate = 170.0 <= c_deg <= 255.0
    cool_base = 170.0 <= b_deg <= 255.0
    if cool_candidate and cool_base:
        return hue_dist <= 40.0 and sat_dist <= 0.58 and rgb_dist <= 140.0

    return hue_dist <= 20.0 and sat_dist <= 0.30 and rgb_dist <= 60.0


def _load_source_image(source_image_uri: str | None) -> Image.Image:
    if not source_image_uri:
        return Image.new("RGBA", (512, 512), color=(245, 245, 245, 255))
    parsed = urlparse(source_image_uri)
    if parsed.scheme in {"http", "https"}:
        with httpx.Client(timeout=60.0) as client:
            response = client.get(source_image_uri)
            response.raise_for_status()
            return Image.open(BytesIO(response.content)).convert("RGBA")

    # API can pass data URI path like "/data/runtime_cache/..." while inside containers
    # real files are mounted under "/app/data/...".
    if source_image_uri.startswith("/data/"):
        path = Path("/app") / source_image_uri.lstrip("/")
    elif source_image_uri.startswith("data/"):
        path = Path("/app") / source_image_uri
    else:
        path = Path(source_image_uri)
    if not path.exists():
        raise FileNotFoundError(f"Source image not found: {path}")
    return Image.open(path).convert("RGBA")


def prepare_vector_source(
    session_dir: Path,
    candidate_id: str,
    source_image_uri: str | None,
) -> dict[str, str]:
    source_image = _load_source_image(source_image_uri)
    prepare_dir = session_dir / f"base_{candidate_id}"
    prepare_dir.mkdir(parents=True, exist_ok=True)
    prepared_path = prepare_dir / "prepared_rgba.png"
    source_image.save(prepared_path)
    return {"prepared_image_path": str(prepared_path)}


def _build_run_rects(
    pixels: list[tuple[int, int, int]],
    width: int,
    height: int,
    target_color: tuple[int, int, int],
    alpha_mask: list[bool] | None = None,
) -> list[tuple[int, int, int]]:
    rects: list[tuple[int, int, int]] = []
    for y in range(height):
        row_start = y * width
        x = 0
        while x < width:
            color = pixels[row_start + x]
            if color != target_color or (alpha_mask is not None and not alpha_mask[row_start + x]):
                x += 1
                continue
            start_x = x
            x += 1
            while x < width and pixels[row_start + x] == target_color and (
                alpha_mask is None or alpha_mask[row_start + x]
            ):
                x += 1
            rects.append((start_x, y, x - start_x))
    return rects


def _build_mask(
    pixels: list[tuple[int, int, int]],
    width: int,
    height: int,
    target_color: tuple[int, int, int],
    alpha_mask: list[bool],
) -> list[bool]:
    out = [False] * (width * height)
    for idx in range(width * height):
        out[idx] = alpha_mask[idx] and pixels[idx] == target_color
    return out


def _extract_loops_from_mask(mask: list[bool], width: int, height: int) -> list[list[tuple[float, float]]]:
    # Build boundary edges along pixel grid.
    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def is_on(x: int, y: int) -> bool:
        if x < 0 or y < 0 or x >= width or y >= height:
            return False
        return mask[y * width + x]

    def add_edge(a: tuple[int, int], b: tuple[int, int]) -> None:
        edges.setdefault(a, []).append(b)

    for y in range(height):
        for x in range(width):
            if not is_on(x, y):
                continue
            if not is_on(x, y - 1):  # top
                add_edge((x, y), (x + 1, y))
            if not is_on(x + 1, y):  # right
                add_edge((x + 1, y), (x + 1, y + 1))
            if not is_on(x, y + 1):  # bottom
                add_edge((x + 1, y + 1), (x, y + 1))
            if not is_on(x - 1, y):  # left
                add_edge((x, y + 1), (x, y))

    used: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    loops: list[list[tuple[float, float]]] = []

    for start, outs in edges.items():
        for nxt in outs:
            edge = (start, nxt)
            if edge in used:
                continue
            loop_i: list[tuple[int, int]] = [start]
            cur = start
            to = nxt
            used.add(edge)
            safety = 0
            while safety < width * height * 8:
                safety += 1
                loop_i.append(to)
                cur = to
                if cur == start:
                    break
                candidates = edges.get(cur, [])
                if not candidates:
                    break
                picked = None
                for c in candidates:
                    if (cur, c) not in used:
                        picked = c
                        break
                if picked is None:
                    break
                used.add((cur, picked))
                to = picked
            if len(loop_i) >= 4 and loop_i[0] == loop_i[-1]:
                loop = [(float(x), float(y)) for (x, y) in loop_i[:-1]]
                loops.append(loop)
    return loops


def _remove_small_components(mask: list[bool], width: int, height: int, min_area: int) -> list[bool]:
    if min_area <= 1:
        return mask
    out = mask[:]
    visited = [False] * (width * height)
    for y in range(height):
        for x in range(width):
            idx = y * width + x
            if visited[idx] or not out[idx]:
                continue
            stack = [idx]
            visited[idx] = True
            comp: list[int] = []
            while stack:
                cur = stack.pop()
                comp.append(cur)
                cx = cur % width
                cy = cur // width
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    nidx = ny * width + nx
                    if visited[nidx] or not out[nidx]:
                        continue
                    visited[nidx] = True
                    stack.append(nidx)
            if len(comp) < min_area:
                for c in comp:
                    out[c] = False
    return out


def _polygon_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += (x1 * y2) - (x2 * y1)
    return area * 0.5


def _remove_collinear(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) < 4:
        return points
    out: list[tuple[float, float]] = []
    n = len(points)
    for i in range(n):
        p0 = points[(i - 1) % n]
        p1 = points[i]
        p2 = points[(i + 1) % n]
        v1 = (p1[0] - p0[0], p1[1] - p0[1])
        v2 = (p2[0] - p1[0], p2[1] - p1[1])
        cross = (v1[0] * v2[1]) - (v1[1] * v2[0])
        if abs(cross) > 1e-6:
            out.append(p1)
    return out if len(out) >= 3 else points


def _rdp_open(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    start = points[0]
    end = points[-1]
    max_dist = -1.0
    idx = -1
    ex = end[0] - start[0]
    ey = end[1] - start[1]
    den = math.hypot(ex, ey)
    for i in range(1, len(points) - 1):
        px, py = points[i]
        if den <= 1e-9:
            d = math.hypot(px - start[0], py - start[1])
        else:
            d = abs((ey * px) - (ex * py) + (end[0] * start[1]) - (end[1] * start[0])) / den
        if d > max_dist:
            max_dist = d
            idx = i
    if max_dist > epsilon and idx > 0:
        left = _rdp_open(points[: idx + 1], epsilon)
        right = _rdp_open(points[idx:], epsilon)
        return left[:-1] + right
    return [start, end]


def _simplify_closed_loop(points: list[tuple[float, float]], epsilon: float = 1.1) -> list[tuple[float, float]]:
    if len(points) < 4:
        return points
    pts = _remove_collinear(points)
    open_pts = pts + [pts[0]]
    simp = _rdp_open(open_pts, epsilon)
    simp = simp[:-1]
    if len(simp) < 3:
        return pts
    return simp


def _chaikin_closed(points: list[tuple[float, float]], iterations: int = 1) -> list[tuple[float, float]]:
    if len(points) < 4:
        return points
    out = points[:]
    for _ in range(iterations):
        nxt: list[tuple[float, float]] = []
        n = len(out)
        for i in range(n):
            p0 = out[i]
            p1 = out[(i + 1) % n]
            q = (0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1])
            r = (0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1])
            nxt.extend([q, r])
        out = nxt
    return out


def _loops_to_path(loops: list[list[tuple[float, float]]]) -> str:
    if not loops:
        return ""
    parts: list[str] = []
    for loop in loops:
        if len(loop) < 3:
            continue
        simp = _simplify_closed_loop(loop, epsilon=1.6)
        smoothed = _chaikin_closed(simp, iterations=1 if len(simp) >= 8 else 0)
        pts = smoothed if smoothed else simp
        if _polygon_area(pts) < 0:
            pts = list(reversed(pts))
        start = pts[0]
        parts.append(f"M{start[0]:.2f},{start[1]:.2f}")
        for x, y in pts[1:]:
            parts.append(f"L{x:.2f},{y:.2f}")
        parts.append("Z")
    return " ".join(parts)


def _merge_vertical_rects(rects: list[tuple[int, int, int]]) -> list[tuple[int, int, int, int]]:
    # Merge adjacent 1px-high runs that share x and width into taller rectangles.
    runs = sorted(rects, key=lambda item: (item[0], item[2], item[1]))
    merged: list[tuple[int, int, int, int]] = []
    for x, y, w in runs:
        if merged and merged[-1][0] == x and merged[-1][2] == w and (merged[-1][1] + merged[-1][3]) == y:
            px, py, pw, ph = merged[-1]
            merged[-1] = (px, py, pw, ph + 1)
        else:
            merged.append((x, y, w, 1))
    return merged


def _build_clean_mask_from_rects(rects: list[tuple[int, int, int]]) -> tuple[list[bool], int, int]:
    if not rects:
        return [], 0, 0
    max_x = max(x + w for x, _y, w in rects)
    max_y = max(y for _x, y, _w in rects) + 1
    mask = [False] * (max_x * max_y)
    for x, y, w in rects:
        row = y * max_x
        for ix in range(x, x + w):
            mask[row + ix] = True

    on_pixels = sum(1 for v in mask if v)
    min_area = max(
        24,
        int((max_x * max_y) * 0.00005),
        int(on_pixels * 0.0015),
    )
    mask = _remove_small_components(mask=mask, width=max_x, height=max_y, min_area=min_area)
    return mask, max_x, max_y


def _render_layer_svg(rects: list[tuple[int, int, int]], color: str, width: int, height: int) -> str:
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
    ]
    path_d = ""
    if rects:
        mask, max_x, max_y = _build_clean_mask_from_rects(rects)
        loops = _extract_loops_from_mask(mask=mask, width=max_x, height=max_y)
        path_d = _loops_to_path(loops)
    if path_d:
        lines.append(f'  <path d="{path_d}" fill="{color}" fill-rule="evenodd" />')
    else:
        for x, y, w, h in _merge_vertical_rects(rects):
            lines.append(f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}" />')
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _render_master_svg(all_layers: list[tuple[str, list[tuple[int, int, int]]]], width: int, height: int) -> str:
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
    ]
    for color, rects in all_layers:
        lines.append(f'  <g id="{color[1:].lower()}">')
        path_d = ""
        if rects:
            mask, max_x, max_y = _build_clean_mask_from_rects(rects)
            loops = _extract_loops_from_mask(mask=mask, width=max_x, height=max_y)
            path_d = _loops_to_path(loops)
        if path_d:
            lines.append(f'    <path d="{path_d}" fill="{color}" fill-rule="evenodd" />')
        else:
            for x, y, w, h in _merge_vertical_rects(rects):
                lines.append(f'    <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}" />')
        lines.append("  </g>")
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _border_background_mask(source_image: Image.Image, threshold: int = 10) -> list[bool]:
    # Detect white/flat background by flood-filling similar colors connected to image borders.
    rgb = source_image.convert("RGB")
    width, height = rgb.size
    pixels = list(pixel_data(rgb))
    alpha = list(pixel_data(source_image.getchannel("A")))

    border_colors: list[tuple[int, int, int]] = []
    for x in range(width):
        border_colors.append(pixels[x])
        border_colors.append(pixels[(height - 1) * width + x])
    for y in range(height):
        border_colors.append(pixels[y * width])
        border_colors.append(pixels[y * width + (width - 1)])

    bg_color = Counter(border_colors).most_common(1)[0][0]

    def close_to_bg(c: tuple[int, int, int]) -> bool:
        return (
            abs(c[0] - bg_color[0]) <= threshold
            and abs(c[1] - bg_color[1]) <= threshold
            and abs(c[2] - bg_color[2]) <= threshold
        )

    visited = [False] * (width * height)
    q: deque[tuple[int, int]] = deque()

    def try_seed(x: int, y: int) -> None:
        idx = y * width + x
        if visited[idx] or alpha[idx] <= 8 or not close_to_bg(pixels[idx]):
            return
        visited[idx] = True
        q.append((x, y))

    for x in range(width):
        try_seed(x, 0)
        try_seed(x, height - 1)
    for y in range(height):
        try_seed(0, y)
        try_seed(width - 1, y)

    while q:
        x, y = q.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            idx = ny * width + nx
            if visited[idx] or alpha[idx] <= 8 or not close_to_bg(pixels[idx]):
                continue
            visited[idx] = True
            q.append((nx, ny))
    return visited


def build_vector_assets(
    pack_dir: Path,
    candidate_id: str,
    palette: list[str],
    source_image_uri: str | None = None,
) -> dict:
    _ = candidate_id

    vector_dir = pack_dir / "vector"
    layer_dir = vector_dir / "layers"
    layer_dir.mkdir(parents=True, exist_ok=True)

    if not source_image_uri:
        width, height = 480, 480
        layer_entries: list[tuple[str, list[tuple[int, int, int]]]] = []
        color_layers: list[dict[str, str]] = []
        color_count = max(1, len(palette))
        band_width = max(1, width // color_count)
        for idx, color in enumerate(palette):
            x0 = idx * band_width
            x1 = width if idx == color_count - 1 else min(width, (idx + 1) * band_width)
            rects = [(x0, y, max(1, x1 - x0)) for y in range(height)]
            layer_svg = _render_layer_svg(rects=rects, color=color, width=width, height=height)
            layer_name = f"{color[1:].lower()}.svg"
            (layer_dir / layer_name).write_text(layer_svg, encoding="utf-8")
            layer_entries.append((color, rects))
            color_layers.append({"color": color, "path": f"vector/layers/{layer_name}"})
        master_svg = _render_master_svg(all_layers=layer_entries, width=width, height=height)
        (vector_dir / "master.svg").write_text(master_svg, encoding="utf-8")
        return {
            "master_svg": "vector/master.svg",
            "color_layers": color_layers,
        }

    source_image = _load_source_image(source_image_uri)
    src_w, src_h = source_image.size
    if max(src_w, src_h) > MAX_VECTOR_DIM:
        scale = MAX_VECTOR_DIM / float(max(src_w, src_h))
        new_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
        source_image = source_image.resize(new_size, Image.Resampling.LANCZOS)
    width, height = source_image.size
    background_mask = _border_background_mask(source_image=source_image, threshold=10)
    alpha_values = list(pixel_data(source_image.getchannel("A")))
    alpha_mask = [alpha_values[idx] > 8 and not background_mask[idx] for idx in range(len(alpha_values))]

    rgba = source_image.copy()
    rgba_data = list(pixel_data(rgba))
    rgba_data = [
        (r, g, b, 0) if not alpha_mask[idx] else (r, g, b, 255)
        for idx, (r, g, b, _a) in enumerate(rgba_data)
    ]
    rgba.putdata(rgba_data)
    rgba = rgba.filter(ImageFilter.MedianFilter(size=3))
    pixels = [(r, g, b) for (r, g, b, _a) in list(pixel_data(rgba))]

    # Merge near-identical shades to avoid edge-only micro-layers (e.g. multiple whites).
    color_counts = Counter(pixels)
    ordered_colors = [color for color, _count in color_counts.most_common()]
    palette_map: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    merged_colors: list[tuple[int, int, int]] = []
    for color in ordered_colors:
        mapped = None
        for base in merged_colors:
            if _can_merge_colors(color, base):
                mapped = base
                break
        if mapped is None:
            mapped = color
            merged_colors.append(color)
        palette_map[color] = mapped
    pixels = [palette_map[c] for c in pixels]

    # Keep max 6 colors by occupied area (in-object pixels only).
    merged_color_area: Counter[tuple[int, int, int]] = Counter()
    for idx, color in enumerate(pixels):
        if not alpha_mask[idx]:
            continue
        if _is_white(color):
            continue
        merged_color_area[color] += 1
    top_colors = {color for color, _area in merged_color_area.most_common(MAX_OUTPUT_COLORS)}

    # Preserve original generated colors (no palette recoloring) and keep stable draw order.
    color_order: list[tuple[int, int, int]] = []
    seen_colors: set[tuple[int, int, int]] = set()
    for idx, color in enumerate(pixels):
        if not alpha_mask[idx]:
            continue
        if _is_white(color):
            continue
        if color not in top_colors:
            continue
        if color in seen_colors:
            continue
        seen_colors.add(color)
        color_order.append(color)

    prepared_layers: list[dict] = []
    for color_rgb in color_order:
        color = "#{:02X}{:02X}{:02X}".format(*color_rgb)
        rects = _build_run_rects(
            pixels=pixels,
            width=width,
            height=height,
            target_color=color_rgb,
            alpha_mask=alpha_mask,
        )
        if not rects:
            continue
        layer_svg = _render_layer_svg(rects=rects, color=color, width=width, height=height)
        prepared_layers.append(
            {
                "color": color,
                "rects": rects,
                "svg": layer_svg,
                "size_bytes": len(layer_svg.encode("utf-8")),
            }
        )

    kept_layers = [item for item in prepared_layers if item["size_bytes"] >= MIN_LAYER_SVG_BYTES]
    if not kept_layers and prepared_layers:
        # Keep one dominant layer to avoid empty output on tiny/simple assets.
        kept_layers = [max(prepared_layers, key=lambda item: item["size_bytes"])]

    layer_entries: list[tuple[str, list[tuple[int, int, int]]]] = []
    color_layers: list[dict[str, str]] = []
    for item in kept_layers:
        color = item["color"]
        rects = item["rects"]
        layer_svg = item["svg"]
        layer_name = f"{color[1:].lower()}.svg"
        (layer_dir / layer_name).write_text(layer_svg, encoding="utf-8")
        layer_entries.append((color, rects))
        color_layers.append({"color": color, "path": f"vector/layers/{layer_name}"})

    master_svg = _render_master_svg(all_layers=layer_entries, width=width, height=height)
    (vector_dir / "master.svg").write_text(master_svg, encoding="utf-8")

    return {
        "master_svg": "vector/master.svg",
        "color_layers": color_layers,
    }
