# ------------------------------------------------------------------------
# DN-DETR
# Copyright (c) 2022 IDEA. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]


import torch
from util.misc import (NestedTensor, nested_tensor_from_tensor_list,
                       accuracy, get_world_size, interpolate,
                       is_dist_avail_and_initialized, inverse_sigmoid)
# from .DABDETR import sigmoid_focal_loss
from util import box_ops
import torch.nn.functional as F


def sigmoid_focal_loss(inputs, targets, num_boxes, alpha: float = 0.25, gamma: float = 2):
    """
    Loss used in RetinaNet for dense detection: https://arxiv.org/abs/1708.02002.
    Args:
        inputs: A float tensor of arbitrary shape.
                The predictions for each example.
        targets: A float tensor with the same shape as inputs. Stores the binary
                 classification label for each element in inputs
                (0 for the negative class and 1 for the positive class).
        alpha: (optional) Weighting factor in range (0,1) to balance
                positive vs negative examples. Default = -1 (no weighting).
        gamma: Exponent of the modulating factor (1 - p_t) to
               balance easy vs hard examples.
    Returns:
        Loss tensor
    """
    prob = inputs.sigmoid()
    ce_loss = F.binary_cross_entropy_with_logits(inputs, targets, reduction="none")
    p_t = prob * targets + (1 - prob) * (1 - targets)
    loss = ce_loss * ((1 - p_t) ** gamma)

    if alpha >= 0:
        alpha_t = alpha * targets + (1 - alpha) * (1 - targets)
        loss = alpha_t * loss


    return loss.mean(1).sum() / num_boxes

def prepare_for_dn(dn_args, tgt_weight, embedweight, batch_size, training, num_queries, num_classes, hidden_dim, label_enc):
    """
    The major difference from DN-DAB-DETR is that the author process pattern embedding pattern embedding in its detector
    forward function and use learnable tgt embedding, so we change this function a little bit.
    :param dn_args: targets, scalar, label_noise_scale, box_noise_scale, num_patterns
    :param tgt_weight: use learnbal tgt in dab deformable detr
    :param embedweight: positional anchor queries
    :param batch_size: bs
    :param training: if it is training or inference
    :param num_queries: number of queires
    :param num_classes: number of classes
    :param hidden_dim: transformer hidden dim
    :param label_enc: encode labels in dn
    :return:
    """

    if training:
        targets, scalar, label_noise_scale, box_noise_scale, num_patterns, contrastive = dn_args
    else:
        num_patterns = dn_args

    if num_patterns == 0:
        num_patterns = 1
    if tgt_weight is not None and embedweight is not None:
        indicator0 = torch.zeros([num_queries * num_patterns, 1]).cuda()
        # sometimes the target is empty, add a zero part of label_enc to avoid unused parameters
        tgt = torch.cat([tgt_weight, indicator0], dim=1) + label_enc.weight[0][0]*torch.tensor(0).cuda()
        refpoint_emb = embedweight
    else:
        tgt = None
        refpoint_emb = None

    if training:
        if contrastive:
            new_targets = []
            
            tmp_label = [torch.cat([torch.tensor(l).cuda(), torch.tensor(len(l) * [num_classes], dtype=torch.int64).cuda()], dim=0) for l in targets['labels']]
            tmp_key = [torch.cat([key, key], dim=0) for key in targets['keypoints']]
            for l, k in zip(tmp_label, tmp_key):
                new_t = {}
                new_t['labels'] = l
                new_t['keys'] = k
                new_targets.append(new_t)
            # for t in targets:
            #     new_t = {}
            #     new_t['labels'] = torch.cat([t['labels'], torch.tensor(len(t['labels']) * [num_classes], dtype=torch.int64).cuda()], dim=0)
            #     new_t['boxes'] = torch.cat([t['boxes'], t['boxes']], dim=0)
            #     new_targets.append(new_t)
            targets = new_targets
        known = [(torch.ones_like(t['labels'])).cuda() for t in targets] # [ [ 1, 1], [1, 1, 1], ... ]
        know_idx = [torch.nonzero(t) for t in known] # [ [0, 1], [0, 1, 2], ... ]
        known_num = [sum(k) for k in known] # [ 2, 3, ... ]

        # to use fix number of dn queries
        if int(max(known_num)) == 0:
            scalar = 1
        elif scalar >= 100 and int(max(known_num))>0:
            scalar=scalar//int(max(known_num))

        if scalar <= 0:
            scalar = 1

        # can be modified to selectively denosie some label or boxes; also known label prediction
        unmask_key = unmask_label = torch.cat(known)
        # torch.cat(known) = [1, 1, 1, 1, 1, ... ]
        labels = torch.cat([t['labels'] for t in targets])
        keys = torch.cat([t['keys'] for t in targets])
        batch_idx = torch.cat([torch.full_like(t['labels'].long(), i) for i, t in enumerate(targets)])
        # batch_idx = [ 0, 0, 1, 1, 1, ... ]

        known_indice = torch.nonzero(unmask_label + unmask_key)
        # known_indice = [ 0, 1, 2, 3, 4, ... ] "elementwise addition = logical_and" of labels and bbox
        known_indice = known_indice.view(-1)

        # add noise
        known_indice = known_indice.repeat(scalar, 1).view(-1)
        known_bid = batch_idx.repeat(scalar, 1).view(-1)
        known_labels = labels.repeat(scalar, 1).view(-1)
        known_keys = keys.repeat(scalar, 1)
        known_labels_expaned = known_labels.clone()
        known_key_expand = known_keys.clone()
        #print("known_bbox_expand = " +str(known_bbox_expand.shape))

        # noise on the label
        if label_noise_scale > 0:
            p = torch.rand_like(known_labels_expaned.float())
            chosen_indice = torch.nonzero(p < (label_noise_scale)).view(-1)  # usually half of bbox noise
            new_label = torch.randint_like(chosen_indice, 0, num_classes)  # randomly put a new one here
            known_labels_expaned.scatter_(0, chosen_indice, new_label)

        # noise on the box
        if box_noise_scale > 0:
            known_key_ = known_keys
            # # x, y, w, h를 x1, y1, x2, y2로 바꾸는 코드
            # known_key_ = torch.zeros_like(known_keys)
            # known_key_[:, :2] = known_keys[:, :2] - known_keys[:, 2:] / 2
            # known_key_[:, 2:] = known_keys[:, :2] + known_keys[:, 2:] / 2

            diff = known_key_expand
            # # x, y, w, h를 w/2, h/2, w/2, h/2로 바꾸는 코드
            # diff = torch.zeros_like(known_key_expand)
            # diff[:, :2] = known_key_expand[:, 2:] / 2
            # diff[:, 2:] = known_key_expand[:, 2:] / 2

            if contrastive:
                rand_sign = torch.randint_like(known_key_expand, low=0, high=2, dtype=torch.float32) * 2.0 - 1.0
                rand_part = torch.rand_like(known_key_expand)
                positive_idx = torch.tensor(range(len(keys)//2)).long().cuda().unsqueeze(0).repeat(scalar, 1)
                positive_idx += (torch.tensor(range(scalar)) * len(keys)).long().cuda().unsqueeze(1)
                positive_idx = positive_idx.flatten()
                negative_idx = positive_idx + len(keys)//2
                rand_part[negative_idx] += 1.0
                rand_part *= rand_sign

                known_key_ += torch.mul(rand_part, diff).cuda() * box_noise_scale

            else:
                known_key_ += torch.mul((torch.rand_like(known_key_expand) * 2 - 1.0),
                                           diff).cuda() * box_noise_scale

            # 다시 x, y, w, h coord로 바꿔줌
            known_key_ = known_key_.clamp(min=0.0, max=1.0)
            # known_key_expand[:, :2] = (known_key_[:, :2] + known_key_[:, 2:]) / 2
            # known_key_expand[:, 2:] = known_key_[:, 2:] - known_key_[:, :2]

        # in the case of negatives, override the label with "num_classes" label
        if contrastive:
            known_labels_expaned.scatter_(0, negative_idx, num_classes)

        m = known_labels_expaned.long().to('cuda')
        input_label_embed = label_enc(m)
        # add dn part indicator
        indicator1 = torch.ones([input_label_embed.shape[0], 1]).cuda()
        input_label_embed = torch.cat([input_label_embed, indicator1], dim=1)
        input_key_embed = inverse_sigmoid(known_key_expand)
        single_pad = int(max(known_num))
        pad_size = int(single_pad * scalar)
        padding_label = torch.zeros(pad_size, hidden_dim).cuda()
        padding_key = torch.zeros(pad_size, 42).cuda()

        if tgt is not None and refpoint_emb is not None:
            input_query_label = torch.cat([padding_label, tgt], dim=0).repeat(batch_size, 1, 1)
            input_query_key = torch.cat([padding_key, refpoint_emb], dim=0).repeat(batch_size, 1, 1)
        else:
            input_query_label = padding_label.repeat(batch_size, 1, 1)
            input_query_key = padding_key.repeat(batch_size, 1, 1)

        # map in order
        map_known_indice = torch.tensor([]).to('cuda')
        if len(known_num):
            map_known_indice = torch.cat([torch.tensor(range(num)) for num in known_num])  # [0, 1, 0, 1, 2]
            map_known_indice = torch.cat([map_known_indice + single_pad * i for i in range(scalar)]).long()
            # 
        if len(known_bid):
            input_query_label[(known_bid.long(), map_known_indice)] = input_label_embed # [ bs, query_idx, hidden_dim ]
            input_query_key[(known_bid.long(), map_known_indice)] = input_key_embed

        tgt_size = pad_size + num_queries * num_patterns
        attn_mask = torch.ones(tgt_size, tgt_size).to('cuda') < 0
        # match query cannot see the reconstruct
        attn_mask[pad_size:, :pad_size] = True
        # reconstruct cannot see each other
        for i in range(scalar):
            if i == 0:
                attn_mask[single_pad * i:single_pad * (i + 1), single_pad * (i + 1):pad_size] = True
            if i == scalar - 1:
                attn_mask[single_pad * i:single_pad * (i + 1), :single_pad * i] = True
            else:
                attn_mask[single_pad * i:single_pad * (i + 1), single_pad * (i + 1):pad_size] = True
                attn_mask[single_pad * i:single_pad * (i + 1), :single_pad * i] = True
        mask_dict = {
            'known_indice': torch.as_tensor(known_indice).long(),
            'batch_idx': torch.as_tensor(batch_idx).long(),
            'map_known_indice': torch.as_tensor(map_known_indice).long(),
            'known_lbs_keys': (known_labels, known_keys),
            'know_idx': know_idx,
            'pad_size': pad_size,
            'scalar': scalar,
            'contrastive' : contrastive,
        }
    else:  # no dn for inference
        if tgt is not None and refpoint_emb is not None:
            input_query_label = tgt.repeat(batch_size, 1, 1)
            input_query_key = refpoint_emb.repeat(batch_size, 1, 1)
        else:
            input_query_label = None
            input_query_key = None
        attn_mask = None
        mask_dict = None

    # input_query_label = input_query_label.transpose(0, 1)
    # input_query_bbox = input_query_bbox.transpose(0, 1)

    return input_query_label, input_query_key, attn_mask, mask_dict


# def dn_post_process(outputs_class, outputs_coord, mask_dict):
def dn_post_process(outputs_class, outputs_coord, mask_dict):
    """
    post process of dn after output from the transformer
    put the dn part in the mask_dict
    """
    if mask_dict and mask_dict['pad_size'] > 0:
        output_known_class = outputs_class[:, :, :mask_dict['pad_size'], :] # [ levels, bs, query size, hidden dim]
        # output_known_coord = outputs_coord[:, :, :mask_dict['pad_size'], :]
        outputs_class = outputs_class[:, :, mask_dict['pad_size']:, :]
        # outputs_coord = outputs_coord[:, :, mask_dict['pad_size']:, :]
        # mask_dict['output_known_lbs_bboxes']=(output_known_class,output_known_coord)
        mask_dict['output_known_lbs_bboxes']=output_known_class
    return outputs_class, outputs_coord


def prepare_for_loss(mask_dict):
    """
    prepare dn components to calculate loss
    Args:
        mask_dict: a dict that contains dn information
    Returns:

    """
    output_known_class, output_known_coord = mask_dict['output_known_lbs_bboxes']
    known_labels, known_bboxs = mask_dict['known_lbs_bboxes']
    map_known_indice = mask_dict['map_known_indice']
    # [0, 1, 2, 3, 4, ..., 0, 1, 2, 3, 4, ...]

    known_indice = mask_dict['known_indice']
    # [0, 1, 2, 3, 4, ...]

    batch_idx = mask_dict['batch_idx']
    bid = batch_idx[known_indice]
    num_tgt = known_indice.numel()

    if len(output_known_class) > 0:
        output_known_class = output_known_class.permute(1, 2, 0, 3)[(bid, map_known_indice)].permute(1, 0, 2)
        # [ levels, bs, qs, hdim ] -> [ bs, qs, lvls, hdim] -> [ lvls, bs * qs, hdim ]
        output_known_coord = output_known_coord.permute(1, 2, 0, 3)[(bid, map_known_indice)].permute(1, 0, 2)

    if mask_dict['contrastive'] :
        scalar = mask_dict['scalar']
        num_tgt = num_tgt // 2
        num_box = num_tgt // scalar
        positive_idx = torch.tensor(range(num_box)).long().cuda().unsqueeze(0).repeat(scalar, 1)
        positive_idx += (torch.tensor(range(scalar)) * num_box * 2).long().cuda().unsqueeze(1)
        positive_idx = positive_idx.flatten()
        # bbox reconstruction only use positive cases
        # but, class reconstruction use both positive and negative(with no-object)
        output_known_coord = output_known_coord[:,positive_idx,:]
        known_bboxs = known_bboxs[positive_idx,:]

    return known_labels, known_bboxs, output_known_class, output_known_coord, num_tgt


def tgt_loss_boxes(src_boxes, tgt_boxes, num_tgt,):
    """Compute the losses related to the bounding boxes, the L1 regression loss and the GIoU loss
       targets dicts must contain the key "boxes" containing a tensor of dim [nb_target_boxes, 4]
       The target boxes are expected in format (center_x, center_y, w, h), normalized by the image size.
    """
    if len(tgt_boxes) == 0:
        return {
            'tgt_loss_bbox': torch.as_tensor(0.).to('cuda'),
            'tgt_loss_giou': torch.as_tensor(0.).to('cuda'),
        }

    loss_bbox = F.l1_loss(src_boxes, tgt_boxes, reduction='none')

    losses = {}
    losses['tgt_loss_bbox'] = loss_bbox.sum() / num_tgt

    loss_giou = 1 - torch.diag(box_ops.generalized_box_iou(
        box_ops.box_cxcywh_to_xyxy(src_boxes),
        box_ops.box_cxcywh_to_xyxy(tgt_boxes)))
    losses['tgt_loss_giou'] = loss_giou.sum() / num_tgt
    return losses


def tgt_loss_labels(src_logits_, tgt_labels_, num_tgt, focal_alpha, log=True):
    """Classification loss (NLL)
    targets dicts must contain the key "labels" containing a tensor of dim [nb_target_boxes]
    """
    if len(tgt_labels_) == 0:
        return {
            'tgt_loss_ce': torch.as_tensor(0.).to('cuda'),
            'tgt_class_error': torch.as_tensor(0.).to('cuda'),
        }

    src_logits, tgt_labels= src_logits_.unsqueeze(0), tgt_labels_.unsqueeze(0)

    target_classes_onehot = torch.zeros([src_logits.shape[0], src_logits.shape[1], src_logits.shape[2] + 1],
                                        dtype=src_logits.dtype, layout=src_logits.layout, device=src_logits.device)
    target_classes_onehot.scatter_(2, tgt_labels.unsqueeze(-1), 1)

    target_classes_onehot = target_classes_onehot[:, :, :-1]
    loss_ce = sigmoid_focal_loss(src_logits, target_classes_onehot, num_tgt, alpha=focal_alpha, gamma=2) * src_logits.shape[1]

    losses = {'tgt_loss_ce': loss_ce}

    losses['tgt_class_error'] = 100 - accuracy(src_logits_, tgt_labels_)[0]
    return losses


def compute_dn_loss(mask_dict, training, aux_num, focal_alpha):
    """
       compute dn loss in criterion
       Args:
           mask_dict: a dict for dn information
           training: training or inference flag
           aux_num: aux loss number
           focal_alpha:  for focal loss
       """
    losses = {}
    if training and 'output_known_lbs_bboxes' in mask_dict:
        known_labels, known_bboxs, output_known_class, output_known_coord, \
        num_tgt = prepare_for_loss(mask_dict)
        # -1 is the final level [ levels, bs * qs, hidden_dim ]
        losses.update(tgt_loss_labels(output_known_class[-1], known_labels, num_tgt, focal_alpha))
        losses.update(tgt_loss_boxes(output_known_coord[-1], known_bboxs, num_tgt))
    else:
        losses['tgt_loss_bbox'] = torch.as_tensor(0.).to('cuda')
        losses['tgt_loss_giou'] = torch.as_tensor(0.).to('cuda')
        losses['tgt_loss_ce'] = torch.as_tensor(0.).to('cuda')
        losses['tgt_class_error'] = torch.as_tensor(0.).to('cuda')

    if aux_num:
        for i in range(aux_num):
            # dn aux loss
            if training and 'output_known_lbs_bboxes' in mask_dict:
                l_dict = tgt_loss_labels(output_known_class[i], known_labels, num_tgt, focal_alpha)
                l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                losses.update(l_dict)
                l_dict = tgt_loss_boxes(output_known_coord[i], known_bboxs, num_tgt)
                l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                losses.update(l_dict)
            else:
                l_dict = dict()
                l_dict['tgt_loss_bbox'] = torch.as_tensor(0.).to('cuda')
                l_dict['tgt_class_error'] = torch.as_tensor(0.).to('cuda')
                l_dict['tgt_loss_giou'] = torch.as_tensor(0.).to('cuda')
                l_dict['tgt_loss_ce'] = torch.as_tensor(0.).to('cuda')
                l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                losses.update(l_dict)
    return losses
