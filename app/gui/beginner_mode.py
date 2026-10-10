"""An extra simple view; the original expert dashboard stays alive unchanged."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
import webbrowser

from app.core.beginner_mode import (
    automatic_repair_profile, beginner_service_state, build_example_launch_url,
    save_interface_preferences,
    beginner_download_summary,
)


class BeginnerModeMixin:
    def _build_mode_switch(self, colors, fonts):
        self._beginner_colors, self._beginner_fonts = colors, fonts
        bar = tk.Frame(self, bg=colors["surface"], height=48)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        switch = tk.Frame(bar, bg=colors["surface"])
        switch.place(relx=0.5, rely=0.5, anchor="center")
        self._mode_buttons = {}
        for mode, label in (("beginner", "新手模式"), ("expert", "专家模式")):
            button = tk.Button(
                switch, text=label, font=fonts["bold"], relief="flat", bd=0,
                padx=24, pady=6, cursor="hand2",
                command=lambda value=mode: self._set_interface_mode(value),
            )
            button.pack(side="left", padx=3)
            self._mode_buttons[mode] = button

    def _build_beginner_page(self):
        c, f = self._beginner_colors, self._beginner_fonts
        page = self._beginner_page = tk.Frame(self, bg=c["bg"])
        brand = tk.Frame(page, bg=c["bg"])
        brand.pack(fill="x", padx=36, pady=(22, 16))
        tk.Label(brand, text="LingJingAPI", font=f["title"], fg=c["text"], bg=c["bg"]).pack(side="left")
        tk.Label(brand, text="一键调用算力，简单好用。", font=f["small"], fg=c["muted"], bg=c["bg"]).pack(side="left", padx=14)

        status = self._card(page, fill="x", padx=36, pady=(0, 14))
        self._beginner_status_label = tk.Label(status, text="启动中…", font=f["title"], fg=c["warn"], bg=c["card"])
        self._beginner_status_label.pack(anchor="center", pady=(22, 5))
        self._beginner_status_detail = tk.Label(status, text="正在检查环境并启动服务。", font=f["normal"], fg=c["text2"], bg=c["card"], wraplength=850)
        self._beginner_status_detail.pack(anchor="center", pady=(0, 19))
        self._beginner_repair_button = self._button(status, "一键修复", self._open_beginner_repair, "primary", width=160)

        middle = tk.Frame(page, bg=c["bg"])
        middle.pack(fill="both", expand=True, padx=36, pady=(12, 24))
        middle.columnconfigure(0, weight=1)
        middle.columnconfigure(1, weight=5)
        middle.columnconfigure(2, weight=1)
        panel = self._card(middle)
        panel.grid(row=0, column=1, sticky="ew")
        tk.Label(panel, text="服务连接信息", font=f["h2"], fg=c["text"], bg=c["card"]).pack(anchor="w", padx=26, pady=(24, 18))
        self._beginner_url_var = tk.StringVar(self, value="服务启动后自动生成")
        self._beginner_key_var = tk.StringVar(self, value="")
        for label, variable in (("URL", self._beginner_url_var), ("Key", self._beginner_key_var)):
            tk.Label(panel, text=label, font=f["bold"], fg=c["text"], bg=c["card"]).pack(anchor="w", padx=26, pady=(4, 7))
            row = tk.Frame(panel, bg=c["card"])
            row.pack(fill="x", padx=26, pady=(0, 15))
            entry = tk.Entry(
                row, textvariable=variable, state="readonly", readonlybackground=c["entry"],
                fg=c["text"], relief="flat", highlightthickness=1,
                highlightbackground=c["border2"], font=f["normal"],
                show="•" if label == "Key" else "",
            )
            if label == "Key":
                self._beginner_key_entry = entry
                self._beginner_reveal_button = self._button(row, "显示", self._toggle_beginner_key, width=65)
                self._beginner_reveal_button.pack(side="right", padx=(6, 0))
            button = self._button(row, "复制", lambda v=variable: self._copy_beginner_value(v), width=65)
            button.pack(side="right", padx=(9, 0))
            entry.pack(side="left", fill="x", expand=True, ipady=10)
        self._beginner_create_button = self._button(panel, "开始创作", self._open_beginner_example, "primary", width=200)
        self._beginner_create_button.pack(fill="x", padx=26, pady=(8, 10), ipady=8)
        self._beginner_feedback = tk.StringVar(self, value="打开示例页面，自动连接当前服务")
        tk.Label(panel, textvariable=self._beginner_feedback, font=f["small"], fg=c["muted"], bg=c["card"], wraplength=620).pack(padx=26, pady=(0, 24))
        self._refresh_beginner_view()

    def _save_interface_preferences(self):
        try:
            save_interface_preferences(self._interface_preferences_path, self._interface_preferences)
        except OSError:
            # An unwritable preference file must not prevent generation or mode switching.
            feedback = self.__dict__.get("_beginner_feedback")
            if feedback is not None:
                feedback.set("当前模式可正常使用，但无法保存；下次启动可能恢复默认模式。")

    def _set_interface_mode(self, mode: str, *, persist: bool = True):
        if mode not in ("beginner", "expert"):
            return
        self._ui_mode = mode
        self._interface_preferences["mode"] = mode
        if mode == "beginner":
            self._app_shell.pack_forget()
            self._beginner_page.pack(fill="both", expand=True)
        else:
            self._beginner_page.pack_forget()
            self._app_shell.pack(fill="both", expand=True)
        c = self._beginner_colors
        for value, button in self._mode_buttons.items():
            selected = value == mode
            button.configure(bg=c["primary"] if selected else c["surface"], fg="#ffffff" if selected else c["text2"], activebackground=c["soft_primary"], activeforeground=c["primary"])
        if persist:
            self._save_interface_preferences()
        self._refresh_beginner_view()

    def _beginner_state(self):
        preferences = self._interface_preferences
        profile = preferences.get("profile") or automatic_repair_profile(
            self._environment_status.get("vram_mb", 0), self._quick_repair_state.get("pending_profile", "")
        )
        run = self._quick_repair_run or {}
        return beginner_service_state(
            environment=self._environment_status, lights=self._light_states,
            health=self._last_health, profile=profile,
            onboarding_complete=preferences.get("onboarding_complete", False),
            api_key_ready=bool(self._api_key), repair_phase=run.get("phase", ""),
            maintaining=self._runtime_maintenance_in_progress, start_blocked=self._runtime_start_blocked,
            backend_error=self.__dict__.get("_beginner_backend_error", ""),
        )

    def _refresh_beginner_view(self):
        if "_beginner_status_label" not in self.__dict__:
            return
        state, title, detail = self._beginner_state()
        c = self._beginner_colors
        self._beginner_status_label.configure(text=title, fg=c["success"] if state == "ready" else c["error"] if state == "failed" else c["warn"])
        self._beginner_status_detail.configure(text=detail)
        if state in ("repair", "failed"):
            self._beginner_repair_button.pack(pady=(0, 18))
        else:
            self._beginner_repair_button.pack_forget()
        self._beginner_create_button.configure(state="normal" if state == "ready" else "disabled")
        url = self._tunnel_url or self.__dict__.get("_lan_url") or self._local_url
        self._beginner_url_var.set(url if self._api_key else "服务启动后自动生成")
        self._beginner_key_var.set(self._api_key)

    def _open_beginner_repair(self):
        if self._shutting_down:
            return
        self._quick_repair_prompt_seen = True
        if self._runtime_start_blocked and "ready" not in self._environment_status:
            # An interrupted updater deliberately blocks all service probes.
            # Route a confirmed repair through runtime replacement instead.
            self._environment_status = {"ready": False, "package_ready": False}
        profile = automatic_repair_profile(
            self.__dict__.get("_beginner_vram_mb", 0) or self._environment_status.get("vram_mb", 0),
            self._quick_repair_state.get("pending_profile", ""),
        )
        self._show_quick_repair_dialog(initial_profile=profile, simplified=True)

    def _show_beginner_startup_repair(self, profile, vram_mb, pending, environment_ready, qwen_ready):
        if self._shutting_down:
            return
        beginner = self.__dict__.get("_ui_mode") == "beginner"
        # Mode may have changed while this delayed startup callback was queued.
        if not pending and environment_ready and qwen_ready:
            return
        self._show_quick_repair_dialog(
            initial_profile=profile, vram_mb=vram_mb,
            auto_resume=bool(pending and (beginner or environment_ready)), simplified=beginner,
        )

    def _attach_beginner_runtime_download(self, dialog, plan):
        if not dialog["popup"].winfo_exists():
            return
        self._center_popup(dialog["popup"], 620, 430)
        c, f = self._beginner_colors, self._beginner_fonts
        tk.Label(dialog["content"], text=beginner_download_summary(plan),
                 font=f["small"], fg=c["text2"], bg=c["card"], justify="left", wraplength=510).pack(fill="x", pady=(8, 4))
        dialog["total_download_var"] = tk.DoubleVar(value=0)
        dialog["total_download_label"] = tk.StringVar()
        tk.Label(dialog["content"], textvariable=dialog["total_download_label"],
                 font=f["small"], fg=c["text2"], bg=c["card"]).pack(anchor="w")
        ttk.Progressbar(dialog["content"], maximum=100, variable=dialog["total_download_var"],
                        style="Progress.Horizontal.TProgressbar").pack(fill="x", pady=(4, 0))
        self._update_beginner_runtime_download(dialog, plan, plan["runtime_have"])

    def _update_beginner_runtime_download(self, dialog, plan, runtime_done):
        if "total_download_var" not in dialog or not dialog["popup"].winfo_exists():
            return
        done = min(plan["runtime_bytes"], max(0, runtime_done)) + plan["model_have"]
        percent = done * 100 / max(1, plan["total_bytes"])
        dialog["total_download_var"].set(percent)
        dialog["total_download_label"].set(f"总下载进度 {percent:.1f}% · {done / 1024**3:.2f} / {plan['total_bytes'] / 1024**3:.2f} GB")

    def _mark_beginner_prepared(self, profile: str):
        if "_interface_preferences" not in self.__dict__:
            return
        self._interface_preferences.update(onboarding_complete=True, profile=profile)
        self._save_interface_preferences()
        self._refresh_beginner_view()

    def _close_completed_beginner_repair(self, run, dialog):
        if self._quick_repair_run is run and self._quick_repair_popup is dialog and run.get("phase") == "complete":
            self._close_quick_repair_dialog()

    def _copy_beginner_value(self, variable):
        value = variable.get()
        if not self._api_key or not value:
            return
        self._copy(value)
        self._beginner_feedback.set("已复制；请妥善保管 Key，不要公开分享。")

    def _toggle_beginner_key(self):
        revealed = not bool(self._beginner_key_entry.cget("show"))
        self._beginner_key_entry.configure(show="•" if revealed else "")
        self._beginner_reveal_button.configure(text="显示" if revealed else "隐藏")

    def _open_beginner_example(self):
        if self._beginner_state()[0] != "ready":
            self._refresh_beginner_view()
            return
        try:
            url = build_example_launch_url(self._local_url, self._api_key)
            if not webbrowser.open(url):
                self._beginner_feedback.set("无法自动打开浏览器，请用 URL 与 Key 手动连接。")
        except (ValueError, OSError):
            self._beginner_feedback.set("无法打开示例页面，请检查本机服务状态。")
