[简体中文](README.md) | [English](README_EN.md)

# 灵境 · LingJingAPI

**One-click access to compute power. Simple to use.**

Use remote GPU compute to generate text, images and video.

Deploy the generation service on a machine with GPU compute, then connect from computers or phones using its URL and API Key. Models run on the server; calling devices submit tasks and display or download the results.

Two typical uses:

- **Rented remote compute:** deploy on a rented GPU server and connect from your computer's client page or your phone's browser.
- **Shared office compute:** deploy on one company machine and let colleagues generate text, images and video from their computers or phones over the LAN.

**Computer / phone ⇄ URL + API Key ⇄ Remote server / office GPU machine**

## Installation

The program and runtime packages are distributed separately through this repository’s Releases section.

Version: `2.0.3`. Install the program and runtime on the GPU server. Neither package includes model weights; download or map existing models separately. Calling devices do not need local models.

Uninstall keeps models and generated outputs, then shows their locations so you can remove them manually if desired.

## Three steps

1. **Deploy the service:** install the program, runtime and required models on a rented server or office machine. Start the service and copy its public or LAN URL and API Key.
2. **Connect from your device:** enter the URL and Key in the companion client page, or open the service URL in a computer or phone browser and enter the Key.
3. **Generate:** select a workflow, enter a prompt or reference image, and let the remote server generate the result.

## Features

- Text, image generation/editing and video generation, depending on the installed workflows and models.
- Computer and phone access over public HTTPS, with local and LAN access also supported.
- ComfyUI workflow import, model/environment management and APIs for third-party software.
- A shared FIFO queue across desktop, phone and API requests, with queue positions and task cancellation.

## Screenshots

![Client workspace](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/studio.png?raw=true)

![GPU server control panel](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/console.png?raw=true)

![Workflow and model management](https://github.com/Yaro-lu/LingJingAPI/blob/main/docs/images/2.0/workflow-models.png?raw=true)

*The last two screenshots are interface demonstrations with placeholder connection details and status values.*

The current release package targets **Windows x64 GPU hosts with NVIDIA graphics and CUDA 13**. Calling computers and phones can use a browser.

[Detailed usage and API guide (Chinese)](docs/使用与接口说明.md) · [Apache License 2.0](LICENSE)
