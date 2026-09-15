[简体中文](README.md) | [English](README_EN.md)

# LingJingAPI · LingJing AI Studio

**Use remote GPU compute to generate text, images and video.**

Deploy the generation service on a rented GPU server, then use the companion client page on your computer or a browser on your phone. Models run on the server; your device submits tasks and displays or downloads the results.

**Computer / phone ⇄ URL + API Key ⇄ Remote GPU server**

## Download

**[Server program · Windows EXE](https://github.com/Yaro-lu/LingJingAPI/releases/download/v2.0.0/LingJingAI-Setup-2.0.0-win-x64.exe) | [Separate runtime package](https://github.com/Yaro-lu/LingJingAPI/releases/download/v2.0.0/runtime-nvidia-rtx20plus-cu130-v2.0.0.7z)**

[2.0 release notes](https://github.com/Yaro-lu/LingJingAPI/releases/tag/v2.0.0) · [Companion client page](examples/灵境造片厂示例页.html)

Version: `2.0.0`. Install the program and runtime on the GPU server. Neither package includes model weights; download or map existing models separately. Calling devices do not need local models.

## Three steps

1. **Deploy on the server:** install the program, runtime and required models, start the service, and copy its public URL and API Key.
2. **Connect from your device:** open the companion page on your computer and enter the URL and Key. On a phone, open the same public URL in a browser and enter the Key.
3. **Generate:** select a workflow, enter a prompt or reference image, and let the remote server generate the result.

## Features

- Text, image generation/editing and video generation, depending on the installed workflows and models.
- Computer and phone access over public HTTPS, with local and LAN access also supported.
- ComfyUI workflow import, model/environment management and APIs for third-party software.

## Screenshots

![Client workspace](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/studio.png?raw=true)

![GPU server control panel](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/console.png?raw=true)

![Workflow and model management](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/workflow-models.png?raw=true)

*The last two screenshots are interface demonstrations with placeholder connection details and status values.*

The current release package targets **Windows x64 GPU hosts with NVIDIA graphics and CUDA 13**. Calling computers and phones can use a browser.

[Detailed usage and API guide (Chinese)](docs/使用与接口说明.md) · [Apache License 2.0](LICENSE)
