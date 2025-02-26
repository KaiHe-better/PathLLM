# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# timm: https://github.com/rwightman/pytorch-image-models/tree/master/timm
# DeiT: https://github.com/facebookresearch/deit
# MAE: https://github.com/facebookresearch/mae
# --------------------------------------------------------
#
# Portions Copyright Prov-GigaPath
# Original File: https://github.com/facebookresearch/mae

from functools import partial

import os
import sys
import torch
import torch.nn as nn
import numpy as np

import timm
from timm.layers import Mlp, DropPath
from timm.models.registry import register_model
import huggingface_hub
from transformers import BertModel, BertConfig

from .pos_embed import get_2d_sincos_pos_embed
from .torchscale.model.LongNet import make_longnet_from_name

class LayerScale(nn.Module):
    def __init__(self, dim, init_values=1e-5, inplace=False):
        super().__init__()
        self.inplace = inplace
        self.gamma = nn.Parameter(init_values * torch.ones(dim))

    def forward(self, x):
        return x.mul_(self.gamma) if self.inplace else x * self.gamma

class CrossAttention(nn.Module):
    def __init__(self, embed_dim, num_heads, dropout=0.1):
        super(CrossAttention, self).__init__()
        self.attn = nn.MultiheadAttention(embed_dim, num_heads, dropout=dropout, batch_first=True)
        self.norm = nn.LayerNorm(embed_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, query, key, value, key_padding_mask=None, return_weights=False):
        attn_output, attn_weights = self.attn(query, key, value, key_padding_mask=key_padding_mask)
        output = self.norm(query + self.dropout(attn_output))
        
        if return_weights:
            return output, attn_weights  # Return output and attention weights
        return output

class TransformerBlock(nn.Module):
    def __init__(self, dim, num_heads, ff_dim, init_values=1e-5, dropout=0.1, drop_path=0.1, pre_norm=True):
        super().__init__()
        self.pre_norm = pre_norm
        self.attention = nn.MultiheadAttention(dim, num_heads, dropout=dropout, batch_first=True)
        self.norm1 = nn.LayerNorm(dim)
        self.layerscale1 = LayerScale(dim, init_values=init_values)
        self.drop_path1 = DropPath(drop_path) if drop_path > 0 else nn.Identity()

        self.ffn = Mlp(in_features=dim, hidden_features=ff_dim, drop=dropout)
        self.norm2 = nn.LayerNorm(dim)
        self.layerscale2 = LayerScale(dim, init_values=init_values)
        self.drop_path2 = DropPath(drop_path) if drop_path > 0 else nn.Identity()

    def forward(self, x, attention_mask=None):
        attention_mask = (attention_mask == 0) if attention_mask is not None else None

        if self.pre_norm:
            x_norm = self.norm1(x)
            attn_output, _ = self.attention(x_norm, x_norm, x_norm, key_padding_mask=attention_mask)
            x = x + self.drop_path1(self.layerscale1(attn_output))

            x_norm = self.norm2(x)
            ffn_output = self.ffn(x_norm)
            x = x + self.drop_path2(self.layerscale2(ffn_output))
        else:
            attn_output, _ = self.attention(x, x, x, key_padding_mask=attention_mask)
            x = x + self.drop_path1(self.layerscale1(self.norm1(attn_output)))

            ffn_output = self.ffn(x)
            x = x + self.drop_path2(self.layerscale2(self.norm2(ffn_output)))

        return x

class LongNetViT(nn.Module):
    """
    Backbone of Vision Transformer for downstream tasks

    Arguments:
    ----------
    in_chans: int
        The number of input channels, should be the llm encoding dimension 4096.
    embed_dim: int
        The embedding dimension of the LongNet model.
    depth: int
        The number of LongNet layers in the LongNet model.
    slide_ngrids: int
        The number of grids in the slide.
    tile_size: int
        The tile size. Default is 256px.
    max_wsi_size: int
        The maximum size of the WSI.
    norm_layer: nn.LayerNorm
        The normalization layer used in the model.
    global_pool: bool
        Whether to use global pooling or not.
    dropout: float
        The dropout rate used in the model.
    drop_path_rate: float
        The drop path rate used in the model.
    num_layers: int
        The number of stacked "encoder and xatten"
    """

    def __init__(self, 
                in_chans=4096, 
                embed_dim=512,
                depth=12, 
                slide_ngrids=1000, 
                tile_size=256,
                max_wsi_size=262144,
                norm_layer=nn.LayerNorm, 
                dropout=0.25, 
                drop_path_rate=0.1,
                num_layers = 2,
                num_heads = 8,
                ff_dim = 2048,
                **kwargs):
        super().__init__()

        # --------------------------------------------------------------------------
        print("####Vision-Text Interaction based Adaptors (Longnet) ####")

        self.slide_ngrids = slide_ngrids
        num_patches = slide_ngrids**2

        self.register_buffer('pos_embed', torch.zeros(1, num_patches, embed_dim), persistent=True)  # fixed sin-cos embedding
        self.num_layers = num_layers

        self.encoder_name = "LongNet_{}_layers_{}_dim".format(depth, embed_dim)
        if kwargs.get("mlp_ratio", 4.0) != 4.0:
            self.encoder_name += "_mlp{}".format(kwargs.get("mlp_ratio"))
        
        # get optimal segment length
        # segment_length = self.get_optimal_segment_length(max_wsi_size, tile_size)
        # print(segment_length)
        # fixed segment for all levels
        segment_length = '[512, 1024, 2048, 4096, 8192]'

        self.encoder_wsi = nn.ModuleList([make_longnet_from_name(self.encoder_name, drop_path_rate=drop_path_rate, dropout=dropout, segment_length=segment_length)
                       for _ in range(num_layers)])

        # self.self_attention = nn.ModuleList([BertModel(config_self) for _ in range(num_layers)])
        self.self_attention = nn.ModuleList([TransformerBlock(embed_dim, num_heads, ff_dim) for _ in range(num_layers)])
        # nn.ModuleList([SelfAttention(embed_dim, num_heads) for _ in range(num_layers)])

        self.cross_attention = nn.ModuleList([CrossAttention(embed_dim, num_heads) for _ in range(num_layers)])

        # self.encoder2 = make_longnet_from_name(self.encoder_name, drop_path_rate=drop_path_rate, dropout=dropout, segment_length=segment_length)
        # self.norm = nn.ModuleList([norm_layer(embed_dim) for _ in range(num_layers)])
        # --------------------------------------------------------------------------

        self.initialize_vit_weights()

    def initialize_vit_weights(self):
        # initialization
        # initialize (and freeze) pos_embed by sin-cos embedding
        pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1], self.slide_ngrids, cls_token=False)
        self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

        # initialize reduce like nn.Linear (instead of nn.Conv2d)
        # w = self.reduce.proj.weight.data
        # torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

        # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
        # torch.nn.init.normal_(self.cls_token, std=0.02)

        # initialize nn.Linear and nn.LayerNorm
        self.apply(self._init_weights)

    def get_optimal_segment_length(self, max_wsi_size: int=262144, tile_size: int=256) -> str:
        '''
        Get the optimal segment length based on the maximum image size and tile size.
        
        Arguments:
        ----------
        max_wsi_size: int
            The maximum size of the WSI.
        tile_size: int
            The tile size.
        '''
        max_seq_len = (max_wsi_size // tile_size) ** 2
        # calculate the segment length
        segment_length = np.linspace(np.log2(512), int(np.log2(max_seq_len)), 5)
        segment_length = np.power(2, segment_length).astype(int)
        # convert to str format
        segment_length = str(list(segment_length))
        return segment_length

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def coords_to_pos(self, coords, patch_size=256.0):
        """
        This function is used to convert the coordinates to the positional indices

        Arguments:
        ----------
        coords: torch.Tensor
            The coordinates of the patches, of shape [N, L, 2]
        output: torch.Tensor
            The positional indices of the patches, of shape [N, L]
        """
        coords_ = torch.floor(coords / patch_size)
        pos = coords_[..., 0] * self.slide_ngrids + coords_[..., 1]
        return pos.long() # + 1  # add 1 for the cls token

    def forward(self, querys , contexts, instructs, coords, patch_size=256.0, self_attention_mask=None, key_padding_mask=None):
        """
        The forward pass of the model

        Arguments:
        ----------
        contexts: torch.Tensor
            The input tile embeddings, of shape [N, L, D]
        coords: torch.Tensor
            The coordinates of the patches, of shape [N, L, 2]
        """

        query_length = querys.shape[1]
        # mask for self attn
        if self_attention_mask is not None:
            # instruct_tensor requires a mask of the same size filled with ones
            query_mask_extension = torch.ones((querys.shape[0], querys.shape[1]), dtype=self_attention_mask.dtype, device=self_attention_mask.device)
            self_attention_mask = torch.cat((query_mask_extension, self_attention_mask), dim=-1)

        # embed patches
        # get pos indices
        pos = self.coords_to_pos(coords=coords, patch_size=patch_size)  # [N, L]
        contexts = contexts + self.pos_embed[:, pos, :].squeeze(0)
        # embed instruct, 4096 -> 512
        # instructs = self.reduce(instructs)

        for i in range(self.num_layers):
            # longnet for wsi tokens
            contexts = self.encoder_wsi[i](src_tokens=None, token_embeddings=contexts, encoder_padding_mask=key_padding_mask)["encoder_out"] # [:,1:,:]
            # self-attention for querys and instructs interaction
            combined_querys = torch.cat((querys, instructs), dim=1)
            self_attn_output = self.self_attention[i](combined_querys, attention_mask=self_attention_mask)
            querys = self_attn_output[:, :query_length, :] # keep query vector only
            # norm querys
            # Cross-Attention: key_padding_mask for padded patch tokens
            querys = self.cross_attention[i](query=querys, key=contexts, value=contexts, key_padding_mask=key_padding_mask)

        return querys.to(torch.bfloat16)
            

        # outcomes = []
        # print("x_list:",len(x_list))
        # for x in x_list:
        #     print("before pool:",x.shape)
        #     if self.global_pool:
        #         x = x[:, 1:, :].mean(dim=1)  # global average pooling
        #         outcome = self.norm(x)
        #         print("after pool:",x.shape)
        #     else:
        #         x = self.norm(x)
        #         outcome = x[:, 0]
        #     outcomes.append(outcome)

        # return outcomes


def create_model(pretrained: str, model_arch: str, tile_size: int, local_dir: str = os.path.join(os.path.expanduser("~"), ".cache/"), **kwargs):
    model = timm.create_model(model_arch, pretrained=False, tile_size=tile_size, **kwargs)

    if pretrained.startswith("hf_hub:"):
        hub_name = pretrained.split(":")[1]
        huggingface_hub.hf_hub_download(hub_name, filename="slide_encoder.pth", local_dir=local_dir, force_download=True)
        local_path = os.path.join(local_dir, "slide_encoder.pth")
    else:
        local_path = pretrained

    if os.path.exists(local_path):
        state_dict = torch.load(local_path, map_location="cpu")["model"]

        missing_keys, unexpected_keys = model.load_state_dict(state_dict, strict=False)
        if len(missing_keys) > 0:
            for k in missing_keys:
                print("Missing ", k)

        if len(unexpected_keys) > 0:
            for k in unexpected_keys:
                print("Unexpected ", k)

        print("\033[92m Successfully Loaded Pretrained GigaPath model from {} \033[00m".format(pretrained))
    else:
        print("\033[93m Pretrained weights not found at {}. Randomly initialized the model! \033[00m".format(local_path))

    return model


@register_model
def gigapath_slide_enc2l512d(**kwargs):
    model = LongNetViT(embed_dim=512, depth=2, mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs).to(torch.bfloat16)
    return model
