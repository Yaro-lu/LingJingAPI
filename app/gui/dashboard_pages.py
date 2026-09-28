"""Product pages around the existing desktop gateway console.

The console itself remains in ``main_gateway.py``.  These pages deliberately
reuse the same visual tokens while presenting only actions that already exist
in the client.
"""

from __future__ import annotations

import json
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

from app.config import Config
from app.core.runtime_package import (
    REQUIRED_RUNTIME_PATHS,
    missing_runtime_paths,
)
from app.core.model_maintenance import MODEL_REQUIREMENTS
from app.core.workflow_dependencies import workflow_dependency_report
from app.workflow_registry import merge_workflow_catalog, read_local_workflow_catalog


BASE_DIR = Path(__file__).resolve().parents[2]


class RoundedTag(tk.Canvas):
    """Small auto-sized rounded label used for user-facing categories and states."""

    def __init__(self, parent, *, text: str, font, foreground: str, fill: str):
        try:
            parent_bg = parent.cget("bg")
        except Exception:
            parent_bg = "#ffffff"
        measured = tkfont.Font(font=font).measure(str(text))
        width = max(44, measured + 22)
        height = 24
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent_bg,
            bd=0,
            highlightthickness=0,
        )
        radius = 8
        points = [
            radius, 1, width - radius, 1, width - 1, 1,
            width - 1, radius, width - 1, height - radius,
            width - 1, height - 1, width - radius, height - 1,
            radius, height - 1, 1, height - 1,
            1, height - radius, 1, radius, 1, 1,
        ]
        self.create_polygon(
            points,
            smooth=True,
            splinesteps=16,
            fill=fill,
            outline="",
        )
        self.create_text(
            width / 2,
            height / 2,
            text=text,
            font=font,
            fill=foreground,
        )


def runtime_package_status(base_dir: Path) -> tuple[str, list[str]]:
    """Return ``ready``, ``repair`` or ``missing`` for the portable runtime."""
    base_dir = Path(base_dir)
    missing = missing_runtime_paths(base_dir)
    if not missing:
        return "ready", []
    present_count = sum(1 for path in REQUIRED_RUNTIME_PATHS if (base_dir / path).is_file())
    return ("repair" if present_count else "missing"), missing


class StaticDashboardPages:
    """Build and refresh the shared maintenance page and settings."""

    PAGE_BUILDERS = {
        "resources": "_build_resources",
        "settings": "_build_settings",
    }

    def __init__(self, app, colors: dict, fonts: dict):
        self.app = app
        self.c = colors
        self.f = fonts
        self._pages: dict[str, tk.Frame] = {}
        self._last_snapshot = ""
        self._resource_targets: dict[str, tk.Widget] = {}
        self._runtime_focus_job = None
        self._workflow_list_canvas = None
        self._storage_path_labels: dict[str, list[tuple[tk.Widget, int]]] = {}

    def build(self, parent, page_id: str) -> tk.Frame:
        if page_id not in self.PAGE_BUILDERS:
            raise KeyError(f"Unknown dashboard page: {page_id}")
        page = tk.Frame(parent, bg=self.c["bg"])
        self._pages[page_id] = page
        getattr(self, self.PAGE_BUILDERS[page_id])(page)
        return page

    def refresh(self, data: dict | None = None):
        """Refresh model/workflow cards only when their source state changes."""
        data = data if isinstance(data, dict) else {}
        workflows = data.get("workflows") if isinstance(data.get("workflows"), list) else []
        snapshot = json.dumps(
            {
                "workflows": [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "description": item.get("description"),
                        "capability": item.get("capability"),
                        "model_group": item.get("model_group"),
                        "workflow_type": item.get("workflow_type"),
                        "output_type": item.get("output_type"),
                        "type": item.get("type") or item.get("output_type"),
                        "enabled": item.get("enabled", True),
                        "available": item.get("available"),
                        "missing": item.get("missing_models") or item.get("missingModels") or item.get("missing"),
                        "missing_nodes": item.get("missing_nodes") or item.get("missingNodes"),
                        "required_models": item.get("required_models"),
                        "unverified_models": item.get("unverified_models"),
                        "required_nodes": item.get("required_nodes"),
                        "is_default": item.get("is_default", False),
                        "dependency_status": item.get("dependency_status"),
                        "validation_status": item.get("validation_status"),
                        "dependencies": item.get("dependencies"),
                        "input_schema": item.get("input_schema"),
                        "workflow_json": item.get("workflow_json"),
                    }
                    for item in workflows
                    if isinstance(item, dict)
                ],
                "models": getattr(self.app, "_model_status", {}),
                "runtime": self._runtime_status(),
                "environment_check": getattr(self.app, "_environment_status", {}),
                "api_key": bool(getattr(self.app, "_api_key", "")),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        for page_id in self.PAGE_BUILDERS:
            page = self._pages.get(page_id)
            if page is None:
                continue
            old_canvas = getattr(page, "_scroll_canvas", None)
            scroll_top = old_canvas.yview()[0] if old_canvas is not None else 0.0
            list_top = self._workflow_list_canvas.yview()[0] if page_id == "resources" and self._workflow_list_canvas is not None else 0.0
            for child in page.winfo_children():
                child.destroy()
            getattr(self, self.PAGE_BUILDERS[page_id])(page)
            canvas = getattr(page, "_scroll_canvas", None)
            if canvas is not None and scroll_top:
                def restore_scroll(target=canvas, top=scroll_top):
                    if target.winfo_exists():
                        target.update_idletasks()
                        target.yview_moveto(top)
                self.app.after_idle(restore_scroll)
            if page_id == "resources" and self._workflow_list_canvas is not None and list_top:
                def restore_list(target=self._workflow_list_canvas, top=list_top):
                    if target.winfo_exists():
                        target.update_idletasks()
                        target.yview_moveto(top)
                self.app.after_idle(restore_list)

    # ── shared pieces ──────────────────────────────────────
    def _body(self, page) -> tk.Frame:
        body = tk.Frame(page, bg=self.c["bg"])
        body.pack(fill="both", expand=True, padx=18, pady=(4, 14))
        return body

    def _scrollable_body(self, page) -> tk.Frame:
        """Return a full-width body that remains usable at the minimum window height."""
        canvas = tk.Canvas(
            page,
            bg=self.c["bg"],
            highlightthickness=0,
            bd=0,
        )
        scrollbar = self.app._vertical_scrollbar(page, canvas.yview)
        scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=(4, 14))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)

        content = tk.Frame(canvas, bg=self.c["bg"])
        body = tk.Frame(content, bg=self.c["bg"])
        body.pack(fill="x", expand=True, padx=18, pady=(4, 14))
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(content_window, width=event.width),
        )
        body._scroll_canvas = canvas
        body._scrollbar = scrollbar
        page._scroll_canvas = canvas
        self._bind_mousewheel_tree(content, canvas)
        return body

    @staticmethod
    def _mousewheel_units(event) -> int:
        """Translate Windows/macOS and X11 wheel events into canvas units."""
        button_number = getattr(event, "num", None)
        if button_number == 4:
            return -1
        if button_number == 5:
            return 1
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return 0
        magnitude = max(1, abs(delta) // 120)
        return -magnitude if delta > 0 else magnitude

    def _bind_mousewheel_tree(self, root, canvas) -> None:
        """Make wheel scrolling work when the pointer is over any child control."""
        def on_mousewheel(event):
            units = self._mousewheel_units(event)
            if units:
                canvas.yview_scroll(units, "units")
            return "break"

        stack = [root]
        while stack:
            widget = stack.pop()
            nested = getattr(widget, "_nested_scroll_canvas", None)
            if nested is not None and nested is not canvas:
                continue
            widget.bind("<MouseWheel>", on_mousewheel, add="+")
            widget.bind("<Button-4>", on_mousewheel, add="+")
            widget.bind("<Button-5>", on_mousewheel, add="+")
            stack.extend(widget.winfo_children())

    def _card(self, parent, height: int | None = None):
        card = self.app._card(parent)
        if height:
            card.configure(height=height)
            card.pack_propagate(False)
            card.grid_propagate(False)
        return card

    def _section_heading(self, parent, title: str, subtitle: str = "", actions=None):
        row = tk.Frame(parent, bg=self.c["bg"])
        row.pack(fill="x", pady=(0, 8))
        title_box = tk.Frame(row, bg=self.c["bg"])
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_box,
            text=title,
            font=self.f["title"],
            fg=self.c["text"],
            bg=self.c["bg"],
        ).pack(side="left")
        if subtitle:
            tk.Label(
                title_box,
                text=subtitle,
                font=self.f["small"],
                fg=self.c["muted"],
                bg=self.c["bg"],
            ).pack(side="left", padx=(12, 0), pady=(4, 0))
        if actions:
            action_box = tk.Frame(row, bg=self.c["bg"])
            action_box.pack(side="right")
            for index, (text, command, variant) in enumerate(actions):
                self.app._button(action_box, text, command, variant, width=112).pack(
                    side="left", padx=(0 if index == 0 else 8, 0)
                )
        return row

    def _badge(self, parent, text: str, tone: str = "neutral"):
        palette = {
            "primary": (self.c["soft_primary"], self.c["primary"]),
            "success": (self.c["soft_success"], self.c["success"]),
            "warn": (self.c["soft_warn"], self.c["warn"]),
            "danger": (self.c["soft_error"], self.c["error"]),
            "neutral": (self.c["hover"], self.c["text2"]),
        }
        bg, fg = palette.get(tone, palette["neutral"])
        return RoundedTag(
            parent,
            text=text,
            font=self.f["small"],
            foreground=fg,
            fill=bg,
        )

    def _metric(
        self,
        parent,
        column: int,
        label: str,
        value: str,
        note: str,
        tone: str = "primary",
        height: int = 86,
    ):
        card = self._card(parent, height)
        card.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0 if column == 0 else 5, 0 if column == 2 else 5),
        )
        accent = {
            "primary": self.c["primary"],
            "success": self.c["success"],
            "warn": self.c["warn"],
        }.get(tone, self.c["primary"])
        tk.Frame(card, bg=accent, width=4).pack(side="left", fill="y")
        content = tk.Frame(card, bg=self.c["card"])
        content.pack(side="left", fill="both", expand=True, padx=13, pady=7)
        tk.Label(content, text=label, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w")
        tk.Label(content, text=value, font=("Microsoft YaHei UI", 17, "bold"), fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
        tk.Label(content, text=note, font=self.f["tiny"], fg=self.c["muted"], bg=self.c["card"]).pack(anchor="w")

    def _divider(self, parent, pady=(8, 8)):
        tk.Frame(parent, bg=self.c["border2"], height=1).pack(fill="x", pady=pady)

    def _action(self, parent, text: str, command, variant: str = "plain", width: int = 88):
        return self.app._button(parent, text, command, variant, width=width)

    def _short_path(self, path: Path, limit: int = 46) -> str:
        text = str(path)
        if len(text) <= limit:
            return text
        return f"{text[:18]}...{text[-(limit - 21):]}"

    def _track_storage_path_label(
        self,
        name: str,
        widget: tk.Widget,
        limit: int,
    ):
        self._storage_path_labels.setdefault(name, []).append((widget, limit))

    def update_storage_path(self, name: str, path: Path):
        """Show a saved mapping immediately while the services await restart."""
        active = []
        for widget, limit in self._storage_path_labels.get(name, []):
            try:
                if not widget.winfo_exists():
                    continue
                widget.config(text=self._short_path(path, limit))
                active.append((widget, limit))
            except tk.TclError:
                continue
        self._storage_path_labels[name] = active

    @staticmethod
    def _short_text(value, limit: int = 24) -> str:
        text = str(value or "")
        return text if len(text) <= limit else f"{text[:max(1, limit - 1)]}…"

    # ── data helpers ───────────────────────────────────────
    def _runtime_ready(self) -> bool:
        return self._runtime_status()[0] == "ready"

    def _runtime_status(self) -> tuple[str, list[str]]:
        return runtime_package_status(BASE_DIR)

    def _set_card_outline(self, card, color: str):
        """Set a card outline without changing its size in CTk or fallback mode."""
        if hasattr(card, "_outline") and hasattr(card, "_draw_bg"):
            card._outline = color
            card._draw_bg()
            return
        card.configure(border_color=color)

    def focus_runtime_maintenance(self):
        """Briefly highlight the current runtime card after a console deep link."""
        card = self._resource_targets.get("runtime")
        try:
            if card is None or not card.winfo_exists():
                return
            if self._runtime_focus_job is not None:
                self.app.after_cancel(self._runtime_focus_job)
        except Exception:
            self._runtime_focus_job = None
            return

        self._set_card_outline(card, self.c["primary"])
        page = self._pages.get("resources")
        canvas = getattr(page, "_scroll_canvas", None)
        if canvas is not None:
            canvas.update_idletasks()
            bounds = canvas.bbox("all")
            if bounds and bounds[3] > 0:
                top = card.winfo_rooty() - canvas.winfo_rooty() + canvas.canvasy(0)
                canvas.yview_moveto(max(0, top - 36) / bounds[3])

        def restore(target=card):
            self._runtime_focus_job = None
            try:
                if target.winfo_exists() and self._resource_targets.get("runtime") is target:
                    self._set_card_outline(target, self.c["border2"])
            except Exception:
                pass

        self._runtime_focus_job = self.app.after(1600, restore)

    def cancel_pending(self):
        """Cancel page-owned timers before the root window is destroyed."""
        job, self._runtime_focus_job = self._runtime_focus_job, None
        if job is None:
            return
        try:
            self.app.after_cancel(job)
        except Exception:
            pass

    def _workflow_records(self) -> list[dict]:
        health = getattr(self.app, "_last_health", {})
        remote = health.get("workflows") if isinstance(health, dict) else None
        if not isinstance(remote, list):
            remote = []
        try:
            local = read_local_workflow_catalog(
                self.app._storage_directory("workflows"),
                BASE_DIR / "runtime" / "workflow_config.json",
            )
        except (OSError, ValueError):
            local = []
        return merge_workflow_catalog(local, remote)

    def _workflow_type_label(self, workflow: dict) -> tuple[str, str, str]:
        try:
            _key, label, color = self.app._workflow_capability_meta(workflow)
        except Exception:
            label, color = "自定义", self.c["text2"]
        if color == "#e8790c":
            tone = "warn"
        elif color == "#0f9f84":
            tone = "success"
        else:
            tone = "primary"
        return "■", label, tone

    def _workflow_state(self, workflow: dict) -> tuple[str, str, str, str]:
        if workflow.get("api_mapping_status") == "pending_conversion":
            return "待修复", "neutral", "原文件已保留，点击修复节点和模型", ""
        if workflow.get("manifest_error") or not workflow.get("workflow_json", True):
            return "文件异常", "danger", "工作流文件不完整", ""
        if not workflow.get("enabled", True):
            return "已停用", "neutral", "需要启用后才能调用", ""

        model_key = ""
        try:
            model_key = str(self.app._workflow_model_key(workflow) or "")
        except Exception:
            pass
        missing = []
        missing_nodes = []
        model_status = getattr(self.app, "_model_status", {})
        if model_key:
            missing = list((model_status.get("missing") or {}).get(model_key) or [])
        for key in ("missing_models", "missingModels", "missing"):
            value = workflow.get(key)
            if isinstance(value, (list, tuple)):
                missing.extend(str(item) for item in value if item)
        for key in ("missing_nodes", "missingNodes"):
            value = workflow.get(key)
            if isinstance(value, (list, tuple)):
                missing_nodes.extend(str(item) for item in value if item)

        dependency_status = str(workflow.get("dependency_status") or "").lower()
        dependencies = workflow.get("dependencies")
        if isinstance(dependencies, dict):
            report = workflow_dependency_report(
                dependencies,
                self.app._storage_directory("models"),
            )
            if not missing:
                missing.extend(report["missing_models"])
            if not dependency_status:
                dependency_status = report["dependency_status"]
        missing = list(dict.fromkeys(missing))
        if missing:
            detail = "、".join(missing[:2])
            if len(missing) > 2:
                detail += f" 等 {len(missing)} 个文件"
            return f"缺少 {len(missing)} 个模型", "warn", detail, model_key

        missing_nodes = list(dict.fromkeys(missing_nodes))
        if missing_nodes:
            detail = "、".join(missing_nodes[:2])
            if len(missing_nodes) > 2:
                detail += f" 等 {len(missing_nodes)} 个节点"
            return f"缺少 {len(missing_nodes)} 个节点", "danger", detail, ""

        if dependency_status in {"unknown", "unchecked", "unverified", ""} or workflow.get("nodes_verified") is False:
            return "加载中", "neutral", "正在检查模型和 ComfyUI 节点", ""

        try:
            available = bool(self.app._workflow_model_available(workflow))
        except Exception:
            available = True
        if not available:
            return "需要检查", "warn", "模型或依赖尚未准备完成", model_key
        return "可以使用", "success", "输入和输出已经配置完成", model_key

    # ── shared workflow and model list ─────────────────────
    def _model_summary(self, model_key):
        status = getattr(self.app, "_model_status", {})
        missing = list((status.get("missing") or {}).get(model_key) or [])
        if missing:
            return f"模型缺少 {len(missing)} 个文件", "warn", True
        if status.get(model_key) == "完整":
            return "模型完整", "success", False
        return "模型待检查", "neutral", True

    def _build_workflow_models(self, body):
        workflows = self._workflow_records()
        ready = sum(self._workflow_state(item)[0] == "可以使用" for item in workflows)
        self._section_heading(
            body, "工作流与模型", f"共 {len(workflows)} 个工作流 · {ready} 个可用",
            actions=[("添加教程", self.app._show_workflow_tutorial, "plain")],
        )
        toolbar = tk.Frame(body, bg=self.c["bg"])
        toolbar.pack(fill="x", pady=(0, 12))
        for index, (label, command, variant) in enumerate([
            ("＋ 添加工作流", self.app._show_workflow_upload_dialog, "primary"),
            ("导入已有模型", self.app._import_models, "plain"),
            ("重新检查", self.app._start_background_model_recheck, "plain"),
        ]):
            self._action(toolbar, label, command, variant, 112).pack(side="left", padx=(0 if index == 0 else 8, 0))

        listing = self._card(body, min(340, max(108, len(workflows) * 76 + 12)))
        listing.pack(fill="x", pady=(0, 18))
        canvas = tk.Canvas(listing, bg=self.c["card"], highlightthickness=0, bd=0)
        scrollbar = self.app._vertical_scrollbar(listing, canvas.yview)
        scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=6)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=3, pady=4)
        rows = tk.Frame(canvas, bg=self.c["card"])
        window = canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        listing._nested_scroll_canvas = canvas
        self._workflow_list_canvas = canvas
        represented_models = set()
        if not workflows:
            tk.Label(rows, text="还没有工作流，点击“添加工作流”即可开始。", font=self.f["normal"],
                     fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w", padx=16, pady=20)
        for index, workflow in enumerate(workflows):
            model_key = str(self.app._workflow_model_key(workflow) or "")
            represented_models.add(model_key)
            state, tone, detail, _ = self._workflow_state(workflow)
            _glyph, kind, kind_tone = self._workflow_type_label(workflow)
            row = tk.Frame(rows, bg=self.c["card"])
            row.pack(fill="x", padx=12, pady=8)
            actions = tk.Frame(row, bg=self.c["card"])
            actions.pack(side="right", padx=(12, 0))
            wf_id = str(workflow.get("id") or "")
            enabled = bool(workflow.get("enabled", True))
            if enabled and state not in {"待修复", "文件异常", "需要检查"} and not state.startswith("缺少") and not workflow.get("is_default"):
                self._action(actions, "设为默认", lambda key=wf_id: self.app._set_default_workflow(key),
                             "plain", 66).pack(side="left", padx=(0, 5))
            if state == "待修复":
                self._action(actions, "修复", lambda item=dict(workflow): self.app._show_workflow_repair(item),
                             "plain", 84).pack(side="left")
            else:
                self._action(actions, "停用" if enabled else "启用",
                             lambda key=wf_id, value=not enabled: self.app._set_workflow_enabled(key, value),
                             "plain" if enabled else "primary", 52).pack(side="left", padx=(0, 5))
                self._action(actions, "配置 / 详情", lambda item=dict(workflow): self.app._show_workflow_schema(item),
                             "plain", 84).pack(side="left")
            # Every workflow can resolve downloads or map existing local files.
            self._action(actions, "模型文件", lambda item=dict(workflow): self.app._show_workflow_model_help(item),
                         "primary" if state.startswith("缺少") else "plain", 76).pack(side="left", padx=(5, 0))

            text_box = tk.Frame(row, bg=self.c["card"], width=160, height=58)
            text_box.pack(side="left", fill="x", expand=True)
            text_box.pack_propagate(False)
            heading = tk.Frame(text_box, bg=self.c["card"])
            heading.pack(fill="x")
            self._badge(heading, "待修复" if state == "待修复" else kind,
                        "neutral" if state == "待修复" else kind_tone).pack(side="left", padx=(0, 7))
            if workflow.get("is_default"):
                self._badge(heading, "默认", "primary").pack(side="right", padx=(5, 0))
            name_label = tk.Label(heading, text=self._short_text(workflow.get("name") or wf_id, 30), font=self.f["bold"],
                                  fg=self.c["text"], bg=self.c["card"], anchor="w")
            name_label.pack(side="left", fill="x", expand=True)
            if state == "待修复":
                name_label.configure(cursor="hand2")
                name_label.bind("<Button-1>", lambda _event, item=dict(workflow): self.app._show_workflow_repair(item))
            tk.Label(text_box, text=self._short_text(workflow.get("description") or "暂无简介", 56), font=self.f["small"],
                     fg=self.c["text2"], bg=self.c["card"], anchor="w").pack(fill="x", pady=(2, 0))
            model_note, _, _ = self._model_summary(model_key) if model_key else ("", "neutral", False)
            note = " · ".join(part for part in (state, model_note, detail) if part)
            tk.Label(text_box, text=self._short_text(note, 62), font=self.f["tiny"],
                     fg=self.c["warn"] if tone in {"warn", "danger"} else self.c["muted"],
                     bg=self.c["card"], anchor="w").pack(fill="x")
            if index < len(workflows) - 1:
                tk.Frame(rows, bg=self.c["border2"], height=1).pack(fill="x", padx=12)
        self._bind_mousewheel_tree(listing, canvas)

        remaining = [key for key in MODEL_REQUIREMENTS if key not in represented_models]
        if remaining:
            self._section_heading(body, "其他模型", "未关联当前工作流的模型仍可单独维护")
            other = self._card(body)
            other.pack(fill="x", pady=(0, 20))
            for index, key in enumerate(remaining):
                row = tk.Frame(other, bg=self.c["card"])
                row.pack(fill="x", padx=16, pady=10)
                label, tone, needs_download = self._model_summary(key)
                if needs_download:
                    self._action(row, "下载模型", lambda item=key: self.app._show_model_install_help(item),
                                 "primary", 94).pack(side="right", padx=(12, 0))
                self._badge(row, label, tone).pack(side="right", padx=(12, 0))
                tk.Label(row, text=str(MODEL_REQUIREMENTS[key].get("title") or key), font=self.f["bold"],
                         fg=self.c["text"], bg=self.c["card"], anchor="w").pack(side="left", fill="x", expand=True)
                if index < len(remaining) - 1:
                    tk.Frame(other, bg=self.c["border2"], height=1).pack(fill="x", padx=16)

    # ── models and environment ─────────────────────────────
    def _build_resources(self, page):
        body = self._scrollable_body(page)
        # Maintenance sections share this scrollable page.
        body.pack_configure(pady=(4, 12))
        runtime_state, runtime_missing = self._runtime_status()
        runtime_ok = runtime_state == "ready"
        environment_check = getattr(self.app, "_environment_status", {})
        model_status = getattr(self.app, "_model_status", {})
        model_keys = tuple(MODEL_REQUIREMENTS)
        ready_models = sum(1 for key in model_keys if model_status.get(key) == "完整")
        missing_count = sum(len((model_status.get("missing") or {}).get(key) or []) for key in model_keys)

        metrics = tk.Frame(body, bg=self.c["bg"])
        metrics.pack(fill="x", pady=(0, 14))
        for col in range(3):
            metrics.columnconfigure(col, weight=1, uniform="resource_metrics")
        runtime_metric = {
            "ready": ("已就绪", "环境文件完整", "success"),
            "repair": ("需要修复", f"缺少 {len(runtime_missing)} 项", "warn"),
            "missing": ("需要安装", "首次使用先安装", "warn"),
        }[runtime_state]
        if runtime_state == "ready" and environment_check and not environment_check.get("ready"):
            runtime_metric = ("需要检查", "文件完整，运行检查未通过", "warn")
        self._metric(metrics, 0, "运行环境", *runtime_metric)
        self._metric(metrics, 1, "模型组", f"{ready_models}/{len(model_keys)}", "按工作流自动检查", "success" if ready_models == len(model_keys) else "warn")
        self._metric(metrics, 2, "缺少文件", f"{missing_count} 个", "只下载实际需要的内容", "warn" if missing_count else "success")

        self._build_workflow_models(body)

        self._section_heading(body, "运行环境维护", "安装一次，之后由客户端自动启动")
        runtime_card = self._card(body, 78)
        self._resource_targets["runtime"] = runtime_card
        runtime_card.pack(fill="x", pady=(0, 14))
        runtime_left = tk.Frame(runtime_card, bg=self.c["card"])
        runtime_left.pack(side="left", fill="both", expand=True, padx=16, pady=12)
        title_row = tk.Frame(runtime_left, bg=self.c["card"])
        title_row.pack(fill="x")
        tk.Label(title_row, text="本地生成环境", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
        runtime_check_failed = runtime_ok and environment_check and not environment_check.get("ready")
        badge_text = {"ready": "环境完整", "repair": "需要修复", "missing": "尚未安装"}[runtime_state]
        if runtime_check_failed:
            badge_text = "运行需处理"
        self._badge(title_row, badge_text, "warn" if runtime_check_failed or not runtime_ok else "success").pack(side="left", padx=(10, 0))
        if runtime_state == "ready" and environment_check:
            if environment_check.get("ready"):
                gpu_name = str(environment_check.get("gpu_name") or "显卡环境正常")
                runtime_note = f"检查通过 · {gpu_name}"
            else:
                runtime_note = str(environment_check.get("message") or "环境文件完整，运行检查需要处理。")
        elif runtime_state == "ready":
            runtime_note = "环境文件完整；可检查显卡与 PyTorch，或执行修复更新。"
        elif runtime_state == "repair":
            short_missing = "、".join(Path(item).name for item in runtime_missing[:2])
            runtime_note = f"环境不完整，缺少 {len(runtime_missing)} 项：{short_missing}{'…' if len(runtime_missing) > 2 else ''}"
        else:
            runtime_note = "首次使用先安装运行环境；模型文件继续单独管理，不会重复下载。"
        tk.Label(runtime_left, text=runtime_note, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w", pady=(6, 0))

        runtime_actions = tk.Frame(runtime_card, bg=self.c["card"])
        runtime_actions.pack(side="right", padx=16)
        if runtime_ok:
            self._action(runtime_actions, "检查环境", self.app._start_background_runtime_recheck, "primary", 88).pack(side="left")
            self._action(runtime_actions, "修复 / 更新", self.app._show_runtime_maintenance, "plain", 94).pack(side="left", padx=(8, 0))
            self._action(runtime_actions, "打开目录", self.app._open_runtime_dir, "plain", 82).pack(side="left", padx=(8, 0))
        else:
            self._action(runtime_actions, "一键修复", self.app._install_runtime_from_mirror, "primary", 88).pack(side="left")
            self._action(runtime_actions, "本地安装包", self.app._select_runtime, "plain", 94).pack(side="left", padx=(8, 0))
            self._action(runtime_actions, "更多方式", self.app._show_runtime_maintenance, "plain", 82).pack(side="left", padx=(8, 0))

        self._section_heading(body, "组件更新", "修复运行环境或更新内置 ComfyUI")
        maintenance = self._card(body, 94)
        maintenance.pack(fill="x", pady=(0, 14))
        maintenance_left = tk.Frame(maintenance, bg=self.c["card"])
        maintenance_left.pack(side="left", fill="both", expand=True, padx=16, pady=13)
        tk.Label(
            maintenance_left,
            text="ComfyUI 核心维护",
            font=self.f["h2"],
            fg=self.c["text"],
            bg=self.c["card"],
        ).pack(anchor="w")
        tk.Label(
            maintenance_left,
            text="只更新 ComfyUI 核心，不影响模型、自定义节点和用户数据。",
            font=self.f["small"],
            fg=self.c["text2"],
            bg=self.c["card"],
        ).pack(anchor="w", pady=(6, 0))
        maintenance_actions = tk.Frame(maintenance, bg=self.c["card"])
        maintenance_actions.pack(side="right", padx=16)
        self._action(
            maintenance_actions,
            "修复运行环境",
            self.app._show_runtime_maintenance,
            "plain",
            104,
        ).pack(side="left", padx=(0, 8))
        self._action(
            maintenance_actions,
            "一键更新 ComfyUI",
            self.app._start_comfyui_update,
            "primary",
            132,
        ).pack(side="left")

        storage = self._card(body, 74)
        storage.pack(fill="x")
        paths = [
            (
                "models",
                "模型",
                self.app._storage_directory("models"),
                self.app._open_models,
            ),
            (
                "workflows",
                "工作流",
                self.app._storage_directory("workflows"),
                self.app._open_workflows_dir,
            ),
            (
                "outputs",
                "生成结果",
                self.app._storage_directory("outputs"),
                self.app._open_outputs,
            ),
        ]
        for index, (name, label, path, command) in enumerate(paths):
            cell = tk.Frame(storage, bg=self.c["card"])
            cell.pack(side="left", fill="both", expand=True, padx=(16 if index == 0 else 8, 8), pady=8)
            cell_head = tk.Frame(cell, bg=self.c["card"])
            cell_head.pack(fill="x")
            tk.Label(cell_head, text=f"{label}位置", font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            self._action(cell_head, "打开", command, "plain", 66).pack(side="right")
            self._action(cell_head, "修改", lambda key=name: self.app._choose_storage_directory(key), "plain", 54).pack(side="right", padx=(0, 4))
            path_label = tk.Label(
                cell,
                text=self._short_path(path, 30),
                font=self.f["tiny"],
                fg=self.c["muted"],
                bg=self.c["card"],
            )
            path_label.pack(anchor="w", pady=(3, 0))
            self._track_storage_path_label(name, path_label, 30)

        self._bind_mousewheel_tree(body, body._scroll_canvas)

    # ── settings ───────────────────────────────────────────
    def _build_settings(self, page):
        body = self._scrollable_body(page)
        self._section_heading(body, "连接与安全", "管理其他软件连接本客户端时使用的密钥")
        access = self._card(body, 94)
        access.pack(fill="x", pady=(0, 14))
        access_left = tk.Frame(access, bg=self.c["card"])
        access_left.pack(side="left", fill="both", expand=True, padx=16, pady=13)
        tk.Label(access_left, text="访问密钥", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
        key = str(getattr(self.app, "_api_key", "") or "")
        masked = f"{key[:12]}{'•' * 12}{key[-6:]}" if len(key) > 20 else "服务启动后自动生成"
        self.app._settings_key_label = tk.Label(access_left, text=masked, font=self.f["mono"], fg=self.c["primary"], bg=self.c["card"])
        self.app._settings_key_label.pack(anchor="w", pady=(5, 0))
        tk.Label(access_left, text="密钥只保存在本机；请不要发送给不信任的人。", font=self.f["tiny"], fg=self.c["muted"], bg=self.c["card"]).pack(anchor="w", pady=(3, 0))
        access_actions = tk.Frame(access, bg=self.c["card"])
        access_actions.pack(side="right", padx=16)
        self._action(access_actions, "修改密钥", self.app._edit_api_key, "plain", 84).pack(side="left", padx=(0, 8))

        self._section_heading(body, "文件位置", "快速打开模型、工作流、生成结果和日志")
        paths_card = self._card(body, 188)
        paths_card.pack(fill="x", pady=(0, 14))
        path_rows = [
            (
                "models",
                "模型",
                self.app._storage_directory("models"),
                self.app._open_models,
            ),
            (
                "workflows",
                "工作流",
                self.app._storage_directory("workflows"),
                self.app._open_workflows_dir,
            ),
            (
                "outputs",
                "生成结果",
                self.app._storage_directory("outputs"),
                self.app._open_outputs,
            ),
            (
                "logs",
                "运行日志",
                self.app._storage_directory("logs"),
                self.app._open_logs_dir,
            ),
        ]
        for index, (name, label, path, command) in enumerate(path_rows):
            row = tk.Frame(paths_card, bg=self.c["card"])
            row.pack(fill="x", padx=16, pady=(9 if index == 0 else 5, 5))
            tk.Label(row, text=label, width=10, anchor="w", font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            path_label = tk.Label(
                row,
                text=self._short_path(path, 56),
                anchor="w",
                font=self.f["mono"],
                fg=self.c["text2"],
                bg=self.c["card"],
            )
            path_label.pack(side="left", fill="x", expand=True)
            self._track_storage_path_label(name, path_label, 56)
            actions = tk.Frame(row, bg=self.c["card"])
            actions.pack(side="right")
            self._action(
                actions,
                "设置",
                lambda item=name: self.app._choose_storage_directory(item),
                "plain",
                66,
            ).pack(side="left", padx=(0, 8))
            self._action(actions, "打开", command, "plain", 66).pack(side="left")
            if index < len(path_rows) - 1:
                tk.Frame(paths_card, bg=self.c["border2"], height=1).pack(fill="x", padx=16)

        lower = tk.Frame(body, bg=self.c["bg"])
        lower.pack(fill="x")
        lower.columnconfigure(0, weight=1, uniform="settings_lower")
        lower.columnconfigure(1, weight=1, uniform="settings_lower")

        behavior = self._card(lower, 168)
        behavior.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(behavior, text="使用方式", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w", padx=16, pady=(13, 7))
        behavior_rows = [
            ("关闭主窗口", "停止全部后台服务"),
            ("最小化窗口", "继续后台运行"),
            ("任务并发", "1 个任务"),
            ("单实例运行", "已开启"),
        ]
        for label, value in behavior_rows:
            row = tk.Frame(behavior, bg=self.c["card"])
            row.pack(fill="x", padx=16, pady=3)
            tk.Label(row, text=label, font=self.f["small"], fg=self.c["muted"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=value, font=self.f["small"], fg=self.c["text"], bg=self.c["card"]).pack(side="right")

        advanced = self._card(lower, 168)
        advanced.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        head = tk.Frame(advanced, bg=self.c["card"])
        head.pack(fill="x", padx=16, pady=(13, 7))
        tk.Label(head, text="高级设置", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
        self._badge(head, "高级", "primary").pack(side="right")
        local_config = Config(BASE_DIR)
        advanced_rows = [
            ("本地接口", f"端口 {local_config.server_port}"),
            ("ComfyUI", f"端口 {str(local_config.comfyui_url).rsplit(':', 1)[-1]}"),
            ("公网连接", "启动后自动建立"),
        ]
        for label, value in advanced_rows:
            row = tk.Frame(advanced, bg=self.c["card"])
            row.pack(fill="x", padx=16, pady=3)
            tk.Label(row, text=label, font=self.f["small"], fg=self.c["muted"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=value, font=self.f["small"], fg=self.c["text"], bg=self.c["card"]).pack(side="right")
        self._action(advanced, "编辑 TXT 设置", self.app._open_runtime_config, "plain", 112).pack(anchor="e", padx=16, pady=(7, 0))

        self._bind_mousewheel_tree(body, body._scroll_canvas)
