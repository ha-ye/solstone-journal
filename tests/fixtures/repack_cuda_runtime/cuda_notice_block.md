## runtime-downloaded provider artifacts (llama.cpp CUDA)

These artifacts are downloaded on demand into the journal provider cache for
solstone's local inference runtime on supported NVIDIA GPU systems. They are
distributed as application components, not as a stand-alone CUDA distribution.

### llama.cpp and ggml runtime

Files: `llama-server`, `libllama-server-impl.so`,
`libllama-common.so.0`, `libmtmd.so.0`, `libllama.so.0`,
`libggml.so.0`, `libggml-base.so.0`, `libggml-cuda.so`, and the
architecture-specific `libggml-cpu-*.so` files.

Source: https://github.com/ggml-org/llama.cpp

License: MIT License.

The complete llama.cpp MIT license and copyright notice is reproduced in
`licenses/llama.cpp-LICENSE.txt` and accompanies each runtime artifact.

### NVIDIA CUDA runtime components

Files: `libcudart.so.13`, `libcublas.so.13`,
`libcublasLt.so.13`.

Source: NVIDIA CUDA Toolkit 13.3 packages contained in the pinned upstream
llama.cpp CUDA image.

License: NVIDIA CUDA Toolkit End User License Agreement, Release 13.3,
including the CUDA Toolkit Supplement, Attachment A, and Attachment B.

These files are proprietary NVIDIA software. They are not licensed under
solstone's AGPL-3.0 license or the llama.cpp MIT license. Their use and
redistribution remain subject to the NVIDIA CUDA Toolkit EULA. A verbatim
copy of the package-accompanying EULA, including its third-party notices,
is reproduced in `licenses/NVIDIA-CUDA-EULA-13.3.txt` and accompanies each
runtime artifact. NVIDIA does not sponsor or endorse solstone.
