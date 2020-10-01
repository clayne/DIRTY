import math
from typing import Dict, List, Union

import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
from torch.nn import TransformerEncoder, TransformerEncoderLayer
from utils import util
from utils.vocab import PAD_ID, Vocab


class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer("pe", pe)

    def forward(self, x):
        x = x + self.pe[: x.size(0), :]
        return self.dropout(x)


class TransformerModel(nn.Module):
    def __init__(self, ninp, nhead, nhid, nlayers, dropout=0.5):
        super(TransformerModel, self).__init__()
        self.model_type = "Transformer"
        self.pos_encoder = PositionalEncoding(ninp, dropout)
        encoder_layers = TransformerEncoderLayer(
            nhid, nhead, nhid, dropout, activation="gelu"
        )
        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)
        self.ninp = ninp
        self.nhid = nhid

    def forward(self, src, src_padding):
        src = src * math.sqrt(self.ninp)
        # src = self.pos_encoder(src)
        output = self.transformer_encoder(src, src_key_padding_mask=src_padding)
        # set state and cell to the average of output
        masked_sum = (
            output.transpose(0, 1)
            * ((~src_padding).unsqueeze(-1).expand(-1, -1, self.nhid))
        ).sum(dim=1)
        lengths = (~src_padding).sum(dim=1, keepdim=True)
        avg = masked_sum / lengths
        state = torch.stack(
            [avg[:, : self.nhid // 2], avg[:, self.nhid // 2 :]]
        ).unsqueeze(0)
        return output, (state, state)


class XfmrSequentialEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.vocab = vocab = Vocab.load(config["vocab_file"])

        self.src_word_embed = nn.Embedding(
            len(vocab.source_tokens), config["source_embedding_size"]
        )

        dropout = config["dropout"]
        self.encoder = TransformerModel(
            self.src_word_embed.embedding_dim,
            1,
            config["source_encoding_size"],
            config["num_layers"],
            dropout=dropout,
        )

        self.decoder_cell_init = nn.Linear(
            config["source_encoding_size"], config["decoder_hidden_size"]
        )

        self.dropout = nn.Dropout(dropout)
        self.config = config

    @property
    def device(self):
        return self.src_word_embed.weight.device

    @classmethod
    def default_params(cls):
        return {
            "source_encoding_size": 256,
            "decoder_hidden_size": 128,
            "source_embedding_size": 128,
            "vocab_file": None,
            "num_layers": 1,
        }

    @classmethod
    def build(cls, config):
        params = util.update(XfmrSequentialEncoder.default_params(), config)

        return cls(params)

    def forward(self, tensor_dict: Dict[str, Union[torch.Tensor, int]]):
        (
            code_token_encoding,
            code_token_mask,
            (last_states, last_cells),
        ) = self.encode_sequence(tensor_dict["src_code_tokens"])

        # (batch_size, max_variable_mention_num)
        variable_mention_mask = tensor_dict["variable_mention_mask"]
        variable_mention_to_variable_id = tensor_dict["variable_mention_to_variable_id"]

        # (batch_size, max_variable_num)
        variable_encoding_mask = tensor_dict["variable_encoding_mask"]
        variable_mention_num = tensor_dict["variable_mention_num"]

        # # (batch_size, max_variable_mention_num, encoding_size)
        max_time_step = variable_mention_to_variable_id.size(1)
        variable_num = variable_mention_num.size(1)
        encoding_size = code_token_encoding.size(-1)

        variable_mention_encoding = (
            code_token_encoding * variable_mention_mask.unsqueeze(-1)
        )
        variable_encoding = torch.zeros(
            tensor_dict["batch_size"], variable_num, encoding_size, device=self.device
        )
        variable_encoding.scatter_add_(
            1,
            variable_mention_to_variable_id.unsqueeze(-1).expand(-1, -1, encoding_size),
            variable_mention_encoding,
        ) * variable_encoding_mask.unsqueeze(-1)
        variable_encoding = variable_encoding / (
            variable_mention_num + (1.0 - variable_encoding_mask) * 1e-8
        ).unsqueeze(-1)

        context_encoding = dict(
            variable_encoding=variable_encoding,
            code_token_encoding=code_token_encoding,
            code_token_mask=code_token_mask,
            last_states=last_states,
            last_cells=last_cells,
        )

        # context_encoding.update(tensor_dict)

        return context_encoding

    def encode_sequence(self, code_sequence):

        # (batch_size, max_code_length, embed_size)
        code_token_embedding = self.src_word_embed(code_sequence)

        # (batch_size, max_code_length)
        code_token_mask = torch.ne(code_sequence, PAD_ID).float()

        sorted_encodings, (last_states, last_cells) = self.encoder(
            code_token_embedding.transpose(0, 1), (1 - code_token_mask).bool()
        )
        sorted_encodings = sorted_encodings.transpose(0, 1)

        # apply dropout to the last layer
        # (batch_size, seq_len, hidden_size * 2)
        sorted_encodings = self.dropout(sorted_encodings)

        return sorted_encodings, code_token_mask, (last_states, last_cells)

    def get_decoder_init_state(self, context_encoder, config=None):
        fwd_last_layer_cell = context_encoder["last_cells"][-1, 0]
        bak_last_layer_cell = context_encoder["last_cells"][-1, 1]

        dec_init_cell = self.decoder_cell_init(
            torch.cat([fwd_last_layer_cell, bak_last_layer_cell], dim=-1)
        )
        dec_init_state = torch.tanh(dec_init_cell)

        return dec_init_state, dec_init_cell

    def get_attention_memory(self, context_encoding, att_target="terminal_nodes"):
        assert att_target == "terminal_nodes"

        memory = context_encoding["code_token_encoding"]
        mask = context_encoding["code_token_mask"]

        return memory, mask
