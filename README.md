[简体中文](README.md) | [English](README_EN.md)

# 灵境造片厂 · LingJingAPI

**远程调用算力，生成文字、图片和视频。**

在租用的算力服务器上部署生成服务，在自己的电脑上运行配套客户端页面，也可以用手机浏览器调用。模型在服务器上运行，本地设备负责提交任务、查看和下载结果。

**本地电脑 / 手机 ⇄ URL + API Key ⇄ 远程算力服务器**

## 下载

**[下载服务端程序 · Windows EXE](https://github.com/Yaro-lu/LingJingAPI/releases/download/v2.0.0/LingJingAI-Setup-2.0.0-win-x64.exe)　｜　[下载独立运行环境包](https://github.com/Yaro-lu/LingJingAPI/releases/download/v2.0.0/runtime-nvidia-rtx20plus-cu130-v2.0.0.7z)**

[2.0 更新说明](https://github.com/Yaro-lu/LingJingAPI/releases/tag/v2.0.0) · [本地客户端页面](examples/灵境造片厂示例页.html)

当前版本：`2.0.0`。程序和环境包安装在算力端，**不包含模型**；模型可单独下载或映射已有文件。本地轻量客户端页面和手机无需安装模型。

## 怎么用

1. **服务器部署**：在租用的算力服务器上安装程序、运行环境和所需模型，启动服务，获取公网 URL 与 API Key。
2. **本地连接**：电脑打开配套客户端页面，填入 URL 和 Key；手机直接用浏览器打开同一个公网 URL 并填写 Key。
3. **开始生成**：选择工作流，输入提示词或上传参考图，调用服务器算力生成文字、图片或视频。

例如：电脑提交“一只猫坐在杯子里”，服务器完成生成后返回图片；也可以在手机上继续上传参考图、发起图片编辑或视频生成。

## 支持哪些功能

- **图文视频生成**：文字生成、文生图、参考图编辑和视频生成，具体能力取决于工作流与模型。
- **多端调用**：支持电脑、手机、公网、本机及局域网访问。
- **工作流与 API**：导入 ComfyUI 工作流，管理环境和模型，为第三方软件提供调用接口。

## 界面演示

电脑和手机通过客户端页面提交任务、查看作品。

![客户端创作页面](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/studio.png?raw=true)

算力端提供连接地址，并集中管理工作流、模型与运行环境。

![算力端控制台](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/console.png?raw=true)

![工作流与模型管理](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/workflow-models.png?raw=true)

*后两张为界面演示，连接信息和状态数值为演示数据。*

## 使用说明

当前发行包适用于 **Windows x64 算力端 + NVIDIA 显卡**，配套 CUDA 13 环境；电脑和手机可通过浏览器调用。服务端一键修复拉取失败时，可手动导入环境包。

[详细使用与接口说明](docs/使用与接口说明.md) · [使用教学 PDF](docs/灵境造片厂使用教学.pdf) · [Apache License 2.0](LICENSE)
