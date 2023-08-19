# ------------------------------------------------------------------------
# Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

import sys
sys.path = ["./arctic_tools"] + sys.path

import time
import torch
import random
import argparse
import datetime
import numpy as np
from pathlib import Path
import matplotlib.pyplot as plt
import torch.backends.cudnn as cudnn

import wandb
from cfg import Config
import util.misc as utils
import datasets.samplers as samplers
from torch.utils.data import DataLoader

from models import build_model
from datasets import build_dataset
from engine import train_pose, test_pose
from util.settings import get_args_parser
#GPUS_PER_NODE=4 ./tools/run_dist_launch.sh 4 ./configs/r50_deformable_detr.sh

# main script
def main(args):
    utils.init_distributed_mode(args)
    print("git:\n  {}\n".format(utils.get_sha()))

    if args.wandb:
        if args.distributed and utils.get_local_rank() != 0:
            pass
        else:
            wandb.init(
                project='2023_ICCV_hand'
            )
            wandb.config.update(args)

    if args.frozen_weights is not None:
        assert args.masks, "Frozen training is meant for segmentation only"
    print(args)
    cfg = Config(args)
    
    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    cudnn.benchmark = False
    cudnn.deterministic = True
    random.seed(seed)

    if not args.eval:
        dataset_train = build_dataset(image_set='train', args=args)
    dataset_val = build_dataset(image_set='val', args=args)
    # dataset_val[0]

    model, criterion = build_model(args, cfg)
    model.to(device)
    model_without_ddp = model

    if args.wandb:
        if args.distributed:
            if utils.get_local_rank() == 0:
                wandb.watch(model_without_ddp)
        else:
            wandb.watch(model_without_ddp)


    if args.dataset_file == 'arctic':
        collate_fn=utils.collate_custom_fn
    else:
        collate_fn=utils.collate_fn

    if args.distributed:
        if args.cache_mode:
            if not args.eval:
                sampler_train = samplers.NodeDistributedSampler(dataset_train)
            sampler_val = samplers.NodeDistributedSampler(dataset_val, shuffle=False)
        else:
            if not args.eval:
                sampler_train = samplers.DistributedSampler(dataset_train)
            sampler_val = samplers.DistributedSampler(dataset_val, shuffle=False)
    else:
        if not args.eval:
            sampler_train = torch.utils.data.RandomSampler(dataset_train)
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    if not args.eval:
        batch_sampler_train = torch.utils.data.BatchSampler(
            sampler_train, args.batch_size, drop_last=True)
        data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train,
                                    collate_fn=collate_fn, num_workers=args.num_workers,
                                    pin_memory=True)
    # data_loader_val = DataLoader(dataset_val, 1, sampler=sampler_val,
    data_loader_val = DataLoader(dataset_val, args.val_batch_size, sampler=sampler_val,
                                drop_last=False, collate_fn=collate_fn, num_workers=args.num_workers,
                                pin_memory=True)

    # lr_backbone_names = ["backbone.0", "backbone.neck", "input_proj", "transformer.encoder"]
    def match_name_keywords(n, name_keywords):
        out = False
        for b in name_keywords:
            if b in n:
                out = True
                break
        return out

    param_dicts = [
        {
            "params":
                [p for n, p in model_without_ddp.named_parameters()
                 if not match_name_keywords(n, args.lr_backbone_names) and not match_name_keywords(n, args.lr_linear_proj_names) and p.requires_grad],
            "lr": args.lr,
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if match_name_keywords(n, args.lr_backbone_names) and p.requires_grad],
            "lr": args.lr_backbone,
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if match_name_keywords(n, args.lr_linear_proj_names) and p.requires_grad],
            "lr": args.lr * args.lr_linear_proj_mult,
        }
    ]
    if args.sgd:
        optimizer = torch.optim.SGD(param_dicts, lr=args.lr, momentum=0.9,
                                    weight_decay=args.weight_decay)
    else:
        optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                      weight_decay=args.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)

    if args.distributed:
        print(utils.get_local_rank())
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        model_without_ddp = model.module

    if args.frozen_weights is not None:
        checkpoint = torch.load(args.frozen_weights, map_location='cpu')
        model_without_ddp.detr.load_state_dict(checkpoint['model'])

    output_dir = Path(args.output_dir)
    if args.resume:
        new_model_dict = model_without_ddp.state_dict()
        
        #temp dir
        checkpoint = torch.load(args.resume)
        pretraind_model = checkpoint["model"]
        name_list = [name for name in new_model_dict.keys() if name in pretraind_model.keys()]

        if args.use_h2o_pth:
            name_list = list(filter(lambda x : "cls_embed" not in x, name_list))
            name_list = list(filter(lambda x : "obj_keypoint_embed" not in x, name_list))
        pretraind_model_dict = {k : v for k, v in pretraind_model.items() if k in name_list }
        
        new_model_dict.update(pretraind_model_dict)
        missing_keys, unexpected_keys = model_without_ddp.load_state_dict(new_model_dict)
        unexpected_keys = [k for k in unexpected_keys if not (k.endswith('total_params') or k.endswith('total_ops'))]
        if len(missing_keys) > 0:
            print('Missing Keys: {}'.format(missing_keys))
        if len(unexpected_keys) > 0:
            print('Unexpected Keys: {}'.format(unexpected_keys))

    print("Start training")
    start_time = time.time()

    # for evaluation
    if args.eval:
        test_pose(model, criterion, data_loader_val, device, cfg, args=args, vis=args.visualization)
        sys.exit(0)
        
    # for training
    else:
        for epoch in range(args.start_epoch, args.epochs):
            if args.distributed:
                sampler_train.set_epoch(epoch)

            # collate_fn(
            #     data_loader_train.dataset[0] + data_loader_train.dataset[1] + data_loader_train.dataset[2] + data_loader_train.dataset[3]
            # )

            # train
            train_pose(
                model, criterion, data_loader_train, optimizer, device, epoch, args.clip_max_norm, args, cfg=cfg
            )
            lr_scheduler.step()

            utils.save_on_master({
                'model': model_without_ddp.state_dict(),
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': lr_scheduler.state_dict(),
                'epoch': epoch,
                'args': args,
            }, f'{args.output_dir}/{args.dataset_file}/{epoch}.pth')

            # evaluate
            test_pose(model, criterion, data_loader_val, device, cfg, args=args, vis=args.visualization, epoch=epoch)
            
        total_time = time.time() - start_time
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))
        print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Deformable DETR training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_known_args()[0]

    if args.dataset_file == 'arctic':
        from arctic_tools.src.parsers.parser import construct_args
        args = construct_args(parser)

    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    main(args)