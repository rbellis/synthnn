#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
synthnn.exec.fa_train

command line interface to train a deep convolutional neural network for
synthesis of MR (brain) images with fastai

Author: Jacob Reinhold (jacob.reinhold@jhu.edu)

Created on: Nov 2, 2018
"""

import argparse
import logging
import sys
import warnings

with warnings.catch_warnings():
    warnings.filterwarnings('ignore', category=FutureWarning)
    warnings.filterwarnings('ignore', category=UserWarning)
    import fastai as fai
    import fastai.vision as faiv
    import torch
    from torch import nn
    from niftidataset.fastai import niidatabunch, get_patch3d, get_slice
    from synthnn import split_filename
    from synthnn.models.unet import Unet
    from synthnn.util.io import AttrDict


def arg_parser():
    parser = argparse.ArgumentParser(description='train a CNN for MR image synthesis')

    required = parser.add_argument_group('Required')
    required.add_argument('-s', '--source-dir', type=str, required=True,
                          help='path to directory with source images')
    required.add_argument('-t', '--target-dir', type=str, required=True,
                          help='path to directory with target images')

    options = parser.add_argument_group('Options')
    options.add_argument('-vs', '--valid-split', type=float, default=0.2,
                          help='split the data in source_dir and target_dir into train/validation '
                               'with this split percentage [Default=0.2]')
    options.add_argument('-vsd', '--valid-source-dir', type=str, default=None,
                          help='path to directory with source images for validation, '
                               'see -vs for default action if this is not provided [Default=None]')
    options.add_argument('-vtd', '--valid-target-dir', type=str, default=None,
                          help='path to directory with target images for validation, '
                               'see -vs for default action if this is not provided [Default=None]')
    options.add_argument('-na', '--nn-arch', type=str, default='unet', choices=('unet', 'nconv'),
                         help='specify neural network architecture to use')
    options.add_argument('-o', '--output', type=str, default=None,
                         help='path to output the trained model')
    options.add_argument('-v', '--verbosity', action="count", default=0,
                         help="increase output verbosity (e.g., -vv is more than -v)")
    options.add_argument('-csv', '--out-csv', type=str, default='history',
                         help='name of output csv which holds training log')
    options.add_argument('-ocf', '--out-config-file', type=str, default='config.json',
                         help='output a config file for the options used in this experiment '
                              '(saves them as a json file with the name as input in this argument)')

    nn_options = parser.add_argument_group('Neural Network Options')
    nn_options.add_argument('-ps', '--patch-size', type=int, default=64,
                            help='patch size^3 or ^2 (depending on if net3d enabled) extracted from image '
                                 '(0 for a full slice, sample-axis must be defined if full slice used) [Default=64]')
    nn_options.add_argument('-n', '--n-jobs', type=int, default=None,
                            help='number of CPU processors to use for data loading [Default=None (all cpus)]')
    nn_options.add_argument('-ne', '--n-epochs', type=int, default=100,
                            help='number of epochs [Default=100]')
    nn_options.add_argument('-bpe', '--batches-per-epoch', type=int, default=10,
                            help='number of batches in each epoch [Default=10]')
    nn_options.add_argument('-nl', '--n-layers', type=int, default=3,
                            help='number of layers to use in network (different meaning per arch) [Default=3]')
    nn_options.add_argument('-ks', '--kernel-size', type=int, default=3,
                            help='convolutional kernel size (cubed) [Default=3]')
    nn_options.add_argument('-sa', '--sample-axis', type=int, default=None,
                            help='axis on which to sample for 2d (None for random orientation) [Default=None]')
    nn_options.add_argument('-sp', '--sample-pct', type=float, default=(0, 1), nargs=2,
                            help='range along axis (as percentage) from which to randomly sample in 2d [Default=(0,1)]')
    nn_options.add_argument('-dc', '--deconv', action='store_true', default=False,
                            help='use transpose conv and strided conv for upsampling & downsampling respectively [Default=False]')
    nn_options.add_argument('-im', '--interp-mode', type=str, default='nearest', choices=('nearest','bilinear','trilinear'),
                            help='use this type of interpolation for upsampling (when deconv is false) [Default=nearest]')
    nn_options.add_argument('-flr', '--flip-lr', action='store_true', default=False,
                            help='use flip lr data augmentation')
    nn_options.add_argument('-rot', '--rotate', action='store_true', default=False,
                            help='use rotation for data augmentation')
    nn_options.add_argument('-zm', '--zoom', action='store_true', default=False,
                            help='use zoom for data augmentation')
    nn_options.add_argument('-oc', '--one-cycle', action='store_true', default=False,
                            help='train using one-cycle policy (see "A Disciplined Approach...", Leslie Smith, 2018)')
    nn_options.add_argument('-dp', '--dropout-prob', type=float, default=0,
                            help='dropout probability per conv block [Default=0]')
    nn_options.add_argument('-lr', '--learning-rate', type=float, default=1e-3,
                            help='learning rate of the neural network (uses Adam) [Default=1e-3]')
    nn_options.add_argument('-bs', '--batch-size', type=int, default=32,
                            help='batch size (num of images to process at once) [Default=32]')
    nn_options.add_argument('-cbp', '--channel-base-power', type=int, default=5,
                            help='number of channels in the first layer of unet (2**cbp) [Default=5]')
    nn_options.add_argument('-pl', '--plot-loss', type=str, default=None,
                            help='plot the loss vs epoch and save at the filename provided here [Default=None]')
    nn_options.add_argument('-usc', '--upsampconv', action='store_true', default=False,
                            help='Use resize-convolution in the U-net as per the Distill article: '
                                 '"Deconvolution and Checkerboard Artifacts" [Default=False]')
    nn_options.add_argument('-atu', '--add-two-up', action='store_true', default=False,
                            help='Add two to the kernel size on the upsampling in the U-Net as '
                                 'per Zhao, et al. 2017 [Default=False]')
    nn_options.add_argument('-nm', '--normalization', type=str, default='instance', choices=('instance', 'batch', 'none'),
                            help='type of normalization layer to use in network [Default=instance]')
    nn_options.add_argument('-ac', '--activation', type=str, default='relu',
                            choices=('relu', 'lrelu', 'elu', 'prelu', 'celu', 'selu', 'tanh', 'sigmoid'),
                            help='type of activation to use throughout network except output [Default=relu]')
    nn_options.add_argument('-oac', '--out-activation', type=str, default='linear', choices=('relu', 'lrelu', 'linear'),
                            help='type of activation to use in network on output [Default=linear]')
    nn_options.add_argument('-mp', '--fp16', action='store_true', default=False,
                            help='enable mixed precision training')
    nn_options.add_argument('-prl', '--preload', action='store_true', default=False,
                            help='preload dataset (memory intensive) vs loading data from disk each epoch')
    nn_options.add_argument('--disable-cuda', action='store_true', default=False,
                            help='Disable CUDA regardless of availability')
    nn_options.add_argument('--disable-metrics', action='store_true', default=False,
                            help='disable the calculation of ncc, mi, mssim regardless of availability')
    nn_options.add_argument('--net3d', action='store_true', default=False,
                            help='create a 3d network instead of 2d [Default=False]')
    nn_options.add_argument('--n-gpus', type=int, default=1, help='use n-gpus [Default=1]')
    return parser


def main(args=None):
    no_config_file = not sys.argv[1].endswith('.json') if args is None else not args[0].endswith('json')
    if no_config_file:
        args = arg_parser().parse_args(args)
    else:
        import json
        fn = sys.argv[1:][0] if args is None else args[0]
        with open(fn, 'r') as f:
            args = AttrDict(json.load(f))
    if args.verbosity == 1:
        level = logging.getLevelName('INFO')
    elif args.verbosity >= 2:
        level = logging.getLevelName('DEBUG')
    else:
        level = logging.getLevelName('WARNING')
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=level)
    logger = logging.getLogger(__name__)
    try:

        # get the desired neural network architecture
        model = Unet(args.n_layers, kernel_size=args.kernel_size, dropout_p=args.dropout_prob, patch_size=args.patch_size,
                     channel_base_power=args.channel_base_power, add_two_up=args.add_two_up, normalization=args.normalization,
                     activation=args.activation, output_activation=args.out_activation, is_3d=args.net3d, deconv=args.deconv,
                     interp_mode=args.interp_mode, upsampconv=args.upsampconv)

        logger.debug(model)

        # put the model on the GPU if available and desired
        if torch.cuda.is_available() and not args.disable_cuda:
            model.cuda()
            torch.backends.cudnn.benchmark = True

        # define device to put tensors on
        device = torch.device("cuda" if torch.cuda.is_available() and not args.disable_cuda else "cpu")

        if not args.net3d:
            tfms = val_tfms = [get_slice(pct=args.sample_pct, axis=args.sample_axis)]
            if args.flip_lr:
                tfms.append(faiv.flip_lr(p=0.5))
            if args.rotate:
                tfms.append(faiv.rotate(degrees=(-45, 45.), p=0.5))
            if args.zoom:
                tfms.append(faiv.zoom(scale=(0.95, 1.05), p=0.8))
        else:
            tfms = val_tfms = [get_patch3d(ps=args.patch_size, h_pct=args.sample_pct, w_pct=args.sample_pct, d_pct=args.sample_pct)]

        # define the fastai data class
        n_jobs = args.n_jobs if args.n_jobs is not None else fai.defaults.cpus
        idb = niidatabunch(args.source_dir, args.target_dir, args.valid_split, tfms=tfms, val_tfms=val_tfms,
                           bs=args.batch_size, device=device, n_jobs=n_jobs,
                           val_src_dir=args.valid_source_dir, val_tgt_dir=args.valid_target_dir,
                           b_per_epoch=args.batches_per_epoch)

        # setup the learner
        loss = nn.MSELoss()

        try:
            if not args.disable_metrics:
                from synthnn.util.metrics import ncc, mi
                if not args.net3d:
                    from synthnn.util.metrics import mssim2d as mssim
                else:
                    from synthnn.util.metrics import mssim3d as mssim
                ncc.__name__ = 'NCC'
                mi.__name__ = 'MI'
                mssim.__name__ = 'MSSIM'
                metrics = [ncc, mi, mssim]
            else:
                metrics = []
        except ImportError:
            logger.debug('synthqc not installed so no additional metrics (other than MSE) will be shown')
            metrics = []

        pth, base, _ = split_filename(args.output)
        learner = fai.Learner(idb, model, loss_func=loss, metrics=metrics, model_dir=pth)

        if args.n_gpus > 1:
            logger.debug(f'Enabling use of {torch.cuda.device_count()} gpus')
            learner.model = torch.nn.DataParallel(learner.model)

        # enable fp16 (mixed) precision if desired
        if args.fp16:
            learner.to_fp16()

        # train the learner
        cb = fai.callbacks.CSVLogger(learner, args.out_csv)
        if not args.one_cycle:
            learner.fit(args.n_epochs, args.learning_rate, callbacks=cb)
        else:
            learner.fit_one_cycle(args.n_epochs, args.learning_rate, callbacks=[cb])

        # output a config file if desired
        if args.out_config_file is not None:
            import json
            import os
            arg_dict = vars(args)
            # add these keys so that the output config file can be edited for use in prediction
            arg_dict['trained_model'] = args.output + '.pth'
            arg_dict['monte_carlo'] = None
            arg_dict['predict_dir'] = None
            arg_dict['predict_out'] = None
            with open(args.out_config_file, 'w') as f:
                json.dump(arg_dict, f, sort_keys=True, indent=2)

        # save the trained model
        learner.save(args.output)

        # plot the loss vs epoch (if desired)
        if args.plot_loss is not None:
            import matplotlib.pyplot as plt
            plt.switch_backend('agg')
            learner.recorder.plot_losses()
            plt.savefig(args.plot_loss)

        return 0
    except Exception as e:
        logger.exception(e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
