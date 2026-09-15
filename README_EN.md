[简体中文](README.md) | [English](README_EN.md)

# LingJing AI Studio 2.0

**Generate text, images and video on a Windows PC, and expose ComfyUI workflows through a browser workspace and authenticated APIs.** Workflow and model management, environment maintenance, reference images, video duration/frame-rate controls and asynchronous results are available in one client. Actual capabilities depend on the installed workflows and models.

## Download

### [Download Windows client 2.0.0](https://github.com/Yaro-lu/LingJingAPI/releases/download/v2.0.0/LingJingAI-Setup-2.0.0-win-x64.exe)

### [Download separate NVIDIA / CUDA 13 runtime](https://github.com/Yaro-lu/LingJingAPI/releases/download/v2.0.0/runtime-nvidia-rtx20plus-cu130-v2.0.0.7z)

[Release notes and checksums](https://github.com/Yaro-lu/LingJingAPI/releases/tag/v2.0.0) · [Chinese guide (PDF)](docs/灵境造片厂使用教学.pdf) · [API examples](README.md#接口调用)

**Neither package contains model weights, generated assets, account sessions or personal settings.** Model files are downloaded separately or mapped from an existing local directory.

## Three-step walkthrough

1. Install the lightweight client. In **Models & Environment**, install the runtime, then download the models needed by your workflow or map existing files. Workflow configuration and model actions share one compact list.

![Workflow and model management](docs/images/2.0/workflow-models.png)

*Interface demonstration; status counters use demo data.*

2. Open the local URL in a browser (normally `http://127.0.0.1:18188`), or use the LAN URL from a device on the same network. For remote access, use the public HTTPS URL. Enter the generation API Key at the top right. Connection settings are remembered in that browser.

![Local, LAN and public URL controls](docs/images/2.0/console.png)

*Interface demonstration with placeholder public URL and Key.*

3. Select an available workflow, enter a prompt and generate. Images load as previews; click to fetch and enlarge the original. Video previews show the first frame and load the full video on playback. Projects group a chronological history that loads in batches.

![Browser creation workspace](docs/images/2.0/studio.png)

*User-provided workspace screenshot showing generated image history and video workflow controls.*

## Features in 2.0

- Text workflows with clear system/user prompts and text or JSON response options.
- Image generation and editing with reference images, workflow default dimensions and custom aspect ratios.
- Video workflow duration and frame rate; frame counts are calculated to match workflow constraints.
- Workflow-specific API documentation, schema discovery, async task progress and result downloads.
- Workflow import and rule-based parameter recognition; optional Qwen3.5 4B assistance only when the model and backend are ready. Manual configuration remains available.
- Unified workflow/model management, local model mapping and directory changes with scanning.
- Local, LAN and public browser access without a separate web-hosting service.

## Requirements and behavior

Windows 10 22H2 or Windows 11 x64; NVIDIA RTX 20 series or newer. The CUDA 13 runtime requires R580 or newer drivers. 8 GB VRAM is suitable only for lightweight workflows; larger models need more resources. Reserve storage for the runtime, models and outputs.

The client attempts to create a Cloudflare Tunnel by default. API operations and generated files require a valid Key. Stop the public connection or background services when it is not needed. LAN access requires a reachable PC and a firewall rule for the listening port; guest Wi-Fi isolation may block access.

Projects and connection settings are browser-local. This is a single-user application, without per-Key asset isolation or project sync across browsers. Changing URL or Key does not erase saved history, but original files must still be accessible on their source client. Deleting a project removes its grouping; deleting a file from the asset context menu removes that local file.

Generation and local management keys are separate; client-side secrets use Windows DPAPI. Browser connection storage and third-party model credentials need to be kept private. Optional platform login shares the access URL, generation Key and device/task status with the configured platform; only use a trusted platform. Signing out does not rotate a previously shared Key.

## Development and license

The repository contains source, documentation, example workflows and release scripts, not an installed AI environment. With the portable environment present, use `start.bat` or `check-env.bat`. See the [Chinese README](README.md) for API examples and maintenance details.

Author-owned source and documentation are licensed under [Apache License 2.0](LICENSE). External workflows, models, assets and dependencies retain their original licenses. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [license notes](开源许可说明.txt).
