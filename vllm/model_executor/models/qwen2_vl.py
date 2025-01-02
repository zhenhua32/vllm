# Adapted from
# https://github.com/huggingface/transformers/blob/19e6e80e10118f855137b90740936c0b11ac397f/src/transformers/models/qwen2_vl/modeling_qwen2_vl.py
# Copyright 2024 The Qwen team.
# Copyright 2023 The vLLM team.
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Inference-only Qwen2-VL model compatible with HuggingFace weights."""
from functools import cached_property, partial
from typing import (Any, Callable, Iterable, List, Literal, Mapping, Optional,
                    Set, Tuple, Type, TypedDict, Union)

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange, repeat
from transformers import BatchFeature
from transformers.models.qwen2_vl import (Qwen2VLImageProcessor,
                                          Qwen2VLProcessor)
from transformers.models.qwen2_vl.configuration_qwen2_vl import (
    Qwen2VLConfig, Qwen2VLVisionConfig)
from transformers.models.qwen2_vl.image_processing_qwen2_vl import smart_resize

from vllm.attention import AttentionMetadata
from vllm.config import VllmConfig
from vllm.distributed import parallel_state
from vllm.distributed import utils as dist_utils
from vllm.logger import init_logger
from vllm.model_executor import SamplingMetadata
from vllm.model_executor.layers.activation import QuickGELU
from vllm.model_executor.layers.linear import (ColumnParallelLinear,
                                               RowParallelLinear)
from vllm.model_executor.layers.quantization import QuantizationConfig
from vllm.model_executor.layers.quantization.gptq import GPTQConfig
from vllm.model_executor.layers.quantization.gptq_marlin import (
    GPTQMarlinConfig)
from vllm.model_executor.layers.sampler import SamplerOutput, get_sampler
from vllm.model_executor.model_loader.weight_utils import default_weight_loader
from vllm.model_executor.models.module_mapping import MultiModelKeys
from vllm.multimodal import MULTIMODAL_REGISTRY
from vllm.multimodal.inputs import (ImageItem, ModalityData,
                                    MultiModalFieldConfig, MultiModalKwargs,
                                    NestedTensors, VideoItem)
from vllm.multimodal.parse import ModalityDataItems, MultiModalDataParser
from vllm.multimodal.processing import (BaseMultiModalProcessor,
                                        MultiModalDataItems, ProcessorInputs,
                                        PromptReplacement)
from vllm.platforms import _Backend
from vllm.sequence import IntermediateTensors
from vllm.transformers_utils.config import uses_mrope

from .interfaces import SupportsLoRA, SupportsMultiModal, SupportsPP
from .utils import (AutoWeightsLoader, WeightsMapper, get_vit_attn_backend,
                    init_vllm_registered_model, maybe_prefix)

logger = init_logger(__name__)

# === Vision Inputs === #


class Qwen2VLImagePixelInputs(TypedDict):
    type: Literal["pixel_values"]
    pixel_values: torch.Tensor
    """Shape:
    `(num_patches, num_channels * patch_size * patch_size)`
    """

    image_grid_thw: torch.Tensor
    """Shape: `(num_images, 3)`
    This should be in `(grid_t, grid_h, grid_w)` format.
    """


class Qwen2VLImageEmbeddingInputs(TypedDict):
    type: Literal["image_embeds"]
    image_embeds: torch.Tensor
    """Supported types:
    - List[`torch.Tensor`]: A list of tensors holding all images' features.
        Each tensor holds an image's features.
    - `torch.Tensor`: A tensor holding all images' features
        (concatenation of all images' feature tensors).
    
    Tensor shape: `(num_image_features, hidden_size)`
    - `num_image_features` varies based on
        the number and resolution of the images.
    - `hidden_size` must match the hidden size of language model backbone.
    """

    image_grid_thw: torch.Tensor
    """Shape: `(num_images, 3)`
    This should be in `(grid_t, grid_h, grid_w)` format.
    """


Qwen2VLImageInputs = Union[Qwen2VLImagePixelInputs,
                           Qwen2VLImageEmbeddingInputs]


class Qwen2VLVideoPixelInputs(TypedDict):
    type: Literal["pixel_values_videos"]
    pixel_values_videos: torch.Tensor
    """Shape:
    `(num_patches,
      num_channels * temporal_patch_size * patch_size * patch_size)`
    """

    video_grid_thw: torch.Tensor
    """Shape: `(num_videos, 3)`

    This should be in `(grid_t, grid_h, grid_w)` format.
    """


class Qwen2VLVideoEmbeddingInputs(TypedDict):
    type: Literal["video_embeds"]
    video_embeds: torch.Tensor
    """Supported types:
    - List[`torch.Tensor`]: A list of tensors holding all videos' features.
        Each tensor holds an video's features.
    - `torch.Tensor`: A tensor holding all videos' features
      (concatenation of all videos' feature tensors).
    
    Tensor shape: `(num_image_features, hidden_size)`
    - `num_image_features` varies based on 
        the number and resolution of the videos.
    - `hidden_size` must match the hidden size of language model backbone.
    """

    video_grid_thw: torch.Tensor
    """Shape: `(num_videos, 3)`
    This should be in `(grid_t, grid_h, grid_w)` format.
    """


Qwen2VLVideoInputs = Union[Qwen2VLVideoPixelInputs,
                           Qwen2VLVideoEmbeddingInputs]

# === Vision Encoder === #


class Qwen2VisionMLP(nn.Module):

    def __init__(
        self,
        in_features: int,  # 1280
        hidden_features: int,  # 5120
        act_layer: Type[nn.Module] = QuickGELU,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ):
        super().__init__()
        self.fc1 = ColumnParallelLinear(in_features,  # 1280
                                        hidden_features,  # 5120
                                        quant_config=quant_config,
                                        prefix=f"{prefix}.fc1")
        self.act = act_layer()
        self.fc2 = RowParallelLinear(hidden_features,  # 5120
                                     in_features,  # 1280
                                     quant_config=quant_config,
                                     prefix=f"{prefix}.fc2")

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x 的 shape 是 (14308, 1, 1280)
        x_parallel, _ = self.fc1(x)  # (14308, 1, 5120)
        x_parallel = self.act(x_parallel)
        x, _ = self.fc2(x_parallel)  # (14308, 1, 1280)
        return x


def rotate_half(x: torch.Tensor, interleaved: bool = False) -> torch.Tensor:
    if not interleaved:  # interleaved=False 时
        x1, x2 = x.chunk(2, dim=-1)  # (1, 14308, 16, 80) -> (1, 14308, 16, 40), (1, 14308, 16, 40)
        return torch.cat((-x2, x1), dim=-1)  # (1, 14308, 16, 80)
    else:
        # interleaved 是交错的意思
        x1, x2 = x[..., ::2], x[..., 1::2]
        return rearrange(torch.stack((-x2, x1), dim=-1),
                         "... d two -> ... (d two)",
                         two=2)


def apply_rotary_emb_torch(x: torch.Tensor,
                           cos: torch.Tensor,
                           sin: torch.Tensor,
                           interleaved: bool = False) -> torch.Tensor:
    """
    x: (batch_size, seqlen, nheads, headdim)
    cos, sin: (seqlen, rotary_dim / 2) or (batch_size, seqlen, rotary_dim / 2)
    """
    # x 的 shape 是 (1, 14308, 16, 80)
    ro_dim = cos.shape[-1] * 2  # 80
    assert ro_dim <= x.shape[-1]
    cos = repeat(
        cos,
        "... d -> ... 1 (2 d)" if not interleaved else "... d -> ... 1 (d 2)")  # (14308, 40) -> (14308, 1, 80)
    sin = repeat(
        sin,
        "... d -> ... 1 (2 d)" if not interleaved else "... d -> ... 1 (d 2)")  # (14308, 40) -> (14308, 1, 80)
    return torch.cat(
        [
            x[..., :ro_dim] * cos +  # (1, 14308, 16, 80)
            rotate_half(x[..., :ro_dim], interleaved) * sin, x[..., ro_dim:]
        ],
        dim=-1,
    )  # (1, 14308, 16, 240)


def apply_rotary_pos_emb_vision(t: torch.Tensor,
                                freqs: torch.Tensor) -> torch.Tensor:
    t_ = t.float()  # (1, 14308, 16, 80)
    # freqs 是 (14308, 40)
    cos = freqs.cos()
    sin = freqs.sin()
    output = apply_rotary_emb_torch(t_, cos, sin).type_as(t)
    return output  # (1, 14308, 16, 240)


class Qwen2VisionAttention(nn.Module):

    def __init__(
        self,
        embed_dim: int,  # 1280
        num_heads: int,  # 16
        projection_size: int,  # 1280
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ) -> None:
        super().__init__()
        # Per attention head and per partition values.
        world_size = parallel_state.get_tensor_model_parallel_world_size()  # 假设只有 1卡, 1
        self.hidden_size_per_attention_head = dist_utils.divide(
            projection_size, num_heads)  # 80
        self.num_attention_heads_per_partition = dist_utils.divide(
            num_heads, world_size)  # 16

        self.qkv = ColumnParallelLinear(input_size=embed_dim,  # 1280
                                        output_size=3 * projection_size,  # 3840
                                        quant_config=quant_config,
                                        prefix=f"{prefix}.qkv")
        self.proj = RowParallelLinear(input_size=projection_size,  # 1280
                                      output_size=embed_dim,  # 1280
                                      quant_config=quant_config,
                                      prefix=f"{prefix}.proj")

        # Detect attention implementation.
        self.attn_backend: _Backend = get_vit_attn_backend(support_fa=True)
        if self.attn_backend not in {
                _Backend.FLASH_ATTN, _Backend.TORCH_SDPA, _Backend.XFORMERS
        }:
            raise RuntimeError(
                f"Qwen2-VL does not support {self.attn_backend} backend now.")

    def forward(
        self,
        x: torch.Tensor,
        cu_seqlens: torch.Tensor,
        rotary_pos_emb: torch.Tensor,
    ) -> torch.Tensor:
        """返回的 shape 是 (14308, 1, 1280)"""
        # x 的 shape 是 (14308, 1, 1280)
        # cu_seqlens 的值是 [0, 14308]
        # rotary_pos_emb 的 shape 是 (14308, 40)
        # [s, b, c] --> [s, b, head * 3 * head_dim]
        x, _ = self.qkv(x)  # (14308, 1, 3840)

        # [s, b, head * 3 * head_dim] --> [s, b, head, 3 * head_dim]
        new_x_shape = x.size()[:-1] + (
            self.num_attention_heads_per_partition,  # 16
            3 * self.hidden_size_per_attention_head,  # 240
        )
        x = x.view(*new_x_shape)  # (14308, 1, 16, 240)

        # [s, b, head, 3 * head_dim] --> 3 [s, b, head, head_dim]
        q, k, v = dist_utils.split_tensor_along_last_dim(x, 3)  # 每个的 shape 是 (14308, 1, 16, 80)
        batch_size = q.shape[1]  # 1

        q, k, v = (rearrange(x, "s b ... -> b s ...").contiguous()
                   for x in (q, k, v))  # (1, 14308, 16, 80)
        if rotary_pos_emb is not None:
            q = apply_rotary_pos_emb_vision(q, rotary_pos_emb)  # (1, 14308, 16, 240)
            k = apply_rotary_pos_emb_vision(k, rotary_pos_emb)  # (1, 14308, 16, 240)

        if self.attn_backend == _Backend.FLASH_ATTN:
            # from vllm_flash_attn.flash_attn_interface import (
            #   flash_attn_varlen_func)
            from flash_attn import flash_attn_varlen_func

            q, k, v = (rearrange(x, "b s ... -> (b s) ...") for x in [q, k, v])

            max_seqlen = (cu_seqlens[1:] - cu_seqlens[:-1]).max().item()
            output = flash_attn_varlen_func(q,
                                            k,
                                            v,
                                            cu_seqlens_q=cu_seqlens,
                                            cu_seqlens_k=cu_seqlens,
                                            max_seqlen_q=max_seqlen,
                                            max_seqlen_k=max_seqlen,
                                            dropout_p=0,
                                            causal=False)

            context_layer = rearrange(output,
                                      "(b s) ... -> b s ...",
                                      b=batch_size)
        elif self.attn_backend == _Backend.TORCH_SDPA:
            seq_length = q.size(1)  # 14308
            q, k, v = (rearrange(x, "b s h d -> b h s d") for x in [q, k, v])
            # q 和 k 是 (1, 16, 14308, 240), v 是 (1, 16, 14308, 80)
            attention_mask = torch.zeros([1, seq_length, seq_length],
                                         device=q.device,
                                         dtype=torch.bool)  # (1, 14308, 14308)
            for i in range(1, len(cu_seqlens)):
                # i 是 1
                attention_mask[..., cu_seqlens[i - 1]:cu_seqlens[i],
                               cu_seqlens[i - 1]:cu_seqlens[i]] = True
                # attention_mask[..., 0:14308, 0:14308] = True
            output = F.scaled_dot_product_attention(q,
                                                    k,
                                                    v,
                                                    attention_mask,
                                                    dropout_p=0.0)  # (1, 16, 14308, 80)
            context_layer = rearrange(output, "b h s d -> b s h d ")  # (1, 14308, 16, 80)
        elif self.attn_backend == _Backend.XFORMERS:
            from xformers import ops as xops
            from xformers.ops.fmha.attn_bias import BlockDiagonalMask

            seqlens = (cu_seqlens[1:] - cu_seqlens[:-1]).tolist()
            attn_bias = BlockDiagonalMask.from_seqlens(q_seqlen=seqlens,
                                                       kv_seqlen=None)

            context_layer = xops.memory_efficient_attention_forward(
                q, k, v, attn_bias=attn_bias, p=0, scale=None)
        context_layer = rearrange(context_layer,
                                  "b s h d -> s b (h d)").contiguous()  # (14308, 1, 1280)

        output, _ = self.proj(context_layer)  # (14308, 1, 1280)
        return output


class Qwen2VisionBlock(nn.Module):

    def __init__(
        self,
        dim: int,  # 1280
        num_heads: int,  # 16
        mlp_ratio: float,  # 4
        act_layer: Type[nn.Module] = QuickGELU,
        norm_layer: Optional[Callable[[int], nn.Module]] = None,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ) -> None:
        super().__init__()
        if norm_layer is None:
            norm_layer = partial(nn.LayerNorm, eps=1e-6)
        self.norm1 = norm_layer(dim)
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)  # 5120

        self.attn = Qwen2VisionAttention(embed_dim=dim,  # 1280
                                         num_heads=num_heads,  # 16
                                         projection_size=dim,  # 1280
                                         quant_config=quant_config,
                                         prefix=f"{prefix}.attn")
        self.mlp = Qwen2VisionMLP(dim,  # 1280
                                  mlp_hidden_dim,  # 5120
                                  act_layer=act_layer,
                                  quant_config=quant_config,
                                  prefix=f"{prefix}.mlp")

    def forward(self, x: torch.Tensor, cu_seqlens: torch.Tensor,
                rotary_pos_emb: torch.Tensor) -> torch.Tensor:
        # x 的 shape 是 (14308, 1, 1280)
        # cu_seqlens 的值是 [0, 14308]
        # rotary_pos_emb 的 shape 是 (14308, 40)
        x = x + self.attn(self.norm1(x),
                          cu_seqlens=cu_seqlens,
                          rotary_pos_emb=rotary_pos_emb)
        # attn 返回的 shape 是 (14308, 1, 1280)
        x = x + self.mlp(self.norm2(x))  # (14308, 1, 1280)
        return x


class Qwen2VisionPatchEmbed(nn.Module):

    def __init__(
        self,
        patch_size: int = 14,  # 14
        temporal_patch_size: int = 2,  # 2
        in_channels: int = 3,  # 3
        embed_dim: int = 1152,  # 1280
    ) -> None:
        super().__init__()
        self.patch_size = patch_size  # 14
        self.temporal_patch_size = temporal_patch_size  # 2
        self.embed_dim = embed_dim  # 1280

        kernel_size = (temporal_patch_size, patch_size, patch_size)  # [2, 14, 14]
        self.proj = nn.Conv3d(in_channels,  # 3
                              embed_dim,  # 1280
                              kernel_size=kernel_size,  # [2, 14, 14]
                              stride=kernel_size,  # [2, 14, 14]
                              bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """返回的 shape 是 (num_patches=14308, 1280)"""
        # x 的 shape 是 (14308, 1176)
        L, C = x.shape
        x = x.view(L, -1, self.temporal_patch_size, self.patch_size,
                   self.patch_size)  # (14308, 3, 2, 14, 14)
        x = self.proj(x).view(L, self.embed_dim)  # (14308, 1280)
        return x


class Qwen2VisionPatchMerger(nn.Module):

    def __init__(
        self,
        d_model: int,
        context_dim: int,
        norm_layer: Optional[Callable[[int], nn.Module]] = None,
        spatial_merge_size: int = 2,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ) -> None:
        super().__init__()
        self.hidden_size = context_dim * (spatial_merge_size**2)
        if norm_layer is None:
            norm_layer = partial(nn.LayerNorm, eps=1e-6)
        self.ln_q = norm_layer(context_dim)
        self.mlp = nn.ModuleList([
            ColumnParallelLinear(self.hidden_size,
                                 self.hidden_size,
                                 bias=True,
                                 quant_config=quant_config,
                                 prefix=f"{prefix}.mlp.0"),
            nn.GELU(),
            RowParallelLinear(self.hidden_size,
                              d_model,
                              bias=True,
                              quant_config=quant_config,
                              prefix=f"{prefix}.mlp.2"),
        ])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.ln_q(x)
        x = x.view(-1, self.hidden_size)

        mlp_fc1, mlp_act, mlp_fc2 = self.mlp
        x_parallel, _ = mlp_fc1(x)
        x_parallel = mlp_act(x_parallel)
        out, _ = mlp_fc2(x_parallel)
        return out


class Qwen2VisionRotaryEmbedding(nn.Module):

    def __init__(self, dim: int, theta: float = 10000.0) -> None:
        super().__init__()
        self.dim = dim  # 40
        self.theta = theta  # 10000.0
        inv_freq = 1.0 / (theta
                          **(torch.arange(0, dim, 2, dtype=torch.float) / dim))
        # inv_freq shape 是 (20,)
        self.register_buffer("inv_freq", inv_freq, persistent=False)
        self._seq_len_cached = 0
        self._freqs_cached = None

    def update_freqs_cache(self, seqlen: int) -> None:
        if seqlen > self._seq_len_cached:
            seqlen *= 2
            self._seq_len_cached = seqlen
            self.inv_freq = 1.0 / (self.theta**(torch.arange(
                0, self.dim, 2, dtype=torch.float, device=self.inv_freq.device)
                                                / self.dim))
            seq = torch.arange(seqlen,
                               device=self.inv_freq.device,
                               dtype=self.inv_freq.dtype)
            freqs = torch.outer(seq, self.inv_freq)
            self._freqs_cached = freqs

    def forward(self, seqlen: int) -> torch.Tensor:
        """返回的 shape 是 (seqlen, 20)"""
        self.update_freqs_cache(seqlen)
        return self._freqs_cached[:seqlen]  # (seqlen, 20)


class Qwen2VisionTransformer(nn.Module):

    def __init__(
        self,
        vision_config: Qwen2VLVisionConfig,
        norm_eps: float = 1e-6,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ) -> None:
        super().__init__()

        # 以 Qwen2-VL-7B-Instruct_config 为例, 标注下输入输出的 shape
        patch_size = vision_config.patch_size  # 14
        temporal_patch_size = vision_config.temporal_patch_size  # 2
        spatial_merge_size = vision_config.spatial_merge_size  # 2
        in_channels = vision_config.in_channels  # 3
        hidden_size = vision_config.hidden_size  # 3584
        embed_dim = vision_config.embed_dim  # 1280
        depth = vision_config.depth  # 32
        num_heads = vision_config.num_heads  # 16
        mlp_ratio = vision_config.mlp_ratio  # 4

        self.spatial_merge_size = spatial_merge_size  # 2
        self.num_heads = num_heads
        self.embed_dim = embed_dim

        self.patch_embed = Qwen2VisionPatchEmbed(
            patch_size=patch_size,  # 14
            temporal_patch_size=temporal_patch_size,  # 2
            in_channels=in_channels,  # 3
            embed_dim=embed_dim,  # 1280
        )

        norm_layer = partial(nn.LayerNorm, eps=norm_eps)
        head_dim = embed_dim // num_heads  # 80
        self.rotary_pos_emb = Qwen2VisionRotaryEmbedding(head_dim // 2)  # 40

        self.blocks = nn.ModuleList([
            Qwen2VisionBlock(dim=embed_dim,  # 1280
                             num_heads=num_heads,  # 16
                             mlp_ratio=mlp_ratio,  # 4
                             norm_layer=norm_layer,
                             quant_config=quant_config,
                             prefix=f"{prefix}.blocks.{layer_idx}")
            for layer_idx in range(depth)
        ])
        self.merger = Qwen2VisionPatchMerger(
            d_model=hidden_size,  # 3584
            context_dim=embed_dim,  # 1280
            norm_layer=norm_layer,
            quant_config=quant_config,
            prefix=f"{prefix}.merger",
        )

    @property
    def dtype(self) -> torch.dtype:
        return self.patch_embed.proj.weight.dtype

    @property
    def device(self) -> torch.device:
        return self.patch_embed.proj.weight.device

    def rot_pos_emb(self, grid_thw: torch.Tensor) -> torch.Tensor:
        pos_ids = []
        for t, h, w in grid_thw:  # grid_thw shape 是 (1, 3), 假设值是 1,  98, 146
            # t=1, h=98, w=146
            hpos_ids = torch.arange(h).unsqueeze(1).expand(-1, w)  # (98, 146)
            wpos_ids = torch.arange(w).unsqueeze(0).expand(h, -1)   # (98, 146)
            hpos_ids = hpos_ids.reshape(
                h // self.spatial_merge_size,  # 49
                self.spatial_merge_size,  # 2
                w // self.spatial_merge_size,  # 73
                self.spatial_merge_size,  # 2
            ).permute(0, 2, 1, 3).flatten()  # (49, 73, 2, 2) => 14308
            wpos_ids = wpos_ids.reshape(
                h // self.spatial_merge_size,  # 49
                self.spatial_merge_size,  # 2
                w // self.spatial_merge_size,  # 73
                self.spatial_merge_size,  # 2
            ).permute(0, 2, 1, 3).flatten()  # (49, 73, 2, 2) => 14308
            pos_ids.append(
                torch.stack([hpos_ids, wpos_ids], dim=-1).repeat(t, 1))  # (14308, 2)
        pos_ids = torch.cat(pos_ids, dim=0)  # (14308, 2)
        max_grid_size = grid_thw[:, 1:].max()  # 146
        rotary_pos_emb_full = self.rotary_pos_emb(max_grid_size)  # (292, 20)
        rotary_pos_emb = rotary_pos_emb_full[pos_ids].flatten(1)  # (14308, 2, 20) => (14308, 40)
        return rotary_pos_emb

    def forward(
        self,
        x: torch.Tensor,  # (14308, 1176)
        grid_thw: torch.Tensor,  # (1, 3)
    ) -> torch.Tensor:
        # patchify
        # x 的 shape 是 (14308, 1176)
        x = x.to(device=self.device, dtype=self.dtype)
        x = self.patch_embed(x)  # (14308, 1280)

        # compute position embedding
        rotary_pos_emb = self.rot_pos_emb(grid_thw)  # (14308, 40)

        # compute cu_seqlens
        cu_seqlens = torch.repeat_interleave(grid_thw[:, 1] * grid_thw[:, 2],
                                             grid_thw[:, 0]).cumsum(
                                                 dim=0, dtype=torch.int32)
        # cu_seqlens 的值是 [14308]
        cu_seqlens = F.pad(cu_seqlens, (1, 0), "constant", 0)
        # cu_seqlens 的值是 [0, 14308]

        # transformers
        x = x.unsqueeze(1)  # (14308, 1, 1280)
        for blk in self.blocks:
            x = blk(x, cu_seqlens=cu_seqlens, rotary_pos_emb=rotary_pos_emb)  # (14308, 1, 1280)

        # adapter
        x = self.merger(x)
        return x

    def load_weights(self, weights: Iterable[Tuple[str,
                                                   torch.Tensor]]) -> Set[str]:
        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            ("qkv_proj", "q_proj", "q"),
            ("qkv_proj", "k_proj", "k"),
            ("qkv_proj", "v_proj", "v"),
        ]
        params_dict = dict(self.named_parameters(remove_duplicate=False))
        loaded_params: Set[str] = set()

        for name, loaded_weight in weights:
            for (param_name, weight_name, shard_id) in stacked_params_mapping:
                if weight_name not in name:
                    continue
                name = name.replace(weight_name, param_name)

                param = params_dict[name]
                weight_loader = param.weight_loader
                weight_loader(param, loaded_weight, shard_id)
                break
            else:
                if name.endswith("qkv.weight"):
                    visual_num_heads = self.num_heads
                    visual_embed_dim = self.embed_dim
                    head_size = visual_embed_dim // visual_num_heads
                    loaded_weight = loaded_weight.view(3, visual_num_heads,
                                                       head_size,
                                                       visual_embed_dim)
                    loaded_weight = loaded_weight.transpose(0, 1)
                    loaded_weight = loaded_weight.reshape(-1, visual_embed_dim)
                elif name.endswith("qkv.bias"):
                    visual_num_heads = self.num_heads
                    visual_embed_dim = self.embed_dim
                    head_size = visual_embed_dim // visual_num_heads
                    loaded_weight = loaded_weight.view(3, visual_num_heads,
                                                       head_size)
                    loaded_weight = loaded_weight.transpose(0, 1)
                    loaded_weight = loaded_weight.reshape(-1)

                param = params_dict[name]
                weight_loader = getattr(param, "weight_loader",
                                        default_weight_loader)
                weight_loader(param, loaded_weight)
            loaded_params.add(name)
        return loaded_params


# === Vision input helpers === #


def _get_vision_info(
    vision_config: Qwen2VLVisionConfig,
    height: int,
    width: int,
    min_pixels: int,
    max_pixels: int,
    *,
    do_resize: bool = True,
    modality: str = "image",
    mm_count: int = 1,
):
    """Get information (resized height / width and number of vision tokens)
    of input image / video frame."""
    patch_size = vision_config.patch_size
    merge_size = vision_config.spatial_merge_size
    temporal_patch_size = vision_config.temporal_patch_size

    if do_resize:
        resized_height, resized_width = smart_resize(
            height=height,
            width=width,
            factor=patch_size * merge_size,
            min_pixels=min_pixels,
            max_pixels=max_pixels,
        )
    else:
        resized_height, resized_width = height, width

    if modality == "image":
        grid_t = mm_count
    elif modality == "video":
        grid_t = max(mm_count // temporal_patch_size, 1)
    else:
        raise ValueError(f"Modality {modality} is not supported")

    grid_h = resized_height // patch_size
    grid_w = resized_width // patch_size
    vision_tokens = grid_t * grid_h * grid_w
    llm_num_vision_tokens = vision_tokens // (merge_size**2)

    return resized_height, resized_width, llm_num_vision_tokens


def _get_image_processor(hf_processor: Qwen2VLProcessor):
    image_processor = hf_processor.image_processor  # type: ignore
    assert isinstance(image_processor, Qwen2VLImageProcessor)
    return image_processor


class Qwen2EmbeddingItems(ModalityDataItems[dict[str, torch.Tensor],
                                            dict[str, torch.Tensor]]):

    def __init__(self, data: dict, modality: str) -> None:
        super().__init__(data, modality)

        grid_thw = data[f"{modality}_grid_thw"]
        slice_idxs = [0] + grid_thw.prod(-1).cumsum_(0).tolist()
        self._slices = [
            slice(slice_idxs[i], slice_idxs[i + 1])
            for i in range(len(grid_thw))
        ]

    def get_count(self) -> int:
        return len(self.data[f"{self.modality}_grid_thw"])

    def get(self, index: int) -> dict[str, torch.Tensor]:
        out = {}
        for k, v in self.data.items():
            if v != f"{self.modality}_grid_thw":
                v = v[self._slices[index]]

            out[k] = v

        return out

    def get_processor_data(self) -> Mapping[str, object]:
        return {}

    def get_passthrough_data(self) -> Mapping[str, object]:
        return self.data


class Qwen2ImageEmbeddingItems(Qwen2EmbeddingItems):

    def __init__(self, data: dict) -> None:
        super().__init__(data, "image")


class Qwen2VideoEmbeddingItems(Qwen2EmbeddingItems):

    def __init__(self, data: dict) -> None:
        super().__init__(data, "video")


class Qwen2MultiModalDataParser(MultiModalDataParser):

    def _parse_image_data(
        self,
        data: Union[dict[str, torch.Tensor], ModalityData[ImageItem]],
    ) -> ModalityDataItems[Any, Any]:
        if isinstance(data, dict):
            return Qwen2EmbeddingItems(data, modality="image")

        return super()._parse_image_data(data)

    def _parse_video_data(
        self,
        data: Union[dict[str, torch.Tensor], ModalityData[VideoItem]],
    ) -> ModalityDataItems[Any, Any]:
        if isinstance(data, dict):
            return Qwen2EmbeddingItems(data, modality="video")

        return super()._parse_video_data(data)


class Qwen2VLMultiModalProcessor(BaseMultiModalProcessor):

    def get_supported_mm_limits(self) -> Mapping[str, Optional[int]]:
        return {"image": None, "video": None}

    def _get_max_mm_tokens(self, modality: str) -> int:
        hf_config = self.ctx.get_hf_config(Qwen2VLConfig)
        vision_config = hf_config.vision_config

        hf_processor = self._get_hf_processor()
        image_processor = _get_image_processor(hf_processor)

        _, _, max_llm_image_tokens = _get_vision_info(
            vision_config,
            height=9999999,
            width=9999999,
            min_pixels=image_processor.min_pixels,
            max_pixels=image_processor.max_pixels,
            modality=modality,
        )
        return max_llm_image_tokens

    def get_mm_max_tokens_per_item(self) -> Mapping[str, int]:
        return {
            "image": self._get_max_mm_tokens("image"),
            "video": self._get_max_mm_tokens("video"),
        }

    def _get_data_parser(self) -> MultiModalDataParser:
        return Qwen2MultiModalDataParser()

    def _get_hf_processor(
        self,
        *,
        min_pixels: Optional[int] = None,
        max_pixels: Optional[int] = None,
    ) -> Qwen2VLProcessor:
        hf_processor = self.ctx.get_hf_processor(Qwen2VLProcessor)
        image_processor = _get_image_processor(hf_processor)

        if min_pixels:
            image_processor.min_pixels = min_pixels
        if max_pixels:
            image_processor.max_pixels = max_pixels
        if max_pixels or min_pixels:
            image_processor.size = {
                "min_pixels": image_processor.min_pixels,
                "max_pixels": image_processor.max_pixels,
            }

        return hf_processor

    def _get_prompt_replacements(
        self,
        mm_items: MultiModalDataItems,
        hf_processor_mm_kwargs: Mapping[str, object],
        out_mm_kwargs: MultiModalKwargs,
    ) -> list[PromptReplacement]:
        hf_processor = self._get_hf_processor()
        image_processor = _get_image_processor(hf_processor)

        # NOTE: Only Qwen2VLProcessor in transformers 4.47.0 has
        # image_token and video_token registered
        placeholder = {
            "image": hf_processor.image_token,
            "video": hf_processor.video_token,
        }
        merge_length = image_processor.merge_size**2

        def get_replacement_qwen2vl(item_idx: int, modality: str):
            grid_thw = out_mm_kwargs[f"{modality}_grid_thw"][item_idx]
            assert isinstance(grid_thw, torch.Tensor)

            num_tokens = grid_thw.prod() // merge_length
            return placeholder[modality] * num_tokens

        return [
            PromptReplacement(
                modality=modality,
                target=placeholder[modality],
                replacement=partial(get_replacement_qwen2vl,
                                    modality=modality),
            ) for modality in ("image", "video")
        ]

    def _get_mm_fields_config(
        self,
        hf_inputs: BatchFeature,
        hf_processor_mm_kwargs: Mapping[str, object],
    ) -> Mapping[str, MultiModalFieldConfig]:
        image_grid_thw = hf_inputs.get("image_grid_thw", torch.empty((0, 3)))
        image_slice_idxs = [0] + image_grid_thw.prod(-1).cumsum_(0).tolist()
        image_slices = [
            slice(image_slice_idxs[i], image_slice_idxs[i + 1])
            for i in range(len(image_grid_thw))
        ]

        video_grid_thw = hf_inputs.get("video_grid_thw", torch.empty((0, 3)))
        video_slice_idxs = [0] + video_grid_thw.prod(-1).cumsum_(0).tolist()
        video_slices = [
            slice(video_slice_idxs[i], video_slice_idxs[i + 1])
            for i in range(len(video_grid_thw))
        ]

        return dict(
            pixel_values=MultiModalFieldConfig.flat("image", image_slices),
            image_embeds=MultiModalFieldConfig.flat("image", image_slices),
            image_grid_thw=MultiModalFieldConfig.batched("image"),
            pixel_values_videos=MultiModalFieldConfig.flat(
                "video", video_slices),
            video_embeds=MultiModalFieldConfig.flat("video", video_slices),
            video_grid_thw=MultiModalFieldConfig.batched("video"),
        )

    def _get_dummy_mm_inputs(
        self,
        mm_counts: Mapping[str, int],
    ) -> ProcessorInputs:
        hf_processor = self._get_hf_processor()
        image_processor = _get_image_processor(hf_processor)

        image_token: str = hf_processor.image_token
        resized_height, resized_width = smart_resize(
            height=9999999,
            width=9999999,
            factor=image_processor.patch_size * image_processor.merge_size,
            min_pixels=image_processor.min_pixels,
            max_pixels=image_processor.max_pixels,
        )
        num_images = mm_counts.get("image", 0)

        mm_data = {
            "image":
            self._get_dummy_images(width=resized_width,
                                   height=resized_height,
                                   num_images=num_images)
        }

        return ProcessorInputs(
            prompt_text=image_token * num_images,
            mm_data=mm_data,
        )


@MULTIMODAL_REGISTRY.register_processor(Qwen2VLMultiModalProcessor)
class Qwen2VLForConditionalGeneration(nn.Module, SupportsMultiModal,
                                      SupportsLoRA, SupportsPP):
    packed_modules_mapping = {
        "qkv_proj": [
            "q_proj",
            "k_proj",
            "v_proj",
        ],
        "gate_up_proj": [
            "gate_proj",
            "up_proj",
        ],
    }

    # LoRA specific attributes
    supported_lora_modules = [
        "qkv_proj",
        "o_proj",
        "gate_up_proj",
        "down_proj",
        # vision tower
        "qkv",
        "attn.proj",  # Distinguish patch_embed.proj
        "fc1",
        "fc2",
        # projector
        "mlp.0",
        "mlp.2"
    ]
    embedding_modules = {}
    embedding_padding_modules = []

    # To ensure correct weight loading and mapping.
    hf_to_vllm_mapper = WeightsMapper(orig_to_new_prefix={
        "lm_head.": "language_model.lm_head.",
        "model.": "language_model.model.",
    })

    def __init__(self, *, vllm_config: VllmConfig, prefix: str = ""):
        super().__init__()
        config: Qwen2VLConfig = vllm_config.model_config.hf_config
        cache_config = vllm_config.cache_config
        quant_config = vllm_config.quant_config
        multimodal_config = vllm_config.model_config.multimodal_config
        assert not cache_config.enable_prefix_caching, \
            "Qwen2-VL currently does not support prefix caching"

        self.config = config
        self.multimodal_config = multimodal_config

        # 主要多了一个视觉模型
        self.visual = Qwen2VisionTransformer(
            config.vision_config,
            norm_eps=getattr(config, "rms_norm_eps", 1e-6),
            quant_config=self._maybe_ignore_quant_config(quant_config),
            prefix=maybe_prefix(prefix, "visual"),
        )

        self.language_model = init_vllm_registered_model(
            vllm_config=vllm_config,
            prefix=maybe_prefix(prefix, "language_model"),
            architectures=["Qwen2ForCausalLM"],
        )

        self.make_empty_intermediate_tensors = (
            self.language_model.make_empty_intermediate_tensors)

    @cached_property
    def sampler(self):
        if hasattr(self.language_model, "sampler"):
            return self.language_model.sampler

        return get_sampler()

    def _maybe_ignore_quant_config(self, quant_config: QuantizationConfig):
        # GPTQ configs do not have a list of ignored modules, however AutoGPTQ
        # seems to avoid vision encoder sections for some models.
        # See: https://huggingface.co/Qwen/Qwen2-VL-2B-Instruct-GPTQ-Int4
        if isinstance(quant_config, (GPTQConfig, GPTQMarlinConfig)):
            return None
        return quant_config

    def _validate_and_reshape_mm_tensor(self, mm_input: object,
                                        name: str) -> torch.Tensor:
        if not isinstance(mm_input, (torch.Tensor, list)):
            raise ValueError(f"Incorrect type of {name}. "
                             f"Got type: {type(mm_input)}")
        if isinstance(mm_input, torch.Tensor):
            if mm_input.ndim == 2:
                # 两个维度的情况下，直接返回
                return mm_input
            if mm_input.ndim != 3:
                raise ValueError(f"{name} should be 2D or batched 3D tensor. "
                                 f"Got ndim: {mm_input.ndim} "
                                 f"(shape={mm_input.shape})")
            # 三个维度下, 在第一个维度上进行拼接
            return torch.concat(list(mm_input))
        else:
            # 如果是list的情况下，直接拼接
            return torch.concat(mm_input)

    def _parse_and_validate_image_input(
            self, **kwargs: object) -> Optional[Qwen2VLImageInputs]:
        """
        解析验证图像输入
        """
        pixel_values = kwargs.pop("pixel_values", None)
        image_embeds = kwargs.pop("image_embeds", None)
        image_grid_thw = kwargs.pop("image_grid_thw", None)

        if pixel_values is None and image_embeds is None:
            return None

        if pixel_values is not None:
            # 如果传入了像素
            pixel_values = self._validate_and_reshape_mm_tensor(
                pixel_values, "image pixel values")
            image_grid_thw = self._validate_and_reshape_mm_tensor(
                image_grid_thw, "image grid_thw")

            if not isinstance(pixel_values, (torch.Tensor, list)):
                raise ValueError("Incorrect type of image pixel values. "
                                 f"Got type: {type(pixel_values)}")

            # 像素类型的输入
            return Qwen2VLImagePixelInputs(type="pixel_values",
                                           pixel_values=pixel_values,
                                           image_grid_thw=image_grid_thw)

        if image_embeds is not None:
            image_embeds = self._validate_and_reshape_mm_tensor(
                image_embeds, "image embeds")
            image_grid_thw = self._validate_and_reshape_mm_tensor(
                image_grid_thw, "image grid_thw")

            if not isinstance(image_embeds, torch.Tensor):
                raise ValueError("Incorrect type of image embeddings. "
                                 f"Got type: {type(image_embeds)}")
            # 图片嵌入类型的输入
            return Qwen2VLImageEmbeddingInputs(type="image_embeds",
                                               image_embeds=image_embeds,
                                               image_grid_thw=image_grid_thw)

    def _parse_and_validate_video_input(
            self, **kwargs: object) -> Optional[Qwen2VLVideoInputs]:
        # TODO: 先不看视频输入
        pixel_values_videos = kwargs.pop("pixel_values_videos", None)
        video_embeds = kwargs.pop("video_embeds", None)
        video_grid_thw = kwargs.pop("video_grid_thw", None)

        if pixel_values_videos is None and video_embeds is None:
            return None

        if pixel_values_videos is not None:
            pixel_values_videos = self._validate_and_reshape_mm_tensor(
                pixel_values_videos, "video pixel values")
            video_grid_thw = self._validate_and_reshape_mm_tensor(
                video_grid_thw, "video grid_thw")

            return Qwen2VLVideoPixelInputs(
                type="pixel_values_videos",
                pixel_values_videos=pixel_values_videos,
                video_grid_thw=video_grid_thw,
            )

        if video_embeds is not None:
            video_embeds = self._validate_and_reshape_mm_tensor(
                video_embeds, "video embeds")
            video_grid_thw = self._validate_and_reshape_mm_tensor(
                video_grid_thw, "video grid_thw")

            if not isinstance(video_embeds, torch.Tensor):
                raise ValueError("Incorrect type of video embeddings. "
                                 f"Got type: {type(video_embeds)}")
            return Qwen2VLVideoEmbeddingInputs(type="video_embeds",
                                               video_embeds=video_embeds,
                                               video_grid_thw=video_grid_thw)

    def _process_image_input(self,
                             image_input: Qwen2VLImageInputs) -> torch.Tensor:
        # 根据 type 判断
        if image_input["type"] == "image_embeds":
            return image_input["image_embeds"].type(self.visual.dtype)

        # 如果输入的是图片像素, 就需要经过视觉模型处理
        pixel_values = image_input["pixel_values"].type(self.visual.dtype)  # (14308, 1176)
        image_embeds = self.visual(pixel_values,
                                   grid_thw=image_input["image_grid_thw"])  # (1, 3)
        return image_embeds

    def _process_video_input(self,
                             video_input: Qwen2VLVideoInputs) -> torch.Tensor:
        if video_input["type"] == "video_embeds":
            return video_input["video_embeds"].type(self.visual.dtype)

        pixel_values_videos = video_input["pixel_values_videos"].type(
            self.visual.dtype)
        video_embeds = self.visual(pixel_values_videos,
                                   grid_thw=video_input["video_grid_thw"])
        return video_embeds

    def _merge_multimodal_embeddings(
        self,
        input_ids: torch.Tensor,
        inputs_embeds: torch.Tensor,
        multimodal_embeddings: torch.Tensor,
        placeholder_token_id: int,
    ) -> torch.Tensor:
        """合并多模态嵌入"""
        mask = (input_ids == placeholder_token_id)
        inputs_embeds[mask, :] = multimodal_embeddings
        return inputs_embeds

    def get_multimodal_embeddings(
            self, **kwargs) -> Optional[List[Tuple[NestedTensors, str]]]:

        image_input = self._parse_and_validate_image_input(**kwargs)
        video_input = self._parse_and_validate_video_input(**kwargs)
        if image_input is None and video_input is None:
            return None

        # We make a tuple of each embedding with its modality string. This is a
        # temporary workaround for models to handle mixed modalities when
        # get_multimodal_embeddings and get_input_embeddings are called
        # separately.
        # TODO(ywang96): Add support for mixed-modality inference for v1.
        multimodal_embeddings: List[Tuple[NestedTensors, str]] = []

        if image_input is not None:
            image_embeds = self._process_image_input(image_input)
            multimodal_embeddings.append((image_embeds, "image"))
        if video_input is not None:
            video_embeds = self._process_video_input(video_input)
            multimodal_embeddings.append((video_embeds, "video"))

        return multimodal_embeddings

    def get_input_embeddings(
        self,
        input_ids: torch.Tensor,
        multimodal_embeddings: Optional[List[Tuple[NestedTensors,
                                                   str]]] = None,
    ) -> torch.Tensor:
        inputs_embeds = self.language_model.get_input_embeddings(input_ids)
        if multimodal_embeddings is not None:
            for embeddings, modality in multimodal_embeddings:
                if modality == "image":
                    inputs_embeds = self._merge_multimodal_embeddings(
                        input_ids,
                        inputs_embeds,
                        embeddings,
                        placeholder_token_id=self.config.image_token_id,
                    )
                if modality == "video":
                    inputs_embeds = self._merge_multimodal_embeddings(
                        input_ids,
                        inputs_embeds,
                        embeddings,
                        placeholder_token_id=self.config.video_token_id,
                    )
        return inputs_embeds

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
        intermediate_tensors: Optional[IntermediateTensors] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        **kwargs: object,
    ) -> Union[torch.Tensor, IntermediateTensors]:
        """Run forward pass for Qwen2-VL.

        Args:
            input_ids: Flattened (concatenated) input_ids corresponding to a
                batch.
            positions: Flattened (concatenated) position ids corresponding to a
                batch.
                **NOTE**: If mrope is enabled (default setting for Qwen2-VL
                opensource models), the shape will be `(3, seq_len)`,
                otherwise it will be `(seq_len,).
            pixel_values: Pixel values to be fed to a model.
                `None` if no images are passed.
            image_grid_thw: Tensor `(n_images, 3)` of image 3D grid in LLM.
                `None` if no images are passed.
            pixel_values_videos: Pixel values of videos to be fed to a model.
                `None` if no videos are passed.
            video_grid_thw: Tensor `(n_videos, 3)` of video 3D grid in LLM.
                `None` if no videos are passed.
        """

        if intermediate_tensors is not None:
            inputs_embeds = None

        # NOTE: In v1, inputs_embeds is always generated at model runner, this
        # condition is for v0 compatibility.
        elif inputs_embeds is None:
            multimodal_embeddings = self.get_multimodal_embeddings(**kwargs)

            # We need to check for usage of mrope here in case there is
            # multimodal data.
            # TODO (ywang96): move this to model runner in V1.
            if multimodal_embeddings is not None and uses_mrope(self.config):
                assert positions.ndim == 2 and positions.size(0) == 3, (
                    "multimodal section rotary embedding requires "
                    f"(3, seq_len) positions, but got {positions.size()}")

            inputs_embeds = self.get_input_embeddings(input_ids,
                                                      multimodal_embeddings)
            input_ids = None

        hidden_states = self.language_model.model(
            input_ids=input_ids,
            positions=positions,
            kv_caches=kv_caches,
            attn_metadata=attn_metadata,
            intermediate_tensors=intermediate_tensors,
            inputs_embeds=inputs_embeds,
        )
        return hidden_states

    def compute_logits(
        self,
        hidden_states: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[torch.Tensor]:
        return self.language_model.compute_logits(hidden_states,
                                                  sampling_metadata)

    def sample(
        self,
        logits: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        return self.language_model.sample(logits, sampling_metadata)

    def load_weights(self, weights: Iterable[Tuple[str,
                                                   torch.Tensor]]) -> Set[str]:

        loader = AutoWeightsLoader(self)
        return loader.load_weights(weights, mapper=self.hf_to_vllm_mapper)

    def get_mm_mapping(self) -> MultiModelKeys:
        """
        Get the module prefix in multimodal models
        """
        return MultiModelKeys.from_string_field(
            language_model="language_model",
            connector="visual.",
            tower_model="visual.merger.")
