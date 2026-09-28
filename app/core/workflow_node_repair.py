"""Install workflow node packs using the official Manager CLI, without a browser."""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

MANAGER_URL = "https://github.com/Comfy-Org/ComfyUI-Manager.git"
CHANNEL = "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main"


def node_inventory(workflow):
    nodes = list(workflow.get("nodes") or [])
    definitions = workflow.get("definitions") or {}
    if isinstance(definitions, dict):
        for graph in definitions.get("subgraphs") or []:
            if isinstance(graph, dict):
                nodes.extend(graph.get("nodes") or [])
    types = sorted({str(node.get("type")) for node in nodes
                    if isinstance(node, dict) and isinstance(node.get("type"), str)})
    # Only node types go to dependency discovery. Workflow properties cannot
    # supply arbitrary repository URLs, installation commands or version pins.
    return {"nodes": [{"id": i, "type": name} for i, name in enumerate(types)], "links": []}


def install_workflow_nodes(core, python, workflow, supervisor, env, progress, cancelled=lambda: False):
    python = Path(python)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    core = Path(core).resolve(strict=True)
    custom = core / "custom_nodes"
    custom.mkdir(exist_ok=True)
    if custom.is_symlink() or custom.resolve().parent != core:
        raise ValueError("节点目录必须位于当前 ComfyUI 目录内")
    git = shutil.which("git", path=env.get("PATH"))
    if not git:
        raise ValueError("未找到 Git，无法下载节点插件；安装 Git 后可重新一键修复")
    temp_root = core.parent / "temp"
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="node-repair-", dir=temp_root) as temp:
        temp = Path(temp)
        process_env = {**env, "GIT_TERMINAL_PROMPT": "0", "PYTHONUTF8": "1",
                       "PYTHONNOUSERSITE": "1", "PIP_NO_INPUT": "1",
                       "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
                       "PIP_DISABLE_PIP_VERSION_CHECK": "1", "COMFYUI_PATH": str(core)}
        def run(args, stage, timeout=900):
            if cancelled():
                raise ValueError("客户端正在退出，修复已停止")
            progress(stage)
            result = supervisor.run("workflow-node-repair", args, cwd=str(core),
                                    env=process_env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
            if result.returncode:
                raise ValueError(stage + "失败：" + (result.stderr or result.stdout or "未知错误")[-1800:])
            return result
        # Prevent plugin installers from replacing the core GPU stack.
        snapshot = run([str(python), "-s", "-B", "-c",
                        "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions() if d.metadata.get('Name')}))"],
                       "检查当前 Python 环境", 40)
        versions = json.loads(snapshot.stdout)
        protected = {"torch", "torchvision", "torchaudio", "numpy", "xformers"}
        constraints = temp / "constraints.txt"
        constraints.write_text("\n".join(f"{name}=={version}" for name, version in versions.items()
                                          if name.lower() in protected), encoding="utf-8")
        process_env["PIP_CONSTRAINT"] = str(constraints)
        managers = [p for p in custom.iterdir() if p.is_dir() and not p.is_symlink()
                    and p.resolve().parent == custom.resolve() and (p / "cm-cli.py").is_file()]
        manager = next(iter(sorted(managers)), None)
        if manager is None:
            manager = custom / "ComfyUI-Manager"
            if manager.exists():
                raise ValueError("ComfyUI-Manager 目录不完整，请检查目录，未覆盖现有文件")
            staged = temp / "ComfyUI-Manager"
            run([git, "-c", "core.hooksPath=/dev/null", "clone", "--depth", "1", MANAGER_URL, str(staged)],
                "准备官方节点管理组件", 180)
            if not (staged / "cm-cli.py").is_file():
                raise ValueError("官方管理组件缺少命令行入口")
            os.replace(staged, manager)
        requirements = manager / "requirements.txt"
        if requirements.is_file():
            run([str(python), "-s", "-B", "-m", "pip", "install", "--no-input", "--no-cache-dir",
                 "-r", str(requirements)], "准备节点管理依赖")
        cli = [str(python), "-s", "-B", str(manager / "cm-cli.py")]
        inventory = temp / "workflow.json"
        inventory.write_text(json.dumps(node_inventory(workflow)), encoding="utf-8")
        dependency_file = temp / "dependencies.json"
        run([*cli, "deps-in-workflow", "--workflow", str(inventory), "--output", str(dependency_file),
             "--mode", "remote", "--channel", CHANNEL], "匹配节点插件来源", 180)
        dependencies = json.loads(dependency_file.read_text(encoding="utf-8"))
        repositories = dependencies.get("custom_nodes")
        if not isinstance(repositories, dict):
            raise ValueError("节点管理器返回的依赖格式无效")
        for url in repositories:
            if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", url):
                raise ValueError("节点来源不支持自动安装：" + str(url))
        run([*cli, "install-deps", str(dependency_file), "--mode", "remote", "--channel", CHANNEL],
            "安装缺失节点及依赖", 1800)
        # install-deps skips already present packs; retry their dependency setup
        # as an existing plugin may have failed to import.
        for url, metadata in repositories.items():
            if isinstance(metadata, dict) and metadata.get("state") == "installed":
                name = url.rstrip("/").split("/")[-1].removesuffix(".git")
                folder = custom / name
                if folder.is_dir() and folder.resolve().parent == custom.resolve() and not folder.is_symlink():
                    run([*cli, "post-install", str(folder)], "修复节点依赖：" + name)
        unknown = dependencies.get("unknown_nodes") or []
        return [str(name) for name in unknown]
