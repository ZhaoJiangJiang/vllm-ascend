#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# Copyright 2023 The vLLM team.
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
# This file is a part of the vllm-ascend project.
# Adapted from vllm-project/vllm/vllm/worker/worker.py
#
import dataclasses
from typing import Any, Dict, List, Optional, Tuple, Type

import torch
from vllm.distributed import get_pp_group
from vllm.model_executor.pooling_metadata import PoolingMetadata
from vllm.multimodal import MultiModalKwargs
from vllm.pooling_params import PoolingParams
from vllm.sequence import (IntermediateTensors, SequenceData,
                           SequenceGroupMetadata)

from vllm_ascend.ascend_forward_context import set_ascend_forward_context
from vllm_ascend.worker.model_runner import (ModelInputForNPU,
                                             ModelInputForNPUBuilder,
                                             NPUModelRunnerBase)


@dataclasses.dataclass(frozen=True)
class ModelInputForNPUWithPoolingMetadata(ModelInputForNPU):
    """
    Used by the PoolingModelRunner.
    """
    pooling_metadata: Optional["PoolingMetadata"] = None


class NPUPoolingModelRunner(
        NPUModelRunnerBase[ModelInputForNPUWithPoolingMetadata]):

    _model_input_cls: Type[ModelInputForNPUWithPoolingMetadata] = (
        ModelInputForNPUWithPoolingMetadata)
    _builder_cls: Type[ModelInputForNPUBuilder] = ModelInputForNPUBuilder

    def make_model_input_from_broadcasted_tensor_dict(
            self,
            tensor_dict: Dict[str,
                              Any]) -> ModelInputForNPUWithPoolingMetadata:
        return ModelInputForNPUWithPoolingMetadata.from_broadcasted_tensor_dict(
            tensor_dict,
            attn_backend=self.attn_backend,
        )

    def prepare_model_input(
        self,
        seq_group_metadata_list: Optional[List[SequenceGroupMetadata]],
        virtual_engine: int = 0,
        finished_requests_ids: Optional[List[str]] = None
    ) -> ModelInputForNPUWithPoolingMetadata:
        assert seq_group_metadata_list is not None
        model_input = self._prepare_model_input_tensors(
            seq_group_metadata_list, finished_requests_ids)
        # Prepare PoolingMetadata.
        assert model_input.seq_lens is not None
        pooling_metadata = self._prepare_pooling(seq_group_metadata_list,
                                                 model_input.seq_lens)

        return dataclasses.replace(model_input,
                                   pooling_metadata=pooling_metadata)

    def _prepare_pooling(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        prompt_lens: List[int],
    ) -> PoolingMetadata:
        """Prepare PoolingMetadata for the sequence group metadata list."""
        seq_groups: List[Tuple[List[int], PoolingParams]] = []
        for i, seq_group_metadata in enumerate(seq_group_metadata_list):
            seq_ids = list(seq_group_metadata.seq_data.keys())
            pooling_params = seq_group_metadata.pooling_params
            seq_groups.append((seq_ids, pooling_params))

        seq_data: Dict[int, SequenceData] = {}
        for seq_group_metadata in seq_group_metadata_list:
            seq_data.update(seq_group_metadata.seq_data)

        pooling_metadata = PoolingMetadata(
            seq_groups=seq_groups,
            seq_data=seq_data,
            prompt_lens=prompt_lens,
        )

        return pooling_metadata

    @torch.inference_mode()
    def execute_model(
        self,
        model_input: ModelInputForNPUWithPoolingMetadata,
        kv_caches: List[torch.Tensor],
        intermediate_tensors: Optional[IntermediateTensors] = None,
        num_steps: int = 1,
    ):
        if num_steps > 1:
            raise ValueError(
                "PoolingModelRunner does not support multi-step execution.")

        if self.lora_config:
            assert model_input.lora_requests is not None
            assert model_input.lora_mapping is not None
            self.set_active_loras(model_input.lora_requests,
                                  model_input.lora_mapping)

        if self.prompt_adapter_config:
            assert model_input.prompt_adapter_requests is not None
            assert model_input.prompt_adapter_mapping is not None
            self.set_active_prompt_adapters(
                model_input.prompt_adapter_requests,
                model_input.prompt_adapter_mapping)

        assert model_input.attn_metadata is not None
        virtual_engine = model_input.virtual_engine
        model_executable = self.model

        multi_modal_kwargs = model_input.multi_modal_kwargs or {}
        seqlen_agnostic_kwargs = {
            "finished_requests_ids": model_input.finished_requests_ids,
            "request_ids_to_seq_ids": model_input.request_ids_to_seq_ids,
        } if self.has_inner_state else {}
        if (self.observability_config is not None
                and self.observability_config.collect_model_forward_time):
            model_forward_start = torch.npu.Event(enable_timing=True)
            model_forward_end = torch.npu.Event(enable_timing=True)
            model_forward_start.record()

        cross_enc_kwargs = {}
        if model_input.token_types is not None:
            cross_enc_kwargs["token_type_ids"] = model_input.token_types

        with set_ascend_forward_context(model_input.attn_metadata,
                                        self.vllm_config, virtual_engine):
            hidden_or_intermediate_states = model_executable(
                input_ids=model_input.input_tokens,
                positions=model_input.input_positions,
                intermediate_tensors=intermediate_tensors,
                **MultiModalKwargs.as_kwargs(multi_modal_kwargs,
                                             device=self.device),
                **cross_enc_kwargs,
                **seqlen_agnostic_kwargs)

        if (self.observability_config is not None
                and self.observability_config.collect_model_forward_time):
            model_forward_end.record()

        # Only perform pooling in the last pipeline stage.
        if not get_pp_group().is_last_rank:
            if (self.is_driver_worker
                    and hidden_or_intermediate_states is not None
                    and isinstance(hidden_or_intermediate_states,
                                   IntermediateTensors)
                    and self.observability_config is not None
                    and self.observability_config.collect_model_forward_time):
                model_forward_end.synchronize()
                model_forward_time = model_forward_start.elapsed_time(
                    model_forward_end)
                orig_model_forward_time = 0.0
                if intermediate_tensors is not None:
                    orig_model_forward_time = intermediate_tensors.tensors.get(
                        "model_forward_time", torch.tensor(0.0)).item()
                hidden_or_intermediate_states.tensors["model_forward_time"] = (
                    torch.tensor(model_forward_time + orig_model_forward_time))
            return hidden_or_intermediate_states

        # Only perform pooling in the driver worker.
        if not self.is_driver_worker:
            return []

        return [
            self.model.pooler(hidden_states=hidden_or_intermediate_states,
                              pooling_metadata=model_input.pooling_metadata)
        ]
