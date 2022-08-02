from torch.nn import Module
import torch
import numpy as np
import torch.nn as nn
import cv2
import math
import time
from einops.einops import rearrange
import torch.nn.functional as F
import einops
from einops.layers.torch import Rearrange
import torchvision

"""
Notes/pseudocode about writing attentions with einops in notes.py. 
I only implement decoder-only and encoder-only architectures, 
since they are used in GPT and ViT. Code inspired by karpathy/mingpt
Maybe later ill make a full transformer for translation and stuff.
"""

class NewGELU(nn.Module):
    """
    Implementation of the GELU activation function currently in Google BERT repo (identical to OpenAI GPT).
    Reference: Gaussian Error Linear Units (GELU) paper: https://arxiv.org/abs/1606.08415
    """
    def forward(self, x):
        return 0.5 * x * (1.0 + torch.tanh(math.sqrt(2.0 / math.pi) * (x + 0.044715 * torch.pow(x, 3.0))))

class Config():
    # model parameters
    m = None # width of input sequence
    k = 32 # key dimension size
    v = 32 # value dimension size
    d = 192 # dimension of hidden state between blocks
    h = 6 # number of heads
    b = 1 # batch dimension
    d_ff = 128 # size of fully-connected layer
    n_encoders = 6 # number of encoder layers
    n_decoders = 6 # number of decoder layers
    max_seq_length = 1024 # max m

    # hyperparameters for training
    lr = 1e-6 # use lr scheduler later
    dropout = 0.1
    adam_params = {
        'beta_1': 0.9,
        'beta_2': 0.98,
        'epsilon': 10e-9
    }

    # other parameters
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    vocab_size = 30000 # shared vocab, if doing NLP
    is_decoder = False

    def __init__(self, **kwargs): 
        self.__dict__.update(kwargs)

class MHA(torch.nn.Module):
    
    """ 
    Multi Head Attention for the Encoder. 
    Params:
    m: length of the input to the Encoder.
    k: key dimension
    v: value dimension
    d: embedding dimension (d_model)
    h: number of heads
    b: batch dimension
    Args:
    x: a vector with shape [b d]. This becomes Q, K, V
    P_q: a tensor with shape [h, d, k]. model weights.
    P_k: a tensor with shape [h, d, k]. model weights.
    P_v: a tensor with shape [h, d, v]. model weights.
    P_o: a tensor with shape [h, d, v]. model weights.
    Returns : 
    y: a vector with shape [b, d]
    """

    def __init__(self, config, mask=False):
        super().__init__()
       
        self.P_i = torch.nn.Parameter(torch.nn.init.kaiming_uniform_(torch.zeros(config.h, config.d, config.v * 3)), requires_grad=True)
        self.B_i = torch.nn.Parameter(torch.nn.init.kaiming_uniform_(torch.zeros(1, 1, 1, config.v * 3)), requires_grad=True)
        self.P_o = torch.nn.Parameter(torch.nn.init.kaiming_uniform_(torch.zeros(config.h, config.d, config.v)), requires_grad=True)
        self.B_o = torch.nn.Parameter(torch.nn.init.kaiming_uniform_(torch.zeros(1, 1, config.d)), requires_grad=True)

        self.config = config



        # output projection
        self.c_proj = nn.Linear(config.d, config.d)
        # regularization
        self.attn_dropout = nn.Dropout(0.1)
        self.resid_dropout = nn.Dropout(0.1)
        # causal mask to ensure that attention is only applied to the left in the input sequence
        self.register_buffer("bias", torch.tril(torch.ones(128, 128))
                                     .view(1, 1, 128, 128))
        self.n_head = config.h
        self.n_embd = config.d

    def forward(self, x):
        B, T, C = x.size()
        # linear layers before attention
        transformed = torch.einsum('bmd, hde -> bhme', x, self.P_i) 
        q, k, v = transformed.split(self.config.v, -1)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.bias[:,:,:T,:T] == 0, float('-inf'))
        att = F.softmax(att, dim=-1)
        att = self.attn_dropout(att)
        y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_dropout(self.c_proj(y))
        return y
    
    def mask(self, x):
        seq_length = x.shape[-1]
        return x.masked_fill(self.triu_mask[:, :, :seq_length, :seq_length] == 0, float('-inf'))

    """
    def __init__(self, config, mask):
        super().__init__()
        assert config.d % config.h == 0
        # key, query, value projections for all heads, but in a batch
        self.c_attn = nn.Linear(config.d, 3 * config.d)
        # output projection
        self.c_proj = nn.Linear(config.d, config.d)
        # regularization
        self.attn_dropout = nn.Dropout(0.1)
        self.resid_dropout = nn.Dropout(0.1)
        # causal mask to ensure that attention is only applied to the left in the input sequence
        self.register_buffer("bias", torch.tril(torch.ones(128, 128))
                                     .view(1, 1, 128, 128))
        self.n_head = config.h
        self.n_embd = config.d

    def forward(self, x):
        B, T, C = x.size() # batch size, sequence length, embedding dimensionality (n_embd)

        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        q, k ,v  = self.c_attn(x).split(self.n_embd, dim=2)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.bias[:,:,:T,:T] == 0, float('-inf'))
        att = F.softmax(att, dim=-1)
        att = self.attn_dropout(att)
        y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_dropout(self.c_proj(y))
        return y

        def mask(self, x):
            seq_length = x.shape[-1]
            return x.masked_fill(self.triu_mask[:, :, :seq_length, :seq_length] == 0, float('-inf'))
    """
            
class Block(Module):
    """
    A transformer block. Can act as both an encoder (ViT) or standalone decoder (GPT).
    As far as i can tell, the only major difference between an encoder and the "decoder" used in GPT
    is the mask. To make this a decoder, set mask=True
    """

    def __init__(self, config, mask=False):
        super().__init__()
        self.mhastack = torch.nn.Sequential(
            torch.nn.LayerNorm(config.d),
            MHA(config, mask=mask)
        )
        self.linstack = torch.nn.Sequential(
            torch.nn.LayerNorm(config.d),
            torch.nn.Linear(config.d, config.d * 4),
            NewGELU(),
            torch.nn.Linear(config.d * 4, config.d),
            torch.nn.Dropout(0.1)
        )

    def forward(self, x):
        x = x + self.mhastack(x)
        x = x + self.linstack(x)
        return x

class Transformer(Module):

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
        elif isinstance(module, nn.LayerNorm):
            torch.nn.init.zeros_(module.bias)
            torch.nn.init.ones_(module.weight)

    def __init__(self, config):
        super().__init__()
        self.input_embedding = torch.nn.Embedding(config.vocab_size, config.d)
        self.pos_embedding = torch.nn.Embedding(config.max_seq_length, config.d)
        self.config = config
        self.encoder = nn.Sequential(
            *[Block(config, mask=True) for i in range(config.n_decoders)])
        self.device = config.device
        self.head = nn.Sequential(
            torch.nn.Linear(config.d, config.vocab_size)
        )  
        self.dropout = torch.nn.Dropout(config.dropout)
        self.ln = torch.nn.LayerNorm(config.d)
        self.max_seq_length = config.max_seq_length
         
    def forward(self, _input):
        pos = torch.arange(0, _input.shape[-1], dtype=torch.long, device=self.config.device).unsqueeze(0) # shape (1, t)
        #pos_emb = self.pos_encoding(self.pos)
        input_embeddings = self.input_embedding(_input)
        pos_embeddings = self.pos_embedding(pos)
        _input = self.dropout(input_embeddings + pos_embeddings)
        encoder_output = self.encoder(_input)
        encoder_output = self.ln(encoder_output)
        # The head can be modified for different purposes.
        # for example, VIT can use a classifier head instead. 
        _output = self.head(encoder_output)
        return _output
    
    # generate function stolen from https://github.com/karpathy/mingpt
    @torch.no_grad()
    def generate(self, idx, max_new_tokens, temperature=1.0, do_sample=False, top_k=None):
        """
        Take a conditioning sequence of indices idx (LongTensor of shape (b,t)) and complete
        the sequence max_new_tokens times, feeding the predictions back into the model each time.
        Most likely you'll want to make sure to be in model.eval() mode of operation for this.
        """
        for _ in range(max_new_tokens):
            # if the sequence context is growing too long we must crop it at block_size
            idx_cond = idx if idx.size(1) <= self.max_seq_length else idx[:, -self.max_seq_length:]
            # forward the model to get the logits for the index in the sequence
            logits = self(idx_cond)
            # pluck the logits at the final step and scale by desired temperature
            logits = logits[:, -1, :] / temperature
            # optionally crop the logits to only the top k options
            if top_k is not None:
                v, _ = torch.topk(logits, top_k)
                logits[logits < v[:, [-1]]] = -float('Inf')
            # apply softmax to convert logits to (normalized) probabilities
            probs = F.softmax(logits, dim=-1)
            # either sample from the distribution or take the most likely element
            if do_sample:
                idx_next = torch.multinomial(probs, num_samples=1)
            else:
                _, idx_next = torch.topk(probs, k=1, dim=-1)
            # append sampled index to the running sequence and continue
            idx = torch.cat((idx, idx_next), dim=1)
        return idx

    def get_optimizer(self):
        self.wd_dict = {
            'P_q': False,
            'P_k': False,
            'P_v': False,
            'P_o': False,
            'P_i': True,
            'weight': True,
            'bias': True,
            'B_i': False,
            'B_o': False
        }

        wd_params = []
        no_wd_params=[]

        for a, b in self.named_parameters():
            last = a.split('.')[-1]
            wd = self.wd_dict[last]
            if wd:
                wd_params.append(b)
            else:
                no_wd_params.append(b)
        
        wd_params = {'params': wd_params, 'weight_decay': 0.1}
        no_wd_params= {'params':no_wd_params, 'weight_decay': 0.0}

        return torch.optim.AdamW([no_wd_params, wd_params], lr=self.config.lr, betas=(0.9, 0.98))

class ViT(Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.device = config.device
        self.out_size = config.vocab_size
        patch_size = (4, 4)
        flat_patch_size = 16
        self.learnable_patch = torch.nn.Parameter(torch.randn(4 * 4,)).view(1, 1, 16)
        self.pos_embedding = torch.nn.Embedding(config.max_seq_length, config.d)
        self.patch_embedding = torch.nn.Linear(flat_patch_size, config.d)
        
        self.encoder = nn.Sequential(
            *[Block(config, mask=False) for i in range(config.n_decoders)])

        self.head = nn.Sequential(
            torch.nn.Linear(config.d, 10)
        )  
        
        self.dropout = torch.nn.Dropout(config.dropout)
        self.ln = torch.nn.LayerNorm(config.d)
        self.dumb = torch.nn.Linear(28 * 28, 128)
        self.dumb2 = torch.nn.Linear(128, 10)

    def forward(self, _input):
        batch_size, channels, width, height = _input.shape
        pos = torch.arange(0, 49, dtype=torch.long, device=self.config.device).unsqueeze(0) # shape (1, t)
        # encode the input as a bunch of flattened patches (last dim)
        # then append the learnable patch at zero-index. now we have a nice even seq length
        #_input = torch.cat([self.learnable_patch.expand(_input.shape[0], -1, 16), self.to_patches(_input)], dim=1)  
        _input = self.to_patches(_input)   
        input_embeddings = self.patch_embedding(_input) # -> (b, l_seq, d_model)
        pos_embeddings = self.pos_embedding(pos)
        _input = self.dropout(input_embeddings + pos_embeddings)
        encoder_output = self.encoder(_input)
        encoder_output = self.ln(encoder_output)
        encoder_output = torch.mean(encoder_output, 1)
        _output = self.head(encoder_output)
        return _output
    

    def to_patches(self, img):
        # b, 28, 28,
        batch_size, _, _, _ = img.shape
        img = torch.nn.Unfold((4, 4), stride=4)(img).view(batch_size, 4, 4, 49)
        img = img.transpose(3, 1)
        img = img.transpose(2, 3)
        img = img.view(batch_size, 49, -1)
        return img