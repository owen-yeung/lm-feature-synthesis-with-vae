# Based on infer_ldlm.py from the LDLM repo

import argparse
from contextlib import contextmanager
from itertools import chain, islice
import json
import math
from pathlib import Path
import random
import sys
import zipfile
import typing
import matplotlib.pyplot as plt

import accelerate
from datasets import load_dataset
from einops import rearrange
# import k_diffusion as K
import peft
import safetensors.torch as safetorch
import torch
from torch import nn, optim
from torch.nn import functional as F
from torch.utils import data
from tqdm import trange, tqdm
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig, LlamaTokenizer, PreTrainedModel, Trainer, LlamaConfig, TrainingArguments

import bitsandbytes

class DecoderOnlyTransformerVAE(nn.Module):
    def __init__(self, model_name, device, z_dim=768, lora_rank=32, dropout=0.0, dtype=torch.bfloat16):
        super().__init__()
        self.dtype = dtype
        # if model_name == "openlm-research/open_llama_3b_v2":
        #     self.tokenizer = LlamaTokenizer.from_pretrained(model_name)
        # else:
        #     self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        #     print("WARNING: tokenizer only verified to work for open_llama_3b_v2")
        # self.tokenizer.padding_side = "left"
        self.device = device
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            device_map={"": self.device},
            quantization_config=bnb_config,
            torch_dtype=self.dtype,
        )
        peft_config = peft.LoraConfig(
            peft.TaskType.CAUSAL_LM,
            inference_mode=True, #TODO: used to be False, check for errors down the line
            r=lora_rank,
            lora_alpha=8,
            lora_dropout=dropout,
            target_modules=[
                "self_attn.q_proj",
                "self_attn.k_proj",
                "self_attn.v_proj",
                "self_attn.o_proj",
                "mlp.gate_proj",
                "mlp.up_proj",
                "mlp.down_proj",
            ],
        )

        self.z_dim = z_dim


        self.model = peft.get_peft_model(model, peft_config, "encoder")
        self.model.add_adapter("decoder", peft_config)
        self.model.set_adapter("decoder")
        self.model.config.output_hidden_states = True
        # self.model.to(self.device)
        self.vae = VAEComponent(self.model.config.hidden_size, self.z_dim).to(self.device)
        # self.model, self.vae = accelerator.prepare(self.model, self.vae)


    
    def save_pretrained(self, path):
        path = Path(path)
        self.model.save_pretrained(path, safe_serialization=True)
        safetorch.save_file(self.vae.state_dict(), path / "vae.safetensors")

    def load_pretrained(self, path, is_trainable=False):
        path = Path(path)
        self.model.delete_adapter("encoder")
        # if "encoder" in list(self.model.peft_config.keys()):
        #     self.model.delete_adapter("encoder")
        encoder_load_result = self.model.load_adapter(path / "encoder", "encoder", is_trainable=is_trainable)
        # print(encoder_load_result)
        self.model.delete_adapter("decoder")
        # if "decoder" in list(self.model.peft_config.keys()):
        #     self.model.delete_adapter("decoder")
        # self.model.set_adapter("encoder")
        self.model.disable_adapter()
        self.model.set_adapter("encoder")
        
        decoder_load_result = self.model.load_adapter(path / "decoder", "decoder", is_trainable=is_trainable)
        # print(decoder_load_result)
        # self.model.set_adapter("decoder")
        self.vae.load_state_dict(safetorch.load_file(path / "vae.safetensors"))

    def encode(self, input_ids, attention_mask):
        with set_adapter(self.model, "encoder"), disable_causal_mask():
            outputs = self.model(
                input_ids=input_ids, attention_mask=attention_mask, use_cache=False
            )
        return self.vae.encode(outputs.hidden_states[-1], attention_mask)
    

    def input_ids_to_embeds(self, input_ids):
        embed_weight = self.model.get_input_embeddings().weight
        input_one_hots = F.one_hot(input_ids, num_classes=self.model.config.vocab_size)
        return input_one_hots.to(embed_weight) @ embed_weight

    # @torch.no_grad()
    def generate(self, z, input_ids, attention_mask, n_tokens, tau=1.0, output_hidden_states=False, eos_id=None):
        """Generates n_tokens from a latent code.
            If output_hidden_states: generates a dict of output_ids and hidden_states.
            If given an EOS id, will stop generation when it is generated.
        """
        with torch.cuda.amp.autocast(dtype=torch.bfloat16):
            z_embed = self.vae.decode(z)[:, None]
            inputs_embeds = self.input_ids_to_embeds(input_ids)
            # print(inputs_embeds.shape)
            inputs_embeds = torch.cat([z_embed, inputs_embeds], dim=1)
            attention_mask = torch.cat(
                [attention_mask.new_ones([attention_mask.shape[0], 1]), attention_mask], dim=1
            )
            new_embeds, past = None, None
        

        with set_adapter(self.model, "decoder"):
            for _ in range(n_tokens):
                outputs = self.model(
                    inputs_embeds=inputs_embeds if past is None else new_embeds,
                    attention_mask=attention_mask,
                    use_cache=True,
                    past_key_values=past,
                    output_hidden_states=output_hidden_states,
                )
                logits = outputs.logits[:, -1:, :].float()
                new_input_ids = torch.argmax(logits + gumbel_like(logits) * tau, dim=-1)
            

                input_ids = torch.cat([input_ids, new_input_ids], dim=1)
                if eos_id != None and (new_input_ids == eos_id).any(): #TODO check if this works
                    break
                new_embeds = self.input_ids_to_embeds(new_input_ids)
                attention_mask = torch.cat(
                    [attention_mask, attention_mask.new_ones([attention_mask.shape[0], 1])], dim=1
                )
                past = outputs.past_key_values
        if output_hidden_states:
            hidden_states = outputs.hidden_states
            return {'output_ids': input_ids, 'hidden_states': hidden_states}
        else:
            return input_ids

    def forward(self, input_ids, attention_mask, decoder_prefix_ids, decoder_prefix_mask, output_hidden_states=False):
        input_ids_all = torch.cat([decoder_prefix_ids, input_ids], dim=1)
        attn_mask_all = torch.cat([decoder_prefix_mask, attention_mask], dim=1)
        mean = self.encode(input_ids, attention_mask)
        z = self.vae.sample(mean)
        z_embed = self.vae.decode(z)[:, None]
        inputs_embeds = self.input_ids_to_embeds(input_ids_all)
        inputs_embeds = torch.cat([z_embed, inputs_embeds], dim=1)
        attention_mask = torch.cat(
            [attention_mask.new_ones([attn_mask_all.shape[0], 1]), attn_mask_all], dim=1
        )
        with set_adapter(self.model, "decoder"):
            outputs = self.model(
                inputs_embeds=inputs_embeds, attention_mask=attention_mask, use_cache=False, output_hidden_states=output_hidden_states
            )
        return outputs, mean

class VAEComponent(nn.Module):
    def __init__(self, d_model, z_dim):
        super().__init__()
        self.d_model = d_model
        self.z_dim = z_dim
        self.f = nn.Linear(d_model, 1)
        self.w_e = nn.Linear(d_model, z_dim)
        self.w_d = nn.Linear(z_dim, d_model)
        nn.init.orthogonal_(self.w_e.weight)
        with torch.no_grad():
            self.w_d.weight.copy_(self.w_e.weight.T)

    def encode(self, hidden_states, attention_mask):
        scores = self.f(hidden_states)
        scores = scores + attention_mask[:, :, None].log().nan_to_num()
        weights = torch.softmax(scores, dim=1)
        pooled = torch.sum(hidden_states * weights, dim=1)
        return self.w_e(pooled)

    def sample(self, mean, tau=1.0):
        return mean + torch.randn_like(mean) * tau**0.5

    def decode(self, z):
        return self.w_d(z)

@contextmanager
def set_adapter(model, adapter_name):
    old_adapter_name = model.active_adapter
    try:
        if adapter_name is not None:
            model.set_adapter(adapter_name)
            yield model
        else:
            with model.disable_adapter():
                yield model
    finally:
        model.set_adapter(old_adapter_name)

def gumbel_like(x):
    return torch.rand_like(x).log_().nan_to_num_().neg_().log_().neg_()

@contextmanager
def disable_causal_mask():
    import transformers.models.llama.modeling_llama as modeling

    decoder_fn = modeling._make_causal_mask

    def encoder_fn(*args, **kwargs):
        return torch.zeros_like(decoder_fn(*args, **kwargs))

    try:
        modeling._make_causal_mask = encoder_fn
        yield
    finally:
        modeling._make_causal_mask = decoder_fn
