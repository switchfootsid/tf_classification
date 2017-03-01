from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os

import tensorflow as tf
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import graph_util
from tensorflow.python.saved_model import builder as saved_model_builder
from tensorflow.python.saved_model import signature_constants
from tensorflow.python.saved_model import signature_def_utils
from tensorflow.python.saved_model import tag_constants
from tensorflow.python.saved_model import utils
from tensorflow.python.tools import optimize_for_inference_lib
slim = tf.contrib.slim

from config.parse_config import parse_config_file
from nets import nets_factory

def preprocess_image(image_buffer, input_height, input_width):
  """Preprocess JPEG encoded bytes to 3D float Tensor."""

  # Decode the string as an RGB JPEG.
  image = tf.image.decode_jpeg(image_buffer, channels=3)
  image = tf.image.convert_image_dtype(image, dtype=tf.float32)
  # Resize the image to the original height and width.
  image = tf.expand_dims(image, 0)
  image = tf.image.resize_bilinear(image,
                                   [input_height, input_width],
                                   align_corners=False)
  image = tf.squeeze(image, [0])
  # Finally, rescale to [-1,1] instead of [0, 1)
  image = tf.subtract(image, 0.5)
  image = tf.multiply(image, 2.0)
  return image

def export(checkpoint_path, export_dir, export_version, export_for_serving, cfg):

    graph = tf.Graph()

    with graph.as_default():

        global_step = slim.get_or_create_global_step()
        input_size = cfg.IMAGE_PROCESSING.INPUT_SIZE
        image_data = tf.placeholder(tf.float32, [None, input_size * input_size * 3], name="images")
        images = tf.reshape(image_data, [-1, input_size, input_size, 3])

        arg_scope = nets_factory.arg_scopes_map[cfg.MODEL_NAME]()

        with slim.arg_scope(arg_scope):
            logits, end_points = nets_factory.networks_map[cfg.MODEL_NAME](
                inputs=images,
                num_classes=cfg.NUM_CLASSES,
                is_training=False
            )
            class_scores, predicted_classes = tf.nn.top_k(end_points['Predictions'], k=cfg.NUM_CLASSES)

        if 'MOVING_AVERAGE_DECAY' in cfg and cfg.MOVING_AVERAGE_DECAY > 0:
            variable_averages = tf.train.ExponentialMovingAverage(
                cfg.MOVING_AVERAGE_DECAY, global_step)
            variables_to_restore = variable_averages.variables_to_restore(
                slim.get_model_variables())
        else:
            variables_to_restore = slim.get_variables_to_restore()

        saver = tf.train.Saver(variables_to_restore, reshape=True)

        if os.path.isdir(checkpoint_path):
            checkpoint_dir = checkpoint_path
            checkpoint_path = tf.train.latest_checkpoint(checkpoint_dir)

            if checkpoint_path is None:
                raise ValueError("Unable to find a model checkpoint in the " \
                                 "directory %s" % (checkpoint_dir,))

        tf.logging.info('Exporting model: %s' % checkpoint_path)

        sess_config = tf.ConfigProto(
                log_device_placement=cfg.SESSION_CONFIG.LOG_DEVICE_PLACEMENT,
                allow_soft_placement = True,
                gpu_options = tf.GPUOptions(
                    per_process_gpu_memory_fraction=cfg.SESSION_CONFIG.PER_PROCESS_GPU_MEMORY_FRACTION
                )
            )
        sess = tf.Session(graph=graph, config=sess_config)

        with sess.as_default():

            tf.global_variables_initializer().run()

            saver.restore(sess, checkpoint_path)

            input_graph_def = graph.as_graph_def()
            input_node_names= ["images"]
            output_node_names = [end_points['Predictions'].name[:-2]]

            constant_graph_def = graph_util.convert_variables_to_constants(
                sess=sess,
                input_graph_def=input_graph_def,
                output_node_names=output_node_names,
                variable_names_whitelist=None,
                variable_names_blacklist=None)

            optimized_graph_def = optimize_for_inference_lib.optimize_for_inference(
                input_graph_def=constant_graph_def,
                input_node_names=input_node_names,
                output_node_names=output_node_names,
                placeholder_type_enum=dtypes.float32.as_datatype_enum)

            if export_for_serving:

                classification_signature = signature_def_utils.build_signature_def(
                    inputs={signature_constants.CLASSIFY_INPUTS: image_data},
                    outputs={
                        signature_constants.CLASSIFY_OUTPUT_CLASSES:
                            classification_outputs_classes,
                        signature_constants.CLASSIFY_OUTPUT_SCORES:
                            classification_outputs_scores
                    },
                    method_name=signature_constants.CLASSIFY_METHOD_NAME
                )

                tensor_info_x = utils.build_tensor_info(x)
                tensor_info_y = utils.build_tensor_info(y)

                prediction_signature = signature_def_utils.build_signature_def(
                    inputs={'images': tensor_info_x},
                    outputs={'scores': tensor_info_y},
                    method_name=signature_constants.PREDICT_METHOD_NAME
                )

                legacy_init_op = tf.group(tf.initialize_all_tables(), name='legacy_init_op')
                builder.add_meta_graph_and_variables(
                    sess, [tag_constants.SERVING],
                    signature_def_map={
                      'predict_images':
                          prediction_signature,
                      signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY:
                          classification_signature,
                    },
                    legacy_init_op=legacy_init_op
                )

                builder.save()

                export_saver = tf.train.Saver(sharded=True)
                model_exporter = exporter.Exporter(export_saver)
                signature = exporter.classification_signature(input_tensor=image_data, scores_tensor=class_scores, classes_tensor=predicted_classes)
                model_exporter.init(optimized_graph_def,
                                  default_graph_signature=signature)
                model_exporter.export(export_dir, tf.constant(export_version), sess)

            else:
                if not os.path.exists(export_dir):
                    os.makedirs(export_dir)
                save_path = os.path.join(export_dir, 'optimized_model-%d.pb' % (export_version,))
                with open(save_path, 'w') as f:
                    f.write(optimized_graph_def.SerializeToString())

def parse_args():

    parser = argparse.ArgumentParser(description='Test an Inception V3 network')

    parser.add_argument('--checkpoint_path', dest='checkpoint_path',
                          help='Path to the specific model you want to export.',
                          required=True, type=str)

    parser.add_argument('--export_dir', dest='export_dir',
                          help='Path to a directory where the exported model will be saved.',
                          required=True, type=str)

    parser.add_argument('--export_version', dest='export_version',
                        help='Version number of the model.',
                        required=True, type=int)

    parser.add_argument('--config', dest='config_file',
                        help='Path to the configuration file',
                        required=True, type=str)

    parser.add_argument('--serving', dest='serving',
                        help='Export for TensorFlow Serving usage. Otherwise, a constant graph will be generated.',
                        action='store_true', default=False)


    args = parser.parse_args()

    return args

if __name__ == '__main__':

    args = parse_args()
    cfg = parse_config_file(args.config_file)

    export(args.checkpoint_path, args.export_dir, args.export_version, args.serving, cfg=cfg)