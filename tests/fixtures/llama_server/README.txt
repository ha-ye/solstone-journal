These fixtures cover Linux Vulkan local-provider device selection.

The b9291 `llama-server --list-devices` text is intentionally not parsed. It
reports names and memory only, and the iGPU can report more shared memory than
the discrete GPU:

Available devices:
  Vulkan0: Intel(R) Graphics (RKL GT1) (23814 MiB, 23814 MiB free)
  Vulkan1: NVIDIA GeForce GTX 1660 Ti (6390 MiB, 6390 MiB free)
  Vulkan2: llvmpipe (LLVM ..., 256 bits) (0 MiB, 0 MiB free)

The production gate classifies by Vulkan device TYPE through ctypes and pins
llama.cpp with GGML_VK_VISIBLE_DEVICES, so it does not VRAM-rank this text.
