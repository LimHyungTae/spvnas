# import os
# os.environ["PL_TORCH_DISTRIBUTED_BACKEND"] = "gloo"
import os
import argparse
import sys

import numpy as np
import torch
import torch.backends.cudnn
import torch.cuda
import torch.nn
import torch.utils.data

import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from torchpack import distributed as dist
from torchpack.callbacks import Callbacks, SaverRestore
from torchpack.environ import auto_set_run_dir, set_run_dir
from torchpack.utils.config import configs
from torchpack.utils.logging import logger
from tqdm import tqdm

from core import builder
from core.callbacks import MeanIoU
from core.trainers import SemanticKITTITrainer
from model_zoo import minkunet, spvcnn, spvnas_specialized


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('config', metavar='FILE', help='config file')
    parser.add_argument('--run-dir', metavar='DIR', help='run directory')
    parser.add_argument('--name', type=str, help='model name')
    parser.add_argument('--save-dir', type=str, help='save directory of `.label` files')
    args, opts = parser.parse_known_args()

    for i in range(11):
        directory_name = f"{i:02d}"  # format the number as a two-digit string
        if not os.path.exists(args.save_dir + "/" + directory_name):
            os.makedirs(args.save_dir + "/" + directory_name)

    configs.load(args.config, recursive=True)
    configs.update(opts)

    if configs.distributed:
       dist.init()

       torch.backends.cudnn.benchmark = True
       torch.cuda.set_device(dist.local_rank())

    if args.run_dir is None:
        args.run_dir = auto_set_run_dir()
    else:
        set_run_dir(args.run_dir)

    logger.info(' '.join([sys.executable] + sys.argv))
    logger.info(f'Experiment started: "{args.run_dir}".' + '\n' + f'{configs}')

    dataset = builder.make_dataset()
    dataflow = {}
    for split in dataset:
        sampler = torch.utils.data.distributed.DistributedSampler(
            dataset[split],
            num_replicas=dist.size(),
            rank=dist.rank(),
            shuffle=(split == 'train'))
        dataflow[split] = torch.utils.data.DataLoader(
            dataset[split],
            batch_size=configs.batch_size if split == 'train' else 1,
            sampler=sampler,
            num_workers=configs.workers_per_gpu,
            pin_memory=True,
            collate_fn=dataset[split].collate_fn)

    if 'spvnas' in args.name.lower():
        model = spvnas_specialized(args.name)
    elif 'spvcnn' in args.name.lower():
        model = spvcnn(args.name)
    elif 'mink' in args.name.lower():
        model = minkunet(args.name)
    else:
        raise NotImplementedError

    if configs.distributed:
        model = torch.nn.parallel.DistributedDataParallel(
            model.cuda(),
            device_ids=[dist.local_rank()],
            find_unused_parameters=True)
    model.eval()

    criterion = builder.make_criterion()
    optimizer = builder.make_optimizer(model)
    scheduler = builder.make_scheduler(optimizer)

    trainer = SemanticKITTITrainer(model=model,
                                   criterion=criterion,
                                   optimizer=optimizer,
                                   scheduler=scheduler,
                                   num_workers=configs.workers_per_gpu,
                                   seed=configs.train.seed)
    callbacks = Callbacks([
        SaverRestore(),
        MeanIoU(configs.data.num_classes, configs.data.ignore_label)
    ])
    callbacks._set_trainer(trainer)
    trainer.callbacks = callbacks
    trainer.dataflow = dataflow['test']

    trainer.before_train()
    trainer.before_epoch()

    model.eval()
    with torch.no_grad():
        for feed_dict in tqdm(dataflow['test'], desc='eval'):
            _inputs = {}
            for key, value in feed_dict.items():
                if 'name' not in key:
                    _inputs[key] = value.cuda()

            inputs = _inputs['lidar']

            targets = feed_dict['targets'].F.long().cuda(non_blocking=True)
            outputs = model(inputs)

            invs = feed_dict['inverse_map']
            all_labels = feed_dict['targets_mapped']
            _outputs = []
            _targets = []
            for idx in range(invs.C[:, -1].max() + 1):
                cur_scene_pts = (inputs.C[:, -1] == idx).cpu().numpy()
                cur_inv = invs.F[invs.C[:, -1] == idx].cpu().numpy()
                cur_label = (all_labels.C[:, -1] == idx).cpu().numpy()
                outputs_mapped = outputs[cur_scene_pts][cur_inv].argmax(1)
                targets_mapped = all_labels.F[cur_label]
                _outputs.append(outputs_mapped)
                _targets.append(targets_mapped)
            outputs = torch.cat(_outputs, 0)
            targets = torch.cat(_targets, 0)
            output_dict = {'outputs': outputs, 'targets': targets}
            trainer.after_step(output_dict)

            abs_input_path = feed_dict['file_name'][0].split("/")
            output_file_name = args.save_dir + "/" + abs_input_path[-3] + "/" + abs_input_path[-1].replace('.bin', '.label')
            sem = outputs.cpu().numpy().astype(np.uint32)
            ins = np.zeros_like(sem).astype(np.uint32)
            pred_eval = sem + (ins << 16)
            pred_eval.astype(np.uint32).tofile(output_file_name)
        trainer.after_epoch()


if __name__ == '__main__':
    main()
