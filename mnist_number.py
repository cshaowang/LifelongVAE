import os
import h5py
import numpy as np

from copy import deepcopy
from tensorflow.examples.tutorials.mnist import input_data
from itertools import compress
from utils import zip_filter_unzip
from scipy.misc import imrotate as rotate
from scipy.misc import imresize as imresize

# An object that filters MNIST to a single number
class MNIST_Number(object):
    def __init__(self, number, mnist, is_one_vs_all=False,
                 is_flat=True, resize_dims=None,
                 convert_to_rgb=False):
        self.input_size = len(mnist.train.images[0])
        self.number = number  # the number to filter out
        self.is_one_vs_all = is_one_vs_all
        if not is_one_vs_all:
            self.blacklist = list(np.arange(11))
            self.blacklist.remove(self.number)
        else:
            self.blacklist = [1]  # the 'other' class

        # filter out all other numbers
        self.mnist = MNIST_Number.filter_numbers(mnist, self.blacklist)

        # return images in [batch, row, col]
        if not is_flat:
            self.mnist = MNIST_Number._unflatten_mnist(self.mnist)

        # resizes images if resize_dims tuple is provided
        if resize_dims is not None:
            self.mnist = MNIST_Number.resize_mnist(self.mnist, resize_dims)

        # tile images as [img, img, img]
        if convert_to_rgb:
            self.mnist = MNIST_Number.bw_to_rgb_mnist(self.mnist)

    @staticmethod
    def _unflatten_mnist(mnist):
        mnist.train._images = mnist.train._images.reshape([-1, 28, 28])
        mnist.validation._images = mnist.validation._images.reshape([-1, 28, 28])
        mnist.test._images = mnist.test._images.reshape([-1, 28, 28])
        return mnist

    @staticmethod
    def resize_mnist(mnist, new_dims):
        mnist.train._images = MNIST_Number.resize_images(mnist.train._images, new_dims)
        mnist.validation._images = MNIST_Number.resize_images(mnist.validation._images, new_dims)
        mnist.test._images = MNIST_Number.resize_images(mnist.test._images, new_dims)
        return mnist

    @staticmethod
    def bw_to_rgb_mnist(mnist):
        mnist.train._images = MNIST_Number.bw_to_rgb(mnist.train._images)
        mnist.validation._images = MNIST_Number.bw_to_rgb(mnist.validation._images)
        mnist.test._images = MNIST_Number.bw_to_rgb(mnist.test._images)
        return mnist


    @staticmethod
    def resize_images(imgs, new_dims, flatten=False):
        flattened_dims = [-1, np.prod(new_dims)] if flatten else [-1] + new_dims
        return np.vstack([imresize(img.reshape(28, 28),
                                   new_dims).reshape(flattened_dims)
                          for img in imgs]) / 255.

    @staticmethod
    def bw_to_rgb(imgs):
        return np.vstack([np.tile(img.reshape(img.shape[0], imgs.shape[1], 1), 3)
                          .reshape(-1, img.shape[0], img.shape[1], 3)
                          for img in imgs])

    @staticmethod
    def _rotate_batch(batch, angle):
        return np.vstack([rotate(x_i.reshape(28, 28), angle).reshape([-1, 28*28])
                          for x_i in batch]) / 255.

    @staticmethod
    def _check_and_load_angle(angle, number, base_path='MNIST_data'):
        ''' Returns None if the file doesn't exists'''
        filename = os.path.join(base_path, "mnist_num%d_angle%d.hdf5"
                                % (number, angle))
        if os.path.exists(filename):
            f = h5py.File(filename, "r")
            return f['train'][()], f['validation'][()], f['test'][()]
            # return f['train'], f['validation'], f['test']

        return None

    @staticmethod
    def _check_and_write_angle(angle, number, mnist, base_path='MNIST_data'):
        ''' serializes the rotated number to disk as a hdf5 file'''
        filename = os.path.join(base_path, "mnist_num%d_angle%d.hdf5"
                                % (number, angle))
        if not os.path.exists(filename):
            f = h5py.File(filename, "w")
            f['train'] = mnist.train._images
            f['validation'] = mnist.validation._images
            f['test'] = mnist.test._images

            print 'serialized %s to disk...' % filename

    @staticmethod
    def rotate_all_sets(mnist, number, angle):
        hpf5_load = MNIST_Number._check_and_load_angle(angle, number)
        if hpf5_load is not None:
            train_imgs = np.asarray(hpf5_load[0], np.float32)
            validation_imgs = np.asarray(hpf5_load[1], np.float32)
            test_imgs = np.asarray(hpf5_load[2], np.float32)
        else:
            train_imgs = MNIST_Number._rotate_batch(mnist.train._images, angle)
            validation_imgs = MNIST_Number._rotate_batch(mnist.validation._images, angle)
            test_imgs = MNIST_Number._rotate_batch(mnist.test._images, angle)

        mnist.train._images = train_imgs
        mnist.validation._images = validation_imgs
        mnist.test._images = test_imgs

        MNIST_Number._check_and_write_angle(angle, number, mnist)
        return mnist

    @staticmethod
    def filter_numbers(mnist, blacklist):
        digits = deepcopy(mnist)
        digits.train._images, digits.train._labels = zip_filter_unzip(digits.train._images
                                                                      , digits.train._labels
                                                                      , blacklist)
        digits.train._images = np.array(digits.train._images)
        digits.train._labels = np.array(digits.train._labels)
        digits.train._num_examples = len(digits.train.images)
        digits.validation._images, digits.validation._labels = zip_filter_unzip(digits.validation._images
                                                                                , digits.validation._labels
                                                                                , blacklist)
        digits.validation._num_examples = len(digits.validation.images)
        digits.validation._images = np.array(digits.validation._images)
        digits.validation._labels = np.array(digits.validation._labels)
        digits.test._images, digits.test._labels = zip_filter_unzip(digits.test._images
                                                                    , digits.test._labels
                                                                    , blacklist)
        digits.test._images = np.array(digits.test._images)
        digits.test._labels = np.array(digits.test._labels)
        digits.test._num_examples = len(digits.test.images)
        return digits

    # if one vs. all then 0 = true class, 1 = other
    # otherwise we just use lbl = lbl,  10 = other
    def _augment(self, images, labels):
        indexer = np.array(labels == self.number)
        if self.is_one_vs_all:
            return zip(*((im, 0) if ind else (im, 1)
                         for im, lbl, ind in zip(images, labels, indexer)))
        else:
            return zip(*((im, lbl) if ind else (im, 10)
                         for im, lbl, ind in zip(images, labels, indexer)))

    def get_train_batch_iter(self, batch_size):
        images, labels = self.mnist.train.next_batch(batch_size)
        #images, labels = self._augment(images, labels)
        return np.array(images), np.array(labels)

    def get_validation_batch_iter(self, batch_size):
        images, labels = self.mnist.validation.next_batch(batch_size)
        #images, labels = self._augment(images, labels)
        return np.array(images), np.array(labels)

    def _get_test_batch_iter(self, batch_size):
        images, labels = self.mnist.test.next_batch(batch_size)
        images, labels = self._augment(images, labels)
        return np.array(images), np.array(labels)

    def get_test_batch_iter(self, batch_size):
        images = []; labels = []; count = 0
        while(count < batch_size):
            max_batch = self.mnist.test._num_examples
            im, lbl = self._get_test_batch_iter(max_batch)
            tar = 0 if self.is_one_vs_all else self.number
            if tar in lbl:
                im, lbl = zip_filter_unzip(im, lbl, self.blacklist)
                count += len(im)
                #  im = np.asarray(im); lbl = np.asarray(lbl); count += len(lbl)
                images.append(im); labels.append(lbl)

        return np.vstack(images)[0:batch_size], np.hstack(labels)[0:batch_size]

    def get_batch_iter(self, batch_size):
        images = []; labels = []; count = 0
        while(count < batch_size):
            im, lbl = self.get_train_batch_iter(batch_size)
            tar = 0 if self.is_one_vs_all else self.number
            if tar in lbl:
                # im, lbl = zip_filter_unzip(im, lbl, self.blacklist)
                im = np.asarray(im); lbl = np.asarray(lbl); count += len(lbl)
                images.append(im); labels.append(lbl)

        return np.vstack(images)[0:batch_size], np.hstack(labels)[0:batch_size]


class AllMnist():
    def __init__(self, one_hot=True,
                 is_flat=True,
                 resize_dims=None,
                 convert_to_rgb=False):
        self.mnist = input_data.read_data_sets('MNIST_data', one_hot=one_hot)
        self.one_hot = one_hot
        self.number = 99997 # XXX
        self.num_examples = self.mnist.test._num_examples

        # return images in [batch, row, col]
        if not is_flat:
            self.mnist = MNIST_Number._unflatten_mnist(self.mnist)

        # resizes images if resize_dims tuple is provided
        if resize_dims is not None:
            self.mnist = MNIST_Number.resize_mnist(self.mnist, resize_dims)

        # tile images as [img, img, img]
        if convert_to_rgb:
            self.mnist = MNIST_Number.bw_to_rgb_mnist(self.mnist)

    def get_train_batch_iter(self, batch_size):
        images, labels = self.mnist.train.next_batch(batch_size)
        #images, labels = self._augment(images, labels)
        return np.array(images), np.array(labels)

    def get_validation_batch_iter(self, batch_size):
        images, labels = self.mnist.validation.next_batch(batch_size)
        #images, labels = self._augment(images, labels)
        return np.array(images), np.array(labels)

    def _get_test_batch_iter(self, batch_size):
        images, labels = self.mnist.test.next_batch(batch_size)
        images, labels = self._augment(images, labels)
        return np.array(images), np.array(labels)

    def get_test_batch_iter(self, batch_size):
        return self._get_test_batch_iter(batch_size)

    def get_batch_iter(self, batch_size):
        return self.get_train_batch_iter(batch_size)


# Read mnist only once [~ 230Mb]
full_mnist = input_data.read_data_sets('MNIST_data', one_hot=False)
# full_mnist.train._images /= 255.
# full_mnist.validation._images /= 255.
# full_mnist.test._images /= 255.
