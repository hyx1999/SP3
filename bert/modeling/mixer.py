import re
import torch
import torch.nn as nn
import numpy as np
import math
from datasets import Dataset
from typing import Union, Tuple, Dict, List
from collections import defaultdict

from .modeling_compact_bert import (
    CompactBertLayer,
    CompactBertForSequenceClassification,
    CompactBertForQuestionAnswering
)
from .modeling_packed_bert import (
    PackedBertLayerConfig,
    PackedBertConfig,
    PackedBertForSequenceClassification,
    PackedBertForQuestionAnswering,
)
from .compactor import (
    Mask,
    LayerNormWithCompactor,
    LinearWithCompactorBefore,
    LinearWithCompactorAfter,
    LinearWithMaskBefore,
)
from copy import deepcopy
from tqdm import tqdm


Models = Union[CompactBertForSequenceClassification, CompactBertForQuestionAnswering]
PModels = Union[PackedBertForSequenceClassification, PackedBertForQuestionAnswering]


def get_packed_bert_config(model: Models):
    config = model.config
    p_config = PackedBertConfig(**vars(config))

    per_layer_config = []
    past_layer_norm = model.bert.embeddings.LayerNorm[1]
    for layer in model.bert.encoder.layer:
        attn_output_norm = layer.attention.output.LayerNorm
        ffn_output_norm = layer.output.LayerNorm
        w_q = layer.attention.self.query
        w_v = layer.attention.self.value
        w_0 = layer.intermediate.dense

        layer_config = PackedBertLayerConfig(
            input_dim=past_layer_norm.packed_size,
            attn_output_dim=attn_output_norm.packed_size,
            ffn_output_dim=ffn_output_norm.packed_size,
            num_heads=config.num_attention_heads,
            qk_dim=w_q.out_features,
            vo_dim=w_v.out_features,
            ffn_dim=w_0.out_features,
            prune_attn=False,
            prune_ffn=False
        )
        per_layer_config.append(layer_config)        
        past_layer_norm = ffn_output_norm

    p_config.per_layer_config = per_layer_config
    return p_config


class LinearMixer:
    
    def __init__(self, linear: nn.Linear) -> None:
        self.linear = linear
    
    @torch.no_grad()
    def merge(self, other: nn.Linear) -> 'LinearMixer':
        in_features = self.linear.in_features
        out_features = other.out_features
        bias = (self.linear.bias is not None) and (other.bias is not None)
        new_linear = nn.Linear(in_features, out_features, bias)
        
        new_weight = other.weight @ self.linear.weight
        new_bias = 0
        if self.linear.bias is not None:
            new_bias = new_bias + other.weight @ self.linear.bias
        if other.bias is not None:
            new_bias = new_bias + other.bias
        
        new_linear.weight.copy_(new_weight)
        if new_linear.bias is not None:
            new_linear.bias.copy_(new_bias)
        
        return LinearMixer(new_linear)

    @torch.no_grad()
    def merge_mask(self, indices: torch.Tensor) -> 'LinearMixer':
        in_features = self.linear.in_features
        out_features = indices.shape[0]
        bias = self.linear.bias is not None
        new_linear = nn.Linear(in_features, out_features, bias)
        new_linear.weight.copy_(self.linear.weight[indices, :])
        if new_linear.bias is not None:
            new_linear.bias.copy_(self.linear.bias[indices])
        return LinearMixer(new_linear)

    def unwrap(self) -> nn.Linear:
        return self.linear


class CompactorMixer:
    
    def __init__(self,
        args,
        model: Models,
        factory: PModels,
    ) -> None:
        self.args = args
        self.model = model
        self.factory = factory
        
        self.model.to("cpu")
        self.model.eval()
        
    @torch.no_grad()   
    def mix(self):
        
        past_layer_norm = self.model.bert.embeddings.LayerNorm        
        num_layers = len(self.model.bert.encoder.layer)
        for layer_id, module in tqdm(enumerate(self.model.bert.encoder.layer), total=num_layers):
            assert isinstance(module, CompactBertLayer)              
            # 0. get modules
            q_module: LinearWithCompactorAfter = module.attention.self.query
            k_module: LinearWithCompactorAfter = module.attention.self.key
            v_module: LinearWithCompactorAfter = module.attention.self.value
            o_module: LinearWithCompactorBefore = module.attention.output.dense

            w1_module: nn.Linear = module.intermediate.dense
            w2_module: LinearWithMaskBefore = module.output.dense

            norm0: LayerNormWithCompactor = past_layer_norm
            norm1: LayerNormWithCompactor = module.attention.output.LayerNorm
            norm2: LayerNormWithCompactor = module.output.LayerNorm                

            norm0_values, norm0_indices = norm0.mask.parse()
            norm1_values, norm1_indices = norm1.mask.parse()
            norm2_values, norm2_indices = norm2.mask.parse()
            qk_values, qk_indices = q_module.mask.parse()
            vo_values, vo_indices = v_module.mask.parse()
            ffn_values, ffn_indices = w2_module.mask.parse()
            
            # 1. update layer-norm
            module.attention.output.LayerNorm = \
                norm1.extract(norm1_indices)
            module.output.LayerNorm = \
                norm2.extract(norm2_indices)
            
            # 2. update attention                        
            norm0_out_comp = norm0.out_comp.extract("input", norm0_indices, norm0_values)
            norm1_in_comp = norm1.in_comp.extract("output", norm1_indices, norm1_values)
            query = LinearMixer(norm0_out_comp)\
                .merge(q_module.extract())\
                .merge(q_module.compactor.extract("output", qk_indices, qk_values))\
                .unwrap()
            key = LinearMixer(norm0_out_comp)\
                .merge(k_module.extract())\
                .merge(k_module.compactor.extract("output", qk_indices))\
                .unwrap()
            value = LinearMixer(norm0_out_comp)\
                .merge(v_module.extract())\
                .merge(v_module.compactor.extract("output", vo_indices, vo_values))\
                .unwrap()
            output = LinearMixer(
                    o_module.compactor.extract("input", vo_indices)
                )\
                .merge(o_module.extract())\
                .merge(norm1_in_comp)\
                .unwrap()

            module.attention.self.query = query
            module.attention.self.key = key
            module.attention.self.value = value
            module.attention.output.dense = output

            # 3. update FFN
            norm1_out_comp = norm1.out_comp.extract("input", norm1_indices, norm1_values)
            norm2_in_comp = norm2.in_comp.extract("output", norm2_indices, norm2_values)
            w1 = LinearMixer(norm1_out_comp)\
                .merge(w1_module)\
                .merge_mask(ffn_indices)\
                .unwrap()
            w2 = LinearMixer(w2_module.extract(ffn_indices, ffn_values))\
                .merge(norm2_in_comp)\
                .unwrap()
            
            module.intermediate.dense = w1
            module.output.dense = w2
            
            # 4. update residual
            module.attention.output.residual = \
                LinearMixer(norm0_out_comp)\
                .merge(norm1_in_comp).unwrap()
            module.output.residual = \
                LinearMixer(norm1_out_comp)\
                .merge(norm2_in_comp).unwrap()
            
            # 5. update past_layer_norm
            past_layer_norm = norm2

        first_norm: LayerNormWithCompactor = self.model.bert.embeddings.LayerNorm
        last_norm: LayerNormWithCompactor = past_layer_norm
        
        first_values, first_indices = first_norm.mask.parse()
        last_values, last_indices = last_norm.mask.parse()

        assert self.model.bert.pooler is not None

        self.model.bert.embeddings.LayerNorm = nn.Sequential(
            first_norm.in_comp.extract("output", first_indices, first_values),
            first_norm.extract(first_indices)
        )
        self.model.bert.pooler.dense = LinearMixer(
                last_norm.out_comp.extract("input", last_indices, last_values)
            )\
            .merge(self.model.bert.pooler.dense)\
            .unwrap()

        p_config = get_packed_bert_config(self.model)
        p_model: PModels = self.factory(p_config)
        p_model.load_state_dict(self.model.state_dict(), strict=False)
        
        return p_model

# # TEST

# @torch.no_grad()
# def fn0(x_test):
#     q_test = q_module(norm0.out_comp(x_test))
#     k_test = k_module(norm0.out_comp(x_test))
#     return torch.einsum('ik,jk->ij', q_test, k_test)

# @torch.no_grad()
# def fn1(x_test):
#     q1 = q_module.extract()
#     q2 = q_module.compactor
#     k1 = k_module.extract()
#     k2 = k_module.compactor
#     q_test = q2(q1(norm0.out_comp(x_test)))
#     k_test = k2(k1(norm0.out_comp(x_test)))
#     return torch.einsum('ik,jk->ij', q_test, k_test)

# @torch.no_grad()
# def fn2(x_test):
#     x_test = x_test[..., norm0_indices]
#     q_test = query(x_test)
#     k_test = key(x_test)
#     return torch.einsum('ik,jk->ij', q_test, k_test)

# @torch.no_grad()
# def fn3(x_test):
#     o = o_module(v_module(norm0.out_comp(x_test)))
#     o = norm1.in_comp(o)[..., norm1_indices]
#     return o

# @torch.no_grad()
# def fn4(x_test):
#     v1 = v_module.extract()
#     v2 = v_module.compactor
#     o1 = o_module.compactor
#     o2 = o_module.extract()
#     o = o2(o1(v2(v1(norm0.out_comp(x_test)))))
#     o = norm1.in_comp(o)[..., norm1_indices]
#     return o

# @torch.no_grad()
# def fn5(x_test):
#     o = output(value(x_test[..., norm0_indices]))
#     return o

# x_test = torch.randn(3, 768)
# diff = (fn0(x_test) - fn1(x_test)).abs().sum()
# diff2 = (fn0(x_test) - fn2(x_test)).abs().sum()
# print(diff, diff2)
# x_test = torch.randn(3, 768)
# diff = (fn3(x_test) - fn4(x_test)).abs().sum()
# diff2 = (fn3(x_test) - fn5(x_test)).abs().sum()
# print(diff, diff2)

# # TEST END

# # TEST

# @torch.no_grad()
# def fn6(x_test):
#     output = norm2.in_comp(w2_module(w1_module(norm1.out_comp(x_test))))
#     return output[..., norm2_indices]

# @torch.no_grad()
# def fn7(x_test):
#     output = w2(w1(x_test[..., norm1_indices]))
#     return output

# x_test = torch.randn(3, 768)
# diff = (fn6(x_test) - fn7(x_test)).abs().sum()
# print(diff)           

# # TEST END