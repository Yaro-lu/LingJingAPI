import json
import re
import requests
from pathlib import Path
from typing import Dict, Optional, Any
import logging


class ComfyUIClient:
    def __init__(self, url: str = "http://127.0.0.1:8188", logger: Optional[logging.Logger] = None, log_path: Optional[Path] = None):
        self.url = url
        self.logger = logger or logging.getLogger(__name__)
        self.log_path = Path(log_path) if log_path else None
        self._progress_high_water: Dict[str, int] = {}

    def get_history(self, prompt_id: Optional[str] = None) -> Dict:
        if prompt_id:
            response = requests.get(f"{self.url}/history/{prompt_id}", timeout=30)
        else:
            response = requests.get(f"{self.url}/history", timeout=30)
        return response.json()

    def queue_prompt(self, workflow: Dict[str, Any]) -> str:
        payload = {"prompt": workflow}
        response = requests.post(f"{self.url}/prompt", json=payload, timeout=60)
        try:
            data = response.json()
        except Exception:
            response.raise_for_status()
            raise RuntimeError(f"ComfyUI returned non-JSON response: {response.text[:300]}")
        if not response.ok or "prompt_id" not in data:
            message = data.get("error") or data.get("message") or data.get("detail") or str(data)
            raise RuntimeError(f"ComfyUI prompt validation failed: {message}")
        return data["prompt_id"]

    def upload_input_image(self, image_data: bytes, filename: str, mime_type: str) -> str:
        """Upload a validated image to ComfyUI and return its LoadImage name."""
        response = requests.post(
            f"{self.url}/upload/image",
            files={"image": (filename, image_data, mime_type)},
            data={"type": "input", "overwrite": "true"},
            timeout=60,
        )
        try:
            payload = response.json()
        except Exception:
            response.raise_for_status()
            raise RuntimeError("ComfyUI image upload returned a non-JSON response")
        if not response.ok:
            message = payload.get("error") or payload.get("message") or payload.get("detail") or str(payload)
            raise RuntimeError(f"ComfyUI image upload failed: {message}")
        name = str(payload.get("name") or "").strip().replace("\\", "/")
        subfolder = str(payload.get("subfolder") or "").strip().strip("/\\").replace("\\", "/")
        if not name or name.startswith("/") or ".." in name.split("/"):
            raise RuntimeError("ComfyUI image upload returned an invalid filename")
        if subfolder and (subfolder.startswith("/") or ".." in subfolder.split("/")):
            raise RuntimeError("ComfyUI image upload returned an invalid subfolder")
        return f"{subfolder}/{name}" if subfolder else name

    def get_queue_status(self) -> Dict:
        """获取 ComfyUI 队列状态，含当前执行进度"""
        response = requests.get(f"{self.url}/queue", timeout=30)
        return response.json()

    @staticmethod
    def _queue_item_prompt_id(item: Any) -> str:
        if isinstance(item, (list, tuple)):
            for value in item:
                if isinstance(value, str) and len(value) >= 8:
                    return value
        if isinstance(item, dict):
            return str(item.get("prompt_id") or item.get("promptId") or item.get("id") or "")
        return ""

    def _latest_log_progress(self, expected_steps: int = 20, log_offset: int = 0) -> Optional[Dict]:
        if not self.log_path or not self.log_path.exists():
            return None
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                start = min(size, max(0, int(log_offset or 0), size - 160 * 1024))
                f.seek(start)
                text = f.read().decode("utf-8", errors="ignore")
        except Exception:
            return None

        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text.replace("\r", "\n"))
        tail = text[-20000:]
        matches = list(re.finditer(r"(\d{1,3})%\|[^\n]*?\|\s*(\d+)\s*/\s*(\d+)", tail))
        if matches:
            match = matches[-1]
            percent = max(0, min(100, int(match.group(1))))
            value = int(match.group(2))
            max_value = max(1, int(match.group(3)))
            return {
                "value": value,
                "max": max_value,
                "percent": percent,
                "status": "running",
                "phase": "采样中",
                "label": f"采样 {value}/{max_value}",
            }

        if "Model Initializing" in tail or "model weight dtype" in tail:
            return {
                "value": 0,
                "max": max(1, int(expected_steps or 20)),
                "percent": 0,
                "status": "running",
                "phase": "模型初始化",
                "label": "模型初始化",
            }
        return None

    def _with_progress_high_water(self, prompt_id: str, progress: Dict) -> Dict:
        status = str(progress.get("status") or "").lower()
        percent = max(0, min(100, int(progress.get("percent") or 0)))
        if status == "completed":
            progress["percent"] = 100
            self._progress_high_water.pop(prompt_id, None)
            return progress

        percent = min(94, percent)
        percent = max(self._progress_high_water.get(prompt_id, 0), percent)
        progress["percent"] = percent
        self._progress_high_water[prompt_id] = percent
        return progress

    def get_progress(self, prompt_id: str, expected_steps: int = 20, log_offset: int = 0) -> Optional[Dict]:
        """获取指定 prompt 的执行进度 { value, max, status } 或 None"""
        q = self.get_queue_status()
        running = q.get("queue_running", [])
        pending = q.get("queue_pending", [])

        # 在运行队列中找
        for item in running:
            if self._queue_item_prompt_id(item) == prompt_id:
                progress = self._latest_log_progress(expected_steps, log_offset=log_offset)
                if progress:
                    progress["status"] = "running"
                    return self._with_progress_high_water(prompt_id, progress)
                return self._with_progress_high_water(prompt_id, {
                    "value": 0,
                    "max": max(1, int(expected_steps or 20)),
                    "percent": 0,
                    "status": "running",
                    "phase": "生成中",
                    "label": "生成中",
                })
        # 在等待队列中找
        for item in pending:
            if self._queue_item_prompt_id(item) == prompt_id:
                return self._with_progress_high_water(prompt_id, {
                    "value": 0,
                    "max": max(1, int(expected_steps or 20)),
                    "percent": 0,
                    "status": "pending",
                    "phase": "排队中",
                    "label": "排队中",
                })

        # 不在队列了 — 检查是否已完成
        history = self.get_history(prompt_id)
        if prompt_id in history:
            status = history[prompt_id].get("status", {})
            if status.get("status_str") == "error":
                messages = status.get("messages", [])
                detail = next((item[1] for item in reversed(messages)
                               if isinstance(item, (list, tuple)) and len(item) > 1
                               and item[0] in ("execution_error", "execution_interrupted")
                               and isinstance(item[1], dict)), {})
                reason = str(detail.get("exception_message") or "任务被中断").strip()
                return {"status": "failed", "value": 0, "max": 1,
                        "error": f"ComfyUI 节点 {detail.get('node_type', '')} 执行失败：{reason[:500]}"}
            return self._with_progress_high_water(prompt_id, {
                "value": 1,
                "max": 1,
                "percent": 100,
                "status": "completed",
                "phase": "已完成",
                "label": "已完成",
            })

        return None  # 不存在

    def get_output_files(self, history_item: Dict) -> list:
        outputs = history_item.get("outputs", {})
        files = []
        for node_id, node_output in outputs.items():
            if "images" in node_output:
                for img in node_output["images"]:
                    filename = img["filename"]
                    subfolder = img.get("subfolder", "")
                    type_ = img.get("type", "output")
                    files.append({
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": type_
                    })
            for key in ("text", "texts", "string", "strings", "value", "values", "result", "results", "json"):
                if key not in node_output:
                    continue
                value = node_output.get(key)
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if isinstance(item, (dict, list)):
                        text = json.dumps(item, ensure_ascii=False)
                    else:
                        text = str(item or "")
                    if text.strip():
                        files.append({
                            "type": "text",
                            "text": text,
                            "node_id": node_id,
                        })
            for key in ("files", "text_files"):
                for item in node_output.get(key, []) or []:
                    if isinstance(item, dict):
                        filename = item.get("filename") or item.get("file") or item.get("name")
                        subfolder = item.get("subfolder", "")
                        type_ = item.get("type", "output")
                    else:
                        filename = str(item or "")
                        subfolder = ""
                        type_ = "output"
                    if filename:
                        files.append({
                            "filename": filename,
                            "subfolder": subfolder,
                            "type": type_,
                        })
        return files
