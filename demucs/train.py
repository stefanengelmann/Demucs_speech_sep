# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import sys

import tqdm
import torch as th
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from .utils import apply_model, average_metric, center_trim

from torch import nn
from itertools import permutations
import numpy as np


def train_model(epoch,
                dataset,
                model,
                criterion,
                optimizer,
                augment,
                quantizer=None,
                diffq=0,
                repeat=1,
                device="cpu",
                seed=None,
                workers=4,
                world_size=1,
                batch_size=16):

    if world_size > 1:
        sampler = DistributedSampler(dataset)
        sampler_epoch = epoch * repeat
        if seed is not None:
            sampler_epoch += seed * 1000
        sampler.set_epoch(sampler_epoch)
        batch_size //= world_size
        loader = DataLoader(dataset, batch_size=batch_size, sampler=sampler, num_workers=workers)
    else:
        loader = DataLoader(dataset, batch_size=batch_size, num_workers=workers, shuffle=True)
    current_loss = 0
    model_size = 0
    for repetition in range(repeat):
        tq = tqdm.tqdm(loader,
                       ncols=120,
                       desc=f"[{epoch:03d}] train ({repetition + 1}/{repeat})",
                       leave=False,
                       file=sys.stdout,
                       unit=" batch")
        total_loss = 0
        for idx, sources in enumerate(tq):
            if len(sources) < batch_size:
                # skip uncomplete batch for augment.Remix to work properly
                continue
            sources = sources.to(device)
            #print("sources shape:", sources.shape)
            #sources = augment(sources)
            mix = sources.sum(dim=1)
            #print("mix shape:", mix.shape)

            estimates = model(mix)
            #print("estimates shape:", estimates.shape)
            sources = center_trim(sources, estimates)
            #print("sources shape after center trim:", sources.shape)
            loss = criterion(estimates, sources)
            model_size = 0
            if quantizer is not None:
                model_size = quantizer.model_size()

            train_loss = loss + diffq * model_size
            train_loss.backward()
            grad_norm = 0
            for p in model.parameters():
                if p.grad is not None:
                    grad_norm += p.grad.data.norm()**2
            grad_norm = grad_norm**0.5
            optimizer.step()
            optimizer.zero_grad()

            if quantizer is not None:
                model_size = model_size.item()

            total_loss += loss.item()
            current_loss = total_loss / (1 + idx)
            tq.set_postfix(loss=f"{current_loss:.4f}", ms=f"{model_size:.2f}",
                           grad=f"{grad_norm:.5f}")

            # free some space before next round
            del sources, mix, estimates, loss, train_loss

        if world_size > 1:
            sampler.epoch += 1

    if world_size > 1:
        current_loss = average_metric(current_loss)
    return current_loss, model_size


def validate_model(epoch, #Batched validation (fast)
                   dataset,
                   model,
                   criterion,
                   device="cpu",
                   rank=0,
                   world_size=1,
                   shifts=0,
                   overlap=0.25,
                   split=False, 
                   workers=4,
                   batch_size=16):

    loader = DataLoader(dataset, batch_size=batch_size, num_workers=workers, shuffle=False)
    
    tq = tqdm.tqdm(loader,
                    ncols=120,
                    desc=f"[{epoch:03d}] valid",
                    leave=False,
                    file=sys.stdout,
                    unit=" batch")
    total_loss = 0
    for idx, sources in enumerate(tq):
        sources = sources.to(device)
        #print("sources shape:", sources.shape)
        #sources = augment(sources)
        mix = sources.sum(dim=1)
        #print("mix shape:", mix.shape)

        with th.no_grad():
            estimates = model(mix)
        #print("estimates shape:", estimates.shape)
        sources = center_trim(sources, estimates)
        #print("sources shape after center trim:", sources.shape)
        loss = criterion(estimates, sources)
        

        total_loss += loss.item()
        current_loss = total_loss / (1 + idx)
        tq.set_postfix(loss=f"{current_loss:.4f}")

        # free some space before next round
        del sources, mix, estimates, loss
    
    return current_loss

# def validate_model(epoch,  # Non-batched validation (slow)
#                    dataset,
#                    model,
#                    criterion,
#                    device="cpu",
#                    rank=0,
#                    world_size=1,
#                    shifts=0,
#                    overlap=0.25,
#                    split=False,
#                    workers=4,
#                    batch_size=16):
#     indexes = range(rank, len(dataset), world_size)
#     tq = tqdm.tqdm(indexes,
#                    ncols=120,
#                    desc=f"[{epoch:03d}] valid",
#                    leave=False,
#                    file=sys.stdout,
#                    unit=" track")
#     #current_loss = 0
#     total_loss=0
#     for index in tq:
#         streams = dataset[index]
#         # first five minutes to avoid OOM on --upsample models
#         streams = streams[..., :15_000_000]
#         streams = streams.to(device)
#         sources = streams
#         mix = streams.sum(dim=0)
#         estimates = apply_model(model, mix, shifts=shifts, split=split, overlap=overlap)

#         n_src=sources.shape[0]
#         perms = th.tensor(list(permutations(range(n_src))), dtype=th.long).to(device)

#         testCriterion = nn.L1Loss()

#         testLoss=testCriterion(estimates,sources)
#         print(testLoss)

#         for perm in perms:
#             estimates_tmp=estimates[perm,...]
#             testLoss_tmp=testCriterion(estimates_tmp,sources)
#             if testLoss_tmp<testLoss:
#                 testLoss=th.clone(testLoss_tmp)
#                 estimates=th.clone(estimates_tmp)
#         print(testLoss)
#         print("---------------")


#         print(testCriterion(estimates,sources))
#         loss = criterion(estimates.unsqueeze(0), sources.unsqueeze(0))
#         print(loss)
#         print("__________________________")
#         #current_loss += loss.item() / len(indexes)
#         total_loss += loss.item()
#         current_loss = total_loss / (1 + index)
#         tq.set_postfix(loss=f"{current_loss:.4f}")

#         del estimates, streams, sources

#     if world_size > 1:
#         current_loss = average_metric(current_loss, len(indexes))
#     return current_loss
