"""Resolve H3's linked resolution selector without changing saved workflows."""
import math
import re


def h3_dimensions(graph):
    nodes = [(key, node) for key, node in graph.items()
             if node.get("class_type") == "MiniMaxH3ImageToVideo"]
    if len(nodes) != 1:
        return None
    node_id, node = nodes[0]
    width, height = (node["inputs"].get(key) for key in ("width", "height"))
    if not (isinstance(width, list) and isinstance(height, list) and len(width) == len(height) == 2
            and width[0] == height[0] and width[1] == 0 and height[1] == 1):
        return None
    selector = graph.get(str(width[0]), {})
    if selector.get("class_type") != "ResolutionSelector":
        return None
    inputs = selector["inputs"]
    ratio = re.match(r"^(\d+):(\d+)(?:\s|$)", str(inputs.get("aspect_ratio", "")))
    megapixels, multiple = inputs.get("megapixels"), inputs.get("multiple")
    if not ratio or not isinstance(megapixels, (int, float)) or isinstance(megapixels, bool):
        return None
    if not math.isfinite(megapixels) or not 0 < megapixels <= 16:
        return None
    if type(multiple) is not int or not 8 <= multiple <= 128:
        return None
    w, h = map(int, ratio.groups())
    if not w or not h:
        return None
    scale = math.sqrt(megapixels * 1024 * 1024 / (w * h))
    step = math.lcm(multiple, 32)
    first = node["inputs"].get("first_frame")
    image_node = str(first[0]) if isinstance(first, list) and len(first) == 2 and first[1] == 0 else None
    if graph.get(image_node, {}).get("class_type") != "LoadImage":
        image_node = None
    return {"node_id": node_id, "width": max(step, round(w * scale / step) * step),
            "height": max(step, round(h * scale / step) * step), "step": step,
            "megapixels": megapixels, "image_node": image_node}


def reference_field(mapping, dimensions):
    for name, targets in mapping.get("api_bindings", {}).items():
        if any(t.get("node_id") == dimensions["image_node"] and t.get("input") == "image" for t in targets):
            return name
    return None


def h3_dimension_values(dimensions, body):
    supplied = [key in body for key in ("width", "height")]
    if any(supplied) and not all(supplied):
        raise ValueError("H3 宽度和高度必须一起填写")
    if not any(supplied):
        return None
    result = tuple(body[key] for key in ("width", "height"))
    if any(type(value) is not int or not dimensions["step"] <= value <= 8192
           or value % dimensions["step"] for value in result):
        raise ValueError(f"H3 宽高必须是 {dimensions['step']} 的整数倍，范围 {dimensions['step']}–8192")
    return result


def apply_h3_dimensions(graph, mapping, dimensions, values, image_sizes):
    if values is None:
        name = reference_field(mapping, dimensions)
        size = (image_sizes or {}).get(name)
        if not size:
            return
        width, height = size
        scale = math.sqrt(dimensions["megapixels"] * 1024 * 1024 / (width * height))
        step = dimensions["step"]
        values = tuple(max(step, math.floor(value * scale / step + 0.5) * step) for value in size)
        h3_dimension_values(dimensions, dict(zip(("width", "height"), values)))
    graph[dimensions["node_id"]]["inputs"].update(zip(("width", "height"), values))
