"""Resolve workflow model locations from declarations and installed templates."""
import json
from pathlib import Path
from urllib.parse import urlsplit

from app.core.model_maintenance import MODEL_REQUIREMENTS
from app.core.model_mappings import CATEGORIES, safe_relative
from app.core.workflow_dependencies import normalize_workflow_dependencies
from app.core.model_source_discovery import load_saved_source

INPUT_CATEGORIES = {"vae_name": "vae", "unet_name": "diffusion_models", "clip_name": "text_encoders", "clip_name1": "text_encoders", "clip_name2": "text_encoders", "lora_name": "loras", "ckpt_name": "checkpoints", "clip_vision": "clip_vision", "control_net_name": "controlnet", "upscale_model": "upscale_models"}
NODE_CATEGORIES = {"VAELoader": "vae", "UNETLoader": "diffusion_models", "CLIPLoader": "text_encoders",
                   "DualCLIPLoader": "text_encoders", "TripleCLIPLoader": "text_encoders",
                   "LoraLoader": "loras", "LoraLoaderModelOnly": "loras",
                   "CheckpointLoaderSimple": "checkpoints", "CLIPVisionLoader": "clip_vision",
                   "ControlNetLoader": "controlnet", "UpscaleModelLoader": "upscale_models"}


def _source_item(value, input_name=""):
    if not isinstance(value, dict):
        return None
    raw = str(value.get("path") or value.get("source") or value.get("name") or "").replace("\\", "/")
    directory = value.get("directory") or value.get("category") or INPUT_CATEGORIES.get(input_name) or NODE_CATEGORIES.get(value.get("node"))
    if raw.startswith("models/"):
        raw = raw[7:]
    if raw.split("/")[0] not in CATEGORIES and directory:
        raw = f"{directory}/{raw}"
    try:
        relative = safe_relative(raw)
    except ValueError:
        return None
    url = str(value.get("url") or value.get("download_url") or "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        url = ""
    return {"path": relative, "url": url, **{k: value[k] for k in ("size_bytes", "sha256") if value.get(k)}}


def _template_items(data):
    if isinstance(data, dict):
        for item in data.get("models", []) if isinstance(data.get("models"), list) else []:
            spec = _source_item(item)
            if spec:
                yield spec
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _template_items(value)
    elif isinstance(data, list):
        for value in data:
            yield from _template_items(value)


def workflow_model_items(workflow, base_dir, workflow_graph=None):
    dependencies = normalize_workflow_dependencies(workflow.get("dependencies"), workflow_graph)
    embedded = [item for item in _template_items(workflow_graph or {}) if item.get("url")]
    known = [dict(item) for group in MODEL_REQUIREMENTS.values() for item in group.get("items", [])]
    templates = Path(base_dir) / "runtime/python/Lib/site-packages/comfyui_workflow_templates_json/templates"
    wanted = {item["name"] for item in dependencies["models"]}
    # Model references often survive conversion; UI-only download metadata does not.
    for path in templates.glob("*.json"):
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
            if not any(name in text for name in wanted):
                continue
            known.extend(_template_items(json.loads(text)))
        except (OSError, ValueError, RecursionError):
            continue
    result = []
    for entry in dependencies["models"]:
        inferred = _source_item(entry, entry.get("input", ""))
        candidates = [item for item in known if Path(item["path"]).name == entry["name"]]
        author_candidates = [item for item in embedded if Path(item["path"]).name == entry["name"]]
        if inferred:
            candidates = [item for item in candidates if item["path"] == inferred["path"]]
            author_candidates = [item for item in author_candidates if item["path"] == inferred["path"]]
        if author_candidates:
            candidates = author_candidates
        # Prefer an explicit URL. Otherwise require agreement on the destination.
        if inferred and inferred["url"]:
            item = inferred
        elif candidates and len({item["path"] for item in candidates}) == 1:
            identities = {str(candidate.get("sha256") or candidate.get("url") or "") for candidate in candidates}
            if len(identities) == 1:
                item = dict(candidates[0])
            else:
                # Same path is not proof of identical content. Verify and offer every version.
                item = {"path": candidates[0]["path"], "url": "",
                        "candidate_urls": list(dict.fromkeys(c["url"] for c in candidates if c.get("url")))}
        else:
            item = inferred
        if item and not item.get("url"):
            saved = load_saved_source(Path(base_dir) / "runtime/model_sources.json", item["path"])
            if saved:
                item = {**item, **saved}
        if item and item["path"] not in {existing["path"] for existing in result}:
            result.append(item)
    return result
