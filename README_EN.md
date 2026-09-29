[简体中文](README.md) | [English](README_EN.md)

# 灵境 · LingJingAPI

**One-click access to compute power. Simple to use.**

Use remote GPU compute to generate text, images and video.

Deploy the generation service on a machine with GPU compute, then connect from computers or phones using its URL and API Key. Models run on the server; calling devices submit tasks and display or download the results.

No platform account or sign-in is required. The API Key protects your own generation endpoint.

Two typical uses:

- **Rented remote compute:** deploy on a rented GPU server and connect from your computer's client page or your phone's browser.
- **Shared office compute:** deploy on one company machine and let colleagues generate text, images and video from their computers or phones over the LAN.

**Computer / phone ⇄ URL + API Key ⇄ Remote server / office GPU machine**

## First-time setup: four steps

Version `2.0.3`: get the program installer from this repository's Releases page and install it on a Windows machine with an NVIDIA GPU. You can choose the installation directory. The program and runtime are distributed separately; the installer **does not include model weights**.

1. **Launch the program.** On first launch, the environment installation and repair window appears.
2. **Click one-click repair.** The program selects the “above 12 GB” or “12 GB and below” profile from detected VRAM. Confirm the choice, then wait while it downloads and verifies the runtime and text, image and video models, and starts the services. Existing models are reused.

![Environment installation and repair on first launch](https://raw.githubusercontent.com/Yaro-lu/LingJingAPI/main/docs/images/2.0/quick-repair.png)

*This screenshot is from the 2.0.3 installer. The source has since removed the old “local mode” badge and platform sign-in; the installed UI will catch up in the next release.*

3. **Copy connection details.** Get the URL and API Key from the control panel. Use the local URL on the GPU machine, the LAN URL on other devices in the same network, or the public URL when connected from elsewhere. Keep the API Key private.
4. **Generate.** Open that URL in a computer or phone browser, enter the API Key, choose an available workflow, and provide a prompt or reference image.

If a download stops, open “Models & Environment” and run one-click repair again. You can import the runtime package manually if automatic download fails. Uninstall preserves models and generated outputs and shows their locations.

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
