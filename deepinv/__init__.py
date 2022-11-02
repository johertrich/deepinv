from .__about__ import *
import torch

# import utils


from utils.nn import adjust_learning_rate, save_model
from utils.logger import AverageMeter, ProgressMeter, get_timestamp
from utils.metric import cal_psnr

__all__ = [
    "__title__",
    "__summary__",
    "__url__",
    "__version__",
    "__author__",
    "__email__",
    "__license__",
]


try:
    from deepinv import models
    __all__ += ['models']
except ImportError:
    pass

try:
    from deepinv import loss
    __all__ += ['loss']
except ImportError:
    pass


try:
    from deepinv.models import iterative
    __all__ += ['iterative']
except ImportError:
    pass


try:
    from deepinv import datasets
    __all__ += ['datasets']
except ImportError:
    pass

try:
    from deepinv import nn
    __all__ += ['nn']
except ImportError:
    pass


try:
    from deepinv.diffops import physics
    __all__ += ['physics']
except ImportError:
    pass


try:
    from deepinv.diffops import transform
    __all__ += ['transform']
except ImportError:
    pass


try:
    from deepinv.diffops import noise
    __all__ += ['noise']
except ImportError:
    pass

try:
    from torch import optim
    __all__ += ['optim']
except ImportError:
    pass

try:
    from deepinv.loss import loss as metric
    __all__ += ['metric']
except ImportError:
    pass

# GLOBAL PROPERTY
dtype = torch.float
device = torch.device(f'cuda:0')


def load_checkpoint(model, path_checkpoint, device):
    checkpoint = torch.load(path_checkpoint, map_location=device)
    model.load_state_dict(checkpoint['state_dict'])
    return model

def data_parallel(model, ngpu=1):
    if ngpu>1:
        model = torch.nn.DataParallel(model, list(range(ngpu)))
    return model


# def iterative(mode, backbone_net, weight_tied, step_size, iterations, device, physics):
#     return models.iterative.unroll(mode, backbone_net, weight_tied, step_size, iterations, device, physics)


def train(model,
          train_dataloader,
          learning_rate,
          epochs,
          schedule,
          loss_closure=None,#list
          loss_weight=None,
          optimizer=None,
          physics=None,
          noise=None,
          dtype=torch.float,
          device=torch.device(f"cuda:0"),
          ckp_interval=100,
          save_path=None):

    losses = AverageMeter('loss', ':.2e')
    meters = [losses]
    progress = ProgressMeter(epochs, meters, surfix=f"[{save_path}]")

    save_path='./ckp/{}'.format('_'.join([get_timestamp(), save_path]))
    # os.makedirs(save_path, exist_ok=True)

    f = lambda y: model(physics.A_dagger(y))

    for epoch in range(epochs):
        adjust_learning_rate(optimizer, epoch, learning_rate, cos=False, epochs=epochs, schedule=schedule)

        for i, x in enumerate(train_dataloader):
            x = x[0] if isinstance(x, list) else x
            x = x.type(dtype).to(device) # todo: dataloader is only for y

            y0 = physics.A(x)  # generate measurement input y
            if noise is not None:
                y0 = noise(y0)

            x1 = f(y0)
            y1 = physics.A(x1)

            # loss = loss_closure(y0, model)

            loss = 0
            for l, w in zip(loss_closure, loss_weight):
                if l.name in ['mc']:
                    loss += w * l(x1, y0)
                if l.name in ['ms']:
                    loss += w * l(y0, f)
                if l.name in ['sup']:
                    loss += w * l(x1, x)
                if l.name.startswith('sure'):
                    loss += w * l(y0, y1, f)
                if l.name in ['ei', 'rei']:
                    loss += w * l(x1, f)

            losses.update(loss.item())

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        progress.display(epoch + 1)
        save_model(epoch, model, optimizer, ckp_interval, epochs, save_path)
    return model


def debug(model,
          train_dataloader,
          learning_rate,
          epochs,
          schedule,
          loss_closure=None,  # list
          loss_weight=None,
          optimizer=None,
          physics=None,
          noise=None,
          dtype=torch.float,
          device=torch.device(f"cuda:0"),
          ckp_interval=100,
          save_path=None,
          verbos=False):
    # losses = AverageMeter('loss', ':.2e')
    losses = AverageMeter('loss', ':.3e')
    meters = [losses]
    if verbos:
        losses_verbos = [AverageMeter('loss_' + l.name, ':.3e') for l in loss_closure]
        psnr_net = AverageMeter('psnr_net', ':.2f')
        psnr_fbp = AverageMeter('psnr_fbp', ':.2f')

        for loss in losses_verbos:
            meters.append(loss)
        meters.append(psnr_fbp)
        meters.append(psnr_net)

    progress = ProgressMeter(epochs, meters, surfix=f"[{save_path}]")

    save_path = './ckp/{}'.format('_'.join([get_timestamp(), save_path]))
    # os.makedirs(save_path, exist_ok=True)

    f = lambda y: model(physics.A_dagger(y))

    for epoch in range(epochs):
        adjust_learning_rate(optimizer, epoch, learning_rate, cos=False, epochs=epochs, schedule=schedule)

        for i, x in enumerate(train_dataloader):
            x = x[0] if isinstance(x, list) else x
            x = x.type(dtype).to(device)  # todo: dataloader is only for y

            y0 = physics.A(x)  # generate measurement input y
            if noise is not None:
                y0 = noise(y0)

            fbp = physics.A_dagger(y0)

            x1 = f(y0)
            y1 = physics.A(x1)

            # loss = loss_closure(y0, model)

            loss = 0
            for l, w, v in zip(loss_closure, loss_weight, losses_verbos):
                if l.name in ['mc']:
                    loss += w * l(x1, y0)
                if l.name in ['ms']:
                    loss += w * l(y0, f)
                if l.name in ['sup']:
                    loss += w * l(x1, x)
                if l.name.startswith('sure'):
                    loss += w * l(y0, y1, f)
                if l.name in ['ei', 'rei']:
                    loss += w * l(x1, f)
                v.update(loss.item())

            losses.update(loss.item())

            if verbos:
                psnr_fbp.update(cal_psnr(fbp, x))
                psnr_net.update(cal_psnr(x1, x))

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        progress.display(epoch + 1)
        save_model(epoch, model, optimizer, ckp_interval, epochs, save_path)
    return model