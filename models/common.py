import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from collections import OrderedDict
from .downsampler import Downsampler


def add_module(self, module):
    self.add_module(str(len(self) + 1), module)


torch.nn.Module.add = add_module


class Concat(nn.Module):
    def __init__(self, dim, *args):
        super(Concat, self).__init__()
        self.dim = dim

        for idx, module in enumerate(args):
            self.add_module(str(idx), module)

    def forward(self, input):
        inputs = []
        for module in self._modules.values():
            inputs.append(module(input))

        inputs_shapes2 = [x.shape[2] for x in inputs]
        inputs_shapes3 = [x.shape[3] for x in inputs]

        if np.all(np.array(inputs_shapes2) == min(inputs_shapes2)) and np.all(np.array(inputs_shapes3) == min(inputs_shapes3)):
            inputs_ = inputs
        else:
            target_shape2 = min(inputs_shapes2)
            target_shape3 = min(inputs_shapes3)

            inputs_ = []
            for inp in inputs:
                diff2 = (inp.size(2) - target_shape2) // 2
                diff3 = (inp.size(3) - target_shape3) // 2
                inputs_.append(inp[:, :, diff2: diff2 + target_shape2, diff3:diff3 + target_shape3])

        return torch.cat(inputs_, dim=self.dim)

    def __len__(self):
        return len(self._modules)

class AddNoisyFMs(nn.Module):
    def __init__(self, dim, sigma=1):
        super(AddNoisyFMs, self).__init__()
        self.dim = dim
        self.sigma = sigma

    def forward(self, input):
        a = list(input.size())
        a[1] = self.dim

        b = torch.zeros(a, dtype=input.dtype)#.type_as(input.data)
        b.normal_(std=self.sigma)

        return torch.cat((input, b), axis=1)


class GenNoise(nn.Module):
    def __init__(self, dim2):
        super(GenNoise, self).__init__()
        self.dim2 = dim2

    def forward(self, input):
        a = list(input.size())
        a[1] = self.dim2

        b = torch.zeros(a).type_as(input.data)
        b.normal_()

        return b


class ProbabilityDropout2d(nn.Module):
    def __init__(self, probs):
        super(ProbabilityDropout2d, self).__init__()
        self.probs = probs

    def forward(self, x):

        if np.array_equal(self.probs, np.ones(len(self.probs)) * self.probs[0]):
            return F.dropout2d(x, p=self.probs[0])
        else:
            bino = np.random.rand(self.probs.shape[0])
            dropout_probs = bino - self.probs

            dropout_probs[dropout_probs >= 0] = 1
            dropout_probs[dropout_probs < 0] = 0
            zeros = torch.zeros(x.shape[2:], dtype=x.dtype)

            # if x.device.type == 'cuda':
            #     zeros.to(device)

            # if torch.cuda.is_available():
            #     zeros = zeros.cuda()
            #
            # x[0, dropout_probs == 0] = zeros#.cuda()#.to(x.device)
            x[0, dropout_probs == 0] = torch.zeros(x.shape[2:], device=x.device, dtype=x.dtype)

            return x / np.sum(dropout_probs) * dropout_probs.shape[0]

            # outputs = []
            # for i, prob in enumerate(self.probs):
            #     outputs.append(F.dropout2d(x, p=prob)[:,i])
            # return torch.cat(outputs)[None]
            # bino = np.random.rand(self.probs.shape[0])
            # dropout_probs = bino - self.probs
            #
            # dropout_probs[dropout_probs >= 0] = 1
            # dropout_probs[dropout_probs < 0] = 0
            #
            # dropout_mask = np.array([np.ones(x.shape[2:]) if a == 1 else np.zeros(x.shape[2:]) for a in dropout_probs])
            #
            # dropout_mask = np_to_torch(dropout_mask)
            #
            # return dropout_mask * x / np.sum(dropout_probs) * dropout_probs.shape[0]


class ProbabilityDropout(nn.Module):
    def __init__(self, probs):
        super(ProbabilityDropout, self).__init__()
        self.probs = probs

    def forward(self, x):
        # should i drop out a value of the conv kernel?
        x = torch.tensor([[F.dropout(x[0,i], self.probs[i]).tolist() for i in range(self.probs.shape[0])]])
        return x


class Swish(nn.Module):
    """
        https://arxiv.org/abs/1710.05941
        The hype was so huge that I could not help but try it
    """
    def __init__(self):
        super(Swish, self).__init__()
        self.s = nn.Sigmoid()

    def forward(self, x):
        return x * self.s(x)


def act(act_fun = 'LeakyReLU'):
    '''
        Either string defining an activation function or module (e.g. nn.ReLU)
    '''
    if isinstance(act_fun, str):
        if act_fun == 'LeakyReLU':
            return nn.LeakyReLU(0.2, inplace=True)
        elif act_fun == 'Swish':
            return Swish()
        elif act_fun == 'ELU':
            return nn.ELU()
        elif act_fun == 'none':
            return nn.Sequential()
        else:
            assert False
    else:
        return act_fun()


def bn(num_features):
    return nn.BatchNorm2d(num_features)


def conv(in_f, out_f, kernel_size, stride=1, bayes=False, bias=True, pad='zero', downsample_mode='stride',
         dropout_mode=None, dropout_p=0.2, iterator=1, string='deeper'):
    downsampler = None
    if stride != 1 and downsample_mode != 'stride':

        if downsample_mode == 'avg':
            downsampler = nn.AvgPool2d(stride, stride)
        elif downsample_mode == 'max':
            downsampler = nn.MaxPool2d(stride, stride)
        elif downsample_mode  in ['lanczos2', 'lanczos3']:
            downsampler = Downsampler(n_planes=out_f, factor=stride, kernel_type=downsample_mode, phase=0.5,
                                      preserve_size=True)
        else:
            assert False

        stride = 1

    padder = None
    to_pad = int((kernel_size - 1) / 2)
    if pad == 'reflection':
        padder = nn.ReflectionPad2d(to_pad)
        to_pad = 0

    convolver = nn.Conv2d(in_f, out_f, kernel_size, stride, padding=to_pad, bias=bias)

    dropout = None
    if dropout_mode == '2d' and not bayes:
        dropout = nn.Dropout2d(p=dropout_p)
    elif dropout_mode == '1d' and not bayes:
        dropout = nn.Dropout(p=dropout_p)

    layers = filter(lambda x: x is not None, [padder, convolver, dropout, downsampler])

    ordered_layers = OrderedDict([('{}_{}_{}'.format(layer._get_name(), string, iterator), layer) for layer in layers])

    return nn.Sequential(ordered_layers)