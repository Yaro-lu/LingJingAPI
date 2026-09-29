"""Model package metadata and safe local import helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
from pathlib import Path
from typing import Callable, Iterable


SAFE_MODEL_EXTENSIONS = (".safetensors", ".gguf")
UNSAFE_MODEL_EXTENSIONS = (".pt", ".pth", ".bin", ".ckpt")
MODEL_EXTENSIONS = SAFE_MODEL_EXTENSIONS + UNSAFE_MODEL_EXTENSIONS
COMFY_MODEL_FOLDERS = {
    "audio_encoders",
    "checkpoints",
    "clip",
    "clip_vision",
    "configs",
    "controlnet",
    "diffusers",
    "diffusion_models",
    "embeddings",
    "gligen",
    "hypernetworks",
    "loras",
    "model_patches",
    "photomaker",
    "style_models",
    "text_encoders",
    "unet",
    "upscale_models",
    "vae",
    "vae_approx",
}

# Exact byte sizes are part of the bundled workflow contract.  They let the
# client reject HTML error pages and interrupted downloads without hashing
# tens of gigabytes on every startup.
MODEL_REQUIREMENTS = {
    "H3 Regular": {
        "title": "性价比 H3 图生视频模型",
        "performance": {
            "level": "高负载视频",
            "minimum": "8GB 显存与足够的系统内存、磁盘空间；需模型卸载",
            "recommended": "12GB+ 显存、32GB+ 内存",
            "preset": "先用短片段与较低分辨率验证",
            "notes": "模型文件体积较大；实际能否在 8GB 显卡运行取决于 ComfyUI 卸载与系统内存。",
        },
        "items": [
            {
                "path": "vae/minimax_h3_video_vae_int8_convrot.safetensors",
                "url": "https://huggingface.co/Kijai/MiniMax-H3-experimental/resolve/e042fe480f58806578713532b8ae4e3d47d1bd63/minimax_h3_video_vae_int8_convrot.safetensors",
                "size_bytes": 3_171_670_912,
                "sha256": "9bb2d96f218c76babd85e0611b85ca8fb330a90546c01a0005e8a58a59593410",
            },
            {
                "path": "vae/minimax_h3_audio_vae_fp32.safetensors",
                "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/bf92c4091e333e69b8ca1998e0a669f15cb0832b/vae/minimax_h3_audio_vae_fp32.safetensors",
                "size_bytes": 605_254_808,
                "sha256": "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48",
            },
            {
                "path": "diffusion_models/minimax_h3_fl2va_pruned_w4a8_mixed.safetensors",
                "url": "https://huggingface.co/Kijai/MiniMax-H3-experimental/resolve/e042fe480f58806578713532b8ae4e3d47d1bd63/minimax_h3_fl2va_pruned_w4a8_mixed.safetensors",
                "size_bytes": 12_540_858_008,
                "sha256": "01aa7b92c007c599890461c325f9b7e3c96fb06c36f242f95b62f7f20e538dec",
            },
            {
                "path": "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/bf92c4091e333e69b8ca1998e0a669f15cb0832b/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "size_bytes": 15_687_142_551,
                "sha256": "35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6",
            },
        ],
    },
    "H3 High Quality": {
        "title": "高质量 H3 图生视频模型",
        "performance": {
            "level": "高负载视频",
            "minimum": "12GB+ 显存与足够的系统内存、磁盘空间；需模型卸载",
            "recommended": "16GB+ 显存、48GB+ 内存",
            "preset": "先用短片段与较低分辨率验证",
            "notes": "模型文件体积较大，实际资源占用以本机运行情况为准。",
        },
        "items": [
            {
                "path": "vae/minimax_h3_video_vae_fp16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/bf92c4091e333e69b8ca1998e0a669f15cb0832b/vae/minimax_h3_video_vae_fp16.safetensors",
                "size_bytes": 5_207_808_496,
                "sha256": "7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522",
            },
            {
                "path": "vae/minimax_h3_audio_vae_fp32.safetensors",
                "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/bf92c4091e333e69b8ca1998e0a669f15cb0832b/vae/minimax_h3_audio_vae_fp32.safetensors",
                "size_bytes": 605_254_808,
                "sha256": "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48",
            },
            {
                "path": "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/bf92c4091e333e69b8ca1998e0a669f15cb0832b/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                "size_bytes": 20_970_379_616,
                "sha256": "e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a",
            },
            {
                "path": "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/bf92c4091e333e69b8ca1998e0a669f15cb0832b/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                "size_bytes": 15_687_142_551,
                "sha256": "35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6",
            },
        ],
    },
    "Qwen3.5": {
        "title": "Qwen3.5 文字模型",
        "performance": {
            "level": "标准",
            "minimum": "RTX 20 系列或以上，8GB 显存，16GB 内存",
            "recommended": "RTX 30/40 系列，12GB+ 显存，32GB 内存",
            "preset": "单任务运行；8GB 显卡启用模型卸载",
            "notes": "首次载入较慢，显存不足时会占用系统内存。",
        },
        "items": [
            {
                "path": "text_encoders/qwen3.5_4b_bf16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Qwen3.5/resolve/main/text_encoders/qwen3.5_4b_bf16.safetensors",
                "size_bytes": 9_319_828_320,
                "sha256": "9fb3ae42003750fe2d16350259a3ec07761d6d13a8e2b244a6e22fa9d8050841",
            },
        ],
    },
    "Flux2": {
        "title": "FLUX.2 Klein 9B 文生图模型",
        "performance": {
            "level": "高负载",
            "minimum": "RTX 20 系列或以上，8GB 显存，32GB 内存（需卸载）",
            "recommended": "RTX 30/40 系列，16GB+ 显存，32GB 内存",
            "preset": "建议 768×768、批量 1；8GB 显卡避免高分辨率",
            "notes": "8GB 可以尝试，但模型换入换出会明显降低速度。",
        },
        "items": [
            {
                "path": "diffusion_models/flux-2-klein-9b-fp8.safetensors",
                # Public pinned copy; retain the original file's size and SHA256 checks.
                "url": "https://huggingface.co/bhatugan/Files/resolve/c360544e1ad1d130b3fc2bc32615d86c092b9cad/flux-2-klein-9b-fp8.safetensors",
                "fallback_urls": [
                    "https://huggingface.co/hfmaster/models-moved/resolve/135ab0aa526a5be5e61677043b1ec2fbbed7cb3d/flux2/flux-2-klein-9b-fp8.safetensors",
                ],
                "size_bytes": 9_433_061_528,
                "sha256": "865ba09f5b4c3cbd3468a4bd3acb9fcb2f8740c54317482f0bcd4ed1d3655cee",
            },
            {
                "path": "text_encoders/qwen_3_8b_fp8mixed.safetensors",
                "url": "https://huggingface.co/Comfy-Org/flux2-klein-9B/resolve/main/split_files/text_encoders/qwen_3_8b_fp8mixed.safetensors",
                "size_bytes": 8_664_848_742,
                "sha256": "abad16806e0cbabc54e0325d6565847443fe396d5f0be38bb3cd3fe75a1201d6",
            },
            {
                "path": "vae/full_encoder_small_decoder.safetensors",
                "url": "https://huggingface.co/black-forest-labs/FLUX.2-small-decoder/resolve/main/full_encoder_small_decoder.safetensors",
                "size_bytes": 249_519_092,
                "sha256": "ea4273f02d1fafbf8e1d1c2cf6018ed8748652eb0bf34f2dd91171f16f15ab62",
            },
        ],
    },
    "Flux2 Klein 4B": {
        "title": "FLUX.2 Klein 4B 文/图生图模型",
        "performance": {
            "level": "轻量",
            "minimum": "RTX 20 系列或以上，8GB 显存，16GB 内存",
            "recommended": "RTX 30/40 系列，12GB+ 显存，32GB 内存",
            "preset": "4 步、CFG 1、批量 1；图生图建议约 0.6MP",
            "notes": "8GB 显卡使用内存卸载，分辨率越高等待越久。",
        },
        "items": [
            {
                "path": "diffusion_models/flux-2-klein-4b-fp8.safetensors",
                "url": "https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8/resolve/5b4408e59397a4a37ccb46afe426d8ed86379441/flux-2-klein-4b-fp8.safetensors?download=true",
                "size_bytes": 4_070_624_520,
                "sha256": "97ed34fe0567e436200f2faee3939b88f2b5d99f8af2a4dc16532c4245c0ccb6",
            },
            {
                "path": "text_encoders/qwen_3_4b.safetensors",
                "url": "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/d24c4cf2a0cd98a42f23467e27e3d76ee9438b8e/split_files/text_encoders/qwen_3_4b.safetensors?download=true",
                "size_bytes": 8_044_982_048,
                "sha256": "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a",
            },
            {
                "path": "vae/flux2-vae.safetensors",
                "url": "https://huggingface.co/Comfy-Org/flux2-dev/resolve/03d6521e6f6a47396b3f951cbea50f7e6c2f482e/split_files/vae/flux2-vae.safetensors?download=true",
                "size_bytes": 336_213_556,
                "sha256": "d64f3a68e1cc4f9f4e29b6e0da38a0204fe9a49f2d4053f0ec1fa1ca02f9c4b5",
            },
        ],
    },
    "Z-Image": {
        "title": "Z-Image Turbo 文生图模型",
        "performance": {
            "level": "标准",
            "minimum": "RTX 20 系列或以上，8GB 显存，32GB 内存（需卸载）",
            "recommended": "RTX 30/40 系列，16GB+ 显存，32GB 内存",
            "preset": "768×768、8 步、CFG 1、批量 1",
            "notes": "8GB 模式会把文本编码器放到 CPU，速度低于高显存设备。",
        },
        "items": [
            {
                "path": "diffusion_models/z_image_turbo_bf16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/d24c4cf2a0cd98a42f23467e27e3d76ee9438b8e/split_files/diffusion_models/z_image_turbo_bf16.safetensors?download=true",
                "size_bytes": 12_309_866_400,
                "sha256": "2407613050b809ffdff18a4ac99af83ea6b95443ecebdf80e064a79c825574a6",
            },
            {
                "path": "text_encoders/qwen_3_4b.safetensors",
                "url": "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/d24c4cf2a0cd98a42f23467e27e3d76ee9438b8e/split_files/text_encoders/qwen_3_4b.safetensors?download=true",
                "size_bytes": 8_044_982_048,
                "sha256": "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a",
            },
            {
                "path": "vae/ae.safetensors",
                "url": "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/d24c4cf2a0cd98a42f23467e27e3d76ee9438b8e/split_files/vae/ae.safetensors?download=true",
                "size_bytes": 335_304_388,
                "sha256": "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38",
            },
        ],
    },
    "Wan2.1": {
        "title": "Wan2.1 VACE 1.3B 轻量视频模型",
        "performance": {
            "level": "轻量视频",
            "minimum": "RTX 20 系列或以上，8GB 显存，32GB 内存",
            "recommended": "RTX 30/40 系列，12GB+ 显存，32GB 内存",
            "preset": "480P、33 帧、批量 1；8GB 显卡启用 lowvram",
            "notes": "视频帧数和分辨率会直接增加显存与生成时间。",
        },
        "items": [
            {
                "path": "diffusion_models/wan2.1_vace_1.3B_fp16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/356481ee2846efa571514320e6c4e10aee42adf8/split_files/diffusion_models/wan2.1_vace_1.3B_fp16.safetensors?download=true",
                "size_bytes": 4_309_519_800,
                "sha256": "640ccc0577e6a5d4bb15cd91b11b699ef914fc55f126c5a1c544e152130784f2",
            },
            {
                "path": "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/dfcea77bcf258496e20c69cd84e8e8e41909bb3b/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors?download=true",
                "size_bytes": 6_735_906_897,
                "sha256": "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
            },
            {
                "path": "vae/wan_2.1_vae.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/dfcea77bcf258496e20c69cd84e8e8e41909bb3b/split_files/vae/wan_2.1_vae.safetensors?download=true",
                "size_bytes": 253_815_318,
                "sha256": "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b",
            },
        ],
    },
    "Wan2.1 FLF2V 14B": {
        "title": "WAN2.1 14B 原版首尾帧视频模型",
        "performance": {
            "level": "高负载视频",
            "minimum": "8GB 显存仅可强制卸载尝试，建议至少 64GB 内存",
            "recommended": "RTX 3090/4090 或专业卡，24GB+ 显存，64GB 内存",
            "preset": "先用 480P/短帧测试；720P 需要更高显存",
            "notes": "14B FP16 不适合作为 8GB 设备的速度方案，保留用于质量优先场景。",
        },
        "items": [
            {
                "path": "diffusion_models/wan2.1_flf2v_720p_14B_fp16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/diffusion_models/wan2.1_flf2v_720p_14B_fp16.safetensors",
                "size_bytes": 32_792_693_440,
                "sha256": "bf4ac25667d00f53f49df02c5771f5aa7801c1dcb9b3ccade1407687c426d030",
            },
            {
                "path": "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "size_bytes": 6_735_906_897,
                "sha256": "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
            },
            {
                "path": "vae/wan_2.1_vae.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors",
                "size_bytes": 253_815_318,
                "sha256": "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b",
            },
            {
                "path": "clip_vision/clip_vision_h.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors",
                "size_bytes": 1_264_219_396,
                "sha256": "64a7ef761bfccbadbaa3da77366aac4185a6c58fa5de5f589b42a65bcc21f161",
            },
        ],
    },
    "LTX-2.3": {
        "title": "LTX-2.3 22B Distilled FP8 首尾帧模型",
        "performance": {
            "level": "超高负载视频",
            "minimum": "16GB 显存、64GB 内存，可用卸载运行短视频",
            "recommended": "RTX 3090/4090 或专业卡，32GB+ 显存，64GB 内存",
            "preset": "先用 512×768、5 秒、25fps；固定 8 步、CFG 1",
            "notes": "官方生态建议 32GB+ 显存；8GB 设备不建议本地运行。",
        },
        "items": [
            {
                "path": "checkpoints/ltx-2.3-22b-distilled-fp8.safetensors",
                "url": "https://huggingface.co/Lightricks/LTX-2.3-fp8/resolve/1d756cd27fa11c0896c4dfee093cd1bf36c7f7a1/ltx-2.3-22b-distilled-fp8.safetensors?download=true",
                "size_bytes": 29_531_884_062,
                "sha256": "d9646b6f2d5c42d337b23671634c43bfeece6989644f51b4a3aa088465ccd3b2",
            },
            {
                "path": "text_encoders/gemma_3_12B_it_fp4_mixed.safetensors",
                "url": "https://huggingface.co/Comfy-Org/ltx-2/resolve/bd5f9c87fcb0360ae7112f9784562670894d9492/split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors?download=true",
                "size_bytes": 9_447_702_218,
                "sha256": "aaca463d11e6d8d2a4bdb0d6299214c15ef78a3f73e0ef8113d5a9d0219b3f6d",
            },
        ],
    },
    "Wan2.1 Fun 1.3B": {
        "title": "Wan2.1-Fun 1.3B InP 首尾帧模型",
        "performance": {
            "level": "轻量视频",
            "minimum": "RTX 20 系列或以上，8GB 显存，32GB 内存",
            "recommended": "RTX 30/40 系列，12GB+ 显存，32GB 内存",
            "preset": "480×768、81 帧、16fps、20 步、批量 1",
            "notes": "1.3B 更适合 8GB 设备；启用卸载后可进一步降低显存占用。",
        },
        "items": [
            {
                "path": "diffusion_models/wan2.1_fun_inp_1.3B_bf16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/06e001fc51048fb03433a6fb25334de7836704a5/split_files/diffusion_models/wan2.1_fun_inp_1.3B_bf16.safetensors?download=true",
                "size_bytes": 3_128_957_992,
                "sha256": "8495d2b1673ffb18abb548a64ff3b0e4bd367734f653096f7a8a3ad46954d511",
            },
            {
                "path": "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/dfcea77bcf258496e20c69cd84e8e8e41909bb3b/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors?download=true",
                "size_bytes": 6_735_906_897,
                "sha256": "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
            },
            {
                "path": "vae/wan_2.1_vae.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/dfcea77bcf258496e20c69cd84e8e8e41909bb3b/split_files/vae/wan_2.1_vae.safetensors?download=true",
                "size_bytes": 253_815_318,
                "sha256": "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b",
            },
            {
                "path": "clip_vision/clip_vision_h.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/dfcea77bcf258496e20c69cd84e8e8e41909bb3b/split_files/clip_vision/clip_vision_h.safetensors?download=true",
                "size_bytes": 1_264_219_396,
                "sha256": "64a7ef761bfccbadbaa3da77366aac4185a6c58fa5de5f589b42a65bcc21f161",
            },
        ],
    },
}


def model_file_ready(path: Path, expected_size: int | None = None) -> bool:
    """Return whether a model file exists and matches its known byte size."""
    try:
        from app.core.model_mappings import resolve_model_file
        path = resolve_model_file(path)
        if not path.is_file():
            return False
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    return not expected_size or size == int(expected_size)


def model_file_sha256_matches(
    path: Path,
    expected_sha256: str,
    chunk_size: int = 8 * 1024 * 1024,
) -> bool:
    """Stream a model once and compare it with a trusted manifest digest."""
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        return False
    digest = hashlib.sha256()
    try:
        from app.core.model_mappings import resolve_model_file
        with resolve_model_file(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
    except OSError:
        return False
    return hmac.compare_digest(digest.hexdigest(), expected)


def cleanup_incomplete_imports(models_dir: Path) -> int:
    """Remove temporary imports left by an interrupted previous client run."""
    removed = 0
    models_dir = Path(models_dir)
    if not models_dir.is_dir():
        return 0
    for path in models_dir.rglob(".*.importing"):
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def check_model_groups(
    models_dir: Path,
    requirements: dict = MODEL_REQUIREMENTS,
) -> dict:
    """Check every model group used by the bundled workflows."""
    models_dir = Path(models_dir)
    result = {"all_ok": False, "missing": {}}
    all_ok = True
    for group, spec in requirements.items():
        missing = []
        for item in spec.get("items", []):
            relative = str(item.get("path") or "").strip()
            if not relative:
                continue
            if not model_file_ready(models_dir / relative, item.get("size_bytes")):
                missing.append(Path(relative).name)
        ready = not missing
        result[group] = "完整" if ready else "缺失"
        result["missing"][group] = missing
        all_ok = all_ok and ready
    result["all_ok"] = all_ok
    return result


def _known_destinations(
    requirements: dict,
) -> dict[str, tuple[Path, int | None, str]]:
    destinations: dict[str, tuple[Path, int | None, str]] = {}
    for spec in requirements.values():
        for item in spec.get("items", []):
            relative = Path(str(item.get("path") or ""))
            if relative.name:
                destinations[relative.name.lower()] = (
                    relative,
                    item.get("size_bytes"),
                    str(item.get("sha256") or ""),
                )
    return destinations


def _candidate_files(source: Path) -> Iterable[Path]:
    for path in source.rglob("*"):
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS:
            yield path


def unsafe_model_files(source: Path) -> list[Path]:
    """List pickle-compatible model files that require an explicit warning."""
    source = Path(source)
    if not source.is_dir():
        return []
    return [
        path
        for path in source.rglob("*")
        if path.is_file() and path.suffix.lower() in UNSAFE_MODEL_EXTENSIONS
    ]


def _unknown_model_destination(source: Path, relative: Path) -> Path:
    parts = relative.parts
    if source.name.lower() == "models":
        return relative
    if parts and parts[0].lower() == "models":
        remaining = parts[1:]
        if remaining:
            return Path(*remaining)
        raise ValueError("models 目录下没有可导入的文件路径")
    for index, part in enumerate(parts):
        if part.lower() in COMFY_MODEL_FOLDERS:
            return Path(*parts[index:])
    if source.name.lower() in COMFY_MODEL_FOLDERS:
        return Path(source.name) / relative
    raise ValueError(
        "无法判断模型类型；请选择 ComfyUI 的 models 目录，或 checkpoints、loras、vae 等具体分类目录"
    )


def import_model_directory(
    source: Path,
    models_dir: Path,
    requirements: dict = MODEL_REQUIREMENTS,
    on_progress: Callable[[int, int, str], None] | None = None,
    allow_unsafe: bool = False,
) -> dict:
    """Copy model files safely and return imported/skipped/failed counters.

    Known bundled filenames are routed to their required ComfyUI subfolders.
    Other model files keep their relative layout.  Every copy lands in a
    temporary file first and is atomically promoted only after size checks.
    """
    source = Path(source)
    models_dir = Path(models_dir)
    if not source.is_dir():
        raise ValueError("选择的模型目录不存在")

    candidates = list(_candidate_files(source))
    known = _known_destinations(requirements)
    result = {
        "found": len(candidates),
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }

    for index, source_file in enumerate(candidates, 1):
        if on_progress:
            on_progress(index, len(candidates), source_file.name)

        try:
            if source_file.suffix.lower() in UNSAFE_MODEL_EXTENSIONS and not allow_unsafe:
                raise ValueError(
                    "该格式可能包含可执行反序列化内容；仅在确认来源可信后显式允许导入"
                )
            relative = source_file.relative_to(source)
            expected = known.get(source_file.name.lower())
            if expected:
                relative, expected_size, expected_sha256 = expected
            else:
                expected_size = None
                expected_sha256 = ""
                relative = _unknown_model_destination(source, relative)
            destination = models_dir / relative
            if source_file.resolve() == destination.resolve():
                if model_file_ready(source_file, expected_size) and (
                    not expected_sha256
                    or model_file_sha256_matches(source_file, expected_sha256)
                ):
                    result["skipped"] += 1
                    continue
                raise ValueError("文件已在目标目录，但完整性校验未通过")
            source_size = source_file.stat().st_size
            if source_size <= 0:
                raise ValueError("文件为空")
            if expected_size and source_size != int(expected_size):
                raise ValueError(
                    f"文件大小不匹配（应为 {int(expected_size)} 字节，实际 {source_size} 字节）"
                )
            if expected_sha256 and not model_file_sha256_matches(
                source_file, expected_sha256
            ):
                raise ValueError("SHA256 与可信模型清单不一致")
            if model_file_ready(destination, expected_size) and (
                not expected_sha256
                or model_file_sha256_matches(destination, expected_sha256)
            ):
                result["skipped"] += 1
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.importing")
            try:
                temporary.unlink(missing_ok=True)
                shutil.copy2(source_file, temporary)
                if temporary.stat().st_size != source_size:
                    raise IOError("复制后的文件大小与源文件不一致")
                if expected_sha256 and not model_file_sha256_matches(
                    temporary, expected_sha256
                ):
                    raise IOError("复制后的文件 SHA256 校验失败")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            result["imported"] += 1
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append(f"{source_file.name}: {exc}")

    return result
