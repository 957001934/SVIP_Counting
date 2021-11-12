# import os
# import time
# import argparse
# import datetime
import numpy as np
# import pdb
import torch
import torch.nn as nn
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
# import torch.backends.cudnn as cudnn
# import torch.distributed as dist
import math
# from torchsummary import summary
from models.swin_transformer import SwinTransformer
from torch.cuda.amp import autocast
# from config import get_config
# from models import build_model
# from data import build_loader
# from logger import create_logger
# from tool import load_checkpoint, save_checkpoint, get_grad_norm, auto_resume_helper, reduce_tensor

device = torch.device("cuda")


class attention(nn.Module):
    """Scaled dot-product attention mechanism."""

    def __init__(self, scale=64, att_dropout=None):
        super().__init__()
        # self.dropout = nn.Dropout(attention_dropout)
        self.softmax = nn.Softmax(dim=-1)
        self.dropout = nn.Dropout(att_dropout)
        self.scale = scale

    def forward(self, q, k, v, attn_mask=None):
        # q: [B, head, F, model_dim]
        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.scale)  # [B,Head, F, F]
        if attn_mask:
            # 给需要mask的地方设置一个负无穷
            scores = scores.masked_fill_(attn_mask, -np.inf)
        # 计算softmax
        scores = self.softmax(scores)
        # 添加dropout
        scores = self.dropout(scores)  # [B,head, F, F]
        # 和V做点积
        # context = torch.matmul(scores, v)  # output
        return scores  # [B,head,F, F]


class Similarity_matrix(nn.Module):

    def __init__(self, num_heads=8, model_dim=512, input_size=512):
        super().__init__()

        # self.dim_per_head = model_dim // num_heads
        self.num_heads = num_heads
        self.model_dim = model_dim
        self.input_size = input_size
        self.linear_q = nn.Linear(self.input_size, model_dim)
        self.linear_k = nn.Linear(self.input_size, model_dim)
        self.linear_v = nn.Linear(self.input_size, model_dim)

        self.attention = attention(att_dropout=0)

    def forward(self, query, key, value, attn_mask=None):
        # 残差连接

        batch_size = query.size(0)

        num_heads = self.num_heads

        # linear projection

        query = self.linear_q(query)  # [B,F,model_dim]
        key = self.linear_k(key)
        value = self.linear_v(value)

        # split by heads
        # [B,F,model_dim] ->  [B,F,num_heads,per_head]->[B,num_heads,F,per_head]
        query = query.view(batch_size, -1, num_heads, self.model_dim // self.num_heads).transpose(1, 2)
        key = key.view(batch_size, -1, num_heads, self.model_dim // self.num_heads).transpose(1, 2)
        value = value.view(batch_size, -1, num_heads, self.model_dim // self.num_heads).transpose(1, 2)
        # similar_matrix :[B,H,F,F ]
        matrix = self.attention(query, key, value, attn_mask)

        return matrix


class PositionalEncoding(nn.Module):
    def __init__(self, length):
        super().__init__()
        self.pos_encoding = torch.empty(1, length, 1).normal_(mean=0, std=0.02)
        self.pos_encoding.requires_grad = True

    def forward(self, x):
        x = x + self.pos_encoding.to(x.device)
        return x


class swcp(nn.Module):
    """
    input = torch.rand([B, F, C, 224, 224])
    output = [B,F]

    """

    def __init__(self,
                 frame=10,
                 head=4,
                 transformer_layers_config: tuple = ((512, 4, 512),),
                 transformer_dropout_rate: float = 0.0,
                 transformer_reorder_ln: bool = True,
                 dropout_rate=0.5,
                 density_fc_channels=(512, 512),
                 period_fc_channels: tuple = (512, 512),
                 within_period_fc_channels: tuple = (512, 512)
                 # pos_encoding=None
                 ):
        super().__init__()
        self.image_size = 224
        self.num_frames = frame
        self.dropout_rate = dropout_rate
        self.num_heads = head
        # self.density_fc_channels = density_fc_channels
        self.period_fc_channels = period_fc_channels
        self.within_period_fc_channels = within_period_fc_channels
        self.sw_1 = SwinTransformer()  # output (1024,)

        # # temporal conv layers
        # self.temporal_conv_layers = nn.Conv3d(in_channels=1024,
        #                                       out_channels=512,
        #                                       kernel_size=3,
        #                                       padding=(3, 1, 1),
        #                                       dilation=(3, 1, 1))
        #
        # self.temporal_bn_layers = [nn.BatchNorm3d(num_features=512) for _ in self.temporal_conv_layers]
        #
        # self.conv_3x3_layer = nn.Conv2d(in_channels=1,
        #                                 out_channels=self.conv_channels,
        #                                 kernel_size=self.conv_kernel_size,
        #                                 padding=1)
        #
        self.sm = Similarity_matrix(num_heads=self.num_heads, input_size=128)

        # Transformer config in form of (channels, heads, bottleneck channels).
        self.transformer_layers_config = transformer_layers_config
        self.transformer_dropout_rate = transformer_dropout_rate

        self.input_projection = nn.Linear(in_features=self.num_frames * self.num_heads, out_features=512, bias=True)
        self.input_projection2 = nn.Linear(in_features=self.num_frames * self.num_heads, out_features=512, bias=True)

        self.pos_encoder = PositionalEncoding(self.num_frames)
        self.pos_encoder2 = PositionalEncoding(self.num_frames)

        self.transformer_layers = nn.ModuleList()
        for d_model, num_heads, dff in self.transformer_layers_config:
            self.transformer_layers.append(
                nn.TransformerEncoderLayer(d_model=d_model,
                                           nhead=num_heads,
                                           dim_feedforward=dff,
                                           dropout=self.transformer_dropout_rate))

        self.transformer_layers2 = nn.ModuleList()
        for d_model, num_heads, dff in self.transformer_layers_config:
            self.transformer_layers2.append(
                nn.TransformerEncoderLayer(d_model=d_model,
                                           nhead=num_heads,
                                           dim_feedforward=dff,
                                           dropout=self.transformer_dropout_rate))

        self.dropout_layer = nn.Dropout(self.dropout_rate)

        # period length prediction
        num_preds = self.num_frames
        self.fc_layers = nn.ModuleList()
        for channels in self.period_fc_channels:
            self.fc_layers.append(
                nn.Linear(in_features=channels,
                          out_features=channels)
            )
            self.fc_layers.append(nn.ReLU())
        self.fc_layers.append(
            nn.Linear(in_features=self.period_fc_channels[0],
                      out_features=num_preds)
        )

        # Within Period Module
        num_preds = 1
        self.within_period_fc_layers = nn.ModuleList()
        for channels in self.within_period_fc_channels:
            self.within_period_fc_layers.append(
                nn.Linear(in_features=channels,
                          out_features=channels)
            )
            self.within_period_fc_layers.append(nn.ReLU())
        self.within_period_fc_layers.append(
            nn.Linear(in_features=self.within_period_fc_channels[0],
                      out_features=num_preds)
        )
        self.apply(self._init_weights)

    def forward(self, x):

        # Ensures we are always using the right batch_size during train/eval.
        b, f, c, h, w = x.shape  # x:[B,F,C,H,W]  [B,F,3,224,224]
        assert self.image_size == x.shape[3]

        # Swin_T Feature Extractor per frame
        x = x.view([-1, c, self.image_size, self.image_size])
        with torch.no_grad():
            x = self.sw_1(x)  # output: [b*f,1024]
        with autocast():
            x = x.view([b, -1, 128])  # output: [B, frames, features]

            # Get self-similarity matrix.
            x = self.sm(x, x, x)  # output:[B, head, H_frames, W_frames]
            x = x.transpose(1, 2)  # to output:[B, H_frames, head, W_frames]
            x = torch.reshape(x, [b, f, -1])  # output: [B, f，head*f] [2,10,4*10]
            within_period_x = x

            # Period prediction.
            x = self.input_projection(x)  # output: [B, f，512]
            x = self.pos_encoder(x)  # output:x += [1,f,1]
            for transformer_layer in self.transformer_layers:
                x = transformer_layer(x)  # output: [b,f,512]
            for fc in self.fc_layers:
                x = self.dropout_layer(x)
                x = fc(x)
            # output: [b,f,num_preds]

            # Within period prediction.
            within_period_x = self.input_projection2(within_period_x)  # output: [B, f，512]
            within_period_x = self.pos_encoder2(within_period_x)  # output:x += [1,f,1]
            for transformer_layer in self.transformer_layers2:
                within_period_x = transformer_layer(within_period_x)  # output: [b,f,512]
            for fc in self.within_period_fc_layers:
                within_period_x = self.dropout_layer(within_period_x)
                within_period_x = fc(within_period_x)
            within_period_x = within_period_x.reshape([b, -1])
            # output: [b,f]
            return x, within_period_x

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

# nn.Linear()
# config = get_config()
# x = build_model(config)
# input = torch.rand([2, 10, 3, 224, 224])
# # input1 = input[0].unsqueeze(0)
# # input2 = input[1].unsqueeze(0)
# # print(input1[0] == input[0])
# # print(input2[0] == input[1])
# net = swrepnet(frame=10)
# # print(net)
# # sw_path = r'swt_log/swin_base_patch4_window12_384_22k.pth'
# # net.load_state_dict(torch.load(sw_path), strict=False)
# output = net(input)
# # output1 = net(input1)
# # output2 = net(input2)
# print(output.shape)
# # print(output)
# print(output[0] == output1[0])
# print(output[1] == output2[0])
# print(net)
# summary(net, (3, 224, 224))
