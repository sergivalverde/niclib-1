import torch
import os
import copy

from niclib.network.training import EarlyStoppingTrain
from niclib.network.generator import PatchGeneratorBuilder

from niclib.io.results import *
from niclib.metrics import *

from niclib.utils import *
from niclib.io.metrics import print_metrics_list


class SimpleCrossvalidation:
    def __init__(self, model_definition, images, num_folds, model_trainer, train_instr_gen, val_instr_gen, checkpoint_pathfile, log_pathfile, test_predictor, results_path):
        assert isinstance(model_trainer, EarlyStoppingTrain)
        assert isinstance(train_instr_gen, PatchGeneratorBuilder)
        assert isinstance(val_instr_gen, PatchGeneratorBuilder)

        self.model_definition = model_definition
        self.images = images
        self.num_folds = num_folds

        if checkpoint_pathfile.endswith('.pt'):
            checkpoint_pathfile, _ = os.path.splitext(checkpoint_pathfile)
        self.checkpoint_pathfile = checkpoint_pathfile + '_{}_to_{}.pt'

        if log_pathfile.endswith('.csv'):
            log_pathfile, _ = os.path.splitext(log_pathfile)
        self.log_pathfile = log_pathfile + '_{}_to_{}.csv'

        self.trainer = model_trainer
        self.train_instr_gen = train_instr_gen
        self.val_instr_gen = val_instr_gen

        self.predictor = test_predictor
        if not os.path.exists(results_path):
            os.mkdir(results_path)
        self.results_path = results_path

    def run_crossval(self):
        print("\n" + "-" * 75 + "\n Running {}-fold crossvalidation \n".format(self.num_folds) + "-" * 75 + "\n", sep='')


        for fold_idx in range(self.num_folds):
            start_idx_val, stop_idx_val = get_crossval_indexes(
                images=self.images, fold_idx=fold_idx, num_folds=self.num_folds)

            print("\n" + "-" * 50 +"\n Running fold {} - val images {} to {} \n".format(fold_idx, start_idx_val, stop_idx_val) + "-" * 50 + "\n", sep='')

            model_fold = copy.deepcopy(self.model_definition)
            train_images = self.images[:start_idx_val] + self.images[stop_idx_val:]
            val_images = self.images[start_idx_val:stop_idx_val]

            print("Building training generator...")
            train_gen = self.train_instr_gen.build_patch_generator(images=train_images)
            print("Building validation generator...")
            val_gen = self.val_instr_gen.build_patch_generator(images=val_images)
            print("\nGenerators with {} training and {} validation patches".format(
                len(train_gen)*self.trainer.bs, len(val_gen)*self.trainer.bs))

            model_filepath =  self.checkpoint_pathfile.format(start_idx_val, stop_idx_val)
            log_filepath = self.log_pathfile.format(start_idx_val, stop_idx_val)
            self.trainer.train(model_fold, train_gen, val_gen, model_filepath, log_filepath)

            print("Loading trained model {}".format(model_filepath))
            model_fold = torch.load(model_filepath, self.trainer.device)

            # Predict validation set
            for n, sample in enumerate(val_images):
                probs = self.predictor.predict_sample(model_fold, sample)
                save_image_probs(self.results_path + '{}_probs.nii.gz'.format(sample.id), sample, probs)