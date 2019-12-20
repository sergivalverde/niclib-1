import copy
import itertools
from abc import ABC, abstractmethod
from typing import List
import numpy as np
import torch
from torch.utils.data import Dataset as TorchDataset

import time

from ..utils import resample_list, parallel_load

class PatchInstruction:
    def __init__(self, idx, center, shape, normalize_function=None, augment_function=None):
        self.idx = idx
        self.center = center
        self.shape = shape
        self.normalize_function = normalize_function
        self.augment_function = augment_function


class PatchSampling(ABC):
    @abstractmethod
    def sample_centers(self, images, patch_shape):
        pass


class UniformSampling(PatchSampling):
    def __init__(self, step, num_patches=None, masks=None):
        """
        :param step:
        :param num_patches:
        :param masks: TODO
        """

        self.step = step
        self.npatches = num_patches
        self.masks = masks

    def sample_centers(self, images, patch_shape):
        patches_per_image = int(np.ceil(self.npatches / len(images))) if self.npatches is not None else None
        if self.masks is not None:
            return [sample_centers_uniform(img[0], patch_shape, self.step, patches_per_image, img_mask)
                    for img, img_mask in zip(images, self.masks)]
        else:
            return [sample_centers_uniform(img[0], patch_shape, self.step, patches_per_image) for img in images]


class BalancedSampling(PatchSampling):
    def __init__(self, labels, num_patches, add_rand_offset=False):
        self.labels = labels
        self.npatches = num_patches
        self.add_rand_offset = add_rand_offset

    def sample_centers(self, images, patch_shape):
        assert len(images) == len(self.labels)
        assert all(img[0].shape == lbl.shape for img, lbl in zip(images, self.labels)), '{}, {}'.format(images[0].shape, self.labels[0].shape)
        patches_per_image = int(np.ceil(self.npatches / len(images)))

        args = [[label_img, patch_shape, patches_per_image, self.add_rand_offset] for label_img in self.labels]
        result = parallel_load(sample_centers_balanced, args, num_workers=12)
        return result

def _norm_patch(x):
    channel_means = np.mean(x, axis=(1, 2, 3), keepdims=True)
    channel_stds = np.std(x, axis=(1, 2, 3), keepdims=True)
    return np.divide(x - channel_means, channel_stds)

class PatchSet(TorchDataset):
    def  __init__(self, images, patch_shape, sampling, normalize, dtype=torch.float, centers=None):
        """
        Creates a torch dataset that returns patches extracted from images.

        :param List[np.ndarray] images: list of images with shape (C, X, Y, Z)
        :param PatchSampling sampling:
        :param normalize: one of ``'none'``, ``'patch'``, ``'image'``.
        :param dtype: the desired output data type (de"ault: torch.float)
        :param List[List[tuple]] centers: (optional) a list containing a list of centers for each provided image.
        If provided it overrides the sampling and directly uses the provided centers to extract the patches.
        """
        assert all([img.ndim == 4 for img in images]), 'Images must be numpy ndarrays with dimensions (C, X, Y, Z)'
        assert len(patch_shape) == 3
        assert normalize in ['none', 'patch', 'image']
        if centers is None: assert isinstance(sampling, PatchSampling)
        if centers is not None: assert len(centers) == len(images)

        self.images, self.dtype = images, dtype

        # Build all instructions according to centers and normalize
        self.instructions = []
        images_centers = sampling.sample_centers(images, patch_shape) if centers is None else centers

        for image_idx, image_centers in enumerate(images_centers):
            # Compute normalize function for this image's patches
            if normalize == 'patch':
                norm_func = _norm_patch
            elif normalize == 'image': # Update norm_func with the statistics of the image
                means = np.mean(self.images[image_idx], axis=(1,2,3), keepdims=True, dtype=np.float64)
                stds = np.std(self.images[image_idx], axis=(1,2,3), keepdims=True, dtype=np.float64)

                # BY PROVIDING MEANS AND STDS AS DEFAULT ARGUMENTS, WE MAKE A COPY of their values inside norm_func
                # If not, the means and stds would be of the last value (last image) leading to incorrect results
                norm_func = lambda x, m=means, s=stds : (x - m) / s
            else:
                norm_func = lambda x : x  # Identity function (normalize == 'none')

            ## Generate instructions
            self.instructions += [PatchInstruction(
                image_idx, center=center, shape=patch_shape, normalize_function=norm_func) for center in image_centers]

        print("Created PatchSet with {} patches".format(len(self.instructions)))

    def __len__(self):
        return len(self.instructions)

    def __getitem__(self, index):
        instr = self.instructions[index]
        x_patch = copy.deepcopy(self.images[instr.idx][_get_patch_slice(instr.center, instr.shape)])
        if instr.normalize_function is not None:
            x_patch = instr.normalize_function(x_patch)
        if instr.augment_function is not None:
            x_patch = instr.augment_function(x_patch)
        return torch.tensor(np.ascontiguousarray(x_patch), dtype=self.dtype)


def _get_patch_slice(center, patch_shape):
    """
    :param center: a tuple or list of (x,y,z) tuples
    :param tuple patch_shape: (x,y,z) tuple with arr dimensions
    :return: a tuple (channel_slice, x_slice, y_slice, z_slice) or a list of them
    """
    if not isinstance(center, list): center = [center]
    span = [[int(np.ceil(dim / 2.0)), int(np.floor(dim / 2.0))] for dim in patch_shape]
    patch_slices = \
        [(slice(None),) + tuple(slice(cdim - s[0], cdim + s[1]) for cdim, s in zip(c, span)) for c in center]
    return patch_slices if len(patch_slices) > 1 else patch_slices[0]




def sample_centers_balanced(label_img, patch_shape, num_centers, add_rand_offset=False, exclude=None):
    """Samples centers for patch extraction from the given volume. An equal number of centers is sampled from each label.

    :param label_img: label image with dimensions (X, Y, Z) containing the label
    :param tuple patch_shape: tuple (x,y,z) shape of the patches to be extracted on returned centers
    :param int num_centers:
    :param bool add_rand_offset: if True, adds a random offset of up to half the patch size to sampled centers.
    :param list exclude: list with label ids to exclude from sampling
    :return List[tuple]: the sampled centers as a list of (x,y,z) tuples
    """

    assert len(label_img.shape) == len(patch_shape), 'len({}) ¿=? len({})'.format(label_img.shape, patch_shape)

    label_ids = np.unique(label_img).tolist()
    if exclude is not None:
        label_ids = [i for i in label_ids if i not in exclude]

    centers_labels = {label_id: np.argwhere(label_img == label_id) for label_id in label_ids}

    # Resample (repeating or removing) to appropiate number
    centers_labels = \
        {k: resample_list(v, num_centers / len(label_ids)) for k, v in centers_labels.items()}

    # Add random offset of up to half the patch size
    if add_rand_offset:
        for label_centers in centers_labels.values():
            np.random.seed(0) # Repeatability
            label_centers += np.expand_dims(np.divide(patch_shape, 2).astype(int), axis=0) * \
                             (2.0 * np.random.rand(len(label_centers), len(label_centers[0])) - 1.0)

    # Clip so not out of bounds
    for k in centers_labels.keys():
        centers_labels[k] = np.clip(centers_labels[k],
            a_min=np.ceil(np.divide(patch_shape, 2.0)).astype(int),
            a_max=label_img.shape - np.floor(np.divide(patch_shape, 2.0).astype(int))).astype(int)

    # Join the centers of each label and return in appropiate format
    return [tuple(c) for c in np.concatenate(list(centers_labels.values()), axis=0)]


def sample_centers_uniform(vol, patch_shape, extraction_step, max_centers=None, mask=None):
    """
    This sampling is uniform, not regular! It will extract patches

    :param vol:
    :param patch_shape:
    :param extraction_step:
    :param max_centers: (Optional) If given, the centers will be resampled to max_len
    :param mask: (Optional) If given, discard centers not in foreground
    :return:
    """
    assert len(vol.shape) == len(patch_shape) == len(extraction_step), '{}, {}, {}'.format(vol.shape, patch_shape, extraction_step)
    if mask is not None:
        assert len(mask.shape) == len(vol.shape), '{}, {}'.format(mask.shape, vol.shape)
        mask = mask.astype('float16')

    # Get patch span from the center in each dimension
    span = [[int(np.ceil(dim / 2.0)), int(np.floor(dim / 2.0))] for dim in patch_shape]

    # Generate the sampling indexes for each dimension first and then get all their combinations (itertools.product)
    dim_indexes = [list(range(sp[0], vs - sp[1], step)) for sp, vs, step in zip(span, vol.shape, extraction_step)]
    centers = list(itertools.product(*dim_indexes))

    if mask is not None:
        centers = [c for c in centers if mask[c[0], c[1], c[2]] != 0.0]
    if max_centers is not None:
        centers = resample_list(centers, max_centers)

    return centers