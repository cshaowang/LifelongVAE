import os
import sys
import datetime
import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim
import tensorflow.contrib.distributions as distributions
# from tensorflow.python.training.moving_averages import weighted_moving_average
from reparameterizations import gumbel_reparmeterization, gaussian_reparmeterization
from encoders import forward, DenseEncoder, CNNEncoder
from decoders import CNNDecoder
from utils import *

sg = tf.contrib.bayesflow.stochastic_graph
st = tf.contrib.bayesflow.stochastic_tensor
sys.setrecursionlimit(200)

# Global variables
GLOBAL_ITER = 0  # keeps track of the iteration ACROSS models
TRAIN_ITER = 0  # the iteration of the current model


class VanillaVAE(object):
    """ Vanilla VAE that can be discrete or continuous

    See "Auto-Encoding Variational Bayes" by Kingma and Welling
    for more details on the original work.

    Note: vae_tm1, num_discrete and submodel are not used. Provided for compatibility
          with online version

    """
    def __init__(self, sess, input_size, batch_size, latent_size,
                 encoder, decoder, is_training, activation=tf.nn.elu,
                 reconstr_loss_type="binary_cross_entropy",
                 reparam_type="continuous",
                 vae_tm1=None, submodel=None,  # XXX
                 learning_rate=1e-3, base_dir=".",
                 mutual_info_reg=0.0, discrete_size=None):
        self.activation = activation
        self.learning_rate = learning_rate
        self.is_training = is_training
        self.encoder_model = encoder
        self.decoder_model = decoder
        self.global_iter_base = GLOBAL_ITER
        self.input_size = input_size
        self.latent_size = latent_size
        self.batch_size = batch_size
        self.iteration = 0
        self.reconstr_loss_type = reconstr_loss_type
        self.reparam_type = reparam_type
        self.base_dir = base_dir  # dump all our stuff into this dir

        # gumbel params
        self.tau0 = 1.0
        self.tau_host = self.tau0
        self.anneal_rate = 0.00003
        # self.anneal_rate = 0.0003 #1e-5
        self.min_temp = 0.5

        # sess & graph
        self.sess = sess
        # self.graph = tf.Graph()

        # create these in scope
        self._create_variables()

        # Create autoencoder network
        self._create_network()

        # Define loss function based variational upper-bound and
        # corresponding optimizer
        self._create_loss_optimizer()

        # create the required directories to hold data for this specific model
        self._create_local_directories()

        # Create all the summaries and their corresponding ops
        self._create_summaries()

        # Check for NaN's
        # self.check_op = tf.add_check_numerics_ops()

        # collect variables & build saver
        self.vae_vars = [v for v in tf.global_variables()
                         if v.name.startswith(self.get_name())]
        self.vae_local_vars = [v for v in tf.local_variables()
                               if v.name.startswith(self.get_name())]
        self.saver = tf.train.Saver(tf.global_variables())  # XXX: use local
        self.init_op = tf.variables_initializer(self.vae_vars
                                                + self.vae_local_vars)

        print 'model: ', self.get_name()
        # print 'there are ', len(self.vae_vars), ' vars in ', \
        #     tf.get_variable_scope().name, ' out of a total of ', \
        #     len(tf.global_variables()), ' with %d total trainable vars' \
        #     % len(tf.trainable_variables())

    '''
    Helper to create the :
         1) models/name_time directory
         2) imgs/name_time directory
         3) logs/name_time directory
    '''
    def _create_local_directories(self):
        models_dir = '%s/models' % (self.base_dir)
        if not os.path.exists(models_dir):
            os.makedirs(models_dir)

        imgs_dir = '%s/imgs' % (self.base_dir)
        if not os.path.exists(imgs_dir):
            os.makedirs(imgs_dir)

        logs_dir = '%s/logs' % (self.base_dir)
        if not os.path.exists(logs_dir):
            os.makedirs(logs_dir)

    def _create_variables(self):
        with tf.variable_scope(self.get_name()):
            self.x = tf.placeholder(tf.float32, shape=[self.batch_size,
                                                       self.input_size],
                                    name="input_placeholder")

            # gpu iteration count
            self.iteration_gpu = tf.Variable(0.0, trainable=False)
            self.iteration_gpu_op = self.iteration_gpu.assign_add(1.0)

            # gumbel related
            self.tau = tf.Variable(5.0, trainable=False, dtype=tf.float32,
                                   name="temperature")
            # self.ema = tf.train.ExponentialMovingAverage(decay=0.9999)

    '''
    A helper function to create all the summaries.
    Adds things like image_summary, histogram_summary, etc.
    '''
    def _create_summaries(self):
        # Summaries and saver
        summaries = [tf.summary.scalar("vae_loss_mean", self.cost_mean),
                     tf.summary.scalar("vae_latent_loss_mean", self.latent_loss_mean),
                     tf.summary.scalar("vae_grad_norm", self.grad_norm),
                     tf.summary.scalar("vae_selected_class", tf.argmax(tf.reduce_sum(self.z, 0), 0)),
                     tf.summary.histogram("vae_latent_dist", self.latent_kl),
                     tf.summary.scalar("vae_latent_loss_max", tf.reduce_max(self.latent_kl)),
                     tf.summary.scalar("vae_latent_loss_min", tf.reduce_min(self.latent_kl)),
                     tf.summary.scalar("vae_reconstr_loss_mean", self.reconstr_loss_mean),
                     tf.summary.scalar("vae_reconstr_loss_max", tf.reduce_max(self.reconstr_loss)),
                     tf.summary.scalar("vae_reconstr_loss_min", tf.reduce_min(self.reconstr_loss)),
                     tf.summary.histogram("z_dist", self.z)]

        # Display image summaries : i.e. samples from P(X|Z=z_i)
        # Visualize:
        #           1) original images[current distribution]
        #           2) reconstructed images
        img_shp = [self.batch_size, 28, 28, 1]
        image_summaries = [tf.summary.image("x_t", tf.reshape(self.x, img_shp),
                                            max_outputs=self.batch_size),
                           tf.summary.image("x_reconstr_mean_activ_t",
                                            tf.reshape(self.x_reconstr_mean_activ, img_shp),
                                            max_outputs=self.batch_size)]

        # Merge all the summaries, but ensure we are post-activation
        # keep the image summaries separate, but also include the regular
        # summaries in them
        with tf.control_dependencies([self.x_reconstr_mean_activ]):
            self.summaries = tf.summary.merge(summaries)
            self.image_summaries = tf.summary.merge(image_summaries
                                                    + summaries)

        # Write all summaries to logs, but VARY the model name AND add a TIMESTAMP
        # current_summary_name = self.get_name() + self.get_formatted_datetime()
        self.summary_writer = tf.summary.FileWriter("%s/logs" % self.base_dir,
                                                    self.sess.graph,
                                                    flush_secs=60)

    '''
    A helper function to format the name as a function of the hyper-parameters
    '''
    def get_name(self):
        full_hash_str = self.activation.__name__ \
                        + '_enc' + str(self.encoder_model.get_sizing()) \
                        + '_dec' + str(self.decoder_model.get_sizing()) \
                        + "_learningrate" + str(self.learning_rate) \
                        + "_latent size" + str(self.latent_size)
        full_hash_str = full_hash_str.strip().lower().replace('[', '')  \
                                                     .replace(']', '')  \
                                                     .replace(' ', '')  \
                                                     .replace('{', '') \
                                                     .replace('}', '') \
                                                     .replace(',', '_') \
                                                     .replace(':', '') \
                                                     .replace('\'', '')
        return 'vae0_' + full_hash_str

    def get_formatted_datetime(self):
        return str(datetime.datetime.now()).replace(" ", "_") \
                                           .replace("-", "_") \
                                           .replace(":", "_")

    def save(self):
        model_filename = "%s/models/%s.cpkt" % (self.base_dir, self.get_name())
        print 'saving vae model to %s...' % model_filename
        self.saver.save(self.sess, model_filename)

    def restore(self):
        model_filename = "%s/models/%s.cpkt" % (self.base_dir, self.get_name())
        print 'into restore, model name = ', model_filename
        if os.path.isfile(model_filename):
            print 'restoring vae model from %s...' % model_filename
            self.saver.restore(self.sess, model_filename)

    @staticmethod
    def kl_categorical(p=None, q=None, p_logits=None, q_logits=None, eps=1e-6):
        '''
        Given p and q (as EITHER BOTH logits or softmax's)
        then this func returns the KL between them.

        Utilizes an eps in order to resolve divide by zero / log issues
        '''
        if p_logits is not None and q_logits is not None:
            Q = distributions.Categorical(logits=q_logits, dtype=tf.float32)
            P = distributions.Categorical(logits=p_logits, dtype=tf.float32)
        elif p is not None and q is not None:
            print 'p shp = ', p.get_shape().as_list(), \
                ' | q shp = ', q.get_shape().as_list()
            Q = distributions.Categorical(probs=q+eps, dtype=tf.float32)
            P = distributions.Categorical(probs=p+eps, dtype=tf.float32)
        else:
            raise Exception("please provide either logits or dists")

        return distributions.kl(P, Q)

    @staticmethod
    def reparameterize(encoded, reparam_type, tau, hard=False,
                       rnd_sample=None, eps=1e-20):
        if reparam_type == "discrete":
            # we reparameterize using both the N(0, I) and the gumbel(0, 1)
            z, kl = gumbel_reparmeterization(encoded,
                                             tau,
                                             rnd_sample,
                                             hard)
        else:
            z, kl = gaussian_reparmeterization(encoded)

        return [slim.flatten(z), kl]

    def encoder(self, X, rnd_sample=None, reuse=False, hard=False):
        with tf.variable_scope(self.get_name() + "/encoder", reuse=reuse):
            encoded = forward(X, self.encoder_model)
            return VanillaVAE.reparameterize(encoded, self.reparam_type,
                                             self.tau, hard=hard,
                                             rnd_sample=rnd_sample)

    def generator(self, Z, reuse=False):
        with tf.variable_scope(self.get_name() + "/generator", reuse=reuse):
            print 'generator scope: ', tf.get_variable_scope().name
            # Use generator to determine mean of
            # Bernoulli distribution of reconstructed input
            # print 'batch norm for decoder: ', use_ln
            return forward(Z, self.decoder_model)

    def generate_at_least(self, vae_tm1, batch_size):
        # Returns :
        # 1) a categorical and a Normal distribution concatenated
        # 2) x_hat_tm1 : the reconstructed data from the old model
        print 'generating data from previous #discrete: ', vae_tm1.num_discrete
        z_cat = generate_random_categorical(vae_tm1.num_discrete,
                                            batch_size)
        z_normal = tf.random_normal([batch_size, vae_tm1.latent_size])
        z = tf.concat([z_normal, z_cat], axis=1)
        zshp = z.get_shape().as_list()  # TODO: debug trace
        print 'z_generated = ', zshp

        # Generate reconstructions of historical Z's
        xr = tf.stop_gradient(tf.nn.sigmoid(vae_tm1.generator(z, reuse=True)))
        print 'xhat internal shp = ', xr.get_shape().as_list()  # TODO: debug

        return [z, z_cat, xr]

    def _generate_vae_tm1_data(self):
        if self.vae_tm1 is not None:
            num_instances = self.x.get_shape().as_list()[0]
            self.num_current_data = int((1.0/(self.submodel + 1.0))
                                        * float(num_instances))
            self.num_old_data = num_instances - self.num_current_data
            # TODO: Remove debug trace
            print 'total instances: %d | current_model: %d | current data number: %d | old data number: %d' \
                % (num_instances, self.submodel, self.num_current_data, self.num_old_data)

            if self.num_old_data > 0:  # make sure we aren't in base case
                # generate data by randomly sampling a categorical for
                # N-1 positions; also sample a N(0, I) in order to
                # generate variability
                self.z_tm1, self.z_discrete_tm1, self.xhat_tm1 \
                    = self.generate_at_least(self.vae_tm1,
                                             self.batch_size)

                print 'z_tm1 = ', self.z_tm1.get_shape().as_list(), \
                    '| xhat_tm1 = ', self.xhat_tm1.get_shape().as_list()

    @staticmethod
    def _z_to_one_hot(z, latent_size):
        indices = tf.arg_max(z, 1)
        return tf.one_hot(indices, latent_size, dtype=tf.float32)

    '''
    Helper op to create the network structure
    '''
    def _create_network(self, num_test_memories=10):
        self.num_current_data = self.x.get_shape().as_list()[0]

        # run the encoder operation
        self.z, self.kl = self.encoder(self.x)
        print 'z_encoded = ', self.z.get_shape().as_list()

        # reconstruct x via the generator & run activation
        self.x_reconstr_mean = self.generator(self.z)
        self.x_reconstr_mean_activ = tf.nn.sigmoid(self.x_reconstr_mean)
        # self.x_reconstr = distributions.Bernoulli(logits=self.x_reconstr_logits)
        # self.x_reconstr_mean_activ = self.x_reconstr.mean()

    def _loss_helper(self, truth, pred):
        if self.reconstr_loss_type == "binary_cross_entropy":
            loss = self._cross_entropy(truth, pred)
        else:
            loss = self._l2_loss(truth, pred)

        return tf.reduce_sum(loss, 1)

    @staticmethod
    def _cross_entropy(x, x_reconstr):
        # To ensure stability and avoid overflow, the implementation uses
        # max(x, 0) - x * z + log(1 + exp(-abs(x)))
        # return tf.maximum(x, 0) - x * z + tf.log(1.0 + tf.exp(-tf.abs(x)))
        return tf.nn.sigmoid_cross_entropy_with_logits(logits=x_reconstr,
                                                       labels=x)

    @staticmethod
    def _l2_loss(x, x_reconstr):
        return tf.square(x - x_reconstr)

    def vae_loss(self, x, x_reconstr_mean, latent_kl):
        # the loss is composed of two terms:
        # 1.) the reconstruction loss (the negative log probability
        #     of the input under the reconstructed bernoulli distribution
        #     induced by the decoder in the data space).
        #     this can be interpreted as the number of "nats" required
        #     for reconstructing the input when the activation in latent
        #     is given.
        # reconstr_loss = tf.reduce_sum(x_reconstr_mean.log_pmf(x), [1])
        reconstr_loss = self._loss_helper(x, x_reconstr_mean)

        # 2.) the latent loss, which is defined as the kullback leibler divergence
        #     between the distribution in latent space induced by the encoder on
        #     the data and some prior. this acts as a kind of regularizer.
        #     this can be interpreted as the number of "nats" required
        #     for transmitting the the latent space distribution given
        #     the prior.
        # kl_categorical(p=none, q=none, p_logits=none, q_logits=none, eps=1e-6):
        # cost = reconstr_loss - latent_kl
        cost = reconstr_loss + latent_kl

        # create the reductions only once
        latent_loss_mean = tf.reduce_mean(latent_kl)
        reconstr_loss_mean = tf.reduce_mean(reconstr_loss)
        cost_mean = tf.reduce_mean(cost)

        return [reconstr_loss, reconstr_loss_mean,
                latent_loss_mean, cost, cost_mean]

    def _create_loss_optimizer(self):
        with tf.variable_scope(self.get_name() + "/loss_optimizer"):
            self.latent_kl = self.kl

            # tabulate total loss
            self.reconstr_loss, self.reconstr_loss_mean, \
                self.latent_loss_mean, \
                self.cost, self.cost_mean \
                = self.vae_loss(self.x,
                                self.x_reconstr_mean,
                                self.latent_kl)

            # construct our optimizer
            with tf.control_dependencies([self.x_reconstr_mean_activ]):
                filtered = [v for v in tf.trainable_variables()
                            if v.name.startswith(self.get_name())]
                self.optimizer = self._create_optimizer(filtered,
                                                        self.cost_mean,
                                                        self.learning_rate)

    def _create_optimizer(self, tvars, cost, lr):
        # optimizer = tf.train.rmspropoptimizer(self.learning_rate)
        optimizer = tf.train.AdamOptimizer(learning_rate=lr)

        print 'there are %d trainable vars in cost %s\n' % (len(tvars), cost.name)
        grads = tf.gradients(cost, tvars)

        # DEBUG: exploding gradients test with this:
        # for index in range(len(grads)):
        #     if grads[index] is not None:
        #         gradstr = "\n grad [%i] | tvar [%s] =" % (index, tvars[index].name)
        #         grads[index] = tf.Print(grads[index], [grads[index]], gradstr, summarize=100)

        # grads, _ = tf.clip_by_global_norm(grads, 5.0)
        self.grad_norm = tf.norm(tf.concat([tf.reshape(t, [-1]) for t in grads],
                                           axis=0))
        return optimizer.apply_gradients(zip(grads, tvars))
        # return tf.train.AdamOptimizer(learning_rate=lr).minimize(cost, var_list=tvars)

    def partial_fit(self, inputs, iteration_print=10,
                    iteration_save_imgs=2000,
                    is_forked=False):
        """Train model based on mini-batch of input data.

        Return cost of mini-batch.
        """

        feed_dict = {self.x: inputs,
                     self.is_training: True,
                     self.tau: self.tau_host}

        try:
            if self.reparam_type == 'discrete' \
               and self.iteration > 0 and self.iteration % 10 == 0:
                rate = -self.anneal_rate*self.iteration
                self.tau_host = np.maximum(self.tau0 * np.exp(rate),
                                           self.min_temp)
                print 'updated tau to ', self.tau_host

            ops_to_run = [self.optimizer, self.iteration_gpu_op,
                          self.cost_mean, self.reconstr_loss_mean,
                          self.latent_loss_mean]

            if self.iteration % iteration_save_imgs == 0:
                # write images + summaries
                _, _, cost, rloss, lloss, summary_img \
                    = self.sess.run(ops_to_run + [self.image_summaries],
                                    feed_dict=feed_dict)

                self.summary_writer.add_summary(summary_img, self.iteration)
            elif self.iteration % iteration_print == 0:
                # write regular summaries
                _, _, cost, rloss, lloss, summary \
                    = self.sess.run(ops_to_run + [self.summaries],
                                    feed_dict=feed_dict)

                self.summary_writer.add_summary(summary, self.iteration)
            else:
                # write no summary
                _, _, cost, rloss, lloss \
                    = self.sess.run(ops_to_run,
                                    feed_dict=feed_dict)

        except Exception as e:
            print 'caught exception in partial fit: ', e

        self.iteration += 1
        return cost, rloss, lloss

    def write_classes_to_file(self, filename, all_classes):
        with open(filename, 'a') as f:
            np.savetxt(f, self.sess.run(all_classes), delimiter=",")

    def transform(self, X):
        """Transform data by mapping it into the latent space."""
        # Note: This maps to mean of distribution, we could alternatively
        # sample from Gaussian distribution
        return self.sess.run(self.z, feed_dict={self.x: X,
                                                self.tau: self.tau_host,
                                                self.is_training: False})

    def generate(self, z=None):
        """ Generate data by sampling from latent space.

        If z_mu is not None, data for this point in latent space is
        generated. Otherwise, z_mu is drawn from prior in latent
        space.
        """
        if z is None:
            z = generate_random_categorical(self.latent_size, self.batch_size)

        # Note: This maps to mean of distribution, we could alternatively
        # sample from Gaussian distribution
        return self.sess.run(self.x_reconstr_mean_activ,
                             feed_dict={self.z: z,
                                        self.tau: self.tau_host,
                                        self.is_training: False})

    def reconstruct(self, X, return_losses=False):
        """ Use VAE to reconstruct given data. """
        if return_losses:
            ops = [self.x_reconstr_mean_activ,
                   self.reconstr_loss, self.reconstr_loss_mean,
                   self.latent_kl, self.latent_loss_mean,
                   self.cost, self.cost_mean, self.cost_mean]
        else:
            ops = self.x_reconstr_mean_activ

        return self.sess.run(ops,
                             feed_dict={self.x: X,
                                        self.tau: self.tau_host,
                                        self.is_training: False})

    def train(self, source, batch_size, training_epochs=10, display_step=5):
        n_samples = source.train.num_examples
        for epoch in range(training_epochs):
            avg_cost = 0.
            total_batch = int(n_samples / batch_size)
            # Loop over all batches
            for i in range(total_batch):
                batch_xs, _ = source.train.next_batch(batch_size)

                # Fit training using batch data
                cost, recon_cost, latent_cost = self.partial_fit(batch_xs)
                # Compute average loss
                avg_cost += cost / n_samples * batch_size

            # Display logs per epoch step
            if epoch % display_step == 0:
                print "[Epoch:", '%04d]' % (epoch+1), \
                    "current cost = ", "{:.4f} | ".format(cost), \
                    "avg cost = ", "{:.4f} | ".format(avg_cost), \
                    "latent cost = ", "{:.4f} | ".format(latent_cost), \
                    "recon cost = ", "{:.4f}".format(recon_cost)
