# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import datetime
import logging
import time

import torch
import torch.distributed as dist

from maskrcnn_benchmark.utils.comm import get_world_size
from maskrcnn_benchmark.utils.metric_logger import MetricLogger

from apex import amp

import numpy as np
import os
import pickle as pkl

from maskrcnn_benchmark.utils.tensor_saver import create_tensor_saver
from maskrcnn_benchmark.utils.tensor_saver import get_tensor_saver
from maskrcnn_benchmark.utils.tensor_saver import create_mock_data_maker
from maskrcnn_benchmark.utils.tensor_saver import get_mock_data_maker


def reduce_loss_dict(loss_dict):
    """
    Reduce the loss dictionary from all processes so that process with rank
    0 has the averaged results. Returns a dict with the same fields as
    loss_dict, after reduction.
    """
    world_size = get_world_size()
    if world_size < 2:
        return loss_dict
    with torch.no_grad():
        loss_names = []
        all_losses = []
        for k in sorted(loss_dict.keys()):
            loss_names.append(k)
            all_losses.append(loss_dict[k])
        all_losses = torch.stack(all_losses, dim=0)
        dist.reduce(all_losses, dst=0)
        if dist.get_rank() == 0:
            # only main process gets accumulated, so only divide by
            # world_size in this case
            all_losses /= world_size
        reduced_losses = {k: v for k, v in zip(loss_names, all_losses)}
    return reduced_losses


def do_train(
    cfg,
    model,
    data_loader,
    optimizer,
    scheduler,
    checkpointer,
    device,
    checkpoint_period,
    arguments,
):
    logger = logging.getLogger("maskrcnn_benchmark.trainer")
    logger.info("Start training")
    meters = MetricLogger(delimiter="  ")
    max_iter = len(data_loader)
    start_iter = arguments["iteration"]
    model.train()
    start_training_time = time.time()
    end = time.time()

    create_tensor_saver(
        training=True,
        base_dir="train_dump",
        iteration=start_iter,
        max_iter=start_iter + 10,
    )
    create_mock_data_maker(start_iter)

    for iteration, (images, targets, image_id) in enumerate(
        data_loader, start_iter
    ):
        data_time = time.time() - end
        iteration = iteration + 1
        arguments["iteration"] = iteration

        get_tensor_saver().step()

        scheduler.step()

        if cfg.ONEFLOW_PYTORCH_COMPARING.FAKE_IMAGE_DATA_PATH != "":
            fake_image_path = os.path.join(
                cfg.ONEFLOW_PYTORCH_COMPARING.FAKE_IMAGE_DATA_PATH,
                "image_{}.npy".format(iteration),
            )
            fake_images = np.load(fake_image_path)
            fake_images = np.transpose(fake_images, (0, 3, 1, 2))
            images.tensors = torch.tensor(fake_images)
            logger.info(
                "Load fake image data from {} at itor {}".format(
                    fake_image_path, iteration
                )
            )
        else:
            get_tensor_saver().save(
                tensor=images.tensors.permute(0, 2, 3, 1), tensor_name="image"
            )

        get_mock_data_maker().step()
        get_mock_data_maker().update_image(image_id, images)
        get_mock_data_maker().update_target(targets)

        images = images.to(device)
        targets = [target.to(device) for target in targets]

        loss_dict = model(images, targets)

        get_mock_data_maker().save()

        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = reduce_loss_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        meters.update(loss=losses_reduced, **loss_dict_reduced)

        optimizer.zero_grad()
        # Note: If mixed precision is not used, this ends up doing nothing
        # Otherwise apply loss scaling for mixed-precision recipe
        with amp.scale_loss(losses, optimizer) as scaled_losses:
            scaled_losses.backward()
        optimizer.step()

        batch_time = time.time() - end
        end = time.time()
        meters.update(time=batch_time, data=data_time)

        eta_seconds = meters.time.global_avg * (max_iter - iteration)
        eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))

        if (
            iteration % cfg.ONEFLOW_PYTORCH_COMPARING.METRICS_PERIODS == 0
            or iteration == max_iter
        ):
            logger.info(
                meters.delimiter.join(
                    [
                        "eta: {eta}",
                        "iter: {iter}",
                        "{meters}",
                        "lr: {lr:.6f}",
                        "max mem: {memory:.0f}",
                    ]
                ).format(
                    eta=eta_string,
                    iter=iteration,
                    meters=str(meters),
                    lr=optimizer.param_groups[0]["lr"],
                    memory=torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                )
            )
        if iteration % checkpoint_period == 0:
            checkpointer.save("model_{:07d}".format(iteration), **arguments)
        if iteration == max_iter:
            checkpointer.save("model_final", **arguments)

            if cfg.ONEFLOW_PYTORCH_COMPARING.DUMP_MOMENTUM_BUFFER:
                state_dict = optimizer.state_dict()
                model_name2momentum_buffer = {}
                for key, value in model.named_parameters():
                    if value.requires_grad:
                        momentum_buffer = (
                            state_dict["state"][id(value)]["momentum_buffer"]
                            .cpu()
                            .detach()
                            .numpy()
                        )
                        model_name2momentum_buffer[key] = momentum_buffer

                pkl.dump(
                    model_name2momentum_buffer,
                    open(
                        "model_final" + ".model_name2momentum_buffer.pkl", "wb"
                    ),
                    protocol=2,
                )

    total_training_time = time.time() - start_training_time
    total_time_str = str(datetime.timedelta(seconds=total_training_time))
    logger.info(
        "Total training time: {} ({:.4f} s / it)".format(
            total_time_str, total_training_time / (max_iter)
        )
    )
