"""Classify the public input/output contract, without guessing from model names."""

def output_kind(workflow):
    schema = workflow.get("input_schema") or workflow.get("inputSchema") or {}
    schema = schema if isinstance(schema, dict) else {}
    response = schema.get("response") or {}
    response = response if isinstance(response, dict) else {}
    for value in (response.get("type"), workflow.get("output_type")):
        if value in {"image", "video", "text", "audio"}:
            return value
    declared = str(workflow.get("workflow_type") or workflow.get("type") or "")
    prefix = declared.split(".")[0]
    return prefix if prefix in {"image", "video", "text", "audio"} else ""


def infer_capability(workflow):
    output = output_kind(workflow)
    schema = workflow.get("input_schema") or workflow.get("inputSchema") or {}
    schema = schema if isinstance(schema, dict) else {}
    inputs = schema.get("inputs", workflow.get("inputs"))
    if output == "text":
        return "text_model"
    if not isinstance(inputs, list) or any(not isinstance(item, dict) for item in inputs):
        return ""
    images = [item for item in inputs if item.get("type") == "image"]
    videos = [item for item in inputs if item.get("type") == "video"]
    if output == "video":
        if videos:
            return "video_to_video"
        names = " ".join(str(i.get("name", "")) + " " + str(i.get("label", "")) for i in images).lower()
        first = any(key in names for key in ("start_image", "first_frame", "start_frame", "首帧"))
        last = any(key in names for key in ("end_image", "last_frame", "end_frame", "尾帧"))
        if len(images) >= 2 and first and last:
            return "first_last_frame"
        return "image_to_video" if images else "text_to_video"
    if output == "image":
        return "text_image_to_image" if images else "text_to_image"
    return ""
