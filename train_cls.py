import os
import torch
import torch.optim as optim
import torch.optim.lr_scheduler as lr_sched
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.autograd import Variable
import numpy as np
from torchvision import transforms
from models import DCCNN_SSN_Cls as DCCNN_SSN
from data import ModelNet40Cls
import utils.pytorch_utils as pt_utils
import utils.pointnet2_utils as pointnet2_utils
import data.data_utils as d_utils

import argparse
import random
import yaml
import gc
import torch.nn.functional as F
import datetime
import logging

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.deterministic = True

seed = np.random.randint(1, 10000)
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)            
torch.cuda.manual_seed(seed)       
torch.cuda.manual_seed_all(seed)

time_str = str(datetime.datetime.now().strftime('_%Y%m%d%H%M%S'))
checkpoint = './log/seed_%d'%(seed) + time_str
try:
    os.makedirs(checkpoint)
except OSError:
    pass

screen_logger = logging.getLogger("Model")
screen_logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(message)s')
file_handler = logging.FileHandler(os.path.join(checkpoint, "out.txt"))
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
screen_logger.addHandler(file_handler)

def printf(str):
    screen_logger.info(str)
    print(str)

parser = argparse.ArgumentParser(description='Dynamic-Cover CNN Shape Classification Training')
parser.add_argument('--config', default='cfgs/config_ssn_cls.yaml', type=str)

def main():
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.load(f,Loader=yaml.FullLoader)
    print("\n**************************")
    for k, v in config['common'].items():
        setattr(args, k, v)
        print('\n[%s]:'%(k), v)
    print("\n**************************\n")

    train_transforms = transforms.Compose([
        d_utils.PointcloudToTensor()
    ])
    test_transforms = transforms.Compose([
        d_utils.PointcloudToTensor()
    ])

    printf('==> Preparing data..')
    train_dataset = ModelNet40Cls(num_points = args.num_points, root = args.data_root, transforms=train_transforms)
    train_dataloader = DataLoader(
        train_dataset, 
        batch_size=args.batch_size,
        shuffle=True, 
        num_workers=int(args.workers), 
        pin_memory=True
    )

    test_dataset = ModelNet40Cls(num_points = args.num_points, root = args.data_root, transforms=test_transforms, train=False)
    test_dataloader = DataLoader(
        test_dataset, 
        batch_size=args.batch_size,
        shuffle=False, 
        num_workers=int(args.workers), 
        pin_memory=True
    )

    printf(f"args: {args}")
    printf('==> Building model..')
    model = DCCNN_SSN(num_classes = args.num_classes, num_kernel = args.num_kernel, input_channels = args.input_channels, relation_prior = args.relation_prior, use_xyz = True)
    model.cuda()
    printf(f"DC-CNN: {model}")

    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    printf(f'Number of training parameters: %.2f M'% (params/1e6) )

    optimizer = optim.Adam(model.parameters(), lr=args.base_lr, weight_decay=args.weight_decay)

    lr_lbmd = lambda e: max(args.lr_decay**(e // args.decay_step), args.lr_clip / args.base_lr)
    bnm_lmbd = lambda e: max(args.bn_momentum * args.bn_decay**(e // args.decay_step), args.bnm_clip)
    lr_scheduler = lr_sched.LambdaLR(optimizer, lr_lbmd)
    bnm_scheduler = pt_utils.BNMomentumScheduler(model, bnm_lmbd)
    
    if args.checkpoint is not '':
        model.load_state_dict(torch.load(args.checkpoint))
        printf('Load model successfully: %s' % (args.checkpoint))

    criterion = nn.CrossEntropyLoss()
    num_batch = len(train_dataset)/args.batch_size
    
    # training
    train(train_dataloader, test_dataloader, model, criterion, optimizer, lr_scheduler, bnm_scheduler, args, num_batch)
    

def train(train_dataloader, test_dataloader, model, criterion, optimizer, lr_scheduler, bnm_scheduler, args, num_batch):
    PointcloudScaleAndTranslate = d_utils.PointcloudScaleAndTranslate()   # initialize augmentation
    global g_acc
    g_acc = 0.0    # only save the model whose acc > 0.91
    batch_count = 0
    model.train()
    for epoch in range(args.epochs):
        time_begin = datetime.datetime.now()
        for i, data in enumerate(train_dataloader, 0):
            if lr_scheduler is not None:
                lr_scheduler.step(epoch)
            if bnm_scheduler is not None:
                bnm_scheduler.step(epoch-1)
            points, target = data
            points, target = points.cuda(), target.cuda()
            points, target = Variable(points), Variable(target)

            # fastest point sampling
            fps_idx = pointnet2_utils.furthest_point_sample(points, 1200)  # (B, npoint)
            fps_idx = fps_idx[:, np.random.choice(1200, args.num_points, False)]
            points = pointnet2_utils.gather_operation(points.transpose(1, 2).contiguous(), fps_idx).transpose(1, 2).contiguous()  # (B, N, 3)
            
            # augmentation
            points.data = PointcloudScaleAndTranslate(points.data)
            
            optimizer.zero_grad()
            
            pred = model(points)
            target = target.view(-1)
            loss = criterion(pred, target)
            loss.backward()
            optimizer.step()
            if i % args.print_freq_iter == 0:
                printf('[epoch %3d: %3d/%3d] \t train loss: %0.6f \t lr: %0.5f' %(epoch+1, i, num_batch, loss.data.clone(), lr_scheduler.get_lr()[0]))
            batch_count += 1
            # validation in between an epoch
            if args.evaluate and batch_count % int(args.val_freq_epoch * num_batch) == 0:
                validate(test_dataloader, model, criterion, args, batch_count, time_begin)


def validate(test_dataloader, model, criterion, args, iter, time_begin): 
    global g_acc
    model.eval()
    losses, preds, labels = [], [], []
    gc.collect()
    with torch.no_grad():
        for j, data in enumerate(test_dataloader, 0):
            points, target = data
            points, target = points.cuda(), target.cuda()
            
            # fastest point sampling
            fps_idx = pointnet2_utils.furthest_point_sample(points, args.num_points)  # (B, npoint)
            points = pointnet2_utils.gather_operation(points.transpose(1, 2).contiguous(), fps_idx).transpose(1, 2).contiguous()

            pred = model(points)
            target = target.view(-1)
            loss = criterion(pred, target)
            losses.append(loss.data.clone())
            _, pred_choice = torch.max(pred.data, -1)
            
            preds.append(pred_choice)
            labels.append(target.data)
            
        preds = torch.cat(preds, 0)
        labels = torch.cat(labels, 0)
        acc = (preds == labels).sum().item() / labels.numel()
        time_cost = int((datetime.datetime.now() - time_begin).total_seconds())
        printf('\nval loss: %0.6f \t acc: %0.6f \t time:%ss \n' %(np.array(losses).mean(), acc, time_cost))
        if acc >= g_acc:
            g_acc = acc
            torch.save(model.state_dict(), '%s/cls_ssn_iter_%d_acc_%0.6f.pth' % (checkpoint, iter, acc))
        model.train()
    
if __name__ == "__main__":
    main()
