# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import logging
import time
import os

import torch
from tqdm import tqdm

from maskrcnn_benchmark.data.datasets.evaluation import evaluate
from ..utils.comm import is_main_process, get_world_size
from ..utils.comm import all_gather
from ..utils.comm import synchronize
from ..utils.timer import Timer, get_time_str

from maskrcnn_benchmark.utils.tensor_saver import create_tensor_saver
from maskrcnn_benchmark.utils.tensor_saver import get_tensor_saver

import numpy as np
import pickle as pkl

def compute_on_dataset(cfg, model, data_loader, device, logger, timer=None):
    model.eval()
    results_dict = {}
    cpu_device = torch.device("cpu")
    fake_image_list = []
    for idx, batch in enumerate(tqdm(data_loader)):
        images, targets, image_ids = batch
        if cfg.ONEFLOW_PYTORCH_COMPARING.FAKE_IMAGE_DATA_PATH != "":
            fake_image_path = os.path.join(cfg.ONEFLOW_PYTORCH_COMPARING.FAKE_IMAGE_DATA_PATH, 'image_{}.npy'.format(idx))
            fake_images = np.load(fake_image_path)
            fake_images = np.transpose(fake_images, (0, 3, 1, 2))
            images.tensors = torch.tensor(fake_images)
            logger.info("Load fake image data from {} at itor {}".format(fake_image_path, idx))
        else:
            # get_tensor_saver().save(
            #     tensor=images.tensors,
            #     tensor_name='images_{}'.format(idx),
            #     save_grad=True,
            #     save_shape=False,
            # )
            image_size = []
            for box_list in targets:
                image_size.append(np.array(box_list.size, dtype=np.int32))
            image_size = np.stack(image_size, axis=0)
            image_size = np.concatenate([image_size[:, 1:2], image_size[:, 0:1]], axis=1)
            print("image_size, height, width")
            print(image_size)

            # gen fake image list
            fake_image_list.append(images.tensors.detach().cpu().numpy())
            if idx == len(data_loader) - 1:
                print("dump fake image list...")
                pkl.dump(fake_image_list, open("/tmp/fake_image_list.pkl", "wb"))

        images = images.to(device)
        with torch.no_grad():
            if timer:
                timer.tic()
            output = model(images)
            if timer:
                torch.cuda.synchronize()
                timer.toc()
            output = [o.to(cpu_device) for o in output]
        results_dict.update(
            {img_id: result for img_id, result in zip(image_ids, output)}
        )
    return results_dict


def _accumulate_predictions_from_multiple_gpus(predictions_per_gpu):
    all_predictions = all_gather(predictions_per_gpu)
    if not is_main_process():
        return
    # merge the list of dicts
    predictions = {}
    for p in all_predictions:
        predictions.update(p)
    # convert a dict where the key is the index in a list
    image_ids = list(sorted(predictions.keys()))
    if len(image_ids) != image_ids[-1] + 1:
        logger = logging.getLogger("maskrcnn_benchmark.inference")
        logger.warning(
            "Number of images that were gathered from multiple processes is not "
            "a contiguous set. Some images might be missing from the evaluation"
        )

    # convert to a list
    predictions = [predictions[i] for i in image_ids]
    return predictions


def inference(
        cfg,
        model,
        data_loader,
        dataset_name,
        iou_types=("bbox",),
        box_only=False,
        device="cuda",
        expected_results=(),
        expected_results_sigma_tol=4,
        output_folder=None,
):
    # convert to a torch.device for efficiency
    device = torch.device(device)
    num_devices = get_world_size()
    logger = logging.getLogger("maskrcnn_benchmark.inference")
    dataset = data_loader.dataset
    logger.info("Start evaluation on {} dataset({} images).".format(dataset_name, len(dataset)))
    total_timer = Timer()
    inference_timer = Timer()
    total_timer.tic()

    create_tensor_saver(
        training=False,
        base_dir="inference_dump",
        iteration=0,
        max_iter=1
    )

    predictions = compute_on_dataset(cfg, model, data_loader, device, logger, inference_timer)
    # wait for all processes to complete before measuring the time
    synchronize()
    total_time = total_timer.toc()
    total_time_str = get_time_str(total_time)
    logger.info(
        "Total run time: {} ({} s / img per device, on {} devices)".format(
            total_time_str, total_time * num_devices / len(dataset), num_devices
        )
    )
    total_infer_time = get_time_str(inference_timer.total_time)
    logger.info(
        "Model inference time: {} ({} s / img per device, on {} devices)".format(
            total_infer_time,
            inference_timer.total_time * num_devices / len(dataset),
            num_devices,
        )
    )

    predictions = _accumulate_predictions_from_multiple_gpus(predictions)
    if not is_main_process():
        return

    if output_folder:
        torch.save(predictions, os.path.join(output_folder, "predictions.pth"))

    extra_args = dict(
        box_only=box_only,
        iou_types=iou_types,
        expected_results=expected_results,
        expected_results_sigma_tol=expected_results_sigma_tol,
    )

    return evaluate(dataset=dataset,
                    predictions=predictions,
                    output_folder=output_folder,
                    **extra_args)
