# coding=utf-8
# Copyright 2018 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""SV2P: Stochastic Variational Video Prediction.

   based on the following paper:
   https://arxiv.org/abs/1710.11252
   by Mohammad Babaeizadeh, Chelsea Finn, Dumitru Erhan,
      Roy H. Campbell and Sergey Levine
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial

from tensor2tensor.layers import common_layers
from tensor2tensor.layers import common_video
from tensor2tensor.models.research import next_frame_basic_stochastic
from tensor2tensor.models.research import next_frame_sv2p_params  # pylint: disable=unused-import
from tensor2tensor.utils import registry
import tensorflow as tf

tfl = tf.layers
tfcl = tf.contrib.layers


@registry.register_model
class NextFrameSv2p(next_frame_basic_stochastic.NextFrameBasicStochastic):
  """Stochastic Variational Video Prediction."""

  def tinyify(self, array):
    return common_video.tinyify(array, self.hparams.tiny_mode)

  def visualize_predictions(self, real_frames, gen_frames):
    def concat_on_y_axis(x):
      x = tf.unstack(x, axis=1)
      x = tf.concat(x, axis=1)
      return x

    frames_gd = common_video.swap_time_and_batch_axes(real_frames)
    frames_pd = common_video.swap_time_and_batch_axes(gen_frames)
    frames_gd = concat_on_y_axis(frames_gd)
    frames_pd = concat_on_y_axis(frames_pd)
    side_by_side_video = tf.concat([frames_gd, frames_pd], axis=2)
    tf.summary.image("full_video", side_by_side_video)

  def get_scheduled_sample_func(self, batch_size):
    """Creates a function for scheduled sampling based on given hparams."""
    with tf.variable_scope("scheduled_sampling_func", reuse=False):
      iter_num = self.get_iteration_num()
      if self.hparams.scheduled_sampling_mode == "prob":
        decay_steps = self.hparams.scheduled_sampling_decay_steps
        probability = tf.train.polynomial_decay(
            1.0, iter_num, decay_steps, 0.0)
        scheduled_sampling_func = common_video.scheduled_sample_prob
        scheduled_sampling_func_var = probability
      else:
        # Calculate number of ground-truth frames to pass in.
        k = self.hparams.scheduled_sampling_k
        num_ground_truth = tf.to_int32(
            tf.round(
                tf.to_float(batch_size) *
                (k / (k + tf.exp(tf.to_float(iter_num) / tf.to_float(k))))))
        scheduled_sampling_func = common_video.scheduled_sample_count
        scheduled_sampling_func_var = num_ground_truth

      tf.summary.scalar("scheduled_sampling_var", scheduled_sampling_func_var)
      partial_func = partial(scheduled_sampling_func,
                             batch_size=batch_size,
                             scheduled_sample_var=scheduled_sampling_func_var)
      return partial_func

  def get_scheduled_sample_inputs(self,
                                  done_warm_start,
                                  groundtruth_items,
                                  generated_items,
                                  scheduled_sampling_func):
    """Scheduled sampling.

    Args:
      done_warm_start: whether we are done with warm start or not.
      groundtruth_items: list of ground truth items.
      generated_items: list of generated items.
      scheduled_sampling_func: scheduled sampling function to choose between
        groundtruth items and generated items.

    Returns:
      A mix list of ground truth and generated items.
    """
    def sample():
      """Calculate the scheduled sampling params based on iteration number."""
      with tf.variable_scope("scheduled_sampling", reuse=tf.AUTO_REUSE):
        output_items = []
        for item_gt, item_gen in zip(groundtruth_items, generated_items):
          output_items.append(scheduled_sampling_func(item_gt, item_gen))
        return output_items

    cases = [
        (tf.logical_not(done_warm_start), lambda: groundtruth_items),
        (tf.logical_not(self.is_training), lambda: generated_items),
    ]
    output_items = tf.case(cases, default=sample, strict=True)

    return output_items

  def get_input_if_exists(self, features, key, batch_size, num_frames):
    if key in features:
      x = features[key]
    else:
      x = tf.zeros((batch_size, num_frames, 1, self.hparams.hidden_size))
    return common_video.swap_time_and_batch_axes(x)

  def inject_additional_input(self, layer, inputs, scope, concatenate=True):
    layer_shape = common_layers.shape_list(layer)
    input_shape = common_layers.shape_list(inputs)
    if concatenate:
      emb = common_video.encode_to_shape(inputs, layer_shape, scope)
      layer = tf.concat(values=[layer, emb], axis=-1)
    else:
      filters = layer_shape[-1]
      input_reshaped = tf.reshape(inputs, [-1, 1, 1, input_shape[-1]])
      input_mask = tf.layers.dense(input_reshaped, filters, name=scope)
      zeros_mask = tf.zeros(layer_shape, dtype=tf.float32)
      input_broad = input_mask + zeros_mask
      layer *= input_broad
    return layer

  def bottom_part_tower(self, input_image, input_reward, action, latent,
                        lstm_state, lstm_size, conv_size, concat_latent=False):
    """The bottom part of predictive towers.

    With the current (early) design, the main prediction tower and
    the reward prediction tower share the same arcitecture. TF Scope can be
    adjusted as required to either share or not share the weights between
    the two towers.

    Args:
      input_image: the current image.
      input_reward: the current reward.
      action: the action taken by the agent.
      latent: the latent vector.
      lstm_state: the current internal states of conv lstms.
      lstm_size: the size of lstms.
      conv_size: the size of convolutions.
      concat_latent: whether or not to concatenate the latent at every step.

    Returns:
      - the output of the partial network.
      - intermidate outputs for skip connections.
    """
    lstm_func = common_video.conv_lstm_2d
    tile_and_concat = common_video.tile_and_concat

    input_image = common_layers.make_even_size(input_image)
    concat_input_image = tile_and_concat(
        input_image, latent, concat_latent=concat_latent)

    enc0 = tfl.conv2d(
        concat_input_image,
        conv_size[0], [5, 5],
        strides=(2, 2),
        activation=tf.nn.relu,
        padding="SAME",
        name="scale1_conv1")
    enc0 = tfcl.layer_norm(enc0, scope="layer_norm1")

    hidden1, lstm_state[0] = lstm_func(
        enc0, lstm_state[0], lstm_size[0], name="state1")
    hidden1 = tile_and_concat(hidden1, latent, concat_latent=concat_latent)
    hidden1 = tfcl.layer_norm(hidden1, scope="layer_norm2")
    hidden2, lstm_state[1] = lstm_func(
        hidden1, lstm_state[1], lstm_size[1], name="state2")
    hidden2 = tfcl.layer_norm(hidden2, scope="layer_norm3")
    hidden2 = common_layers.make_even_size(hidden2)
    enc1 = tfl.conv2d(hidden2, hidden2.get_shape()[3], [3, 3], strides=(2, 2),
                      padding="SAME", activation=tf.nn.relu, name="conv2")
    enc1 = tile_and_concat(enc1, latent, concat_latent=concat_latent)

    hidden3, lstm_state[2] = lstm_func(
        enc1, lstm_state[2], lstm_size[2], name="state3")
    hidden3 = tile_and_concat(hidden3, latent, concat_latent=concat_latent)
    hidden3 = tfcl.layer_norm(hidden3, scope="layer_norm4")
    hidden4, lstm_state[3] = lstm_func(
        hidden3, lstm_state[3], lstm_size[3], name="state4")
    hidden4 = tile_and_concat(hidden4, latent, concat_latent=concat_latent)
    hidden4 = tfcl.layer_norm(hidden4, scope="layer_norm5")
    hidden4 = common_layers.make_even_size(hidden4)
    enc2 = tfl.conv2d(hidden4, hidden4.get_shape()[3], [3, 3], strides=(2, 2),
                      padding="SAME", activation=tf.nn.relu, name="conv3")

    if action is not None:
      enc2 = self.inject_additional_input(
          enc2, action, "action_enc", self.hparams.concatenate_actions)
    if input_reward is not None:
      enc2 = self.inject_additional_input(enc2, input_reward, "reward_enc")
    if latent is not None and not concat_latent:
      with tf.control_dependencies([latent]):
        enc2 = tf.concat([enc2, latent], axis=3)

    enc3 = tfl.conv2d(enc2, hidden4.get_shape()[3], [1, 1], strides=(1, 1),
                      padding="SAME", activation=tf.nn.relu, name="conv4")

    hidden5, lstm_state[4] = lstm_func(
        enc3, lstm_state[4], lstm_size[4], name="state5")  # last 8x8
    hidden5 = tfcl.layer_norm(hidden5, scope="layer_norm6")
    hidden5 = tile_and_concat(hidden5, latent, concat_latent=concat_latent)
    return hidden5, (enc0, enc1)

  def reward_prediction(self, input_images, input_reward, action, latent):
    """Builds a reward prediction network."""
    conv_size = self.tinyify([32, 32, 16, 8])

    with tf.variable_scope("reward_pred", reuse=tf.AUTO_REUSE):
      x = tf.concat(input_images, axis=3)
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="reward_bn0")
      x = tfl.conv2d(x, conv_size[1], [3, 3], strides=(2, 2),
                     activation=tf.nn.relu, name="reward_conv1")
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="reward_bn1")

      # Inject additional inputs
      if action is not None:
        x = self.inject_additional_input(
            x, action, "action_enc", self.hparams.concatenate_actions)
      if input_reward is not None:
        x = self.inject_additional_input(x, input_reward, "reward_enc")
      if latent is not None:
        latent = tfl.flatten(latent)
        latent = tf.expand_dims(latent, axis=1)
        latent = tf.expand_dims(latent, axis=1)
        x = self.inject_additional_input(x, latent, "latent_enc")

      x = tfl.conv2d(x, conv_size[2], [3, 3], strides=(2, 2),
                     activation=tf.nn.relu, name="reward_conv2")
      x = tfcl.batch_norm(x, updates_collections=None,
                          is_training=self.is_training, scope="reward_bn2")
      x = tfl.conv2d(x, conv_size[3], [3, 3], strides=(2, 2),
                     activation=tf.nn.relu, name="reward_conv3")
      return x

  def construct_predictive_tower(
      self, input_image, input_reward, action, lstm_state, latent,
      concat_latent=False):
    # Main tower
    lstm_func = common_video.conv_lstm_2d
    frame_shape = common_layers.shape_list(input_image)
    batch_size, img_height, img_width, color_channels = frame_shape
    # the number of different pixel motion predictions
    # and the number of masks for each of those predictions
    num_masks = self.hparams.num_masks
    upsample_method = self.hparams.upsample_method
    tile_and_concat = common_video.tile_and_concat

    lstm_size = self.tinyify([32, 32, 64, 64, 128, 64, 32])
    conv_size = self.tinyify([32])

    with tf.variable_scope("main", reuse=tf.AUTO_REUSE):
      hidden5, skips = self.bottom_part_tower(
          input_image, input_reward, action, latent,
          lstm_state, lstm_size, conv_size, concat_latent=concat_latent)
      enc0, enc1 = skips

      with tf.variable_scope("upsample1", reuse=tf.AUTO_REUSE):
        enc4 = common_layers.cyclegan_upsample(
            hidden5, num_outputs=hidden5.shape.as_list()[-1],
            stride=[2, 2], method=upsample_method)

      enc1_shape = common_layers.shape_list(enc1)
      enc4 = enc4[:, :enc1_shape[1], :enc1_shape[2], :]  # Cut to shape.
      enc4 = tile_and_concat(enc4, latent, concat_latent=concat_latent)

      hidden6, lstm_state[5] = lstm_func(
          enc4, lstm_state[5], lstm_size[5], name="state6",
          spatial_dims=enc1_shape[1:-1])  # 16x16
      hidden6 = tile_and_concat(hidden6, latent, concat_latent=concat_latent)
      hidden6 = tfcl.layer_norm(hidden6, scope="layer_norm7")
      # Skip connection.
      hidden6 = tf.concat(axis=3, values=[hidden6, enc1])  # both 16x16

      with tf.variable_scope("upsample2", reuse=tf.AUTO_REUSE):
        enc5 = common_layers.cyclegan_upsample(
            hidden6, num_outputs=hidden6.shape.as_list()[-1],
            stride=[2, 2], method=upsample_method)

      enc0_shape = common_layers.shape_list(enc0)
      enc5 = enc5[:, :enc0_shape[1], :enc0_shape[2], :]  # Cut to shape.
      enc5 = tile_and_concat(enc5, latent, concat_latent=concat_latent)

      hidden7, lstm_state[6] = lstm_func(
          enc5, lstm_state[6], lstm_size[6], name="state7",
          spatial_dims=enc0_shape[1:-1])  # 32x32
      hidden7 = tfcl.layer_norm(hidden7, scope="layer_norm8")

      # Skip connection.
      hidden7 = tf.concat(axis=3, values=[hidden7, enc0])  # both 32x32

      with tf.variable_scope("upsample3", reuse=tf.AUTO_REUSE):
        enc6 = common_layers.cyclegan_upsample(
            hidden7, num_outputs=hidden7.shape.as_list()[-1],
            stride=[2, 2], method=upsample_method)
      enc6 = tfcl.layer_norm(enc6, scope="layer_norm9")
      enc6 = tile_and_concat(enc6, latent, concat_latent=concat_latent)

      if self.hparams.model_options == "DNA":
        # Using largest hidden state for predicting untied conv kernels.
        enc7 = tfl.conv2d_transpose(
            enc6,
            self.hparams.dna_kernel_size**2,
            [1, 1],
            strides=(1, 1),
            padding="SAME",
            name="convt4",
            activation=None)
      else:
        # Using largest hidden state for predicting a new image layer.
        enc7 = tfl.conv2d_transpose(
            enc6,
            color_channels,
            [1, 1],
            strides=(1, 1),
            padding="SAME",
            name="convt4",
            activation=None)
        # This allows the network to also generate one image from scratch,
        # which is useful when regions of the image become unoccluded.
        transformed = [tf.nn.sigmoid(enc7)]

      if self.hparams.model_options == "CDNA":
        # cdna_input = tf.reshape(hidden5, [int(batch_size), -1])
        cdna_input = tfcl.flatten(hidden5)
        transformed += common_video.cdna_transformation(
            input_image, cdna_input, num_masks, int(color_channels),
            self.hparams.dna_kernel_size, self.hparams.relu_shift)
      elif self.hparams.model_options == "DNA":
        # Only one mask is supported (more should be unnecessary).
        if num_masks != 1:
          raise ValueError("Only one mask is supported for DNA model.")
        transformed = [
            common_video.dna_transformation(
                input_image, enc7,
                self.hparams.dna_kernel_size, self.hparams.relu_shift)]

      masks = tfl.conv2d(
          enc6, filters=num_masks + 1, kernel_size=[1, 1],
          strides=(1, 1), name="convt7", padding="SAME")
      masks = tf.reshape(
          tf.nn.softmax(tf.reshape(masks, [-1, num_masks + 1])),
          [batch_size,
           int(img_height),
           int(img_width), num_masks + 1])
      mask_list = tf.split(
          axis=3, num_or_size_splits=num_masks + 1, value=masks)
      output = mask_list[0] * input_image
      for layer, mask in zip(transformed, mask_list[1:]):
        output += layer * mask

      return output, lstm_state

  def construct_model(self,
                      images,
                      actions,
                      rewards):
    """Build convolutional lstm video predictor using CDNA, or DNA.

    Args:
      images: list of tensors of ground truth image sequences
              there should be a 4D image ?xWxHxC for each timestep
      actions: list of action tensors
               each action should be in the shape ?x1xZ
      rewards: list of reward tensors
               each reward should be in the shape ?x1xZ
    Returns:
      gen_images: predicted future image frames
      gen_rewards: predicted future rewards
      latent_mean: mean of approximated posterior
      latent_std: std of approximated posterior

    Raises:
      ValueError: if more than 1 mask specified for DNA model.
    """
    context_frames = self.hparams.video_num_input_frames
    buffer_size = self.hparams.reward_prediction_buffer_size
    if buffer_size == 0:
      buffer_size = context_frames
    if buffer_size > context_frames:
      raise ValueError("Buffer size is bigger than context frames %d %d." %
                       (buffer_size, context_frames))

    batch_size = common_layers.shape_list(images)[1]
    ss_func = self.get_scheduled_sample_func(batch_size)

    def process_single_frame(prev_outputs, inputs):
      """Process a single frame of the video."""
      cur_image, input_reward, action = inputs
      time_step, prev_image, prev_reward, frame_buf, lstm_states = prev_outputs

      generated_items = [prev_image]
      groundtruth_items = [cur_image]
      done_warm_start = tf.greater(time_step, context_frames - 1)
      input_image, = self.get_scheduled_sample_inputs(
          done_warm_start, groundtruth_items, generated_items, ss_func)

      # Prediction
      pred_image, lstm_states = self.construct_predictive_tower(
          input_image, None, action, lstm_states, latent)

      if self.hparams.reward_prediction:
        reward_input_image = pred_image
        if self.hparams.reward_prediction_stop_gradient:
          reward_input_image = tf.stop_gradient(reward_input_image)
        with tf.control_dependencies([time_step]):
          frame_buf = [reward_input_image] + frame_buf[:-1]
        pred_reward = self.reward_prediction(frame_buf, None, action, latent)
        pred_reward = common_video.decode_to_shape(
            pred_reward, common_layers.shape_list(input_reward), "reward_dec")
      else:
        pred_reward = prev_reward

      time_step += 1
      outputs = (time_step, pred_image, pred_reward, frame_buf, lstm_states)

      return outputs

    # Latent tower
    latent = None
    if self.hparams.stochastic_model:
      latent_mean, latent_std = self.construct_latent_tower(images, time_axis=0)
      latent = common_video.get_gaussian_tensor(latent_mean, latent_std)

    # HACK: Do first step outside to initialize all the variables
    lstm_states = [None] * 7
    frame_buffer = [tf.zeros_like(images[0])] * buffer_size
    inputs = images[0], rewards[0], actions[0]
    prev_outputs = (tf.constant(0),
                    tf.zeros_like(images[0]),
                    tf.zeros_like(rewards[0]),
                    frame_buffer,
                    lstm_states)

    initializers = process_single_frame(prev_outputs, inputs)
    first_gen_images = tf.expand_dims(initializers[1], axis=0)
    first_gen_rewards = tf.expand_dims(initializers[2], axis=0)

    inputs = (images[1:-1], rewards[1:-1], actions[1:-1])

    outputs = tf.scan(process_single_frame, inputs, initializers)
    gen_images, gen_rewards = outputs[1:3]

    gen_images = tf.concat((first_gen_images, gen_images), axis=0)
    gen_rewards = tf.concat((first_gen_rewards, gen_rewards), axis=0)

    return gen_images, gen_rewards, [latent_mean], [latent_std]

  def get_extra_loss(self, latent_means=None, latent_stds=None,
                     true_frames=None, gen_frames=None, beta=1.0):
    """Losses in addition to the default modality losses."""
    del true_frames
    del gen_frames
    kl_loss = 0.0
    if self.is_training:
      for i, (mean, std) in enumerate(zip(latent_means, latent_stds)):
        kl_loss += common_layers.kl_divergence(mean, std)
        tf.summary.histogram("posterior_mean_%d" % i, mean)
        tf.summary.histogram("posterior_std_%d" % i, std)
      tf.summary.scalar("kl_raw", tf.reduce_mean(kl_loss))
    return beta * kl_loss

  def infer(self, features, *args, **kwargs):
    """Produce predictions from the model by running it."""
    del args, kwargs
    if "targets" not in features:
      if "infer_targets" in features:
        targets_shape = common_layers.shape_list(features["infer_targets"])
      elif "inputs" in features:
        targets_shape = common_layers.shape_list(features["inputs"])
        targets_shape[1] = self.hparams.video_num_target_frames
      else:
        raise ValueError("no inputs are given.")
      features["targets"] = tf.zeros(targets_shape, dtype=tf.float32)

    output, _ = self(features)  # pylint: disable=not-callable

    output["targets"] = tf.squeeze(output["targets"], axis=-1)
    output["target_reward"] = tf.argmax(output["target_reward"], axis=-1)

    # only required for decoding.
    output["outputs"] = output["targets"]
    output["scores"] = output["targets"]
    return output

  def body(self, features):
    hparams = self.hparams
    batch_size = common_layers.shape_list(features["inputs"])[0]

    # Swap time and batch axes.
    input_frames = common_video.swap_time_and_batch_axes(features["inputs"])
    target_frames = common_video.swap_time_and_batch_axes(features["targets"])

    # Get actions if exist otherwise use zeros
    input_actions = self.get_input_if_exists(
        features, "input_action", batch_size, hparams.video_num_input_frames)
    target_actions = self.get_input_if_exists(
        features, "target_action", batch_size, hparams.video_num_target_frames)

    # Get rewards if exist otherwise use zeros
    input_rewards = self.get_input_if_exists(
        features, "input_reward", batch_size, hparams.video_num_input_frames)
    target_rewards = self.get_input_if_exists(
        features, "target_reward", batch_size, hparams.video_num_target_frames)

    all_actions = tf.concat([input_actions, target_actions], axis=0)
    all_rewards = tf.concat([input_rewards, target_rewards], axis=0)
    all_frames = tf.concat([input_frames, target_frames], axis=0)

    # Each image is being used twice, in latent tower and main tower.
    # This is to make sure we are using the *same* image for both, ...
    # ... given how TF queues work.
    # NOT sure if this is required at all. Doesn"t hurt though! :)
    all_frames = tf.identity(all_frames)

    gen_images, gen_rewards, latent_means, latent_stds = self.construct_model(
        images=all_frames,
        actions=all_actions,
        rewards=all_rewards,
    )

    beta = self.get_beta()
    extra_loss = self.get_extra_loss(
        latent_means=latent_means,
        latent_stds=latent_stds, beta=beta, true_frames=all_frames,
        gen_frames=gen_images)

    # Visualize predictions in Tensorboard
    self.visualize_predictions(all_frames[1:], gen_images)

    # Ignore the predictions from the input frames.
    # This is NOT the same as original paper/implementation.
    predictions = gen_images[hparams.video_num_input_frames-1:]
    reward_pred = gen_rewards[hparams.video_num_input_frames-1:]
    if self.is_training:
      reward_pred = tf.squeeze(reward_pred, axis=2)  # Remove extra dimension.

    # Swap back time and batch axes.
    predictions = common_video.swap_time_and_batch_axes(predictions)
    reward_pred = common_video.swap_time_and_batch_axes(reward_pred)

    return_targets = predictions
    if hparams.reward_prediction:
      return_targets = {"targets": predictions, "target_reward": reward_pred}

    if hparams.internal_loss:
      loss = tf.losses.mean_squared_error(all_frames[1:], gen_images)
      extra_loss = {"training": loss + extra_loss}

    return return_targets, extra_loss


@registry.register_model
class NextFrameSv2pTwoFrames(NextFrameSv2p):
  """Stochastic next-frame model with 2 frames posterior."""

  def construct_model(self, images, actions, rewards):
    images = tf.unstack(images, axis=0)
    actions = tf.unstack(actions, axis=0)
    rewards = tf.unstack(rewards, axis=0)

    batch_size = common_layers.shape_list(images[0])[0]
    context_frames = self.hparams.video_num_input_frames

    # Predicted images and rewards.
    gen_rewards, gen_images, latent_means, latent_stds = [], [], [], []

    # LSTM states.
    lstm_state = [None] * 7

    # Create scheduled sampling function
    ss_func = self.get_scheduled_sample_func(batch_size)

    pred_image = tf.zeros_like(images[0])
    pred_reward = tf.zeros_like(rewards[0])
    latent = None
    for timestep, image, action, reward in zip(
        range(len(images)-1), images[:-1], actions[:-1], rewards[:-1]):
      # Scheduled Sampling
      done_warm_start = timestep > context_frames - 1
      groundtruth_items = [image, reward]
      generated_items = [pred_image, pred_reward]
      input_image, input_reward = self.get_scheduled_sample_inputs(
          done_warm_start, groundtruth_items, generated_items, ss_func)

      # Latent
      # TODO(mbz): should we use input_image iunstead of image?
      latent_images = tf.stack([image, images[timestep+1]], axis=0)
      latent_mean, latent_std = self.construct_latent_tower(
          latent_images, time_axis=0)
      latent = common_video.get_gaussian_tensor(latent_mean, latent_std)
      latent_means.append(latent_mean)
      latent_stds.append(latent_std)

      # Prediction
      pred_image, lstm_state = self.construct_predictive_tower(
          input_image, input_reward, action, lstm_state, latent)

      if self.hparams.reward_prediction:
        pred_reward = self.reward_prediction(
            pred_image, input_reward, action, latent)
        pred_reward = common_video.decode_to_shape(
            pred_reward, common_layers.shape_list(input_reward), "reward_dec")
      else:
        pred_reward = input_reward

      gen_images.append(pred_image)
      gen_rewards.append(pred_reward)

    gen_images = tf.stack(gen_images, axis=0)
    gen_rewards = tf.stack(gen_rewards, axis=0)

    return gen_images, gen_rewards, latent_means, latent_stds
