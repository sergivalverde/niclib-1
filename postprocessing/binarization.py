import copy
import itertools
import numpy as np
from scipy import ndimage

from niclib.metrics import compute_segmentation_metrics, compute_avg_std_metrics_list
from niclib.volume.binarization import ThreshSizeBinarizer

from niclib.io.terminal import printProgressBar

def thresh_size_search_inefficient(result_set, images, thresholds, lesion_sizes, compute_lesion_metrics=False):
    true_vols = [img.labels[0] for img in images]
    prob_vols = [result_set[img.id] for img in images]

    # Generate result filename and try to load_samples results
    metrics_list = list()
    metrics_names = list()

    for n, (thresh, lesion_size) in enumerate(itertools.product(thresholds, lesion_sizes)):
        printProgressBar(n, len(thresholds)*len(lesion_sizes), suffix=" parameters evaluated")

        metrics_iter = list()
        for lesion_probs, true_vol in zip(prob_vols, true_vols):
            rec_vol = ThreshSizeBinarizer(thresh, lesion_size).binarize(lesion_probs)
            metrics_iter.append(
                compute_segmentation_metrics(true_vol, rec_vol, lesion_metrics=compute_lesion_metrics))

        m_avg_std = compute_avg_std_metrics_list(metrics_iter)

        metrics_list.append(m_avg_std)
        metrics_names.append("th={}_ls={}".format(thresh, lesion_size))

    printProgressBar(len(thresholds)*len(lesion_sizes), len(thresholds)*len(lesion_sizes), suffix=" parameters evaluated")
    return metrics_list, metrics_names


def thresh_size_search(result_set, images, thresholds, lesion_sizes, compute_lesion_metrics=False):
    # Preallocate empty list to store the samples metrics, for each th and ls combination
    metrics_iter = {}
    for thresh in thresholds:
        for min_lesion_size in lesion_sizes:
            metrics_iter["th={}_ls={}".format(thresh, min_lesion_size)] = []

    # Compute and store the results for each sample, thresh and min_lesion_size combination
    print("Evaluating threshold and lesion size for binarization")
    for sample_num, sample in enumerate(images):
        printProgressBar(sample_num, len(result_set), suffix=" samples processed")
        if sample.id not in result_set:
            continue

        true_vol = sample.labels[0]
        lesion_probs = result_set[sample.id]

        for thresh in thresholds:
            y_prob = lesion_probs > thresh

            # Get connected components information
            y_prob_labelled, nlesions = ndimage.label(y_prob)

            label_list = np.arange(1, nlesions + 1)
            lesion_volumes = [0.0]
            if nlesions > 0:
                lesion_volumes = ndimage.labeled_comprehension(y_prob, y_prob_labelled, label_list, np.sum, float, 0)

            for min_lesion_size in lesion_sizes:
                if nlesions > 0:
                    # Set to 0 invalid lesions
                    lesions_to_ignore = [idx + 1 for idx, lesion_vol in enumerate(lesion_volumes) if
                                         lesion_vol < min_lesion_size]

                    rec_vol = copy.deepcopy(y_prob_labelled)
                    rec_vol[np.isin(y_prob_labelled, lesions_to_ignore)] = 0.0
                else:
                    rec_vol = np.zeros_like(y_prob_labelled)

                metrics_iter["th={}_ls={}".format(thresh, min_lesion_size)].append(
                    compute_segmentation_metrics(true_vol, rec_vol, lesion_metrics=compute_lesion_metrics))
    printProgressBar(len(result_set), len(result_set), suffix=" samples processed")

    # Compute avg_std for each metric th and ls combination
    metrics_list, metrics_names = list(), list()
    for thresh in thresholds:
        for min_lesion_size in lesion_sizes:
            metrics_name = "th={}_ls={}".format(thresh, min_lesion_size)
            metrics_names.append(metrics_name)

            metrics_avg_std = compute_avg_std_metrics_list(metrics_iter[metrics_name])
            metrics_list.append(metrics_avg_std)

    return metrics_list, metrics_names