from __future__ import division
import os
import sys
import tensorflow as tf
import skimage.io as io
import numpy as np
import scipy
import cv2
import dlib
import csv
sys.path.append('/Users/yu-chieh/seg_models/models/slim/')
sys.path.append("/Users/yu-chieh/seg_models/tf-image-segmentation/")
from tf_image_segmentation.models.fcn_16s import FCN_16s
from tf_image_segmentation.utils.inference import adapt_network_for_any_size_input
from portrait_plus import BatchDatset, TestDataset
import TensorflowUtils_plus as utils
from scipy import misc

FLAGS = tf.flags.FLAGS
tf.flags.DEFINE_integer("batch_size", "5", "batch size for training")
tf.flags.DEFINE_string("logs_dir", "logs/", "path to logs directory")
tf.flags.DEFINE_string("data_dir", "Data_zoo/MIT_SceneParsing/", "path to dataset")
tf.flags.DEFINE_float("learning_rate", "1e-4", "Learning rate for Adam Optimizer")
tf.flags.DEFINE_string("model_dir", "Model_zoo/", "Path to vgg model mat")
tf.flags.DEFINE_bool('debug', "False", "Debug mode: True/ False")
tf.flags.DEFINE_string('mode', "train", "Mode train/ test/ visualize")

MODEL_URL = 'http://www.vlfeat.org/matconvnet/models/beta16/imagenet-vgg-verydeep-19.mat'

MAX_ITERATION = int(1e5 + 1)
NUM_OF_CLASSESS = 2
IMAGE_WIDTH = 600
IMAGE_HEIGHT = 800

slim = tf.contrib.slim
cpstandard = "/Users/yu-chieh/Downloads/fcn_16s_checkpoint/model_fcn16s_final.ckpt"


### SMART NETWORK
"""
    large portion derived from taken from https://github.com/PetroWu/AutoPortraitMatting/blob/master/FCN_plus.py
"""
def vgg_net(weights, image):
    layers = (
        'conv1_1', 'relu1_1', 'conv1_2', 'relu1_2', 'pool1',

        'conv2_1', 'relu2_1', 'conv2_2', 'relu2_2', 'pool2',

        'conv3_1', 'relu3_1', 'conv3_2', 'relu3_2', 'conv3_3',
        'relu3_3', 'conv3_4', 'relu3_4', 'pool3',

        'conv4_1', 'relu4_1', 'conv4_2', 'relu4_2', 'conv4_3',
        'relu4_3', 'conv4_4', 'relu4_4', 'pool4',

        'conv5_1', 'relu5_1', 'conv5_2', 'relu5_2', 'conv5_3',
        'relu5_3', 'conv5_4', 'relu5_4'
    )

    net = {}
    current = image
    for i, name in enumerate(layers):
        if name in ['conv3_4', 'relu3_4', 'conv4_4', 'relu4_4', 'conv5_4', 'relu5_4']:
            continue
        kind = name[:4]
        if kind == 'conv':
            kernels, bias = weights[i][0][0][0][0]
            # matconvnet: weights are [width, height, in_channels, out_channels]
            # tensorflow: weights are [height, width, in_channels, out_channels]
            kernels = utils.get_variable(np.transpose(kernels, (1, 0, 2, 3)), name=name + "_w")
            bias = utils.get_variable(bias.reshape(-1), name=name + "_b")
            current = utils.conv2d_basic(current, kernels, bias)
        elif kind == 'relu':
            current = tf.nn.relu(current, name=name)
            if FLAGS.debug:
                utils.add_activation_summary(current)
        elif kind == 'pool':
            current = utils.avg_pool_2x2(current)
        net[name] = current

    return net


def inference(image, keep_prob):
    """
    Semantic segmentation network definition
    :param image: input image. Should have values in range 0-255
    :param keep_prob:
    :return:
    """
    print("setting up vgg initialized conv layers ...")
    model_data = utils.get_model_data(FLAGS.model_dir, MODEL_URL)

    mean = model_data['normalization'][0][0][0]

    weights = np.squeeze(model_data['layers'])

    with tf.variable_scope("inference"):
        image_net = vgg_net(weights, image)
        conv_final_layer = image_net["conv5_3"]

        pool5 = utils.max_pool_2x2(conv_final_layer)

        W6 = utils.weight_variable([7, 7, 512, 4096], name="W6")
        b6 = utils.bias_variable([4096], name="b6")
        conv6 = utils.conv2d_basic(pool5, W6, b6)
        relu6 = tf.nn.relu(conv6, name="relu6")
        if FLAGS.debug:
            utils.add_activation_summary(relu6)
        relu_dropout6 = tf.nn.dropout(relu6, keep_prob=keep_prob)

        W7 = utils.weight_variable([1, 1, 4096, 4096], name="W7")
        b7 = utils.bias_variable([4096], name="b7")
        conv7 = utils.conv2d_basic(relu_dropout6, W7, b7)
        relu7 = tf.nn.relu(conv7, name="relu7")
        if FLAGS.debug:
            utils.add_activation_summary(relu7)
        relu_dropout7 = tf.nn.dropout(relu7, keep_prob=keep_prob)

        W8 = utils.weight_variable([1, 1, 4096, NUM_OF_CLASSESS], name="W8")
        b8 = utils.bias_variable([NUM_OF_CLASSESS], name="b8")
        conv8 = utils.conv2d_basic(relu_dropout7, W8, b8)
        # annotation_pred1 = tf.argmax(conv8, dimension=3, name="prediction1")

        # now to upscale to actual image size
        deconv_shape1 = image_net["pool4"].get_shape()
        W_t1 = utils.weight_variable([4, 4, deconv_shape1[3].value, NUM_OF_CLASSESS], name="W_t1")
        b_t1 = utils.bias_variable([deconv_shape1[3].value], name="b_t1")
        conv_t1 = utils.conv2d_transpose_strided(conv8, W_t1, b_t1, output_shape=tf.shape(image_net["pool4"]))
        fuse_1 = tf.add(conv_t1, image_net["pool4"], name="fuse_1")

        deconv_shape2 = image_net["pool3"].get_shape()
        W_t2 = utils.weight_variable([4, 4, deconv_shape2[3].value, deconv_shape1[3].value], name="W_t2")
        b_t2 = utils.bias_variable([deconv_shape2[3].value], name="b_t2")
        conv_t2 = utils.conv2d_transpose_strided(fuse_1, W_t2, b_t2, output_shape=tf.shape(image_net["pool3"]))
        fuse_2 = tf.add(conv_t2, image_net["pool3"], name="fuse_2")

        shape = tf.shape(image)
        deconv_shape3 = tf.stack([shape[0], shape[1], shape[2], NUM_OF_CLASSESS])
        W_t3 = utils.weight_variable([16, 16, NUM_OF_CLASSESS, deconv_shape2[3].value], name="W_t3")
        b_t3 = utils.bias_variable([NUM_OF_CLASSESS], name="b_t3")
        conv_t3 = utils.conv2d_transpose_strided(fuse_2, W_t3, b_t3, output_shape=deconv_shape3, stride=8)

        annotation_pred = tf.argmax(conv_t3, dimension=3, name="prediction")

    return tf.expand_dims(annotation_pred, dim=3), conv_t3

def record_train_val_data(train_lst, test_lst):
    with open('train.csv', 'a') as fp:
        writer = csv.writer(fp, delimiter=',')
        writer.writerows(train_lst)
    with open('test.csv', 'a') as fp:
        writer = csv.writer(fp, delimiter=',')
        writer.writerows(test_lst)


def train(loss_val, var_list):
    optimizer = tf.train.AdamOptimizer(FLAGS.learning_rate)
    grads = optimizer.compute_gradients(loss_val, var_list=var_list)
    if FLAGS.debug:
        # print(len(var_list))
        for grad, var in grads:
            utils.add_gradient_summary(grad, var)
    return optimizer.apply_gradients(grads)

def main(argv=None):
    train_errors = []
    val_errors = []
    keep_probability = tf.placeholder(tf.float32, name="keep_probabilty")
    image = tf.placeholder(tf.float32, shape=[None, IMAGE_HEIGHT, IMAGE_WIDTH, 6], name="input_image")
    annotation = tf.placeholder(tf.int32, shape=[None, IMAGE_HEIGHT, IMAGE_WIDTH, 1], name="annotation")

    pred_annotation, logits = inference(image, keep_probability)
    #tf.image_summary("input_image", image, max_images=2)
    #tf.image_summary("ground_truth", tf.cast(annotation, tf.uint8), max_images=2)
    #tf.image_summary("pred_annotation", tf.cast(pred_annotation, tf.uint8), max_images=2)
    loss = tf.reduce_mean((tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits,
                                                                          labels=tf.squeeze(annotation, squeeze_dims=[3]),
                                                                          name="entropy")))
    #tf.scalar_summary("entropy", loss)

    trainable_var = tf.trainable_variables()
    train_op = train(loss, trainable_var)

    #print("Setting up summary op...")
    #summary_op = tf.merge_all_summaries()

    '''
    print("Setting up image reader...")
    train_records, valid_records = scene_parsing.read_dataset(FLAGS.data_dir)
    print(len(train_records))
    print(len(valid_records))
    print("Setting up dataset reader")
    image_options = {'resize': True, 'resize_size': IMAGE_SIZE}
    if FLAGS.mode == 'train':
        train_dataset_reader = dataset.BatchDatset(train_records, image_options)
    validation_dataset_reader = dataset.BatchDatset(valid_records, image_options)
    '''
    train_dataset_reader = BatchDatset('data/trainlist.mat')

    sess = tf.Session()

    print("Setting up Saver...")
    saver = tf.train.Saver()
    #summary_writer = tf.train.SummaryWriter(FLAGS.logs_dir, sess.graph)

    sess.run(tf.initialize_all_variables())
    ckpt = tf.train.get_checkpoint_state(FLAGS.logs_dir)
    if ckpt and ckpt.model_checkpoint_path:
        saver.restore(sess, ckpt.model_checkpoint_path)
        print("Model restored...")

    #if FLAGS.mode == "train":
    itr = 0
    train_images, train_annotations = train_dataset_reader.next_batch()
    trloss = 0.0
    while len(train_annotations) > 0 or itr < 10000:
        print(itr)
        #train_images, train_annotations = train_dataset_reader.next_batch(FLAGS.batch_size)
        #print('==> batch data: ', train_images[0][100][100], '===', train_annotations[0][100][100])
        feed_dict = {image: train_images, annotation: train_annotations, keep_probability: 0.5}
        _, rloss =  sess.run([train_op, loss], feed_dict=feed_dict)
        trloss += rloss

        if itr % 1000 == 0:
            #train_loss, rpred = sess.run([loss, pred_annotation], feed_dict=feed_dict)
            print("Step: %d, Train_loss:%f" % (itr, trloss / 100))
            train_errors.append(trloss / 100)
            trloss = 0.0
            #summary_writer.add_summary(summary_str, itr)

        if itr % 1000 == 0 and itr > 0:
            valid_images, valid_annotations = validation_dataset_reader.next_batch(FLAGS.batch_size)
            valid_loss = sess.run(loss, feed_dict={image: valid_images, annotation: valid_annotations,
                                                           keep_probability: 1.0})
            val_errors.append(valid_loss/100)
            print("%s ---> Validation_loss: %g" % (datetime.datetime.now(), valid_loss))
        itr += 1

        train_images, train_annotations = train_dataset_reader.next_batch()
    saver.save(sess, FLAGS.logs_dir + "plus_model.ckpt", itr)
    record_train_val_data(train_errors, val_errors)

    '''elif FLAGS.mode == "visualize":
        valid_images, valid_annotations = validation_dataset_reader.get_random_batch(FLAGS.batch_size)
        pred = sess.run(pred_annotation, feed_dict={image: valid_images, annotation: valid_annotations,
                                                    keep_probability: 1.0})
        valid_annotations = np.squeeze(valid_annotations, axis=3)
        pred = np.squeeze(pred, axis=3)
        for itr in range(FLAGS.batch_size):
            utils.save_image(valid_images[itr].astype(np.uint8), FLAGS.logs_dir, name="inp_" + str(5+itr))
            utils.save_image(valid_annotations[itr].astype(np.uint8), FLAGS.logs_dir, name="gt_" + str(5+itr))
            utils.save_image(pred[itr].astype(np.uint8), FLAGS.logs_dir, name="pred_" + str(5+itr))
            print("Saved image: %d" % itr)'''

#### DUMB NETWORK
def get_all_images_for_fcn(num_images):
    # get num_images images form the path and put as a matrix
    imgs = []
    num = 0
    path = '/Users/yu-chieh/Downloads/images_data_crop/'
    for f in os.listdir(path):
        if num >= num_images:
            return np.array(imgs)
        image_path = os.path.join(path,f)
        image = scipy.ndimage.imread(image_path, mode='RGB')
        # cheating version
        # image = np.dstack((image, get_xy_mask(image)))
        imgs.append(image)
        num += 1
    return np.array(imgs)

def get_facial_points(image, num_points):
    predictor = dlib.shape_predictor('shape_predictor_68_face_landmarks.dat')
    detector = dlib.get_frontal_face_detector()
    dets = detector(image, 1)
    points = []
    for k, d in enumerate(dets):
        # Get the landmarks/parts for the face in box d.
        shape = predictor(image, d)
        for i in range(num_points):
            pt = shape.part(i)
            points.append([int(pt.x), int(pt.y)])
    return np.array(points)

def get_xy_mask(image):
    # bad version
    image_src = image
    mask_dst = scipy.ndimage.imread('/Users/yu-chieh/Downloads/images_data_crop/02457.jpg', mode='RGB')
    dst = get_facial_points(mask_dst, 30)
    src = get_facial_points(image_src, 30)
    h, status = cv2.findHomography(src, dst)
    im_dst = cv2.warpPerspective(image_src, h, (image_src.shape[1], image_src.shape[0]))
    return im_dst

def test_dumb_fcn_featurizer(test_size, x, train_fcn=False, checkpoint_path=cpstandard):
    """
    ========== Args ==========
      checkpoint_path: Str. Path to `.npy` file containing AlexNet parameters.
       can be found here: `https://github.com/warmspringwinds/tf-image-segmentation/`
      num_channels: Int. number of channels in the input image to be featurized.
       FCN is pretrained with 3 channels.
      train_fcn: Boolean. Whether or not to train the preloaded weights.
      
    ========== Returns ==========
        A featurizer function that takes in a tensor with shape (b, h, w, c) and
        returns a tensor with shape (b, dim).
    """
    size_muliple=32
    num_class=21
    num_channels=3
    image_shape = (None, None, num_channels)  # RGB + Segmentation id
    images = tf.placeholder(tf.uint8, shape=(test_size,) + image_shape)
    preprocessed_images = tf.image.resize_images(images, size=(229, 229))

    # # Be careful: after adaptation, network returns final labels
    # # and not logits
    # with tf.variable_scope("conv_to_channel3"):
    #     filter_m = tf.Variable(tf.random_normal([1,1,num_channels,3]))
    #     preprocessed_images_3_channels = tf.nn.conv2d(preprocessed_images, filter_m, strides=[1, 1, 1, 1], padding='VALID')
    #     shape_of_this = tf.shape(preprocessed_images_3_channels)

    model = adapt_network_for_any_size_input(FCN_16s, size_muliple)
    pred, fcn_16s_variables_mapping = model(image_batch_tensor=preprocessed_images,
                                          number_of_classes=num_class,
                                          is_training=train_fcn)
    binary_pred = tf.nn.sigmoid(tf.cast(pred, tf.float32), name="sigmoid")
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        # restore checkpoint
        saver = tf.train.Saver()
        saver.restore(sess, checkpoint_path)
        # a = sess.run([shape_of_this], feed_dict={images: x})
        # print(a)
        original_imgs, output_masks = sess.run([images, binary_pred], feed_dict={images: x})
        io.imshow(original_imgs[0])
        io.show()
        io.imshow(output_masks[0].squeeze())
        io.show()


image = np.zeros((1, IMAGE_HEIGHT, IMAGE_WIDTH, 6))
image = tf.cast(image, tf.float32)
# # print(imgs.shape)
# # test_dumb_fcn_featurizer(2, imgs)
# model_data = utils.get_model_data(FLAGS.model_dir, MODEL_URL)
# mean = model_data['normalization'][0][0][0]
# mean_pixel = np.mean(mean, axis=(0, 1))
# weights = np.squeeze(model_data['layers'])


# print(inference(image, 0.8))

main()