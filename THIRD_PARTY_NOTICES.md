# 第三方组件说明 / Third-Party Notices

LingJingAPI 中由项目作者拥有版权并有权授权的部分采用 Apache License 2.0。本文件用于提示主要第三方组件及其许可证来源；Apache License 2.0 不会覆盖这些第三方内容，也不替代各组件随包附带的完整许可证文本。

主要组件包括：

| 组件 | 当前用途 | 许可证/许可标识 |
| --- | --- | --- |
| Python 3.13 | 便携解释器 | Python Software Foundation License |
| Tcl/Tk | 桌面界面 | Tcl/Tk License |
| ComfyUI | 本地生成引擎 | GNU GPL v3；完整文本位于 `runtime/ComfyUI/LICENSE` |
| Comfy-Org/workflow_templates | 部分内置工作流模板的来源或改编基础 | MIT；https://github.com/Comfy-Org/workflow_templates |
| PyTorch | GPU 推理 | BSD-3-Clause |
| cloudflared | 公网 Tunnel | Apache-2.0 |
| 7-Zip | 环境包解压 | LGPL 与 7-Zip 自带许可；安装包内保留 `bin/7-Zip-License.txt` |
| FastAPI / Pydantic | 本地 API | MIT |
| Starlette / Uvicorn | Web 服务 | BSD-3-Clause |
| Requests | HTTP 客户端 | Apache-2.0 |
| Pillow | 图片处理 | MIT-CMU |
| CustomTkinter | 界面组件 | CC0-1.0 |
| pystray | 系统托盘 | LGPLv3 |
| NumPy / SciPy | 数值计算 | 以各发行包内许可证为准；主要代码为 BSD 系列许可 |
| Transformers / Diffusers | 模型组件 | Apache-2.0 |
| ReportLab | PDF 生成工具 | BSD 系列许可 |

完整、锁定的 Python 依赖与版本见 `requirements.lock` 和 `requirements-runtime.lock`。发行包应保留 Python、ComfyUI、7-Zip 及各 Python distribution 自带的 LICENSE、METADATA 或 `*.dist-info/licenses` 内容。

本项目不随客户端或运行环境包分发模型权重。用户另行下载的模型、工作流素材和服务接口分别受其来源方许可、使用条款和适用法律约束。

Third-party components remain governed by their respective licenses. The dependency lock files identify the shipped Python package versions; authoritative license texts are retained with the corresponding distributions where provided.
