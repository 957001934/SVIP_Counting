import cv2
import os
import numpy as np






"""
get_counts(model, imgs,
           strides=[1, 2, 3, 4],
           batch_size=20,
           threshold=THRESHOLD,
           within_period_threshold=WITHIN_PERIOD_THRESHOLD,
           constant_speed=CONSTANT_SPEED,
           median_filter=MEDIAN_FILTER,
           fully_periodic=FULLY_PERIODIC)

"""


def get_counts(model, frames,
               strides,
               batch_size,
               threshold,
               within_period_threshold,
               constant_speed=False,
               median_filter=False,
               fully_periodic=False):
    """Pass frames through model and conver period predictions to count."""
    seq_len = len(frames)
    raw_scores_list = []
    scores = []
    within_period_scores_list = []

    if fully_periodic:
        within_period_threshold = 0.0

    frames = model.preprocess(frames)

    for stride in strides:
        num_batches = int(np.ceil(seq_len / model.num_frames / stride / batch_size))
        raw_scores_per_stride = []
        within_period_score_stride = []
        for batch_idx in range(num_batches):
            idxes = tf.range(batch_idx * batch_size * model.num_frames * stride,
                             (batch_idx + 1) * batch_size * model.num_frames * stride,
                             stride)
            idxes = tf.clip_by_value(idxes, 0, seq_len - 1)
            curr_frames = tf.gather(frames, idxes)
            curr_frames = tf.reshape(
                curr_frames,
                [batch_size, model.num_frames, model.image_size, model.image_size, 3])

            raw_scores, within_period_scores, _ = model(curr_frames)
            raw_scores_per_stride.append(np.reshape(raw_scores.numpy(),
                                                    [-1, model.num_frames // 2]))
            within_period_score_stride.append(np.reshape(within_period_scores.numpy(),
                                                         [-1, 1]))
        raw_scores_per_stride = np.concatenate(raw_scores_per_stride, axis=0)
        raw_scores_list.append(raw_scores_per_stride)
        within_period_score_stride = np.concatenate(
            within_period_score_stride, axis=0)
        pred_score, within_period_score_stride = get_score(
            raw_scores_per_stride, within_period_score_stride)
        scores.append(pred_score)
        within_period_scores_list.append(within_period_score_stride)

    # Stride chooser
    argmax_strides = np.argmax(scores)
    chosen_stride = strides[argmax_strides]
    raw_scores = np.repeat(
        raw_scores_list[argmax_strides], chosen_stride, axis=0)[:seq_len]
    within_period = np.repeat(
        within_period_scores_list[argmax_strides], chosen_stride,
        axis=0)[:seq_len]
    within_period_binary = np.asarray(within_period > within_period_threshold)
    if median_filter:
        within_period_binary = medfilt(within_period_binary, 5)

    # Select Periodic frames
    periodic_idxes = np.where(within_period_binary)[0]

    if constant_speed:
        # Count by averaging predictions. Smoother but
        # assumes constant speed.
        scores = tf.reduce_mean(
            tf.nn.softmax(raw_scores[periodic_idxes], axis=-1), axis=0)
        max_period = np.argmax(scores)
        pred_score = scores[max_period]
        pred_period = chosen_stride * (max_period + 1)
        per_frame_counts = (
                np.asarray(seq_len * [1. / pred_period]) *
                np.asarray(within_period_binary))
    else:
        # Count each frame. More noisy but adapts to changes in speed.
        pred_score = tf.reduce_mean(within_period)
        per_frame_periods = tf.argmax(raw_scores, axis=-1) + 1
        per_frame_counts = tf.where(
            tf.math.less(per_frame_periods, 3),
            0.0,
            tf.math.divide(1.0,
                           tf.cast(chosen_stride * per_frame_periods, tf.float32)),
        )
        if median_filter:
            per_frame_counts = medfilt(per_frame_counts, 5)

        per_frame_counts *= np.asarray(within_period_binary)

        pred_period = seq_len / np.sum(per_frame_counts)

    if pred_score < threshold:
        print('No repetitions detected in video as score '
              '%0.2f is less than threshold %0.2f.' % (pred_score, threshold))
        per_frame_counts = np.asarray(len(per_frame_counts) * [0.])

    return (pred_period, pred_score, within_period,
            per_frame_counts, chosen_stride)
