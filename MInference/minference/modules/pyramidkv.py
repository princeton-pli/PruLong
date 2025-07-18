# Copyright (c) 2024 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Refer to the code in https://github.com/Zefan-Cai/PyramidKV/blob/main/pyramidkv/pyramidkv_utils.py

import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from .quest import repeat_kv

class PyramidKVCluster:
    def __init__(
        self,
        num_hidden_layers=32,
        window_size=64,
        max_capacity_prompt=256 + 64,
        kernel_size=5,
        pooling="avgpool",
        beta=20,
        num_layers=80,
        layer_idx=None,
        n_rep=None,
    ):
        self.layer_idx = layer_idx
        self.num_hidden_layers = num_hidden_layers

        self.steps = -1
        self.beta = beta

        self.window_size = window_size
        self.max_capacity_prompt = max_capacity_prompt
        assert self.max_capacity_prompt - self.window_size > 0
        self.kernel_size = kernel_size
        self.pooling = pooling
        self.n_rep = n_rep

    def reset(
        self,
        window_size=64,
        max_capacity_prompt=256 + 64,
        kernel_size=5,
        pooling="avgpool",
        n_rep=None,
    ):
        self.window_size = window_size
        self.max_capacity_prompt = max_capacity_prompt
        assert self.max_capacity_prompt - self.window_size > 0
        self.kernel_size = kernel_size
        self.pooling = pooling
        self.n_rep = n_rep

    def update_kv(
        self,
        key_states,
        query_states,
        value_states,
        attention_mask,
        num_key_value_groups,
        capacity_override=None,
    ):
        # check if prefix phase
        assert key_states.shape[-2] == query_states.shape[-2]
        if self.n_rep is None:
            bsz, num_heads, q_len, head_dim = query_states.shape
        else:
            bsz, num_kv_heads, q_len, head_dim = key_states.shape
            num_heads = num_kv_heads * self.n_rep
        
        k_cur = key_states[:, :, -self.window_size :, :]
        v_cur = value_states[:, :, -self.window_size :, :]

        # TODO
        # window_sizes = 32
        # capacity_override overrides self.max_capacity_prompt
        current_max_capacity = capacity_override if capacity_override is not None else self.max_capacity_prompt

        min_num = (current_max_capacity - self.window_size) // self.beta
        max_num = (current_max_capacity - self.window_size) * 2 - min_num

        if max_num >= q_len - self.window_size:
            max_num = q_len - self.window_size
            min_num = (current_max_capacity - self.window_size) * 2 - max_num

        steps = (max_num - min_num) // (self.num_hidden_layers - 1)
        max_capacity_prompt = max_num - self.layer_idx * steps

        # print(f"PyramidKV max_capacity_prompt {max_capacity_prompt}")
        if q_len < current_max_capacity:
            return key_states, value_states
        elif q_len < (current_max_capacity - self.window_size) * 2:
            if self.n_rep is None:
                key_states_for_calculation = key_states
            else:
                key_states_for_calculation = repeat_kv(key_states, self.n_rep)

            attn_weights = torch.matmul(
                query_states[..., -self.window_size :, :], key_states_for_calculation.transpose(2, 3)
            ) / math.sqrt(head_dim)
            mask = torch.full(
                (self.window_size, self.window_size),
                torch.finfo(attn_weights.dtype).min,
                device=attn_weights.device,
            )
            mask_cond = torch.arange(mask.size(-1), device=attn_weights.device)
            mask.masked_fill_(mask_cond < (mask_cond + 1).view(mask.size(-1), 1), 0)
            mask = mask.to(attn_weights.device)
            attention_mask = mask[None, None, :, :]

            attn_weights[
                :, :, -self.window_size :, -self.window_size :
            ] += attention_mask

            attn_weights = nn.functional.softmax(
                attn_weights, dim=-1, dtype=torch.float32
            ).to(query_states.dtype)
            attn_weights_sum = attn_weights[
                :, :, -self.window_size :, : -self.window_size
            ].sum(dim=-2)
            
            if self.pooling == "avgpool":
                attn_cache = F.avg_pool1d(
                    attn_weights_sum,
                    kernel_size=self.kernel_size,
                    padding=self.kernel_size // 2,
                    stride=1,
                )
            elif self.pooling == "maxpool":
                attn_cache = F.max_pool1d(
                    attn_weights_sum,
                    kernel_size=self.kernel_size,
                    padding=self.kernel_size // 2,
                    stride=1,
                )
            else:
                raise ValueError("Pooling method not supported")
            
            if self.n_rep is not None:
                # The attn is are currently [bsz, num_heads, window_size, q_len]
                # We need to convert them to [bsz, num_kv_heads, window_size, q_len] by mean pooling
                l = attn_cache.shape[-1]
                attn_cache = attn_cache.view(bsz, num_kv_heads, self.n_rep, l)
                attn_cache = attn_cache.mean(dim=2)

            if current_max_capacity <= self.window_size:
                key_states = k_cur
                value_states = v_cur
            else:
                indices = attn_cache.topk(
                    min(
                        current_max_capacity - self.window_size,
                        attn_cache.shape[-1]
                    ), 
                    dim=-1
                ).indices
                indices = indices.sort(dim=-1)[0] # New: sort in the correct order of tokens
                
                indices = indices.unsqueeze(-1).expand(-1, -1, -1, head_dim)
                k_past_compress = key_states[:, :, : -self.window_size, :].gather(
                    dim=2, index=indices
                )
                v_past_compress = value_states[:, :, : -self.window_size, :].gather(
                    dim=2, index=indices
                )
                k_cur = key_states[:, :, -self.window_size :, :]
                v_cur = value_states[:, :, -self.window_size :, :]
                key_states = torch.cat([k_past_compress, k_cur], dim=2)
                value_states = torch.cat([v_past_compress, v_cur], dim=2)
            return key_states, value_states
        else:
            if self.n_rep is None:
                key_states_for_calculation = key_states
            else:
                key_states_for_calculation = repeat_kv(key_states, self.n_rep)

            attn_weights = torch.matmul(
                query_states[..., -self.window_size :, :], key_states_for_calculation.transpose(2, 3)
            ) / math.sqrt(head_dim)
            mask = torch.full(
                (self.window_size, self.window_size),
                torch.finfo(attn_weights.dtype).min,
                device=attn_weights.device,
            )
            mask_cond = torch.arange(mask.size(-1), device=attn_weights.device)
            mask.masked_fill_(mask_cond < (mask_cond + 1).view(mask.size(-1), 1), 0)
            mask = mask.to(attn_weights.device)
            attention_mask = mask[None, None, :, :]

            attn_weights[
                :, :, -self.window_size :, -self.window_size :
            ] += attention_mask

            attn_weights = nn.functional.softmax(
                attn_weights, dim=-1, dtype=torch.float32
            ).to(query_states.dtype)
            attn_weights_sum = attn_weights[
                :, :, -self.window_size :, : -self.window_size
            ].sum(dim=-2)
            if self.pooling == "avgpool":
                attn_cache = F.avg_pool1d(
                    attn_weights_sum,
                    kernel_size=self.kernel_size,
                    padding=self.kernel_size // 2,
                    stride=1,
                )
            elif self.pooling == "maxpool":
                attn_cache = F.max_pool1d(
                    attn_weights_sum,
                    kernel_size=self.kernel_size,
                    padding=self.kernel_size // 2,
                    stride=1,
                )
            else:
                raise ValueError("Pooling method not supported")
            
            if self.n_rep is not None:
                # The attn is are currently [bsz, num_heads, window_size, q_len]
                # We need to convert them to [bsz, num_kv_heads, window_size, q_len] by mean pooling
                l = attn_cache.shape[-1]
                attn_cache = attn_cache.view(bsz, num_kv_heads, self.n_rep, l)
                attn_cache = attn_cache.mean(dim=2)

            if current_max_capacity <= self.window_size:
                key_states = key_states[:, :, -self.window_size:, :]
                value_states = value_states[:, :, -self.window_size:, :]
            else:
                indices = attn_cache.topk(
                    min(
                        max_capacity_prompt,
                        attn_cache.shape[-1]
                    ), 
                    dim=-1
                ).indices
                indices = indices.sort(dim=-1)[0] # New: sort in the correct order of tokens
                
                indices = indices.unsqueeze(-1).expand(-1, -1, -1, head_dim)
                k_past_compress = key_states[:, :, : -self.window_size, :].gather(
                    dim=2, index=indices
                )
                v_past_compress = value_states[:, :, : -self.window_size, :].gather(
                    dim=2, index=indices
                )
                k_cur = key_states[:, :, -self.window_size :, :]
                v_cur = value_states[:, :, -self.window_size :, :]
                key_states = torch.cat([k_past_compress, k_cur], dim=2)
                value_states = torch.cat([v_past_compress, v_cur], dim=2)
            return key_states, value_states
