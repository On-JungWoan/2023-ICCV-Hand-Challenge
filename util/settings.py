import torch
import pickle
import argparse
import numpy as np
import os.path as op

def get_args_parser():
    parser = argparse.ArgumentParser('Deformable DETR Detector', add_help=False)
    parser.add_argument('--lr', default=2e-4, type=float)
    parser.add_argument('--lr_backbone_names', default=["backbone.0"], type=str, nargs='+')
    parser.add_argument('--lr_backbone', default=2e-5, type=float)
    parser.add_argument('--lr_linear_proj_names', default=['reference_points', 'sampling_offsets'], type=str, nargs='+')
    parser.add_argument('--lr_linear_proj_mult', default=0.1, type=float)
    parser.add_argument('--batch_size', default=8, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=50, type=int)
    parser.add_argument('--lr_drop', default=40, type=int)
    parser.add_argument('--lr_drop_epochs', default=None, type=int, nargs='+')
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')


    parser.add_argument('--sgd', action='store_true')

    # Variants of Deformable DETR
    parser.add_argument('--with_box_refine', default=False, action='store_true')
    parser.add_argument('--two_stage', default=False, action='store_true')

    # Model parameters
    parser.add_argument('--frozen_weights', type=str, default=None,
                        help="Path to the pretrained model. If set, only the mask head will be trained")

    # * Backbone
    parser.add_argument('--backbone', default='resnet50', type=str,
                        help="Name of the convolutional backbone to use")
    parser.add_argument('--dilation', action='store_true',
                        help="If true, we replace stride with dilation in the last convolutional block (DC5)")
    parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                        help="Type of positional embedding to use on top of the image features")
    parser.add_argument('--position_embedding_scale', default=2 * np.pi, type=float,
                        help="position / size * scale")
    parser.add_argument('--num_feature_levels', default=4, type=int, help='number of feature levels')

    # * Transformer
    parser.add_argument('--enc_layers', default=6, type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--dec_layers', default=6, type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument('--dim_feedforward', default=1024, type=int,
                        help="Intermediate size of the feedforward layers in the transformer blocks")
    parser.add_argument('--hidden_dim', default=256, type=int,
                        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout', default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument('--nheads', default=8, type=int,
                        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_queries', default=300, type=int,
                        help="Number of query slots")
    parser.add_argument('--dec_n_points', default=4, type=int)
    parser.add_argument('--enc_n_points', default=4, type=int)

    # Loss
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help="Disables auxiliary decoding losses (loss at each layer)")

    # * Matcher
    # parser.add_argument('--set_cost_class', default=2, type=float,            
    #                     help="Class coefficient in the matching cost")
    # parser.add_argument('--set_cost_keypoint', default=5, type=float,
    #                     help="L1 box coefficient in the matching cost") 
    parser.add_argument('--set_cost_class', default=1.5, type=float,            
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_keypoint', default=4, type=float,
                        help="L1 box coefficient in the matching cost") 

    # * Loss coefficients
    parser.add_argument('--cls_loss_coef', default=2, type=float) 
    parser.add_argument('--keypoint_loss_coef', default=5, type=float)
    parser.add_argument('--focal_alpha', default=0.25, type=float)

    # dataset parameters
    parser.add_argument('--train_stage', default='pose')
    parser.add_argument('--dataset_file', default='FPHA')
    parser.add_argument('--coco_path', required=True, type=str)
    parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('--output_dir', default='./weights/local_test',
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=42, type=int)
    # parser.add_argument('--resume', default='./weights/paper_pose.pth', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--num_workers', default=8, type=int)
    parser.add_argument('--cache_mode', default=False, action='store_true', help='whether to cache images on memory')
    parser.add_argument('--img_size', default=(960, 540), type=tuple)
    parser.add_argument('--make_pickle', default=False, action='store_true')

    # for debug
    parser.add_argument('--eval', default=False, action='store_true')
    parser.add_argument('--visualization', default=False, action='store_true')
    parser.add_argument('--debug', default=False, action='store_true')
    parser.add_argument('--num_debug', default=3, type=int)

    # for train
    parser.add_argument('--not_use_params', default=[], nargs='+', help='The params, including this keywords, are ignored when the model imports the checkpoint.')
    parser.add_argument('--wandb', default=False, action='store_true', help='Use wandb')
    parser.add_argument('--dist_backend', default=None, help='Choose backend of distribtion mode.')

    # for eval
    parser.add_argument('--val_batch_size', default=4, type=int)
    parser.add_argument('--test_viewpoint', default=None, type=str, \
                        help='If you want to evaluate a specific viewpoint, then you can simply put the viewpoint name.\n \
                            e.g) --test_viewpoint nusar-2021_action_both_9081-c11b_9081_user_id_2021-02-12_161433/HMC_21110305_mono10bit')
    parser.add_argument('--eval_metrics', default=["aae","mpjpe.ra","mrrpe","success_rate","cdev","mdev","acc_err_pose"], nargs='+', \
                        help='Choose evaluation metrics.')
    parser.add_argument('--extract', default=False, action='store_true',
                        help='Save pred_keypoints to json format.')
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--resume_dir', default='', help='resume dir from checkpoint')
    parser.add_argument('--full_validation', default=False, action='store_true', \
                        help='Use full size of validation dataset to evaluate the model.')
    
    # for custom arctic
    parser.add_argument('--seq', default=None, type=str)
    parser.add_argument('--split_window', default=False, action='store_true')
    parser.add_argument('--feature_type', default='local_fm', choices=['origin', 'global_fm', 'local_fm'])
    parser.add_argument('--train_smoothnet', default=False, action='store_true')
    parser.add_argument('--iter', default=20, type=int)

    return parser


def match_name_keywords(n, name_keywords):
    out = False
    for b in name_keywords:
        if b in n:
            out = True
            break
    return out


def set_training_scheduler(args, model, general_lr=None):
    if general_lr is None:
        general_lr = args.lr

    param_dicts = [
        {
            "params":
                [p for n, p in model.named_parameters()
                 if not match_name_keywords(n, args.lr_backbone_names) and not match_name_keywords(n, args.lr_linear_proj_names) and p.requires_grad],
            "lr": general_lr,
        },
        {
            "params": [p for n, p in model.named_parameters() if match_name_keywords(n, args.lr_backbone_names) and p.requires_grad],
            "lr": args.lr_backbone,
        },
        {
            "params": [p for n, p in model.named_parameters() if match_name_keywords(n, args.lr_linear_proj_names) and p.requires_grad],
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

    return optimizer, lr_scheduler


def load_resume(args, model, resume):
    checkpoint = torch.load(resume, map_location='cpu')
    ckpt = checkpoint['model'].copy()
    for key in ckpt.keys():
        if len(args.not_use_params) != 0:
            for ig_key in args.not_use_params:
                if ig_key in key:
                    print(f'ignored params : {key}')
                    checkpoint['model'].pop(key)
    del ckpt

    missing_keys, unexpected_keys = model.load_state_dict(checkpoint['model'], strict=False)
    unexpected_keys = [k for k in unexpected_keys if not (k.endswith('total_params') or k.endswith('total_ops'))]
    if len(missing_keys) > 0:
        print('Missing Keys: {}'.format(missing_keys))
    if len(unexpected_keys) > 0:
        print('Unexpected Keys: {}'.format(unexpected_keys))
    
    return model


def extract_epoch(file_path):
    file_name = file_path.split('/')[-1]
    epoch = op.splitext(file_name)[0]

    return int(epoch)


def make_arctic_environments(args):
    check_dir = 'datasets/arctic/common/environments.py'
    env_dir = op.join(args.coco_path, args.dataset_file)

    with open(check_dir, 'w') as f:
        f.write(
            f"DATASET_ROOT = '{env_dir}'"
        )